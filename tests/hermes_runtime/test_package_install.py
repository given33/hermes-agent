from __future__ import annotations

import subprocess

from hermes_runtime import package_install


def _result(argv, returncode=0):
    return subprocess.CompletedProcess(argv, returncode, stdout="", stderr="")


def test_uv_success_is_returned_without_probing_pip(monkeypatch):
    calls = []
    monkeypatch.setattr(package_install.shutil, "which", lambda _name: "C:/uv.exe")

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _result(argv)

    monkeypatch.setattr(package_install.subprocess, "run", fake_run)

    result = package_install.install_python_packages(["example==1"], creationflags=7)

    assert result.returncode == 0
    assert [call[0] for call in calls] == [
        ["C:/uv.exe", "pip", "install", "example==1"]
    ]
    assert calls[0][1]["creationflags"] == 7
    assert calls[0][1]["env"]["VIRTUAL_ENV"]


def test_missing_pip_is_bootstrapped_before_install(monkeypatch):
    calls = []
    monkeypatch.setattr(package_install.shutil, "which", lambda _name: None)

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[-1] == "--version":
            return _result(argv, returncode=1)
        return _result(argv)

    monkeypatch.setattr(package_install.subprocess, "run", fake_run)

    result = package_install.install_python_packages(["example"], creationflags=0)

    assert result.returncode == 0
    assert calls[0][0][-1] == "--version"
    assert "ensurepip" in calls[1][0]
    assert calls[2][0][-2:] == ["install", "example"]


def test_ensurepip_failure_returns_a_completed_failure(monkeypatch):
    monkeypatch.setattr(package_install.shutil, "which", lambda _name: None)

    def fake_run(argv, **kwargs):
        if argv[-1] == "--version":
            raise FileNotFoundError
        if "ensurepip" in argv:
            raise subprocess.CalledProcessError(1, argv)
        raise AssertionError(f"unexpected subprocess: {argv}")

    monkeypatch.setattr(package_install.subprocess, "run", fake_run)

    result = package_install.install_python_packages(["example"], creationflags=0)

    assert result.returncode == 1
    assert "ensurepip failed" in result.stderr
