import tempfile
from pathlib import Path
from tools.file_operations import ShellFileOperations
from tools.environments.local import LocalEnvironment

env = LocalEnvironment(cwd=tempfile.mkdtemp())
fops = ShellFileOperations(env)
p = Path(env.cwd) / "crlf.txt"
p.write_bytes("\ufeffa\r\nb\r\nc".encode("utf-16-le"))

# Spy on _exec
orig_exec = fops._exec
captured = {}
def spy_exec(command, **kwargs):
    r = orig_exec(command, **kwargs)
    rc = r.get("returncode") if isinstance(r, dict) else r.exit_code
    out = r.get("output", "") if isinstance(r, dict) else r.stdout
    captured["rc"] = rc
    captured["stdout"] = out
    return r
fops._exec = spy_exec

result = fops.read_file(str(p))
print("rc:", captured.get("rc"))
print("stdout:", repr(captured.get("stdout", ""))[:400])
print()
print("total_lines:", result.total_lines)
print("content:", repr(result.content))
