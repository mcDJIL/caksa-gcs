# CAKSA GCS — Ground Control Station untuk Drone

**CAKSA GCS** adalah aplikasi Desktop Ground Control Station modern yang dibangun menggunakan framework **Tauri v2**. Dirancang untuk memonitor dan mengendalikan drone berbasis ArduPilot melalui protokol MAVLink, dengan antarmuka responsif, fitur misi otonom, dan voice control offline.

---

## Arsitektur Sistem

```
┌──────────────────────────────────────────┐
│           Tauri v2 Desktop App           │
│  ┌────────────────┐  ┌────────────────┐  │
│  │  React + Vite   │  │   Rust Shell   │  │
│  │  (Frontend UI)  │  │   (Sidecar)    │  │
│  └───────┬────────┘  └───────┬────────┘  │
│          │ WebSocket :8080   │ spawn      │
│  ┌───────▼───────────────────▼─────────┐ │
│  │      Python Backend (main.py)       │ │
│  │  MAVSDK (telemetry/action)          │ │
│  │  pymavlink (mission upload)         │ │
│  │  Vosk (voice control offline)       │ │
│  └──────────┬──────────┬──────────────┘  │
└─────────────┼──────────┼────────────────┘
              │UDP:14550 │UDP:14551
         ┌────▼──────────▼────┐
         │  ArduPilot SITL    │
         │  (sim_vehicle.py)  │
         └────────┬───────────┘
                  │ JSON
         ┌────────▼───────────┐
         │   Gazebo Sim       │
         └────────────────────┘
```

| Layer | Teknologi |
|---|---|
| Desktop Shell | Tauri v2 + Rust |
| Frontend UI | React 19 + TypeScript + Tailwind CSS v4 + Leaflet + Three.js |
| Backend Sidecar | Python 3.11+ (MAVSDK, pymavlink, Vosk, WebSockets) |
| Simulator | ArduPilot SITL + Gazebo |

---

## Fitur Utama

- **Real-time Telemetry (20Hz)** — Posisi GPS, attitude (roll/pitch/yaw), baterai, kecepatan, satellite count
- **Tri-View System:**
  - **2D Map** — Leaflet map dengan marker drone + heading auto-rotate
  - **3D View** — Visualisasi orientasi drone real-time (Three.js)
  - **Live Cam** — Video feed placeholder
- **Artificial Horizon (HUD)** — Instrumen penerbangan sekelas kokpit
- **Smart Pre-Flight Check** — Diagnostik wajib: IMU, GPS, kompas, baterai, RC, home point
- **Mission Planner** — Upload & eksekusi misi Figure-8 otonom dengan satu klik
- **Keyboard Offboard Control** — Kontrol manual via keyboard (WASD + Arrow)
- **Offline Voice Command** — Perintah suara bahasa Indonesia tanpa internet (Vosk)
- **Multi-mode** — Takeoff, Land, RTL, Hold, Mission, Offboard

---

## Prasyarat

### Windows (Host — Aplikasi Utama)

| Software | Versi | Keterangan |
|---|---|---|
| **Node.js** | 18+ | Frontend build |
| **Rust** | stable | Tauri backend |
| **Python** | 3.11+ | Sidecar backend |
| **Git** | any | Clone repository |
| **C++ Build Tools** | MSVC | Diperlukan Rust/Tauri |

Install Rust (jika belum):
```powershell
winget install Rustlang.Rustup
rustup default stable
```

### WSL2 / Linux (Simulator)

| Software | Keterangan |
|---|---|
| **ArduPilot SITL** | Simulator drone |
| **Gazebo** (Harmonic/Garden) | 3D physics simulator |
| **ardupilot_gazebo** plugin | Bridge ArduPilot ↔ Gazebo |

---

## Setup — Step by Step

### 1. Clone Repository

```powershell
git clone <repo-url> caksa-gcs
cd caksa-gcs
```

### 2. Install Frontend Dependencies

```powershell
npm install
```

### 3. Setup Python Backend

Buat virtual environment dan install dependencies:

```powershell
cd python-sidecar
python -m venv venv
.\venv\Scripts\activate
pip install mavsdk pymavlink websockets sounddevice vosk
```

#### Download Voice Model (Opsional)

Untuk fitur voice control, download model Vosk bahasa Indonesia dan ekstrak ke folder `python-sidecar/model/`:

```
python-sidecar/
  model/
    am/
    conf/
    graph/
    ivector/
```

