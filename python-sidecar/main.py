import asyncio
import json
import math
import threading
import queue
import sys
import os
import time

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

VISION_UDP_HOST = "127.0.0.1"
VISION_UDP_PORT = 9000

MISSION_ALTITUDE = 15.0
MISSION2_CLEARANCE = 30.0
MISSION2_PRECISION = 20
MISSION2_AREA_WIDTH = 100.0
MISSION2_AREA_LENGTH = 30.0
MISSION2_FOV = 70.0
MISSION2_OVERLAP = 0.30
MISSION2_POLE1_REL = (-80.0, 20.0)
MISSION2_POLE2_REL = (-230.0, 20.0)

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
    "rc_rssi": 0,
    "vision_enabled": False,
    "vision_target_active": False,
    "vision_target_dx": 0.0,
    "vision_target_dy": 0.0,
    "vision_last_seen": 0.0
}

connected_clients = set()
drone_system = None
is_offboard_active = False
preflight_passed = False # STATE WAJIB PRE-FLIGHT
vision_udp_transport = None

vision_state = {
    "enabled": False,
    "target_active": False,
    "target_dx": 0.0,
    "target_dy": 0.0,
    "last_seen": 0.0,
    "source": "udp",
    "host": VISION_UDP_HOST,
    "port": VISION_UDP_PORT,
}

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

def rel_xy_to_latlon(x_m, y_m, base_lat, base_lon):
    meters_to_lat_deg = 1 / 111111.0
    meters_to_lon_deg = 1 / (111111.0 * max(0.000001, math.cos(math.radians(base_lat))))
    lat = base_lat + (y_m * meters_to_lat_deg)
    lon = base_lon + (x_m * meters_to_lon_deg)
    return lat, lon

def latlon_to_rel_xy(lat, lon, home_lat, home_lon):
    meters_per_lat = 111111.0
    meters_per_lon = 111111.0 * max(0.000001, math.cos(math.radians(home_lat)))
    x = (lon - home_lon) * meters_per_lon
    y = (lat - home_lat) * meters_per_lat
    return x, y

def perpendicular_offset(point_a, point_b, origin_point, side, clearance):
    ax, ay = point_a
    bx, by = point_b
    ox, oy = origin_point

    vx = bx - ax
    vy = by - ay
    norm = math.sqrt(vx * vx + vy * vy)
    if norm == 0:
        raise ValueError("pole points are identical")

    nx = -vy / norm
    ny = vx / norm
    return ox + clearance * side * nx, oy + clearance * side * ny

def generate_capsule_waypoint(point_a, point_b, radius, tau):
    ax, ay = point_a
    bx, by = point_b

    vx = bx - ax
    vy = by - ay
    length = math.sqrt(vx * vx + vy * vy)
    if length == 0:
        raise ValueError("capsule endpoints are identical")

    ux = vx / length
    uy = vy / length
    nx = -uy
    ny = ux

    perimeter = 2 * length + 2 * math.pi * radius
    t = tau * perimeter

    if t < length:
        s = t / length
        px = ax + radius * nx + s * vx
        py = ay + radius * ny + s * vy
        segment = 0
    elif t < length + math.pi * radius:
        t2 = t - length
        theta = t2 / radius - math.pi / 2
        px = bx + radius * (math.cos(theta) * ux - math.sin(theta) * nx)
        py = by + radius * (math.cos(theta) * uy - math.sin(theta) * ny)
        segment = 1
    elif t < 2 * length + math.pi * radius:
        t3 = t - (length + math.pi * radius)
        s = t3 / length
        px = bx - radius * nx - s * vx
        py = by - radius * ny - s * vy
        segment = 2
    else:
        t4 = t - (2 * length + math.pi * radius)
        theta = t4 / radius - math.pi / 2
        px = ax + radius * (-math.cos(theta) * ux + math.sin(theta) * nx)
        py = ay + radius * (-math.cos(theta) * uy + math.sin(theta) * ny)
        segment = 3

    return px, py, segment

