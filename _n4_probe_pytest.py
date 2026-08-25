
import faulthandler, sys
faulthandler.dump_traceback_later(12, exit=True)
import pytest
sys.exit(pytest.main(["tests/tui_gateway/test_compute_host_phase1.py::test_supervisor_startup_reconcile_pid_reuse_guard", "-q", "--no-header"]))
