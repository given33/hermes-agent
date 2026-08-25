from tools.file_operations import ShellFileOperations
from tools.environments.local import LocalEnvironment
import tempfile

env = LocalEnvironment(cwd=tempfile.mkdtemp())
fops = ShellFileOperations(env)
win_path = r"C:\Users\test\file.txt"
fwd_path = win_path.replace("\\", "/")

test_snippet = f"import os; print('exists:', os.path.exists({fwd_path!r}))"
escaped = fops._escape_shell_text_arg(test_snippet)
r = env.execute(f"python -c {escaped}")
rc = r.get("returncode") if isinstance(r, dict) else r.exit_code
out = r.get("output", "") if isinstance(r, dict) else r.stdout
print("forward-slash path rc:", rc)
print("stdout:", repr(out))