def generate_scan(area_width, area_length, altitude, fov_deg, overlap, n_line=10, n_turn=4):
    swath = 2 * altitude * math.tan(math.radians(fov_deg / 2))
    lane_spacing = swath * (1 - overlap)
    if lane_spacing <= 0:
        lane_spacing = 1.0

    n_lanes = max(1, int(math.ceil(area_width / lane_spacing)))
    total_span = (n_lanes - 1) * lane_spacing
    x0 = -total_span / 2
    lane_x = [x0 + i * lane_spacing for i in range(n_lanes)]
    radius = lane_spacing / 2
    half_len = area_length / 2

    waypoints = []
    for i in range(n_lanes):
        x = lane_x[i]
        going_up = (i % 2 == 0)
        y_start, y_end = (-half_len, half_len) if going_up else (half_len, -half_len)

        for j in range(n_line):
            alpha = 0 if n_line <= 1 else j / (n_line - 1)
            y = y_start + (y_end - y_start) * alpha
            waypoints.append((y, x))

        if i >= n_lanes - 1:
            continue

        x_next = lane_x[i + 1]
        y_turn = y_end
        cx = y_turn
        cy = (x + x_next) / 2
        for j in range(n_turn):
            alpha = 0 if n_turn <= 1 else j / (n_turn - 1)
            theta = (math.pi * (1 - alpha)) if going_up else (math.pi * alpha)
            arc_x = cx + radius * math.sin(theta)
            arc_y = cy + (radius * math.cos(theta) if going_up else -radius * math.cos(theta))
            waypoints.append((arc_x, arc_y))

    return waypoints

def rotate_points(points, theta):
    c = math.cos(theta)
    s = math.sin(theta)
    out = []
    for x, y in points:
        out.append((x * c - y * s, x * s + y * c))
    return out

def parse_float(value, default, min_value=None, max_value=None):
    try:
        parsed = float(value)
    except Exception:
        parsed = float(default)
    if min_value is not None:
        parsed = max(float(min_value), parsed)
    if max_value is not None:
        parsed = min(float(max_value), parsed)
    return parsed

def parse_int(value, default, min_value=None, max_value=None):
    try:
        parsed = int(value)
    except Exception:
        parsed = int(default)
    if min_value is not None:
        parsed = max(int(min_value), parsed)
    if max_value is not None:
        parsed = min(int(max_value), parsed)
    return parsed

def parse_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return value != 0
    return default

def resolve_runtime_poles_xy(home_lat, home_lon, params):
    use_pole_latlon = parse_bool(params.get('use_pole_latlon'), False)
    if not use_pole_latlon:
        return MISSION2_POLE1_REL, MISSION2_POLE2_REL, "default_rel"

    p1_lat = parse_float(params.get('pole1_lat'), 0.0, -90.0, 90.0)
    p1_lon = parse_float(params.get('pole1_lon'), 0.0, -180.0, 180.0)
    p2_lat = parse_float(params.get('pole2_lat'), 0.0, -90.0, 90.0)
    p2_lon = parse_float(params.get('pole2_lon'), 0.0, -180.0, 180.0)

    if abs(p1_lat) < 0.000001 and abs(p1_lon) < 0.000001:
        return MISSION2_POLE1_REL, MISSION2_POLE2_REL, "fallback_rel"
    if abs(p2_lat) < 0.000001 and abs(p2_lon) < 0.000001:
        return MISSION2_POLE1_REL, MISSION2_POLE2_REL, "fallback_rel"

    p1_xy = latlon_to_rel_xy(p1_lat, p1_lon, home_lat, home_lon)
    p2_xy = latlon_to_rel_xy(p2_lat, p2_lon, home_lat, home_lon)
    distance = math.sqrt((p1_xy[0] - p2_xy[0]) ** 2 + (p1_xy[1] - p2_xy[1]) ** 2)
    if distance < 1.0:
        return MISSION2_POLE1_REL, MISSION2_POLE2_REL, "fallback_rel"

    return p1_xy, p2_xy, "runtime_latlon"

