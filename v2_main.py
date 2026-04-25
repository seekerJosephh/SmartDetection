import cv2
import json
import numpy as np
import pyodbc
import smtplib
import subprocess
import threading
import time
import winsound
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote

from isapi_client import ISAPIClient, LPREvent

# =====================================================================
#  CONFIGURATION
# =====================================================================
USERNAME         = 'admin'
PASSWORD         = 'SCWS@adm'
PASSWORD_ENCODED = quote(PASSWORD, safe='')

ip = ''

# CAMERAS list is now populated dynamically from the database.
CAMERAS = []

RTSP_URL_TEMPLATE = (
    'rtsp://{u}:{p}@{ip}:554/Streaming/Channels/101?transport=udp'
)

# =========== Configuration Display layout size ========================
DISPLAY_LAYOUT      = (1, 1)
WINDOW_NAME         = 'Multi Hikvision Label Monitor'
TARGET_DISPLAY_SIZE = (1920, 1080)

# ── Detection thresholds ──────────────────────────────────────────────
WHITE_THRESHOLD     = 0.60
YELLOW_THRESHOLD    = 0.50
BASE_AVERAGE_FRAMES = 5
MAX_AVERAGE_FRAMES  = 30

# ── Pallet detection ──────────────────────────────────────────────────
BGS_HISTORY            = 120
BGS_VAR_THRESHOLD      = 60
DELTA_AREA_THRESHOLD   = 800
PALLET_AREA_THRESHOLD  = 850
PALLET_STABLE_FRAMES   = 4
PALLET_GONE_FRAMES     = 15
PALLET_FIT_RATIO       = 0.55

# ── Timing ───────────────────────────────────────────────────────────
DELAY_BEFORE_INSPECT  = 3.0
PASS_DISPLAY_DURATION = 10.0
COOLDOWN_DURATION     = 4.0
RECONNECT_TIMEOUT     = 4.0

# ── SQL ──────────────────────────────────────────────────────────────
SQL_SERVER   = r'172.17.148.90\MSSQLTEST'
SQL_DATABASE = 'LabelDB'
SQL_USERNAME = 'SCWS_User'
SQL_PASSWORD = 'SCWS_User00'
SQL_CONN_STR = (
    f'DRIVER={{SQL Server}};SERVER={SQL_SERVER};'
    f'DATABASE={SQL_DATABASE};UID={SQL_USERNAME};PWD={SQL_PASSWORD};'
)
TABLE_NAME        = 'tblCameraLabel'
CAMERAS_TABLE     = 'tblCameras'

# ── Email ─────────────────────────────────────────────────────────────
SMTP_HOST     = ''
SMTP_PORT     = 587
SMTP_USER     = ''
SMTP_PASSWORD = ''
ALERT_TO      = ''

# ── C# companion app ─────────────────────────────────────────────────
CS_APP_NAME = 'ADAccount'

# ── Color cycle ──────────────────────────────────────────────────────
COLOR_CYCLE = [
    ("White",  (255,255,255), {"h_tol":80, "s_max":100, "v_min":100}),
    ("Yellow", (0,255,255),   {"h_range":(20,40),  "s_min":100, "v_min":100}),
    ("Green",  (0,255,0),     {"h_range":(40,80),  "s_min":80,  "v_min":80}),
    ("Red",    (0,0,255),     {"h_range":[(0,10),(160,180)], "s_min":100, "v_min":80}),
]
current_color_idx = 0
TARGET_COLOR_NAME = COLOR_CYCLE[0][0]
TARGET_CONFIG     = COLOR_CYCLE[0][2]


# =====================================================================
#  STATE ENUM
# =====================================================================
class S:
    PAUSED     = "PAUSED"
    IDLE       = "IDLE"
    WAITING    = "WAITING"
    INSPECTING = "INSPECTING"
    ALARM      = "ALARM"
    PASS       = "PASS"
    COOLDOWN   = "COOLDOWN"


# =====================================================================
#  WINDOW LOCK
# =====================================================================
def _set_csapp_window(enabled: bool):
    flag   = "$true" if enabled else "$false"
    script = (
        'Add-Type @"\nusing System.Runtime.InteropServices;\n'
        'public class Win32 {\n'
        '    [DllImport("user32.dll")]\n'
        '    public static extern bool EnableWindow(System.IntPtr h, bool e);\n'
        '}\n"@\n'
        f'$proc = Get-Process -Name "{CS_APP_NAME}" -ErrorAction SilentlyContinue\n'
        f'if ($proc -and $proc.MainWindowHandle -ne 0) {{\n'
        f'    [Win32]::EnableWindow($proc.MainWindowHandle, {flag})\n'
        f'}}'
    )
    subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-Command", script],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

def lock_csapp():
    print("[LOCK] C# app locked")
    _set_csapp_window(False)

def unlock_csapp():
    print("[UNLOCK] C# app unlocked")
    _set_csapp_window(True)


# =====================================================================
#  COLOR MASK
# =====================================================================
def create_mask(hsv, config):
    if 'h_tol' in config:
        s_mask = hsv[:,:,1] < 45
        v_mask = hsv[:,:,2] > 185
        mask   = np.logical_and(s_mask, v_mask).astype(np.uint8) * 255
        bright = (hsv[:,:,2] > 230).astype(np.uint8) * 255
        return cv2.bitwise_or(mask, bright)
    h_ranges = (config['h_range'] if isinstance(config['h_range'], list)
                else [config['h_range']])
    mask = np.zeros(hsv.shape[:2], np.uint8)
    for h_min, h_max in h_ranges:
        lo   = np.array([h_min, config.get('s_min',100), config.get('v_min',100)])
        hi   = np.array([h_max, 255, 255])
        mask = cv2.bitwise_or(mask, cv2.inRange(hsv, lo, hi))
    return mask


