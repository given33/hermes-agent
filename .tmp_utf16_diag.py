import tempfile
from pathlib import Path
from tools.file_operations import ShellFileOperations
from tools.environments.local import LocalEnvironment

env = LocalEnvironment(cwd=tempfile.mkdtemp())
fops = ShellFileOperations(env)
p = Path(env.cwd) / "crlf.txt"
p.write_bytes("\ufeffa\r\nb\r\nc".encode("utf-16-le"))

snippet = """
import sys, json, os
p = r'%s'
with open(p, 'rb') as f:
    data = f.read()
sample = data[:8]
enc = None
if sample[:2] == b'\\xfe\\xff':
    enc = 'utf-16-be'
elif sample[:2] == b'\\xff\\xfe':
    enc = 'utf-16-le'
print('HERMES_UTF16:OK')
text = data.decode(enc or 'utf-16-le', 'replace')
if text[:1] == chr(0xfeff):
    text = text[1:]
text = text.replace(chr(13)+chr(10), chr(10))
lines = text.split(chr(10))
out = {'total_lines': len(lines), 'encoding': enc, 'content': chr(10).join(lines)}
print(json.dumps(out, ensure_ascii=True))
""" % str(p)

cmd = "python3 -c " + fops._escape_shell_arg(snippet)
r = env.execute(cmd)
print("python3 rc:", r.get("returncode"))
print("stdout:", repr(r.get("output", ""))[:300])
if r.get("returncode") != 0:
    cmd2 = "python -c " + fops._escape_shell_arg(snippet)
    r2 = env.execute(cmd2)
    print("python rc:", r2.get("returncode"))
    print("stdout:", repr(r2.get("output", ""))[:300])