def generate_figure8_around_poles(pole1_xy, pole2_xy, clearance_meters, precision):
    x1, y1 = pole1_xy
    x2, y2 = pole2_xy

    center_x = (x1 + x2) / 2.0
    center_y = (y1 + y2) / 2.0
    dx = x1 - x2
    dy = y1 - y2
    distance = math.sqrt(dx * dx + dy * dy)
    angle = math.atan2(dy, dx)

    # Lemniscate (Gerono): dua loop simetris dengan transisi halus tanpa segmen lurus tambahan.
    a = max((distance / 2.0) + clearance_meters, 1.0)
    b = max(clearance_meters, 1.0)
    total_points = max(24, precision * 6)

    waypoints_xy = []
    for i in range(total_points):
        t = (2.0 * math.pi * i) / total_points
        local_x = a * math.sin(t)
        local_y = b * math.sin(t) * math.cos(t)
        rot_x = local_x * math.cos(angle) - local_y * math.sin(angle)
        rot_y = local_x * math.sin(angle) + local_y * math.cos(angle)
        waypoints_xy.append((center_x + rot_x, center_y + rot_y))

    return waypoints_xy

def reorder_figure8_start_near_drone(points_xy, pole1_xy, pole2_xy, clearance_meters):
    if not points_xy:
        return points_xy

    cx = (pole1_xy[0] + pole2_xy[0]) / 2.0
    cy = (pole1_xy[1] + pole2_xy[1]) / 2.0
    avoid_center_radius = max(5.0, clearance_meters * 0.35)

    def dist_to_drone_sq(p):
        # Drone position saat generate misi dipakai sebagai origin relatif (0,0).
        return p[0] * p[0] + p[1] * p[1]

    candidates = []
    for i, p in enumerate(points_xy):
        d_center = math.sqrt((p[0] - cx) ** 2 + (p[1] - cy) ** 2)
        if d_center >= avoid_center_radius:
            candidates.append(i)

    candidate_indices = candidates if candidates else list(range(len(points_xy)))
    start_idx = min(candidate_indices, key=lambda i: dist_to_drone_sq(points_xy[i]))

    return points_xy[start_idx:] + points_xy[:start_idx]

def validate_mission_points(latlon_points):
    if not latlon_points:
        raise ValueError("Waypoint kosong")
    for idx, (lat, lon) in enumerate(latlon_points):
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError(f"Waypoint {idx} tidak valid: lat/lon out of range")

def build_mission_items_from_latlon(points_latlon, altitude=MISSION_ALTITUDE):
    clat = current_telemetry["latitude"]
    clon = current_telemetry["longitude"]
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

    wp(0, 16, z=altitude)
    wp(3, 22, z=altitude)
    for lat, lon in points_latlon:
        wp(3, 16, p2=2.0, x=lat, y=lon, z=altitude)
    wp(3, 16, z=altitude)
    wp(3, 21)
    return items