# =====================================================================
#  HELPERS
# =====================================================================
def to_abs(relative_rois, w, h):
    return [[int(r[0]*w), int(r[1]*h), int(r[2]*w), int(r[3]*h)]
            for r in relative_rois if len(r) == 4]

def to_rel(abs_rois, w, h):
    return [[round(x/w,4), round(y/h,4), round(x2/w,4), round(y2/h,4)]
            for x,y,x2,y2 in abs_rois if x2>x and y2>y]

def db_connect():
    return pyodbc.connect(SQL_CONN_STR, timeout=8)


# =====================================================================
#  CAMERA REGISTRY — load from DB
# =====================================================================
def ensure_cameras_table():
    """
    Creates tblCameras if it doesn't exist yet.
    Call once at startup before load_cameras_from_db().
    """
    ddl = f"""
    IF NOT EXISTS (
        SELECT 1 FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_NAME = '{CAMERAS_TABLE}'
    )
    CREATE TABLE {CAMERAS_TABLE} (
        CameraID    INT IDENTITY(1,1) PRIMARY KEY,
        Name        NVARCHAR(50)  NOT NULL UNIQUE,  -- e.g. 'CAM1'
        IPAddress   NVARCHAR(64)  NOT NULL,          -- e.g. '192.168.1.64'
        Description NVARCHAR(255) NULL,
        IsActive    BIT           NOT NULL DEFAULT 1,
        CreatedDate DATETIME      NOT NULL DEFAULT GETDATE(),
        UpdatedDate DATETIME      NOT NULL DEFAULT GETDATE()
    )
    """
    try:
        conn = db_connect()
        cur  = conn.cursor()
        cur.execute(ddl)
        conn.commit()
        conn.close()
        print(f'[DB] {CAMERAS_TABLE} ready.')
    except Exception as e:
        print(f'[DB] ensure_cameras_table error: {e}')


def load_cameras_from_db():
    """
    Reads all active rows from tblCameras and populates the global
    CAMERAS list.  Each entry is a dict: {"name": ..., "ip": ...}.
    Returns the number of cameras loaded.
    """
    global CAMERAS
    try:
        conn = db_connect()
        cur  = conn.cursor()
        cur.execute(
            f"SELECT Name, IPAddress FROM {CAMERAS_TABLE} "
            "WHERE IsActive = 1 ORDER BY CameraID"
        )
        rows = cur.fetchall()
        conn.close()

        if not rows:
            print(f'[DB] No active cameras found in {CAMERAS_TABLE}. '
                  'Add rows and restart.')
            return 0

        CAMERAS = [{"name": row[0], "ip": row[1]} for row in rows]
        print(f'[DB] Loaded {len(CAMERAS)} camera(s) from database:')
        for c in CAMERAS:
            print(f'       {c["name"]}  →  {c["ip"]}')
        return len(CAMERAS)

    except Exception as e:
        print(f'[DB] load_cameras_from_db error: {e}')
        return 0


def reload_cameras_from_db():
    """
    Hot-reload: re-reads tblCameras at runtime.
    Prints a warning — a full restart is needed to apply changes,
    because camera threads have already been started.
    """
    old = {c["name"]: c["ip"] for c in CAMERAS}
    load_cameras_from_db()
    new = {c["name"]: c["ip"] for c in CAMERAS}
    changed = {n: new[n] for n in new if old.get(n) != new[n]}
    added   = [n for n in new if n not in old]
    removed = [n for n in old if n not in new]
    if changed or added or removed:
        print('[DB] Camera list changed — RESTART required to apply:')
        for n in added:   print(f'  + ADDED   {n}  {new[n]}')
        for n in removed: print(f'  - REMOVED {n}')
        for n, v in changed.items(): print(f'  ~ CHANGED {n}  {old[n]} → {v}')
    else:
        print('[DB] Camera list unchanged.')


# =====================================================================
#  CAMERA STATE
# =====================================================================
class CameraState:
    def __init__(self, name, cam_ip):
        self.name = name
        self.ip   = cam_ip
        url_ip    = cam_ip if cam_ip else ip          # fallback to global `ip`
        self.url  = RTSP_URL_TEMPLATE.format(
            u=USERNAME, p=PASSWORD_ENCODED, ip=url_ip)
        self.cap  = None

        self.current_rois_relative = []
        self.pallet_zone_relative  = []
        self.editable_rois_abs     = []
        self.editable_zone_abs     = []

        self.missing_rois    = None
        self.all_roi_scores  = []

        self.auto_state             = S.PAUSED
        self.state_enter_time       = 0.0
        self.pallet_detected_frames = 0
        self.pallet_absent_frames   = 0
        self.inspect_running        = False

        self.bg_subtractor = None
        self.prev_gray     = None

        self.alarm_active = False
        self.last_frame_time = time.time()

        self.last_lpr_plate = ''
        self.last_lpr_conf  = 0.0
        self.last_lpr_dir   = ''
        self.motion_from_cam = False
        self._isapi: ISAPIClient = None

    def enter_state(self, new_state):
        print(f'[{self.name}] {self.auto_state} → {new_state}')
        self.auto_state       = new_state
        self.state_enter_time = time.time()

    def elapsed(self):
        return time.time() - self.state_enter_time


# =====================================================================
#  DB OPERATIONS
# =====================================================================
available_label_types = []
current_label_type    = None


