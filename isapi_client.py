
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests

# ── Bypass corporate proxy for direct camera LAN access ──
_NO_PROXY = {"http": "", "https": ""}

# How long (seconds) after the last motion event before we call on_motion(False)
MOTION_RESET_SECS = 3.0

# Retry delay after stream disconnect
RETRY_DELAY = 8.0


# ===== Data classes =================================================

@dataclass
class PalletEvent:
    event_type: str
    channel: str
    region_id :str
    timestamp: float = field(default_factory=time.time)

@dataclass
class LPREvent:
    plate:        str
    confidence:   float
    direction:    str
    vehicle_type: str
    timestamp:    float = field(default_factory=time.time)


@dataclass
class VCAEvent:
    event_type: str
    channel:    str
    direction:  str
    timestamp:  float = field(default_factory=time.time)


# ===== ISAPIClient ==================================================

class ISAPIClient:
    """
    Connects to a single Hikvision camera's alertStream endpoint.

    Callbacks (all optional, called from background thread):
        on_plate(LPREvent)          – plate / label-text read
        on_motion(active: bool)     – True on first motion event, False after
                                      MOTION_RESET_SECS of silence
        on_vca(VCAEvent)            – every other smart event
    """

    # Single unified event endpoint — works on all firmware versions
    _ALERT_STREAM = '/ISAPI/Event/notification/alertStream'

    # LPR endpoint — only on cameras with Traffic licence; we try it but
    # fall back gracefully when the camera returns "Invalid Operation".
    _LPR_STREAM   = '/ISAPI/Traffic/channels/1/vehicleDetect/plates/stream'

    def __init__(
        self,
        ip:         str,
        username:   str,
        password:   str,
        on_plate:   Optional[Callable] = None,
        on_motion:  Optional[Callable] = None,
        on_vca:     Optional[Callable] = None,
        on_pallet:  Optional[Callable] = None,
    ):
        self._base       = f'http://{ip}'
        self._auth       = requests.auth.HTTPDigestAuth(username, password)
        self._on_plate   = on_plate
        self._on_motion  = on_motion
        self._on_vca     = on_vca
        self._on_pallet = on_pallet
        self._running    = False

        # Motion auto-reset
        self._motion_active    = False
        self._last_motion_time = 0.0
        self._motion_lock      = threading.Lock()

    # ── Public API ──────────────────────────────────────────────────

    def start(self):
        self._running = True
        threading.Thread(target=self._alert_loop,  daemon=True, name='isapi-alert').start()
        threading.Thread(target=self._motion_reset_loop, daemon=True, name='isapi-mreset').start()
        # Attempt LPR stream only if camera has Traffic module
        threading.Thread(target=self._lpr_loop,    daemon=True, name='isapi-lpr').start()
        print(f'[ISAPI] Started → {self._base}')

    def stop(self):
        self._running = False
        print(f'[ISAPI] Stopped → {self._base}')

    # ── Alert stream (motion + VCA + ANPR on capable cameras) ───────

    def _alert_loop(self):

        tag = b'</EventNotificationAlert>'
        while self._running:
            try:
                with requests.get(
                    self._base + self._ALERT_STREAM,
                    auth    = self._auth,
                    stream  = True,
                    timeout = (10, 300),
                    proxies = _NO_PROXY,
                ) as r:
                    if r.status_code != 200:
                        print(f'[ISAPI] alertStream HTTP {r.status_code} — retry in {RETRY_DELAY}s')
                        time.sleep(RETRY_DELAY)
                        continue

                    print('[ISAPI] alertStream connected ✓')
                    buf = b''
                    for chunk in r.iter_content(chunk_size=4096):
                        if not self._running:
                            break
                        buf += chunk
                        while tag in buf:
                            end  = buf.index(tag) + len(tag)
                            self._dispatch_event(buf[:end])
                            buf  = buf[end:]
                        # Prevent buffer growing unbounded on malformed data
                        if len(buf) > 131072:
                            buf = buf[-65536:]

            except requests.exceptions.ConnectionError:
                print('[ISAPI] alertStream: connection refused — camera unreachable?')
            except requests.exceptions.ReadTimeout:
                print('[ISAPI] alertStream: read timeout — reconnecting')
            except Exception as e:
                print(f'[ISAPI] alertStream error: {e}')

            if self._running:
                time.sleep(RETRY_DELAY)

    def _dispatch_event(self, data: bytes):
        """Parse one XML alert block and route to the right callback."""
        try:
            text = data.decode('utf-8', 'ignore')
            # Skip MIME headers before the XML
            xml_start = text.find('<')
            if xml_start < 0:
                return
            text = text[xml_start:]
            root = ET.fromstring(text)

            ns = root.tag.split('}')[0].strip('{') if '}' in root.tag else ''

            def _get(tag_name: str) -> str:
                el = (root.find(f'.//{{{ns}}}{tag_name}') if ns
                      else root.find(f'.//{tag_name}'))
                return (el.text or '').strip() if el is not None else ''

            event_type = _get('eventType').lower()
            channel    = _get('channelID') or '1'
            direction  = _get('direction') or ''

            # ── Motion / VMD ──
            MOTION_TYPES = {'vmd', 'motiondetection', 'fielddetection',
                            'linedetection', 'shelteralarm', 'regionentrance',
                            'regionexiting', 'loitering', 'groupdetection'}

            if event_type in MOTION_TYPES:
                self._signal_motion()

            # ── Pallet Entrance / Exit ──────────────────────────────
            PALLET_TYPES = {'regionentrance', 'regionexiting'}
            if event_type in PALLET_TYPES and self._on_pallet:
                evt = PalletEvent(
                    event_type = event_type,
                    channel = channel,
                    region_id = _get('regionID') or _get('detectRegionID') or '1',
                )
                try:
                    self._on_pallet(evt)
                except Exception as e:
                    print(f'[ISAPI] on_pallet error: {e}]')

            # ── ANPR / LPR from alertStream (some firmware sends it here) ──
            plate_text = (_get('licensePlate') or _get('plateNumber') or
                          _get('plateNo')       or _get('plateText'))
            if plate_text and self._on_plate:
                evt = LPREvent(
                    plate        = plate_text.upper(),
                    confidence   = self._safe_float(_get('confidence')),
                    direction    = _get('vehicleDirection') or direction or 'unknown',
                    vehicle_type = _get('vehicleType') or 'unknown',
                )
                self._on_plate(evt)

            # ── Generic VCA ──
            if self._on_vca and event_type not in MOTION_TYPES:
                self._on_vca(VCAEvent(
                    event_type = event_type,
                    channel    = channel,
                    direction  = direction,
                ))

        except ET.ParseError:
            pass   # Partial / malformed XML — ignore silently
        except Exception as e:
            print(f'[ISAPI] dispatch error: {e}')

    # ── LPR stream (Traffic module only) ────────────────────────────

    def _lpr_loop(self):
        """
        Tries the dedicated LPR stream endpoint. If the camera returns
        HTTP 400 / 404 / "Invalid Operation", exits permanently — no retry.
        """
        tag = b'</EventNotificationAlert>'
        while self._running:
            try:
                with requests.get(
                    self._base + self._LPR_STREAM,
                    auth    = self._auth,
                    stream  = True,
                    timeout = (10, 120),
                    proxies = _NO_PROXY,
                ) as r:
                    # Camera doesn't have Traffic licence — bail out entirely
                    if r.status_code in (400, 404):
                        print('[ISAPI] LPR stream not supported on this camera (HTTP '
                              f'{r.status_code}) — disabled.')
                        return

                    if r.status_code != 200:
                        # Could be a temporary error; check body for "Invalid Operation"
                        body = r.content.decode('utf-8', 'ignore')
                        if 'invalidOperation' in body or 'Invalid Operation' in body:
                            print('[ISAPI] LPR stream: Invalid Operation — disabled.')
                            return
                        print(f'[ISAPI] LPR stream HTTP {r.status_code} — retry in {RETRY_DELAY}s')
                        time.sleep(RETRY_DELAY)
                        continue

                    print('[ISAPI] LPR stream connected ✓')
                    buf = b''
                    for chunk in r.iter_content(chunk_size=4096):
                        if not self._running:
                            break
                        buf += chunk
                        while tag in buf:
                            end = buf.index(tag) + len(tag)
                            evt = self._parse_lpr(buf[:end])
                            buf = buf[end:]
                            if evt and self._on_plate:
                                self._on_plate(evt)

            except requests.exceptions.ConnectionError:
                print('[ISAPI] LPR: connection refused')
            except Exception as e:
                print(f'[ISAPI] LPR error: {e}')

            if self._running:
                time.sleep(RETRY_DELAY)

    def _parse_lpr(self, data: bytes) -> Optional[LPREvent]:
        try:
            text = data.decode('utf-8', 'ignore')
            text = text[text.find('<'):]
            root = ET.fromstring(text)
            ns   = root.tag.split('}')[0].strip('{') if '}' in root.tag else ''

            def f(tag_name):
                el = (root.find(f'.//{{{ns}}}{tag_name}') if ns
                      else root.find(f'.//{tag_name}'))
                return (el.text or '').strip() if el is not None else ''

            plate = (f('licensePlate') or f('plateNumber') or
                     f('plateNo')       or f('plateText'))
            if not plate:
                return None

            return LPREvent(
                plate        = plate.upper(),
                confidence   = self._safe_float(f('confidence')),
                direction    = f('vehicleDirection') or f('direction') or 'unknown',
                vehicle_type = f('vehicleType') or 'unknown',
            )
        except Exception as e:
            print(f'[ISAPI] LPR parse error: {e}')
            return None

    # ── Motion auto-reset ────────────────────────────────────────────

    def _signal_motion(self):
        """Mark motion active; reset timer."""
        with self._motion_lock:
            self._last_motion_time = time.time()
            if not self._motion_active:
                self._motion_active = True
                if self._on_motion:
                    try:
                        self._on_motion(True)
                    except Exception as e:
                        print(f'[ISAPI] on_motion(True) error: {e}')

    def _motion_reset_loop(self):
        """Background thread: calls on_motion(False) after silence."""
        while self._running:
            time.sleep(0.5)
            with self._motion_lock:
                if (self._motion_active and
                        time.time() - self._last_motion_time > MOTION_RESET_SECS):
                    self._motion_active = False
                    if self._on_motion:
                        try:
                            self._on_motion(False)
                        except Exception as e:
                            print(f'[ISAPI] on_motion(False) error: {e}')

    # ── Utility ─────────────────────────────────────────────────────

    @staticmethod
    def _safe_float(val: str) -> float:
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0