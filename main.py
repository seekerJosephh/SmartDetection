
import cv2
import numpy as np
import time
import json
import threading
import winsound
import pyodbc
import subprocess
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import quote
from isapi_client import ISAPIClient, LPREvent


# ==================== CONFIGURATION ====================
USERNAME = 'admin'
PASSWORD = 'SCWS@adm'
PASSWORD_ENCODED = quote(PASSWORD, safe='')
ip = '172.16.197.120'
CAMERAS = [
    {"name": "CAM1", "ip": "172.16.197.120"},
    # {"name": "CAM2", "ip": ""},
    # {"name": "CAM3", "ip": ""},
    # {"name": "CAM4", "ip": ""},
]

ISAPI_USERNAME = USERNAME
ISAPI_PASSWORD = PASSWORD

RTSP_URL_TEMPLATE = f'rtsp://{USERNAME}:{PASSWORD_ENCODED}@{ip}:554/Streaming/Channels/101?transport=udp'

DISPLAY_LAYOUT      = (2, 2)
WINDOW_NAME         = 'Multi Hikvision Label Monitor'
TARGET_DISPLAY_SIZE = (960, 540)

# ── Detection thresholds ──
WHITE_THRESHOLD        = 0.60     # White Detection  ~ 95% pass
YELLOW_THRESHOLD       = 0.50     # yellow detection ~ 50% Pass
BASE_AVERAGE_FRAMES    = 5
MAX_AVERAGE_FRAMES     = 30

# ── Pallet detection ──
PALLET_AREA_THRESHOLD  = 600      # min fg pixels to count as pallet present prev: 600
PALLET_STABLE_FRAMES   = 5        # consecutive frames before "pallet confirmed"
PALLET_FIT_RATIO       = 0.40     # fg must cover >55% of zone area for zone→green
PALLET_GONE_FRAMES     = 15       # consecutive empty frames before "pallet gone"

# ── Timing (seconds) ──
DELAY_BEFORE_INSPECT   = 3.0      # wait after pallet confirmed → then inspect
PASS_DISPLAY_DURATION  = 8.0      # show green result for 4s then reset
COOLDOWN_AFTER_PASS    = 1.0      # short gap before watching for next pallet
RECONNECT_TIMEOUT      = 4.0      # seconds without frame → reconnect


# ==================== WINDOW LOCK (PowerShell) ====================
CS_APP_NAME = "ADAccount"   #  process name WITHOUT .exe

def _set_csapp_window(enabled: bool):

    enable_str = "$true" if enabled else "$false"
    script = (
        'Add-Type @"\n'
        'using System.Runtime.InteropServices;\n'
        'public class Win32 {\n'
        '    [DllImport("user32.dll")]\n'
        '    public static extern bool EnableWindow(System.IntPtr h, bool e);\n'
        '}\n'
        '"@\n'
        '$proc = Get-Process -Name "' + CS_APP_NAME + '" -ErrorAction SilentlyContinue\n'
        'if ($proc -and $proc.MainWindowHandle -ne 0) {\n'
        '    [Win32]::EnableWindow($proc.MainWindowHandle, ' + enable_str + ')\n'
        '}'
    )
    subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-Command", script],
        creationflags=subprocess.CREATE_NO_WINDOW
    )

def lock_csapp():
    print("[LOCK] C# app locked")
    _set_csapp_window(False)

def unlock_csapp():
    print("[UNLOCK] C# app unlocked")
    _set_csapp_window(True)




# ── Color cycle ──
COLOR_CYCLE = [
    ("White",  (255,255,255), {"h_tol":80, "s_max":100, "v_min":100}),
    ("Yellow", (0,255,255),   {"h_range":(20,40),  "s_min":100, "v_min":100}),
    ("Green",  (0,255,0),     {"h_range":(40,80),  "s_min":80,  "v_min":80 }),
    ("Red",    (0,0,255),     {"h_range":[(0,10),(160,180)], "s_min":100, "v_min":80}),
]
current_color_idx  = 0
TARGET_COLOR_NAME  = COLOR_CYCLE[0][0]
TARGET_CONFIG      = COLOR_CYCLE[0][2]

# ── SQL ──
SQL_SERVER   = r'172.17.148.90\MSSQLTEST'
SQL_DATABASE = 'LabelDB'
SQL_USERNAME = 'SCWS_User'
SQL_PASSWORD = 'SCWS_User00'
SQL_CONN_STR = (
    f'DRIVER={{SQL Server}};SERVER={SQL_SERVER};'
    f'DATABASE={SQL_DATABASE};UID={SQL_USERNAME};PWD={SQL_PASSWORD};'
)
TABLE_NAME = "tblCameraLabel"

# ── Email (optional) ──
SMTP_HOST     = ''
SMTP_PORT     = 587
SMTP_USER     = ''
SMTP_PASSWORD = ''
ALERT_TO      = ''




# ==================== STATE ENUMS ====================
class AutoState:
    IDLE        = "IDLE"         # waiting for pallet
    WAITING     = "WAITING"      # pallet confirmed, 2s countdown
    INSPECTING  = "INSPECTING"   # running detection
    ALARM       = "ALARM"        # missing found, alarm active
    PASS        = "PASS"         # all labels OK, 4s display
    PAUSED      = "PAUSED"       # auto off

