#!/usr/bin/env python3
"""
Comprehensive Camera Benchmark & Performance Comparison:
1. Raspberry Pi Camera Module 3 (IMX708 CSI) @ 1080p 30fps + Audio
2. USB Webcam (/dev/video2) @ 1080p 30fps + Audio

Measures:
- Real achieved FPS & Frame drop / jitter
- CPU usage % (overall & 4-core breakdown)
- RAM usage & OOM risk
- SoC Temperature (°C)
- Hardware acceleration vs Software processing impact
"""

import os
import sys
import time
import json
import signal
import subprocess
import threading
from pathlib import Path
import psutil
import cv2

OUTPUT_DIR = Path("/tmp/bench_results")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

AUDIO_DEVICE = "plughw:CARD=Webcams,DEV=0"
TEST_DURATION_SEC = 12

def get_cpu_temp():
    try:
        out = subprocess.check_output(["vcgencmd", "measure_temp"], text=True)
        return float(out.replace("temp=", "").replace("'C\n", "").strip())
    except Exception:
        return 0.0

class MetricCollector:
    def __init__(self, interval=0.5):
        self.interval = interval
        self.running = False
        self.thread = None
        self.samples = []

    def start(self):
        self.running = True
        self.samples = []
        psutil.cpu_percent(interval=None)  # prime
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while self.running:
            cpu_tot = psutil.cpu_percent(interval=None)
            cpu_cores = psutil.cpu_percent(interval=None, percpu=True)
            mem = psutil.virtual_memory()
            temp = get_cpu_temp()
            self.samples.append({
                "time": time.time(),
                "cpu_total": cpu_tot,
                "cpu_cores": cpu_cores,
                "ram_used_mb": mem.used / (1024 * 1024),
                "ram_avail_mb": mem.available / (1024 * 1024),
                "temp_c": temp
            })
            time.sleep(self.interval)

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2.0)
        return self.summary()

    def summary(self):
        if not self.samples:
            return {}
        valid = [s for s in self.samples if s["cpu_total"] > 0]
        if not valid:
            valid = self.samples
        cpu_totals = [s["cpu_total"] for s in valid]
        temps = [s["temp_c"] for s in valid]
        ram_used = [s["ram_used_mb"] for s in valid]
        return {
            "avg_cpu_percent": round(sum(cpu_totals) / len(cpu_totals), 1),
            "max_cpu_percent": round(max(cpu_totals), 1),
            "min_cpu_percent": round(min(cpu_totals), 1),
            "start_temp_c": temps[0] if temps else 0,
            "max_temp_c": max(temps) if temps else 0,
            "end_temp_c": temps[-1] if temps else 0,
            "avg_ram_used_mb": round(sum(ram_used) / len(ram_used), 1),
            "peak_ram_used_mb": round(max(ram_used), 1),
            "sample_count": len(valid)
        }

def analyze_video_file(filepath):
    if not Path(filepath).exists() or os.path.getsize(filepath) == 0:
        return {"error": "File does not exist or is empty"}
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate,avg_frame_rate,nb_frames,width,height,duration:format=duration,size,bit_rate",
        "-of", "json",
        str(filepath)
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        probe = json.loads(res.stdout)
    except Exception as e:
        return {"error": f"ffprobe error: {e}"}

    pts_cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "packet=pts_time",
        "-of", "csv=p=0",
        str(filepath)
    ]
    deltas = []
    total_packets = 0
    try:
        pts_res = subprocess.run(pts_cmd, capture_output=True, text=True, timeout=15)
        pts_list = [float(p.strip()) for p in pts_res.stdout.splitlines() if p.strip()]
        total_packets = len(pts_list)
        if len(pts_list) > 1:
            for i in range(1, len(pts_list)):
                d = pts_list[i] - pts_list[i-1]
                if d > 0:
                    deltas.append(d)
    except Exception:
        pass

    stream_info = probe.get("streams", [{}])[0]
    format_info = probe.get("format", {})
    file_size_mb = round(int(format_info.get("size", 0)) / (1024 * 1024), 2)
    duration = float(format_info.get("duration", stream_info.get("duration", 0)))
    
    avg_fps = 0.0
    if duration > 0 and total_packets > 0:
        avg_fps = round(total_packets / duration, 2)
    elif stream_info.get("avg_frame_rate"):
        num, den = stream_info["avg_frame_rate"].split("/")
        if float(den) > 0:
            avg_fps = round(float(num) / float(den), 2)

    jitter_ms = 0.0
    if deltas:
        mean_d = sum(deltas) / len(deltas)
        variance = sum((d - mean_d) ** 2 for d in deltas) / len(deltas)
        jitter_ms = round((variance ** 0.5) * 1000, 2)

    return {
        "file_size_mb": file_size_mb,
        "duration_sec": round(duration, 2),
        "total_frames": total_packets,
        "measured_avg_fps": avg_fps,
        "fps_jitter_ms": jitter_ms,
        "min_frame_interval_ms": round(min(deltas) * 1000, 1) if deltas else 0,
        "max_frame_interval_ms": round(max(deltas) * 1000, 1) if deltas else 0,
        "width": stream_info.get("width"),
        "height": stream_info.get("height")
    }

