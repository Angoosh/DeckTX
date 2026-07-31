import sys
import time
import struct
import threading
import json
import os
import uuid
import pygame
import serial
import cv2  
import glob

APP_VERSION = "1.1.0"

# --- CRSF CONFIG & PROTOCOL PARAMETERS ---
RATE_HZ = 150
BAUDRATE = 115200
SERIAL_PORT = "/dev/ttyUSB0"
serial_state = False

CRSF_ADDRESS_FLIGHT_CONTROLLER = 0xC8  
CRSF_ADDRESS_RADIO_TRANSMITTER = 0xEA  
CRSF_FRAMETYPE_LINK_STATISTICS = 0x14
CRSF_FRAMETYPE_RC_CHANNELS_PACKED = 0x16
CRSF_FRAMETYPE_BATTERY_SENSOR = 0x08
CRSF_FRAMETYPE_GPS = 0x02
CRSF_FRAMETYPE_FLIGHT_MODE = 0x21
CRC8_POLY = 0xD5

running = True
current_page = "CONTROL"  

# --- SCAN SERIAL PORTS ---
serial_ports = glob.glob('/dev/ttyUSB*')
if serial_ports:
    SERIAL_PORT = serial_ports[0]

telemetry_data = {
    "rssi": 0, "lq": 0, "snr": 0, "uplink_power": 0, 
    "v_bat": 0.0, "current": 0.0, "capacity": 0,
    "gps_lat": 0.0, "gps_lon": 0.0, "gps_sats": 0, "gps_speed": 0.0,
    "flight_mode": "UNKNOWN", "packets_received": 0, "last_model_time": 0.0, "last_packet_time": 0.0
}

channels = [1500] * 16
channel_trims = [0] * 16
channel_reversed = [False] * 16
channel_max_deflection = [100] * 16

def generate_id(): return str(uuid.uuid4())[:8]

# --- DECOUPLED LOGICAL KEYBOARD CONTROLS ARRAY ---
custom_keys = [
    {"id": generate_id(), "name": "ARM_LATCH", "key": pygame.K_f, "mode": "TOGGLE", "step": 20, "target": None, "state": False, "val": 1000},
    {"id": generate_id(), "name": "VIRTUAL_1", "key": 0, "mode": "NORMAL", "step": 20, "target": None, "state": False, "val": 1500}
]

# --- ADVANCED CHANNEL MIXING MATRIX ---
custom_mixes = [
    {"id": generate_id(), "name": "CUSTOM_MIX_1", "inputs": [{"src": "STATIC_CENTER", "op": "ADD", "weight": 100}]}
]

last_key_states = {}
channel_sources = ["STATIC_CENTER"] * 16
channel_sources[0] = "JOYSTICK_0_0"
channel_sources[1] = "JOYSTICK_0_1"
channel_sources[2] = "JOYSTICK_0_2"
channel_sources[3] = "JOYSTICK_0_3"

# --- UI STATE VARIABLES ---
page_scroll_y = 0
active_dropdown = None   # {"rect": box, "options": [(id, label)], "type": str, "ref": dict, "scroll": 0, "selected_idx": 0}
active_textbox = None    

all_bindable_keys = [0, pygame.K_SPACE, pygame.K_RETURN, pygame.K_LSHIFT, pygame.K_LCTRL, pygame.K_LALT, pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT, pygame.K_a, pygame.K_b, pygame.K_c, pygame.K_d, pygame.K_e, pygame.K_f, pygame.K_g, pygame.K_h, pygame.K_i, pygame.K_j, pygame.K_k, pygame.K_l, pygame.K_m, pygame.K_n, pygame.K_o, pygame.K_p, pygame.K_q, pygame.K_r, pygame.K_s, pygame.K_t, pygame.K_u, pygame.K_v, pygame.K_w, pygame.K_x, pygame.K_y, pygame.K_z, pygame.K_0, pygame.K_1, pygame.K_2, pygame.K_3, pygame.K_4, pygame.K_5]
all_available_modes = ["NORMAL", "TOGGLE", "INCREASE", "DECREASE", "INC_TARGET", "DEC_TARGET"]

model_input_text = "MODEL_1"
model_input_active = False
model_files_list = []
status_message = "Ready"
status_message_expiry = 0

fpv_frame_surface = None
fpv_lock = threading.Lock()
show_fpv_telemetry = True          
active_camera_index = 0             
camera_switch_requested = False     
available_cameras = [0, 1, 2, 3]    

def get_all_sources():
    srcs = [
        ("STATIC_CENTER", "Center (1500)"), ("STATIC_LOW", "Low (1000)"),
        ("JOYSTICK_0_0", "Joy 0 Axis 0"), ("JOYSTICK_0_1", "Joy 0 Axis 1"), ("JOYSTICK_0_2", "Joy 0 Axis 2"), ("JOYSTICK_0_3", "Joy 0 Axis 3"), ("JOYSTICK_0_4", "Joy 0 Axis 4"), ("JOYSTICK_0_5", "Joy 0 Axis 5"),
        ("JOYSTICK_1_0", "Joy 1 Axis 0"), ("JOYSTICK_1_1", "Joy 1 Axis 1"), ("JOYSTICK_1_2", "Joy 1 Axis 2"), ("JOYSTICK_1_3", "Joy 1 Axis 3")
    ]
    for k in custom_keys: srcs.append((f"KEY_{k['id']}", f"Key: {k['name']}"))
    for m in custom_mixes: srcs.append((f"MIX_{m['id']}", f"Mix: {m['name']}"))
    return srcs

def get_source_label(src_id):
    for id_val, lbl in get_all_sources():
        if id_val == src_id: return lbl
    return src_id

# --- PERSISTENT APP CONFIG (AUTO-LOAD SUPPORT) ---
def save_app_config(last_model_filename):
    try:
        with open("app_config.json", "w") as f:
            json.dump({"last_model": last_model_filename}, f)
    except Exception: pass

def load_app_config():
    try:
        if os.path.exists("app_config.json"):
            with open("app_config.json", "r") as f:
                return json.load(f).get("last_model")
    except Exception: pass
    return None

def update_available_models_list():
    global model_files_list
    try:
        model_files_list = [f for f in os.listdir(".") if f.endswith(".json") and f.startswith("model_")]
        model_files_list.sort()
    except Exception: pass

