import sys, time
sys.path.insert(0, r"C:\Users\given\hermes-audit\hermes-agent")
from hermes_cli.win_pty_bridge import WinPtyBridge

script = (
    "import sys; "
    "line = sys.stdin.readline().strip(); "
    "sys.stdout.write('GOT:' + line + chr(10)); "
    "sys.stdout.flush()"
)
bridge = WinPtyBridge.spawn([sys.executable, "-c", script])
buf = bytearray()
deadline = time.monotonic() + 8
while time.monotonic() < deadline:
    chunk = bridge.read(timeout=0.2)
    if chunk is None:
        buf.extend(b"<EOF>")
        break
    buf.extend(chunk)
print("ALIVE:", bridge.is_alive())
print("BUF:", repr(bytes(buf)))
bridge.close()
