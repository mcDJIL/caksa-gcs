import asyncio
import json
import math
import threading
import queue
import sys
import os

# --- LIBRARY DRONE ---
import websockets
from mavsdk import System
from mavsdk.offboard import (VelocityBodyYawspeed)
from pymavlink import mavutil

# --- LIBRARY VOICE OFFLINE (SAFE IMPORT) ---
try:
    import sounddevice as sd
    from vosk import Model, KaldiRecognizer
    VOICE_AVAILABLE = True
except ImportError:
    VOICE_AVAILABLE = False
    print("[SYSTEM] Voice module not found. Installing defaults.")

# --- KONFIGURASI KONEKSI ---
CONNECTION_STRING = "udpin://0.0.0.0:14550"
WS_PORT = 8080
MAVLINK_UPLOAD = "udpin:0.0.0.0:14551"  # Separate port for pymavlink mission upload

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

# --- INIT VOICE ---
voice_cmd_queue = asyncio.Queue()
is_voice_listening = False
voice_model = None

if VOICE_AVAILABLE:
    model_path = resource_path("model")
    if os.path.exists(model_path):
        try:
            null_fds = [os.open(os.devnull, os.O_RDWR) for x in range(2)]
            save = os.dup(2)
            os.dup2(null_fds[1], 2)
            voice_model = Model(model_path)
            os.dup2(save, 2)
            os.close(save)
            print("[SYSTEM] Voice Model Loaded.")
        except: pass

current_telemetry = {
    "connected": False, "armed": False, "mode": "UNKNOWN",
    "battery_voltage": 0.0, "battery_remaining": 0.0,
    "latitude": 0, "longitude": 0, "altitude_relative": 0,
    "heading": 0, "pitch": 0, "roll": 0,
    "satellites": 0, "ground_speed": 0, "climb_rate": 0,
    "health_gyro": False, "health_accel": False, "health_mag": False, "health_gps": False, "health_home": False,
    "rc_rssi": 0
}

connected_clients = set()
drone_system = None
is_offboard_active = False
preflight_passed = False # STATE WAJIB PRE-FLIGHT

# FIX: Fungsi ini tadi namanya tidak sengaja terhapus
def voice_worker(loop):
    global is_voice_listening
    if not voice_model or not VOICE_AVAILABLE: return

    q = queue.Queue()
    def callback(indata, frames, time, status):
        q.put(bytes(indata))

    try:
        with sd.RawInputStream(samplerate=16000, blocksize=8000, channels=1, callback=callback):
            rec = KaldiRecognizer(voice_model, 16000)
            while True:
                if not is_voice_listening:
                    sd.sleep(500)
                    continue
                data = q.get()
                if rec.AcceptWaveform(data):
                    res = json.loads(rec.Result())
                    text = res['text']
                    if text:
                        print(f"[VOICE]: {text}")
                        try:
                            asyncio.run_coroutine_threadsafe(voice_cmd_queue.put(text), loop)
                        except RuntimeError:
                            break
    except: pass

def get_offset_location(lat, lon, d_north, d_east):
    earth_radius = 6378137.0 
    d_lat = d_north / earth_radius
    d_lon = d_east / (earth_radius * math.cos(math.pi * lat / 180))
    return lat + (d_lat * 180 / math.pi), lon + (d_lon * 180 / math.pi)

async def generate_figure8_mission(altitude=15):
    clat = current_telemetry["latitude"]
    clon = current_telemetry["longitude"]

    if abs(clat) < 0.001:
        print("!!! GPS belum fix !!!")
        return None

    items = []

    def wp(frame, cmd, p1=0, p2=0, p3=0, p4=0, x=None, y=None, z=0):
        items.append({
            'seq': len(items), 'frame': frame, 'command': cmd,
            'current': 0, 'autocontinue': 1,
            'param1': float(p1), 'param2': float(p2),
            'param3': float(p3), 'param4': float(p4),
            'x': int((x if x is not None else clat) * 1e7),
            'y': int((y if y is not None else clon) * 1e7),
            'z': float(z),
        })

    # Home (seq 0, frame=GLOBAL)
    wp(0, 16, z=altitude)
    # Takeoff (cmd 22)
    wp(3, 22, z=altitude)

    # Figure-8 waypoints (clean infinity/figure-8 pattern)
    radius = 30
    steps = 24
    for i in range(steps):
        t = (2 * math.pi * i) / steps
        d_east  = radius * math.sin(t)
        d_north = (radius / 2) * math.sin(2 * t)
        wlat, wlon = get_offset_location(clat, clon, d_north, d_east)
        wp(3, 16, p2=2.0, x=wlat, y=wlon, z=altitude)

    # Kembali ke titik awal
    wp(3, 16, z=altitude)
    # Land (cmd 21)
    wp(3, 21)

    print(f"[MISSION] Total {len(items)} waypoints generated")
    for item in items:
        print(f"  [seq={item['seq']}] cmd={item['command']} "
              f"lat={item['x']/1e7:.6f} lon={item['y']/1e7:.6f} alt={item['z']}m")

    return items

