"""Case-insensitive filesystems must not be able to walk past the write guard.

Regression cover for a credential-write bypass. The denylist entries are
built from lowercase literals (``.ssh/authorized_keys``, ``.aws``,
``.gnupg``, …) but were compared **case sensitively** against
``os.path.realpath`` output, and ``realpath`` does not case-normalise.

On a case-insensitive filesystem (macOS APFS/HFS+ by default; Windows for
a target that does not exist yet) that meant:

    write ~/.SSH/authorized_keys
      → resolved "/Users/x/.SSH/authorized_keys"
      → != denylist "/Users/x/.ssh/authorized_keys"  → ALLOWED
      → the OS writes the real ~/.ssh/authorized_keys

turning a prompt injection into a persistent SSH public key.

These tests assert on the *classification*, not on real filesystem
behaviour, so they are meaningful on every platform including
case-sensitive Linux CI.
"""

from __future__ import annotations

import os

import pytest

from agent import file_safety
from agent.file_safety import (
    _classify_write_denial,
    get_write_denied_error,
    is_write_denied,
)


def _home() -> str:
    return os.path.realpath(os.path.expanduser("~"))


class TestCredentialPathCaseVariants:
    @pytest.mark.parametrize(
        "relative",
        [
            ".ssh/authorized_keys",
            ".SSH/authorized_keys",
            ".Ssh/Authorized_Keys",
            ".ssh/AUTHORIZED_KEYS",
        ],
    )
    def test_authorized_keys_denied_in_any_case(self, relative):
        target = os.path.join(_home(), *relative.split("/"))
        assert _classify_write_denial(target) == "credential"
        assert is_write_denied(target) is True

    @pytest.mark.parametrize(
        "relative",
        [
            ".aws/credentials",
            ".AWS/credentials",
            ".gnupg/secring.gpg",
            ".GNUPG/secring.gpg",
            ".kube/config",
            ".KUBE/config",
            ".docker/config.json",
            ".DOCKER/config.json",
        ],
    )
    def test_denied_directory_prefixes_in_any_case(self, relative):
        target = os.path.join(_home(), *relative.split("/"))
        assert _classify_write_denial(target) == "credential"

    def test_unrelated_path_still_allowed(self):
        # The fold must not over-reach into ordinary files: denying
        # everything would "pass" these tests while breaking the product.
        target = os.path.join(_home(), "projects", "app", "main.py")
        assert _classify_write_denial(target) is None
        assert is_write_denied(target) is False

    def test_similar_prefix_is_not_denied(self):
        # ".sshfoo" shares a prefix with ".ssh" but is a different
        # directory; build_write_denied_prefixes appends os.sep to
        # prevent the over-match, and folding must preserve that.
        target = os.path.join(_home(), ".sshfoo", "notes.txt")
        assert _classify_write_denial(target) is None


class TestPosixSemanticsSimulated:
    """Prove the fix under POSIX ``normcase`` — where the bypass lived.

    ``os.path.normcase`` lowercases on Windows but is the identity
    function on POSIX, so a fix built on ``normcase`` alone would look
    green on Windows CI while leaving macOS wide open. Forcing the
    identity behaviour here exercises the ``casefold`` half of
    :func:`agent.file_safety._fold`, which is the part that actually
    closes the macOS hole.
    """

    @pytest.fixture
    def posix_normcase(self, monkeypatch):
        monkeypatch.setattr(
            file_safety.os.path, "normcase", lambda p: p, raising=True
        )

    def test_uppercase_ssh_denied_under_identity_normcase(
        self, posix_normcase
    ):
        target = os.path.join(_home(), ".SSH", "authorized_keys")
        assert _classify_write_denial(target) == "credential"

    def test_uppercase_aws_denied_under_identity_normcase(self, posix_normcase):
        target = os.path.join(_home(), ".AWS", "credentials")
        assert _classify_write_denial(target) == "credential"

    def test_mixed_case_gnupg_denied_under_identity_normcase(
        self, posix_normcase
    ):
        target = os.path.join(_home(), ".GnuPG", "secring.gpg")
        assert _classify_write_denial(target) == "credential"

    def test_ordinary_file_still_allowed_under_identity_normcase(
        self, posix_normcase
    ):
        target = os.path.join(_home(), "projects", "README.md")
        assert _classify_write_denial(target) is None


