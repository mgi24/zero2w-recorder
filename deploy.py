#!/usr/bin/env python3
"""
Deployment script: Syncs all code from local workspace to Raspberry Pi Zero 2 W.
Target directory: ~/recorder/
After upload, reloads systemd and restarts the recorder service.
"""

import subprocess
from pathlib import Path

PI_USER    = "mamad"
PI_HOST    = "192.168.0.115"
PI_PASS    = "123"
REMOTE_DIR = "/home/mamad/recorder"

FILES_TO_SYNC = [
    "config.py",
    "recorder.py",
    "tests/trigger_sim.py",
    "recorder.service",
    "flow.md",
]

def ssh(cmd: str, check=True, sudo=False):
    if sudo:
        # pipe password into sudo -S so it doesn't prompt interactively
        cmd = f"echo {PI_PASS} | sudo -S {cmd}"
    return subprocess.run(
        ["plink", "-batch", "-pw", PI_PASS, f"{PI_USER}@{PI_HOST}", cmd],
        check=check,
    )

def scp(local: str, remote: str):
    subprocess.run(
        ["pscp", "-batch", "-pw", PI_PASS, local, f"{PI_USER}@{PI_HOST}:{remote}"],
        check=True,
    )

def main():
    print(f"[DEPLOY] Ensuring remote directories exist...")
    ssh(f"mkdir -p {REMOTE_DIR}/DCIM")

    print(f"[DEPLOY] Copying project files to {REMOTE_DIR}...")
    for f in FILES_TO_SYNC:
        local_path = Path(f)
        if local_path.exists():
            print(f"  Uploading {f} -> {REMOTE_DIR}/{local_path.name}...")
            scp(str(local_path), f"{REMOTE_DIR}/{local_path.name}")
        else:
            print(f"  [SKIP] {f} not found locally.")

    print("[DEPLOY] Setting execute permissions on scripts...")
    ssh(f"chmod +x {REMOTE_DIR}/recorder.py {REMOTE_DIR}/trigger_sim.py")

    print("[DEPLOY] Installing / refreshing systemd service...")
    ssh(f"cp {REMOTE_DIR}/recorder.service /etc/systemd/system/recorder.service", sudo=True)
    ssh("systemctl daemon-reload", sudo=True)
    ssh("systemctl enable recorder.service", sudo=True)

    print("[DEPLOY] Restarting recorder service...")
    ssh("systemctl restart recorder.service", sudo=True)

    print("[DEPLOY] Waiting 3s for service to start...")
    import time
    time.sleep(3)

    print("[DEPLOY] Service status:")
    ssh("systemctl status recorder.service --no-pager -l", check=False, sudo=True)

    print("\n[DEPLOY] [OK] Done! Open http://192.168.0.115 in your browser.")

if __name__ == "__main__":
    main()
