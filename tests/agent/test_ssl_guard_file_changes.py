import shutil

import certifi
import pytest

from agent.errors import SSLConfigurationError
from agent.ssl_guard import _validate_bundle_path


def test_verified_bundle_is_revalidated_after_corruption_and_deletion(tmp_path):
    bundle = tmp_path / "bundle.pem"
    shutil.copyfile(certifi.where(), bundle)
    _validate_bundle_path("test", str(bundle))
    _validate_bundle_path("test", str(bundle))
    bundle.write_text("invalid certificate content")
    with pytest.raises(SSLConfigurationError, match="cannot be loaded"):
        _validate_bundle_path("test", str(bundle))
    shutil.copyfile(certifi.where(), bundle)
    _validate_bundle_path("test", str(bundle))
    bundle.unlink()
    with pytest.raises(SSLConfigurationError, match="missing"):
        _validate_bundle_path("test", str(bundle))