class TestDenialCategoryContract:
    """``_classify_write_denial`` must return its documented categories.

    The session-state branches used to ``return True`` — a boolean, not
    one of ``'credential' | 'safe_root' | None``. Blocking still worked
    (``is_write_denied`` tests ``is not None``), but
    ``get_write_denied_error`` fell through to the credential wording and
    told the user a session transcript was "a protected system/credential
    file", and any future caller dispatching on ``== "credential"`` would
    have silently mishandled these two branches.
    """

    @pytest.fixture
    def hermes_home(self, tmp_path, monkeypatch):
        home = tmp_path / "hermes-home"
        (home / "sessions").mkdir(parents=True)
        (home / "state.db").write_text("")
        monkeypatch.setattr(file_safety, "_hermes_home_path", lambda: home)
        monkeypatch.setattr(file_safety, "_hermes_root_path", lambda: home)
        return home

    def test_state_db_classified_as_session_state(self, hermes_home):
        target = str(hermes_home / "state.db")
        assert _classify_write_denial(target) == "session_state"

    def test_sessions_dir_classified_as_session_state(self, hermes_home):
        target = str(hermes_home / "sessions" / "abc.json")
        assert _classify_write_denial(target) == "session_state"

    def test_returned_category_is_always_a_documented_string(self, hermes_home):
        for target in (
            str(hermes_home / "state.db"),
            str(hermes_home / "sessions" / "abc.json"),
            os.path.join(_home(), ".ssh", "authorized_keys"),
        ):
            denial = _classify_write_denial(target)
            assert isinstance(denial, str), f"{target!r} → {denial!r}"
            assert denial in {"credential", "session_state", "safe_root"}

    def test_session_state_error_no_longer_says_credential(self, hermes_home):
        message = get_write_denied_error(str(hermes_home / "state.db"))
        assert message is not None
        assert "session state" in message.lower()
        assert "credential" not in message.lower()

    def test_credential_error_still_says_credential(self):
        message = get_write_denied_error(
            os.path.join(_home(), ".ssh", "authorized_keys")
        )
        assert message is not None
        assert "credential" in message.lower()

    def test_blocking_behaviour_unchanged(self, hermes_home):
        # The contract repair must not change *whether* these are blocked.
        assert is_write_denied(str(hermes_home / "state.db")) is True
        assert is_write_denied(str(hermes_home / "sessions" / "abc.json")) is True


class TestMcpTokensAndPairingCaseVariants:
    @pytest.fixture
    def hermes_home(self, tmp_path, monkeypatch):
        home = tmp_path / "hermes-home"
        (home / "mcp-tokens").mkdir(parents=True)
        (home / "pairing").mkdir(parents=True)
        monkeypatch.setattr(file_safety, "_hermes_home_path", lambda: home)
        monkeypatch.setattr(file_safety, "_hermes_root_path", lambda: home)
        return home

    @pytest.mark.parametrize("name", ["mcp-tokens", "MCP-TOKENS", "Mcp-Tokens"])
    def test_mcp_tokens_denied_in_any_case(self, hermes_home, name):
        target = str(hermes_home / name / "server.json")
        assert _classify_write_denial(target) == "credential"

    @pytest.mark.parametrize("name", ["pairing", "PAIRING", "Pairing"])
    def test_pairing_denied_in_any_case(self, hermes_home, name):
        target = str(hermes_home / name / "device.json")
        assert _classify_write_denial(target) == "credential"