async def generate_mission2_capsule_scan(
    altitude=MISSION_ALTITUDE,
    clearance=MISSION2_CLEARANCE,
    precision=MISSION2_PRECISION,
    area_width=MISSION2_AREA_WIDTH,
    area_length=MISSION2_AREA_LENGTH,
    fov=MISSION2_FOV,
    overlap=MISSION2_OVERLAP,
    pole1_xy=None,
    pole2_xy=None,
):
    home_lat = current_telemetry["latitude"]
    home_lon = current_telemetry["longitude"]
    if abs(home_lat) < 0.001:
        return None

    rel_pole1 = pole1_xy if pole1_xy is not None else MISSION2_POLE1_REL
    rel_pole2 = pole2_xy if pole2_xy is not None else MISSION2_POLE2_REL

    capsule_raw = [
        generate_capsule_waypoint(rel_pole1, rel_pole2, clearance, i / max(1, precision - 1))
        for i in range(precision)
    ]
    capsule_points = [(x, y) for (x, y, seg) in capsule_raw if seg < 2]

    scan_pts = generate_scan(
        area_width=area_width,
        area_length=area_length,
        altitude=altitude,
        fov_deg=fov,
        overlap=overlap,
        n_line=10,
        n_turn=4,
    )

    x1, y1 = rel_pole1
    x2, y2 = rel_pole2
    center_x = (x1 + x2) / 2.0
    center_y = (y1 + y2) / 2.0

    scan_pts = [(-x, y) for (x, y) in scan_pts]
    scan_pts = [(x - center_x, y - (center_y + clearance)) for (x, y) in scan_pts]

    vx = x2 - x1
    vy = y2 - y1
    theta = math.atan2(vy, vx)
    scan_pts = rotate_points(scan_pts, theta)

    perp_home = perpendicular_offset(rel_pole1, rel_pole2, (0.0, 0.0), -1, clearance)
    mission_xy = capsule_points + scan_pts + [perp_home]
    mission_latlon = [rel_xy_to_latlon(x, y, home_lat, home_lon) for (x, y) in mission_xy]

    validate_mission_points(mission_latlon)
    return build_mission_items_from_latlon(mission_latlon, altitude)

def build_vision_status_message():
    age = 0.0
    if vision_state["last_seen"] > 0:
        age = max(0.0, time.time() - vision_state["last_seen"])
    return {
        "type": "VISION_STATUS",
        "enabled": vision_state["enabled"],
        "target_active": vision_state["target_active"],
        "target_dx": vision_state["target_dx"],
        "target_dy": vision_state["target_dy"],
        "last_seen": vision_state["last_seen"],
        "age_sec": age,
        "source": vision_state["source"],
        "host": vision_state["host"],
        "port": vision_state["port"],
    }

async def send_json_safe(client, payload):
    try:
        await client.send(json.dumps(payload))
        return True
    except Exception:
        return False

async def send_vision_status(client=None):
    payload = build_vision_status_message()
    if client is not None:
        await send_json_safe(client, payload)
        return

    for ws in list(connected_clients):
        ok = await send_json_safe(ws, payload)
        if not ok:
            connected_clients.discard(ws)

def mission_items_to_preview_points(items):
    preview = []
    for item in items:
        # Hanya tampilkan waypoint navigasi misi (param2 > 0),
        # jangan ikut titik home/takeoff agar tidak terlihat garis lurus berbahaya.
        if int(item.get('command', 0)) != 16:
            continue
        if float(item.get('param2', 0.0)) <= 0.0:
            continue

        lat = item.get('x', 0) / 1e7
        lon = item.get('y', 0) / 1e7
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            preview.append({
                "seq": int(item.get('seq', len(preview))),
                "command": int(item.get('command', 0)),
                "lat": lat,
                "lon": lon,
            })
    return preview

async def send_mission_preview(client, mission_name, items):
    points = mission_items_to_preview_points(items)
    await send_json_safe(client, {
        "type": "MISSION_PREVIEW",
        "mission": mission_name,
        "count": len(points),
        "points": points,
    })