> **Model:** Download dari [vosk-model-small-id](https://alphacephei.com/vosk/models)
>
> Jika tidak butuh voice control, folder model bisa dikosongkan — backend tetap berjalan normal tanpa fitur suara.

### 4. Build Python Sidecar (Executable)

Tauri membutuhkan backend Python dalam bentuk executable binary. Build dengan PyInstaller:

```powershell
cd python-sidecar
pip install pyinstaller
pyinstaller caksa-backend.spec
```

Hasil build: `python-sidecar/dist/caksa-backend.exe`

Copy ke folder binaries Tauri **dengan nama sesuai target triple**:

```powershell
copy dist\caksa-backend.exe ..\src-tauri\binaries\caksa-backend-x86_64-pc-windows-msvc.exe
```

> **PENTING:** Nama file harus persis `caksa-backend-x86_64-pc-windows-msvc.exe`. Tauri menambahkan target triple secara otomatis saat runtime.

### 5. Setup ArduPilot SITL (di WSL2)

```bash
# Install dependencies
sudo apt update
sudo apt install git python3-pip python3-dev python3-venv

# Clone ArduPilot
git clone --recurse-submodules https://github.com/ArduPilot/ardupilot.git
cd ardupilot

# Install tools
Tools/environment_install/install-prereqs-ubuntu.sh -y
. ~/.profile

# Build ArduCopter SITL
./waf configure --board sitl
./waf copter
```

### 6. Setup Gazebo + Plugin (di WSL2)

```bash
# Install Gazebo Harmonic (Ubuntu 22.04+)
sudo apt install gz-harmonic

# Clone dan build plugin ArduPilot-Gazebo
git clone https://github.com/ArduPilot/ardupilot_gazebo.git
cd ardupilot_gazebo
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=RelWithDebInfo
make -j4

# Set environment variables (tambahkan ke ~/.bashrc)
echo 'export GZ_SIM_SYSTEM_PLUGIN_PATH=$HOME/ardupilot_gazebo/build:$GZ_SIM_SYSTEM_PLUGIN_PATH' >> ~/.bashrc
echo 'export GZ_SIM_RESOURCE_PATH=$HOME/ardupilot_gazebo/models:$HOME/ardupilot_gazebo/worlds:$GZ_SIM_RESOURCE_PATH' >> ~/.bashrc
source ~/.bashrc
```

---

## Menjalankan dengan SITL + Gazebo

### Langkah 1 — Jalankan Gazebo (Terminal WSL #1)

```bash
gz sim -r iris_runway.sdf
```

### Langkah 2 — Jalankan ArduPilot SITL (Terminal WSL #2)

```bash
sim_vehicle.py -v ArduCopter -f JSON --console --map \
  --out=<IP_WINDOWS>:14550 \
  --out=<IP_WINDOWS>:14551
```

> **Cara cari IP Windows dari WSL:**
> ```bash
> cat /etc/resolv.conf | grep nameserver
> ```
> Contoh output: `nameserver 172.29.128.1`
>
> Maka command-nya:
> ```bash
> sim_vehicle.py -v ArduCopter -f JSON --console --map \
>   --out=172.29.128.1:14550 \
>   --out=172.29.128.1:14551
> ```

**Mengapa dua port?**
| Port | Dipakai Oleh | Fungsi |
|------|-------------|--------|
| 14550 | MAVSDK | Telemetry, arm, takeoff, mode change |
| 14551 | pymavlink | Mission upload (raw MAVLink protocol) |

### Langkah 3 — Jalankan Backend Python (Terminal Windows)

**Mode Development** (langsung Python):

```powershell
cd python-sidecar
.\venv\Scripts\activate
python main.py
```

Output yang diharapkan:
```
[SYSTEM] Voice Model Loaded.
--- CAKSA GCS BACKEND ---
Waiting for Heartbeat...
>>> DRONE CONNECTED
>>> Pre-arm checks dimatikan & Frame diset ke Quadcopter (SIAP TERBANG!)
```

### Langkah 4 — Jalankan Frontend / Tauri App (Terminal Windows)

**Opsi A — Tauri Desktop App (full build):**
```powershell
npm run tauri dev
```

**Opsi B — Browser saja (tanpa Tauri shell):**
> Jalankan backend Python manual (Langkah 3) terlebih dahulu, lalu:
```powershell
npm run dev
```
Buka `http://localhost:5173` di browser.

---

## Alur Operasi (Cara Pakai)

```
1. RUN DIAGNOSTICS  →  Pre-flight check (IMU, GPS, Battery, dll)
2. ARM              →  Baling-baling berputar
3. TAKEOFF          →  Drone naik ke altitude default
4. UPLOAD FIG-8     →  Upload misi figure-8 ke flight controller
5. START MISSION    →  Drone terbang mengikuti waypoint figure-8
6. RTL / LAND       →  Kembali ke home / landing
```

> **PENTING:** Pre-flight check **WAJIB** dilakukan sebelum ARM/Takeoff/Mission. Sistem akan menolak perintah jika belum dijalankan.

---

## Konfigurasi Koneksi

Semua konfigurasi ada di `python-sidecar/main.py`:

```python
CONNECTION_STRING = "udpin://0.0.0.0:14550"   # MAVSDK (telemetry/action)
WS_PORT = 8080                                 # WebSocket ke frontend
MAVLINK_UPLOAD = "udpin:0.0.0.0:14551"         # pymavlink (mission upload)
```

| Port | Protokol | Fungsi |
|------|----------|--------|
| 14550 | UDP MAVLink | MAVSDK — telemetry, arm, takeoff, mode |
| 14551 | UDP MAVLink | pymavlink — mission upload |
| 8080 | WebSocket | Frontend ↔ Backend komunikasi |
| 5173 | HTTP | Vite dev server (hanya development) |

> **Catatan format URI:**
> - MAVSDK: `udpin://host:port` (dengan `//`)
> - pymavlink: `udpin:host:port` (tanpa `//`)

---

## Build Production

### 1. Build Python Sidecar

```powershell
cd python-sidecar
.\venv\Scripts\activate
pyinstaller caksa-backend.spec
copy dist\caksa-backend.exe ..\src-tauri\binaries\caksa-backend-x86_64-pc-windows-msvc.exe
```

### 2. Build Tauri Installer

```powershell
npm run tauri build
```

Output installer: `src-tauri/target/release/bundle/` (MSI / NSIS)

---

## Struktur Folder

```
caksa-gcs/
├── src/                          # Frontend React
│   ├── App.tsx                   # Entry + backend sidecar launcher
│   ├── gcs-frontend.tsx          # UI utama (map, telemetry, controls)
│   ├── main.tsx                  # React root
│   └── index.css                 # Tailwind styles
├── src-tauri/                    # Tauri (Rust)
│   ├── tauri.conf.json           # Konfigurasi Tauri + external binary
│   ├── Cargo.toml                # Rust dependencies
│   ├── capabilities/default.json # Permission sidecar shell
│   ├── binaries/                 # Python executable (sidecar)
│   │   └── caksa-backend-x86_64-pc-windows-msvc.exe
│   └── src/
│       ├── main.rs               # Rust entry point
│       └── lib.rs                # Tauri plugin setup (shell)
├── python-sidecar/               # Python Backend
│   ├── main.py                   # Backend utama (MAVLink + WebSocket)
│   ├── requirements.txt          # Python dependencies
│   ├── caksa-backend.spec        # PyInstaller build config
│   └── model/                    # Vosk voice model (offline)
│       ├── am/                   # Acoustic model
│       ├── conf/                 # Configuration
│       ├── graph/                # Language model graph
│       └── ivector/              # i-vector extractor
├── package.json                  # Node.js dependencies
├── vite.config.ts                # Vite + Tailwind configuration
├── tsconfig.json                 # TypeScript configuration
└── README.md                     # File ini
```

---

## Troubleshooting

### Backend tidak connect ke SITL
- Pastikan SITL sudah running dan `--out` mengarah ke IP Windows yang benar
- Cek firewall Windows: buka UDP port **14550** dan **14551**
  ```powershell
  # Buka port di Windows Firewall
  netsh advfirewall firewall add rule name="SITL 14550" dir=in action=allow protocol=UDP localport=14550
  netsh advfirewall firewall add rule name="SITL 14551" dir=in action=allow protocol=UDP localport=14551
  ```
- Dari WSL, tes koneksi: `nc -u <IP_WINDOWS> 14550`

### Mission upload timeout / gagal
- Pastikan SITL dijalankan dengan **dua output**: `--out=...:14550 --out=...:14551`
- Mission upload menggunakan port **14551** via pymavlink (terpisah dari MAVSDK)
- Restart backend Python dan SITL jika error berulang

### Voice control tidak berfungsi
- Pastikan folder `python-sidecar/model/` berisi model Vosk yang valid
- Cek mikrofon terdeteksi: `python -c "import sounddevice; print(sounddevice.query_devices())"`
- Voice control bersifat **opsional** — aplikasi tetap berjalan tanpa fitur ini

### Tauri build error "sidecar not found"
- File executable harus ada di `src-tauri/binaries/` dengan nama: `caksa-backend-x86_64-pc-windows-msvc.exe`
- Build ulang dengan PyInstaller jika file belum ada:
  ```powershell
  cd python-sidecar && pyinstaller caksa-backend.spec
  copy dist\caksa-backend.exe ..\src-tauri\binaries\caksa-backend-x86_64-pc-windows-msvc.exe
  ```

### Frontend WebSocket error / tidak konek
- Pastikan backend Python sudah running di port **8080**
- Jika pakai mode browser (`npm run dev`), jalankan `python main.py` manual terlebih dahulu
- Jika pakai Tauri (`npm run tauri dev`), backend otomatis di-spawn sebagai sidecar