# ─────────────────────────────────────────────────────────────────────────────
# TEST 1: CSI Camera Module 3 (IMX708) @ 1080p 30fps + Audio
# ─────────────────────────────────────────────────────────────────────────────
def run_csi_test():
    print("\n" + "="*60, flush=True)
    print(">>> [TEST 1] CSI Camera Module 3 (IMX708) 1080p @ 30 FPS + Audio", flush=True)
    print("="*60, flush=True)
    out_file = OUTPUT_DIR / "csi_imx708_1080p30.ts"
    if out_file.exists():
        out_file.unlink()

    from picamera2 import Picamera2
    from picamera2.encoders import H264Encoder
    from picamera2.outputs.output import Output

    class AlsaOutput(Output):
        def __init__(self, filename):
            super().__init__()
            cmd = [
                "ffmpeg", "-loglevel", "warning", "-nostats", "-y",
                "-thread_queue_size", "1024",
                "-use_wallclock_as_timestamps", "1",
                "-f", "alsa", "-ac", "1", "-ar", "48000", "-i", AUDIO_DEVICE,
                "-thread_queue_size", "1024",
                "-r", "30",
                "-use_wallclock_as_timestamps", "1",
                "-f", "h264", "-i", "-",
                "-c:a", "aac", "-b:a", "128k",
                "-c:v", "copy",
                "-r", "30",
                str(filename)
            ]
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            super().start()

        def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
            if frame and self.proc and self.proc.stdin:
                try:
                    self.proc.stdin.write(frame)
                    self.proc.stdin.flush()
                except Exception:
                    pass

        def close(self):
            super().stop()
            if self.proc:
                try:
                    if self.proc.stdin:
                        self.proc.stdin.close()
                    self.proc.send_signal(signal.SIGINT)
                    self.proc.wait(timeout=2.0)
                except Exception:
                    self.proc.kill()

    print("[CSI] Initializing Picamera2...", flush=True)
    picam2 = Picamera2()
    video_config = picam2.create_video_configuration(
        main={"size": (1920, 1080), "format": "YUV420"},
        controls={"FrameRate": 30}
    )
    video_config["sensor"] = {"output_size": (2304, 1296), "bit_depth": 10}
    picam2.configure(video_config)
    picam2.set_controls({"AfMode": 2})  # Continuous AF
    picam2.start()
    time.sleep(1.0)

    encoder = H264Encoder(bitrate=6000000)
    output = AlsaOutput(out_file)

    collector = MetricCollector(interval=0.5)
    print(f"[CSI] Recording 1080p 30fps for {TEST_DURATION_SEC}s...", flush=True)
    collector.start()
    picam2.start_recording(encoder, output)
    time.sleep(TEST_DURATION_SEC)
    picam2.stop_recording()
    output.close()
    picam2.stop()
    picam2.close()
    metrics = collector.stop()

    print("[CSI] Analyzing recorded video...", flush=True)
    video_analysis = analyze_video_file(out_file)
    return {"metrics": metrics, "analysis": video_analysis}

