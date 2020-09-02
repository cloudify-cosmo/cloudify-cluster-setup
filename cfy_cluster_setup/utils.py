import os
import re
import shlex
import socket
import subprocess
from os.path import dirname, exists, expanduser, isdir, isfile, join
from socket import error as socket_error

import yaml
from fabric import Connection
from paramiko import AuthenticationException

from .logger import get_cfy_cluster_setup_logger

logger = get_cfy_cluster_setup_logger()


class ClusterInstallError(Exception):
    pass


class ProcessExecutionError(ClusterInstallError):
    def __init__(self, message, return_code=None):
        self.return_code = return_code
        super(ProcessExecutionError, self).__init__(message)


class ValidationError(ClusterInstallError):
    pass


def run(command, retries=0, ignore_failures=False):
    if isinstance(command, str):
        command = shlex.split(command)
    logger.debug('Running: {0}'.format(command))
    proc = subprocess.Popen(command, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc.aggr_stdout, proc.aggr_stderr = proc.communicate(input=u'')
    if proc.aggr_stdout is not None:
        proc.aggr_stdout = proc.aggr_stdout.decode('utf-8')
    if proc.aggr_stderr is not None:
        proc.aggr_stderr = proc.aggr_stderr.decode('utf-8')
    if proc.returncode != 0:
        if retries:
            logger.warn('Failed running command: %s. Retrying. '
                        '(%s left)', command, retries)
            proc = run(command, retries - 1)
        elif not ignore_failures:
            msg = 'Failed running command: {0} ({1}).'.format(
                command, proc.aggr_stderr)
            err = ProcessExecutionError(msg, proc.returncode)
            err.aggr_stdout = proc.aggr_stdout
            err.aggr_stderr = proc.aggr_stderr
            raise err
    return proc


def sudo(command, *args, **kwargs):
    if isinstance(command, str):
        command = shlex.split(command)
    command.insert(0, 'sudo')
    return run(command=command, *args, **kwargs)


def ensure_destination_dir_exists(destination):
    destination_dir = dirname(destination)
    if not exists(destination_dir):
        sudo(['mkdir', '-p', destination_dir])


def copy(source, destination):
    ensure_destination_dir_exists(destination)
    sudo(['cp', '-rp', source, destination])


def move(source, destination):
    ensure_destination_dir_exists(destination)
    sudo(['mv', source, destination])


class VM(object):
    def __init__(self,
                 private_ip,
                 public_ip,
                 key_file_path,
                 username):
        self.username = username
        self.private_ip = private_ip
        self.public_ip = public_ip
        self.key_file_path = expanduser(key_file_path)

    def _get_connection(self):
        connection = Connection(
            host=self.private_ip, user=self.username, port=22,
            connect_kwargs={'key_filename': self.key_file_path})
        self.test_connection(connection)

        return connection

    def test_connection(self, connection=None):
        """ Connection is lazy, so **we** need to check it can be opened."""
        connection = connection or Connection(
            host=self.private_ip, user=self.username, port=22,
            connect_kwargs={'key_filename': self.key_file_path})
        try:
            connection.open()
        except (socket_error, AuthenticationException) as exc:
            raise ClusterInstallError(
                "SSH: could not connect to {host} (username: {user}, "
                "key: {key}): {exc}".format(
                    host=self.private_ip, user=self.username,
                    key=self.key_file_path, exc=exc))
        finally:
            connection.close()

    def run_command(self, command, hide_stdout=False, use_sudo=False):
        hide = 'both' if hide_stdout else 'stderr'
        with self._get_connection() as connection:
            logger.debug('Running `%s` on %s', command, self.private_ip)
            result = (connection.sudo(command, warn=True, hide=hide)
                      if use_sudo else
                      connection.run(command, warn=True, hide=hide))
            if result.failed:
                if hide == 'both':  # No logs are shown
                    raise ClusterInstallError(
                        'The command `{0}` on host {1} failed with the '
                        'error {2}'.format(command, self.private_ip,
                                           result.stderr))
                raise ClusterInstallError(
                    'Error on host {0}'.format(self.private_ip))
            return result.stdout

    def put_file(self, local_path, remote_path):
        if not isfile(local_path):
            raise ClusterInstallError(
                '{} is not a file'.format(local_path))

        with self._get_connection() as connection:
            logger.debug('Copying %s to %s on host %a',
                         local_path, remote_path, self.private_ip)
            connection.put(expanduser(local_path), remote_path)

    def put_dir(self, local_dir_path, remote_dir_path, override=False):
        """Copy a local directory to a remote host.

        :param local_dir_path: An existing local directory path.
        :param remote_dir_path: A directory path on the remote host. If the
                                path doesn't exist, it will be created.
        :param override: If True and the remote directory path exists, then
                         it will be deleted.
                         If False and the remote directory path exists, then
                         the files from the local directory will be added to
                         the remote one.
                         If the remote directory path doesn't exist, it
                         doesn't have any effect.
        """
        if not isdir(local_dir_path):
            raise ClusterInstallError(
                '{} is not a directory'.format(local_dir_path))

        logger.debug('Copying %s to %s on host %a',
                     local_dir_path, remote_dir_path, self.private_ip)
        if override:
            self.run_command('rm -rf {}'.format(remote_dir_path))
        self.run_command('mkdir -p {}'. format(remote_dir_path))
        for file_name in os.listdir(local_dir_path):
            object_path = join(local_dir_path, file_name)
            if isfile(object_path):
                self.put_file(join(local_dir_path, file_name), remote_dir_path)
            elif isdir(object_path):
                self.put_dir(object_path, join(remote_dir_path, file_name))


def get_dict_from_yaml(yaml_path):
    with open(yaml_path) as yaml_file:
        yaml_dict = yaml.load(yaml_file, yaml.Loader)
    return yaml_dict


def cloudify_is_installed(cloudify_rpm):
    proc = run(['rpm', '-qa'])
    if cloudify_rpm in proc.aggr_stdout.strip():
        return True
    return False


def yum_is_present():
    try:
        run(['command', '-v', 'yum'])
        return True
    except ProcessExecutionError:
        return False


def current_host_ip():
    return socket.gethostbyname(socket.getfqdn())


def openssl_command(file_name, file_format='x509', extra_flags_list=None):
    command = ['openssl', file_format, '-in', file_name, '-noout']
    if extra_flags_list:
        command.extend(extra_flags_list)
    return command


def check_key_path(key_file_name, errors_list):
    proc = run(openssl_command(key_file_name, 'rsa', ['-check']),
               ignore_failures=True)
    if proc.returncode != 0:
        errors_list.append('The key file {0} is invalid'.format(key_file_name))
        return False

    return True


def check_cert_path(cert_file_name, errors_list):
    proc = run(openssl_command(cert_file_name), ignore_failures=True)
    if proc.returncode != 0:
        errors_list.append('The certificate file {0} is '
                           'invalid'.format(cert_file_name))
        return False

    return True


def check_cert_key_match(cert_filename, key_filename, errors_list):
    """Check the cert_filename matches the key_filename"""
    key_file_valid = check_key_path(key_filename, errors_list)
    if key_file_valid:
        modulus = ['-modulus']
        key_modulus = run(openssl_command(key_filename, 'rsa', modulus))
        cert_modulus = run(openssl_command(cert_filename, 'x509', modulus))

        if cert_modulus.aggr_stdout.strip() != key_modulus.aggr_stdout.strip():
            errors_list.append(
                'Provided Key {key_path} does not match the provided '
                'certificate {cert_path}'.format(key_path=key_filename,
                                                 cert_path=cert_filename))
            return False
        return True

    return False


def check_signed_by(ca_filename, cert_filename, errors_list):
    """Check the cert_filename is signed by the ca_filename"""
    ca_check_command = [
        'openssl', 'verify', '-CAfile', ca_filename, cert_filename]
    try:
        run(ca_check_command)
    except ProcessExecutionError:
        errors_list.append(
            'Provided certificate {cert} was not signed by provided '
            'CA {ca}'.format(cert=cert_filename, ca=ca_filename))


def check_san(vm_name, vm_dict, cert_path, errors_list):
    """Check the vm is specified in the certificate's SAN"""
    hostname = vm_dict.get('hostname')
    get_cert_command = openssl_command(cert_path, 'x509', ['-text'])
    cert = run(get_cert_command).aggr_stdout.strip()
    ip_addresses = re.findall(r'\bIP Address:(\S+)\b', cert)
    dns_addresses = re.findall(r'\bDNS:(\S+)\b', cert)
    for ip in vm_dict['private_ip'], vm_dict['public_ip']:
        if (ip in ip_addresses) and (ip in dns_addresses):
            return
    if hostname and hostname in dns_addresses:
        return

    suffix = ' Allowed IP addresses: {0}, Allowed DNS: {1}'.format(
        ip_addresses, dns_addresses) if (ip_addresses or dns_addresses) else ''
    errors_list.append(
        'The certificate {0} does not match the instance {1}.{2}'.format(
            cert_path, vm_name, suffix))


def raise_errors_list(errors_list):
    err_str = 'Errors:\n'
    err_lst = '\n'.join(' [{0}] {1}'.format(i+1, err) for i, err
                        in enumerate(errors_list))
    raise ValidationError(err_str + err_lst)
