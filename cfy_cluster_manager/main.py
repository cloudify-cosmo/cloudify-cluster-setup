import os
import sys
import time
import shutil
import string
import random
import argparse
from getpass import getuser
from traceback import format_exception
from collections import OrderedDict
from os.path import (basename, dirname, exists, expanduser, isdir, join,
                     splitext)

from jinja2 import Environment, FileSystemLoader

from .logger import get_cfy_cluster_manager_logger, setup_logger
from .utils import (check_cert_key_match, check_cert_path, check_san,
                    check_signed_by, cloudify_rpm_is_installed,
                    ClusterInstallError, copy, current_host_ip,
                    raise_errors_list, get_dict_from_yaml, move, run,
                    sudo, VM, write_dict_to_yaml_file, yum_is_present)

logger = get_cfy_cluster_manager_logger()

CERTS_DIR_NAME = 'certs'
CFY_CERTS_PATH = '{0}/.cloudify-test-ca'.format(expanduser('~'))
CONFIG_FILES = 'config_files'
DIR_NAME = 'cloudify_cluster_manager'
RPM_NAME = 'cloudify-manager-install.rpm'
TOP_DIR = '/tmp'

RPM_PATH = join(TOP_DIR, RPM_NAME)
CLUSTER_INSTALL_DIR = join(TOP_DIR, DIR_NAME)
CERTS_DIR = join(CLUSTER_INSTALL_DIR, CERTS_DIR_NAME)
CONFIG_FILES_DIR = join(CLUSTER_INSTALL_DIR, CONFIG_FILES)

CA_PATH = join(CERTS_DIR, 'ca.pem')
EXTERNAL_DB_CA_PATH = join(CERTS_DIR, 'external_db_ca.pem')

CREDENTIALS_FILE_PATH = join(os.getcwd(), 'secret_credentials.yaml')
CLUSTER_CONFIG_FILES_DIR = join(dirname(__file__), 'cfy_cluster_config_files')
CLUSTER_CONFIG_FILE_NAME = 'cfy_cluster_config.yaml'
CLUSTER_INSTALL_CONFIG_PATH = join(os.getcwd(), CLUSTER_CONFIG_FILE_NAME)

SYSTEMD_RUN_UNIT_NAME = 'cfy_cluster_manager_{}'
BASE_CFY_DIR = '/etc/cloudify/'
INITIAL_INSTALL_DIR = join(BASE_CFY_DIR + '.installed')


class CfyNode(VM):
    def __init__(self,
                 private_ip,
                 public_ip,
                 key_file_path,
                 username,
                 node_name,
                 hostname,
                 cert_path,
                 key_path):
        super(CfyNode, self).__init__(private_ip, public_ip,
                                      key_file_path, username)
        self.name = node_name
        self.hostname = hostname
        self.cert_path = cert_path
        self.key_path = key_path
        self.type = node_name.split('-')[0]
        self.installed = False
        self.config_path = join(
            BASE_CFY_DIR, '{}_config.yaml'.format(node_name))
        self.unit_name = SYSTEMD_RUN_UNIT_NAME.format(self.type)


def _exception_handler(type_, value, traceback):
    error = type_.__name__
    if str(value):
        error = '{0}: {1}'.format(error, str(value))
    logger.error(error)
    debug_traceback = ''.join(format_exception(type_, value, traceback))
    logger.debug(debug_traceback)


sys.excepthook = _exception_handler


def _generate_instance_certificate(instance):
    run(['cfy_manager', 'generate-test-cert', '-s',
         instance.private_ip + ',' + instance.public_ip])
    move(join(CFY_CERTS_PATH, instance.private_ip + '.crt'),
         join(CFY_CERTS_PATH, instance.name + '_cert.pem'))
    new_key_path = join(CFY_CERTS_PATH, instance.name + '_key.pem')
    move(join(CFY_CERTS_PATH, instance.private_ip + '.key'), new_key_path)
    sudo(['chmod', '444', new_key_path])


def _generate_certs(instances_dict):
    logger.info('Generating certificates')
    for instances_list in instances_dict.values():
        for instance in instances_list:
            _generate_instance_certificate(instance)
    copy(join(CFY_CERTS_PATH, 'ca.crt'), join(CFY_CERTS_PATH, 'ca.pem'))
    if not exists(CERTS_DIR):
        os.mkdir(CERTS_DIR)
    copy(join(CFY_CERTS_PATH, '.'), CERTS_DIR)
    shutil.rmtree(CFY_CERTS_PATH)


