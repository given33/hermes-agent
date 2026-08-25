import tempfile
from pathlib import Path
from tools.file_operations import ShellFileOperations
from tools.environments.local import LocalEnvironment

env = LocalEnvironment(cwd=tempfile.mkdtemp())
fops = ShellFileOperations(env)
p = Path(env.cwd) / "crlf.txt"
p.write_bytes("\ufeffa\r\nb\r\nc".encode("utf-16-le"))

result = fops.read_file(str(p))
print("total_lines:", result.total_lines)
print("content:", repr(result.content))
print("hint:", repr(result.hint))