def save_model_to_json(name):
    global status_message, status_message_expiry
    clean_name = "".join([c for c in name if c.isalnum() or c in ("_", "-")]).strip()
    if not clean_name: return
    filename = f"model_{clean_name}.json"
    payload = {
        "trims": channel_trims, "reversed": channel_reversed, "deflections": channel_max_deflection,
        "sources": channel_sources, "custom_keys": custom_keys, "custom_mixes": custom_mixes
    }
    try:
        with open(filename, "w") as f: json.dump(payload, f, indent=4)
        status_message = f"Saved: {filename}"
        save_app_config(filename)
        update_available_models_list()
    except Exception as e: status_message = f"Save Error: {str(e)}"
    status_message_expiry = time.time() + 3

def load_model_from_json(filename):
    global channel_sources, channel_trims, channel_reversed, channel_max_deflection, custom_keys, custom_mixes, status_message, status_message_expiry, model_input_text
    if not filename or not os.path.exists(filename): return
    try:
        with open(filename, "r") as f: data = json.load(f)
        channel_trims = data.get("trims", [0]*16)
        channel_reversed = data.get("reversed", [False]*16)
        channel_max_deflection = data.get("deflections", [100]*16)
        channel_sources = data.get("sources", ["STATIC_CENTER"]*16)
        custom_keys = data.get("custom_keys", custom_keys)
        custom_mixes = data.get("custom_mixes", custom_mixes)
        
        model_input_text = filename.replace("model_", "").replace(".json", "")
        status_message = f"Loaded Profile: {filename}"
        save_app_config(filename)
    except Exception as e: status_message = f"Load Error: {str(e)}"
    status_message_expiry = time.time() + 3

update_available_models_list()

def crc8_calc(payload):
    crc = 0
    for byte in payload: crc = crc8_table[crc ^ byte]
    return crc

crc8_table = [0] * 256
for i in range(256):
    crc = i
    for _ in range(8): crc = ((crc << 1) ^ CRC8_POLY) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    crc8_table[i] = crc

def pack_crsf_channels(source_channels):
    buffer = bytearray(22)
    bit_bucket, bits_in_bucket, byte_idx = 0, 0, 0
    for ch in source_channels:
        val = int(172 + (ch - 1000) * (1811 - 172) / 1000)
        val = max(172, min(1811, val))
        bit_bucket |= (val & 0x7FF) << bits_in_bucket
        bits_in_bucket += 11
        while bits_in_bucket >= 8:
            buffer[byte_idx] = bit_bucket & 0xFF
            bit_bucket >>= 8
            bits_in_bucket -= 8
            byte_idx += 1
    return buffer

def serial_worker():
    global telemetry_data, running, serial_state
    try: ser = None #serial.Serial(SERIAL_PORT, BAUDRATE, timeout=0.01)
    except Exception: ser = None
    while running:
        if ser is None or not ser.is_open:
            try:
                serial_ports = glob.glob('/dev/ttyUSB*')
                if serial_ports:
                    SERIAL_PORT = serial_ports[0]
                ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=0.01)
                serial_state = True
                status_message = f"Connected to CRSF module on {SERIAL_PORT}"
            except Exception:
                ser = None
                serial_state = False
                time.sleep(1.0)  # Wait 1 second before trying to reconnect again
                continue
        start_time = time.time()
        #if ser and ser.is_open:
        try:
            packet = bytearray([CRSF_ADDRESS_FLIGHT_CONTROLLER, 24, CRSF_FRAMETYPE_RC_CHANNELS_PACKED]) + pack_crsf_channels(channels)
            packet.append(crc8_calc(packet[2:]))
            try: ser.write(packet)
            except Exception: pass
            if ser.in_waiting > 0:
                try:
                    telemetry_data["last_packet_time"] = time.time()
                    header = ser.read(1)
                    if header and header[0] == CRSF_ADDRESS_RADIO_TRANSMITTER:
                        len_byte = ser.read(1)
                        if len_byte:
                            frame_len = len_byte[0]
                            frame_data = ser.read(frame_len)
                            if len(frame_data) == frame_len and crc8_calc(frame_data[:-1]) == frame_data[-1]:
                                telemetry_data["packets_received"] += 1
                                f_type = frame_data[0]
                                payload = frame_data[1:-1]
                                if f_type in [CRSF_FRAMETYPE_BATTERY_SENSOR, CRSF_FRAMETYPE_GPS, CRSF_FRAMETYPE_FLIGHT_MODE]:
                                    telemetry_data["last_model_time"] = time.time()
                                if f_type == CRSF_FRAMETYPE_LINK_STATISTICS:
                                    telemetry_data["rssi"] = -1 * payload[0]
                                    telemetry_data["lq"] = payload[2]
                                    telemetry_data["snr"] = int(struct.unpack('b', bytes([payload[3]]))[0])
                                    telemetry_data["uplink_power"] = payload[7]
                                    if telemetry_data["lq"] > 0:
                                        telemetry_data["last_model_time"] = time.time()
                                elif f_type == CRSF_FRAMETYPE_BATTERY_SENSOR:
                                    v, curr, caph, capl, percent = struct.unpack(">HHHBB", payload[0:8])
                                    telemetry_data["v_bat"] = v / 10.0
                                    telemetry_data["current"] = curr / 10.0
                                    telemetry_data["capacity"] = (caph << 8) + capl
                                    telemetry_data["percent"] = percent
                                elif f_type == CRSF_FRAMETYPE_GPS:
                                    lat, lon, speed = struct.unpack(">iii", payload[0:12])
                                    telemetry_data["gps_lat"] = lat / 10000000.0
                                    telemetry_data["gps_lon"] = lon / 10000000.0
                                    telemetry_data["gps_speed"] = speed / 36.0
                                    telemetry_data["gps_sats"] = payload[14]
                                elif f_type == CRSF_FRAMETYPE_FLIGHT_MODE:
                                    end_idx = payload.find(b'\x00')
                                    telemetry_data["flight_mode"] = payload[:end_idx].decode('ascii', errors='ignore') if end_idx != -1 else payload.decode('ascii', errors='ignore')
                except Exception: pass

        except Exception:
            # If a write or read fails abruptly, drop connection handle and trigger reconnect sequence
            try:
                if ser: ser.close()
            except Exception: pass
            ser = None
            time.sleep(0.5)

        sleep_dur = (1 / float(RATE_HZ)) - (time.time() - start_time)
        if sleep_dur > 0: time.sleep(sleep_dur)

    if ser: 
        try: ser.close()
        except Exception: pass

