#!/usr/bin/env python3
"""
Raspberry Pi Zero 2 W - Camera & Audio Recorder Daemon
Author: Antigravity AI
Handles GPIO Buttons, Indicator LEDs, Auto-Exposure Photos,
Hardware-Accelerated MPEG-TS Video+Audio Recording,
and a built-in HTTP Web UI on port 80.
"""

import os
import sys
import time
import json
import socket
import queue
import signal
import threading
import subprocess
import urllib.parse
import mimetypes
import collections
from datetime import datetime
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from io import BytesIO

import config

PID_FILE = "/tmp/recorder.pid"
WEB_PORT = 80

# ─────────────────────────────────────────
# Logging & Ring Buffer (Last 1000 lines) + Continuous log.txt Dump
# ─────────────────────────────────────────
_log_buffer = collections.deque(maxlen=getattr(config, 'LOG_BUFFER_SIZE', 1000))
_log_file_lock = threading.Lock()

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    _log_buffer.append(line)
    try:
        log_file = getattr(config, 'LOG_FILE_PATH', config.DCIM_DIR / "log.txt")
        with _log_file_lock:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
    except Exception:
        pass


# ─────────────────────────────────────────
# Picamera2 & GPIO: real or mock
# ─────────────────────────────────────────
try:
    from picamera2 import Picamera2
    from picamera2.encoders import H264Encoder
    from picamera2.outputs.output import Output
    PICAMERA2_AVAILABLE = True

    class AlsaFfmpegOutput(Output):
        """FFmpeg output muxer supporting native ALSA audio input directly on Raspberry Pi."""
        def __init__(self, output_filename, audio=True, audio_device="plughw:CARD=Audio,DEV=0",
                     audio_samplerate=48000, audio_codec="aac", audio_bitrate="128k"):
            super().__init__()
            self.output_filename = str(output_filename)
            self.audio_device = audio_device
            self.audio = bool(audio and audio_device and audio_device != "none")
            self.audio_samplerate = audio_samplerate
            self.audio_codec = audio_codec
            self.audio_bitrate = audio_bitrate
            self.ffmpeg = None
            self.needs_pacing = True
            self.last_frame_time = time.time()
            self.error_reason = None

        def start(self):
            general_options = ['-loglevel', 'info', '-nostats', '-y']
            video_input = [
                '-thread_queue_size', '1024',
                '-r', str(config.VIDEO_FPS),
                '-use_wallclock_as_timestamps', '1',
                '-f', 'h264',
                '-i', '-'
            ]
            video_codec = ['-c:v', 'copy']
            audio_input = []
            audio_codec = []
            if self.audio and self.audio_device and self.audio_device != "none":
                audio_input = [
                    '-thread_queue_size', '1024',
                    '-use_wallclock_as_timestamps', '1',
                    '-f', 'alsa',
                    '-ar', str(self.audio_samplerate),
                    '-ac', '2',
                    '-i', self.audio_device
                ]
                bitrate_val = str(self.audio_bitrate).lower().replace("bps", "")
                audio_codec = [
                    '-c:a', self.audio_codec,
                    '-b:a', bitrate_val
                ]

            command = ['ffmpeg'] + general_options + audio_input + video_input + \
                      audio_codec + video_codec + ['-r', str(config.VIDEO_FPS), self.output_filename]
            self.last_frame_time = time.time()
            self.error_reason = None
            try:
                self.log_file = open("/tmp/ffmpeg_record.log", "w")
            except Exception:
                self.log_file = subprocess.DEVNULL
            self.ffmpeg = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=self.log_file)
            super().start()

        def stop(self):
            super().stop()
            if self.ffmpeg is not None:
                # 1. Signal FFmpeg to finalize container (SIGINT triggers FFmpeg clean exit)
                try:
                    self.ffmpeg.send_signal(signal.SIGINT)
                    self.ffmpeg.wait(timeout=0.8)
                except subprocess.TimeoutExpired:
                    try:
                        self.ffmpeg.terminate()
                        self.ffmpeg.wait(timeout=0.4)
                    except subprocess.TimeoutExpired:
                        try:
                            self.ffmpeg.kill()  # Force SIGKILL if still hung on ALSA
                            self.ffmpeg.wait(timeout=0.3)
                        except Exception:
                            pass
                    except Exception:
                        pass
                except Exception:
                    try:
                        self.ffmpeg.kill()
                    except Exception:
                        pass

                # 2. Close stdin pipe safely without blocking
                try:
                    if self.ffmpeg.stdin:
                        try:
                            os.close(self.ffmpeg.stdin.fileno())
                        except Exception:
                            pass
                        self.ffmpeg.stdin.close()
                except Exception:
                    pass
                self.ffmpeg = None
            if hasattr(self, 'log_file') and self.log_file and self.log_file != subprocess.DEVNULL:
                try:
                    self.log_file.close()
                except Exception:
                    pass

        def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
            if self.recording and self.ffmpeg:
                self.first_frame_received = True
                self.last_frame_time = time.time()
                try:
                    self.ffmpeg.stdin.write(frame)
                    self.ffmpeg.stdin.flush()
                except Exception as e:
                    # Check FFmpeg log for exact root cause
                    tail = ""
                    try:
                        with open("/tmp/ffmpeg_record.log", "r") as f:
                            tail = f.read()[-200:]
                    except Exception:
                        pass
                    if any(k in tail for k in ["Input/output error", "cannot open audio device", "Device or resource busy", "ALSA read error"]):
                        self.error_reason = f"Audio/Mic device failed ({self.audio_device}): {tail.strip()}"
                    else:
                        self.error_reason = f"FFmpeg pipe write failed (process dead / mic dropped): {e}"
                    log(f"[ALSA FFMPEG PIPE ERROR] {self.error_reason}")
                    self.ffmpeg = None

        def is_healthy(self):
            """Check if FFmpeg process is alive and camera frames are actively arriving."""
            if not self.recording:
                return True
            if self.ffmpeg is None:
                if not self.error_reason:
                    self.error_reason = "FFmpeg process is None (pipe broken / terminated)"
                return False

            ret = self.ffmpeg.poll()
            if ret is not None:
                err_text = ""
                try:
                    with open("/tmp/ffmpeg_record.log", "r") as f:
                        err_text = f.read()[-300:]
                except Exception:
                    pass
                if any(k in err_text for k in ["Input/output error", "cannot open audio device", "Device or resource busy", "ALSA read error", "No such device"]):
                    self.error_reason = f"Audio/Microphone device error ({self.audio_device}): {err_text.strip()[-180:]}"
                else:
                    self.error_reason = f"Audio/FFmpeg process exited unexpectedly (code {ret}): {err_text.strip()[-180:]}"
                return False

            # Check log file directly for audio device errors
            try:
                with open("/tmp/ffmpeg_record.log", "r") as f:
                    log_tail = f.read()
                    if any(k in log_tail for k in ["ALSA read error", "No such device", "Input/output error", "cannot open audio device"]):
                        self.error_reason = f"Audio hardware error: {log_tail.strip()[-180:]}"
                        return False
            except Exception:
                pass

            # Check if camera frame delivery stalled
            gap = time.time() - self.last_frame_time
            if not getattr(self, 'first_frame_received', False):
                startup_limit = getattr(config, 'WATCHDOG_STARTUP_GRACE_SEC', 15.0)
                if gap > startup_limit:
                    # Check if audio error logged
                    try:
                        with open("/tmp/ffmpeg_record.log", "r") as f:
                            tail = f.read()
                            if any(k in tail for k in ["Input/output error", "cannot open audio device", "Device or resource busy"]):
                                self.error_reason = f"Audio device error on startup: {tail.strip()[-180:]}"
                                return False
                    except Exception:
                        pass
                    self.error_reason = f"Camera encoder failed to start: No initial frame after {gap:.1f}s (> {startup_limit}s)"
                    return False
            else:
                timeout_limit = getattr(config, 'WATCHDOG_FRAME_TIMEOUT_SEC', 5.0)
                if gap > timeout_limit:
                    self.error_reason = f"Camera stream stalled: No frame received for {gap:.1f}s (> {timeout_limit}s)"
                    return False

            return True

except (ImportError, Exception) as e:
    PICAMERA2_AVAILABLE = False
    log(f"[WARN] Picamera2 not available: {e}. Camera will run in mock mode.")
    class AlsaFfmpegOutput:
        def __init__(self, *args, **kwargs):
            self.error_reason = None
        def is_healthy(self): return True

try:
    from gpiozero import Button, LED
    GPIO_AVAILABLE = True
except (ImportError, Exception) as e:
    GPIO_AVAILABLE = False
    log(f"[WARN] gpiozero not available: {e}. Running in mock mode.")


class MockLED:
    def __init__(self, pin):
        self.pin = pin
        self.value = 0
        self.is_lit = False

    def on(self):
        self.value = 1
        self.is_lit = True
        log(f"[GPIO MOCK] LED (GPIO {self.pin}) -> ON")

    def off(self):
        self.value = 0
        self.is_lit = False
        log(f"[GPIO MOCK] LED (GPIO {self.pin}) -> OFF")

    def close(self):
        pass


class MockButton:
    def __init__(self, pin, bounce_time=None):
        self.pin = pin
        self.bounce_time = bounce_time
        self.when_pressed = None

    def close(self):
        pass


# ─────────────────────────────────────────
# SSE Broadcaster
# ─────────────────────────────────────────
class SSEBroadcaster:
    """Fan-out Server-Sent Events to all connected browser clients."""

    def __init__(self):
        self._lock = threading.Lock()
        self._clients = []  # list of queue.Queue

    def subscribe(self):
        import queue
        q = queue.Queue(maxsize=20)
        with self._lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            try:
                self._clients.remove(q)
            except ValueError:
                pass

    def broadcast(self, event_type: str, data: dict):
        payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
        with self._lock:
            dead = []
            for q in self._clients:
                try:
                    q.put_nowait(payload)
                except Exception:
                    dead.append(q)
            for q in dead:
                self._clients.remove(q)


# Global broadcaster shared between RecorderApp and WebHandler
_sse = SSEBroadcaster()