def load_available_label_types():
    global available_label_types
    try:
        conn   = db_connect()
        cur    = conn.cursor()
        cur.execute(f"SELECT DISTINCT LabelType FROM {TABLE_NAME} ORDER BY LabelType")
        available_label_types = [r[0] for r in cur.fetchall()]
        conn.close()
    except Exception as e:
        print("Cannot load label types:", e)


def load_rois_for_label_type(label_type):
    global current_label_type
    current_label_type = label_type
    for st in states:
        try:
            conn = db_connect()
            cur  = conn.cursor()
            cur.execute(
                f"SELECT ROI_JSON, PalletZone_JSON FROM {TABLE_NAME} "
                "WHERE LabelType=? AND CameraID=?", (label_type, st.name))
            row = cur.fetchone()
            if row:
                st.current_rois_relative = json.loads(row[0]) if row[0] else []
                st.pallet_zone_relative  = json.loads(row[1]) if row[1] else []
            else:
                st.current_rois_relative = []
                st.pallet_zone_relative  = []
            conn.close()
        except Exception as e:
            print(f'[{st.name}] Load error: {e}')


def save_config_to_db(st):
    if not current_label_type:
        return False
    label_json = json.dumps(st.current_rois_relative)
    zone_json  = json.dumps(st.pallet_zone_relative)
    count      = len(st.current_rois_relative)
    try:
        conn = db_connect()
        cur  = conn.cursor()
        cur.execute(
            f"UPDATE {TABLE_NAME} SET ROI_JSON=?, ExpectedLabels=?, "
            "PalletZone_JSON=?, LastModified=GETDATE() "
            "WHERE LabelType=? AND CameraID=?",
            (label_json, count, zone_json, current_label_type, st.name))
        if cur.rowcount == 0:
            cur.execute(
                f"INSERT INTO {TABLE_NAME} "
                "(LabelType,CameraID,ExpectedLabels,ROI_JSON,PalletZone_JSON,"
                "CreatedDate,LastModified) VALUES (?,?,?,?,?,GETDATE(),GETDATE())",
                (current_label_type, st.name, count, label_json, zone_json))
        conn.commit()
        conn.close()
        print(f'[{st.name}] Saved {count} ROIs + zone.')
        return True
    except Exception as e:
        print(f'[{st.name}] Save error: {e}')
        return False


def log_missing_to_db(st, missing_indices, abs_rois):
    missing_str = ','.join(str(i+1) for i in missing_indices)
    try:
        conn = db_connect()
        cur  = conn.cursor()
        cur.execute(
            "EXEC usp_LogAndAlertMissingLabel "
            "@CameraID=?, @LabelType=?, @MissingROIs=?, "
            "@MissingCount=?, @AllROIsCount=?, @TargetColor=?",
            (st.name, current_label_type, missing_str,
             len(missing_indices), len(abs_rois), TARGET_COLOR_NAME))
        row = cur.fetchone()
        if row and row[0]:
            print(f'[{st.name}] Logged missing ID={int(row[0])}')
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'[{st.name}] DB log error: {e}')


def log_lpr_to_db(st, evt: LPREvent):
    try:
        conn = db_connect()
        cur  = conn.cursor()
        cur.execute("""
            IF NOT EXISTS (
                SELECT 1 FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_NAME = 'tblLPRLog'
            )
            CREATE TABLE tblLPRLog (
                LogID        INT IDENTITY PRIMARY KEY,
                CameraID     NVARCHAR(50),
                Plate        NVARCHAR(50),
                Confidence   FLOAT,
                Direction    NVARCHAR(50),
                VehicleType  NVARCHAR(50),
                LabelType    NVARCHAR(50),
                EventTime    DATETIME DEFAULT GETDATE()
            )
        """)
        cur.execute(
            "INSERT INTO tblLPRLog"
            " (CameraID, Plate, Confidence, Direction, VehicleType, LabelType)"
            " VALUES (?,?,?,?,?,?)",
            (st.name, evt.plate, evt.confidence, evt.direction,
             evt.vehicle_type, current_label_type or ''))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'[{st.name}] LPR DB log error: {e}')


def send_alert_email(cam_name, label_type, missing_str, missing_count, total):
    if not SMTP_HOST or not ALERT_TO:
        return
    try:
        msg            = MIMEMultipart()
        msg['Subject'] = f'[ALARM] Missing Labels — {cam_name} / {label_type}'
        msg['From']    = SMTP_USER
        msg['To']      = ALERT_TO
        body = (f'Camera  : {cam_name}\n'
                f'Label   : {label_type}\n'
                f'Missing : {missing_str} ({missing_count}/{total})\n'
                f'Time    : {time.strftime("%Y-%m-%d %H:%M:%S")}\n')
        msg.attach(MIMEText(body, 'plain'))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
        print(f'[{cam_name}] Alert email sent.')
    except Exception as e:
        print(f'[{cam_name}] Email error: {e}')


# =====================================================================
#  CAMERA THREAD
# =====================================================================
class CameraThread(threading.Thread):
    def __init__(self, st: CameraState):
        super().__init__(daemon=True)
        self.state        = st
        self.latest_frame = None
        self.running      = True

    def _open(self):
        try:
            cap = cv2.VideoCapture(self.state.url)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            ret, _ = cap.read()
            if ret:
                self.state.cap             = cap
                self.state.last_frame_time = time.time()
                print(f'[{self.state.name}] Camera connected.')
                return True
            cap.release()
        except Exception as e:
            print(f'[{self.state.name}] Camera open error: {e}')
        return False

    def run(self):
        self._open()
        while self.running:
            if self.state.cap is None:
                time.sleep(2)
                self._open()
                continue
            ret, frame = self.state.cap.read()
            if ret:
                self.state.last_frame_time = time.time()
                self.latest_frame = cv2.resize(frame, TARGET_DISPLAY_SIZE)
            else:
                if time.time() - self.state.last_frame_time > RECONNECT_TIMEOUT:
                    print(f'[{self.state.name}] No frame — reconnecting…')
                    self.state.cap.release()
                    self.state.cap = None
            time.sleep(0.005)