# ==================== CAMERA STATE ====================
class CameraState:
    def __init__(self, name, ip):
        self.name  = name
        self.ip    = ip
        self.url   = RTSP_URL_TEMPLATE.format(u=USERNAME, p=PASSWORD, ip=ip)
        self.cap   = None

        # ROI / Zone data
        self.current_rois_relative  = []
        self.pallet_zone_relative   = []
        self.editable_rois_abs      = []
        self.editable_zone_abs      = []

        # Detection results
        self.missing_rois           = None   # list of missing ROI indices
        self.all_roi_scores         = []     # per-ROI score for display

        # Auto detection state machine
        self.auto_state             = AutoState.PAUSED
        self.pallet_detected_frames = 0
        self.pallet_absent_frames   = 0
        self.state_enter_time       = 0.0    # when we entered current state
        self.last_frame_time        = time.time()

        # Background subtractor
        self.bg_subtractor = None

        # Alarm
        self.alarm_active  = False

        # AI Camera integration fields
        self.last_lpr_plate: str = ''
        self.last_lpr_conf: float = 0.0
        self.last_lpr_dir: str = ''
        self.motion_from_cam: bool = False


available_label_types = []
current_label_type    = None
states = [CameraState(c["name"], c["ip"]) for c in CAMERAS]

# ── UI globals ──
adjust_mode        = False
zone_adjust_mode   = False
menu_mode          = False
label_type_menu    = False
color_menu_active  = False
selected_camera_idx = 0
selected_roi        = None
drag_start          = None
drag_type           = None

# ==================== COLOR MASK ====================
def create_mask(hsv, config):
    if 'h_tol' in config:
        s_mask = hsv[:,:,1] < 45
        v_mask = hsv[:,:,2] > 185
        mask   = np.logical_and(s_mask, v_mask).astype(np.uint8) * 255
        bright = (hsv[:,:,2] > 230).astype(np.uint8) * 255
        return cv2.bitwise_or(mask, bright)
    h_ranges = config['h_range'] if isinstance(config['h_range'], list) else [config['h_range']]
    mask = np.zeros(hsv.shape[:2], np.uint8)
    for h_min, h_max in h_ranges:
        lo = np.array([h_min, config.get('s_min',100), config.get('v_min',100)])
        hi = np.array([h_max, 255, 255])
        mask = cv2.bitwise_or(mask, cv2.inRange(hsv, lo, hi))
    return mask

# ==================== HELPERS ====================
def to_abs(relative_rois, w, h):
    return [[int(r[0]*w), int(r[1]*h), int(r[2]*w), int(r[3]*h)]
            for r in relative_rois if len(r)==4]

def db_connect():
    return pyodbc.connect(SQL_CONN_STR, timeout=8)

def send_alert_email(cam_name, label_type, missing_str, missing_count, total):
    if not SMTP_HOST or not ALERT_TO:
        return
    try:
        msg = MIMEMultipart()
        msg['Subject'] = f"[ALARM] Missing Labels — {cam_name} / {label_type}"
        msg['From']    = SMTP_USER
        msg['To']      = ALERT_TO
        body = (f"Camera  : {cam_name}\n"
                f"Label   : {label_type}\n"
                f"Missing : {missing_str} ({missing_count}/{total})\n"
                f"Time    : {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        msg.attach(MIMEText(body, 'plain'))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
        print(f"[{cam_name}] Alert email sent.")
    except Exception as e:
        print(f"[{cam_name}] Email error: {e}")

# ==================== DB OPERATIONS ====================
def load_available_label_types():
    global available_label_types
    try:
        conn   = db_connect()
        cursor = conn.cursor()
        cursor.execute(f"SELECT DISTINCT LabelType FROM {TABLE_NAME} ORDER BY LabelType")
        available_label_types = [r[0] for r in cursor.fetchall()]
        conn.close()
    except Exception as e:
        print("Cannot load label types:", e)

def load_rois_for_label_type(label_type):
    global current_label_type
    current_label_type = label_type
    for state in states:
        try:
            conn   = db_connect()
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT ROI_JSON, PalletZone_JSON FROM {TABLE_NAME} "
                f"WHERE LabelType=? AND CameraID=?", (label_type, state.name))
            row = cursor.fetchone()
            if row:
                state.current_rois_relative = json.loads(row[0]) if row[0] else []
                state.pallet_zone_relative  = json.loads(row[1]) if row[1] else []
            else:
                state.current_rois_relative = []
                state.pallet_zone_relative  = []
            conn.close()
        except Exception as e:
            print(f"[{state.name}] Load error: {e}")

def save_config_to_db(state):
    if not current_label_type:
        return False
    label_json = json.dumps(state.current_rois_relative)
    zone_json  = json.dumps(state.pallet_zone_relative)
    count      = len(state.current_rois_relative)
    try:
        conn   = db_connect()
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE {TABLE_NAME} SET ROI_JSON=?, ExpectedLabels=?, "
            f"PalletZone_JSON=?, LastModified=GETDATE() "
            f"WHERE LabelType=? AND CameraID=?",
            (label_json, count, zone_json, current_label_type, state.name))
        if cursor.rowcount == 0:
            cursor.execute(
                f"INSERT INTO {TABLE_NAME} "
                f"(LabelType,CameraID,ExpectedLabels,ROI_JSON,PalletZone_JSON,CreatedDate,LastModified) "
                f"VALUES (?,?,?,?,?,GETDATE(),GETDATE())",
                (current_label_type, state.name, count, label_json, zone_json))
        conn.commit()
        conn.close()
        print(f"[{state.name}] Saved {count} ROIs + zone.")
        return True
    except Exception as e:
        print(f"[{state.name}] Save error: {e}")
        return False