def _get_postgresql_cluster_members(postgresql_instances):
    return {
        postgresql_instances[j].name:
            {'ip': postgresql_instances[j].private_ip}
        for j in range(len(postgresql_instances))}


def _get_rabbitmq_cluster_members(rabbitmq_instances):
    return {
        rabbitmq_instances[j].name: {
            'networks': {'default': rabbitmq_instances[j].private_ip}
        } for j in range(len(rabbitmq_instances))}


def _prepare_postgresql_config_files(template,
                                     postgresql_instances,
                                     credentials):
    logger.info('Preparing PostgreSQL config files')
    postgresql_cluster = _get_postgresql_cluster_members(postgresql_instances)
    for node in postgresql_instances:
        rendered_data = template.render(node=node,
                                        creds=credentials,
                                        ca_path=CA_PATH,
                                        postgresql_cluster=postgresql_cluster)
        _create_config_file(rendered_data, node.name)


def _prepare_rabbitmq_config_files(template, rabbitmq_instances, credentials):
    logger.info('Preparing RabbitMQ config files')
    rabbitmq_cluster = _get_rabbitmq_cluster_members(rabbitmq_instances)
    for i, node in enumerate(rabbitmq_instances):
        join_cluster = rabbitmq_instances[0].name if i > 0 else None
        rendered_data = template.render(node=node,
                                        creds=credentials,
                                        ca_path=CA_PATH,
                                        join_cluster=join_cluster,
                                        rabbitmq_cluster=rabbitmq_cluster)
        _create_config_file(rendered_data, node.name)


def _prepare_manager_config_files(template,
                                  instances_dict,
                                  credentials,
                                  load_balancer_ip,
                                  external_db_config):
    logger.info('Preparing Manager config files')
    if external_db_config:
        external_db_config.update({'ssl_client_verification': False,
                                   'ca_path': EXTERNAL_DB_CA_PATH})

    for node in instances_dict['manager']:
        rendered_data = template.render(
            node=node,
            creds=credentials,
            ca_path=CA_PATH,
            license_path=join(CLUSTER_INSTALL_DIR, 'license.yaml'),
            load_balancer_ip=load_balancer_ip,
            rabbitmq_cluster=_get_rabbitmq_cluster_members(
                instances_dict['rabbitmq']),
            postgresql_cluster={} if external_db_config else
            _get_postgresql_cluster_members(instances_dict['postgresql']),
            external_db_configuration=external_db_config
        )

        _create_config_file(rendered_data, node.name)


def _create_config_file(rendered_data, node_name):
    config_path = join(CONFIG_FILES_DIR, '{0}_config.yaml'.format(node_name))
    with open(config_path, 'w') as config_file:
        config_file.write(rendered_data)


def _prepare_config_files(instances_dict,
                          credentials,
                          load_balancer_ip,
                          external_db_config):
    os.mkdir(join(CLUSTER_INSTALL_DIR, 'config_files'))
    templates_env = Environment(
        loader=FileSystemLoader(
            join(dirname(__file__), 'config_files_templates')))

    if not external_db_config:
        _prepare_postgresql_config_files(
            templates_env.get_template('postgresql_config.yaml'),
            instances_dict['postgresql'],
            credentials)

    _prepare_rabbitmq_config_files(
        templates_env.get_template('rabbitmq_config.yaml'),
        instances_dict['rabbitmq'],
        credentials)

    _prepare_manager_config_files(
        templates_env.get_template('manager_config.yaml'),
        instances_dict,
        credentials,
        load_balancer_ip,
        external_db_config
    )


def _install_cloudify_remotely(instance, rpm_download_link):
    logger.info('Downloading Cloudify RPM on %s from %s',
                instance.name, rpm_download_link)
    instance.run_command('curl -o {0} {1}'.format(
        RPM_PATH, rpm_download_link), hide_stdout=True)
    logger.info('Installing Cloudify RPM on %s', instance.name)
    instance.run_command(
        'yum install -y {}'.format(RPM_PATH), use_sudo=True, hide_stdout=True)


