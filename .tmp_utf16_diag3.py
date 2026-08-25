import tempfile
from pathlib import Path
from tools.file_operations import ShellFileOperations
from tools.environments.local import LocalEnvironment

env = LocalEnvironment(cwd=tempfile.mkdtemp())
fops = ShellFileOperations(env)
p = Path(env.cwd) / "crlf.txt"
p.write_bytes("\ufeffa\r\nb\r\nc".encode("utf-16-le"))

# Reproduce exactly what read_file does
snippet_parts = fops._utf16_read_snippet if hasattr(fops, "_utf16_read_snippet") else None
# Instead, capture the _exec call
orig_exec = fops._exec
captured = {}
def spy_exec(command, **kwargs):
    r = orig_exec(command, **kwargs)
    captured["cmd_prefix"] = command[:30]
    captured["rc"] = r.exit_code
    captured["stdout"] = r.stdout
    return r
fops._exec = spy_exec

result = fops.read_file(str(p))
print("=== spy ===")
print("rc:", captured.get("rc"))
print("stdout repr:", repr(captured.get("stdout", ""))[:400])
print()
print("=== result ===")
print("total_lines:", result.total_lines)
print("content:", repr(result.content))
