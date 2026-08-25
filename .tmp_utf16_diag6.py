import tempfile
from pathlib import Path
from tools.file_operations import ShellFileOperations
from tools.environments.local import LocalEnvironment

env = LocalEnvironment(cwd=tempfile.mkdtemp())
fops = ShellFileOperations(env)
p = Path(env.cwd) / "crlf.txt"
p.write_bytes("\ufeffa\r\nb\r\nc".encode("utf-16-le"))

orig_exec = fops._exec
captured = {}
def spy_exec(command, **kwargs):
    r = orig_exec(command, **kwargs)
    rc = r.get("returncode") if isinstance(r, dict) else r.exit_code
    out = r.get("output", "") if isinstance(r, dict) else r.stdout
    captured["cmd"] = command
    captured["rc"] = rc
    captured["stdout"] = out
    return r
fops._exec = spy_exec

result = fops.read_file(str(p))
# Show the BOM detection part of the command
cmd = captured.get("cmd", "")
bom_idx = cmd.find("BOM_BE") if "BOM_BE" in cmd else cmd.find("xfe")
print("cmd around BOM:", repr(cmd[max(0,bom_idx-20):bom_idx+60]) if bom_idx >= 0 else "BOM not in cmd")
print()
print("rc:", captured.get("rc"))
print("stdout:", repr(captured.get("stdout", ""))[:300])