def _get_service_status_code(instance):
    """Checking the status code of the cfy_manager_install_<type> service."""
    result = instance.run_command(
        'systemctl status {}'.format(instance.unit_name),
        use_sudo=True, hide_stdout=True, ignore_failure=True)
    return result.return_code


def _wait_for_cloudify_current_installation(instance):
    logger.info(
        'Waiting for current installation of %s to finish', instance.name)
    status_code = 0
    retry_count = 0
    while status_code == 0 and retry_count <= 600:
        if retry_count % 20 == 0:
            logger.info('Waiting for current installation of %s to finish',
                        instance.name)
        time.sleep(1)
        retry_count += 1
        status_code = _get_service_status_code(instance)

    if retry_count > 600:
        raise ClusterInstallError(
            'Got a time out while waiting for the current installation of '
            '%s to finish', instance.name)


def _rpm_was_installed(instance, rpm_download_link, override=False):
    rpm_name = splitext(basename(rpm_download_link))[0]
    logger.debug(
        'Checking if Cloudify RPM was installed on %s', instance.private_ip)
    result = instance.run_command('rpm -qi cloudify-manager-install',
                                  hide_stdout=True, ignore_failure=True)
    if result.failed:
        return False

    if result.stdout.strip() == rpm_name or override:
        return True
    else:
        raise ClusterInstallError(
            'Cloudify RPM is already installed with a different version '
            'on %s. Please uninstall it first', instance.private_ip)


def _verify_service_installed(instance):
    """Checking if the instance type .installed file was created."""
    logger.info('Verifying that %s (%s) was installed successfully',
                instance.name, instance.private_ip)
    names_mapping = {'postgresql': 'database_service',
                     'rabbitmq': 'queue_service',
                     'manager': 'manager_service'}
    return instance.file_exists(
        join(INITIAL_INSTALL_DIR, names_mapping[instance.type]))


def _cloudify_was_previously_installed_successfully(instance, verbose):
    """Checking if the previous installation finished successfully.

    We can check if the previous installation of Cloudify on this instance
    finished successfully by inspecting the status of the
    `cfy_manager_install_<type>` service:
        status_code == 0: The `cfy_manager install` command is currently
                          running on the instance, so we need to wait until
                          it finishes, and then check if it succeeded.
        status_code == 3: The service failed, so we need to remove it and the
                          failed Cloudify installation, and return False.
        status_code == 4: The service is not present. It can be either
                          because the installation finished successfully
                          or because it was never running. This is checked by
                          `_verify_service_installed`.
        Any other code is not being taken care of.
    """
    status_code = _get_service_status_code(instance)
    if status_code == 0:
        _wait_for_cloudify_current_installation(instance)
        return _verify_cloudify_installed_successfully(instance)
    elif status_code == 3:
        logger.info(
            'Previous Cloudify installation of %s failed', instance.name)
        logger.info('Removing failed Cloudify installation')
        instance.run_command(
            'cfy_manager remove -c {config_path} {verbose}'.format(
                config_path=instance.config_path,
                verbose='-v' if verbose else ''))
        instance.run_command(
            'systemctl reset-failed {}'.format(instance.unit_name),
            use_sudo=True, hide_stdout=True)
        return False
    elif status_code == 4:
        return _verify_service_installed(instance)
    else:
        raise ClusterInstallError(
            'Service {} status is unknown'.format(instance.unit_name))


def _verify_cloudify_installed_successfully(instance):
    status_code = _get_service_status_code(instance)
    if status_code == 3:
        raise ClusterInstallError(
            'Failed installing Cloudify on instance {}.'.format(
                instance.private_ip))
    elif status_code == 4:
        return _verify_service_installed(instance)
    else:
        raise ClusterInstallError(
            'Service {} status is unknown'.format(instance.unit_name))


