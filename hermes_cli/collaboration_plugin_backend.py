"""Stable backend surface consumed by the collaboration HTTP plugin."""

from hermes_cli import account_lifecycle as _account_lifecycle
from hermes_cli import cloud_file_library as _cloud_files
from hermes_cli import ios_intelligence as _ios_intelligence
from hermes_cli import ios_intelligence_config as _ios_config
from hermes_cli import managed_installations as _managed
from hermes_cli import mobile_console as _mobile_console
from hermes_cli import profiles as _profiles
from hermes_cli import tools_config as _tools_config
from hermes_cli.dashboard_auth import mobile_device_store as _mobile_devices
from tools import mcp_tool as _mcp


CLOUD_FILE_SCHEMA_VERSION = _cloud_files.SCHEMA_VERSION
MANAGED_INSTALLATIONS_SCHEMA_VERSION = _managed.MANAGED_INSTALLATIONS_SCHEMA_VERSION
MOBILE_DEVICE_SCHEMA_VERSION = _mobile_devices.SCHEMA_VERSION


def MobileDeviceStore(*args, **kwargs):
    return _mobile_devices.MobileDeviceStore(*args, **kwargs)


def IOSIntelligenceStore(*args, **kwargs):
    return _ios_intelligence.IOSIntelligenceStore(*args, **kwargs)


def account_lifecycle_commit_guard(*args, **kwargs):
    return _account_lifecycle.account_lifecycle_commit_guard(*args, **kwargs)


def load_ios_intelligence_config(*args, **kwargs):
    return _ios_config.load_ios_intelligence_config(*args, **kwargs)


def connect_managed_installations_database(*args, **kwargs):
    return _managed.connect_managed_installations_database(*args, **kwargs)


def create_managed_installation(*args, **kwargs):
    return _managed.create_managed_installation(*args, **kwargs)


def delete_owner_managed_resources(*args, **kwargs):
    return _managed.delete_owner_managed_resources(*args, **kwargs)


def get_managed_installation(*args, **kwargs):
    return _managed.get_managed_installation(*args, **kwargs)


def list_managed_installations(*args, **kwargs):
    return _managed.list_managed_installations(*args, **kwargs)


def list_managed_resources(*args, **kwargs):
    return _managed.list_managed_resources(*args, **kwargs)


def managed_account_runtime_home(*args, **kwargs):
    return _managed.managed_account_runtime_home(*args, **kwargs)


def managed_installations_db_path(*args, **kwargs):
    return _managed.managed_installations_db_path(*args, **kwargs)


def rollback_managed_installation(*args, **kwargs):
    return _managed.rollback_managed_installation(*args, **kwargs)


def execute_mobile_console_command(*args, **kwargs):
    return _mobile_console.execute_mobile_console_command(*args, **kwargs)


def mobile_console_catalog(*args, **kwargs):
    return _mobile_console.mobile_console_catalog(*args, **kwargs)


def mobile_console_completions(*args, **kwargs):
    return _mobile_console.mobile_console_completions(*args, **kwargs)


def get_profile_dir(*args, **kwargs):
    return _profiles.get_profile_dir(*args, **kwargs)


def normalize_profile_name(*args, **kwargs):
    return _profiles.normalize_profile_name(*args, **kwargs)


def resolve_profile_env(*args, **kwargs):
    return _profiles.resolve_profile_env(*args, **kwargs)


def _get_platform_tools(*args, **kwargs):
    return _tools_config._get_platform_tools(*args, **kwargs)


def enabled_mcp_server_names(*args, **kwargs):
    return _tools_config.enabled_mcp_server_names(*args, **kwargs)


def discover_mcp_tools(*args, **kwargs):
    return _mcp.discover_mcp_tools(*args, **kwargs)


def get_mcp_availability(*args, **kwargs):
    return _mcp.get_mcp_availability(*args, **kwargs)


def select_mcp_servers_for_capabilities(*args, **kwargs):
    return _mcp.select_mcp_servers_for_capabilities(*args, **kwargs)