# ─────────────────────────────────────────
# Camera Preview Manager (Live Preview via Picamera2 Dual-Stream)
# ─────────────────────────────────────────
class CameraPreviewManager:
    """Manages web preview clients and frame extraction via Picamera2 dual-stream (lores)."""
    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self.latest_frame = None
        self.latest_frame_time = 0.0
        self.last_error = None
        self.last_touch = 0.0
        self.running = True
        self.thread = threading.Thread(target=self._worker, daemon=True, name="PreviewWorker")
        self.thread.start()

    def touch(self):
        """Called when a client requests a preview frame."""
        with self.lock:
            self.last_touch = time.time()
            self.condition.notify_all()

    def start(self):
        self.touch()

    def client_disconnect(self):
        pass

    def stop(self):
        with self.lock:
            self.last_touch = 0.0
            self.condition.notify_all()
        log("[PREVIEW] Camera preview stopped.")

    def close(self):
        with self.lock:
            self.running = False
            self.condition.notify_all()

    def _worker(self):
        log("[PREVIEW] Picamera2 dual-stream preview worker started (lores 640x360).")
        while self.running and self.app.running:
            with self.lock:
                # Sleep when no preview frame was requested in the last 4 seconds
                while self.running and self.app.running and (time.time() - self.last_touch) > 4.0:
                    self.condition.wait(timeout=1.0)
                if not self.running or not self.app.running:
                    break

            if self.app.picam2:
                try:
                    bio = BytesIO()
                    self.app.picam2.capture_file(bio, format="jpeg", name="lores")
                    frame = bio.getvalue()
                    with self.lock:
                        self.latest_frame = frame
                        self.latest_frame_time = time.time()
                        self.last_error = None
                        self.condition.notify_all()
                except Exception as e:
                    with self.lock:
                        self.latest_frame = None  # No fake/stale frames: clear immediately
                        self.last_error = str(e)
                        self.condition.notify_all()
                    time.sleep(0.1)
            else:
                with self.lock:
                    self.latest_frame = None
                    self.last_error = "Picamera2 not initialized (mock mode)"
                    self.condition.notify_all()
                time.sleep(0.5)

            time.sleep(0.05)  # ~20 FPS smooth preview
        log("[PREVIEW] Picamera2 dual-stream preview worker stopped.")


# ─────────────────────────────────────────
# Wi-Fi & Auto-Hotspot Failover Manager
# ─────────────────────────────────────────
class WifiFailoverManager:
    """
    Manages automatic Wi-Fi failover between Home Wi-Fi (TP-Link dev) and Hotspot (rasbicam).
    - Checks wlan0 state every 15s.
    - If Home Wi-Fi drops / out-of-range, automatically activates Hotspot (rasbicam, 192.168.2.1).
    - When in Hotspot mode, periodically scans for Home Wi-Fi. When detected, switches back to Home Wi-Fi.
    """
    def __init__(self, home_ssid=config.WIFI_HOME_SSID, hotspot_con=config.WIFI_HOTSPOT_SSID,
                 check_interval=config.WIFI_CHECK_INTERVAL_SEC):
        self.home_ssid = home_ssid
        self.hotspot_con = hotspot_con
        self.check_interval = check_interval
        self.running = True
        self.last_state = "unknown"
        self.thread = threading.Thread(target=self._worker, daemon=True, name="WifiFailoverWorker")

    def start(self):
        self.thread.start()

    def stop(self):
        self.running = False

    def get_status(self):
        active_con = self._get_active_connection("wlan0")
        ip = self._get_ip("wlan0")
        if active_con == self.hotspot_con:
            mode = "hotspot"
        elif active_con:
            mode = "wifi"
        else:
            mode = "disconnected"

        return {
            "mode": mode,
            "active_connection": active_con or "None",
            "ip": ip or "0.0.0.0",
            "home_ssid": self.home_ssid,
            "hotspot_ssid": self.hotspot_con,
            "hotspot_ip": config.WIFI_HOTSPOT_IP
        }

    def switch_to_home(self):
        """Force reconnect to Home Wi-Fi."""
        log(f"[WIFI] Manual trigger: Switching to Home Wi-Fi '{self.home_ssid}'...")
        try:
            res = subprocess.run(["nmcli", "con", "up", self.home_ssid], capture_output=True, text=True, timeout=15)
            log(f"[WIFI] nmcli home output: {res.stdout.strip() or res.stderr.strip()}")
            return res.returncode == 0
        except Exception as e:
            log(f"[WIFI ERROR] Switch to home failed: {e}")
            return False

    def switch_to_hotspot(self):
        """Force switch to Hotspot AP."""
        log(f"[WIFI] Manual trigger: Switching to Hotspot '{self.hotspot_con}' ({config.WIFI_HOTSPOT_IP})...")
        try:
            res = subprocess.run(["nmcli", "con", "up", self.hotspot_con], capture_output=True, text=True, timeout=15)
            log(f"[WIFI] nmcli hotspot output: {res.stdout.strip() or res.stderr.strip()}")
            return res.returncode == 0
        except Exception as e:
            log(f"[WIFI ERROR] Switch to hotspot failed: {e}")
            return False

    def _get_active_connection(self, iface="wlan0"):
        try:
            out = subprocess.check_output(["nmcli", "-t", "-f", "NAME,DEVICE", "con", "show", "--active"],
                                          stderr=subprocess.DEVNULL, text=True)
            for line in out.splitlines():
                parts = line.strip().split(":")
                if len(parts) >= 2 and parts[1] == iface:
                    return parts[0]
        except Exception:
            pass
        return None

    def _get_ip(self, iface="wlan0"):
        try:
            out = subprocess.check_output(["ip", "-4", "addr", "show", iface],
                                          stderr=subprocess.DEVNULL, text=True)
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("inet "):
                    return line.split()[1].split("/")[0]
        except Exception:
            pass
        return None

    def _scan_for_home_wifi(self):
        """Quick scan to see if Home Wi-Fi is visible."""
        try:
            out = subprocess.check_output(["nmcli", "-t", "-f", "SSID", "dev", "wifi", "list", "--rescan", "yes"],
                                          stderr=subprocess.DEVNULL, text=True, timeout=10)
            ssids = [s.strip() for s in out.splitlines() if s.strip()]
            return self.home_ssid in ssids
        except Exception:
            return False

    def _worker(self):
        log(f"[WIFI] Auto-Failover Watchdog started. Home='{self.home_ssid}', Hotspot='{self.hotspot_con}'.")
        time.sleep(10)  # Let boot network settle

        while self.running:
            try:
                active_con = self._get_active_connection("wlan0")

                if active_con == self.home_ssid:
                    if self.last_state != "home":
                        log(f"[WIFI] Connected to Home Wi-Fi '{self.home_ssid}' (IP: {self._get_ip('wlan0')}).")
                        self.last_state = "home"
                    time.sleep(15)

                elif active_con == self.hotspot_con:
                    if self.last_state != "hotspot":
                        log(f"[WIFI] Active in Hotspot Mode '{self.hotspot_con}' (IP: {self._get_ip('wlan0') or config.WIFI_HOTSPOT_IP}).")
                        self.last_state = "hotspot"

                    # Hotspot remains steady without background Wi-Fi channel scans.
                    # Switching back to Home Wi-Fi is triggered manually on-demand from the Web UI.
                    time.sleep(15)

                else:
                    # Neither connected to home nor hotspot
                    time.sleep(5)
                    if not self._get_active_connection("wlan0") and self.running:
                        log(f"[WIFI WARN] wlan0 disconnected. Activating fallback Hotspot '{self.hotspot_con}'...")
                        self.switch_to_hotspot()
                    time.sleep(6)

            except Exception as e:
                time.sleep(10)

        log("[WIFI] Auto-Failover Watchdog stopped.")


