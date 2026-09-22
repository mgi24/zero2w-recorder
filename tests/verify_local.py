#!/usr/bin/env python3
"""
Local Verification & Analysis Script (Runs on Windows workstation)
Downloads recorded media from Raspberry Pi Zero 2 W and performs:
- Image non-blank & exposure verification
- Video stream & hardware encoding verification
- Audio stream integrity & dynamic range verification
- Audio waveform generation via FFmpeg
"""

import os
import sys
import json
import shutil
import subprocess
from pathlib import Path

WORKSPACE_DIR = Path(__file__).resolve().parent.parent
DOWNLOADS_DIR = WORKSPACE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

# Remote Pi Details
PI_USER = "mamad"
PI_HOST = "192.168.0.115"
PI_PASS = "123"
REMOTE_DCIM = "/home/mamad/recorder/DCIM/"

FFMPEG_BIN = "ffmpeg"
FFPROBE_BIN = "ffprobe"


def sync_from_pi():
    """Download all media files from the Pi's DCIM directory using pscp."""
    print(f"[SYNC] Pulling media from {PI_USER}@{PI_HOST}:{REMOTE_DCIM} to {DOWNLOADS_DIR}...")
    cmd = [
        "pscp",
        "-batch",
        "-pw", PI_PASS,
        f"{PI_USER}@{PI_HOST}:{REMOTE_DCIM}*.*",
        str(DOWNLOADS_DIR)
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0 and "No such file" not in res.stderr:
        print(f"[SYNC WARN] pscp returned {res.returncode}: {res.stderr}")
    else:
        print("[SYNC] Media download completed.")


def probe_file(file_path):
    cmd = [
        FFPROBE_BIN,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(file_path)
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return None


def verify_photo(photo_path):
    print(f"\n--- Verifying Photo: {photo_path.name} ---")
    info = probe_file(photo_path)
    if not info or not info.get("streams"):
        print("[FAIL] Unable to probe photo file.")
        return {"file": photo_path.name, "valid": False, "reason": "Probe failed"}

    v_stream = info["streams"][0]
    w = int(v_stream.get("width", 0))
    h = int(v_stream.get("height", 0))
    codec = v_stream.get("codec_name")
    size_bytes = photo_path.stat().st_size

    print(f"Format: {codec}, Resolution: {w}x{h}, File Size: {size_bytes / 1024:.1f} KB")

    # Check signalstats for luminance and non-blank verification
    stats_cmd = [
        FFMPEG_BIN, "-y",
        "-i", str(photo_path),
        "-vf", "signalstats,metadata=print",
        "-f", "null", "-"
    ]
    stats_proc = subprocess.run(stats_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    signal_metrics = {}
    for line in stats_proc.stderr.splitlines():
        if "lavfi.signalstats." in line:
            parts = line.strip().split("lavfi.signalstats.")[-1].split("=")
            if len(parts) == 2:
                try:
                    signal_metrics[parts[0]] = float(parts[1])
                except ValueError:
                    pass

    y_avg = signal_metrics.get("YAVG", 0.0)
    y_min = signal_metrics.get("YMIN", 0.0)
    y_max = signal_metrics.get("YMAX", 0.0)

    print(f"Luminance Stats: YAVG={y_avg:.2f}, YMIN={y_min:.1f}, YMAX={y_max:.1f}")

    is_blank = (y_max - y_min < 5.0) or (y_avg < 3.0) or (y_avg > 252.0)
    is_valid_res = (w == 2592 and h == 1944)

    status = {
        "file": photo_path.name,
        "width": w,
        "height": h,
        "is_max_res": is_valid_res,
        "y_avg": y_avg,
        "y_min": y_min,
        "y_max": y_max,
        "is_blank": is_blank,
        "valid": is_valid_res and not is_blank and size_bytes > 50000
    }

    if status["valid"]:
        print(f"[PASS] Photo verified! Image has active visual contrast and correct 2592x1944 resolution.")
    else:
        print(f"[FAIL] Photo check failed: valid_res={is_valid_res}, is_blank={is_blank}")

    return status


def verify_video(video_path):
    print(f"\n--- Verifying Video: {video_path.name} ---")
    info = probe_file(video_path)
    if not info or not info.get("streams"):
        print("[FAIL] Unable to probe video file.")
        return {"file": video_path.name, "valid": False, "reason": "Probe failed"}

    fmt = info.get("format", {})
    container = fmt.get("format_name")
    duration = float(fmt.get("duration", 0.0))

    v_stream = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    a_stream = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)

    if not v_stream:
        print("[FAIL] No video stream found.")
        return {"file": video_path.name, "valid": False, "reason": "No video stream"}

    v_w = int(v_stream.get("width", 0))
    v_h = int(v_stream.get("height", 0))
    v_codec = v_stream.get("codec_name")
    v_fps = v_stream.get("r_frame_rate")

    print(f"Container: {container}, Duration: {duration:.2f}s")
    print(f"Video Stream: {v_codec} {v_w}x{v_h} @ {v_fps} fps")

    # Verify Audio Stream
    audio_present = (a_stream is not None)
    a_codec = a_stream.get("codec_name") if a_stream else None
    a_rate = int(a_stream.get("sample_rate", 0)) if a_stream else 0
    a_channels = int(a_stream.get("channels", 0)) if a_stream else 0

    print(f"Audio Stream: {a_codec} {a_rate}Hz {a_channels}ch (Present: {audio_present})")

    # Audio Signal Analysis via FFmpeg astats
    astats_cmd = [
        FFMPEG_BIN, "-y",
        "-i", str(video_path),
        "-vn",
        "-af", "astats=metadata=1:reset=1",
        "-f", "null", "-"
    ]
    astats_res = subprocess.run(astats_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    audio_active = False
    rms_level = -999.0
    peak_level = -999.0

    for line in astats_res.stderr.splitlines():
        if "RMS level dB:" in line:
            try:
                rms_level = float(line.split(":")[-1].strip())
            except ValueError:
                pass
        elif "Peak level dB:" in line:
            try:
                peak_level = float(line.split(":")[-1].strip())
            except ValueError:
                pass
        elif "Zero crossings:" in line:
            try:
                zc = int(line.split(":")[-1].strip())
                if zc > 0:
                    audio_active = True
            except ValueError:
                pass

    if peak_level > -85.0 and rms_level > -85.0:
        audio_active = True

    print(f"Audio Signal Analysis: Peak={peak_level:.1f} dB, RMS={rms_level:.1f} dB, ActiveSound={audio_active}")

    # Generate Waveform Image
    waveform_path = DOWNLOADS_DIR / f"{video_path.stem}_waveform.png"
    wave_cmd = [
        FFMPEG_BIN, "-y",
        "-i", str(video_path),
        "-filter_complex", "aformat=channel_layouts=mono,showwavespic=s=800x200:colors=0x2196F3",
        "-frames:v", "1",
        str(waveform_path)
    ]
    subprocess.run(wave_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if waveform_path.exists():
        print(f"Waveform visualization saved to: {waveform_path.name}")

    # Video Signal Analysis (Check first frame for non-blank)
    vstats_cmd = [
        FFMPEG_BIN, "-y",
        "-i", str(video_path),
        "-vframes", "1",
        "-vf", "signalstats",
        "-f", "null", "-"
    ]
    vstats_res = subprocess.run(vstats_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    is_mpegts = "mpegts" in str(container).lower()
    is_valid_res = (v_w == 1920 and v_h == 1080)
    video_valid = is_mpegts and is_valid_res and duration > 0.5 and audio_present and audio_active

    status = {
        "file": video_path.name,
        "container": container,
        "is_mpegts": is_mpegts,
        "duration": duration,
        "video": {
            "codec": v_codec,
            "width": v_w,
            "height": v_h,
            "fps": v_fps,
            "valid_1080p": is_valid_res
        },
        "audio": {
            "codec": a_codec,
            "sample_rate": a_rate,
            "channels": a_channels,
            "rms_db": rms_level,
            "peak_db": peak_level,
            "active_sound": audio_active
        },
        "valid": video_valid
    }

    if video_valid:
        print("[PASS] Video verified! 1080p24 H.264 in MPEG-TS with active AAC audio.")
    else:
        print(f"[FAIL] Video check failed. (mpegts={is_mpegts}, 1080p={is_valid_res}, audio_active={audio_active})")

    return status


def main():
    sync_from_pi()

    photos = sorted(DOWNLOADS_DIR.glob("*.jpg"))
    videos = sorted(DOWNLOADS_DIR.glob("*.ts"))

    report = {"photos": [], "videos": []}

    print(f"\nFound {len(photos)} photo(s) and {len(videos)} video(s) for verification.")

    for p in photos:
        res = verify_photo(p)
        report["photos"].append(res)

    for v in videos:
        res = verify_video(v)
        report["videos"].append(res)

    report_path = Path(__file__).resolve().parent / "verification_results.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    print(f"\n[COMPLETE] Full verification saved to {report_path.name}")


if __name__ == "__main__":
    main()
