"""The ``hermes-agent`` console script must repair stdio before printing.

``run_agent.main`` is the ``hermes-agent`` entry point (pyproject
``[project.scripts]``) and opens with emoji banners. UTF-8 stdio repair
used to happen as an import-time side effect of ``hermes_cli``; when that
side effect was removed, this entry point — which never routes through
``cli.py`` or ``hermes_cli/main.py`` — lost the repair, and on a cp1252
Windows console the very first banner ``print()`` died with
``UnicodeEncodeError`` before the REPL opened.

The contract under test: ``main()`` calls ``ensure_utf8_stdio()`` and
``configure_windows_stdio()`` (both via ``hermes_cli.stdio``, one import
statement — see the comment in ``run_agent.main``) BEFORE its first
``print``, and a failing repair degrades silently instead of killing the
entry point.
"""

import builtins

import pytest

import run_agent


class _BannerReached(Exception):
    """Sentinel raised by the patched ``print`` to stop main() at the banner."""


def test_main_repairs_stdio_before_first_print(monkeypatch):
    """Both stdio repairs run before the emoji banner can hit the console."""
    order = []

    monkeypatch.setattr(
        "hermes_cli.stdio.ensure_utf8_stdio",
        lambda force=False: order.append("ensure_utf8_stdio"),
    )
    monkeypatch.setattr(
        "hermes_cli.stdio.configure_windows_stdio",
        lambda: order.append("configure_windows_stdio"),
    )

    def banner_print(*_args, **_kwargs):
        order.append("print")
        raise _BannerReached

    monkeypatch.setattr(builtins, "print", banner_print)

    with pytest.raises(_BannerReached):
        run_agent.main()

    assert order == ["ensure_utf8_stdio", "configure_windows_stdio", "print"]


def test_main_survives_stdio_repair_failure(monkeypatch):
    """A blown-up repair degrades to unrepaired stdio, never a dead CLI.

    The repair block is best-effort by design (e.g. a partial ``hermes
    update`` can leave ``hermes_cli`` unimportable); main() must still
    reach its banner rather than crash before doing anything.
    """
    def boom(force=False):
        raise RuntimeError("stdio repair exploded")

    monkeypatch.setattr("hermes_cli.stdio.ensure_utf8_stdio", boom)

    def banner_print(*_args, **_kwargs):
        raise _BannerReached

    monkeypatch.setattr(builtins, "print", banner_print)

    with pytest.raises(_BannerReached):
        run_agent.main()