# ─────────────────────────────────────────
# RecorderApp
# ─────────────────────────────────────────
class RecorderApp:
    def __init__(self, mock_gpio=False):
        self.lock = threading.RLock()
        self.is_recording = False
        self.is_capturing = False
        self.current_video_file = None
        self.recording_start_time = None   # datetime when recording started
        self.running = True
        self.active_audio_device = config.AUDIO_DEVICE  # currently selected mic
        self.picam2 = None
        self.video_encoder = None
        self.video_output = None
        self.watchdog_thread = None
        self.preview_mgr = CameraPreviewManager(self)
        self.wifi_mgr = WifiFailoverManager()
        self.wifi_mgr.start()

        # ── Picamera2 Dual-Stream Camera ────────
        if PICAMERA2_AVAILABLE and not mock_gpio:
            try:
                self.picam2 = Picamera2()
                model_name = self.picam2.camera_properties.get("Model", "").lower()
                has_af = "AfMode" in self.picam2.camera_controls

                sensor_cfg = {}
                v3_size = getattr(config, 'CAMERA_V3_SENSOR_SIZE', (2304, 1296))
                if "imx708" in model_name:
                    log(f"[INIT] Camera Module 3 (IMX708 12MP) detected! Locking sensor to {v3_size} for Full FOV (uncropped).")
                    sensor_cfg = {"output_size": v3_size}
                else:
                    log(f"[INIT] Camera detected: {model_name or 'Default/OV5647'}")

                log(f"[INIT] Initializing Picamera2 dual-stream: main ({config.VIDEO_WIDTH}x{config.VIDEO_HEIGHT} @ {config.VIDEO_FPS}fps) + lores (640x360)...")
                frame_duration_us = int(1000000 / config.VIDEO_FPS)
                cam_cfg = self.picam2.create_video_configuration(
                    main={"size": (config.VIDEO_WIDTH, config.VIDEO_HEIGHT), "format": "YUV420"},
                    lores={"size": (640, 360), "format": "YUV420"},
                    controls={"FrameDurationLimits": (frame_duration_us, frame_duration_us)},
                    sensor=sensor_cfg if sensor_cfg else None
                )
                self.picam2.configure(cam_cfg)
                self.picam2.start()

                # Enable Continuous Autofocus if hardware supports it (e.g. Camera Module 3)
                if has_af:
                    try:
                        from libcamera import controls
                        self.picam2.set_controls({
                            "AfMode": controls.AfModeEnum.Continuous,
                            "AfRange": controls.AfRangeEnum.Normal,
                            "AfSpeed": controls.AfSpeedEnum.Normal
                        })
                        log("[INIT] Autofocus enabled: Continuous AF (CAF) active.")
                    except Exception as e:
                        log(f"[INIT WARN] Failed to set AF controls: {e}")
                else:
                    log("[INIT] Camera uses fixed-focus lens.")

                log("[INIT] Picamera2 dual-stream started and ready.")
            except Exception as e:
                log(f"[WARN] Failed to start Picamera2: {e}. Camera will run in mock mode.")
                self.picam2 = None
        else:
            log("[INIT] Running without hardware camera (mock mode).")

        # ── GPIO ────────────────────────────────
        if GPIO_AVAILABLE and not mock_gpio:
            try:
                log(f"[INIT] Configuring GPIO: BTN_PHOTO={config.PIN_BTN_PHOTO}, "
                    f"LED_PHOTO={config.PIN_LED_PHOTO}, BTN_RECORD={config.PIN_BTN_RECORD}, "
                    f"LED_RECORD={config.PIN_LED_RECORD}")
                self.led_photo  = LED(config.PIN_LED_PHOTO)
                self.led_record = LED(config.PIN_LED_RECORD)
                self.btn_photo  = Button(config.PIN_BTN_PHOTO, bounce_time=config.BUTTON_DEBOUNCE_SEC)
                self.btn_record = Button(config.PIN_BTN_RECORD, bounce_time=config.BUTTON_DEBOUNCE_SEC)
                self.btn_photo.when_pressed  = self.on_btn_photo_pressed
                self.btn_record.when_pressed = self.on_btn_record_pressed
                log("[INIT] GPIO initialized successfully.")
            except Exception as e:
                log(f"[WARN] Failed to init hardware GPIO: {e}. Falling back to Mock.")
                self._init_mock_gpio()
        else:
            self._init_mock_gpio()

        self.led_photo.off()
        self.led_record.off()

        # ── IPC socket ──────────────────────────
        self._init_ipc_socket()
        self._update_status()

    # ── GPIO helpers ────────────────────────────────────────────────────────

    def _init_mock_gpio(self):
        self.led_photo  = MockLED(config.PIN_LED_PHOTO)
        self.led_record = MockLED(config.PIN_LED_RECORD)
        self.btn_photo  = MockButton(config.PIN_BTN_PHOTO)
        self.btn_record = MockButton(config.PIN_BTN_RECORD)

    # ── IPC Unix socket (legacy trigger_sim.py) ─────────────────────────────

    def _init_ipc_socket(self):
        sock_path = config.IPC_SOCKET_PATH
        if os.path.exists(sock_path):
            try:
                os.unlink(sock_path)
            except OSError:
                pass

        self.server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_sock.bind(sock_path)
        self.server_sock.listen(5)
        os.chmod(sock_path, 0o777)

        self.ipc_thread = threading.Thread(target=self._ipc_listener, daemon=True)
        self.ipc_thread.start()
        log(f"[INIT] Control IPC socket active at {sock_path}")

    def _ipc_listener(self):
        while self.running:
            try:
                conn, _ = self.server_sock.accept()
                threading.Thread(target=self._handle_ipc_client, args=(conn,), daemon=True).start()
            except Exception:
                if not self.running:
                    break

    def _handle_ipc_client(self, conn):
        with conn:
            try:
                data = conn.recv(1024).decode('utf-8').strip()
                if not data:
                    return

                cmd = data.upper()
                log(f"[IPC] Received command: {cmd}")
                resp = {"status": "ok", "command": cmd}

                if cmd == "PHOTO":
                    success, msg = self.capture_photo()
                    resp.update({"success": success, "message": msg})
                elif cmd == "RECORD_START":
                    success, msg = self.start_recording()
                    resp.update({"success": success, "message": msg})
                elif cmd == "RECORD_STOP":
                    success, msg = self.stop_recording()
                    resp.update({"success": success, "message": msg})
                elif cmd == "RECORD_TOGGLE":
                    success, msg = self.toggle_recording()
                    resp.update({"success": success, "message": msg})
                elif cmd == "STATUS":
                    resp["data"] = self.get_status_data()
                elif cmd == "EXIT":
                    resp["message"] = "Daemon stopping"
                    conn.sendall(json.dumps(resp).encode('utf-8') + b"\n")
                    self.stop()
                    return
                else:
                    resp.update({"status": "error", "message": f"Unknown command: {cmd}"})

                conn.sendall(json.dumps(resp).encode('utf-8') + b"\n")
            except Exception as e:
                try:
                    conn.sendall(json.dumps({"status": "error", "message": str(e)}).encode('utf-8') + b"\n")
                except Exception:
                    pass

    # ── Status ──────────────────────────────────────────────────────────────

    def get_status_data(self):
        with self.lock:
            elapsed = 0
            if self.is_recording and self.recording_start_time:
                elapsed = int((datetime.now() - self.recording_start_time).total_seconds())

            # Disk usage
            try:
                st = os.statvfs(str(config.DCIM_DIR))
                free_bytes  = st.f_bavail * st.f_frsize
                total_bytes = st.f_blocks * st.f_frsize
                used_bytes  = total_bytes - free_bytes
            except Exception:
                free_bytes = total_bytes = used_bytes = 0

            # Camera info
            cam_model = "Unknown"
            has_af = False
            if self.picam2:
                try:
                    cam_model = self.picam2.camera_properties.get("Model", "Unknown")
                    has_af = "AfMode" in self.picam2.camera_controls
                except Exception:
                    pass
            elif not PICAMERA2_AVAILABLE:
                cam_model = "Mock (No hardware)"

            return {
                "is_recording":       self.is_recording,
                "is_capturing":       self.is_capturing,
                "current_video_file": str(self.current_video_file) if self.current_video_file else None,
                "recording_elapsed":  elapsed,
                "led_photo_lit":      bool(getattr(self.led_photo,  'is_lit', self.led_photo.value)),
                "led_record_lit":     bool(getattr(self.led_record, 'is_lit', self.led_record.value)),
                "free_bytes":         free_bytes,
                "total_bytes":        total_bytes,
                "used_bytes":         used_bytes,
                "active_audio_device": self.active_audio_device,
                "camera_model":       cam_model,
                "has_autofocus":      has_af,
                "wifi":               self.wifi_mgr.get_status() if hasattr(self, 'wifi_mgr') else {},
            }

    def _update_status(self):
        data = self.get_status_data()
        try:
            with open(config.STATUS_FILE_PATH, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log(f"[STATUS] Failed to write status file: {e}")
        # Push SSE update to all connected browsers
        _sse.broadcast("status", data)

    # ── Button handlers ─────────────────────────────────────────────────────

    def on_btn_photo_pressed(self):
        log("[EVENT] Button 1 (Photo) pressed!")
        threading.Thread(target=self.capture_photo, daemon=True).start()

    def on_btn_record_pressed(self):
        log("[EVENT] Button 2 (Record) pressed!")
        threading.Thread(target=self.toggle_recording, daemon=True).start()

    # ── Mic helper ──────────────────────────────────────────────────────────

    @staticmethod
    def list_audio_devices():
        """Return list of dicts: {name, device_string} from arecord -l."""
        devices = [
            {
                "name": "🔇 Video Only (No Audio / Mic Disabled)",
                "device_string": "none",
                "alsa_raw": "disabled",
            }
        ]
        try:
            out = subprocess.check_output(["arecord", "-l"], stderr=subprocess.DEVNULL, text=True)
            for line in out.splitlines():
                if line.startswith("card "):
                    # e.g. "card 1: Audio [AB13X USB Audio], device 0: USB Audio [USB Audio]"
                    parts = line.split(":")
                    if len(parts) >= 2:
                        card_part  = parts[0].strip()   # "card 1"
                        label_part = parts[1].strip()   # "Audio [AB13X USB Audio], device 0"
                        card_num   = card_part.split()[1]
                        card_id    = label_part.split("[")[0].strip() if "[" in label_part else label_part
                        card_desc  = label_part.split("[")[1].split("]")[0].strip() if "[" in label_part else card_id
                        devices.append({
                            "name":          f"Card {card_num}: {card_desc} ({card_id})",
                            "device_string": f"plughw:CARD={card_id},DEV=0",
                            "alsa_raw":      line.strip(),
                        })
        except Exception as e:
            log(f"[MIC] arecord -l failed: {e}")
        return devices

    # ── Photo capture ────────────────────────────────────────────────        

    def capture_photo(self):
        with self.lock:
            if self.is_capturing:
                msg = "Photo capture already in progress."
                log(f"[PHOTO] {msg}")
                return False, msg

            self.is_capturing = True
            try:
                self.led_photo.on()
            except Exception as e:
                log(f"[GPIO WARN] LED1 on: {e}")
            self._update_status()

        try:
            timestamp      = datetime.now().strftime("%Y%m%d_%H%M%S")
            photo_filename = f"IMG_{timestamp}.jpg"
            photo_path     = config.DCIM_DIR / photo_filename

            log(f"[PHOTO] Capturing {photo_filename}...")
            if self.picam2:
                # If currently recording, capture from lores to avoid conflict with main H.264 encoder.
                # If idle, capture full resolution from main stream.
                stream_name = "lores" if self.is_recording else "main"
                self.picam2.capture_file(str(photo_path), format="jpeg", name=stream_name, wait=5.0)
            else:
                time.sleep(0.3)
                with open(photo_path, "wb") as f:
                    f.write(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00\xff\xd9")

            if photo_path.exists() and photo_path.stat().st_size > 0:
                size_kb = photo_path.stat().st_size / 1024
                log(f"[PHOTO OK] Saved {photo_filename} ({size_kb:.1f} KB)")
                _sse.broadcast("photo_done", {"file": photo_filename, "size_kb": round(size_kb, 1)})
                return True, str(photo_path)
            else:
                log("[PHOTO ERROR] Output photo file not found or 0 bytes.")
                return False, "Output photo file empty or missing"

        except Exception as e:
            log(f"[PHOTO EXCEPTION] {e}")
            return False, str(e)
        finally:
            with self.lock:
                self.is_capturing = False
                try:
                    self.led_photo.off()
                except Exception as e:
                    log(f"[GPIO WARN] LED1 off: {e}")
                self._update_status()

    # ── Video recording ────────────────────────────────────────────────     

    def start_recording(self):
        with self.lock:
            if self.is_recording:
                return True, "Recording already active."

            timestamp      = datetime.now().strftime("%Y%m%d_%H%M%S")
            video_filename = f"VID_{timestamp}{config.CONTAINER_EXT}"
            video_path     = config.DCIM_DIR / video_filename

            log(f"[RECORD] Starting {video_filename} (1080p24 AAC MPEG-TS via Picamera2)...")
            try:
                if self.picam2:
                    self.video_encoder = H264Encoder(
                        bitrate=config.VIDEO_BITRATE,
                        framerate=config.VIDEO_FPS,
                        enable_sps_framerate=True,
                        iperiod=config.VIDEO_FPS
                    )
                    has_audio = bool(self.active_audio_device and self.active_audio_device != "none")
                    self.video_output  = AlsaFfmpegOutput(
                        str(video_path),
                        audio=has_audio,
                        audio_device=self.active_audio_device if has_audio else None,
                        audio_samplerate=config.AUDIO_SAMPLERATE,
                        audio_codec=config.AUDIO_CODEC,
                        audio_bitrate=config.AUDIO_BITRATE
                    )
                    # Start encoder on main stream WITHOUT stopping or restarting the camera
                    self.picam2.start_encoder(self.video_encoder, self.video_output, name="main")
                else:
                    log("[RECORD MOCK] Picamera2 not active, mock recording.")

                self.current_video_file   = video_path
                self.is_recording         = True
                self.recording_start_time = datetime.now()
                try:
                    self.led_record.on()
                except Exception as e:
                    log(f"[GPIO WARN] LED2 on: {e}")
                self._update_status()
                log(f"[RECORD OK] Recording active -> {video_filename}")

                # Launch Recording Watchdog to guard against Audio/Camera drops
                self.watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True, name="RecordWatchdog")
                self.watchdog_thread.start()

                return True, str(video_path)
            except Exception as e:
                log(f"[RECORD ERROR] Failed to start Picamera2 recording: {e}")
                try:
                    self.led_record.off()
                except Exception:
                    pass
                self.is_recording = False
                self.current_video_file = None
                self._update_status()
                return False, str(e)

    def _watchdog_loop(self):
        """Monitors active recording health every 1s for audio drops or camera stalls."""
        log("[WATCHDOG] Recording health monitor started.")
        while self.is_recording and self.running:
            time.sleep(1.0)
            if not self.is_recording or not self.running:
                break
            with self.lock:
                vo = self.video_output
                is_rec = self.is_recording

            if is_rec and vo:
                if not vo.is_healthy():
                    reason = getattr(vo, 'error_reason', None) or "Hardware stream dropped unexpectedly"
                    log(f"[WATCHDOG DETECTED DROP] {reason}")
                    self.emergency_stop_recording(reason)
                    break
        log("[WATCHDOG] Recording health monitor stopped.")

    def emergency_stop_recording(self, reason="Hardware drop"):
        """Safely finalizes recording if mic drops or camera stalls, preserving captured footage."""
        with self.lock:
            if not self.is_recording:
                return
            log(f"[EMERGENCY STOP] Auto-stopping recording: {reason}")
            saved_file = self.current_video_file

            try:
                if self.video_output:
                    try:
                        self.video_output.stop()
                    except Exception as e:
                        log(f"[EMERGENCY STOP WARN] video_output.stop: {e}")

                if self.picam2 and self.video_encoder:
                    enc = self.video_encoder
                    t = threading.Thread(target=lambda: self.picam2.stop_encoder(enc), daemon=True)
                    t.start()
                    t.join(timeout=2.0)
                    if t.is_alive():
                        log("[EMERGENCY STOP WARN] stop_encoder thread did not finish within 2.0s; proceeding.")
            except Exception as e:
                log(f"[EMERGENCY STOP WARN] stop_encoder: {e}")
            finally:
                self.video_encoder = None
                self.video_output  = None
                self.is_recording  = False
                self.current_video_file   = None
                self.recording_start_time = None
                try:
                    self.led_record.off()
                except Exception as e:
                    log(f"[GPIO WARN] LED2 off: {e}")
                self._update_status()

            fname = saved_file.name if saved_file else ""
            size_kb = 0
            if saved_file and saved_file.exists():
                size_kb = saved_file.stat().st_size / 1024
            log(f"[EMERGENCY STOP] Safe-stop completed. Footage up to drop saved in {fname} ({size_kb:.1f} KB)")
            _sse.broadcast("emergency_stop", {
                "reason": reason,
                "file": fname,
                "size_kb": round(size_kb, 1)
            })
            if "Audio" in reason or "Mic" in reason:
                threading.Thread(target=self._try_usb_recovery, daemon=True).start()

    def _try_usb_recovery(self):
        """Attempts background USB reset if USB audio device hung or dropped."""
        try:
            log("[AUDIO RECOVERY] Attempting background USB audio reset...")
            subprocess.run(["sudo", "usbreset", "0020:0b21"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4)
            log("[AUDIO RECOVERY] USB reset completed.")
        except Exception as e:
            log(f"[AUDIO RECOVERY] usbreset failed: {e}")

    def stop_recording(self):
        with self.lock:
            if not self.is_recording:
                return True, "No active recording."

            saved_file = self.current_video_file
            log(f"[RECORD] Stopping recording {saved_file.name if saved_file else ''}...")

            try:
                # 1. Stop video_output FIRST to kill FFmpeg and close pipes, avoiding encoder deadlock
                if self.video_output:
                    try:
                        self.video_output.stop()
                    except Exception as e:
                        log(f"[RECORD WARN] video_output.stop exception: {e}")

                # 2. Stop Picamera2 encoder in a thread with timeout so it cannot hang
                if self.picam2 and self.video_encoder:
                    enc = self.video_encoder
                    t = threading.Thread(target=lambda: self.picam2.stop_encoder(enc), daemon=True)
                    t.start()
                    t.join(timeout=2.0)
                    if t.is_alive():
                        log("[RECORD WARN] stop_encoder timed out after 2.0s; forcing continue.")
            except Exception as e:
                log(f"[RECORD WARN] stop_encoder exception: {e}")
            finally:
                # 3. Always reset recording state and guarantee LED2 is turned off
                self.video_encoder = None
                self.video_output  = None
                self.is_recording         = False
                self.current_video_file   = None
                self.recording_start_time = None
                try:
                    self.led_record.off()
                except Exception as e:
                    log(f"[GPIO WARN] LED2 off: {e}")
                self._update_status()

            # Wait up to 2.5 seconds for FFmpeg output to flush and finalize
            for _ in range(25):
                if saved_file and saved_file.exists() and saved_file.stat().st_size > 0:
                    break
                time.sleep(0.1)

            if saved_file and saved_file.exists() and saved_file.stat().st_size > 0:
                size_kb = saved_file.stat().st_size / 1024
                log(f"[RECORD OK] Saved {saved_file.name} ({size_kb:.1f} KB)")
                _sse.broadcast("record_done", {"file": saved_file.name, "size_kb": round(size_kb, 1)})
                return True, str(saved_file)
            else:
                log("[RECORD WARN] File missing or empty.")
                return False, "File empty or missing"

    def toggle_recording(self):
        with self.lock:
            rec = self.is_recording
        if rec:
            return self.stop_recording()
        else:
            return self.start_recording()

    # ── Footage listing ─────────────────────────────────────────────────────

    def list_footage(self):
        files = []
        try:
            items = [p for p in config.DCIM_DIR.iterdir() if p.is_file() and p.suffix.lower() in ('.ts', '.mp4', '.jpg', '.jpeg')]
            # Strictly sort by modification time descending (latest first)
            items.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for p in items:
                st = p.stat()
                files.append({
                    "name":       p.name,
                    "size_bytes": st.st_size,
                    "modified":   datetime.fromtimestamp(st.st_mtime).isoformat(),
                    "type":       "video" if p.suffix.lower() in ('.ts', '.mp4') else "photo",
                })
        except Exception as e:
            log(f"[FOOTAGE] List error: {e}")
        return files

    # ── Shutdown ─────────────────────────────────────────────────────────────

    def stop(self):
        log("[SHUTDOWN] Stopping recorder daemon...")
        self.running = False
        if hasattr(self, 'wifi_mgr'):
            self.wifi_mgr.stop()
        if hasattr(self, 'preview_mgr'):
            self.preview_mgr.close()
        if self.is_recording:
            self.stop_recording()
        if self.picam2:
            try:
                self.picam2.stop()
                self.picam2.close()
            except Exception:
                pass

        try:
            self.led_photo.off()
            self.led_record.off()
            self.btn_photo.close()
            self.btn_record.close()
            self.led_photo.close()
            self.led_record.close()
        except Exception:
            pass

        try:
            self.server_sock.close()
            if os.path.exists(config.IPC_SOCKET_PATH):
                os.unlink(config.IPC_SOCKET_PATH)
        except Exception:
            pass

        if os.path.exists(PID_FILE):
            try:
                os.unlink(PID_FILE)
            except OSError:
                pass

        self._update_status()
        log("[SHUTDOWN] Cleanup complete.")


# ─────────────────────────────────────────
# HTTP Web UI (built-in, no dependencies)
# ─────────────────────────────────────────

# Inline HTML/CSS/JS dashboard — served from memory
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RasbiiRecord — Camera Dashboard</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" media="print" onload="this.media='all'">
<style>
  :root {
    --bg:       #0a0d14;
    --surface:  #111827;
    --surface2: #1a2035;
    --border:   #1f2d45;
    --accent:   #3b82f6;
    --accent2:  #60a5fa;
    --red:      #ef4444;
    --green:    #22c55e;
    --yellow:   #f59e0b;
    --text:     #e2e8f0;
    --muted:    #64748b;
    --radius:   14px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, Cantarell, "Helvetica Neue", sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    padding: 24px 16px 48px;
  }

  /* ── Header ── */
  header {
    display: flex; align-items: center; gap: 14px;
    margin-bottom: 28px;
  }
  .logo-icon {
    width: 44px; height: 44px; border-radius: 12px;
    background: linear-gradient(135deg, var(--accent), #7c3aed);
    display: flex; align-items: center; justify-content: center;
    font-size: 22px; flex-shrink: 0;
    box-shadow: 0 0 24px rgba(59,130,246,.35);
  }
  header h1 { font-size: 1.4rem; font-weight: 700; letter-spacing: -.5px; }
  header span { font-size: .8rem; color: var(--muted); margin-left: 2px; }
  .conn-dot {
    margin-left: auto; width: 9px; height: 9px;
    border-radius: 50%; background: var(--muted);
    transition: background .4s;
  }
  .conn-dot.live { background: var(--green); box-shadow: 0 0 8px var(--green); }

  /* ── Grid ── */
  .grid {
    display: grid;
    grid-template-columns: 1fr;
    gap: 16px;
    max-width: 720px;
    margin: 0 auto;
  }

  /* ── Card ── */
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px 22px;
  }
  .card-title {
    font-size: .7rem; font-weight: 600; letter-spacing: 1.2px;
    text-transform: uppercase; color: var(--muted);
    margin-bottom: 16px;
  }

  /* ── Status pill ── */
  .status-pill {
    display: inline-flex; align-items: center; gap: 7px;
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: 100px; padding: 5px 14px; font-size: .8rem;
    font-weight: 500;
  }
  .pill-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--muted);
  }
  .pill-dot.rec { background: var(--red); animation: pulse 1s infinite; }
  .pill-dot.ok  { background: var(--green); }

  @keyframes pulse {
    0%,100% { opacity: 1; }
    50%      { opacity: .3; }
  }

  /* ── Timer ── */
  .timer {
    font-size: 2.6rem; font-weight: 700; letter-spacing: -1px;
    font-variant-numeric: tabular-nums;
    margin: 10px 0 4px;
    background: linear-gradient(90deg, var(--accent2), #a78bfa);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }
  .timer.inactive { -webkit-text-fill-color: var(--muted); background: none; }

  /* ── Buttons ── */
  .btn {
    display: inline-flex; align-items: center; justify-content: center; gap: 8px;
    border: none; border-radius: 10px;
    font-family: inherit; font-size: .9rem; font-weight: 600;
    cursor: pointer; padding: 12px 24px;
    transition: all .18s; user-select: none;
  }
  .btn:active { transform: scale(.96); }
  .btn-primary {
    background: var(--accent);
    color: #fff;
    box-shadow: 0 4px 20px rgba(59,130,246,.3);
  }
  .btn-primary:hover { background: var(--accent2); }
  .btn-danger {
    background: var(--red);
    color: #fff;
    box-shadow: 0 4px 20px rgba(239,68,68,.3);
    animation: pulse-btn 1.2s infinite;
  }
  @keyframes pulse-btn {
    0%,100% { box-shadow: 0 4px 20px rgba(239,68,68,.3); }
    50%      { box-shadow: 0 4px 28px rgba(239,68,68,.6); }
  }
  .btn-secondary {
    background: var(--surface2); color: var(--text);
    border: 1px solid var(--border);
  }
  .btn-secondary:hover { border-color: var(--accent); color: var(--accent2); }
  .btn-icon {
    background: none; border: none; cursor: pointer;
    color: var(--muted); padding: 6px; border-radius: 6px;
    font-size: 1.1rem; transition: color .15s;
  }
  .btn-icon:hover { color: var(--red); }
  .btn-row { display: flex; gap: 10px; flex-wrap: wrap; }

  /* ── Storage bar ── */
  .storage-bar {
    height: 8px; background: var(--surface2);
    border-radius: 100px; overflow: hidden; margin: 10px 0 6px;
  }
  .storage-fill {
    height: 100%; border-radius: 100px;
    background: linear-gradient(90deg, var(--accent), #7c3aed);
    transition: width .6s ease;
  }
  .storage-fill.warn  { background: linear-gradient(90deg, var(--yellow), var(--red)); }
  .storage-labels {
    display: flex; justify-content: space-between;
    font-size: .73rem; color: var(--muted);
  }

  /* ── Footage list ── */
  .footage-list {
    display: flex; flex-direction: column; gap: 8px;
    max-height: 340px; overflow-y: auto;
  }
  .footage-list::-webkit-scrollbar { width: 4px; }
  .footage-list::-webkit-scrollbar-track { background: transparent; }
  .footage-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }

  .footage-item {
    display: flex; align-items: center; gap: 10px;
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: 9px; padding: 10px 12px;
  }
  .footage-icon { font-size: 1.2rem; flex-shrink: 0; }
  .footage-name {
    flex: 1; font-size: .8rem; font-weight: 500;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .footage-meta { font-size: .7rem; color: var(--muted); }
  .footage-actions { display: flex; gap: 4px; flex-shrink: 0; }

  /* ── Mic select ── */
  select {
    width: 100%; background: var(--surface2); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 12px; font-family: inherit; font-size: .85rem;
    appearance: none; cursor: pointer; outline: none;
    margin-bottom: 10px;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' fill='%2364748b' viewBox='0 0 24 24'%3E%3Cpath d='M7 10l5 5 5-5z'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: right 10px center;
    background-size: 20px;
  }
  select:focus { border-color: var(--accent); }

  /* ── Preview ── */
  #preview-wrapper {
    background: #070b14; border: 1px solid var(--border);
    border-radius: 12px; overflow: hidden;
    display: none; margin-bottom: 12px;
    position: relative; aspect-ratio: 16 / 9; min-height: 180px;
    align-items: center; justify-content: center;
  }
  #preview-wrapper.visible { display: flex; }
  #preview-img { width: 100%; height: 100%; display: block; object-fit: cover; }
  .preview-loading {
    position: absolute; color: var(--muted); font-size: .82rem;
    display: flex; align-items: center; gap: 8px; z-index: 1;
    pointer-events: none;
  }

  /* ── Toast ── */
  #toast-container {
    position: fixed; bottom: 24px; right: 20px;
    display: flex; flex-direction: column-reverse; gap: 8px; z-index: 9999;
  }
  .toast {
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: 10px; padding: 10px 16px;
    font-size: .82rem; font-weight: 500;
    box-shadow: 0 8px 30px rgba(0,0,0,.4);
    animation: slideIn .25s ease;
  }
  .toast.success { border-left: 3px solid var(--green); }
  .toast.error   { border-left: 3px solid var(--red); }
  .toast.info    { border-left: 3px solid var(--accent); }
  @keyframes slideIn {
    from { transform: translateX(40px); opacity: 0; }
    to   { transform: translateX(0);    opacity: 1; }
  }
  .empty-state {
    text-align: center; padding: 28px; color: var(--muted);
    font-size: .83rem;
  }
  .badge {
    font-size: .67rem; font-weight: 600; padding: 2px 7px;
    border-radius: 5px; background: var(--surface2);
    border: 1px solid var(--border); color: var(--muted);
    text-transform: uppercase; letter-spacing: .5px;
  }
  .badge.vid { border-color: #7c3aed44; color: #a78bfa; }
  .badge.img { border-color: var(--accent)44; color: var(--accent2); }
</style>
</head>
<body>

<header>
  <div class="logo-icon">🎥</div>
  <div style="flex:1;">
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
      <h1>RasbiiRecord <span>Pi Zero 2 W</span></h1>
      <span id="cam-badge" class="badge vid" style="display:none;font-size:0.7rem;">📷 Camera</span>
    </div>
    <div id="wifi-badge" style="display:flex;align-items:center;gap:8px;font-size:0.75rem;margin-top:4px;color:var(--muted);flex-wrap:wrap;">
      <span id="wifi-icon">📶</span> <span id="wifi-text">Checking Wi-Fi…</span>
      <button id="btn-wifi-switch" onclick="switchWifiHome()" style="font-size:0.68rem;font-weight:600;padding:2px 10px;border-radius:6px;background:var(--surface2);color:var(--muted);border:1px solid var(--border);cursor:pointer;opacity:0.5;" disabled title="Only available when in Hotspot mode">📶 Switch to Wi-Fi</button>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:10px;">
    <button id="btn-open-logs" class="btn btn-secondary" onclick="openLogsModal()" style="font-size:0.73rem;padding:6px 12px;border-radius:8px;display:flex;align-items:center;gap:6px;">📄 Logs</button>
    <div class="conn-dot" id="conn-dot" title="SSE connection"></div>
  </div>
</header>

<div class="grid">

  <!-- ── Record Control ── -->
  <div class="card">
    <div class="card-title">Recording Control</div>
    <div id="status-pill" class="status-pill">
      <span class="pill-dot" id="rec-dot"></span>
      <span id="status-text">Idle</span>
    </div>
    <div class="timer inactive" id="timer">00:00:00</div>

    <div class="btn-row" style="margin-top:14px;">
      <button class="btn btn-primary" id="btn-record" onclick="toggleRecord()">
        ▶ Start Recording
      </button>
      <button class="btn btn-secondary" id="btn-photo" onclick="capturePhoto()">
        📷 Capture Photo
      </button>
    </div>
  </div>

  <!-- ── Camera Preview ── -->
  <div class="card">
    <div class="card-title">Camera Preview</div>
    <div id="preview-wrapper">
      <div class="preview-loading" id="preview-loading">🔄 Connecting camera feed…</div>
      <img id="preview-img" src="" alt="Camera preview">
    </div>
    <div class="btn-row">
      <button class="btn btn-secondary" id="btn-preview" onclick="togglePreview()">
        👁 Enable Preview
      </button>
      <span style="font-size:.73rem;color:var(--muted);align-self:center;">
        Dual-stream active (live during recording)
      </span>
    </div>
  </div>

  <!-- ── Storage ── -->
  <div class="card">
    <div class="card-title">Storage</div>
    <div class="storage-bar">
      <div class="storage-fill" id="storage-fill" style="width:0%"></div>
    </div>
    <div class="storage-labels">
      <span id="storage-used">— used</span>
      <span id="storage-free">— free</span>
    </div>
  </div>

  <!-- ── Footage ── -->
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:8px;">
      <div class="card-title" style="margin-bottom:0;">Footage</div>
      <div style="display:flex;gap:8px;">
        <button class="btn btn-secondary" style="font-size:.78rem;padding:6px 12px;" onclick="loadFootage()">⟳ Refresh</button>
        <button id="btn-delete-all" class="btn" style="font-size:.78rem;padding:6px 12px;background:#ef4444;color:#fff;border-radius:8px;" onclick="deleteAllFootage()">🗑 Delete All</button>
      </div>
    </div>
    <div class="footage-list" id="footage-list">
      <div class="empty-state">Loading footage…</div>
    </div>
  </div>

  <!-- ── Audio / Mic Selection ── -->
  <div class="card">
    <div class="card-title">Microphone Selection</div>
    <select id="mic-select">
      <option value="">Loading audio devices…</option>
    </select>
    <div class="btn-row">
      <button class="btn btn-secondary" onclick="applyMic()" id="btn-apply-mic">
        🎙 Apply Mic
      </button>
      <span id="active-mic-label" style="font-size:.73rem;color:var(--muted);align-self:center;"></span>
    </div>
  </div>

</div><!-- /grid -->

<!-- ── Log Viewer Modal ── -->
<div id="logs-modal" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.78);z-index:10000;align-items:center;justify-content:center;padding:16px;">
  <div style="background:var(--surface);border:1px solid var(--border);border-radius:14px;width:100%;max-width:860px;height:84vh;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 12px 40px rgba(0,0,0,.7);">
    <div style="padding:14px 18px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;background:var(--surface2);">
      <div style="display:flex;align-items:center;gap:10px;">
        <span style="font-size:1.15rem;">📄</span>
        <strong style="font-size:.92rem;">System Logs (Live Stream)</strong>
        <span id="log-count" class="badge" style="background:var(--surface);">0 lines</span>
      </div>
      <div style="display:flex;align-items:center;gap:8px;">
        <button class="btn btn-secondary" style="font-size:.75rem;padding:5px 12px;" onclick="loadLogs()">⟳ Refresh</button>
        <button class="btn-icon" onclick="closeLogsModal()" style="font-size:1.2rem;line-height:1;margin-left:4px;">✕</button>
      </div>
    </div>
    <div id="log-content" style="flex:1;background:#060910;color:#94a3b8;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.75rem;line-height:1.5;padding:14px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;">Loading logs…</div>
  </div>
</div>

<div id="toast-container"></div>

<script>
// ── Utilities ──────────────────────────────────────────────────
function fmt(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes/1024).toFixed(1) + ' KB';
  if (bytes < 1073741824) return (bytes/1048576).toFixed(1) + ' MB';
  return (bytes/1073741824).toFixed(2) + ' GB';
}
function fmtTime(sec) {
  const h = Math.floor(sec/3600);
  const m = Math.floor((sec%3600)/60);
  const s = sec%60;
  return [h,m,s].map(v=>String(v).padStart(2,'0')).join(':');
}
function toast(msg, type='info') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

