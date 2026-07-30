"""Stable account-lifecycle surface consumed by the native iOS plugin."""

from hermes_cli import account_cleanup as _account_cleanup
from hermes_cli import account_lifecycle as _account_lifecycle


def account_lifecycle_commit_guard(*args, **kwargs):
    return _account_lifecycle.account_lifecycle_commit_guard(*args, **kwargs)


def begin_account_owned_cloud_deletion(*args, **kwargs):
    return _account_cleanup.begin_account_owned_cloud_deletion(*args, **kwargs)


def purge_account_owned_cloud_data(*args, **kwargs):
    return _account_cleanup.purge_account_owned_cloud_data(*args, **kwargs)
