#!/usr/bin/env python3
"""
MPEG-TS to MP4 Converter for RasbiiRecord
Converts recorded .ts video files to standard .mp4 using FFmpeg.
Supports hardware-accelerated encoding via NVIDIA NVENC (RTX GPUs)
as well as ultra-fast lossless remuxing (-c copy).
"""

import os
import sys
import time
import argparse
import subprocess
import urllib.request
import json
from pathlib import Path

# Fix Windows console encoding if needed
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

PI_DEFAULT_URL = "http://192.168.0.115"

def check_nvenc_available() -> bool:
    """Check if ffmpeg supports h264_nvenc and an NVIDIA GPU is accessible."""
    try:
        test_cmd = [
            "ffmpeg", "-f", "lavfi", "-i", "nullsrc=s=256x256:d=0.1",
            "-c:v", "h264_nvenc", "-f", "null", "-"
        ]
        test_proc = subprocess.run(test_cmd, capture_output=True)
        return test_proc.returncode == 0
    except Exception:
        return False

def fmt_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

def convert_file(input_path: Path, output_path: Path = None, mode: str = "nvenc") -> bool:
    """
    Convert a single .ts file to .mp4.
    mode: 'nvenc' (GPU hardware transcode), 'copy' (lossless stream copy), or 'cpu' (libx264)
    """
    if not input_path.exists():
        print(f"[ERROR] Input file does not exist: {input_path}")
        return False

    if output_path is None:
        output_path = input_path.with_suffix(".mp4")

    print("\n" + "=" * 56)
    print(f"Converting: {input_path.name}")
    print(f"Input size: {fmt_size(input_path.stat().st_size)}")
    print(f"Target:     {output_path.name}")
    print(f"Mode:       {mode.upper()}")
    print("=" * 56)

    t0 = time.time()

    if mode == "copy":
        # Stream copy: ultra-fast, lossless (since source is already H.264 + AAC)
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-i", str(input_path),
            "-c", "copy",
            "-movflags", "+faststart",
            str(output_path)
        ]
    elif mode == "nvenc":
        # GPU NVENC transcode: clean re-encode with RTX NVENC hardware acceleration
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-i", str(input_path),
            "-c:v", "h264_nvenc",
            "-preset", "p4",        # Medium high performance preset
            "-tune", "hq",
            "-cq", "20",            # Constant quality (visually lossless)
            "-b:v", "0",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(output_path)
        ]
    else:  # CPU fallback
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-i", str(input_path),
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "22",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(output_path)
        ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"[ERROR] FFmpeg failed:\n{proc.stderr}")
            return False

        elapsed = time.time() - t0
        out_size = output_path.stat().st_size
        print(f"[SUCCESS] Converted in {elapsed:.2f}s!")
        print(f"Output size: {fmt_size(out_size)} (saved: {output_path})")
        return True
    except Exception as e:
        print(f"[ERROR] Conversion failed: {e}")
        return False

def download_and_convert(pi_url: str = PI_DEFAULT_URL, mode: str = "nvenc"):
    """Fetch footage list from Pi, download latest .ts, and convert it."""
    print(f"[PI] Connecting to Raspberry Pi at {pi_url}...")
    try:
        req = urllib.request.Request(f"{pi_url}/api/footage")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"[ERROR] Could not connect to Pi: {e}")
        return

    ts_files = [f for f in data.get("files", []) if f.get("type") == "video" or f["name"].endswith(".ts")]
    if not ts_files:
        print("[PI] No video recordings found on Pi.")
        return

    latest = ts_files[0]
    filename = latest["name"]
    local_path = Path(filename)
    download_url = f"{pi_url}/api/footage/{filename}"

    print(f"[PI] Found latest recording: {filename} ({fmt_size(latest['size_bytes'])})")
    print(f"[PI] Downloading from {download_url}...")
    urllib.request.urlretrieve(download_url, local_path)
    print(f"[PI] Download complete! ({fmt_size(local_path.stat().st_size)})")

    convert_file(local_path, mode=mode)

def main():
    parser = argparse.ArgumentParser(description="Convert RasbiiRecord .ts videos to MP4 using FFmpeg (NVENC GPU accelerated)")
    parser.add_argument("input", nargs="?", help="Path to .ts file or directory containing .ts files")
    parser.add_argument("--mode", choices=["nvenc", "copy", "cpu"], default=None,
                        help="Conversion mode: 'nvenc' (NVIDIA GPU, default if GPU found), 'copy' (instant stream copy), 'cpu' (libx264)")
    parser.add_argument("--download-latest", action="store_true", help="Download the newest recording from Raspberry Pi and convert it")
    parser.add_argument("--pi-url", default=PI_DEFAULT_URL, help=f"Raspberry Pi Base URL (default: {PI_DEFAULT_URL})")

    args = parser.parse_args()

    # Determine default mode
    nvenc_ok = check_nvenc_available()
    if args.mode is None:
        if nvenc_ok:
            mode = "nvenc"
            print("[INFO] NVIDIA GPU with NVENC detected. Using hardware-accelerated encoding (h264_nvenc).")
        else:
            mode = "copy"
            print("[INFO] NVENC not detected. Using instant lossless stream copy (-c copy).")
    else:
        mode = args.mode

    if args.download_latest:
        download_and_convert(pi_url=args.pi_url, mode=mode)
        return

    if not args.input:
        # Check current directory for .ts files
        ts_files = sorted(Path(".").glob("*.ts"), key=lambda p: p.stat().st_mtime, reverse=True)
        if ts_files:
            print(f"[INFO] Found {len(ts_files)} .ts file(s) in current directory.")
            for f in ts_files:
                convert_file(f, mode=mode)
            return
        else:
            parser.print_help()
            print("\n[HINT] Pass a .ts file: python convert_ts_to_mp4.py video.ts")
            print("[HINT] Or download latest from Pi: python convert_ts_to_mp4.py --download-latest")
            return

    input_path = Path(args.input)
    if input_path.is_dir():
        ts_files = sorted(input_path.glob("*.ts"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not ts_files:
            print(f"[WARN] No .ts files found in {input_path}")
            return
        print(f"[INFO] Found {len(ts_files)} .ts files in {input_path}")
        for f in ts_files:
            convert_file(f, mode=mode)
    else:
        convert_file(input_path, mode=mode)

if __name__ == "__main__":
    main()
