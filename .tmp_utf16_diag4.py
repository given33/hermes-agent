from tools.file_operations import ShellFileOperations
from tools.environments.local import LocalEnvironment
import tempfile

env = LocalEnvironment(cwd=tempfile.mkdtemp())
fops = ShellFileOperations(env)

# Reproduce the exact snippet from read_file
snippet = (
    "import json, os, sys\n"
    "p = r'C:/test.bin'\n"
    "with open(p, 'rb') as f:\n"
    "    data = f.read()\n"
    "text = data.decode('utf-16-le', 'replace')\n"
    "print('BOM check:', repr(text[:1]), ord(text[0]) == 0xFEFF)\n"
    "if text[:1] == '\\ufeff':\n"
    "    print('stripping BOM')\n"
    "    text = text[1:]\n"
    "print('after:', repr(text))\n"
)

escaped = fops._escape_shell_arg(snippet)
print("escaped prefix:", repr(escaped[:60]))
print("escaped contains ufeff:", "\\ufeff" in escaped)
r = env.execute(f"python -c {escaped}")
print("rc:", r.get("returncode") if isinstance(r, dict) else r.exit_code)
out = r.get("output", "") if isinstance(r, dict) else r.stdout
print("stdout:", repr(out))
