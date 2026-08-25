import pytest

from plugins.workflows.models import WorkflowSecurityError
from plugins.workflows.store import WorkflowStore


@pytest.mark.parametrize(
    "value",
    [
        ".env",
        ".ENV",
        ".env\u200b",
        "credentials",
        "CREDENTIALS",
        "server.pem",
        "SERVER.PEM",
    ],
)
def test_sensitive_paths_are_canonicalized_before_denylist(value):
    with pytest.raises(WorkflowSecurityError):
        WorkflowStore._safe_change_path(value)