def video_capture_worker():
    global fpv_frame_surface, running, camera_switch_requested, active_camera_index
    current_hw_idx = active_camera_index
    cap = cv2.VideoCapture(current_hw_idx)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    while running:
        if camera_switch_requested or current_hw_idx != active_camera_index:
            cap.release()
            current_hw_idx = active_camera_index
            cap = cv2.VideoCapture(current_hw_idx)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            camera_switch_requested = False
            with fpv_lock: fpv_frame_surface = None
        ret, frame = cap.read()
        if ret:
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            surface = pygame.surfarray.make_surface(frame_rgb.transpose((1, 0, 2)))
            with fpv_lock: fpv_frame_surface = surface
        else: time.sleep(0.05)
    cap.release()

def draw_navigation_tabs(screen, font):
    global current_page
    tabs = ["CONTROL", "TELEMETRY", "FPV", "MAPPING", "TRIMS", "KEYS", "MIXES", "MODELS", "INFO"]
    for idx, tab_name in enumerate(tabs):
        rect = pygame.Rect(10 + (idx * 140), 15, 130, 35)
        color = (52, 152, 219) if current_page == tab_name else (44, 62, 80)
        pygame.draw.rect(screen, color, rect, border_radius=4)
        txt = font.render(tab_name, True, (255, 255, 255))
        screen.blit(txt, (rect.x + (rect.width - txt.get_width()) // 2, rect.y + 8))

def resolve_source(src_id, connected_joypads):
    if src_id == "STATIC_CENTER": return 1500
    if src_id == "STATIC_LOW": return 1000
    if src_id.startswith("JOYSTICK_"):
        parts = src_id.split("_")
        j_id, a_id = int(parts[1]), int(parts[2])
        if j_id < len(connected_joypads):
            try:
                val = connected_joypads[j_id].get_axis(a_id)
                if a_id in [1, 3]: val = -val
                return int(1500 + val * 500)
            except Exception: pass
        return 1500
    if src_id.startswith("KEY_"):
        uid = src_id[4:]
        k = next((x for x in custom_keys if x["id"] == uid), None)
        return k["val"] if k else 1500
    if src_id.startswith("MIX_"):
        uid = src_id[4:]
        m = next((x for x in custom_mixes if x["id"] == uid), None)
        return m["val"] if m else 1500
    return 1500

def main():
    global channels, running, current_page, channel_trims, channel_reversed, channel_max_deflection, page_scroll_y, active_dropdown, active_textbox, custom_keys, custom_mixes, channel_sources, status_message, show_fpv_telemetry, active_camera_index, camera_switch_requested, model_input_text, model_input_active
    
    pygame.init()
    screen = pygame.display.set_mode((1280, 800))
    #screen = pygame.display.set_mode((1280, 800), pygame.FULLSCREEN)
    pygame.display.set_caption("Steam Deck Universal CRSF Control Link")
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("monospace", 15)
    
    startup_model = load_app_config()
    if startup_model: load_model_from_json(startup_model)
    
    threading.Thread(target=serial_worker, daemon=True).start()
    threading.Thread(target=video_capture_worker, daemon=True).start()
    tab_rects = [pygame.Rect(10 + (i * 140), 15, 130, 35) for i in range(9)]

    while running:
        screen.fill((21, 26, 30))
        mx, my = pygame.mouse.get_pos()
        click_event = False
        keys = pygame.key.get_pressed()

        connected_joypads = []
        for x in range(pygame.joystick.get_count()):
            try:
                j = pygame.joystick.Joystick(x); j.init()
                connected_joypads.append(j)
            except Exception: pass

        for event in pygame.event.get():
            if event.type == pygame.QUIT: running = False

            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE: running = False
                
                elif active_textbox:
                    if event.key == pygame.K_RETURN: active_textbox = None
                    elif event.key == pygame.K_BACKSPACE: 
                        active_textbox["obj"][active_textbox["field"]] = active_textbox["obj"][active_textbox["field"]][:-1]
                    else:
                        if event.unicode.isprintable() and len(active_textbox["obj"][active_textbox["field"]]) < 15:
                            active_textbox["obj"][active_textbox["field"]] += event.unicode
                            
                elif model_input_active:
                    if event.key == pygame.K_RETURN: model_input_active = False
                    elif event.key == pygame.K_BACKSPACE: model_input_text = model_input_text[:-1]
                    elif (event.unicode.isalnum() or event.unicode in ("_", "-")) and len(model_input_text) < 16:
                        model_input_text += event.unicode

                # --- UPGRADED: KEYBOARD ARROW SELECTION AND ENTER CONFIRMATION ---
                elif event.key == pygame.K_UP: 
                    if active_dropdown: 
                        active_dropdown["selected_idx"] = max(0, active_dropdown.get("selected_idx", 0) - 1)
                        if active_dropdown["selected_idx"] < active_dropdown["scroll"]:
                            active_dropdown["scroll"] = active_dropdown["selected_idx"]
                    else: page_scroll_y = max(0, page_scroll_y - 40)
                elif event.key == pygame.K_DOWN:
                    if active_dropdown:
                        active_dropdown["selected_idx"] = min(len(active_dropdown["options"]) - 1, active_dropdown.get("selected_idx", 0) + 1)
                        if active_dropdown["selected_idx"] >= active_dropdown["scroll"] + 8:
                            active_dropdown["scroll"] = active_dropdown["selected_idx"] - 7
                    else: page_scroll_y += 40
                elif event.key == pygame.K_LEFT and not active_dropdown and not active_textbox and not model_input_active:
                    tabs = ["CONTROL", "TELEMETRY", "FPV", "MAPPING", "TRIMS", "KEYS", "MIXES", "MODELS", "INFO"]
                    current_page = tabs[(tabs.index(current_page) - 1) % len(tabs)]
                    page_scroll_y = 0
                elif event.key == pygame.K_RIGHT and not active_dropdown and not active_textbox and not model_input_active:
                    tabs = ["CONTROL", "TELEMETRY", "FPV", "MAPPING", "TRIMS", "KEYS", "MIXES", "MODELS", "INFO"]
                    current_page = tabs[(tabs.index(current_page) + 1) % len(tabs)]
                    page_scroll_y = 0
                elif event.key == pygame.K_RETURN:
                    if active_dropdown:
                        opt_id, opt_lbl = active_dropdown["options"][active_dropdown.get("selected_idx", 0)]
                        d_type = active_dropdown["type"]
                        d_ref = active_dropdown["ref"]
                        if d_type == "MAP_SRC": channel_sources[d_ref["ch"]] = opt_id
                        elif d_type == "KEY_BIND": d_ref["obj"]["key"] = opt_id
                        elif d_type == "KEY_MODE": d_ref["obj"]["mode"] = opt_id
                        elif d_type == "KEY_TGT": d_ref["obj"]["target"] = opt_id
                        elif d_type == "MIX_SRC": d_ref["obj"]["src"] = opt_id
                        elif d_type == "MIX_OP": d_ref["obj"]["op"] = opt_id
                        elif d_type == "FPV_CAM": 
                            active_camera_index = opt_id
                            camera_switch_requested = True
                        elif d_type == "MODEL_LOAD":
                            load_model_from_json(opt_id)
                        active_dropdown = None

            elif event.type == pygame.MOUSEWHEEL:
                if active_dropdown:
                    max_sc = max(0, len(active_dropdown["options"]) - 8)
                    active_dropdown["scroll"] = max(0, min(max_sc, active_dropdown["scroll"] - event.y))
                else:
                    page_scroll_y = max(0, page_scroll_y - event.y * 50)

            elif event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 1: click_event = True
                elif event.button == 4:
                    if active_dropdown: active_dropdown["scroll"] = max(0, active_dropdown["scroll"] - 1)
                    else: page_scroll_y = max(0, page_scroll_y - 50)
                elif event.button == 5:
                    if active_dropdown:
                        max_sc = max(0, len(active_dropdown["options"]) - 8)
                        active_dropdown["scroll"] = min(max_sc, active_dropdown["scroll"] + 1)
                    else: page_scroll_y += 50

        # Modal Priority Processing via Mouse
        if click_event and active_dropdown:
            dx, dy, dw = active_dropdown["rect"].x, active_dropdown["rect"].bottom, active_dropdown["rect"].width
            for i in range(8):
                opt_idx = active_dropdown["scroll"] + i
                if opt_idx >= len(active_dropdown["options"]): break
                r = pygame.Rect(dx, dy + (i * 26), dw, 26)
                if r.collidepoint(mx, my):
                    opt_id, opt_lbl = active_dropdown["options"][opt_idx]
                    d_type = active_dropdown["type"]
                    d_ref = active_dropdown["ref"]
                    
                    if d_type == "MAP_SRC": channel_sources[d_ref["ch"]] = opt_id
                    elif d_type == "KEY_BIND": d_ref["obj"]["key"] = opt_id
                    elif d_type == "KEY_MODE": d_ref["obj"]["mode"] = opt_id
                    elif d_type == "KEY_TGT": d_ref["obj"]["target"] = opt_id
                    elif d_type == "MIX_SRC": d_ref["obj"]["src"] = opt_id
                    elif d_type == "MIX_OP": d_ref["obj"]["op"] = opt_id
                    elif d_type == "FPV_CAM": 
                        active_camera_index = opt_id
                        camera_switch_requested = True
                    elif d_type == "MODEL_LOAD":
                        load_model_from_json(opt_id)
                        
                    active_dropdown = None
                    click_event = False
                    break
            
            if click_event: 
                active_dropdown = None
                click_event = False

        if click_event and my < 65:
            for i, rect in enumerate(tab_rects):
                if rect.collidepoint(mx, my):
                    current_page = ["CONTROL", "TELEMETRY", "FPV", "MAPPING", "TRIMS", "KEYS", "MIXES", "MODELS", "INFO"][i]
                    page_scroll_y = 0
                    active_textbox = None
                    model_input_active = False
            click_event = False 

        # --- CORE ENGINE: Edge Detected Keys Processing ---
        for item in custom_keys:
            if item["key"] != 0:
                just_pressed = keys[item["key"]] and not last_key_states.get(item["key"], False)
                if item["mode"] == "NORMAL":
                    item["val"] = 2000 if keys[item["key"]] else 1000
                elif item["mode"] == "TOGGLE":
                    if just_pressed: item["state"] = not item["state"]
                    item["val"] = 2000 if item["state"] else 1000
                elif item["mode"] == "INCREASE" and just_pressed:
                    item["val"] = min(2000, item["val"] + item["step"])
                elif item["mode"] == "DECREASE" and just_pressed:
                    item["val"] = max(1000, item["val"] - item["step"])
                elif item["mode"] == "INC_TARGET" and just_pressed:
                    tgt = next((k for k in custom_keys if k["id"] == item["target"]), None)
                    if tgt: tgt["val"] = min(2000, tgt["val"] + item["step"])
                elif item["mode"] == "DEC_TARGET" and just_pressed:
                    tgt = next((k for k in custom_keys if k["id"] == item["target"]), None)
                    if tgt: tgt["val"] = max(1000, tgt["val"] - item["step"])

        for k_code in range(len(keys)): last_key_states[k_code] = True if keys[k_code] else False

        # --- CORE ENGINE: Multi-Input Summing Mixers ---
        for m in custom_mixes:
            total_delta = 0.0
            for inp in m["inputs"]:
                val = resolve_source(inp["src"], connected_joypads)
                delta = (val - 1500) * (inp["weight"] / 100.0)
                if inp["op"] == "ADD": total_delta += delta
                elif inp["op"] == "SUBTRACT": total_delta -= delta
            m["val"] = max(1000, min(2000, 1500 + int(total_delta)))

        # --- CORE ENGINE: Final Channel Output Formatting ---
        for ch in range(16):
            base_val = resolve_source(channel_sources[ch], connected_joypads)
            if channel_reversed[ch]: base_val = 3000 - base_val
            scaled_delta = (base_val - 1500) * (channel_max_deflection[ch] / 100.0)
            channels[ch] = max(1000, min(2000, int(1500 + scaled_delta) + channel_trims[ch]))

        # --- UI VIEW RENDERING ENGINES ---
        if current_page == "CONTROL":
            y_base = 80 - page_scroll_y
            screen.blit(font.render("--- HARDWARE LIVE DEPLOYMENT FEED ---", True, (149, 165, 166)), (40, y_base))
            for i in range(16):
                col, row = (0, i) if i < 8 else (1, i - 8)
                x_coord, y_coord = (100, y_base + 40 + (row * 40)) if col == 0 else (680, y_base + 40 + (row * 40))
                txt = f"CH {i+1:02d}: {channels[i]} us"
                pygame.draw.rect(screen, (34, 41, 47), (x_coord + 160, y_coord + 2, 200, 14))
                pygame.draw.rect(screen, (46, 204, 113), (x_coord + 160, y_coord + 2, int(((channels[i] - 1000) / 1000) * 200), 14))
                screen.blit(font.render(txt, True, (236, 240, 241)), (x_coord, y_coord))

        elif current_page == "TELEMETRY":
            y_base = 80 - page_scroll_y
            screen.blit(font.render(f"--- LINK MONITOR RECEIVER (Frames: {telemetry_data['packets_received']}) ---", True, (52, 152, 219)), (40, y_base))

            is_tx_connected = (time.time() - telemetry_data.get("last_packet_time", 0.0)) < 1.5
            is_model_connected = is_tx_connected and (time.time() - telemetry_data.get("last_model_time", 0.0)) < 2.0

            if not is_tx_connected:
                status_txt = "NO USB DATA (TX MODULE DISCONNECTED)"
                status_color = (231, 76, 60) # Red
            elif not is_model_connected:
                status_txt = "TX CONNECTED | MODEL POWERED OFF (FAILSAFE)"
                status_color = (241, 196, 15) # Yellow/Orange
            else:
                status_txt = "TX CONNECTED | MODEL RF LINK ACTIVE"
                status_color = (46, 204, 113) # Green

            y_base += 40
            pygame.draw.rect(screen, (34, 41, 47), (40, y_base, 540, 32), border_radius=4)
            pygame.draw.rect(screen, status_color, (40, y_base, 540, 32), width=2, border_radius=4)
            screen.blit(font.render(status_txt, True, status_color), (55, y_base + 8))
            y_base += 10

            txt_color = (236, 240, 241) if is_model_connected else (149, 165, 166)
            stats = [
                f"Link Quality (LQ):   {telemetry_data['lq']}%", 
                f"Uplink RSSI:         {telemetry_data['rssi']} dBm",
                f"Signal Noise (SNR):  {telemetry_data['snr']} dB", 
                f"Output TX Power:     {telemetry_data['uplink_power']} mW",
                f"Main Battery Pack:   {telemetry_data['v_bat']:.2f} V", 
                f"Amperage Draw:       {telemetry_data['current']:.1f} A",
                f"Capacity Used:       {telemetry_data['capacity']} mAh",
                f"Flight Mode Flag:    [ {telemetry_data.get('flight_mode', 'UNKNOWN')} ]", 
                f"GPS Locked Sats:     {telemetry_data.get('gps_sats', 0)}",
                f"GPS Coordinates:     Lat {telemetry_data.get('gps_lat', 0.0):.6f}, Lon {telemetry_data.get('gps_lon', 0.0):.6f}", 
                f"Ground Speed Sync:   {telemetry_data.get('gps_speed', 0.0):.1f} km/h"
            ]
            for s in stats: 
                y_base += 40
                screen.blit(font.render(s, True, txt_color), (60, y_base))

        elif current_page == "MAPPING":
            y_base = 80 - page_scroll_y
            screen.blit(font.render("--- CHANNEL SOURCE ALLOCATIONS ---", True, (241, 196, 15)), (40, y_base))
            for ch in range(16):
                y_coord = y_base + 50 + (ch * 45)
                if y_coord < 30 or y_coord > 800: continue
                
                screen.blit(font.render(f"CH {ch+1:02d} Matrix:", True, (236, 240, 241)), (60, y_coord))
                box_src = pygame.Rect(280, y_coord - 4, 300, 28)
                box_rev = pygame.Rect(620, y_coord - 4, 160, 28)
                
                pygame.draw.rect(screen, (34, 41, 47), box_src, border_radius=3)
                pygame.draw.rect(screen, (231, 76, 60) if channel_reversed[ch] else (52, 73, 94), box_rev, border_radius=3)
                
                screen.blit(font.render(get_source_label(channel_sources[ch])[:30], True, (255, 255, 255)), (290, y_coord + 1))
                screen.blit(font.render("REVERSE FLAG", True, (255, 255, 255)), (640, y_coord + 1))
                
                if click_event:
                    if box_src.collidepoint(mx, my):
                        active_dropdown = {"type": "MAP_SRC", "ch": ch, "rect": box_src, "options": get_all_sources(), "scroll": 0, "selected_idx": 0, "ref": {"ch": ch}}
                        click_event = False
                    elif box_rev.collidepoint(mx, my):
                        channel_reversed[ch] = not channel_reversed[ch]
                        click_event = False

        elif current_page == "TRIMS":
            y_base = 80 - page_scroll_y
            screen.blit(font.render("--- CHANNEL CALIBRATIONS MATRIX ---", True, (155, 89, 182)), (40, y_base))
            for ch in range(16):
                y_coord = y_base + 50 + (ch * 45)
                if y_coord < 30 or y_coord > 800: continue
                
                screen.blit(font.render(f"CH {ch+1:02d} | Trim: {channel_trims[ch]:+d}us | Throw: {channel_max_deflection[ch]}%", True, (236, 240, 241)), (60, y_coord))
                
                b_dec_t, b_inc_t = pygame.Rect(480, y_coord - 4, 45, 28), pygame.Rect(535, y_coord - 4, 45, 28)
                b_dec_d, b_inc_d = pygame.Rect(620, y_coord - 4, 45, 28), pygame.Rect(675, y_coord - 4, 45, 28)
                pygame.draw.rect(screen, (192, 57, 43), b_dec_t, border_radius=3); pygame.draw.rect(screen, (39, 174, 96), b_inc_t, border_radius=3)
                pygame.draw.rect(screen, (211, 84, 0), b_dec_d, border_radius=3); pygame.draw.rect(screen, (41, 128, 185), b_inc_d, border_radius=3)
                
                screen.blit(font.render("T-", True, (255, 255, 255)), (b_dec_t.x + 12, b_dec_t.y + 4)); screen.blit(font.render("T+", True, (255, 255, 255)), (b_inc_t.x + 12, b_inc_t.y + 4))
                screen.blit(font.render("D-", True, (255, 255, 255)), (b_dec_d.x + 12, b_dec_d.y + 4)); screen.blit(font.render("D+", True, (255, 255, 255)), (b_inc_d.x + 12, b_inc_d.y + 4))
                
                if click_event:
                    if b_dec_t.collidepoint(mx, my): channel_trims[ch] = max(-120, channel_trims[ch] - 5)
                    elif b_inc_t.collidepoint(mx, my): channel_trims[ch] = min(120, channel_trims[ch] + 5)
                    elif b_dec_d.collidepoint(mx, my): channel_max_deflection[ch] = max(10, channel_max_deflection[ch] - 10)
                    elif b_inc_d.collidepoint(mx, my): channel_max_deflection[ch] = min(100, channel_max_deflection[ch] + 10)

        elif current_page == "KEYS":
            y_base = 80 - page_scroll_y
            btn_add = pygame.Rect(40, y_base, 180, 30)
            pygame.draw.rect(screen, (39, 174, 96), btn_add, border_radius=3)
            screen.blit(font.render("[+] ADD LOGICAL KEY", True, (255, 255, 255)), (50, y_base + 6))
            if click_event and btn_add.collidepoint(mx, my):
                custom_keys.append({"id": generate_id(), "name": f"VAR_{len(custom_keys)+1}", "key": 0, "mode": "NORMAL", "step": 20, "target": None, "state": False, "val": 1500})
                click_event = False

            key_options = [(k, "NONE" if k == 0 else pygame.key.name(k).upper()) for k in all_bindable_keys]
            mode_options = [(m, m) for m in all_available_modes]
            tgt_options = [(k["id"], k["name"]) for k in custom_keys]

            for i, k_obj in enumerate(custom_keys):
                y_coord = y_base + 50 + (i * 50)
                if y_coord < 30 or y_coord > 800: continue
                
                box_name = pygame.Rect(40, y_coord, 140, 30)
                box_key = pygame.Rect(200, y_coord, 100, 30)
                box_mode = pygame.Rect(320, y_coord, 140, 30)
                box_tgt = pygame.Rect(480, y_coord, 140, 30)
                btn_dec_s = pygame.Rect(640, y_coord, 35, 30)
                btn_inc_s = pygame.Rect(730, y_coord, 35, 30)
                btn_del = pygame.Rect(790, y_coord, 70, 30)

                is_active_text = active_textbox and active_textbox["obj"] is k_obj and active_textbox["field"] == "name"
                pygame.draw.rect(screen, (52, 152, 219) if is_active_text else (34, 41, 47), box_name, border_radius=3)
                pygame.draw.rect(screen, (34, 41, 47), box_key, border_radius=3)
                pygame.draw.rect(screen, (34, 41, 47), box_mode, border_radius=3)
                
                txt_name = k_obj["name"] + ("|" if is_active_text and time.time() % 1 > 0.5 else "")
                screen.blit(font.render(txt_name[:15], True, (255, 255, 255)), (48, y_coord + 7))
                screen.blit(font.render("NONE" if k_obj["key"] == 0 else pygame.key.name(k_obj["key"]).upper(), True, (241, 196, 15)), (210, y_coord + 7))
                screen.blit(font.render(k_obj["mode"], True, (241, 196, 15)), (330, y_coord + 7))
                
                if k_obj["mode"] in ["INC_TARGET", "DEC_TARGET"]:
                    pygame.draw.rect(screen, (34, 41, 47), box_tgt, border_radius=3)
                    tgt_name = next((t["name"] for t in custom_keys if t["id"] == k_obj["target"]), "NONE")
                    screen.blit(font.render(tgt_name[:12], True, (241, 196, 15)), (490, y_coord + 7))
                    if click_event and box_tgt.collidepoint(mx, my):
                        active_dropdown = {"type": "KEY_TGT", "rect": box_tgt, "options": tgt_options, "scroll": 0, "selected_idx": 0, "ref": {"obj": k_obj}}
                        click_event = False

                if k_obj["mode"] in ["INCREASE", "DECREASE", "INC_TARGET", "DEC_TARGET"]:
                    pygame.draw.rect(screen, (44, 62, 80), btn_dec_s, border_radius=3); pygame.draw.rect(screen, (44, 62, 80), btn_inc_s, border_radius=3)
                    screen.blit(font.render("-", True, (255, 255, 255)), (650, y_coord + 7))
                    screen.blit(font.render("+", True, (255, 255, 255)), (740, y_coord + 7))
                    screen.blit(font.render(f"{k_obj['step']}", True, (236, 240, 241)), (685, y_coord + 7))
                    if click_event:
                        if btn_dec_s.collidepoint(mx, my): k_obj["step"] = max(1, k_obj["step"] - 5)
                        elif btn_inc_s.collidepoint(mx, my): k_obj["step"] = min(1000, k_obj["step"] + 5)

                pygame.draw.rect(screen, (192, 57, 43), btn_del, border_radius=3)
                screen.blit(font.render("DEL", True, (255, 255, 255)), (805, y_coord + 7))
                screen.blit(font.render(f"({k_obj['val']})", True, (46, 204, 113)), (880, y_coord + 7))

                if click_event:
                    if box_name.collidepoint(mx, my):
                        active_textbox = {"obj": k_obj, "field": "name"}
                        click_event = False
                    elif box_key.collidepoint(mx, my):
                        active_dropdown = {"type": "KEY_BIND", "rect": box_key, "options": key_options, "scroll": 0, "selected_idx": 0, "ref": {"obj": k_obj}}
                        click_event = False
                    elif box_mode.collidepoint(mx, my):
                        active_dropdown = {"type": "KEY_MODE", "rect": box_mode, "options": mode_options, "scroll": 0, "selected_idx": 0, "ref": {"obj": k_obj}}
                        click_event = False
                    elif btn_del.collidepoint(mx, my):
                        custom_keys.pop(i); click_event = False; break

        elif current_page == "MIXES":
            y_base = 80 - page_scroll_y
            btn_add = pygame.Rect(40, y_base, 160, 30)
            pygame.draw.rect(screen, (39, 174, 96), btn_add, border_radius=3)
            screen.blit(font.render("[+] CREATE MIX", True, (255, 255, 255)), (55, y_base + 6))
            if click_event and btn_add.collidepoint(mx, my):
                custom_mixes.append({"id": generate_id(), "name": f"MIX_{len(custom_mixes)+1}", "inputs": []})
                click_event = False

            y_coord = y_base + 50
            for i, m_obj in enumerate(custom_mixes):
                if y_coord > 800: break
                
                box_name = pygame.Rect(40, y_coord, 180, 30)
                btn_add_inp = pygame.Rect(240, y_coord, 140, 30)
                btn_del_mix = pygame.Rect(400, y_coord, 80, 30)
                
                is_active_text = active_textbox and active_textbox["obj"] is m_obj and active_textbox["field"] == "name"
                pygame.draw.rect(screen, (52, 152, 219) if is_active_text else (34, 41, 47), box_name, border_radius=3)
                pygame.draw.rect(screen, (41, 128, 185), btn_add_inp, border_radius=3)
                pygame.draw.rect(screen, (192, 57, 43), btn_del_mix, border_radius=3)
                
                txt_name = m_obj["name"] + ("|" if is_active_text and time.time() % 1 > 0.5 else "")
                if y_coord > 30:
                    screen.blit(font.render(txt_name[:20], True, (255, 255, 255)), (50, y_coord + 6))
                    screen.blit(font.render("[+] APPEND SRC", True, (255, 255, 255)), (245, y_coord + 6))
                    screen.blit(font.render("DEL MIX", True, (255, 255, 255)), (408, y_coord + 6))
                    screen.blit(font.render(f"OUT: {m_obj.get('val', 1500)}", True, (46, 204, 113)), (510, y_coord + 6))
                
                if click_event:
                    if box_name.collidepoint(mx, my): active_textbox, click_event = {"obj": m_obj, "field": "name"}, False
                    elif btn_add_inp.collidepoint(mx, my): m_obj["inputs"].append({"src": "STATIC_CENTER", "op": "ADD", "weight": 100}); click_event = False
                    elif btn_del_mix.collidepoint(mx, my): custom_mixes.pop(i); click_event = False; break
                
                y_coord += 40
                for j, inp in enumerate(m_obj["inputs"]):
                    if y_coord > 800: break
                    if y_coord > 30:
                        pygame.draw.line(screen, (52, 73, 94), (60, y_coord + 15), (75, y_coord + 15), 2)
                        
                        box_src = pygame.Rect(90, y_coord, 260, 30)
                        box_op = pygame.Rect(370, y_coord, 110, 30)
                        btn_dec_w = pygame.Rect(500, y_coord, 35, 30)
                        btn_inc_w = pygame.Rect(600, y_coord, 35, 30)
                        btn_del_inp = pygame.Rect(650, y_coord, 35, 30)
                        
                        pygame.draw.rect(screen, (34, 41, 47), box_src, border_radius=3)
                        pygame.draw.rect(screen, (34, 41, 47), box_op, border_radius=3)
                        pygame.draw.rect(screen, (44, 62, 80), btn_dec_w, border_radius=3); pygame.draw.rect(screen, (44, 62, 80), btn_inc_w, border_radius=3)
                        pygame.draw.rect(screen, (192, 57, 43), btn_del_inp, border_radius=3)
                        
                        screen.blit(font.render(get_source_label(inp["src"])[:25], True, (241, 196, 15)), (100, y_coord + 6))
                        screen.blit(font.render(inp["op"], True, (241, 196, 15)), (380, y_coord + 6))
                        screen.blit(font.render("-", True, (255, 255, 255)), (512, y_coord + 6)); screen.blit(font.render("+", True, (255, 255, 255)), (612, y_coord + 6))
                        screen.blit(font.render(f"{inp['weight']}%", True, (236, 240, 241)), (545, y_coord + 6))
                        screen.blit(font.render("X", True, (255, 255, 255)), (660, y_coord + 6))
                        
                        if click_event:
                            if box_src.collidepoint(mx, my): active_dropdown, click_event = {"type": "MIX_SRC", "rect": box_src, "options": get_all_sources(), "scroll": 0, "selected_idx": 0, "ref": {"obj": inp}}, False
                            elif box_op.collidepoint(mx, my): active_dropdown, click_event = {"type": "MIX_OP", "rect": box_op, "options": [("ADD","ADD"),("SUBTRACT","SUBTRACT")], "scroll": 0, "selected_idx": 0, "ref": {"obj": inp}}, False
                            elif btn_dec_w.collidepoint(mx, my): inp["weight"] = max(0, inp["weight"] - 5)
                            elif btn_inc_w.collidepoint(mx, my): inp["weight"] = min(1000, inp["weight"] + 5)
                            elif btn_del_inp.collidepoint(mx, my): m_obj["inputs"].pop(j); click_event = False; break
                    y_coord += 36
                y_coord += 10

        elif current_page == "MODELS":
            y_base = 100 - page_scroll_y
            screen.blit(font.render("--- PROFILE PERSISTENCE STORAGE ---", True, (52, 152, 219)), (40, y_base))
            
            box_txt = pygame.Rect(280, y_base + 50, 300, 32)
            btn_s = pygame.Rect(600, y_base + 50, 120, 32)
            pygame.draw.rect(screen, (52, 152, 219) if model_input_active else (34, 41, 47), box_txt, border_radius=3)
            pygame.draw.rect(screen, (39, 174, 96), btn_s, border_radius=3)
            
            txt_model = model_input_text + ("|" if model_input_active and time.time() % 1 > 0.5 else "")
            screen.blit(font.render(txt_model, True, (255, 255, 255)), (295, y_base + 56))
            screen.blit(font.render("Save New Profile Name:", True, (255, 255, 255)), (40, y_base + 56))
            screen.blit(font.render("SAVE", True, (255, 255, 255)), (640, y_base + 56))
            
            if click_event:
                if box_txt.collidepoint(mx, my): 
                    model_input_active = True
                    active_textbox = None
                    click_event = False
                elif btn_s.collidepoint(mx, my): 
                    save_model_to_json(model_input_text)

            y_load = y_base + 120
            box_d = pygame.Rect(280, y_load, 440, 32)
            pygame.draw.rect(screen, (34, 41, 47), box_d, border_radius=3)
            
            lbl = "Select Profile ^"
            screen.blit(font.render(lbl, True, (241, 196, 15)), (295, y_load + 8))
            screen.blit(font.render("Load Profile:", True, (255, 255, 255)), (40, y_load + 8))
            
            if click_event and box_d.collidepoint(mx, my) and model_files_list:
                opts = [(f, f) for f in model_files_list]
                active_dropdown, click_event = {"type": "MODEL_LOAD", "rect": box_d, "options": opts, "scroll": 0, "selected_idx": 0, "ref": None}, False

            screen.blit(font.render(f">> {status_message}", True, (241, 196, 15)), (40, y_base + 200))

        elif current_page == "INFO":
            y_base = 80 - page_scroll_y
            
            screen.blit(font.render(f"BUILD VERSION: {APP_VERSION}", True, (241, 196, 15)), (40, y_base))
            y_base += 40
            
            if serial_state:
                screen.blit(font.render(f"SERIAL PORT: {SERIAL_PORT}", True, (46, 204, 113)), (40, y_base))
                y_base += 20
                screen.blit(font.render(f"BAUDRATE: {BAUDRATE}", True, (46, 204, 113)), (40, y_base))
                y_base += 40
            else:
                screen.blit(font.render(f"SERIAL PORT: {SERIAL_PORT} DISCONNECTED", True, (231, 76, 60)), (40, y_base))
                y_base += 20
                screen.blit(font.render(f"BAUDRATE: NONE", True, (231, 76, 60)), (40, y_base))
                y_base += 40
            
            screen.blit(font.render("--- HARDWARE GAMEPAD NODE REGISTRY DIRECTORY ---", True, (46, 204, 113)), (40, y_base))
            y_base += 40
            screen.blit(font.render(f"Total Virtual/Physical Controller Handles Found: {len(connected_joypads)}", True, (149, 165, 166)), (40, y_base))
            y_base += 40
            
            if not connected_joypads:
                screen.blit(font.render("NO CONTROLLERS VISIBLE. Verify Steam layout configurations.", True, (231, 76, 60)), (60, y_base))
            else:
                for idx, pad in enumerate(connected_joypads):
                    pygame.draw.rect(screen, (34, 41, 47), (40, y_base, 800, 40), border_radius=4)
                    device_text = f"PROGRAM ID HARDWARE INDEX [Joy {idx}]  --> Device Name: {pad.get_name()}"
                    screen.blit(font.render(device_text, True, (236, 240, 241)), (60, y_base + 12))
                    y_base += 50

        elif current_page == "FPV":
            video_rect = pygame.Rect(240, 100, 800, 600)
            with fpv_lock: current_frame = fpv_frame_surface
            if current_frame is not None: screen.blit(pygame.transform.scale(current_frame, (800, 600)), (240, 100))
            else: pygame.draw.rect(screen, (34, 41, 47), video_rect)
            
            box_check = pygame.Rect(40, 100, 20, 20)
            pygame.draw.rect(screen, (52, 73, 94), box_check, border_radius=3)
            if show_fpv_telemetry: pygame.draw.rect(screen, (46, 204, 113), (44, 104, 12, 12), border_radius=2)
            if click_event and box_check.collidepoint(mx, my): show_fpv_telemetry = not show_fpv_telemetry
            screen.blit(font.render("Overlay Telemetry", True, (255, 255, 255)), (70, 100))
            
            if show_fpv_telemetry:
                pygame.draw.rect(screen, (10, 15, 20, 180), (250, 110, 220, 80), border_radius=4)
                screen.blit(font.render(f"LQ: {telemetry_data['lq']}%", True, (46, 204, 113)), (260, 120))
                screen.blit(font.render(f"Voltage: {telemetry_data['v_bat']:.2f} V", True, (241, 196, 15)), (260, 145))
                screen.blit(font.render(f"RSSI: {telemetry_data['rssi']} dBm", True, (46, 204, 113)), (260, 170))

            box_cam = pygame.Rect(1060, 100, 180, 28)
            pygame.draw.rect(screen, (34, 41, 47), box_cam, border_radius=3)
            screen.blit(font.render(f"/dev/video{active_camera_index}", True, (255, 255, 255)), (1070, 105))
            if click_event and box_cam.collidepoint(mx, my):
                opts = [(i, f"/dev/video{i}") for i in available_cameras]
                active_dropdown, click_event = {"type": "FPV_CAM", "rect": box_cam, "options": opts, "scroll": 0, "selected_idx": 0, "ref": None}, False

        pygame.draw.rect(screen, (21, 26, 30), (0, 0, 1280, 65))
        pygame.draw.line(screen, (44, 62, 80), (0, 65), (1280, 65), 2)
        draw_navigation_tabs(screen, font)

        if active_dropdown:
            dx, dy, dw = active_dropdown["rect"].x, active_dropdown["rect"].bottom, active_dropdown["rect"].width
            vis_opts = min(8, len(active_dropdown["options"]))
            pygame.draw.rect(screen, (28, 35, 40), (dx, dy, dw, vis_opts * 28), border_radius=4)
            pygame.draw.rect(screen, (52, 152, 219), (dx, dy, dw, vis_opts * 28), width=1, border_radius=4)
            for i in range(vis_opts):
                opt_idx = active_dropdown["scroll"] + i
                if opt_idx >= len(active_dropdown["options"]): break
                opt_id, opt_lbl = active_dropdown["options"][opt_idx]
                r = pygame.Rect(dx, dy + (i * 28), dw, 28)
                
                # Active mouse hovering overtakes arrow key navigation target cleanly
                if r.collidepoint(mx, my):
                    active_dropdown["selected_idx"] = opt_idx
                    if click_event:
                        d_type = active_dropdown["type"]
                        d_ref = active_dropdown["ref"]
                        if d_type == "MAP_SRC": channel_sources[d_ref["ch"]] = opt_id
                        elif d_type == "KEY_BIND": d_ref["obj"]["key"] = opt_id
                        elif d_type == "KEY_MODE": d_ref["obj"]["mode"] = opt_id
                        elif d_type == "KEY_TGT": d_ref["obj"]["target"] = opt_id
                        elif d_type == "MIX_SRC": d_ref["obj"]["src"] = opt_id
                        elif d_type == "MIX_OP": d_ref["obj"]["op"] = opt_id
                        elif d_type == "FPV_CAM": active_camera_index = opt_id; camera_switch_requested = True
                        elif d_type == "MODEL_LOAD": load_model_from_json(opt_id)
                        active_dropdown, click_event = None, False
                        break
                
                # Highlight active selected row parameter
                if opt_idx == active_dropdown.get("selected_idx", 0):
                    pygame.draw.rect(screen, (44, 62, 80), r)
                    
                screen.blit(font.render(opt_lbl[:30], True, (236, 240, 241)), (dx + 10, r.y + 6))
            if click_event: active_dropdown = None

        pygame.display.flip()
        clock.tick(RATE_HZ)

    running = False
    pygame.quit()
    sys.exit()

if __name__ == "__main__":
    main()