// ── State ──────────────────────────────────────────────────────
let _elapsed = 0;
let _timerInterval = null;
let _isRecording = false;
let _previewOn = false;

function startLocalTimer(elapsed) {
  _elapsed = elapsed;
  clearInterval(_timerInterval);
  _timerInterval = setInterval(() => {
    _elapsed++;
    document.getElementById('timer').textContent = fmtTime(_elapsed);
  }, 1000);
}
function stopLocalTimer() {
  clearInterval(_timerInterval);
  _timerInterval = null;
}

// ── Apply status from server ────────────────────────────────────
function applyStatus(st) {
  _isRecording = st.is_recording;
  const dot    = document.getElementById('rec-dot');
  const txt    = document.getElementById('status-text');
  const timer  = document.getElementById('timer');
  const btnRec = document.getElementById('btn-record');
  const btnPhoto = document.getElementById('btn-photo');

  if (st.is_recording) {
    dot.className  = 'pill-dot rec';
    txt.textContent= 'Recording';
    timer.className= 'timer';
    btnRec.className = 'btn btn-danger';
    btnRec.textContent = '⏹ Stop Recording';
    startLocalTimer(st.recording_elapsed || 0);
    btnPhoto.disabled = !!st.is_capturing;
  } else {
    dot.className  = 'pill-dot' + (st.is_capturing ? ' ok' : '');
    txt.textContent= st.is_capturing ? 'Capturing Photo…' : 'Idle';
    timer.className= 'timer inactive';
    timer.textContent = '00:00:00';
    btnRec.className = 'btn btn-primary';
    btnRec.textContent = '▶ Start Recording';
    stopLocalTimer();
    btnPhoto.disabled = !!st.is_capturing;
  }

  // Storage
  if (st.total_bytes > 0) {
    const pct = (st.used_bytes / st.total_bytes * 100).toFixed(1);
    const fill = document.getElementById('storage-fill');
    fill.style.width = pct + '%';
    fill.className = 'storage-fill' + (pct > 80 ? ' warn' : '');
    document.getElementById('storage-used').textContent = fmt(st.used_bytes) + ' used (' + pct + '%)';
    document.getElementById('storage-free').textContent = fmt(st.free_bytes) + ' free';
  }

  // Active mic
  if (st.active_audio_device) {
    document.getElementById('active-mic-label').textContent = '✓ ' + st.active_audio_device;
  }

  // Camera badge
  const camBadge = document.getElementById('cam-badge');
  if (camBadge && st.camera_model) {
    camBadge.style.display = 'inline-block';
    camBadge.textContent = '📷 ' + st.camera_model + (st.has_autofocus ? ' (AF)' : '');
  }

  // Wi-Fi / Hotspot status & Switch Button binding
  const switchBtn = document.getElementById('btn-wifi-switch');
  if (st.wifi) {
    const icon = document.getElementById('wifi-icon');
    const txt  = document.getElementById('wifi-text');
    if (icon && txt) {
      if (st.wifi.mode === 'hotspot') {
        icon.textContent = '🔥';
        txt.textContent = 'Hotspot: ' + (st.wifi.active_connection || 'rasbicam') + ' (' + (st.wifi.ip || '192.168.2.1') + ')';
        txt.style.color = 'var(--yellow)';
        if (switchBtn) {
          switchBtn.disabled = false;
          switchBtn.style.opacity = '1';
          switchBtn.style.background = 'var(--accent)';
          switchBtn.style.color = '#fff';
          switchBtn.style.borderColor = 'var(--accent)';
          switchBtn.textContent = '📶 Switch to Wi-Fi';
          switchBtn.title = 'Click to disconnect Hotspot and reconnect to Home Wi-Fi';
        }
      } else if (st.wifi.mode === 'wifi') {
        icon.textContent = '📶';
        txt.textContent = (st.wifi.active_connection || 'TP-Link dev') + ' (' + (st.wifi.ip || '') + ')';
        txt.style.color = 'var(--green)';
        if (switchBtn) {
          switchBtn.disabled = true;
          switchBtn.style.opacity = '0.4';
          switchBtn.style.background = 'var(--surface2)';
          switchBtn.style.color = 'var(--muted)';
          switchBtn.style.borderColor = 'var(--border)';
          switchBtn.textContent = '✓ On Home Wi-Fi';
          switchBtn.title = 'Currently connected to Home Wi-Fi';
        }
      } else {
        icon.textContent = '⚠️';
        txt.textContent = 'Wi-Fi Disconnected';
        txt.style.color = 'var(--red)';
        if (switchBtn) {
          switchBtn.disabled = true;
          switchBtn.style.opacity = '0.4';
          switchBtn.textContent = '⚠️ No Wi-Fi';
        }
      }
    }
  }
}