def log_missing_to_db(state, missing_indices, abs_rois):
    missing_str = ",".join(str(i+1) for i in missing_indices)
    try:
        conn   = db_connect()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC usp_LogAndAlertMissingLabel "
            "@CameraID=?, @LabelType=?, @MissingROIs=?, "
            "@MissingCount=?, @AllROIsCount=?, @TargetColor=?",
            (state.name, current_label_type, missing_str,
             len(missing_indices), len(abs_rois), TARGET_COLOR_NAME))
        row = cursor.fetchone()
        if row and row[0]:
            print(f"[{state.name}] Logged missing ID={int(row[0])}")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[{state.name}] DB log error: {e}")

def log_lpr_to_db(state, evt):
    """Log every AI Plate / text read to tbleLPRlog. Auto-create table """

    try:
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute('''
            IF NOT EXISTS (
                SELECT 1 FROM INFORMATION_SCHEMA.TABLES 
                WHERE TABLE_NAME = 'tblLPRLog'
            CREATE TABLE tblLPRLog (
                LogID            INT IDENTITY PRIMARY KEY,
                CameraID         NVARCHAR(50),
                Plate            NVARCHAR(50),
                Confidence       FLOAT,
                Direction        NVARCHAR(50),
                VehicleType      NVARCHAR(50),
                LabelType        NVARCHAR(50),
                EventTime        DATETIME DEFAULT GETDATE(),        
            )''')
        cursor.execute(
            'INSERT INTO tblLPRLog'
            ' (CameraID, Plate, Confidence, Direction, VehicleType, LabelType)'
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (state.name, evt.plate, evt.confidence, evt.direction, evt.vehicle_type,
             current_label_type or ''))
        cursor.commit()
        conn.close()
    except Exception as e:
        print(f"[{state.name}] DB log error: {e}")



# ==================== CAMERA INIT ====================
def initialize_camera(state):
    try:
        state.cap = cv2.VideoCapture(state.url)
        state.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        ret, _ = state.cap.read()
        if ret:
            state.last_frame_time = time.time()
            print(f"[{state.name}] Connected.")
            return True
        state.cap.release(); state.cap = None
        return False
    except Exception as e:
        print(f"[{state.name}] Init error: {e}"); return False

def reconnect_camera(state):
    if state.cap: state.cap.release(); state.cap = None
    initialize_camera(state)

# ==================== CAMERA THREAD ====================
class CameraThread(threading.Thread):
    def __init__(self, state):
        super().__init__(daemon=True)
        self.state        = state
        self.latest_frame = None
        self.running      = True

    def run(self):
        while self.running:
            if self.state.cap is None:
                reconnect_camera(self.state); time.sleep(1); continue
            ret, frame = self.state.cap.read()
            if ret:
                self.state.last_frame_time = time.time()
                self.latest_frame = cv2.resize(frame, TARGET_DISPLAY_SIZE)
            else:
                if time.time() - self.state.last_frame_time > RECONNECT_TIMEOUT:
                    reconnect_camera(self.state)
            time.sleep(0.005)

# ==================== ALARM THREAD ====================
def alarm_thread_fn():
    while True:
        if any(s.alarm_active for s in states):
            winsound.Beep(1200, 400)
            time.sleep(0.6)
        time.sleep(0.1)

threading.Thread(target=alarm_thread_fn, daemon=True).start()

