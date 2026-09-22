#!/usr/bin/env python3
"""
Trigger Simulation CLI for Raspberry Pi Zero 2 W Recorder
Enables automated testing and headless button simulation.
Connects to the daemon's Unix socket or runs directly.
"""

import os
import sys
import time
import json
import socket
import argparse
import subprocess
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config


def send_daemon_command(cmd, timeout=30.0):
    sock_path = config.IPC_SOCKET_PATH
    if not os.path.exists(sock_path):
        return False, f"Socket {sock_path} does not exist. Is recorder.py daemon running?"

    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
        client.connect(sock_path)
        client.sendall(cmd.encode('utf-8') + b"\n")
        resp = client.recv(4096).decode('utf-8')
        client.close()
        return True, json.loads(resp)
    except Exception as e:
        return False, f"IPC Error: {e}"


def run_direct_photo():
    """Fallback: capture photo directly without daemon."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    photo_file = config.DCIM_DIR / f"IMG_{timestamp}.jpg"
    cmd = [
        "rpicam-still",
        "-t", str(config.PHOTO_WARMUP_MS),
        "--width", str(config.PHOTO_WIDTH),
        "--height", str(config.PHOTO_HEIGHT),
        "--exposure", config.EXPOSURE_MODE,
        "--metering", config.METERING_MODE,
        "--awb", config.AWB_MODE,
        "-q", str(config.PHOTO_QUALITY),
        "--nopreview",
        "-o", str(photo_file)
    ]
    print(f"[DIRECT] Capturing photo to {photo_file}...")
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode == 0 and photo_file.exists():
        print(f"[DIRECT SUCCESS] Saved {photo_file} ({photo_file.stat().st_size} bytes)")
        return True, str(photo_file)
    else:
        print(f"[DIRECT ERROR] {res.stderr}")
        return False, res.stderr


def run_direct_record(duration_sec=5):
    """Fallback: record video directly without daemon."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    video_file = config.DCIM_DIR / f"VID_{timestamp}{config.CONTAINER_EXT}"
    cmd = [
        "rpicam-vid",
        "-t", str(int(duration_sec * 1000)),
        "--width", str(config.VIDEO_WIDTH),
        "--height", str(config.VIDEO_HEIGHT),
        "--framerate", str(config.VIDEO_FPS),
        "--bitrate", str(config.VIDEO_BITRATE),
        "--codec", config.VIDEO_CODEC,
        "--libav-video-codec", config.VIDEO_LIB_CODEC,
        "--libav-audio",
        "--audio-source", config.AUDIO_SOURCE,
        "--audio-device", config.AUDIO_DEVICE,
        "--audio-codec", config.AUDIO_CODEC,
        "--audio-bitrate", config.AUDIO_BITRATE,
        "--audio-samplerate", str(config.AUDIO_SAMPLERATE),
        "--audio-channels", str(config.AUDIO_CHANNELS),
        "--av-sync", f"{config.AUDIO_AV_SYNC_US}us",
        "--nopreview",
        "-o", str(video_file)
    ]
    print(f"[DIRECT] Recording {duration_sec}s video to {video_file}...")
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode == 0 and video_file.exists():
        print(f"[DIRECT SUCCESS] Saved {video_file} ({video_file.stat().st_size} bytes)")
        return True, str(video_file)
    else:
        print(f"[DIRECT ERROR] {res.stderr}")
        return False, res.stderr


def main():
    parser = argparse.ArgumentParser(description="Trigger simulation for Raspberry Pi Recorder")
    subparsers = parser.add_subparsers(dest="action", help="Action to simulate")

    subparsers.add_parser("photo", help="Simulate Button 1 press (Capture Photo)")
    subparsers.add_parser("record-start", help="Simulate Button 2 press (Start Recording)")
    subparsers.add_parser("record-stop", help="Simulate Button 2 press (Stop Recording)")
    subparsers.add_parser("record-toggle", help="Simulate Button 2 press (Toggle Recording)")
    subparsers.add_parser("status", help="Get daemon status")

    record_for = subparsers.add_parser("record-for", help="Start recording, wait N seconds, then stop")
    record_for.add_argument("seconds", type=float, default=5.0, help="Duration to record in seconds")

    direct_p = subparsers.add_parser("direct-photo", help="Direct standalone photo capture")
    direct_r = subparsers.add_parser("direct-record", help="Direct standalone video recording")
    direct_r.add_argument("seconds", type=float, default=5.0, help="Duration to record in seconds")

    args = parser.parse_args()

    if not args.action:
        parser.print_help()
        sys.exit(1)

    if args.action == "photo":
        print("[SIMULATE] Triggering Button 1 (Photo Capture)...")
        ok, res = send_daemon_command("PHOTO", timeout=30.0)
        print(f"Result: {json.dumps(res, indent=2) if ok else res}")

    elif args.action == "record-start":
        print("[SIMULATE] Triggering Button 2 (Record Start)...")
        ok, res = send_daemon_command("RECORD_START")
        print(f"Result: {json.dumps(res, indent=2) if ok else res}")

    elif args.action == "record-stop":
        print("[SIMULATE] Triggering Button 2 (Record Stop)...")
        ok, res = send_daemon_command("RECORD_STOP")
        print(f"Result: {json.dumps(res, indent=2) if ok else res}")

    elif args.action == "record-toggle":
        print("[SIMULATE] Triggering Button 2 (Record Toggle)...")
        ok, res = send_daemon_command("RECORD_TOGGLE")
        print(f"Result: {json.dumps(res, indent=2) if ok else res}")

    elif args.action == "record-for":
        dur = args.seconds
        print(f"[SIMULATE] Starting record for {dur} seconds...")
        ok, res = send_daemon_command("RECORD_START")
        if not ok or not res.get("success"):
            print(f"Failed to start: {res}")
            sys.exit(1)
        print(f"[SIMULATE] Recording active. Sleeping {dur}s...")
        time.sleep(dur)
        print("[SIMULATE] Stopping record...")
        ok, res = send_daemon_command("RECORD_STOP")
        print(f"Result: {json.dumps(res, indent=2) if ok else res}")

    elif args.action == "status":
        ok, res = send_daemon_command("STATUS")
        print(f"Daemon Status: {json.dumps(res, indent=2) if ok else res}")

    elif args.action == "direct-photo":
        run_direct_photo()

    elif args.action == "direct-record":
        run_direct_record(args.seconds)


if __name__ == "__main__":
    main()
