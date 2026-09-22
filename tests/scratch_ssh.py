import subprocess
import sys

import base64

sys.stdout.reconfigure(encoding='utf-8')

def run(cmd):
    b64 = base64.b64encode(cmd.encode()).decode()
    remote_cmd = f"echo {b64} | base64 -d | bash"
    res = subprocess.run(
        ["plink", "-batch", "-pw", "123", "mamad@192.168.0.115", remote_cmd],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120
    )
    print("STDOUT:\n", res.stdout, flush=True)
    if res.stderr:
        print("STDERR:\n", res.stderr, flush=True)

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "uname -a"
    run(cmd)