# ==================== CORE INSPECTION ====================
def run_inspection(state, thread):
    """
    Collect multi-frame color samples, score each ROI,
    return (all_pass: bool, missing_indices: list, scores: list)
    """
    w, h    = TARGET_DISPLAY_SIZE
    abs_rois = to_abs(state.current_rois_relative, w, h)
    if not abs_rois:
        return True, [], []

    n_frames = min(max(BASE_AVERAGE_FRAMES, len(abs_rois)*4), MAX_AVERAGE_FRAMES)
    white_scores  = [0.0] * len(abs_rois)
    yellow_scores = [0.0] * len(abs_rois)
    collected     = 0
    best_frame    = None
    best_total    = -1.0

    for _ in range(n_frames * 3):
        if thread.latest_frame is None:
            time.sleep(0.015); continue
        frame      = thread.latest_frame
        total_this = 0.0
        for i, (x1,y1,x2,y2) in enumerate(abs_rois):
            if x2<=x1 or y2<=y1: continue
            roi = frame[y1:y2, x1:x2]
            if roi.size < 300: continue
            hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
            # white
            wm  = create_mask(hsv, COLOR_CYCLE[0][2])
            wm  = cv2.morphologyEx(wm, cv2.MORPH_OPEN, np.ones((3,3),np.uint8))
            wf  = np.sum(wm>0)/wm.size if wm.size else 0.0
            # yellow
            ym  = create_mask(hsv, COLOR_CYCLE[1][2])
            ym  = cv2.morphologyEx(ym, cv2.MORPH_OPEN, np.ones((3,3),np.uint8))
            yf  = np.sum(ym>0)/ym.size if ym.size else 0.0
            white_scores[i]  += wf
            yellow_scores[i] += yf
            total_this       += max(wf, yf)
        collected += 1
        if total_this > best_total:
            best_total = total_this; best_frame = frame.copy()
        if collected >= n_frames: break
        time.sleep(0.008)

    if collected == 0:
        return False, list(range(len(abs_rois))), [0.0]*len(abs_rois)

    ws = [s/collected for s in white_scores]
    ys = [s/collected for s in yellow_scores]

    missing = []
    scores  = []
    for i, (x1,y1,x2,y2) in enumerate(abs_rois):
        wf = ws[i]; yf = ys[i]
        passed = wf >= WHITE_THRESHOLD or yf >= YELLOW_THRESHOLD
        scores.append(max(wf, yf))
        if not passed:
            missing.append(i)
            print(f"  ROI {i+1:2d}: MISSING  (W:{wf*100:.1f}% Y:{yf*100:.1f}%)")
        else:
            src = "W" if wf >= WHITE_THRESHOLD else "Y"
            print(f"  ROI {i+1:2d}: OK [{src}]  (W:{wf*100:.1f}% Y:{yf*100:.1f}%)")

    return len(missing)==0, missing, scores

# ==================== PALLET ZONE CHECK ====================
def check_pallet_in_zone(state, frame):
    """
    Returns (has_pallet: bool, fit_ratio: float, fg_area: int)
    """
    if not state.pallet_zone_relative or frame is None:
        return False, 0.0, 0

    fgmask = state.bg_subtractor.apply(frame, learningRate=0.01)
    fgmask = cv2.medianBlur(fgmask, 7) # strong Blur
    _, fgmask = cv2.threshold(fgmask, 200, 255, cv2.THRESH_BINARY) # Higher threshold
    fgmask = cv2.morphologyEx(fgmask, cv2.MORPH_OPEN,  np.ones((7,7),np.uint8))
    fgmask = cv2.dilate(      fgmask, np.ones((5,5),np.uint8), iterations=2)

    w, h = TARGET_DISPLAY_SIZE
    zone_mask  = np.zeros(fgmask.shape[:2], np.uint8)
    zone_area  = 0
    for zx1,zy1,zx2,zy2 in to_abs(state.pallet_zone_relative, w, h):
        if zx2>zx1 and zy2>zy1:
            zone_mask[zy1:zy2, zx1:zx2] = 255
            zone_area += (zx2-zx1)*(zy2-zy1)

    fg_in_zone = cv2.bitwise_and(fgmask, zone_mask)
    fg_area    = cv2.countNonZero(fg_in_zone)
    fit_ratio  = fg_area / zone_area if zone_area > 0 else 0.0
    has_pallet = (fg_area > PALLET_AREA_THRESHOLD) and (fit_ratio > PALLET_FIT_RATIO)

    return has_pallet, fit_ratio, fg_area

# ==================== AUTO STATE MACHINE ====================
def update_auto_state(state, thread, now):
    """
    Drive the per-camera state machine one tick.
    Called every frame from the main loop.
    """
    if state.auto_state == AutoState.PAUSED:
        return

    frame      = thread.latest_frame
    has_pallet, fit_ratio, fg_area = check_pallet_in_zone(state, frame)

    # ── IDLE: watching for pallet ──────────────────────────────
    if state.auto_state == AutoState.IDLE:
        cam_sees_motion = state.motion_from_cam
        if has_pallet or cam_sees_motion:
            state.pallet_detected_frames += 1
            state.pallet_absent_frames    = 0
        else:
            state.pallet_detected_frames  = 0
            state.pallet_absent_frames   += 1

        if state.pallet_detected_frames >= PALLET_STABLE_FRAMES:
            print(f"\n[{state.name}] Pallet confirmed → waiting {DELAY_BEFORE_INSPECT}s…")
            state.auto_state             = AutoState.WAITING
            state.state_enter_time       = now
            state.pallet_detected_frames = 0

    # ── WAITING: pallet stable, 2-second delay ─────────────────
    elif state.auto_state == AutoState.WAITING:
        elapsed = now - state.state_enter_time
        # If pallet disappeared during wait → back to IDLE
        if not has_pallet:
            state.pallet_absent_frames += 1
            if state.pallet_absent_frames >= PALLET_GONE_FRAMES:
                print(f"[{state.name}] Pallet left during wait → IDLE")
                state.auto_state           = AutoState.IDLE
                state.pallet_absent_frames = 0
            return
        state.pallet_absent_frames = 0
        if elapsed >= DELAY_BEFORE_INSPECT:
            print(f"[{state.name}] Starting inspection…")
            state.auto_state       = AutoState.INSPECTING
            state.state_enter_time = now
            # Run inspection in a background thread so UI stays live
            threading.Thread(
                target=_inspect_worker, args=(state, thread), daemon=True
            ).start()

    # ── INSPECTING: background thread is running ───────────────
    elif state.auto_state == AutoState.INSPECTING:
        pass   # result posted by _inspect_worker

    # ── ALARM: missing labels, waiting for 'a' ─────────────────
    elif state.auto_state == AutoState.ALARM:
        pass   # cleared by key handler

    # ── PASS: all labels OK, display 4s then reset ─────────────
    elif state.auto_state == AutoState.PASS:
        if now - state.state_enter_time >= PASS_DISPLAY_DURATION:
            print(f"[{state.name}] Pass window done → IDLE")
            state.missing_rois    = None
            state.all_roi_scores  = []
            state.auto_state      = AutoState.IDLE
            state.pallet_detected_frames = 0
            state.pallet_absent_frames   = 0