def _install_instances(instances_dict,
                       using_three_nodes,
                       rpm_download_link,
                       verbose):
    for i, instance_type in enumerate(instances_dict):
        logger.info('installing %s instances', instance_type)
        three_nodes_not_first_round = using_three_nodes and i > 0
        for instance in instances_dict[instance_type]:
            if instance.installed:
                logger.info('Already installed %s (%s)',
                            instance.name, instance.private_ip)
                continue

            logger.info('Installing %s', instance.name)
            if (instance.private_ip == current_host_ip() or
                    three_nodes_not_first_round):
                logger.info('Already prepared the cluster install files on '
                            'this instance')
            else:
                logger.info('Copying the %s directory to %s',
                            CLUSTER_INSTALL_DIR, instance.name)
                instance.put_dir(CLUSTER_INSTALL_DIR,
                                 CLUSTER_INSTALL_DIR,
                                 override=True)
                if not _rpm_was_installed(instance, rpm_download_link):
                    _install_cloudify_remotely(instance, rpm_download_link)

            instance.run_command('cp {0} {1}'.format(
                join(CONFIG_FILES_DIR, '{}_config.yaml'.format(instance.name)),
                instance.config_path), use_sudo=True)

            install_cmd = (
                'systemd-run -t --unit {unit_name} --uid {user_name} '
                'cfy_manager install -c {config} {verbose}'.format(
                    config=instance.config_path, unit_name=instance.unit_name,
                    user_name=getuser(), verbose='-v' if verbose else ''))

            instance.run_command(install_cmd, use_sudo=True)
            _verify_cloudify_installed_successfully(instance)


def _sort_instances_dict(instances_dict):
    for _, instance_items in instances_dict.items():
        if len(instance_items) > 1:
            instance_items.sort(key=lambda x: int(x.name.rsplit('-', 1)[1]))


def _using_provided_certificates(config):
    return config.get('ca_cert_path')


def _get_external_db_config(config):
    return config.get('external_db_configuration')


def _get_cfy_node(config, node_dict, node_name, validate_connection=True):
    username = config.get('machine_username')
    cert_path = join(CERTS_DIR, node_name + '_cert.pem')
    key_path = join(CERTS_DIR, node_name + '_key.pem')
    if _using_provided_certificates(config):
        copy(expanduser(node_dict.get('cert_path')), cert_path)
        copy(expanduser(node_dict.get('key_path')), key_path)

    public_ip = node_dict.get('public_ip') or node_dict.get('private_ip')
    new_vm = CfyNode(node_dict.get('private_ip'),
                     public_ip,
                     config.get('key_file_path'),
                     username,
                     node_name,
                     node_dict.get('hostname'),
                     cert_path,
                     key_path)
    if validate_connection:
        logger.debug('Testing connection to %s', new_vm.private_ip)
        new_vm.test_connection()

    return new_vm


def _get_instances_ordered_dict(config):
    """Return the instances ordered dictionary template.

    After being filled by `_generate_three_nodes_cluster_dict` or
    `_generate_general_cluster_dict`, the instances ordered dictionary
    will have the following form:
        (('postgresql': [postgresql-1, postgresql-2, postgresql-3]),
         ('rabbitmq': [rabbitmq-1, rabbitmq-2, rabbitmq-3]),
         ('manager': [manager-1, manager-2, manager-3]))
    If using an external DB, the 'postgresql' key is popped.
    """
    instances_dict = OrderedDict(
        (('postgresql', []), ('rabbitmq', []), ('manager', [])))
    if _get_external_db_config(config):
        instances_dict.pop('postgresql')
    return instances_dict


def _generate_three_nodes_cluster_dict(config):
    """Going over the existing_vms list and "replicating" each instance X3."""
    instances_dict = _get_instances_ordered_dict(config)
    existing_nodes_list = config.get('existing_vms').values()
    validate_connection = True
    for node_type in instances_dict:
        for i, node_dict in enumerate(existing_nodes_list):
            new_vm = _get_cfy_node(config,
                                   node_dict,
                                   node_name=(node_type + '-' + str(i + 1)),
                                   validate_connection=validate_connection)
            instances_dict[node_type].append(new_vm)
        validate_connection = False
    return instances_dict


def _generate_general_cluster_dict(config):
    instances_dict = _get_instances_ordered_dict(config)
    for node_name, node_dict in config.get('existing_vms').items():
        new_vm = _get_cfy_node(config, node_dict, node_name)
        instances_dict[new_vm.type].append(new_vm)

    _sort_instances_dict(instances_dict)
    return instances_dict