# =====================================================================
#  ALARM THREAD
# =====================================================================
def _alarm_beep_loop():
    while True:
        if any(s.alarm_active for s in states):
            winsound.Beep(1200, 400)
            time.sleep(0.6)
        else:
            time.sleep(0.1)


# =====================================================================
#  PALLET ZONE CHECK
# =====================================================================
def check_pallet_in_zone(st: CameraState, frame):
    if not st.pallet_zone_relative or frame is None:
        return False, 0

    w, h = TARGET_DISPLAY_SIZE
    zone_boxes = to_abs(st.pallet_zone_relative, w, h)
    if not zone_boxes:
        return False, 0

    zone_mask = np.zeros(frame.shape[:2], np.uint8)
    for zx1, zy1, zx2, zy2 in zone_boxes:
        if zx2 > zx1 and zy2 > zy1:
            zone_mask[zy1:zy2, zx1:zx2] = 255

    fgmask = st.bg_subtractor.apply(frame)
    fgmask = cv2.medianBlur(fgmask, 5)
    _, fgmask = cv2.threshold(fgmask, 180, 255, cv2.THRESH_BINARY)
    kernel = np.ones((5,5), np.uint8)
    fgmask = cv2.morphologyEx(fgmask, cv2.MORPH_OPEN, kernel)
    fgmask = cv2.dilate(fgmask, kernel, iterations=2)
    fg_in_zone  = cv2.bitwise_and(fgmask, zone_mask)
    fg_area_mog = cv2.countNonZero(fg_in_zone)

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5,5), 0)
    fg_area_delta = 0
    if st.prev_gray is not None:
        diff = cv2.absdiff(gray, st.prev_gray)
        _, diff_th = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
        diff_in_zone  = cv2.bitwise_and(diff_th, zone_mask)
        fg_area_delta = cv2.countNonZero(diff_in_zone)
    st.prev_gray = gray

    fg_area    = max(fg_area_mog, fg_area_delta)
    has_pallet = fg_area > PALLET_AREA_THRESHOLD
    return has_pallet, fg_area


# =====================================================================
#  INSPECTION
# =====================================================================
def run_inspection(st: CameraState, thread: CameraThread):
    w, h     = TARGET_DISPLAY_SIZE
    abs_rois = to_abs(st.current_rois_relative, w, h)
    if not abs_rois:
        return True, [], []

    n_frames     = min(max(BASE_AVERAGE_FRAMES, len(abs_rois)*4), MAX_AVERAGE_FRAMES)
    white_acc    = [0.0] * len(abs_rois)
    yellow_acc   = [0.0] * len(abs_rois)
    collected    = 0
    attempts     = 0

    while collected < n_frames and attempts < n_frames * 3:
        attempts += 1
        frame = thread.latest_frame
        if frame is None:
            time.sleep(0.015)
            continue
        for i, (x1,y1,x2,y2) in enumerate(abs_rois):
            if x2 <= x1 or y2 <= y1:
                continue
            roi = frame[y1:y2, x1:x2]
            if roi.size < 300:
                continue
            hsv  = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
            kern = np.ones((3,3), np.uint8)
            wm   = create_mask(hsv, COLOR_CYCLE[0][2])
            wm   = cv2.morphologyEx(wm, cv2.MORPH_OPEN, kern)
            white_acc[i]  += np.sum(wm > 0) / wm.size if wm.size else 0.0
            ym   = create_mask(hsv, COLOR_CYCLE[1][2])
            ym   = cv2.morphologyEx(ym, cv2.MORPH_OPEN, kern)
            yellow_acc[i] += np.sum(ym > 0) / ym.size if ym.size else 0.0
        collected += 1
        time.sleep(0.008)

    if collected == 0:
        return False, list(range(len(abs_rois))), [0.0]*len(abs_rois)

    missing = []
    scores  = []
    for i in range(len(abs_rois)):
        wf     = white_acc[i]  / collected
        yf     = yellow_acc[i] / collected
        passed = wf >= WHITE_THRESHOLD or yf >= YELLOW_THRESHOLD
        scores.append(max(wf, yf))
        if not passed:
            missing.append(i)
            print(f'  ROI {i+1:2d}: MISSING  (W:{wf*100:.1f}% Y:{yf*100:.1f}%)')
        else:
            src = 'W' if wf >= WHITE_THRESHOLD else 'Y'
            print(f'  ROI {i+1:2d}: OK [{src}]  (W:{wf*100:.1f}% Y:{yf*100:.1f}%)')

    return len(missing) == 0, missing, scores