# ─────────────────────────────────────────────────────────────────────────────
# TEST 2: USB Webcam (/dev/video2) 1080p 30fps Capture + Audio
# ─────────────────────────────────────────────────────────────────────────────
def run_usb_test():
    print("\n" + "="*60, flush=True)
    print(">>> [TEST 2] USB Webcam (/dev/video2) 1080p @ 30 FPS Capture + Audio", flush=True)
    print("="*60, flush=True)

    # Concurrently record audio via arecord
    audio_wav = OUTPUT_DIR / "usb_mic_bench.wav"
    if audio_wav.exists():
        audio_wav.unlink()
    
    audio_proc = subprocess.Popen([
        "arecord", "-D", AUDIO_DEVICE, "-f", "S16_LE", "-r", "48000",
        "-c", "1", "-d", str(TEST_DURATION_SEC), str(audio_wav)
    ], stderr=subprocess.DEVNULL)

    print("[USB] Opening /dev/video2 with OpenCV V4L2...", flush=True)
    cap = cv2.VideoCapture(2, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    cap.set(cv2.CAP_PROP_FPS, 30)

    # Warmup
    for _ in range(5):
        cap.read()

    collector = MetricCollector(interval=0.5)
    print(f"[USB] Streaming & decoding 1080p frames for {TEST_DURATION_SEC}s...", flush=True)
    
    frame_times = []
    collector.start()
    start_time = time.time()
    
    while time.time() - start_time < TEST_DURATION_SEC:
        ret, _ = cap.read()
        if ret:
            frame_times.append(time.time())

    metrics = collector.stop()
    cap.release()
    try:
        audio_proc.wait(timeout=3.0)
    except Exception:
        audio_proc.kill()

    # Calculate actual delivered frame rate & stability
    total_frames = len(frame_times)
    elapsed = frame_times[-1] - frame_times[0] if len(frame_times) > 1 else TEST_DURATION_SEC
    actual_fps = round(total_frames / elapsed, 2) if elapsed > 0 else 0.0

    deltas = []
    if len(frame_times) > 1:
        for i in range(1, len(frame_times)):
            d = frame_times[i] - frame_times[i-1]
            deltas.append(d)

    jitter_ms = 0.0
    if deltas:
        mean_d = sum(deltas) / len(deltas)
        variance = sum((d - mean_d) ** 2 for d in deltas) / len(deltas)
        jitter_ms = round((variance ** 0.5) * 1000, 2)

    expected_frames = int(TEST_DURATION_SEC * 30)
    dropped_frames = max(0, expected_frames - total_frames)

    analysis = {
        "duration_sec": round(elapsed, 2),
        "total_frames_captured": total_frames,
        "expected_frames_at_30fps": expected_frames,
        "dropped_frames": dropped_frames,
        "measured_avg_fps": actual_fps,
        "fps_jitter_ms": jitter_ms,
        "min_frame_interval_ms": round(min(deltas) * 1000, 1) if deltas else 0,
        "max_frame_interval_ms": round(max(deltas) * 1000, 1) if deltas else 0,
        "audio_file_recorded": audio_wav.exists() and os.path.getsize(audio_wav) > 0,
        "audio_size_bytes": os.path.getsize(audio_wav) if audio_wav.exists() else 0
    }

    return {"metrics": metrics, "analysis": analysis}

def main():
    print("Starting Comparative Camera Benchmark on Pi Zero 2 W...", flush=True)
    results = {}

    # 1. CSI Camera Test
    try:
        results["csi_imx708"] = run_csi_test()
    except Exception as e:
        results["csi_imx708"] = {"error": str(e)}

    time.sleep(3.0)

    # 2. USB Webcam Test
    try:
        results["usb_webcam"] = run_usb_test()
    except Exception as e:
        results["usb_webcam"] = {"error": str(e)}

    res_path = OUTPUT_DIR / "benchmark_summary.json"
    with open(res_path, "w") as f:
        json.dump(results, f, indent=2)
    print("\n" + "="*60, flush=True)
    print("BENCHMARK COMPLETED SUCCESSFULLY!", flush=True)
    print(json.dumps(results, indent=2), flush=True)
    print("="*60, flush=True)

if __name__ == "__main__":
    main()