// ── SSE ────────────────────────────────────────────────────────
let _es = null;
function connectSSE() {
  if (_es) {
    try { _es.close(); } catch(_) {}
    _es = null;
  }
  const es = new EventSource('/events');
  _es = es;
  const dot = document.getElementById('conn-dot');

  es.addEventListener('status', e => {
    try { applyStatus(JSON.parse(e.data)); } catch(_) {}
  });
  es.addEventListener('photo_done', e => {
    const d = JSON.parse(e.data);
    toast(`📷 Photo saved: ${d.file} (${d.size_kb} KB)`, 'success');
    loadFootage();
  });
  es.addEventListener('record_done', e => {
    const d = JSON.parse(e.data);
    toast(`🎬 Video saved: ${d.file} (${d.size_kb} KB)`, 'success');
    loadFootage();
  });
  es.addEventListener('emergency_stop', e => {
    const d = JSON.parse(e.data);
    toast(`⚠️ Recording Auto-Stopped: ${d.reason}`, 'error');
    loadFootage();
  });
  es.onopen  = () => { dot.className = 'conn-dot live'; };
  es.onerror = () => {
    dot.className = 'conn-dot';
    // Native EventSource auto-reconnects cleanly. Do NOT call connectSSE() in loop!
  };
}

window.addEventListener('beforeunload', () => {
  if (_es) {
    try { _es.close(); } catch(_) {}
  }
  if (_previewActive) {
    stopPreview();
  }
});