def _inspect_worker(st: CameraState, thread: CameraThread):
    try:
        all_pass, missing, scores = run_inspection(st, thread)
        st.all_roi_scores = scores
        if not all_pass:
            st.missing_rois = missing
            st.alarm_active = True
            st.enter_state(S.ALARM)
            print(f'[{st.name}] !!! ALARM — missing ROIs: {[i+1 for i in missing]}')
            try:
                lock_csapp()
            except Exception as e:
                print(f'[LOCK] Failed: {e}')
            abs_rois = to_abs(st.current_rois_relative, *TARGET_DISPLAY_SIZE)
            log_missing_to_db(st, missing, abs_rois)
            threading.Thread(
                target=send_alert_email,
                args=(st.name, current_label_type,
                      ','.join(str(i+1) for i in missing),
                      len(missing), len(abs_rois)),
                daemon=True,
            ).start()
        else:
            st.missing_rois = []
            st.alarm_active = False
            st.enter_state(S.PASS)
            print(f'[{st.name}] ✓ All labels present → PASS')
    finally:
        st.inspect_running = False


# =====================================================================
#  STATE MACHINE
# =====================================================================
def _update_idle(st: CameraState, has_pallet: bool):
    if has_pallet or st.motion_from_cam:
        st.pallet_detected_frames += 1
        st.pallet_absent_frames    = 0
    else:
        st.pallet_detected_frames  = 0
        st.pallet_absent_frames   += 1
    if st.pallet_detected_frames >= PALLET_STABLE_FRAMES:
        print(f'[{st.name}] Pallet confirmed → WAITING {DELAY_BEFORE_INSPECT:.1f}s…')
        st.enter_state(S.WAITING)
        st.pallet_detected_frames = 0
        st.pallet_absent_frames   = 0


def _update_waiting(st: CameraState, has_pallet: bool, thread: CameraThread):
    if not has_pallet:
        st.pallet_absent_frames += 1
        if st.pallet_absent_frames >= PALLET_GONE_FRAMES:
            print(f'[{st.name}] Pallet left during WAIT → IDLE')
            st.enter_state(S.IDLE)
            st.pallet_absent_frames   = 0
            st.pallet_detected_frames = 0
        return
    st.pallet_absent_frames = 0
    if st.elapsed() >= DELAY_BEFORE_INSPECT:
        if not st.inspect_running:
            st.inspect_running = True
            st.enter_state(S.INSPECTING)
            threading.Thread(
                target=_inspect_worker,
                args=(st, thread),
                daemon=True,
            ).start()


def _update_pass(st: CameraState):
    if st.elapsed() >= PASS_DISPLAY_DURATION:
        print(f'[{st.name}] PASS window done → COOLDOWN')
        st.enter_state(S.COOLDOWN)
        st.pallet_absent_frames = 0


def _update_cooldown(st: CameraState, has_pallet: bool):
    if has_pallet:
        st.pallet_absent_frames = 0
    else:
        st.pallet_absent_frames += 1
    if st.pallet_absent_frames >= PALLET_GONE_FRAMES:
        print(f'[{st.name}] Zone clear → back to IDLE')
        st.enter_state(S.IDLE)
        st.pallet_absent_frames   = 0
        st.pallet_detected_frames = 0
        st.missing_rois           = None
        st.all_roi_scores         = []


def update_auto_state(st: CameraState, thread: CameraThread):
    if st.auto_state == S.PAUSED:
        return
    frame = thread.latest_frame
    has_pallet, _ = check_pallet_in_zone(st, frame)
    if   st.auto_state == S.IDLE:       _update_idle(st, has_pallet)
    elif st.auto_state == S.WAITING:    _update_waiting(st, has_pallet, thread)
    elif st.auto_state == S.INSPECTING: pass
    elif st.auto_state == S.ALARM:      pass
    elif st.auto_state == S.PASS:       _update_pass(st)
    elif st.auto_state == S.COOLDOWN:   _update_cooldown(st, has_pallet)


# =====================================================================
#  DRAW OVERLAY  (unchanged)
# =====================================================================
_STATE_LABELS = {
    S.IDLE:       ("IDLE  — Watching…",                  (160,160,160)),
    S.WAITING:    ("WAITING — Pallet detected…",          (0,220,100)),
    S.INSPECTING: ("INSPECTING…",                         (0,200,255)),
    S.ALARM:      ("!!! ALARM — Labels Missing !!!",      (0,0,255)),
    S.PASS:       ("✓  PASS — All Labels OK",             (0,220,0)),
    S.PAUSED:     ("AUTO OFF",                            (120,120,120)),
    S.COOLDOWN:   ("COOLDOWN — Waiting for pallet exit…", (0,160,200)),
}

