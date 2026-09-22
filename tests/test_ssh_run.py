import subprocess
import sys
import base64

cmd = sys.argv[1] if len(sys.argv) > 1 else "uname -a"
timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 60

print(f"Running: {cmd} (timeout {timeout}s)", flush=True)
try:
    p = subprocess.run(
        ["plink", "-batch", "-pw", "123", "mamad@192.168.0.115", cmd],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout
    )
    print("EXIT CODE:", p.returncode, flush=True)
    if p.stdout:
        print("--- STDOUT ---")
        print(p.stdout, flush=True)
    if p.stderr:
        print("--- STDERR ---")
        print(p.stderr, flush=True)
except Exception as e:
    print("EXCEPTION:", repr(e), flush=True)