// ── API calls ─────────────────────────────────────────────────
async function api(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  return r.json();
}

async function toggleRecord() {
  const btn = document.getElementById('btn-record');
  btn.disabled = true;
  try {
    const d = await api('POST', '/api/record');
    if (!d.success) toast('Error: ' + d.message, 'error');
  } catch(e) { toast('Network error', 'error'); }
  btn.disabled = false;
}

async function capturePhoto() {
  const btn = document.getElementById('btn-photo');
  btn.disabled = true;
  btn.textContent = '📷 Capturing…';
  try {
    const d = await api('POST', '/api/photo');
    if (d.success) toast('Photo captured!', 'success');
    else toast('Photo error: ' + d.message, 'error');
  } catch(e) { toast('Network error', 'error'); }
  btn.textContent = '📷 Capture Photo';
  btn.disabled = false;
}

// ── Footage ───────────────────────────────────────────────────
async function loadFootage() {
  const list = document.getElementById('footage-list');
  try {
    const d = await api('GET', '/api/footage');
    if (!d.files || d.files.length === 0) {
      list.innerHTML = '<div class="empty-state">No footage yet.</div>';
      return;
    }
    // Always sort by date modified descending (newest first)
    d.files.sort((a, b) => new Date(b.modified) - new Date(a.modified));
    list.innerHTML = d.files.map(f => {
      const icon  = f.type === 'video' ? '🎬' : '🖼️';
      const badge = f.type === 'video' ? '<span class="badge vid">TS</span>' : '<span class="badge img">JPG</span>';
      return `<div class="footage-item">
        <span class="footage-icon">${icon}</span>
        <div style="flex:1;min-width:0;">
          <div class="footage-name">${f.name} ${badge}</div>
          <div class="footage-meta">${fmt(f.size_bytes)} · ${f.modified.replace('T',' ').slice(0,19)}</div>
        </div>
        <div class="footage-actions">
          <a href="/api/footage/${encodeURIComponent(f.name)}" download="${f.name}">
            <button class="btn-icon" title="Download">⬇</button>
          </a>
          <button class="btn-icon" title="Delete immediately" onclick="deleteFile('${f.name}', this)">🗑</button>
        </div>
      </div>`;
    }).join('');
  } catch(e) { list.innerHTML = '<div class="empty-state">Failed to load footage.</div>'; }
}