def _create_cluster_install_directory():
    logger.info('Creating `{0}` directory'.format(DIR_NAME))
    if exists(CLUSTER_INSTALL_DIR):
        new_dirname = (time.strftime('%Y%m%d-%H%M%S_') + DIR_NAME)
        run(['mv', CLUSTER_INSTALL_DIR, join(TOP_DIR, new_dirname)])

    os.mkdir(CLUSTER_INSTALL_DIR)


def _random_credential_generator():
    return ''.join(random.choice(string.ascii_lowercase + string.digits)
                   for _ in range(40))


def _populate_credentials(credentials):
    """Generating random credentials for the ones that weren't provided."""
    for key, value in credentials.items():
        if isinstance(value, dict):
            _populate_credentials(value)
        else:
            if not value:
                credentials[key] = _random_credential_generator()


def _handle_credentials(credentials):
    _populate_credentials(credentials)
    write_dict_to_yaml_file(credentials, CREDENTIALS_FILE_PATH)
    return credentials


def _log_managers_connection_strings(manager_nodes):
    managers_str = ''
    for manager in manager_nodes:
        managers_str += '{0}: {1}@{2}\n'.format(manager.name,
                                                manager.username,
                                                manager.public_ip)
    logger.info('In order to connect to one of the managers, use one of the '
                'following connection strings:\n%s', managers_str)


def _print_successful_installation_message(start_time):
    running_time = time.time() - start_time
    m, s = divmod(running_time, 60)
    logger.info('Successfully installed a Cloudify cluster in '
                '{0} minutes and {1} seconds'.format(int(m), int(s)))


def _install_cloudify_locally(rpm_download_link):
    rpm_name = splitext(basename(rpm_download_link))[0]
    if cloudify_rpm_is_installed(rpm_name):
        logger.info('Cloudify RPM is already installed')
    else:
        logger.info('Downloading Cloudify RPM from %s', rpm_download_link)
        run(['curl', '-o', RPM_PATH, rpm_download_link])
        logger.info('Installing Cloudify RPM')
        sudo(['yum', 'install', '-y', RPM_PATH])


def _check_path(dictionary, key, errors_list, vm_name=None):
    if _check_value_provided(dictionary, key, errors_list, vm_name):
        expanded_path = expanduser(dictionary.get(key))
        if not exists(expanded_path):
            suffix = ' for instance {0}'.format(vm_name) if vm_name else ''
            errors_list.append('Path {0} for key {1} does not '
                               'exist{2}'.format(expanded_path, key, suffix))
            return False

        dictionary[key] = expanded_path
        return True

    return False


def _check_value_provided(dictionary, key, errors_list, vm_name=None):
    if not dictionary.get(key):
        suffix = ' for instance {0}'.format(vm_name) if vm_name else ''
        errors_list.append('{0} is not provided{1}'.format(key, suffix))
        return False
    return True


def _validate_existing_vms(config, errors_list):
    existing_vms_list = config.get('existing_vms')
    ca_path_exists = (_using_provided_certificates(config) and
                      _check_path(config, 'ca_cert_path', errors_list))
    for vm_name, vm_dict in existing_vms_list.items():
        logger.info('Validating %s', vm_name)
        if not vm_dict.get('private_ip'):
            errors_list.append('private_ip should be provided for '
                               '{0}'.format(vm_name))

        if ca_path_exists:
            ca_cert_path = config.get('ca_cert_path')
            key_path_exists = _check_path(vm_dict, 'key_path',
                                          errors_list, vm_name)
            if _check_path(vm_dict, 'cert_path', errors_list, vm_name):
                cert_path = vm_dict.get('cert_path')
                if check_cert_path(cert_path, errors_list):
                    if key_path_exists:
                        key_path = vm_dict.get('key_path')
                        check_cert_key_match(cert_path, key_path, errors_list)

                    check_signed_by(ca_cert_path, cert_path, errors_list)
                    check_san(vm_name, vm_dict, cert_path, errors_list)


def _validate_external_db_config(config, errors_list):
    external_db_config = _get_external_db_config(config)
    if not external_db_config:
        return

    for key, value in external_db_config.items():
        _check_value_provided(external_db_config, key, errors_list)
    _check_path(external_db_config, 'ca_path', errors_list)


