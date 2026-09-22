import sys
from pathlib import Path

# Add tests directory to sys.path to find scratch_ssh
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import scratch_ssh

def main():
    cmd = """python3 - << 'EOF'
import subprocess
cmd = ["ffmpeg", "-loglevel", "info", "-nostats", "-y", "-f", "alsa", "-ar", "48000", "-ac", "2", "-i", "sysdefault:CARD=Audio", "-t", "2", "-c:a", "aac", "/tmp/test_sysdef_mux.ts"]
p = subprocess.Popen(cmd, stderr=subprocess.PIPE)
out, err = p.communicate(timeout=5)
print("FFMPEG LOG:\n", err.decode(errors="replace")[-600:])
EOF
"""
    scratch_ssh.run(cmd)

if __name__ == "__main__":
    main()
