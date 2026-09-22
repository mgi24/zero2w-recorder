"""
Project Configuration for Raspberry Pi Zero 2 W Recorder
All settings for GPIO, Camera (OV5647), Hardware Video Encoding, and Audio Capture.
"""

import os
from pathlib import Path

# Base Paths
BASE_DIR = Path(__file__).resolve().parent
DCIM_DIR = BASE_DIR / "DCIM"
LOG_FILE_PATH = DCIM_DIR / "log.txt"

# Ensure DCIM directory exists
DCIM_DIR.mkdir(parents=True, exist_ok=True)

# GPIO Pin Configuration (BCM numbering)
# Can be overridden via environment variables if custom wiring is used
PIN_BTN_PHOTO = int(os.getenv("PIN_BTN_PHOTO", "17"))     # Button 1: Photo Capture
PIN_LED_PHOTO = int(os.getenv("PIN_LED_PHOTO", "27"))     # LED 1: Photo In-Progress Indicator
PIN_BTN_RECORD = int(os.getenv("PIN_BTN_RECORD", "23"))   # Button 2: Video Record Toggle
PIN_LED_RECORD = int(os.getenv("PIN_LED_RECORD", "24"))   # LED 2: Video Recording Active Indicator

# Button debounce time in seconds
BUTTON_DEBOUNCE_SEC = 0.3

# Camera (OV5647) Configuration
# Max sensor resolution for OV5647 is 2592 x 1944
PHOTO_WIDTH = 2592
PHOTO_HEIGHT = 1944
PHOTO_WARMUP_MS = 1500  # 1.5s warmup for auto exposure/white balance convergence
PHOTO_QUALITY = 95

# Auto-Exposure & Color Settings
EXPOSURE_MODE = "normal"   # libcamera AEC enabled
METERING_MODE = "centre"   # Centre-weighted metering
AWB_MODE = "auto"          # Auto white-balance enabled

# Video Recording Configuration
VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
VIDEO_FPS = 24
VIDEO_BITRATE = 6000000    # 6 Mbps H.264
VIDEO_CODEC = "libav"
VIDEO_LIB_CODEC = "h264_v4l2m2m"  # Hardware acceleration via V4L2 M2M (BCM2835 VideoCore IV)

# Audio Configuration (AB13X USB Soundcard)
AUDIO_DEVICE = "plughw:CARD=Audio,DEV=0"
AUDIO_SOURCE = "alsa"
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "128k"
AUDIO_SAMPLERATE = 48000
AUDIO_CHANNELS = 2
AUDIO_AV_SYNC_US = 0  # Timestamp offset in microseconds

# Container & Crash Resilience
# MPEG-TS (.ts) container writes independent packets (188 bytes) with periodic PAT/PMT and IDR frames.
# Unlike standard MP4, it does NOT require a final 'moov' atom index at the end of the file.
# If power is lost or crash occurs, all recorded footage up to that moment is fully intact.
CONTAINER_EXT = ".ts"

# Inter-Process Control Socket / Command File for Trigger Simulation
IPC_SOCKET_PATH = "/tmp/recorder_control.sock"
STATUS_FILE_PATH = "/tmp/recorder_status.json"

# Wi-Fi & Auto-Hotspot Failover Configuration
WIFI_HOME_SSID = os.getenv("WIFI_HOME_SSID", "TP-Link dev")
WIFI_HOTSPOT_SSID = os.getenv("WIFI_HOTSPOT_SSID", "rasbicam")
WIFI_HOTSPOT_IP = "192.168.2.1"
WIFI_CHECK_INTERVAL_SEC = 45  # Scan interval when in hotspot mode

# Log Buffer & Diagnostics
LOG_BUFFER_SIZE = 1000  # In-memory ring buffer of the latest 1000 log lines

# Camera Module 3 (IMX708) & Hardware Drop Watchdog
CAMERA_V3_SENSOR_SIZE = (2304, 1296)  # Full FOV 2x2 binned uncropped sensor mode
WATCHDOG_FRAME_TIMEOUT_SEC = 5.0      # Auto-stop if no frame delivered for 5s