class VisionUdpProtocol(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        message = data.decode(errors="ignore").strip()
        if not vision_state["enabled"] or not message.startswith("TARGET:"):
            return

        try:
            coords = message.replace("TARGET:", "").strip()
            dx_raw, dy_raw = coords.split(",")
            dx = float(dx_raw)
            dy = float(dy_raw)
        except Exception:
            return

        vision_state["target_dx"] = max(-1.0, min(1.0, dx))
        vision_state["target_dy"] = max(-1.0, min(1.0, dy))
        vision_state["target_active"] = True
        vision_state["last_seen"] = time.time()

async def start_vision_udp_listener():
    global vision_udp_transport
    import socket as _socket
    loop = asyncio.get_running_loop()
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    sock.bind((VISION_UDP_HOST, VISION_UDP_PORT))
    transport, _ = await loop.create_datagram_endpoint(
        VisionUdpProtocol,
        sock=sock,
    )
    vision_udp_transport = transport
    print(f"[VISION] UDP listener aktif di {VISION_UDP_HOST}:{VISION_UDP_PORT}")

async def vision_state_loop():
    tick = 0
    while True:
        if vision_state["enabled"] and vision_state["last_seen"] > 0:
            if time.time() - vision_state["last_seen"] > 1.5:
                vision_state["target_active"] = False
        else:
            vision_state["target_active"] = False

        current_telemetry["vision_enabled"] = vision_state["enabled"]
        current_telemetry["vision_target_active"] = vision_state["target_active"]
        current_telemetry["vision_target_dx"] = vision_state["target_dx"]
        current_telemetry["vision_target_dy"] = vision_state["target_dy"]
        current_telemetry["vision_last_seen"] = vision_state["last_seen"]

        tick += 1
        if connected_clients and tick % 5 == 0:
            await send_vision_status()
        await asyncio.sleep(0.2)

async def generate_figure8_mission(
    altitude=MISSION_ALTITUDE,
    clearance=MISSION2_CLEARANCE,
    precision=MISSION2_PRECISION,
    pole1_xy=MISSION2_POLE1_REL,
    pole2_xy=MISSION2_POLE2_REL,
):
    home_lat = current_telemetry["latitude"]
    home_lon = current_telemetry["longitude"]
    if abs(home_lat) < 0.001:
        print("!!! GPS belum fix !!!")
        return None

    mission_xy = generate_figure8_around_poles(
        pole1_xy,
        pole2_xy,
        clearance,
        precision,
    )
    mission_xy = reorder_figure8_start_near_drone(mission_xy, pole1_xy, pole2_xy, clearance)
    mission_latlon = [rel_xy_to_latlon(x, y, home_lat, home_lon) for (x, y) in mission_xy]
    validate_mission_points(mission_latlon)
    items = build_mission_items_from_latlon(mission_latlon, altitude)

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

            elif mtype == 'TOGGLE_VISION':
                vision_state["enabled"] = bool(data.get('state', False))
                if not vision_state["enabled"]:
                    vision_state["target_active"] = False
                    vision_state["target_dx"] = 0.0
                    vision_state["target_dy"] = 0.0
                await send_vision_status(websocket)

            elif mtype == 'REQ_VISION_STATUS':
                await send_vision_status(websocket)

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
                params = data.get('params', {}) if isinstance(data.get('params', {}), dict) else {}
                altitude = parse_float(params.get('altitude'), MISSION_ALTITUDE, 5.0, 120.0)
                clearance = parse_float(params.get('clearance'), MISSION2_CLEARANCE, 5.0, 200.0)
                precision = parse_int(params.get('precision'), MISSION2_PRECISION, 8, 200)
                home_lat = current_telemetry["latitude"]
                home_lon = current_telemetry["longitude"]
                pole1_xy, pole2_xy, pole_src = resolve_runtime_poles_xy(home_lat, home_lon, params)
                print(f"[MISSION] Figure8 poles source: {pole_src} | p1={pole1_xy} p2={pole2_xy}")
                items = await generate_figure8_mission(
                    altitude=altitude,
                    clearance=clearance,
                    precision=precision,
                    pole1_xy=pole1_xy,
                    pole2_xy=pole2_xy,
                )
                if not items:
                    await websocket.send(json.dumps({
                        "type": "MISSION_UPLOAD_STATUS", "success": False,
                        "message": "GPS belum fix atau posisi tidak valid"
                    }))
                    continue

                try:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, upload_mission_mavlink, items)
                    await send_mission_preview(websocket, "figure8", items)

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

            elif mtype == 'UPLOAD_MISSION_2':
                params = data.get('params', {}) if isinstance(data.get('params', {}), dict) else {}
                altitude = parse_float(params.get('altitude'), MISSION_ALTITUDE, 5.0, 120.0)
                clearance = parse_float(params.get('clearance'), MISSION2_CLEARANCE, 5.0, 200.0)
                precision = parse_int(params.get('precision'), MISSION2_PRECISION, 8, 200)
                area_width = parse_float(params.get('area_width'), MISSION2_AREA_WIDTH, 10.0, 500.0)
                area_length = parse_float(params.get('area_length'), MISSION2_AREA_LENGTH, 10.0, 500.0)
                fov = parse_float(params.get('fov'), MISSION2_FOV, 20.0, 170.0)
                overlap = parse_float(params.get('overlap'), MISSION2_OVERLAP, 0.0, 0.95)
                home_lat_m2 = current_telemetry["latitude"]
                home_lon_m2 = current_telemetry["longitude"]
                p1_xy_m2, p2_xy_m2, pole_src_m2 = resolve_runtime_poles_xy(home_lat_m2, home_lon_m2, params)
                print(f"[MISSION2] Pole source: {pole_src_m2}, p1={p1_xy_m2}, p2={p2_xy_m2}")
                items = await generate_mission2_capsule_scan(
                    altitude=altitude,
                    clearance=clearance,
                    precision=precision,
                    area_width=area_width,
                    area_length=area_length,
                    fov=fov,
                    overlap=overlap,
                    pole1_xy=p1_xy_m2,
                    pole2_xy=p2_xy_m2,
                )
                if not items:
                    await websocket.send(json.dumps({
                        "type": "MISSION_UPLOAD_STATUS", "success": False,
                        "message": "Mission 2 gagal dibuat: GPS belum fix"
                    }))
                    continue

                try:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, upload_mission_mavlink, items)
                    await send_mission_preview(websocket, "mission2", items)
                    await websocket.send(json.dumps({
                        "type": "MISSION_UPLOAD_STATUS", "success": True,
                        "message": f"Mission 2 upload sukses ({len(items)} waypoints)."
                    }))
                except Exception as e:
                    print(f"!!! UPLOAD MISSION 2 GAGAL: {e} !!!")
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

