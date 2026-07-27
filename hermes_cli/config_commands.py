"""CLI application layer for configuration commands.

The configuration authority is process-agnostic.  This module composes it
with the provider credential lifecycle used by interactive and RPC surfaces.
"""

from hermes_cli.credential_lifecycle import (
    remove_provider_env_credential,
    save_provider_env_credential,
)
from hermes_runtime import config as runtime_config


def config_command(args):
    return runtime_config.config_command(
        args,
        credential_saver=save_provider_env_credential,
        credential_remover=remove_provider_env_credential,
    )


def show_config():
    return runtime_config.show_config()


def set_config_value(key: str, value: str, force: bool = False):
    return runtime_config.set_config_value(
        key,
        value,
        force=force,
        credential_saver=save_provider_env_credential,
    )


def unset_config_value(key: str):
    return runtime_config.unset_config_value(
        key,
        credential_remover=remove_provider_env_credential,
    )
