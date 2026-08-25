import tempfile
from pathlib import Path
from tools.file_operations import ShellFileOperations
from tools.environments.local import LocalEnvironment

env = LocalEnvironment(cwd=tempfile.mkdtemp())
fops = ShellFileOperations(env)
p = Path(env.cwd) / "crlf.txt"
p.write_bytes("\ufeffa\r\nb\r\nc".encode("utf-16-le"))

# Test: can python open the path when embedded via repr?
test_snippet = f"import os; print('exists:', os.path.exists({str(p)!r}))"
escaped = fops._escape_shell_text_arg(test_snippet)
r = env.execute(f"python -c {escaped}")
rc = r.get("returncode") if isinstance(r, dict) else r.exit_code
out = r.get("output", "") if isinstance(r, dict) else r.stdout
print("path exists check rc:", rc)
print("stdout:", repr(out))
print()
print("raw path:", repr(str(p)))