async def drone_connect_and_setup():
    """Background task: connect to drone then start telemetry streams. Never blocks the WS server."""
    print("[DRONE] Mencoba koneksi ke drone...")
    while True:
        try:
            await drone_system.connect(system_address=CONNECTION_STRING)
            break
        except:
            await asyncio.sleep(1)

    print("[DRONE] Waiting for Heartbeat...")
    async for state in drone_system.core.connection_state():
        if state.is_connected:
            print(">>> DRONE CONNECTED")
            break

    await setup_ardupilot_sitl()
    await start_telemetry()
    print("[DRONE] Telemetry streams started.")

async def main():
    global drone_system
    drone_system = System()
    print(f"--- CAKSA GCS BACKEND ---")

    loop = asyncio.get_running_loop()
    threading.Thread(target=voice_worker, args=(loop,), daemon=True).start()

    await start_vision_udp_listener()
    server = await websockets.serve(websocket_handler, "0.0.0.0", WS_PORT)
    print(f">>> WebSocket server aktif di ws://0.0.0.0:{WS_PORT}")

    try:
        await asyncio.gather(
            drone_connect_and_setup(),
            broadcast_loop(),
            voice_command_processor(),
            vision_state_loop(),
            server.wait_closed(),
        )
    finally:
        server.close()
        if vision_udp_transport:
            vision_udp_transport.close()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: print("Backend Dimatikan.")
    except Exception as e: print(f"Server Exited: {e}")