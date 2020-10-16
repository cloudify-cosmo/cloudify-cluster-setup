from os.path import dirname, join

import yaml
import pytest


@pytest.fixture(autouse=True)
def config_dir(tmp_path):
    config_dir = tmp_path / 'config'
    config_dir.mkdir()
    return config_dir


@pytest.fixture(autouse=True)
def ssh_key_path(config_dir):
    ssh_key_path = config_dir / 'key_file.pem'
    ssh_key_path.write_text(u'test_key_path')
    return str(ssh_key_path)


@pytest.fixture(autouse=True)
def license_path(config_dir):
    license_path = config_dir / 'license.yaml'
    license_path.write_text(u'test_license_path')
    return str(license_path)


@pytest.fixture(autouse=True)
def ca_path(config_dir):
    ca_path = config_dir / 'ca.pem'
    ca_path.write_text(u'test_ca_path')
    return str(ca_path)


@pytest.fixture(autouse=True)
def basic_config_dict(ssh_key_path, license_path):
    return {
        'ssh_key_path': ssh_key_path,
        'ssh_user': 'centos',
        'cloudify_license_path': license_path
    }


@pytest.fixture()
def three_nodes_config_dict(basic_config_dict):
    return _get_config_dict('three_nodes_config.yaml', basic_config_dict)


@pytest.fixture()
def three_nodes_external_db_config_dict(basic_config_dict):
    return _get_config_dict('three_nodes_external_db_config.yaml',
                            basic_config_dict)


@pytest.fixture()
def nine_nodes_config_dict(basic_config_dict):
    return _get_config_dict('nine_nodes_config.yaml', basic_config_dict)


def _get_config_dict(config_file_name, basic_config_dict):
    resources_path = join(dirname(__file__), 'resources')
    completed_config_path = join(resources_path, config_file_name)
    with open(completed_config_path) as config_path:
        config_dict = yaml.load(config_path, yaml.Loader)

    config_dict.update(basic_config_dict)
    return config_dict