def _validate_config(config):
    errors_list = []
    _check_path(config, 'key_file_path', errors_list)
    _check_value_provided(config, 'machine_username', errors_list)
    _check_path(config, 'cloudify_license_path', errors_list)
    _check_value_provided(config, 'manager_rpm_download_link', errors_list)
    _validate_existing_vms(config, errors_list)
    _validate_external_db_config(config, errors_list)

    if errors_list:
        raise_errors_list(errors_list)


def _handle_cluster_config_file(cluster_config_file_name, output_path):
    cluster_config_files_env = Environment(
        loader=FileSystemLoader(CLUSTER_CONFIG_FILES_DIR))
    template = cluster_config_files_env.get_template(cluster_config_file_name)
    rendered_data = template.render(
        credentials_file_path=CREDENTIALS_FILE_PATH)
    cluster_config_file_path = join(
        CLUSTER_CONFIG_FILES_DIR, cluster_config_file_name)
    with open(cluster_config_file_path, 'w') as cluster_config_file:
        cluster_config_file.write(rendered_data)
    copy(cluster_config_file_path, output_path)


def generate_config(output_path,
                    verbose,
                    using_three_nodes,
                    using_external_db):
    setup_logger(verbose)
    output_path = output_path or CLUSTER_INSTALL_CONFIG_PATH
    if isdir(output_path):
        output_path = join(output_path, CLUSTER_CONFIG_FILE_NAME)

    if using_three_nodes:
        if using_external_db:
            _handle_cluster_config_file(
                'cfy_three_nodes_external_db_cluster_config.yaml', output_path)
        else:
            _handle_cluster_config_file(
                'cfy_three_nodes_cluster_config.yaml', output_path)

    elif using_external_db:
        _handle_cluster_config_file(
            'cfy_external_db_cluster_config.yaml', output_path)
    else:
        _handle_cluster_config_file(CLUSTER_CONFIG_FILE_NAME, output_path)

    logger.info('Created the cluster install configuration file in %s',
                output_path)


def _handle_certificates(config, instances_dict):
    if _using_provided_certificates(config):
        copy(expanduser(config.get('ca_cert_path')), CA_PATH)
    else:
        _generate_certs(instances_dict)

    external_db_config = _get_external_db_config(config)
    if external_db_config:
        copy(external_db_config.get('ca_path'), EXTERNAL_DB_CA_PATH)


def _previous_installation(instances_dict, rpm_download_link, override):
    logger.info('Checking for a previous installation')
    first_instance = (
        instances_dict['postgresql'][0] if 'postgresql' in instances_dict
        else instances_dict['rabbitmq'][0])
    return _rpm_was_installed(first_instance, rpm_download_link, override)


def _remove_cloudify_installation(instance, verbose):
    instance.run_command(
        'cfy_manager remove -c {config_path} {verbose}'.format(
            config_path=instance.config_path, verbose='-v' if verbose else ''))

    if not isdir(INITIAL_INSTALL_DIR):
        instance.run_command(
            'yum remove -y cloudify-manager-install', use_sudo=True)

    if instance.type == 'manager':
        certs_paths_list = [
            'cloudify_external_cert.pem', 'cloudify_external_key.pem',
            'cloudify_internal_ca_cert.pem', 'cloudify_external_ca_cert.pem',
            'cloudify_internal_cert.pem', 'cloudify_internal_key.pem']

        timestamp = time.strftime('%Y%m%d-%H%M%S_')
        certs_dir_path = join(BASE_CFY_DIR, 'ssl')
        for cert_path in certs_paths_list:
            full_path = join(certs_dir_path, cert_path)
            if instance.file_exists(full_path):
                new_path = join(BASE_CFY_DIR, timestamp + cert_path)
                instance.run_command(
                    'mv {0} {1}'.format(full_path, new_path), use_sudo=True)

    instance.installed = False