def upload_mission_mavlink(items):
    """Upload mission to ArduPilot via pymavlink (separate UDP port)."""
    print(f"[MAVLINK] Connecting to {MAVLINK_UPLOAD}...")
    conn = mavutil.mavlink_connection(MAVLINK_UPLOAD)
    conn.wait_heartbeat(timeout=10)
    print(f"[MAVLINK] Connected (sysid={conn.target_system} comp={conn.target_component})")

    # Clear existing mission
    conn.mav.mission_clear_all_send(
        conn.target_system, conn.target_component, 0
    )
    conn.recv_match(type='MISSION_ACK', blocking=True, timeout=5)

    n = len(items)
    conn.mav.mission_count_send(
        conn.target_system, conn.target_component, n, 0
    )

    for _ in range(n + 10):  # allow retransmissions
        msg = conn.recv_match(
            type=['MISSION_REQUEST_INT', 'MISSION_REQUEST', 'MISSION_ACK'],
            blocking=True, timeout=10
        )
        if msg is None:
            conn.close()
            raise Exception("Timeout during mission upload")

        if msg.get_type() == 'MISSION_ACK':
            conn.close()
            if msg.type == 0:  # MAV_MISSION_ACCEPTED
                print(f"[MAVLINK] Upload sukses! ({n} items)")
                return
            raise Exception(f"Mission rejected (NACK type={msg.type})")

        item = items[msg.seq]
        conn.mav.mission_item_int_send(
            conn.target_system, conn.target_component,
            item['seq'], item['frame'], item['command'],
            item['current'], item['autocontinue'],
            item['param1'], item['param2'], item['param3'], item['param4'],
            item['x'], item['y'], item['z'], 0
        )

    conn.close()
    raise Exception("Upload failed: too many retransmissions")

async def perform_preflight():
    r = []
    h_imu = current_telemetry["health_gyro"] and current_telemetry["health_accel"]
    r.append({"id":"imu", "label":"IMU Sensors", "status":"PASS" if h_imu else "FAIL", "detail":"OK" if h_imu else "Calibrate"})
    
    h_gps = current_telemetry["health_gps"]
    sats = current_telemetry["satellites"]
    r.append({"id":"gps", "label":f"GPS ({sats})", "status":"PASS" if h_gps else "WARN", "detail":"3D Lock" if h_gps else "Waiting"})

    h_mag = current_telemetry["health_mag"]
    r.append({"id":"mag", "label":"Compass", "status":"PASS" if h_mag else "WARN", "detail":"OK" if h_mag else "Check"})

    bat = current_telemetry["battery_remaining"]
    r.append({"id":"bat", "label":f"Battery {int(bat)}%", "status":"PASS" if bat>20 else "WARN", "detail":"Good"})

    rc = current_telemetry["rc_rssi"]
    r.append({"id":"rc", "label":"RC Signal", "status":"PASS" if rc > 0 else "WARN", "detail": f"{rc}%"})
    
    home = current_telemetry["health_home"]
    r.append({"id":"home", "label":"Home Point", "status":"PASS" if home else "WARN", "detail":"Set" if home else "Wait GPS"})

    return r

async def stream_position():
    async for p in drone_system.telemetry.position():
        current_telemetry["latitude"], current_telemetry["longitude"], current_telemetry["altitude_relative"] = p.latitude_deg, p.longitude_deg, p.relative_altitude_m