def _inspect_worker(state, thread):
    all_pass, missing, scores = run_inspection(state, thread)
    state.all_roi_scores = scores

    if not all_pass:
        state.missing_rois     = missing
        state.alarm_active     = True
        state.auto_state       = AutoState.ALARM
        state.state_enter_time = time.time()
        print(f"[{state.name}] !!! ALARM — missing ROIs: {[i+1 for i in missing]}")

        #  Single call, properly guarded
        try:
            lock_csapp()
        except Exception as e:
            print(f"[LOCK] Failed: {e}")

        abs_rois = to_abs(state.current_rois_relative, *TARGET_DISPLAY_SIZE)
        log_missing_to_db(state, missing, abs_rois)
        threading.Thread(
            target=send_alert_email,
            args=(state.name, current_label_type,
                  ",".join(str(i+1) for i in missing),
                  len(missing), len(abs_rois)),
            daemon=True
        ).start()
    else:
        state.missing_rois     = []
        state.alarm_active     = False
        state.auto_state       = AutoState.PASS
        state.state_enter_time = time.time()
        print(f"[{state.name}] ✓ All labels present → PASS")


# ==================== OVERLAY DRAWING ====================
def draw_overlay(display_frame, state, is_selected, now):
    w, h    = TARGET_DISPLAY_SIZE
    abs_rois = to_abs(state.current_rois_relative, w, h)

    # ── Pallet Zone ────────────────────────────────────────────
    if state.pallet_zone_relative:
        for zx1,zy1,zx2,zy2 in to_abs(state.pallet_zone_relative, w, h):
            # Zone turns GREEN when pallet confirmed (WAITING/INSPECTING/PASS/ALARM)
            zone_active = state.auto_state in (
                AutoState.WAITING, AutoState.INSPECTING,
                AutoState.PASS, AutoState.ALARM)
            zone_color = (0,220,0) if zone_active else (255,100,0)
            cv2.rectangle(display_frame, (zx1,zy1), (zx2,zy2), zone_color, 4)
            cv2.putText(display_frame, "Pallet Zone",
                        (zx1+10, zy1+36), cv2.FONT_HERSHEY_SIMPLEX, 0.8, zone_color, 2)

    # ── Label ROIs ─────────────────────────────────────────────
    rois_to_draw = (state.editable_rois_abs
                    if adjust_mode and is_selected else abs_rois)

    for i, (x1,y1,x2,y2) in enumerate(rois_to_draw):
        if x2<=x1 or y2<=y1: continue
        # Determine color
        if adjust_mode and is_selected:
            color = (0,220,255); thick = 2
        elif state.auto_state == AutoState.ALARM and state.missing_rois is not None:
            color = (0,0,255) if i in state.missing_rois else (0,220,0)
            thick = 5
        elif state.auto_state == AutoState.PASS:
            color = (0,220,0); thick = 5
        elif state.auto_state in (AutoState.INSPECTING, AutoState.WAITING):
            # Pulse cyan during inspection
            alpha = abs(np.sin(now * 4))
            color = (int(200*alpha), int(255*alpha), int(255*alpha)); thick = 3
        else:
            color = (180,180,180); thick = 1

        # Draw yellow tint on ROI area
        if state.auto_state not in (AutoState.ALARM, AutoState.PASS):
            roi_img = display_frame[y1:y2, x1:x2]
            if roi_img.size > 0:
                gray   = cv2.cvtColor(roi_img, cv2.COLOR_BGR2GRAY)
                tinted = cv2.merge([np.zeros_like(gray), gray, gray])
                display_frame[y1:y2, x1:x2] = cv2.addWeighted(tinted,0.5,roi_img,0.5,0)

        cv2.rectangle(display_frame, (x1,y1), (x2,y2), color, thick)
        cv2.putText(display_frame, str(i+1),
                    (x1+4, y1+18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

    # ── Status Bar ─────────────────────────────────────────────
    s = state.auto_state
    state_labels = {
        AutoState.IDLE:       ("IDLE  — Watching…",       (160,160,160)),
        AutoState.WAITING:    ("WAITING — Pallet detected, preparing…", (0,220,100)),
        AutoState.INSPECTING: ("INSPECTING…",              (0,200,255)),
        AutoState.ALARM:      ("!!! ALARM — Labels Missing !!!",  (0,0,255)),
        AutoState.PASS:       ("✓  PASS — All Labels OK",  (0,220,0)),
        AutoState.PAUSED:     ("AUTO OFF",                  (120,120,120)),
    }
    label, color = state_labels.get(s, ("UNKNOWN",(200,200,200)))

    # Countdown overlay during WAITING
    if s == AutoState.WAITING:
        remain = max(0.0, DELAY_BEFORE_INSPECT - (now - state.state_enter_time))
        label  = f"PALLET — starting in {remain:.1f}s…"

    # Draw semi-transparent bar at bottom
    bar = display_frame[h-42:h, 0:w].copy()
    cv2.rectangle(bar, (0,0),(w,42),(20,20,20),-1)
    display_frame[h-42:h, 0:w] = cv2.addWeighted(bar,0.65,display_frame[h-42:h,0:w],0.35,0)
    cv2.putText(display_frame, f"{state.name} | {label}",
                (12, h-14), cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2)

    # Missing count badge
    if s == AutoState.ALARM and state.missing_rois:
        badge = f"Missing: {len(state.missing_rois)}/{len(abs_rois)}"
        cv2.putText(display_frame, badge,
                    (w-220, h-14), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0,0,255), 2)

    # Label type tag (top-left)
    if current_label_type:
        cv2.putText(display_frame, f"[{current_label_type}] {TARGET_COLOR_NAME}",
                    (10,28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200,200,200), 2)

        if state.last_lpr_plate:
            plate_txt = state.last_lpr_plate
            conf_txt = f'{state.last_lpr_conf:.0f}%'
            b_color = (30, 169, 60) if state.last_lpr_dir == 'approaching' \
                else (160, 100, 40)
            bw = max(180, len(plate_txt) * 20 + 60)
            bx = w - bw - 8

            overlay = display_frame.copy()
            cv2.rectangle(overlay, (bx, 6), (bx + bw, 68), b_color, -1)
            display_frame = cv2.addWeighted(overlay, 0.72, display_frame, 0.28, 0)
            cv2.putText(display_frame, plate_txt,
                        (bx + 8, 34), cv2.FONT_HERSHEY_DUPLEX,
                        0.9, (255, 255, 255), 2)
            cv2.putText(display_frame,
                        f'{state.last_lpr_dir} conf:{conf_txt}',
                        (bx + 8, 58), cv2.FONT_HERSHEY_SIMPLEX,
                        0.52, (210, 235, 210), 1)
            # Motion dot (top-right dot when camera detects motion)
        if state.motion_from_cam:
            cv2.circle(display_frame, (w - 14, 14), 7, (40, 210, 60), -1)
    return display_frame


# ==================== MOUSE CALLBACK ====================
def get_clicked_cam(mx, my):
    col = mx // TARGET_DISPLAY_SIZE[0]
    row = my // TARGET_DISPLAY_SIZE[1]
    idx = row * DISPLAY_LAYOUT[1] + col
    return idx if 0 <= idx < len(states) else -1

def get_roi_under(x, y, rois, margin=22):
    for i, r in enumerate(rois):
        x1,y1,x2,y2 = r
        if x1<=x<=x2 and y1<=y<=y2:
            edge = (abs(x-x1)<margin or abs(x-x2)<margin or
                    abs(y-y1)<margin or abs(y-y2)<margin)
            return i, 'resize' if edge else 'move'
    return -1, None

def mouse_callback(event, x, y, flags, param):
    global selected_roi, drag_start, drag_type, selected_camera_idx
    if not (adjust_mode or zone_adjust_mode): return
    cidx = get_clicked_cam(x, y)
    if cidx < 0: return
    selected_camera_idx = cidx
    state  = states[cidx]
    rel_x  = x % TARGET_DISPLAY_SIZE[0]
    rel_y  = y % TARGET_DISPLAY_SIZE[1]
    rois   = state.editable_zone_abs if zone_adjust_mode else state.editable_rois_abs

    if event == cv2.EVENT_LBUTTONDOWN:
        idx, typ = get_roi_under(rel_x, rel_y, rois)
        if idx >= 0:
            selected_roi = idx; drag_start = (rel_x,rel_y); drag_type = typ
        else:
            rois.append([rel_x, rel_y, rel_x+10, rel_y+10])
            selected_roi = len(rois)-1; drag_start = (rel_x,rel_y); drag_type = 'new'

    elif event == cv2.EVENT_MOUSEMOVE and drag_start and selected_roi is not None:
        if selected_roi >= len(rois): return
        r  = rois[selected_roi]
        dx = rel_x - drag_start[0]; dy = rel_y - drag_start[1]
        if drag_type == 'new':
            r[2]=rel_x; r[3]=rel_y
        elif drag_type == 'resize':
            if abs(rel_x-r[0])<30: r[0]=rel_x
            if abs(rel_x-r[2])<30: r[2]=rel_x
            if abs(rel_y-r[1])<30: r[1]=rel_y
            if abs(rel_y-r[3])<30: r[3]=rel_y
        elif drag_type == 'move':
            r[0]+=dx; r[1]+=dy; r[2]+=dx; r[3]+=dy
            drag_start=(rel_x,rel_y)
        r[:] = [min(r[0],r[2]),min(r[1],r[3]),max(r[0],r[2]),max(r[1],r[3])]

    elif event == cv2.EVENT_LBUTTONUP:
        drag_start = None; drag_type = None

    elif event == cv2.EVENT_RBUTTONDOWN:
        idx,_ = get_roi_under(rel_x, rel_y, rois)
        if idx >= 0:
            del rois[idx]
            if selected_roi == idx: selected_roi = None
            elif selected_roi and selected_roi > idx: selected_roi -= 1

# ==================== STARTUP ====================
load_available_label_types()
if available_label_types:
    load_rois_for_label_type(available_label_types[0])

for state in states:
    initialize_camera(state)
    state.bg_subtractor = cv2.createBackgroundSubtractorMOG2(
        history=500, varThreshold=60, detectShadows=False)
    # Wire ISAPI Client to this Camera
    def _make_plate_cb(s):
        def _cb(evt: LPREvent):
            print(f'[{s.name}] PLATED: {evt.plate} conf={evt.confidence:.0f}%'
                  f' dir={evt.direction} type={evt.vehicle_type}')
            s.last_lpr_plate = evt.plate
            s.last_lpr_conf = evt.confidence
            s.last_lpr_dir = evt.direction
            log_lpr_to_db(s, evt)

            if (s.auto_state == AutoState.IDLE
                and evt.direction.lower() == 'approaching'):
                print(f'[{s.name}] AI trigger -> WAITING')
                s.auto_state = AutoState.WAITING
                s.state_enter_time = time.time()
                s.pallet_detected_frames = PALLET_STABLE_FRAMES
        return _cb

    def _make_motion_cb(s):
        def _cb(active: bool):
            s.motion_from_cam = active
        return _cb

    client = ISAPIClient(
        ip = state.ip,
        username = USERNAME,
        password = PASSWORD,
        on_plate=_make_plate_cb(state),
        on_motion=_make_motion_cb(state),
    )
    client.start()
    state._isapi = client



threads = [CameraThread(s) for s in states]
for t in threads:
    t.start()

cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
cv2.resizeWindow(WINDOW_NAME, 1920, 1080)
cv2.setMouseCallback(WINDOW_NAME, mouse_callback)

print("\n=== Multi-Camera Label Monitor ===")
print("  p   → Toggle AUTO detection ON/OFF")
print("  m   → Menu")
print("  r   → Adjust Label ROIs")
print("  z   → Adjust Pallet Zone")
print("  s   → Save config")
print("  c   → Manual check now")
print("  a   → Reset alarm")
print("  t   → Cycle detection color")
print("  l   → Switch label type")
print("  q   → Quit\n")

# ==================== MAIN LOOP ====================
while True:
    now    = time.time()
    rows_n = DISPLAY_LAYOUT[0]
    cols_n = DISPLAY_LAYOUT[1]
    canvas = np.zeros((TARGET_DISPLAY_SIZE[1]*rows_n,
                        TARGET_DISPLAY_SIZE[0]*cols_n, 3), np.uint8)

    for idx, state in enumerate(states):
        thread = threads[idx]
        row    = idx // cols_n
        col    = idx %  cols_n
        y0     = row * TARGET_DISPLAY_SIZE[1]
        x0     = col * TARGET_DISPLAY_SIZE[0]

        # ── Get frame ──
        if thread.latest_frame is None or now - state.last_frame_time > RECONNECT_TIMEOUT:
            blank = np.zeros((*TARGET_DISPLAY_SIZE[::-1], 3), np.uint8)
            cv2.putText(blank, f"{state.name} — NO SIGNAL",
                        (80,270), cv2.FONT_HERSHEY_SIMPLEX, 1.4,(0,0,200),3)
            display_frame = blank
        else:
            display_frame = thread.latest_frame.copy()

        # ── Tick state machine ──
        update_auto_state(state, thread, now)

        # ── Draw overlay ──
        display_frame = draw_overlay(
            display_frame, state,
            is_selected=(idx == selected_camera_idx), now=now)

        canvas[y0:y0+TARGET_DISPLAY_SIZE[1], x0:x0+TARGET_DISPLAY_SIZE[0]] = display_frame

    # ── Global alarm banner ──
    if any(s.alarm_active for s in states):
        ch, cw = canvas.shape[:2]
        bar    = canvas[ch-80:ch, 0:cw].copy()
        cv2.rectangle(bar,(0,0),(cw,80),(0,0,160),-1)
        canvas[ch-80:ch, 0:cw] = cv2.addWeighted(bar,0.7,canvas[ch-80:ch,0:cw],0.3,0)
        cv2.putText(canvas, "!!! ALARM — MISSING LABELS — Press 'a' to reset !!!",
                    (60, ch-25), cv2.FONT_HERSHEY_SIMPLEX, 1.4,(0,60,255),4)

    # ── Adjust-mode footer ──
    if adjust_mode or zone_adjust_mode:
        ch, cw = canvas.shape[:2]
        mode   = "ZONE ADJUST" if zone_adjust_mode else "ROI ADJUST"
        cv2.rectangle(canvas,(0,ch-52),(cw,ch),(20,20,50),-1)
        cv2.putText(canvas,
                    f"{mode} — {states[selected_camera_idx].name}  "
                    f"| LMB=draw  RMB=delete  S=save  ESC=cancel",
                    (20, ch-16), cv2.FONT_HERSHEY_SIMPLEX, 0.75,(0,230,230),2)

    # ── Menu overlay ──
    if menu_mode:
        ch,cw = canvas.shape[:2]
        ov    = canvas.copy()
        cv2.rectangle(ov,(cw//2-320,ch//2-280),(cw//2+320,ch//2+280),(25,25,55),-1)
        canvas = cv2.addWeighted(ov,0.82,canvas,0.18,0)
        cv2.putText(canvas,"MENU",(cw//2-60,ch//2-210),
                    cv2.FONT_HERSHEY_DUPLEX,2.0,(0,230,230),4)
        items = ["p - AUTO detection ON/OFF","r - Adjust Label ROIs",
                 "z - Adjust Pallet Zone","l - Switch Label Type",
                 "t - Cycle Color","c - Manual Check","a - Reset Alarm","q - Quit"]
        for i,txt in enumerate(items):
            cv2.putText(canvas, txt,(cw//2-260,ch//2-130+i*52),
                        cv2.FONT_HERSHEY_SIMPLEX,0.95,(240,240,240),2)

    cv2.imshow(WINDOW_NAME, canvas)
    key = cv2.waitKey(1) & 0xFF

    # ── Key handlers ──────────────────────────────────────────
    if key == ord('q'):
        break

    elif key == ord('m'):
        menu_mode = not menu_mode
        adjust_mode = zone_adjust_mode = False

    elif key == ord('p'):
        # Toggle AUTO for all cameras
        any_active = any(s.auto_state != AutoState.PAUSED for s in states)
        for s in states:
            if any_active:
                s.auto_state  = AutoState.PAUSED
                s.alarm_active = False
                s.missing_rois = None
            else:
                s.auto_state             = AutoState.IDLE
                s.pallet_detected_frames = 0
                s.pallet_absent_frames   = 0
        mode = "OFF" if any_active else "ON"
        print(f"Auto detection: {mode}")

    elif key == ord('a'):
        # Reset alarm — return to IDLE
        for s in states:
            s.alarm_active = False
            s.missing_rois = None
            s.all_roi_scores = []
            if s.auto_state == AutoState.ALARM:
                s.auto_state             = AutoState.IDLE
                s.pallet_detected_frames = 0
                s.pallet_absent_frames   = 0
        try:
            unlock_csapp()
        except Exception as e:
            print(f"[UNLOCK] Failed to unlock C# app: {e}")
        print("Alarm reset.")

    elif key == ord('r') and (menu_mode or not zone_adjust_mode):
        menu_mode = False; adjust_mode = True; zone_adjust_mode = False
        for s in states:
            s.editable_rois_abs = to_abs(s.current_rois_relative, *TARGET_DISPLAY_SIZE)

    elif key == ord('z') and (menu_mode or not adjust_mode):
        menu_mode = False; zone_adjust_mode = True; adjust_mode = False
        for s in states:
            s.editable_zone_abs = to_abs(s.pallet_zone_relative, *TARGET_DISPLAY_SIZE)

    elif key == ord('s') and (adjust_mode or zone_adjust_mode):
        w, h  = TARGET_DISPLAY_SIZE
        state = states[selected_camera_idx]
        if adjust_mode:
            state.current_rois_relative = [
                [round(x/w,4),round(y/h,4),round(x2/w,4),round(y2/h,4)]
                for x,y,x2,y2 in state.editable_rois_abs if x2>x and y2>y]
        if zone_adjust_mode:
            state.pallet_zone_relative = [
                [round(x/w,4),round(y/h,4),round(x2/w,4),round(y2/h,4)]
                for x,y,x2,y2 in state.editable_zone_abs if x2>x and y2>y]
        save_config_to_db(state)
        adjust_mode = zone_adjust_mode = False

    elif key == 27:   # ESC
        adjust_mode = zone_adjust_mode = False

    elif key == ord('c') and current_label_type and not adjust_mode:
        print("\n=== MANUAL CHECK ===")
        for i, s in enumerate(states):
            if threads[i].latest_frame is not None:
                threading.Thread(
                    target=_inspect_worker,
                    args=(s, threads[i]),
                    daemon=True
                ).start()
    elif key == ord('t'):
        current_color_idx = (current_color_idx+1) % len(COLOR_CYCLE)
        TARGET_COLOR_NAME = COLOR_CYCLE[current_color_idx][0]
        TARGET_CONFIG     = COLOR_CYCLE[current_color_idx][2]
        print(f"Detection color: {TARGET_COLOR_NAME}")

    elif key == ord('l'):
        load_available_label_types()
        if available_label_types:
            cur = available_label_types.index(current_label_type) \
                  if current_label_type in available_label_types else -1
            nxt = (cur+1) % len(available_label_types)
            load_rois_for_label_type(available_label_types[nxt])
            print(f"Label type: {current_label_type}")

# ── Cleanup ──────────────────────────────────────────────
for t in threads:
    t.running = False
# Stop ISAPI clients ← ADD THIS BLOCK
for s in states:
    if hasattr(s, '_isapi'):
        s._isapi.stop()
for t in threads: # existing — keep
    t.join(timeout=2)
for s in states: # existing — keep
    if s.cap: s.cap.release()
cv2.destroyAllWindows() # existing — keep
print('Terminated.') # existing — keep