def draw_overlay(display_frame, st: CameraState, is_selected: bool, now: float):
    w, h     = TARGET_DISPLAY_SIZE
    abs_rois = to_abs(st.current_rois_relative, w, h)

    if st.pallet_zone_relative:
        zone_active = st.auto_state in (S.WAITING, S.INSPECTING, S.PASS, S.ALARM)
        zone_color  = (0,220,0) if zone_active else (255,100,0)
        if st.auto_state == S.COOLDOWN:
            zone_color = (0,160,200)
        for zx1,zy1,zx2,zy2 in to_abs(st.pallet_zone_relative, w, h):
            cv2.rectangle(display_frame, (zx1,zy1), (zx2,zy2), zone_color, 4)
            cv2.putText(display_frame, "Pallet Zone",
                        (zx1+10, zy1+36), cv2.FONT_HERSHEY_SIMPLEX, 0.8, zone_color, 2)

    rois_to_draw = (st.editable_rois_abs if (adjust_mode and is_selected)
                    else abs_rois)
    for i, (x1,y1,x2,y2) in enumerate(rois_to_draw):
        if x2 <= x1 or y2 <= y1:
            continue
        if adjust_mode and is_selected:
            color, thick = (0,220,255), 2
        elif st.auto_state == S.ALARM and st.missing_rois is not None:
            color = (0,0,255) if i in st.missing_rois else (0,220,0)
            thick = 5
        elif st.auto_state == S.PASS:
            color, thick = (0,220,0), 5
        elif st.auto_state in (S.INSPECTING, S.WAITING):
            alpha = abs(np.sin(now * 4))
            color = (int(200*alpha), int(255*alpha), int(255*alpha))
            thick = 3
        else:
            color, thick = (180,180,180), 1

        if st.auto_state not in (S.ALARM, S.PASS):
            roi_img = display_frame[y1:y2, x1:x2]
            if roi_img.size > 0:
                gray   = cv2.cvtColor(roi_img, cv2.COLOR_BGR2GRAY)
                tinted = cv2.merge([np.zeros_like(gray), gray, gray])
                display_frame[y1:y2, x1:x2] = cv2.addWeighted(
                    tinted, 0.5, roi_img, 0.5, 0)

        cv2.rectangle(display_frame, (x1,y1), (x2,y2), color, thick)
        cv2.putText(display_frame, str(i+1),
                    (x1+4, y1+18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

    label, color = _STATE_LABELS.get(st.auto_state, ("UNKNOWN",(200,200,200)))
    if st.auto_state == S.WAITING:
        remain = max(0.0, DELAY_BEFORE_INSPECT - st.elapsed())
        label  = f"PALLET — starting in {remain:.1f}s…"

    bar = display_frame[h-42:h, 0:w].copy()
    cv2.rectangle(bar, (0,0), (w,42), (20,20,20), -1)
    display_frame[h-42:h, 0:w] = cv2.addWeighted(
        bar, 0.65, display_frame[h-42:h, 0:w], 0.35, 0)
    cv2.putText(display_frame, f'{st.name} | {label}',
                (12, h-14), cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2)

    if st.auto_state == S.ALARM and st.missing_rois:
        badge = f"Missing: {len(st.missing_rois)}/{len(abs_rois)}"
        cv2.putText(display_frame, badge,
                    (w-220, h-14), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0,0,255), 2)

    if current_label_type:
        cv2.putText(display_frame, f'[{current_label_type}] {TARGET_COLOR_NAME}',
                    (10,28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200,200,200), 2)

    if st.last_lpr_plate:
        txt     = st.last_lpr_plate
        b_color = (30,169,60) if st.last_lpr_dir == 'approaching' else (160,100,40)
        bw      = max(180, len(txt)*20+60)
        bx      = w - bw - 8
        overlay = display_frame.copy()
        cv2.rectangle(overlay, (bx,6), (bx+bw,68), b_color, -1)
        display_frame = cv2.addWeighted(overlay, 0.72, display_frame, 0.28, 0)
        cv2.putText(display_frame, txt,
                    (bx+8,34), cv2.FONT_HERSHEY_DUPLEX, 0.9, (255,255,255), 2)
        cv2.putText(display_frame,
                    f'{st.last_lpr_dir} {st.last_lpr_conf:.0f}%',
                    (bx+8,58), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (210,235,210), 1)

    if st.motion_from_cam:
        cv2.circle(display_frame, (w-14, 14), 7, (40,210,60), -1)

    return display_frame


# =====================================================================
#  MOUSE CALLBACK
# =====================================================================
adjust_mode         = False
zone_adjust_mode    = False
menu_mode           = False
selected_camera_idx = 0
selected_roi        = None
drag_start          = None
drag_type           = None


def get_roi_under(x, y, rois, margin=22):
    for i, (x1,y1,x2,y2) in enumerate(rois):
        if x1 <= x <= x2 and y1 <= y <= y2:
            edge = (abs(x-x1)<margin or abs(x-x2)<margin or
                    abs(y-y1)<margin or abs(y-y2)<margin)
            return i, 'resize' if edge else 'move'
    return -1, None


def mouse_callback(event, x, y, flags, _param):
    global selected_roi, drag_start, drag_type, selected_camera_idx
    if not (adjust_mode or zone_adjust_mode):
        return
    col  = x // TARGET_DISPLAY_SIZE[0]
    row  = y // TARGET_DISPLAY_SIZE[1]
    cidx = row * DISPLAY_LAYOUT[1] + col
    if cidx < 0 or cidx >= len(states):
        return
    selected_camera_idx = cidx
    st    = states[cidx]
    rel_x = x % TARGET_DISPLAY_SIZE[0]
    rel_y = y % TARGET_DISPLAY_SIZE[1]
    rois  = st.editable_zone_abs if zone_adjust_mode else st.editable_rois_abs

    if event == cv2.EVENT_LBUTTONDOWN:
        idx, typ = get_roi_under(rel_x, rel_y, rois)
        if idx >= 0:
            selected_roi = idx; drag_start = (rel_x,rel_y); drag_type = typ
        else:
            rois.append([rel_x, rel_y, rel_x+10, rel_y+10])
            selected_roi = len(rois)-1
            drag_start   = (rel_x, rel_y)
            drag_type    = 'new'

    elif event == cv2.EVENT_MOUSEMOVE and drag_start and selected_roi is not None:
        if selected_roi >= len(rois):
            return
        r  = rois[selected_roi]
        dx = rel_x - drag_start[0]
        dy = rel_y - drag_start[1]
        if drag_type == 'new':
            r[2] = rel_x; r[3] = rel_y
        elif drag_type == 'resize':
            if abs(rel_x-r[0]) < 30: r[0] = rel_x
            if abs(rel_x-r[2]) < 30: r[2] = rel_x
            if abs(rel_y-r[1]) < 30: r[1] = rel_y
            if abs(rel_y-r[3]) < 30: r[3] = rel_y
        elif drag_type == 'move':
            r[0]+=dx; r[1]+=dy; r[2]+=dx; r[3]+=dy
            drag_start = (rel_x, rel_y)
        r[:] = [min(r[0],r[2]), min(r[1],r[3]), max(r[0],r[2]), max(r[1],r[3])]

    elif event == cv2.EVENT_LBUTTONUP:
        drag_start = None; drag_type = None

    elif event == cv2.EVENT_RBUTTONDOWN:
        idx, _ = get_roi_under(rel_x, rel_y, rois)
        if idx >= 0:
            del rois[idx]
            if selected_roi == idx:
                selected_roi = None
            elif selected_roi and selected_roi > idx:
                selected_roi -= 1


# =====================================================================
#  STARTUP
# =====================================================================
# 1. Ensure tblCameras exists (creates it if absent)
ensure_cameras_table()

# 2. Load camera list from DB  ← replaces hardcoded CAMERAS list
if load_cameras_from_db() == 0:
    print('[STARTUP] No cameras in database. Exiting.')
    raise SystemExit(1)

# 3. Adjust display layout to match number of cameras loaded
n_cams = len(CAMERAS)
if n_cams == 1:
    DISPLAY_LAYOUT = (1, 1)
elif n_cams <= 2:
    DISPLAY_LAYOUT = (1, 2)
elif n_cams <= 4:
    DISPLAY_LAYOUT = (2, 2)
else:
    cols = -(-n_cams ** 0.5 // 1)   # ceiling sqrt
    DISPLAY_LAYOUT = (int(-(-n_cams // cols)), int(cols))

# 4. Build state objects and threads
states  = [CameraState(c["name"], c["ip"]) for c in CAMERAS]
threads = []

load_available_label_types()
if available_label_types:
    load_rois_for_label_type(available_label_types[0])

threading.Thread(target=_alarm_beep_loop, daemon=True).start()

for st in states:
    st.bg_subtractor = cv2.createBackgroundSubtractorMOG2(
        history=BGS_HISTORY, varThreshold=BGS_VAR_THRESHOLD, detectShadows=False)

    t = CameraThread(st)
    t.start()
    threads.append(t)

    def _make_plate_cb(s: CameraState):
        def _cb(evt: LPREvent):
            print(f'[{s.name}] PLATE: {evt.plate}  conf={evt.confidence:.0f}%'
                  f'  dir={evt.direction}  type={evt.vehicle_type}')
            s.last_lpr_plate = evt.plate
            s.last_lpr_conf  = evt.confidence
            s.last_lpr_dir   = evt.direction
            threading.Thread(target=log_lpr_to_db, args=(s, evt), daemon=True).start()
            if (s.auto_state == S.IDLE
                    and evt.direction.lower() == 'approaching'):
                print(f'[{s.name}] ISAPI trigger → WAITING')
                s.enter_state(S.WAITING)
                s.pallet_detected_frames = PALLET_STABLE_FRAMES
        return _cb

    def _make_motion_cb(s: CameraState):
        def _cb(active: bool):
            s.motion_from_cam = active
        return _cb

    client = ISAPIClient(
        ip       = st.ip or ip,
        username = USERNAME,
        password = PASSWORD,
        on_plate  = _make_plate_cb(st),
        on_motion = _make_motion_cb(st),
    )
    client.start()
    st._isapi = client

cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
cv2.resizeWindow(WINDOW_NAME, 1920, 1080)
cv2.setMouseCallback(WINDOW_NAME, mouse_callback)

print("\n=== Multi-Camera Label Monitor v2 ===")
print("  p   → Toggle AUTO detection ON/OFF")
print("  m   → Menu")
print("  r   → Adjust Label ROIs")
print("  z   → Adjust Pallet Zone")
print("  s   → Save config")
print("  c   → Manual check now")
print("  a   → Reset alarm  (→ COOLDOWN, waits for pallet removal)")
print("  t   → Cycle detection colour")
print("  l   → Switch label type")
print("  q   → Quit\n")


# =====================================================================
#  MAIN LOOP
# =====================================================================
while True:
    now    = time.time()
    rows_n, cols_n = DISPLAY_LAYOUT
    canvas = np.zeros(
        (TARGET_DISPLAY_SIZE[1]*rows_n, TARGET_DISPLAY_SIZE[0]*cols_n, 3), np.uint8)

    for idx, st in enumerate(states):
        thread = threads[idx]
        row    = idx // cols_n
        col    = idx %  cols_n
        y0     = row * TARGET_DISPLAY_SIZE[1]
        x0     = col * TARGET_DISPLAY_SIZE[0]

        if (thread.latest_frame is None or
                now - st.last_frame_time > RECONNECT_TIMEOUT):
            blank = np.zeros((*TARGET_DISPLAY_SIZE[::-1], 3), np.uint8)
            cv2.putText(blank, f'{st.name} — NO SIGNAL',
                        (80,270), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0,0,200), 3)
            display_frame = blank
        else:
            display_frame = thread.latest_frame.copy()

        update_auto_state(st, thread)

        display_frame = draw_overlay(
            display_frame, st,
            is_selected=(idx == selected_camera_idx),
            now=now)

        canvas[y0:y0+TARGET_DISPLAY_SIZE[1],
               x0:x0+TARGET_DISPLAY_SIZE[0]] = display_frame

    if any(s.alarm_active for s in states):
        ch, cw = canvas.shape[:2]
        bar    = canvas[ch-80:ch, 0:cw].copy()
        cv2.rectangle(bar, (0,0), (cw,80), (0,0,160), -1)
        canvas[ch-80:ch, 0:cw] = cv2.addWeighted(
            bar, 0.7, canvas[ch-80:ch, 0:cw], 0.3, 0)
        cv2.putText(canvas,
                    "!!! ALARM — MISSING LABELS — Press 'a' to reset !!!",
                    (60, ch-25), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0,60,255), 4)

    if adjust_mode or zone_adjust_mode:
        ch, cw = canvas.shape[:2]
        mode   = "ZONE ADJUST" if zone_adjust_mode else "ROI ADJUST"
        cv2.rectangle(canvas, (0,ch-52), (cw,ch), (20,20,50), -1)
        cv2.putText(canvas,
                    f'{mode} — {states[selected_camera_idx].name}'
                    '  | LMB=draw  RMB=delete  S=save  ESC=cancel',
                    (20, ch-16), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0,230,230), 2)

    if menu_mode:
        ch, cw = canvas.shape[:2]
        ov = canvas.copy()
        cv2.rectangle(ov,
                      (cw//2-320, ch//2-280), (cw//2+320, ch//2+280),
                      (25,25,55), -1)
        canvas = cv2.addWeighted(ov, 0.82, canvas, 0.18, 0)
        cv2.putText(canvas, "MENU",
                    (cw//2-60, ch//2-210), cv2.FONT_HERSHEY_DUPLEX, 2.0, (0,230,230), 4)
        items = [
            "p - AUTO detection ON/OFF",
            "r - Adjust Label ROIs",
            "z - Adjust Pallet Zone",
            "l - Switch Label Type",
            "t - Cycle Color",
            "c - Manual Check",
            "a - Reset Alarm",
            "q - Quit",
        ]
        for i, txt in enumerate(items):
            cv2.putText(canvas, txt,
                        (cw//2-260, ch//2-130+i*52),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.95, (240,240,240), 2)

    cv2.imshow(WINDOW_NAME, canvas)
    key = cv2.waitKey(1) & 0xFF

    if key == ord('q'):
        break

    elif key == ord('m'):
        menu_mode    = not menu_mode
        adjust_mode  = zone_adjust_mode = False

    elif key == ord('p'):
        any_active = any(s.auto_state != S.PAUSED for s in states)
        for s in states:
            if any_active:
                s.auto_state   = S.PAUSED
                s.alarm_active = False
                s.missing_rois = None
            else:
                s.enter_state(S.IDLE)
                s.pallet_detected_frames = 0
                s.pallet_absent_frames   = 0
        print(f'Auto detection: {"OFF" if any_active else "ON"}')

    elif key == ord('a'):
        for s in states:
            s.alarm_active   = False
            s.missing_rois   = None
            s.all_roi_scores = []
            if s.auto_state == S.ALARM:
                s.enter_state(S.COOLDOWN)
                s.pallet_absent_frames = 0
        try:
            unlock_csapp()
        except Exception as e:
            print(f'[UNLOCK] Failed: {e}')
        print('Alarm reset → COOLDOWN (waiting for pallet removal)')

    elif key == ord('r') and (menu_mode or not zone_adjust_mode):
        menu_mode = False; adjust_mode = True; zone_adjust_mode = False
        for s in states:
            s.editable_rois_abs = to_abs(s.current_rois_relative, *TARGET_DISPLAY_SIZE)

    elif key == ord('z') and (menu_mode or not adjust_mode):
        menu_mode = False; zone_adjust_mode = True; adjust_mode = False
        for s in states:
            s.editable_zone_abs = to_abs(s.pallet_zone_relative, *TARGET_DISPLAY_SIZE)

    elif key == ord('s') and (adjust_mode or zone_adjust_mode):
        w, h = TARGET_DISPLAY_SIZE
        st   = states[selected_camera_idx]
        if adjust_mode:
            st.current_rois_relative = to_rel(st.editable_rois_abs, w, h)
        if zone_adjust_mode:
            st.pallet_zone_relative = to_rel(st.editable_zone_abs, w, h)
        save_config_to_db(st)
        adjust_mode = zone_adjust_mode = False

    elif key == 27:
        adjust_mode = zone_adjust_mode = False

    elif key == ord('c') and current_label_type and not adjust_mode:
        print('\n=== MANUAL CHECK ===')
        for i, s in enumerate(states):
            if threads[i].latest_frame is not None and not s.inspect_running:
                s.inspect_running = True
                threading.Thread(
                    target=_inspect_worker,
                    args=(s, threads[i]),
                    daemon=True,
                ).start()

    elif key == ord('t'):
        current_color_idx = (current_color_idx+1) % len(COLOR_CYCLE)
        TARGET_COLOR_NAME = COLOR_CYCLE[current_color_idx][0]
        TARGET_CONFIG     = COLOR_CYCLE[current_color_idx][2]
        print(f'Detection colour: {TARGET_COLOR_NAME}')

    elif key == ord('l'):
        load_available_label_types()
        if available_label_types:
            cur = (available_label_types.index(current_label_type)
                   if current_label_type in available_label_types else -1)
            nxt = (cur+1) % len(available_label_types)
            load_rois_for_label_type(available_label_types[nxt])
            print(f'Label type: {current_label_type}')


# =====================================================================
#  CLEANUP
# =====================================================================
for t in threads:
    t.running = False
for s in states:
    if s._isapi:
        s._isapi.stop()
for t in threads:
    t.join(timeout=2)
for s in states:
    if s.cap:
        s.cap.release()
cv2.destroyAllWindows()
print('Terminated.')