async def stream_battery():
    async for b in drone_system.telemetry.battery():
        v = b.voltage_v
        rem = b.remaining_percent
        # Memastikan tidak ada crash/error akibat nilai NaN (kosong) dari SITL
        current_telemetry["battery_voltage"] = v if not math.isnan(v) else 0.0
        # Beberapa autopilot/SITL kirim -1 saat data battery belum valid.
        if math.isnan(rem) or rem < 0:
            current_telemetry["battery_remaining"] = 0.0
        else:
            current_telemetry["battery_remaining"] = max(0.0, min(100.0, rem * 100))

async def stream_attitude():
    async for a in drone_system.telemetry.attitude_euler():
        current_telemetry["roll"], current_telemetry["pitch"], current_telemetry["heading"] = a.roll_deg, a.pitch_deg, a.yaw_deg

async def stream_flight_mode():
    async for m in drone_system.telemetry.flight_mode(): current_telemetry["mode"] = str(m)

async def stream_health():
    async for h in drone_system.telemetry.health():
        current_telemetry["health_gyro"], current_telemetry["health_accel"] = h.is_gyrometer_calibration_ok, h.is_accelerometer_calibration_ok
        current_telemetry["health_mag"], current_telemetry["health_gps"], current_telemetry["health_home"] = h.is_magnetometer_calibration_ok, h.is_global_position_ok, h.is_home_position_ok

async def stream_gps():
    async for g in drone_system.telemetry.gps_info(): current_telemetry["satellites"] = g.num_satellites

async def stream_armed():
    async for a in drone_system.telemetry.armed(): current_telemetry["armed"] = a

async def stream_velocity():
    async for v in drone_system.telemetry.velocity_ned():
        current_telemetry["ground_speed"], current_telemetry["climb_rate"] = math.sqrt(v.north_m_s**2 + v.east_m_s**2), -v.down_m_s

async def start_telemetry():
    for f in [stream_position, stream_battery, stream_attitude, stream_flight_mode, stream_health, stream_gps, stream_armed, stream_velocity]:
        asyncio.create_task(f())

async def broadcast_loop():
    while True:
        if connected_clients:
            data = current_telemetry.copy()
            for k in ["health_gyro", "health_accel", "health_mag", "health_gps", "health_home", "rc_rssi"]: data.pop(k, None)
            msg = json.dumps(data)
            for client in list(connected_clients):
                try: await client.send(msg)
                except: connected_clients.remove(client)
        await asyncio.sleep(0.05)

async def voice_command_processor():
    global is_offboard_active
    while True:
        text = await voice_cmd_queue.get()
        print(f"[VOICE CMD]: {text}")
        try:
            if "arm" in text: await drone_system.action.arm()
            elif "disarm" in text: await drone_system.action.disarm()
            elif "take off" in text: await drone_system.action.takeoff()
            elif "land" in text: await drone_system.action.land()
            elif "rtl" in text: await drone_system.action.return_to_launch()
        except Exception as e: print(f"Voice Action Failed: {e}")

async def setup_ardupilot_sitl():
    try:
        print("Mengatur parameter ArduPilot untuk SITL...")
        await drone_system.param.set_param_int("ARMING_CHECK", 0)
        await drone_system.param.set_param_int("FRAME_CLASS", 1) 
        print(">>> Pre-arm checks dimatikan & Frame diset ke Quadcopter (SIAP TERBANG!)")
    except Exception as e:
        print(f"Gagal mengatur parameter: {e}")