def _mark_installed_instances(instances_dict,
                              rpm_download_link,
                              override,
                              verbose):
    """Checking which instances were installed in the previous installation.

    This function goes over each instance in the ordered instances dictionary,
    and checks if it has Cloudify RPM installed. If it does, then it checks
    if the previous installation finished successfully. If it did, then
    the instance.installed attribute is set to True. Otherwise, the failed
    installation is removed, the instance.installed is set to False (this is
    also the default setting), and the function returns.
    The function returns because the failed instance was the last one to be
    installed in the previous installation.
    """
    for instance_type, instances_list in instances_dict.items():
        for instance in instances_list:
            logger.info('Checking if %s was installed', instance.name)
            if _rpm_was_installed(instance, rpm_download_link, override):
                instance.installed = \
                    _cloudify_was_previously_installed_successfully(
                        instance, verbose)
                if instance.installed:
                    logger.info('%s was previously installed successfully',
                                instance.name)
                    if override:
                        logger.info('Removing Cloudify from %s', instance.name)
                        _remove_cloudify_installation(instance, verbose)
                else:
                    return
            else:
                return


def install(config_path, override, verbose):
    setup_logger(verbose)
    if not yum_is_present():
        raise ClusterInstallError('Yum is not present.')

    logger.info('Installing a Cloudify cluster')
    start_time = time.time()
    config_path = config_path or CLUSTER_INSTALL_CONFIG_PATH
    config = get_dict_from_yaml(config_path)
    _validate_config(config)
    load_balancer_ip = config.get('load_balancer_ip')
    rpm_download_link = config.get('manager_rpm_download_link')
    credentials = _handle_credentials(config.get('credentials'))
    using_three_nodes_cluster = (len(config.get('existing_vms')) == 3)
    external_db_config = _get_external_db_config(config)
    instances_dict = (_generate_three_nodes_cluster_dict(config)
                      if using_three_nodes_cluster else
                      _generate_general_cluster_dict(config))

    previous_installation = _previous_installation(
        instances_dict, rpm_download_link, override)
    if previous_installation:
        logger.info('Cloudify cluster was previously installed')
        _mark_installed_instances(instances_dict, rpm_download_link, override,
                                  verbose)
    if (not previous_installation) or override:
        _create_cluster_install_directory()
        copy(config.get('cloudify_license_path'),
             join(CLUSTER_INSTALL_DIR, 'license.yaml'))
        _install_cloudify_locally(rpm_download_link)
        _handle_certificates(config, instances_dict)
        _prepare_config_files(instances_dict, credentials, load_balancer_ip,
                              external_db_config)

    _install_instances(instances_dict, using_three_nodes_cluster,
                       rpm_download_link, verbose)
    _log_managers_connection_strings(instances_dict['manager'])
    logger.info('The credentials file was saved to %s', CREDENTIALS_FILE_PATH)
    _print_successful_installation_message(start_time)


def add_verbose_arg(parser):
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        default=False,
        help='Show verbose output.'
    )


def main():
    parser = argparse.ArgumentParser(
        description='Setting up a Cloudify cluster')

    subparsers = parser.add_subparsers(help='Cloudify cluster manager action',
                                       dest='action')

    generate_config_args = subparsers.add_parser(
        'generate-config',
        help='Generate the cluster install configuration file')

    generate_config_args.add_argument(
        '-o', '--output',
        action='store',
        help='The local path to save the cluster install configuration file '
             'to. default: ./{0}'.format(CLUSTER_CONFIG_FILE_NAME))

    generate_config_args.add_argument(
        '--three-nodes',
        action='store_true',
        default=False,
        help='Using a three nodes cluster.')

    generate_config_args.add_argument(
        '--external-db',
        action='store_true',
        default=False,
        help='Using an external DB.')

    add_verbose_arg(generate_config_args)

    install_args = subparsers.add_parser(
        'install',
        help='Install a Cloudify cluster based on the cluster install '
             'configuration file.')

    install_args.add_argument(
        '--config-path',
        action='store',
        help='The completed cluster install configuration file. '
             'default: ./{0}'.format(CLUSTER_CONFIG_FILE_NAME))

    install_args.add_argument(
        '--override',
        action='store_true',
        default=False,
        help='If specified, any previous installation of Cloudify on the '
             'instances will be removed'
    )

    add_verbose_arg(install_args)

    args = parser.parse_args()

    if args.action == 'generate-config':
        generate_config(args.output, args.verbose, args.three_nodes,
                        args.external_db)

    elif args.action == 'install':
        install(args.config_path, args.override, args.verbose)

    else:
        raise RuntimeError('Invalid action specified in parser.')


if __name__ == "__main__":
    main()