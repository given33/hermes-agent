
import faulthandler, io, json, os, sys, threading
faulthandler.dump_traceback_later(8, exit=True)
from tui_gateway.host_supervisor import HostSupervisor

registry = __import__("pathlib").Path(os.environ["TEMP"]) / "n4probe-registry.json"
registry.write_text(json.dumps({"host_pid": os.getpid(), "boot_id": "stale"}), encoding="utf-8")

supervisor = HostSupervisor(registry_path=registry, argv=[sys.executable, "-c", ""], autostart=False)
supervisor._pid_matches_compute_host = lambda _pid: False
supervisor._terminate_pid = lambda pid, **_kw: None
result = supervisor.reconcile_startup_orphan()
print("RESULT:", result, flush=True)
print("threads:", [(t.name, t.daemon, t.is_alive()) for t in threading.enumerate()], flush=True)
print("PROBE-DONE", flush=True)