async function deleteFile(name, btn) {
  // Direct deletion without confirmation prompt
  if (btn) btn.disabled = true;
  try {
    const d = await api('DELETE', '/api/footage/' + encodeURIComponent(name));
    if (d.success) {
      toast(`Deleted ${name}`, 'success');
      loadFootage();
    } else {
      toast('Delete failed: ' + d.message, 'error');
      if (btn) btn.disabled = false;
    }
  } catch(e) {
    toast('Delete error: ' + e, 'error');
    if (btn) btn.disabled = false;
  }
}

async function deleteAllFootage() {
  const btn = document.getElementById('btn-delete-all');
  if (!confirm('Are you SURE you want to delete ALL recorded videos and photos? This action cannot be undone!')) {
    return;
  }
  if (btn) btn.disabled = true;
  try {
    const d = await api('DELETE', '/api/footage/all');
    if (d.success) {
      toast(`All footage deleted (${d.deleted_count || 0} files removed)`, 'success');
      loadFootage();
    } else {
      toast('Delete all failed: ' + (d.message || d.error), 'error');
    }
  } catch(e) {
    toast('Network error during delete all: ' + e, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ── Mic ───────────────────────────────────────────────────────
async function loadMics() {
  const sel = document.getElementById('mic-select');
  try {
    const d = await api('GET', '/api/mics');
    sel.innerHTML = d.devices.map(m =>
      `<option value="${m.device_string}">${m.name}</option>`
    ).join('');
    if (d.active) {
      const opt = [...sel.options].find(o => o.value === d.active);
      if (opt) opt.selected = true;
    }
    if (d.devices.length === 0)
      sel.innerHTML = '<option value="">No audio devices found</option>';
  } catch(e) {
    sel.innerHTML = '<option value="">Failed to load devices</option>';
  }
}

async function applyMic() {
  const sel = document.getElementById('mic-select');
  const dev = sel.value;
  if (!dev) { toast('Select a device first', 'error'); return; }
  const d = await api('POST', '/api/mic', { device: dev });
  if (d.success) toast(`Mic set to: ${dev}`, 'success');
  else toast('Error: ' + d.message, 'error');
}

async function switchWifiHome() {
  if (!confirm('Switch from Hotspot back to Home Wi-Fi ("TP-Link dev")? Your current device will be disconnected from the Hotspot.')) return;
  toast('Switching to Home Wi-Fi... Please reconnect your phone/PC to your home router.', 'info');
  try {
    const res = await api('POST', '/api/wifi/switch', { target: 'home' });
    if (res.success) toast(res.message, 'success');
  } catch(e) {
    toast('Network switch triggered. Please switch to home Wi-Fi.', 'info');
  }
}

// ── System Logs Modal ──────────────────────────────────────────
function openLogsModal() {
  const m = document.getElementById('logs-modal');
  if (m) {
    m.style.display = 'flex';
    loadLogs();
  }
}

function closeLogsModal() {
  const m = document.getElementById('logs-modal');
  if (m) m.style.display = 'none';
}

async function loadLogs() {
  const el = document.getElementById('log-content');
  const cnt = document.getElementById('log-count');
  if (!el) return;
  el.textContent = 'Loading latest logs…';
  try {
    const res = await api('GET', '/api/logs');
    if (res && res.logs) {
      el.textContent = res.logs.join('\n');
      if (cnt) cnt.textContent = (res.count || res.logs.length) + ' lines';
      el.scrollTop = el.scrollHeight;
    } else {
      el.textContent = 'No logs available.';
    }
  } catch(e) {
    el.textContent = 'Failed to load logs: ' + e;
  }
}

// ── Preview (Ultra-fast blob stream, rock-solid across all browsers) ──
let _previewActive = false;
let _previewController = null;

function togglePreview() {
  if (_previewActive) stopPreview();
  else startPreview();
}

async function startPreview() {
  if (_previewActive) return;
  _previewActive = true;

  const wrapper = document.getElementById('preview-wrapper');
  const img     = document.getElementById('preview-img');
  const btn     = document.getElementById('btn-preview');
  const loading = document.getElementById('preview-loading');

  if (loading) loading.style.display = 'flex';
  wrapper.classList.add('visible');
  btn.textContent = '🚫 Disable Preview';

  let currentBlobUrl = null;

  while (_previewActive) {
    try {
      _previewController = new AbortController();
      const res = await fetch('/preview/frame?t=' + Date.now(), {
        signal: _previewController.signal,
        cache: 'no-store'
      });
      if (res.ok && _previewActive) {
        const blob = await res.blob();
        if (!_previewActive) break;
        const newUrl = URL.createObjectURL(blob);
        img.src = newUrl;
        img.style.display = 'block';
        if (currentBlobUrl) {
          URL.revokeObjectURL(currentBlobUrl);
        }
        currentBlobUrl = newUrl;
        if (loading && loading.style.display !== 'none') {
          loading.style.display = 'none';
        }
      } else if (!res.ok) {
        let errText = 'Frame error (' + res.status + ')';
        try {
          const d = await res.json();
          if (d.details || d.error) errText = d.details || d.error;
        } catch(_) {}
        // Clear stale image so fake/frozen frame is NOT shown!
        if (currentBlobUrl) {
          URL.revokeObjectURL(currentBlobUrl);
          currentBlobUrl = null;
        }
        img.removeAttribute('src');
        img.style.display = 'none';
        if (loading) {
          loading.style.display = 'flex';
          loading.textContent = '⚠️ ' + errText;
        }
      }
    } catch(e) {
      if (e.name === 'AbortError') break;
      if (currentBlobUrl) {
        URL.revokeObjectURL(currentBlobUrl);
        currentBlobUrl = null;
      }
      img.removeAttribute('src');
      img.style.display = 'none';
      if (loading) {
        loading.style.display = 'flex';
        loading.textContent = '⚠️ ' + (e.message || e);
      }
    }
    // ~16 FPS smooth refresh
    await new Promise(r => setTimeout(r, 60));
  }

  if (currentBlobUrl) {
    URL.revokeObjectURL(currentBlobUrl);
  }
}

function stopPreview() {
  _previewActive = false;
  if (_previewController) {
    try { _previewController.abort(); } catch(_) {}
    _previewController = null;
  }
  const wrapper = document.getElementById('preview-wrapper');
  const img     = document.getElementById('preview-img');
  const btn     = document.getElementById('btn-preview');
  const loading = document.getElementById('preview-loading');
  img.src = '';
  wrapper.classList.remove('visible');
  btn.textContent = '👁 Enable Preview';
  if (loading) {
    loading.textContent = '🔄 Connecting camera feed…';
    loading.style.display = 'none';
  }
  fetch('/api/preview/stop', { method: 'POST' }).catch(()=>{});
}

// ── Boot ──────────────────────────────────────────────────────
connectSSE();
loadFootage();
loadMics();

// Initial status fetch
fetch('/api/status').then(r=>r.json()).then(applyStatus).catch(()=>{});
</script>
</body>
</html>
"""


class RecorderHTTPHandler(BaseHTTPRequestHandler):
    """HTTP request handler — references global `_app` RecorderApp instance."""

    app: 'RecorderApp' = None   # set at startup

    def log_message(self, fmt, *args):
        # Suppress default HTTP logging (noisy); use our own log only for errors
        pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path.rstrip('/')

        if path == '' or path == '/':
            self._serve_html(DASHBOARD_HTML)
        elif path == '/api/status':
            self._json(200, self.app.get_status_data())
        elif path == '/api/footage':
            self._json(200, {"files": self.app.list_footage()})
        elif path.startswith('/api/footage/'):
            name = urllib.parse.unquote(path[len('/api/footage/'):])
            self._serve_file(name)
        elif path == '/api/mics':
            devices = self.app.list_audio_devices()
            self._json(200, {"devices": devices, "active": self.app.active_audio_device})
        elif path == '/api/wifi/status':
            self._json(200, self.app.wifi_mgr.get_status() if hasattr(self.app, 'wifi_mgr') else {})
        elif path == '/api/logs':
            self._json(200, {"logs": list(_log_buffer), "count": len(_log_buffer)})
        elif path == '/log.txt' or path == '/api/logs/dump':
            self._serve_log_file()
        elif path == '/events':
            self._sse_stream()
        elif path == '/preview':
            self._mjpeg_stream()
        elif path == '/preview/frame':
            self._single_frame()
        else:
            self._json(404, {"error": "Not found"})

    def _serve_log_file(self):
        """Directly stream continuous log.txt from disk."""
        log_file = getattr(config, 'LOG_FILE_PATH', config.DCIM_DIR / "log.txt")
        if not log_file.exists():
            data = ("\n".join(_log_buffer) + "\n").encode('utf-8')
        else:
            with _log_file_lock:
                try:
                    with open(log_file, 'rb') as f:
                        data = f.read()
                except Exception:
                    data = ("\n".join(_log_buffer) + "\n").encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(data)

    def _mjpeg_stream(self):
        """Stream continuous MJPEG frames for live camera preview via Picamera2 dual-stream."""
        self.app.preview_mgr.start()

        self.send_response(200)
        self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.send_header('Connection', 'close')
        self.end_headers()

        mgr = self.app.preview_mgr
        try:
            last_frame = None
            start_wait = time.time()
            while self.app.running and not mgr.latest_frame and (time.time() - start_wait) < 4.0:
                with mgr.lock:
                    mgr.condition.wait(timeout=0.2)

            while self.app.running and mgr.running:
                with mgr.lock:
                    if not self.app.running or not mgr.running:
                        break
                    mgr.condition.wait(timeout=1.0)
                    frame = mgr.latest_frame

                if frame and frame != last_frame:
                    last_frame = frame
                    header = (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(frame)).encode('ascii') + b"\r\n\r\n"
                    )
                    self.wfile.write(header + frame + b"\r\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            mgr.client_disconnect()

    def _single_frame(self):
        """Serve single latest JPEG frame (fresh only, no stale or fake frames)."""
        self.app.preview_mgr.touch()
        mgr = self.app.preview_mgr

        frame = None
        last_err = None
        with mgr.lock:
            # Only accept frames from the last 2.0 seconds
            if mgr.latest_frame and (time.time() - mgr.latest_frame_time) <= 2.0:
                frame = mgr.latest_frame
            last_err = mgr.last_error

        if not frame:
            with mgr.lock:
                mgr.condition.wait(timeout=0.35)
                if mgr.latest_frame and (time.time() - mgr.latest_frame_time) <= 2.0:
                    frame = mgr.latest_frame
                last_err = mgr.last_error

        if not frame and self.app.picam2:
            try:
                bio = BytesIO()
                self.app.picam2.capture_file(bio, format="jpeg", name="lores")
                frame = bio.getvalue()
                with mgr.lock:
                    mgr.latest_frame = frame
                    mgr.latest_frame_time = time.time()
                    mgr.last_error = None
            except Exception as e:
                frame = None
                last_err = str(e)
                with mgr.lock:
                    mgr.latest_frame = None
                    mgr.last_error = last_err

        if frame:
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', str(len(frame)))
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(frame)
        else:
            err_msg = last_err or "Camera frame capture stalled / not ready"
            self._json(503, {"error": "Camera frame capture failed", "details": err_msg})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path.rstrip('/')
        body   = self._read_body()

        if path == '/api/photo':
            threading.Thread(target=self._do_photo, daemon=True).start()
            self._json(200, {"success": True, "message": "Photo capture started"})
        elif path == '/api/record':
            threading.Thread(target=self._do_record, daemon=True).start()
            self._json(200, {"success": True, "message": "Toggle sent"})
        elif path == '/api/preview/stop':
            self.app.preview_mgr.stop()
            self._json(200, {"success": True})
        elif path == '/api/wifi/switch':
            target = body.get("target", "home")
            if hasattr(self.app, 'wifi_mgr'):
                if target == "home":
                    threading.Thread(target=self.app.wifi_mgr.switch_to_home, daemon=True).start()
                    self._json(200, {"success": True, "message": f"Switching to Home Wi-Fi '{self.app.wifi_mgr.home_ssid}'..."})
                elif target == "hotspot":
                    threading.Thread(target=self.app.wifi_mgr.switch_to_hotspot, daemon=True).start()
                    self._json(200, {"success": True, "message": f"Switching to Hotspot '{self.app.wifi_mgr.hotspot_con}'..."})
                else:
                    self._json(400, {"success": False, "message": "Invalid target"})
            else:
                self._json(500, {"success": False, "message": "WiFi manager not initialized"})
        elif path == '/api/mic':
            device = body.get("device", "")
            if isinstance(device, list):
                device = device[0] if device else ""
            if not device:
                q = urllib.parse.parse_qs(parsed.query)
                device = q.get("device", [""])[0]
            if device:
                self.app.active_audio_device = device
                log(f"[WEB] Audio device changed to: {device}")
                self._json(200, {"success": True, "active": device})
            else:
                self._json(400, {"success": False, "message": "Missing device"})
        else:
            self._json(404, {"error": "Not found"})

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path.rstrip('/')
        if path.startswith('/api/footage/'):
            name = urllib.parse.unquote(path[len('/api/footage/'):])
            self._delete_file(name)
        else:
            self._json(404, {"error": "Not found"})

    # ── Action workers (run in thread to avoid blocking HTTP server) ─────────

    def _do_photo(self):
        self.app.capture_photo()

    def _do_record(self):
        self.app.toggle_recording()

    # ── Response helpers ─────────────────────────────────────────────────────

    def _serve_html(self, html: str):
        data = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code: int, obj: dict):
        data = json.dumps(obj).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get('Content-Length', 0))
        if length:
            try:
                raw = self.rfile.read(length).decode('utf-8').strip()
                if raw.startswith('{'):
                    return json.loads(raw)
                return urllib.parse.parse_qs(raw)
            except Exception:
                pass
        return {}

    def _serve_file(self, name: str):
        """Stream a file from DCIM for download."""
        # Sanitise filename — no path traversal
        name   = Path(name).name
        fpath  = config.DCIM_DIR / name
        if not fpath.exists() or not fpath.is_file():
            self._json(404, {"error": "File not found"})
            return
        mime, _ = mimetypes.guess_type(str(fpath))
        mime     = mime or 'application/octet-stream'
        size     = fpath.stat().st_size
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(size))
        self.send_header('Content-Disposition', f'attachment; filename="{name}"')
        self.end_headers()
        try:
            with open(fpath, 'rb') as fp:
                while True:
                    chunk = fp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except Exception as e:
            log(f"[WEB] File send error: {e}")

    def _delete_file(self, name: str):
        if name == "all":
            self._delete_all_footage()
            return
        name  = Path(name).name
        if name == "log.txt":
            self._json(403, {"success": False, "message": "log.txt cannot be deleted"})
            return
        fpath = config.DCIM_DIR / name
        if not fpath.exists():
            self._json(404, {"success": False, "message": "File not found"})
            return
        try:
            fpath.unlink()
            log(f"[WEB] Deleted footage: {name}")
            self._json(200, {"success": True})
        except Exception as e:
            self._json(500, {"success": False, "message": str(e)})

    def _delete_all_footage(self):
        deleted = 0
        errors = []
        try:
            for p in config.DCIM_DIR.iterdir():
                if p.is_file() and p.name != "log.txt":
                    try:
                        p.unlink()
                        deleted += 1
                    except Exception as e:
                        errors.append(f"{p.name}: {e}")
            log(f"[WEB] Deleted all footage: {deleted} files removed.")
            self._json(200, {"success": True, "deleted_count": deleted, "errors": errors})
        except Exception as e:
            self._json(500, {"success": False, "message": str(e)})

    def _sse_stream(self):
        """Hold the connection open and stream status events via SSE."""
        try:
            self.connection.settimeout(35.0)
        except Exception:
            pass
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache, no-transform')
        self.send_header('Connection', 'keep-alive')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()

        q = _sse.subscribe()
        # Send current status immediately on connect
        try:
            initial = json.dumps(self.app.get_status_data())
            self.wfile.write(f"event: status\ndata: {initial}\n\n".encode('utf-8'))
            self.wfile.flush()
        except Exception:
            _sse.unsubscribe(q)
            return

        try:
            while True:
                try:
                    msg = q.get(timeout=25)   # 25s keepalive
                    self.wfile.write(msg.encode('utf-8'))
                    self.wfile.flush()
                except queue.Empty:
                    # timeout → send keepalive comment
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            _sse.unsubscribe(q)

    def _mjpeg_frame(self):
        """Capture a single JPEG frame via rpicam-still and return it.
        Preview is disabled while recording to avoid camera conflicts."""
        if self.app.is_recording or self.app.is_capturing:
            # Return a 1x1 black JPEG placeholder
            placeholder = (
                b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00'
                b'\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t'
                b'\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a'
                b'\x1f\x1e\x1d\x1a\x1c\x1c $.\' ",#\x1c\x1c(7),01444\x1f\'9=82<.342\x1e'
                b'\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00'
                b'\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00'
                b'\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b'
                b'\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04'
                b'\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa'
                b'\x07"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n'
                b'\x16\x17\x18\x19\x1a%&\'()*456789:CDEFGHIJSTUVWXYZcdefghijstu'
                b'vwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97'
                b'\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfb\xff\xd9'
            )
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', str(len(placeholder)))
            self.end_headers()
            self.wfile.write(placeholder)
            return

        try:
            proc = subprocess.run(
                ["rpicam-still", "-t", "200", "--nopreview", "-o", "-",
                 "--width", "640", "--height", "480", "-q", "60"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5
            )
            data = proc.stdout
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            log(f"[PREVIEW] Capture failed: {e}")
            self._json(503, {"error": "Preview unavailable"})


def make_handler(app_instance):
    """Return a handler class with the app bound."""
    class Handler(RecorderHTTPHandler):
        app = app_instance
    return Handler


def start_web_server(app_instance, port=WEB_PORT):
    handler = make_handler(app_instance)
    try:
        ThreadingHTTPServer.allow_reuse_address = True
        ThreadingHTTPServer.daemon_threads = True
        httpd = ThreadingHTTPServer(('', port), handler)
        log(f"[WEB] HTTP server listening on port {port}")
        httpd.serve_forever()
    except Exception as e:
        log(f"[WEB] Failed to start HTTP server on port {port}: {e}")


# ─────────────────────────────────────────
# Stale instance cleanup
# ─────────────────────────────────────────
def cleanup_stale_instance():
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f:
                old_pid = int(f.read().strip())
            log(f"[INIT] Found existing PID {old_pid}. Terminating...")
            os.kill(old_pid, signal.SIGTERM)
            time.sleep(1.0)
            try:
                os.kill(old_pid, signal.SIGKILL)
            except OSError:
                pass
        except Exception as e:
            log(f"[INIT] Could not kill old PID: {e}")
        try:
            os.unlink(PID_FILE)
        except OSError:
            pass


# ─────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────
def main():
    cleanup_stale_instance()

    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    app = RecorderApp()

    def sig_handler(signum, frame):
        log(f"\n[SIGNAL] Received signal {signum}. Exiting cleanly...")
        app.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    # Start web server in a background daemon thread
    web_thread = threading.Thread(
        target=start_web_server,
        args=(app, WEB_PORT),
        daemon=True,
        name="WebServer",
    )
    web_thread.start()

    log("══════════════════════════════════════════════════════")
    log("  Raspberry Pi Zero 2 W Recorder Daemon Started       ")
    log(f"  Photo Button: GPIO {config.PIN_BTN_PHOTO} | LED 1: GPIO {config.PIN_LED_PHOTO}")
    log(f"  Record Button: GPIO {config.PIN_BTN_RECORD} | LED 2: GPIO {config.PIN_LED_RECORD}")
    log(f"  Storage Directory: {config.DCIM_DIR}")
    log(f"  Control Socket: {config.IPC_SOCKET_PATH}")
    log(f"  Web UI: http://0.0.0.0:{WEB_PORT}")
    log("══════════════════════════════════════════════════════")

    try:
        while app.running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        app.stop()


if __name__ == "__main__":
    main()
