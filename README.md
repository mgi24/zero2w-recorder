# Raspberry Pi Zero 2 W Recorder

> Pi Zero 2 W as action cam & field recorder — supports Raspberry Pi CSI cameras (Picamera2) + USB Microphone.

Sistem perekam kamera dan audio berkinerja tinggi yang dirancang khusus untuk **Raspberry Pi Zero 2 W** menggunakan **Picamera2** (libcamera), akselerasi perangkat keras H.264 (VideoCore IV), kontainer crash-resilient MPEG-TS, dan dashboard pemantauan berbasis web.

---

## 🌟 Fitur Utama

- **Dual-Stream Pipeline (Picamera2)**:
  - **Main Stream (1080p24)**: Perekaman video Full HD dengan hardware encoder VideoCore IV V4L2 M2M (6 Mbps) dan pengambilan foto instan (~75 ms).
  - **Lores Stream (640×360)**: Live MJPEG streaming untuk Web Preview menggunakan hardware ISP scaler bawaan Raspberry Pi tanpa membebani CPU.
- **Instant Photo Snapshots**: Pengambilan foto beresolusi tinggi dapat dilakukan secara instan tanpa menghentikan atau menjeda perekaman video yang sedang berjalan.
- **Crash-Resilient MPEG-TS**: Format kontainer MPEG-TS (`.ts`) menjamin rekaman video tetap utuh dan dapat diputar meskipun Raspberry Pi mengalami mati listrik tiba-tiba atau shutdown paksa (tanpa ketergantungan MOOV atom seperti pada MP4).
- **Audio Berkualitas Tinggi**: Perekaman audio stereo via ALSA (USB Microphone) dengan codec AAC 48kHz 128 kbps yang ter-sinkronisasi secara real-time.
- **Web Dashboard & Real-Time Monitoring**:
  - UI Web modern berbasis Dark Mode pada port `80`.
  - Live video preview MJPEG (15 FPS).
  - Server-Sent Events (SSE) untuk status perangkat, timer rekaman, dan notifikasi instan.
  - File manager untuk mengunduh dan meninjau rekaman langsung dari browser.
- **GPIO Hardware Triggers & IPC**:
  - Tombol fisik & indikator LED dengan hardware debounce filter.
  - Kontrol headless via Unix Socket IPC (`/tmp/recorder_control.sock`).
- **Systemd Autostart Service**: Berjalan sebagai daemon otomatis saat Raspberry Pi booting dan pulih otomatis saat terjadi gangguan.

---

## 📐 Pinout GPIO

| Komponen | GPIO BCM | Physical Pin | Mode | Keterangan |
| :--- | :--- | :--- | :--- | :--- |
| **Photo Button** | GPIO 17 | Pin 11 | Input (Pull-Up) | Menekan tombol akan memicu pengambilan foto instan |
| **Photo LED** | GPIO 27 | Pin 13 | Output | Indikator snapshot aktif |
| **Record Button** | GPIO 23 | Pin 16 | Input (Pull-Up) | Menekan tombol akan memulai / menghentikan rekaman video |
| **Record LED** | GPIO 24 | Pin 18 | Output | Indikator rekaman aktif (solid menyala saat merekam) |

---

## 📁 Struktur Direktori

```text
├── config.py             # Konfigurasi hardware GPIO, resolusi, bitrate, dan audio
├── recorder.py           # Daemon utama (Picamera2, Web Server, IPC, GPIO handler)
├── recorder.service      # Unit file systemd untuk autostart pada boot
├── convert_ts_to_mp4.py  # Utilitas konversi MPEG-TS ke MP4 dengan metadata
├── deploy.py             # Skrip deploy otomatis ke Raspberry Pi via SSH/SCP
├── flow.md               # Diagram arsitektur dan flowchart sistem (Mermaid TD)
├── RULES.md              # Aturan direktori unduhan
├── tests/                # Skrip pengujian, benchmark, dan simulasi trigger
│   ├── trigger_sim.py    # CLI simulator event tombol dan IPC
│   ├── verify_local.py   # Skrip analisis & verifikasi footage hasil rekaman
│   ├── bench_comparison.py # Script benchmark performa kamera vs USB webcam
│   ├── scratch_ssh.py    # Utilitas remote command via SSH
│   ├── test_mic_none.py  # Pengujian ALSA audio capture
│   └── test_ssh_run.py   # Runner pengujian remote SSH
└── downloads/            # Direktori lokal untuk hasil download dari Raspberry Pi
```

---

## 🚀 Instalasi & Menjalankan

### 1. Kebutuhan Sistem pada Raspberry Pi Zero 2 W
- Raspberry Pi OS (Bookworm 64-bit disarankan)
- Python 3.9+
- Paket dependensi:
  ```bash
  sudo apt update
  sudo apt install -y python3-picamera2 ffmpeg alsa-utils python3-pip
  ```

### 2. Konfigurasi
Sesuaikan konfigurasi audio dan pin GPIO pada `config.py` jika diperlukan. Cek nama perangkat mikrofon USB:
```bash
arecord -l
```

### 3. Menjalankan Manual
```bash
sudo python3 recorder.py
```

### 4. Mengaktifkan Systemd Service (Autostart saat Boot)
```bash
sudo cp recorder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now recorder.service
```

Untuk melihat log:
```bash
journalctl -u recorder.service -f
```

---

## 🌐 Web UI & API Endpoints

Akses dashboard melalui browser: `http://<ip-raspberry-pi>/`

- `GET /` : Antarmuka Dashboard Web
- `GET /preview` : MJPEG Live Video Stream
- `GET /events` : Real-Time Event Stream (SSE)
- `POST /api/photo` : Memicu snapshot foto
- `POST /api/record` : Toggle mulai / stop perekaman video
- `GET /api/footage` : Daftar file rekaman di `DCIM/`

---

## 🔄 Alur Sistem

Lihat [flow.md](flow.md) untuk melihat diagram alur detail proses boot, pipeline kamera, dan penanganan crash resilience.