async def websocket_handler(websocket):
    global is_offboard_active, is_voice_listening, preflight_passed
    connected_clients.add(websocket)
    print(">>> Frontend Connected")
    try:
        async for message in websocket:
            data = json.loads(message)
            mtype = data.get('type')

            if mtype == 'TOGGLE_VOICE_BACKEND':
                is_voice_listening = data.get('state', False)

            elif mtype == 'COMMAND_LONG':
                if data['param1'] == 1: 
                    if not preflight_passed:
                        print("!!! GAGAL ARMING: WAJIB MELAKUKAN PRE-FLIGHT CHECK (RUN DIAGNOSTICS) DULU !!!")
                        continue
                    print("Mencoba ARM...")
                    try:
                        await drone_system.action.hold()
                        await asyncio.sleep(0.5)
                        await drone_system.action.arm()
                        print(">>> Baling-baling berputar (ARMED)!")
                    except Exception as e:
                        print(f"!!! ARDUPILOT MENOLAK ARMING: {e} !!!")
                else: 
                    try: await drone_system.action.disarm()
                    except Exception as e: print(f"!!! GAGAL DISARM: {e} !!!")

            elif mtype == 'SET_MODE':
                mode = data['mode']
                try:
                    if mode == 'MISSION':
                        if not current_telemetry["armed"]: 
                            if not preflight_passed:
                                print("!!! GAGAL MEMULAI MISI: Pre-Flight Check Belum Dilakukan !!!")
                                continue
                            await drone_system.action.hold()
                            await asyncio.sleep(0.5)
                            await drone_system.action.arm()
                        await drone_system.mission_raw.start_mission()
                        print(">>> Misi Dimulai!")
                    elif mode == 'OFFBOARD':
                        await drone_system.offboard.set_velocity_body(VelocityBodyYawspeed(0.0,0.0,0.0,0.0))
                        await drone_system.offboard.start()
                        is_offboard_active = True
                        print(">>> Masuk Mode Offboard (Manual Keyboard)")
                    elif mode == 'TAKEOFF': 
                        if not preflight_passed:
                             print("!!! GAGAL TAKEOFF: Pre-Flight Check Belum Dilakukan !!!")
                             continue
                        print("Mencoba Takeoff...")
                        await drone_system.action.takeoff()
                    elif mode == 'LAND': await drone_system.action.land()
                    elif mode == 'RTL': await drone_system.action.return_to_launch()
                    elif mode == 'HOLD': await drone_system.action.hold()
                except Exception as e:
                    print(f"!!! GAGAL GANTI MODE {mode}: {e} !!!")

            elif mtype == 'MANUAL_CONTROL' and is_offboard_active:
                await drone_system.offboard.set_velocity_body(
                    VelocityBodyYawspeed(float(data['x']), float(data['y']), float(data['z']), float(data['r']))
                )

            elif mtype == 'UPLOAD_MISSION_FIGURE8':
                items = await generate_figure8_mission()
                if not items:
                    await websocket.send(json.dumps({
                        "type": "MISSION_UPLOAD_STATUS", "success": False,
                        "message": "GPS belum fix atau posisi tidak valid"
                    }))
                    continue

                try:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, upload_mission_mavlink, items)

                    await websocket.send(json.dumps({
                        "type": "MISSION_UPLOAD_STATUS", "success": True,
                        "message": f"Upload sukses ({len(items)} waypoints). ARM drone lalu tekan START MISSION."
                    }))

                except Exception as e:
                    print(f"!!! UPLOAD MISI GAGAL: {e} !!!")
                    await websocket.send(json.dumps({
                        "type": "MISSION_UPLOAD_STATUS", "success": False,
                        "message": str(e)
                    }))

            elif mtype == 'REQ_PREFLIGHT':
                report = await perform_preflight()
                all_passed = all(item["status"] in ["PASS", "WARN"] for item in report)
                if all_passed:
                    preflight_passed = True
                    print(">>> PRE-FLIGHT CHECK SELESAI: Drone siap diterbangkan!")
                else:
                    preflight_passed = False
                    print("!!! PRE-FLIGHT GAGAL: Ada sensor yang bermasalah (FAIL) !!!")
                    
                await websocket.send(json.dumps({"type": "PREFLIGHT_REPORT", "report": report}))

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if websocket in connected_clients: connected_clients.remove(websocket)
        print(">>> Frontend Disconnected")

async def main():
    global drone_system
    drone_system = System()
    print(f"--- CAKSA GCS BACKEND ---")
    
    while True:
        try:
            await drone_system.connect(system_address=CONNECTION_STRING)
            break
        except: await asyncio.sleep(1)

    print("Waiting for Heartbeat...")
    async for state in drone_system.core.connection_state():
        if state.is_connected:
            print(">>> DRONE CONNECTED")
            break
            
    await setup_ardupilot_sitl()

    loop = asyncio.get_running_loop()
    threading.Thread(target=voice_worker, args=(loop,), daemon=True).start()
    
    await start_telemetry()
    server = await websockets.serve(websocket_handler, "localhost", WS_PORT)
    await asyncio.gather(broadcast_loop(), voice_command_processor(), server.wait_closed())

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: print("Backend Dimatikan.")
    except Exception as e: print(f"Server Exited: {e}")