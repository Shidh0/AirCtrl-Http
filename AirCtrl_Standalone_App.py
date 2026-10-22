#!/usr/bin/env python3
"""
Air Mouse for PC - control your computer with hand gestures through the webcam.

Single file. No phone, no browser, no PIN. Tracking keeps running while the
window is minimised or hidden in the system tray.

    python AirMouse.py                 first run installs what it needs, then starts
    python AirMouse.py --minimized     start hidden in the tray
    python AirMouse.py --setup         only install dependencies
    python AirMouse.py --no-admin      do not ask for administrator rights
    python AirMouse.py --install-startup / --remove-startup

Emergency stop / resume hand tracking anywhere:  Ctrl + Alt + H
"""
import argparse
import base64
import collections
import ctypes
import importlib.util
import json
import logging
import math
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

APP = "AirMouse"
VERSION = "4.0"
SCRIPT = os.path.abspath(__file__)
REQUIRED = {"mediapipe": "mediapipe", "opencv-python": "cv2", "pynput": "pynput",
            "pillow": "PIL", "customtkinter": "customtkinter"}
OPTIONAL = {"pystray": "pystray"}                       # system tray icon
LOCK_PORT = 47653                                       # single-instance guard
HOTKEY = "<ctrl>+<alt>+h"


# =============================================================================
# Config folder, logging, dependency bootstrap
# =============================================================================
def config_dir():
    for base in (os.getenv("APPDATA"), os.path.expanduser("~"), os.path.dirname(SCRIPT)):
        if not base:
            continue
        path = os.path.join(base, APP)
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except OSError:
            continue
    return os.path.dirname(SCRIPT)


CFG_DIR = config_dir()
SETTINGS_PATH = os.path.join(CFG_DIR, "settings.json")
LOG_PATH = os.path.join(CFG_DIR, "airmouse.log")
UI_LOG = collections.deque(maxlen=400)
_log_new = collections.deque(maxlen=400)


def log(msg, level="info"):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    UI_LOG.append((level, line))
    _log_new.append((level, line))
    getattr(logging, "warning" if level == "warn" else level, logging.info)(msg)


_last = {}


def log_limited(key, msg, level="warn", every=8.0):
    now = time.monotonic()
    if now - _last.get(key, 0) >= every:
        _last[key] = now
        log(msg, level)


def setup_logging(debug=False):
    try:
        from logging.handlers import RotatingFileHandler
        h = RotatingFileHandler(LOG_PATH, maxBytes=500_000, backupCount=2, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logging.getLogger().addHandler(h)
    except Exception:
        pass
    logging.getLogger().setLevel(logging.DEBUG if debug else logging.INFO)
    logging.raiseExceptions = False


def say(text=""):
    if sys.stdout is not None:      # None under pythonw.exe
        print(text, flush=True)


def missing(mods):
    return [pkg for pkg, mod in mods.items() if importlib.util.find_spec(mod) is None]


def pip_install(pkgs):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.stdout is None else 0
    ok = True
    for pkg in pkgs:
        say(f"Installing {pkg} ...")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", pkg],
                           capture_output=True, text=True, creationflags=flags)
        if r.returncode != 0:
            ok = False
            say(f"  FAILED: {(r.stderr.strip().splitlines() or ['unknown error'])[-1]}")
    return ok


def bootstrap(force=False):
    need = missing(REQUIRED)
    if need or force:
        say("=== Air Mouse first-time setup ===")
        if not pip_install(need or list(REQUIRED)):
            msg = ("Could not install: " + ", ".join(need) + f"\n\nTry manually:\n{sys.executable} -m pip install "
                   + " ".join(need) + "\n\nMediaPipe needs 64-bit Python 3.9 - 3.12.")
            say(msg)
            try:
                import tkinter
                from tkinter import messagebox
                r = tkinter.Tk(); r.withdraw(); messagebox.showerror("Air Mouse setup", msg); r.destroy()
            except Exception:
                pass
            sys.exit(1)
    opt = missing(OPTIONAL)
    if opt:
        pip_install(opt)            # optional: failure is fine, the app just has no tray icon
    if need or force:
        say("Setup complete.")
        if not force:               # restart so the new packages are importable
            subprocess.Popen([sys.executable, SCRIPT] + sys.argv[1:])
            sys.exit(0)


# =============================================================================
# Administrator rights + autostart
# =============================================================================
def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def pythonw_exe():
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return pyw if os.path.exists(pyw) else sys.executable


def relaunch_as_admin():
    """Ask Windows (UAC prompt) to restart this script elevated. True if the user accepted."""
    try:
        params = subprocess.list2cmdline([SCRIPT] + sys.argv[1:])
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", pythonw_exe(), params, os.path.dirname(SCRIPT), 1)
        return rc > 32
    except Exception:
        return False


def startup_vbs():
    appdata = os.getenv("APPDATA")
    if not appdata:
        return None
    return os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs\Startup", APP + ".vbs")


def _schtasks(args):
    try:
        return subprocess.run("schtasks " + args, capture_output=True, text=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode == 0
    except Exception:
        return False


def autostart_enabled():
    p = startup_vbs()
    return bool((p and os.path.exists(p)) or (sys.platform == "win32" and _schtasks(f"/Query /TN {APP}")))


def set_autostart(on):
    """Admin: a logon task that runs elevated with no UAC prompt. Otherwise: a Startup-folder script."""
    if sys.platform != "win32":
        return False
    vbs = startup_vbs()
    try:
        if vbs and os.path.exists(vbs):
            os.remove(vbs)
        if not on or is_admin():
            _schtasks(f"/Delete /TN {APP} /F")
        if not on:
            return True
        exe = pythonw_exe()
        if is_admin():
            return _schtasks(f'/Create /TN {APP} /TR "\\"{exe}\\" \\"{SCRIPT}\\" --minimized" '
                             f'/SC ONLOGON /RL HIGHEST /F')
        if vbs and os.path.isdir(os.path.dirname(vbs)):
            with open(vbs, "w", encoding="utf-16") as f:
                f.write(f'CreateObject("WScript.Shell").Run """{exe}"" ""{SCRIPT}"" --minimized --no-admin", 0, False\r\n')
            return True
    except OSError as exc:
        log(f"Autostart change failed: {exc}", "error")
    return False


# =============================================================================
# Settings + control bindings
# =============================================================================
HOLD = "Left click & drag (hold)"
# id, title, description, default action
BINDINGS = [
    ("pinch",       "Pinch",                    "Index raised + pinch",                      HOLD),
    ("pinch_half",  "Pinch, index half-bent",   "Lower the index to half height and pinch",  "Double click"),
    ("pinch_low",   "Pinch, index curled",      "Curl the index down and pinch",             "Right click"),
    ("media_up",    "Media pose: hand up",      "Index + middle + ring raised, move up",     "Volume up"),
    ("media_down",  "Media pose: hand down",    "Same pose, move down",                      "Volume down"),
    ("media_right", "Media pose: swipe right",  "Same pose, swipe right",                    "Next track"),
    ("media_left",  "Media pose: swipe left",   "Same pose, swipe left",                     "Previous track"),
    ("zoom_in",     "Two hands: spread apart",  "Needs two-handed mode, both hands open",    "Zoom in"),
    ("zoom_out",    "Two hands: bring together", "Needs two-handed mode, both hands open",   "Zoom out"),
    ("fist",        "Closed fist",              "Hold a fist for half a second",             "None"),
    ("palm",        "Open palm",                "Hold all five fingers up for half a second", "None"),
]
BIND_DEFAULTS = {b[0]: b[3] for b in BINDINGS}

DEFAULTS = {"sens": 4.0, "two_hand": False, "hand": "Right", "res": 240, "fps": 30, "complexity": 0,
            "detect": 0.3, "track": 0.3, "camera": "0", "hide_preview": False, "engine_on": True,
            "tray": True, "admin": True, "pinch": 0.30, "pinch_finger": "Thumb + Pinky",
            "dwell": False, "dwell_ms": 900, "bindings": {}}
S = dict(DEFAULTS)
try:
    with open(SETTINGS_PATH, encoding="utf-8") as _f:
        S.update({k: v for k, v in json.load(_f).items() if k in DEFAULTS})
except (OSError, ValueError):
    pass
S["bindings"] = {**BIND_DEFAULTS, **(S["bindings"] if isinstance(S["bindings"], dict) else {})}


def save_settings():
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(S, f, indent=2)
    except OSError:
        pass


# =============================================================================
# Actions (what a gesture can trigger) + input injection
# =============================================================================
ACTIONS = collections.OrderedDict([
    ("None", ("none",)),
    ("Left click", ("click", "left", 1)), ("Double click", ("click", "left", 2)),
    ("Right click", ("click", "right", 1)), ("Middle click", ("click", "middle", 1)),
    (HOLD, ("hold",)),
    ("Volume up", ("press", "volumeup")), ("Volume down", ("press", "volumedown")), ("Mute", ("press", "mute")),
    ("Next track", ("press", "nexttrack")), ("Previous track", ("press", "prevtrack")),
    ("Play / pause", ("press", "playpause")),
    ("Alt + Tab", ("hotkey", "alt", "tab")), ("Task view", ("hotkey", "win", "tab")),
    ("Show desktop", ("hotkey", "win", "d")), ("Close window", ("hotkey", "alt", "f4")),
    ("Copy", ("hotkey", "ctrl", "c")), ("Paste", ("hotkey", "ctrl", "v")), ("Undo", ("hotkey", "ctrl", "z")),
    ("Screenshot (Win+Shift+S)", ("hotkey", "win", "shift", "s")),
    ("Zoom in", ("hotkey", "ctrl", "=")), ("Zoom out", ("hotkey", "ctrl", "-")),
    ("Browser back", ("hotkey", "alt", "left")), ("Browser forward", ("hotkey", "alt", "right")),
    ("Enter", ("press", "enter")), ("Space", ("press", "space")), ("Escape", ("press", "esc")),
    ("Full screen (F11)", ("press", "f11")),
    ("Turn off hand tracking", ("special", "pause")),
])
KEY_NAMES = {"ctrl", "alt", "shift", "win", "enter", "backspace", "space", "esc", "tab", "delete", "up", "down",
             "left", "right", "home", "end", "pageup", "pagedown", "insert", "printscreen", "volumeup",
             "volumedown", "mute", "nexttrack", "prevtrack", "playpause"} | {f"f{i}" for i in range(1, 13)}
ALIASES = {"control": "ctrl", "windows": "win", "cmd": "win", "super": "win", "return": "enter",
           "escape": "esc", "del": "delete", "pgup": "pageup", "pgdn": "pagedown", "prtsc": "printscreen"}


def parse_combo(text):
    """'ctrl+shift+t' -> ['ctrl', 'shift', 't'], or None if it is not a valid shortcut."""
    parts = [ALIASES.get(t, t) for t in str(text).replace(" ", "").lower().split("+") if t]
    if not 1 <= len(parts) <= 4 or any(not (p in KEY_NAMES or len(p) == 1) for p in parts):
        return None
    return parts


def valid_action(text):
    return text in ACTIONS or parse_combo(text) is not None


def do_action(inj, name, special=None):
    if not name or name == "None":
        return
    spec = ACTIONS.get(name)
    if spec is None:                                   # user-typed shortcut
        keys = parse_combo(name)
        if keys:
            inj.hotkey(*keys)
        return
    kind = spec[0]
    if kind == "click":
        inj.click(spec[1], spec[2])
    elif kind == "press":
        inj.press(spec[1])
    elif kind == "hotkey":
        inj.hotkey(*spec[1:])
    elif kind == "special" and special:
        special(spec[1])


class Injector:
    def __init__(self):
        from pynput import keyboard, mouse
        self.m, self.k = mouse.Controller(), keyboard.Controller()
        K = keyboard.Key
        self.buttons = {"left": mouse.Button.left, "right": mouse.Button.right, "middle": mouse.Button.middle}
        names = {"volumeup": "media_volume_up", "volumedown": "media_volume_down", "mute": "media_volume_mute",
                 "nexttrack": "media_next", "prevtrack": "media_previous", "playpause": "media_play_pause",
                 "ctrl": "ctrl", "alt": "alt", "shift": "shift", "win": "cmd", "enter": "enter",
                 "backspace": "backspace", "space": "space", "esc": "esc", "tab": "tab", "delete": "delete",
                 "up": "up", "down": "down", "left": "left", "right": "right", "home": "home", "end": "end",
                 "pageup": "page_up", "pagedown": "page_down", "insert": "insert", "printscreen": "print_screen"}
        names.update({f"f{i}": f"f{i}" for i in range(1, 13)})
        self.keys = {n: getattr(K, a) for n, a in names.items() if hasattr(K, a)}
        self.holding = False
        self.lock = threading.Lock()

    def key(self, name):
        return self.keys.get(name) or (name if len(name) == 1 else None)

    def _run(self, name, fn, *a):
        try:
            with self.lock:
                fn(*a)
        except Exception as exc:
            log_limited("inj-" + name, f"Input error ({name}): {exc}", "error")

    def move(self, x, y):       self._run("move", setattr, self.m, "position", (int(x), int(y)))
    def click(self, b="left", n=1): self._run("click", self.m.click, self.buttons[b], n)
    def scroll(self, n):        self._run("scroll", self.m.scroll, 0, int(n))

    def down(self):
        if not self.holding:
            self.holding = True
            self._run("down", self.m.press, self.buttons["left"])

    def up(self):
        if self.holding:
            self.holding = False
            self._run("up", self.m.release, self.buttons["left"])

    def press(self, name):
        key = self.key(name)
        if key is not None:
            self._run("press", lambda: (self.k.press(key), self.k.release(key)))

    def hotkey(self, *names):
        keys = [k for k in (self.key(n) for n in names) if k is not None]

        def go():
            pressed = []
            try:
                for k in keys:
                    self.k.press(k); pressed.append(k)
            finally:
                for k in reversed(pressed):     # never leave Ctrl/Alt stuck down
                    try: self.k.release(k)
                    except Exception: pass
        self._run("hotkey", go)


# =============================================================================
# Gesture logic
# =============================================================================
CLICK_CD, MEDIA_CD, SCROLL_CD = 350, 700, 60
VOL_STEP, SWIPE, EDGE = 0.03, 0.08, 0.06
POSE_HOLD_MS, POSE_COOLDOWN_MS = 500, 1200
_CROP = (1.0 - ((3 / 4) / (16 / 9))) / 2.0          # central band of the camera image is used as the screen
MINY, MAXY = _CROP, 1.0 - _CROP


def npt(lm):
    """Landmark -> (x, y) in 0..1. The frame is already mirrored, so x is used as-is."""
    return lm.x, max(0.0, min(1.0, (lm.y - MINY) / (MAXY - MINY)))


class Gestures:
    def __init__(self, inj, screen, special=None):
        self.inj, self.screen, self.special = inj, screen, special
        self.asp = 4 / 3                    # frame width / height, set by the engine
        self.last_click = self.last_media = self.last_scroll = self.last_zoom = 0
        self.reset()

    def reset(self):
        self.sx = self.sy = None
        self.prev_scroll = self.prev_mx = self.prev_my = self.prev_zoom = 0
        self.pinched = self.pinch_prev = False
        self.flip = 0
        self.pose, self.pose_t, self.pose_fire = None, 0, 0
        self.anchor, self.dwell_t0, self.dwell_armed = None, 0, True

    def release(self):
        self.inj.up()

    def lost(self):
        self.release(); self.sx = self.sy = None
        self.pinched = self.pinch_prev = False
        self.pose = self.anchor = None

    def bind(self, bid):
        return S["bindings"].get(bid) or BIND_DEFAULTS[bid]

    def fire(self, bid):
        do_action(self.inj, self.bind(bid), self.special)

    def dist(self, a, b):                   # distance with the camera aspect ratio corrected
        return math.hypot((a.x - b.x) * self.asp, a.y - b.y)

    def smooth(self, rx, ry):
        if self.sx is None:
            self.sx, self.sy = rx, ry
        else:
            dx, dy = rx - self.sx, ry - self.sy
            d = math.hypot(dx, dy)
            if d >= 2.5:
                f = 0.12 if d < 15 else (0.85 if d > 50 else 0.12 + (d - 15) / 35 * (0.85 - 0.12))
                self.sx += f * dx; self.sy += f * dy
        return round(self.sx), round(self.sy)

    def update_pinch(self, h):
        """Scale-invariant pinch with hysteresis and a 2-frame debounce (no accidental clicks)."""
        tip = 16 if S["pinch_finger"].endswith("Ring") else 20
        ratio = self.dist(h[4], h[tip]) / max(self.dist(h[0], h[9]), 1e-4)
        on = float(S["pinch"])
        raw = ratio < (on * 1.6 if self.pinched else on)
        if raw != self.pinched:
            self.flip += 1
            if self.flip >= 2:
                self.pinched, self.flip = raw, 0
        else:
            self.flip = 0
        rising = self.pinched and not self.pinch_prev
        self.pinch_prev = self.pinched
        return rising

    def pose_hold(self, now, name):
        if name and self.bind(name) == "None":
            name = None
        if name != self.pose:
            self.pose, self.pose_t = name, now
        elif name and now - self.pose_t > POSE_HOLD_MS and now - self.pose_fire > POSE_COOLDOWN_MS:
            self.fire(name); self.pose_fire = now

    def handle(self, res):
        inj, now = self.inj, time.time() * 1000
        if not res.multi_hand_landmarks:
            self.lost()
            return
        lms = [h.landmark for h in res.multi_hand_landmarks]
        labels = [c.classification[0].label for c in (res.multi_handedness or [])]
        want, mi = S["hand"], 0
        if want != "Auto" and want in labels:
            mi = labels.index(want)
        h = lms[mi]

        # two-handed zoom
        if S["two_hand"] and len(lms) == 2:
            o = lms[1 - mi]
            a, b = npt(h[5]), npt(o[5])
            dist = math.hypot(a[0] - b[0], a[1] - b[1])
            if all(x[8].y < x[6].y and x[12].y < x[10].y for x in (h, o)):
                if self.prev_zoom and now - self.last_zoom > 250:
                    diff = dist - self.prev_zoom
                    if diff > 0.08:    self.fire("zoom_in"); self.last_zoom = now
                    elif diff < -0.08: self.fire("zoom_out"); self.last_zoom = now
                self.prev_zoom = dist
            else:
                self.prev_zoom = 0

        sw, sh = self.screen
        track = npt(h[9])
        margin = 0.2 / (S["sens"] / 4.0)
        lo, hi = max(0.0, margin), min(1.0, 1.0 - margin)
        nx = max(0.0, min(1.0, (track[0] - lo) / (hi - lo)))
        ny = max(0.0, min(1.0, (track[1] - lo) / (hi - lo)))
        cx, cy = self.smooth(nx * (sw - 1), ny * (sh - 1))

        idx, mid, ring = h[8], h[12], h[16]
        index_up, index_down = idx.y < h[6].y, idx.y > h[5].y
        index_half = h[6].y <= idx.y <= h[5].y
        mid_up, ring_up, pinky_up = mid.y < h[10].y, ring.y < h[14].y, h[20].y < h[18].y
        rising = self.update_pinch(h)
        pinched = self.pinched
        palm = index_up and mid_up and ring_up and pinky_up
        fist = not (index_up or mid_up or ring_up or pinky_up) and not pinched
        self.pose_hold(now, "palm" if palm else "fist" if fist else None)

        if index_up and mid_up and ring_up and not pinky_up:            # media pose
            self.release(); self.prev_scroll = 0
            if self.prev_mx == 0:
                self.prev_mx, self.prev_my = track
            dx, dy = track[0] - self.prev_mx, self.prev_my - track[1]
            if abs(dy) > VOL_STEP:
                self.fire("media_up" if dy > 0 else "media_down"); self.prev_my = track[1]
            if abs(dx) > SWIPE and now - self.last_media > MEDIA_CD:
                self.fire("media_right" if dx > 0 else "media_left"); self.last_media = now; self.prev_mx = track[0]
            elif abs(dx) < 0.03:
                self.prev_mx = track[0]
        elif index_up and mid_up and not ring_up:                       # scroll
            self.release(); self.prev_mx = self.prev_my = 0
            iy = npt(idx)[1]
            if now - self.last_scroll > SCROLL_CD:
                if iy < 0.05 + EDGE:
                    inj.scroll(1); self.prev_scroll = 0; self.last_scroll = now
                elif iy > 0.95 - EDGE:
                    inj.scroll(-1); self.prev_scroll = 0; self.last_scroll = now
                else:
                    if self.prev_scroll:
                        d = self.prev_scroll - iy
                        if abs(d) > 0.015:
                            inj.scroll(math.copysign(min(3, max(1, round(abs(d) * 40))), d)); self.last_scroll = now
                    self.prev_scroll = iy
        else:                                                           # cursor / clicks
            self.prev_scroll = self.prev_mx = self.prev_my = 0
            palm_bound = self.bind("palm") != "None"
            if index_up and not (palm and palm_bound):
                inj.move(cx, cy)
            can_click = now - self.last_click > CLICK_CD
            if index_up and pinched:
                if self.bind("pinch") == HOLD:
                    inj.down()
                else:
                    self.release()
                    if rising and can_click:
                        self.fire("pinch"); self.last_click = now
            else:
                self.release()
                if rising and can_click and index_half:
                    self.fire("pinch_half"); self.last_click = now
                elif rising and can_click and index_down:
                    self.fire("pinch_low"); self.last_click = now
            # dwell click: hold the cursor still to click
            if S["dwell"] and index_up and not pinched and not palm:
                if self.anchor is None or math.hypot(cx - self.anchor[0], cy - self.anchor[1]) > 30:
                    self.anchor, self.dwell_t0, self.dwell_armed = (cx, cy), now, True
                elif self.dwell_armed and now - self.dwell_t0 > S["dwell_ms"]:
                    inj.click("left", 1); self.dwell_armed = False; self.last_click = now
            else:
                self.anchor = None


# =============================================================================
# Camera + tracking thread
# =============================================================================
LINKS = [(0,1),(1,2),(2,3),(3,4),(0,5),(5,6),(6,7),(7,8),(5,9),(9,10),(10,11),(11,12),(9,13),(13,14),
         (14,15),(15,16),(13,17),(17,18),(18,19),(19,20),(0,17)]


def open_camera(src, w, h, fps):
    import cv2
    src = str(src).strip()
    cap = None
    if src.isdigit():
        i = int(src)
        if sys.platform == "win32":
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)       # fast open, no throttling when minimised
        if cap is None or not cap.isOpened():
            cap = cv2.VideoCapture(i)
    else:
        cap = cv2.VideoCapture(src)                         # IP camera / MJPEG / RTSP URL
    if not cap.isOpened():
        raise RuntimeError(f"Could not open camera '{src}'. Is another app using it?")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
    cap.set(cv2.CAP_PROP_FPS, fps)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


MODEL_URL = "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task"


def find_model():
    """Hand model for the new MediaPipe Tasks API (downloaded once, ~8 MB)."""
    for d in (os.path.dirname(SCRIPT), CFG_DIR):
        p = os.path.join(d, "hand_landmarker.task")
        if os.path.exists(p) and os.path.getsize(p) > 1_000_000:
            return p
    dest = os.path.join(CFG_DIR, "hand_landmarker.task")
    log("Downloading the hand tracking model (one time, ~8 MB)...")
    try:
        import urllib.request
        urllib.request.urlretrieve(MODEL_URL, dest + ".part")
        os.replace(dest + ".part", dest)
    except Exception as exc:
        raise RuntimeError(f"Could not download the hand model ({exc}). Download {MODEL_URL} "
                           "and save it next to AirMouse.py, then turn tracking on again.")
    return dest


def make_detector():
    """Returns (detect(rgb) -> result, close). Works with old and new MediaPipe versions."""
    import mediapipe as mp
    n = 2 if S["two_hand"] else 1
    det, track = float(S["detect"]), float(S["track"])
    try:
        legacy = mp.solutions.hands          # removed in the newest MediaPipe releases
    except Exception:
        legacy = None
    if legacy is not None:
        h = legacy.Hands(max_num_hands=n, model_complexity=int(S["complexity"]),
                         min_detection_confidence=det, min_tracking_confidence=track)

        def detect(rgb):
            rgb.flags.writeable = False
            return h.process(rgb)
        return detect, h.close

    from mediapipe.tasks import python as mpp
    from mediapipe.tasks.python import vision
    opts = vision.HandLandmarkerOptions(
        base_options=mpp.BaseOptions(model_asset_path=find_model()),
        running_mode=vision.RunningMode.VIDEO, num_hands=n, min_hand_detection_confidence=det,
        min_hand_presence_confidence=det, min_tracking_confidence=track)
    lm = vision.HandLandmarker.create_from_options(opts)
    last = [0]

    def detect(rgb):
        ts = max(int(time.monotonic() * 1000), last[0] + 1)      # timestamps must increase
        last[0] = ts
        r = lm.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts)
        return SimpleNamespace(
            multi_hand_landmarks=[SimpleNamespace(landmark=l) for l in r.hand_landmarks] or None,
            multi_handedness=[SimpleNamespace(classification=[SimpleNamespace(label=c[0].category_name)])
                              for c in r.handedness])
    return detect, lm.close


class Engine(threading.Thread):
    def __init__(self, app):
        super().__init__(name="tracker", daemon=True)
        self.app, self.stop_evt = app, threading.Event()

    def run(self):
        import cv2
        app = self.app
        cap = close = None
        try:
            h = int(S["res"]); w = round(h * 4 / 3); fps = int(S["fps"])
            cap = open_camera(S["camera"], w, h, fps)
            detect, close = make_detector()
            gestures = Gestures(app.inj, app.screen, app.special)
            app.estate = "on"
            log(f"Camera '{S['camera']}' open, hand tracking running (max {fps} fps).")
            fails, n, t_fps, inf = 0, 0, time.perf_counter(), 0.0
            while not self.stop_evt.is_set():
                t0 = time.perf_counter()
                ok, frame = cap.read()
                if not ok or frame is None:
                    fails += 1
                    if fails > 40:
                        raise RuntimeError("The camera stopped delivering frames.")
                    time.sleep(0.03)
                    continue
                fails = 0
                if frame.shape[1] > w * 1.5:
                    frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
                gestures.asp = frame.shape[1] / frame.shape[0]
                frame = cv2.flip(frame, 1)                       # mirror: natural left/right, correct hand labels
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                ti = time.perf_counter()
                res = detect(rgb)
                inf = (time.perf_counter() - ti) * 1000
                try:
                    gestures.handle(res)
                except Exception as exc:
                    log_limited("gesture", f"Tracking error: {exc}", "error", 10)
                n += 1
                if time.perf_counter() - t_fps >= 1.0:
                    app.stats = (n, round(inf))
                    n, t_fps = 0, time.perf_counter()
                if app.want_preview and res.multi_hand_landmarks:
                    fh, fw = frame.shape[:2]
                    for hl in res.multi_hand_landmarks:
                        pts = [(int(p.x * fw), int(p.y * fh)) for p in hl.landmark]
                        for a, b in LINKS:
                            cv2.line(frame, pts[a], pts[b], (200, 231, 110), 2)
                        for p in pts:
                            cv2.circle(frame, p, 3, (240, 241, 232), -1)
                        cv2.circle(frame, pts[9], 7, (200, 231, 110), -1)
                if app.want_preview:
                    app.frame = frame
                wait = 1.0 / fps - (time.perf_counter() - t0)
                if wait > 0:
                    time.sleep(wait)
        except Exception as exc:
            log(f"Hand tracking could not run: {exc}", "error")
            app.estate, app.eerr = "error", str(exc)
        finally:
            try: app.inj.up()
            except Exception: pass
            if close is not None:
                try: close()
                except Exception: pass
            if cap is not None:
                cap.release()
            app.frame = None
            if app.estate != "error":
                app.estate = "off"
            app.stats = None


# =============================================================================
# GUI
# =============================================================================
BG, SIDE, CARD, CARD2, FIELD, LINE = "#0a1114", "#0d171b", "#121d23", "#18272f", "#0e171b", "#233741"
TEXT, MUTED, DIM = "#e8f1f0", "#8aa3a6", "#5d777b"
ACCENT, ACCENT_H, INK, SEL = "#6ee7c8", "#5bd3b5", "#04231d", "#23806c"
DANGER, WARN = "#ff7070", "#ffc145"
RES = {"240x180 (fast)": 180, "320x240 (default)": 240, "480x360": 360, "640x480 (sharp)": 480}
PAGES = [("tracking", "Tracking"), ("controls", "Controls"), ("shortcuts", "Shortcuts"),
         ("settings", "Settings"), ("log", "Log")]
GUIDE = [("Move", "Raise your index finger, keep the others curled."),
         ("Click / drag", "With the index raised, pinch (see Controls for the pinch fingers)."),
         ("Double / right click", "Half-bend or curl the index, then pinch."),
         ("Scroll", "Raise index + middle and move up or down. Hold near the top or bottom edge to keep scrolling."),
         ("Volume / tracks", "Raise index + middle + ring. Move up/down or swipe left/right."),
         ("Zoom", "Two-handed mode: open both hands, spread or pinch them together.")]
SHORTCUTS = ["Volume up", "Volume down", "Mute", "Previous track", "Play / pause", "Next track", "Alt + Tab",
             "Task view", "Show desktop", "Screenshot (Win+Shift+S)", "Full screen (F11)", "Close window"]


def battery_text():
    try:
        class SPS(ctypes.Structure):
            _fields_ = [("ac", ctypes.c_ubyte), ("flag", ctypes.c_ubyte), ("pct", ctypes.c_ubyte),
                        ("sys", ctypes.c_ubyte), ("life", ctypes.c_ulong), ("full", ctypes.c_ulong)]
        s = SPS()
        ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s))
        if s.pct == 255 or s.flag == 128:
            return "AC"
        return f"{s.pct}%" + (" \u26A1" if s.ac == 1 else "")
    except Exception:
        return "n/a"


class App:
    def __init__(self, start_hidden=False):
        import tkinter as tk
        import customtkinter as ctk
        self.tk, self.ctk = tk, ctk
        ctk.set_appearance_mode("dark")
        self.root = ctk.CTk(fg_color=BG)
        self.root.title("Air Mouse")
        self.root.geometry("1060x700")
        self.root.minsize(960, 640)
        self.screen = (self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        self.inj = Injector()
        self.ui_q = queue.Queue()
        self.engine = None
        self.estate, self.eerr = "off", ""
        self.frame, self.stats, self.want_preview = None, None, False
        self.photo, self.tray, self.hk, self.icon = None, None, None, None
        self.page = "tracking"
        self.bind_vars = {}
        self._battery_t = 0
        self.build()
        self.start_hotkey()
        if S["tray"]:
            self.start_tray()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(300, self.set_icon)           # CustomTkinter replaces the icon at ~200 ms
        log(f"Air Mouse {VERSION} ready. Screen {self.screen[0]}x{self.screen[1]}. "
            f"{'Running as administrator.' if is_admin() else 'Not running as administrator.'}")
        if start_hidden:
            self.hide_window()
        if S["engine_on"]:
            self.root.after(400, self.start_engine)
        self.tick()

    # ---- small widget helpers -------------------------------------------------
    def F(self, size=13, weight="normal"):
        return self.ctk.CTkFont(family="Segoe UI", size=size, weight=weight)

    def card(self, parent, **kw):
        return self.ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14, **kw)

    def lbl(self, parent, text, size=13, weight="normal", color=TEXT, **kw):
        return self.ctk.CTkLabel(parent, text=text, font=self.F(size, weight), text_color=color, **kw)

    def btn(self, parent, text, cmd, primary=False, **kw):
        opts = dict(height=36, corner_radius=10, font=self.F(13, "bold" if primary else "normal"),
                    fg_color=ACCENT if primary else CARD2, hover_color=ACCENT_H if primary else LINE,
                    text_color=INK if primary else TEXT)
        opts.update(kw)
        return self.ctk.CTkButton(parent, text=text, command=cmd, **opts)

    def switch(self, parent, text, value, cmd):
        var = self.tk.BooleanVar(value=bool(value))
        sw = self.ctk.CTkSwitch(parent, text=text, variable=var, onvalue=True, offvalue=False, command=lambda: cmd(var.get()),
                                font=self.F(13), text_color=TEXT, progress_color=ACCENT, button_color=TEXT, button_hover_color="#ffffff")
        return sw, var

    def header(self, parent, title, sub):
        f = self.ctk.CTkFrame(parent, fg_color="transparent")
        f.pack(fill="x", pady=(4, 12))
        self.lbl(f, title, 26, "bold").pack(anchor="w")
        self.lbl(f, sub, 13, color=MUTED).pack(anchor="w")

    def section(self, parent, title, sub=None):
        self.lbl(parent, title, 15, "bold").pack(anchor="w", padx=16, pady=(14, 0 if sub else 6))
        if sub:
            self.lbl(parent, sub, 12, color=MUTED, wraplength=380, justify="left").pack(anchor="w", padx=16, pady=(0, 6))

    def slider_row(self, parent, title, key, lo, hi, steps, fmt, cast=None):
        ctk = self.ctk
        row = ctk.CTkFrame(parent, fg_color="transparent"); row.pack(fill="x", padx=16, pady=(2, 8))
        top = ctk.CTkFrame(row, fg_color="transparent"); top.pack(fill="x")
        self.lbl(top, title, 13, color=MUTED).pack(side="left")
        val = self.lbl(top, fmt % S[key], 13, "bold", ACCENT); val.pack(side="right")

        def changed(v):
            S[key] = cast(float(v)) if cast else round(float(v), 2)
            val.configure(text=fmt % S[key])
        sl = ctk.CTkSlider(row, from_=lo, to=hi, number_of_steps=steps, command=changed, progress_color=ACCENT,
                           button_color=ACCENT, button_hover_color=ACCENT_H, fg_color=LINE)
        sl.set(S[key]); sl.pack(fill="x", pady=(6, 0))
        try:
            sl.bind("<ButtonRelease-1>", lambda e: save_settings())
        except Exception:
            pass
        return sl

    def opt_row(self, parent, title, var, values, cmd=None, width=190):
        row = self.ctk.CTkFrame(parent, fg_color="transparent"); row.pack(fill="x", padx=16, pady=4)
        self.lbl(row, title, 13, color=MUTED).pack(side="left")
        om = self.ctk.CTkOptionMenu(row, values=values, variable=var, width=width, height=32, command=cmd, font=self.F(13),
                                    fg_color=FIELD, button_color=CARD2, button_hover_color=LINE,
                                    dropdown_fg_color=CARD2, dropdown_hover_color=LINE, text_color=TEXT, dropdown_text_color=TEXT)
        om.pack(side="right")
        return om

    # ---- layout --------------------------------------------------------------
    def build(self):
        ctk, tk = self.ctk, self.tk
        self.root.grid_columnconfigure(1, weight=1)
        self.root.grid_rowconfigure(0, weight=1)

        side = ctk.CTkFrame(self.root, width=220, corner_radius=0, fg_color=SIDE)
        side.grid(row=0, column=0, sticky="nsew"); side.grid_propagate(False)
        logo = ctk.CTkFrame(side, fg_color="transparent"); logo.pack(fill="x", padx=18, pady=(24, 22))
        mark = ctk.CTkFrame(logo, width=40, height=40, corner_radius=12, fg_color=ACCENT)
        mark.pack(side="left"); mark.pack_propagate(False)
        self.lbl(mark, "\u27A4", 18, "bold", INK).pack(expand=True)
        tcol = ctk.CTkFrame(logo, fg_color="transparent"); tcol.pack(side="left", padx=12)
        self.lbl(tcol, "Air Mouse", 18, "bold").pack(anchor="w")
        self.lbl(tcol, "Hand gesture control", 11, color=MUTED).pack(anchor="w")

        self.nav = {}
        for key, text in PAGES:
            b = ctk.CTkButton(side, text="   " + text, anchor="w", height=42, corner_radius=10, font=self.F(14, "bold"),
                              fg_color="transparent", hover_color=CARD2, text_color=MUTED, command=lambda k=key: self.show_page(k))
            b.pack(fill="x", padx=12, pady=2)
            self.nav[key] = b

        bottom = ctk.CTkFrame(side, fg_color="transparent"); bottom.pack(side="bottom", fill="x", padx=16, pady=18)
        self.chip = ctk.CTkLabel(bottom, text="\u25CF  Off", height=34, corner_radius=10, fg_color=CARD2, text_color=MUTED, font=self.F(13, "bold"))
        self.chip.pack(fill="x")
        self.lbl(bottom, "Ctrl + Alt + H  toggles tracking", 11, color=DIM).pack(pady=(10, 0))
        self.lbl(bottom, f"v{VERSION}  \u00B7  " + ("Administrator" if is_admin() else "Standard user"), 11, color=DIM).pack(pady=(2, 0))

        main = ctk.CTkFrame(self.root, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew", padx=(18, 18), pady=(14, 14))
        main.grid_columnconfigure(0, weight=1); main.grid_rowconfigure(0, weight=1)
        self.pages = {}
        builders = {"tracking": self.build_tracking, "controls": self.build_controls, "shortcuts": self.build_shortcuts,
                    "settings": self.build_settings, "log": self.build_log}
        for key, _ in PAGES:
            cont = ctk.CTkFrame(main, fg_color="transparent")
            cont.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = cont
            if key in ("controls", "settings", "shortcuts"):
                body = ctk.CTkScrollableFrame(cont, fg_color="transparent")
                body.pack(fill="both", expand=True)
                builders[key](body)
            else:
                builders[key](cont)
        self.show_page("tracking")

    def show_page(self, key):
        self.page = key
        for k, f in self.pages.items():
            if k == key:
                f.grid()
            else:
                f.grid_remove()
        for k, b in self.nav.items():
            b.configure(fg_color=CARD2 if k == key else "transparent", text_color=ACCENT if k == key else MUTED)

    # ---- pages ---------------------------------------------------------------
    def build_tracking(self, p):
        ctk, tk = self.ctk, self.tk
        self.header(p, "Tracking", "Point, pinch and swipe in front of your webcam to control the PC.")
        body = ctk.CTkFrame(p, fg_color="transparent"); body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, weight=3); body.grid_columnconfigure(1, weight=2); body.grid_rowconfigure(0, weight=1)

        left = self.card(body); left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.pv = ctk.CTkFrame(left, fg_color="#060b0d", corner_radius=12)
        self.pv.pack(fill="both", expand=True, padx=14, pady=(14, 10)); self.pv.pack_propagate(False)
        self.pv_lbl = tk.Label(self.pv, bg="#060b0d", fg=DIM, font=("Segoe UI", 11), wraplength=420, justify="center", bd=0)
        self.pv_lbl.pack(expand=True, fill="both")
        row = ctk.CTkFrame(left, fg_color="transparent"); row.pack(fill="x", padx=14)
        row.grid_columnconfigure((0, 1), weight=1, uniform="b")
        self.hide_btn = self.btn(row, "Hide preview", self.toggle_preview)
        self.hide_btn.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.next_btn = self.btn(row, "Next camera", self.next_camera)
        self.next_btn.grid(row=0, column=1, sticky="ew", padx=(5, 0))
        tiles = ctk.CTkFrame(left, fg_color="transparent"); tiles.pack(fill="x", padx=14, pady=(10, 14))
        self.stat = {}
        for i, (k, t) in enumerate((("fps", "FPS"), ("inf", "Inference ms"), ("bat", "Battery"))):
            tiles.grid_columnconfigure(i, weight=1, uniform="t")
            f = ctk.CTkFrame(tiles, fg_color=CARD2, corner_radius=10); f.grid(row=0, column=i, sticky="ew", padx=3)
            self.stat[k] = self.lbl(f, "--", 17, "bold"); self.stat[k].pack(pady=(8, 0))
            self.lbl(f, t, 11, color=MUTED).pack(pady=(0, 8))

        right = ctk.CTkScrollableFrame(body, fg_color="transparent"); right.grid(row=0, column=1, sticky="nsew")
        c = self.card(right); c.pack(fill="x", pady=(0, 10))
        r = ctk.CTkFrame(c, fg_color="transparent"); r.pack(fill="x", padx=16, pady=14)
        col = ctk.CTkFrame(r, fg_color="transparent"); col.pack(side="left")
        self.lbl(col, "Hand tracking", 16, "bold").pack(anchor="w")
        self.status = self.lbl(col, "Off. Camera is not in use.", 12, color=MUTED); self.status.pack(anchor="w")
        self.power = ctk.CTkSwitch(r, text="", width=50, command=self.toggle_engine, progress_color=ACCENT,
                                   button_color=TEXT, button_hover_color="#ffffff")
        self.power.pack(side="right")

        c = self.card(right); c.pack(fill="x", pady=(0, 10))
        self.section(c, "Cursor")
        self.slider_row(c, "Sensitivity", "sens", 1, 10, 18, "%.1f")
        r = ctk.CTkFrame(c, fg_color="transparent"); r.pack(fill="x", padx=16, pady=(2, 8))
        self.lbl(r, "Cursor hand", 13, color=MUTED).pack(side="left")
        seg = ctk.CTkSegmentedButton(r, values=["Right", "Left", "Auto"], command=self.on_hand, selected_color=SEL,
                                     selected_hover_color=SEL, unselected_color=FIELD, unselected_hover_color=LINE,
                                     text_color=TEXT, fg_color=FIELD, font=self.F(12))
        seg.set(S["hand"]); seg.pack(side="right")
        sw, self.two_var = self.switch(c, "Two-handed zoom", S["two_hand"], self.on_two)
        sw.pack(anchor="w", padx=16, pady=(4, 16))

        c = self.card(right); c.pack(fill="x")
        self.section(c, "Gesture guide")
        for title, text in GUIDE:
            self.lbl(c, title, 12, "bold", ACCENT).pack(anchor="w", padx=16, pady=(4, 0))
            self.lbl(c, text, 12, color=MUTED, wraplength=290, justify="left").pack(anchor="w", padx=16)
        self.lbl(c, "", 4).pack(pady=4)

    def build_controls(self, p):
        ctk, tk = self.ctk, self.tk
        self.header(p, "Controls", "Choose how clicks work and what each gesture does.")
        c = self.card(p); c.pack(fill="x", pady=(0, 12))
        self.section(c, "Clicking", "Pinching is scale-aware and debounced, so it works the same near or far from the camera.")
        self.finger_var = tk.StringVar(value=S["pinch_finger"])
        self.opt_row(c, "Pinch fingers", self.finger_var, ["Thumb + Pinky", "Thumb + Ring"], self.on_finger)
        self.slider_row(c, "Pinch sensitivity (higher = easier)", "pinch", 0.15, 0.5, 35, "%.2f")
        sw, _ = self.switch(c, "Dwell click: hold the cursor still to click", S["dwell"], self.on_dwell)
        sw.pack(anchor="w", padx=16, pady=(6, 4))
        self.slider_row(c, "Dwell time", "dwell_ms", 400, 2000, 16, "%d ms", cast=lambda v: int(round(v)))

        c = self.card(p); c.pack(fill="x", pady=(0, 12))
        self.section(c, "Gesture actions", "Pick an action, or type your own shortcut such as ctrl+shift+t or win+e, then press Enter.")
        for bid, title, desc, _ in BINDINGS:
            row = ctk.CTkFrame(c, fg_color=CARD2, corner_radius=12); row.pack(fill="x", padx=14, pady=4)
            row.grid_columnconfigure(0, weight=1)
            left = ctk.CTkFrame(row, fg_color="transparent"); left.grid(row=0, column=0, sticky="w", padx=14, pady=10)
            self.lbl(left, title, 13, "bold").pack(anchor="w")
            self.lbl(left, desc, 11, color=MUTED).pack(anchor="w")
            var = tk.StringVar(value=S["bindings"].get(bid, BIND_DEFAULTS[bid]))
            self.bind_vars[bid] = var
            values = [a for a in ACTIONS if a != HOLD or bid == "pinch"]
            cb = ctk.CTkComboBox(row, values=values, variable=var, width=230, height=34, font=self.F(13),
                                 command=lambda v, b=bid: self.set_binding(b, v), fg_color=FIELD, border_color=LINE,
                                 button_color=LINE, button_hover_color=SEL, dropdown_fg_color=CARD2,
                                 dropdown_hover_color=LINE, text_color=TEXT, dropdown_text_color=TEXT)
            cb.grid(row=0, column=1, padx=14)
            try:
                cb.bind("<Return>", lambda e, b=bid, v=var: self.set_binding(b, v.get()))
            except Exception:
                pass
        self.btn(c, "Reset all to defaults", self.reset_bindings).pack(anchor="e", padx=14, pady=(10, 14))

    def build_shortcuts(self, p):
        ctk = self.ctk
        self.header(p, "Shortcuts", "One-click actions. They are sent to whatever window is in front.")
        c = self.card(p); c.pack(fill="x")
        grid = ctk.CTkFrame(c, fg_color="transparent"); grid.pack(fill="x", padx=12, pady=12)
        for i, name in enumerate(SHORTCUTS):
            grid.grid_columnconfigure(i % 3, weight=1, uniform="s")
            b = ctk.CTkButton(grid, text=name, height=64, corner_radius=12, font=self.F(13, "bold"), fg_color=CARD2,
                              hover_color=LINE, text_color=TEXT, command=lambda n=name: self.run_action(n))
            b.grid(row=i // 3, column=i % 3, sticky="ew", padx=5, pady=5)
        self.lbl(p, "Tip: click the window you want to control first; these buttons act on the active app.",
                 12, color=DIM).pack(anchor="w", pady=10)

    def build_settings(self, p):
        ctk, tk = self.ctk, self.tk
        self.header(p, "Settings", "Camera, performance and background behaviour.")
        c = self.card(p); c.pack(fill="x", pady=(0, 12))
        self.section(c, "Camera", "A number picks a webcam. You can also paste a stream URL, e.g. http://192.168.1.5:8080/video.")
        r = ctk.CTkFrame(c, fg_color="transparent"); r.pack(fill="x", padx=16, pady=(0, 14))
        self.cam = tk.StringVar(value=str(S["camera"]))
        self.cam_cb = ctk.CTkComboBox(r, values=["0", "1", "2", "3"], variable=self.cam, height=34, font=self.F(13),
                                      fg_color=FIELD, border_color=LINE, button_color=LINE, button_hover_color=SEL,
                                      dropdown_fg_color=CARD2, dropdown_hover_color=LINE, text_color=TEXT, dropdown_text_color=TEXT)
        self.cam_cb.pack(side="left", fill="x", expand=True)
        self.btn(r, "Scan", self.scan_cameras, width=80).pack(side="right", padx=(8, 0))

        c = self.card(p); c.pack(fill="x", pady=(0, 12))
        self.section(c, "Performance")
        self.res = tk.StringVar(value=next((k for k, v in RES.items() if v == S["res"]), "320x240 (default)"))
        self.fps = tk.StringVar(value=str(S["fps"]))
        self.model = tk.StringVar(value="Full (precise)" if S["complexity"] else "Lite (fast)")
        self.opt_row(c, "Resolution", self.res, list(RES))
        self.opt_row(c, "Max FPS (lower saves CPU)", self.fps, ["15", "24", "30", "60"])
        self.opt_row(c, "Model", self.model, ["Lite (fast)", "Full (precise)"])
        self.slider_row(c, "Detect confidence", "detect", 0.1, 0.9, 16, "%.2f")
        self.slider_row(c, "Track confidence", "track", 0.1, 0.9, 16, "%.2f")
        self.btn(c, "Apply and restart camera", self.apply_perf, primary=True).pack(fill="x", padx=16, pady=(6, 16))

        c = self.card(p); c.pack(fill="x", pady=(0, 12))
        self.section(c, "Background")
        for text, val, cmd in (("Ask for administrator rights on launch", S["admin"], self.on_admin),
                               ("Start with Windows (hidden in the tray)", autostart_enabled(), self.on_autostart),
                               ("Tray icon (the close button hides the window)", S["tray"], self.on_tray),
                               ("Start hand tracking when the app opens", S["engine_on"], self.on_engine_default)):
            sw, var = self.switch(c, text, val, cmd)
            sw.pack(anchor="w", padx=16, pady=7)
            if "Windows" in text:
                self.auto_var = var
        self.lbl(c, "Administrator rights let the cursor work over elevated windows such as Task Manager.",
                 11, color=DIM, wraplength=560, justify="left").pack(anchor="w", padx=16, pady=(2, 14))
        self.btn(p, "Quit Air Mouse", self.quit, fg_color="#3a1d22", hover_color="#5a2830").pack(anchor="w", pady=6)

    def build_log(self, p):
        ctk = self.ctk
        self.header(p, "Log", "What Air Mouse is doing. Copy this when reporting a problem.")
        r = ctk.CTkFrame(p, fg_color="transparent"); r.pack(fill="x", pady=(0, 8))
        self.btn(r, "Copy", self.copy_log, width=90).pack(side="left")
        self.btn(r, "Clear", self.clear_log, width=90).pack(side="left", padx=8)
        self.logbox = ctk.CTkTextbox(p, fg_color="#070d10", text_color="#9fd8c9", font=ctk.CTkFont(family="Consolas", size=12),
                                     corner_radius=12, wrap="word")
        self.logbox.pack(fill="both", expand=True)
        for lvl, col in (("error", DANGER), ("warn", WARN), ("info", "#9fd8c9")):
            self.logbox.tag_config(lvl, foreground=col)
        for lvl, line in UI_LOG:
            self.append_log(lvl, line)
        _log_new.clear()
        self.logbox.configure(state="disabled")

    # ---- actions -------------------------------------------------------------
    def append_log(self, lvl, line):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", line + "\n", lvl)
        if int(self.logbox.index("end-1c").split(".")[0]) > 500:
            self.logbox.delete("1.0", "60.0")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def clear_log(self):
        UI_LOG.clear()
        self.logbox.configure(state="normal"); self.logbox.delete("1.0", "end"); self.logbox.configure(state="disabled")

    def copy_log(self):
        self.root.clipboard_clear(); self.root.clipboard_append("\n".join(l for _, l in UI_LOG))
        log("Log copied to clipboard.")

    def run_action(self, name):
        threading.Thread(target=do_action, args=(self.inj, name, self.special), daemon=True).start()

    def special(self, what):
        if what == "pause":
            self.ui_q.put(("toggle",))

    def set_binding(self, bid, value):
        value = (value or "").strip()
        if valid_action(value) and (value != HOLD or bid == "pinch"):
            S["bindings"][bid] = value
            save_settings()
            log(f"'{bid}' now does: {value}")
        else:
            self.bind_vars[bid].set(S["bindings"][bid])
            log(f"'{value}' is not a valid action or shortcut. Use names like ctrl+shift+t.", "warn")

    def reset_bindings(self):
        S["bindings"] = dict(BIND_DEFAULTS)
        for bid, var in self.bind_vars.items():
            var.set(BIND_DEFAULTS[bid])
        save_settings()
        log("Gesture actions reset to defaults.")

    def on_finger(self, v):
        S["pinch_finger"] = v; save_settings(); log(f"Pinch fingers: {v}")

    def on_dwell(self, v):
        S["dwell"] = bool(v); save_settings(); log("Dwell click " + ("on." if v else "off."))

    def on_hand(self, v):
        S["hand"] = v; save_settings()

    def toggle_engine(self):
        if self.estate in ("on", "starting"):
            self.stop_engine(); S["engine_on"] = False
        else:
            self.start_engine(); S["engine_on"] = True
        save_settings()

    def start_engine(self):
        if self.engine and self.engine.is_alive():
            return
        self.estate, self.eerr = "starting", ""
        self.engine = Engine(self)
        self.engine.start()
        log("Starting hand tracking...")

    def stop_engine(self, wait=True):
        e = self.engine
        if e and e.is_alive():
            e.stop_evt.set()
            if wait:
                e.join(3)
        self.engine = None
        self.estate = "off"
        log("Hand tracking off. Camera released.")

    def restart_engine(self):
        if self.estate in ("on", "starting", "error"):
            self.stop_engine(); self.start_engine()

    def toggle_preview(self):
        S["hide_preview"] = not S["hide_preview"]; save_settings()
        log("Preview hidden - no video is drawn, which saves CPU." if S["hide_preview"] else "Preview shown.")

    def next_camera(self):
        vals = list(self.cam_cb.cget("values")) or ["0", "1"]
        cur = str(S["camera"])
        nxt = vals[(vals.index(cur) + 1) % len(vals)] if cur in vals else vals[0]
        S["camera"] = nxt; self.cam.set(nxt); save_settings()
        log(f"Switching to camera {nxt}...")
        self.restart_engine()

    def scan_cameras(self):
        def work():
            import cv2
            found = []
            for i in range(5):
                c = cv2.VideoCapture(i, cv2.CAP_DSHOW) if sys.platform == "win32" else cv2.VideoCapture(i)
                if c.isOpened():
                    found.append(str(i))
                c.release()
            self.ui_q.put(("cams", found))
        if self.estate == "on":
            log("Turn hand tracking off before scanning (the active camera is busy).", "warn")
            return
        log("Scanning cameras...")
        threading.Thread(target=work, daemon=True).start()

    def apply_perf(self):
        S["camera"] = self.cam.get().strip() or "0"
        S["res"] = RES.get(self.res.get(), 240)
        S["fps"] = int(self.fps.get())
        S["complexity"] = 1 if self.model.get().startswith("Full") else 0
        save_settings()
        log(f"Applied: camera {S['camera']}, {S['res']}p, max {S['fps']} fps, detect {S['detect']}, track {S['track']}")
        self.restart_engine()

    def on_two(self, v):
        S["two_hand"] = bool(v); save_settings()
        log(f"Hand mode: {'two hands' if v else 'single hand'}")
        self.restart_engine()

    def on_admin(self, v):
        S["admin"] = bool(v); save_settings()
        log("Will " + ("ask for administrator rights on the next launch." if v else "no longer ask for administrator rights."))

    def on_autostart(self, v):
        if not set_autostart(bool(v)):
            log("Could not change autostart (Windows only).", "warn")
        self.auto_var.set(autostart_enabled())
        log("Autostart " + ("enabled." if autostart_enabled() else "disabled."))

    def on_tray(self, v):
        S["tray"] = bool(v); save_settings()
        if v:
            self.start_tray()
        elif self.tray:
            try: self.tray.stop()
            except Exception: pass
            self.tray = None

    def on_engine_default(self, v):
        S["engine_on"] = bool(v); save_settings()

    # ---- tray, hotkey, window -----------------------------------------------
    def set_icon(self):
        try:
            from PIL import Image, ImageDraw, ImageTk
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle((0, 0, 63, 63), 14, fill=(110, 231, 200, 255))
            d.polygon([(20, 14), (48, 30), (36, 34), (31, 50)], fill=(4, 35, 29, 255))
            self.icon = ImageTk.PhotoImage(img)
            self.root.iconphoto(False, self.icon)
        except Exception:
            pass

    def start_tray(self):
        if self.tray:
            return
        try:
            import pystray
            from PIL import Image, ImageDraw
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle((0, 0, 63, 63), 14, fill=(110, 231, 200, 255))
            d.polygon([(20, 14), (48, 30), (36, 34), (31, 50)], fill=(4, 35, 29, 255))
            q = self.ui_q
            menu = pystray.Menu(
                pystray.MenuItem("Show window", lambda i, it: q.put(("show",)), default=True),
                pystray.MenuItem("Hand tracking on/off", lambda i, it: q.put(("toggle",))),
                pystray.MenuItem("Quit", lambda i, it: q.put(("quit",))))
            self.tray = pystray.Icon(APP, img, "Air Mouse", menu)
            self.tray.run_detached()
        except Exception as exc:
            self.tray = None
            log(f"Tray icon unavailable ({exc}). The window will minimise instead of hiding.", "warn")

    def start_hotkey(self):
        try:
            from pynput import keyboard
            self.hk = keyboard.GlobalHotKeys({HOTKEY: lambda: self.ui_q.put(("toggle",))})
            self.hk.daemon = True
            self.hk.start()
        except Exception as exc:
            log(f"Global hotkey unavailable: {exc}", "warn")

    def hide_window(self):
        if self.tray:
            self.root.withdraw()
        else:
            self.root.iconify()

    def show_window(self):
        self.root.deiconify(); self.root.lift(); self.root.focus_force()

    def on_close(self):
        if self.tray:
            self.root.withdraw()
            log("Window hidden. Air Mouse keeps running in the tray.")
        else:
            self.quit()

    def quit(self):
        save_settings()
        try: self.stop_engine()
        except Exception: pass
        try: self.inj.up()
        except Exception: pass
        for obj in (self.tray, self.hk):
            try: obj.stop()
            except Exception: pass
        self.root.destroy()

    # ---- UI refresh ----------------------------------------------------------
    def tick(self):
        try:
            while True:
                msg = self.ui_q.get_nowait()
                if msg[0] == "show": self.show_window()
                elif msg[0] == "toggle": self.toggle_engine()
                elif msg[0] == "quit": return self.quit()
                elif msg[0] == "cams":
                    self.cam_cb.configure(values=msg[1] or ["0"])
                    log("Cameras found: " + (", ".join(msg[1]) or "none"))
        except queue.Empty:
            pass
        while _log_new:
            self.append_log(*_log_new.popleft())

        s = self.estate
        visible = self.root.state() in ("normal", "zoomed")
        self.want_preview = bool(visible and s == "on" and not S["hide_preview"] and self.page == "tracking")
        on = s in ("on", "starting")
        st = self.stats
        if visible and self.page == "tracking":
            self.stat["fps"].configure(text=str(st[0]) if st else "--")
            self.stat["inf"].configure(text=str(st[1]) if st else "--")
            if time.monotonic() - self._battery_t > 30:
                self._battery_t = time.monotonic(); self.stat["bat"].configure(text=battery_text())

        status = {"off": "Off. Camera is not in use.", "starting": "Starting...", "error": "Could not start",
                  "on": ("Tracking" + (f" at {st[0]} fps" if st else "")) + (" (preview hidden)" if S["hide_preview"] else "")}[s]
        self.status.configure(text=status, text_color=ACCENT if s == "on" else DANGER if s == "error" else MUTED)
        self.chip.configure(text="\u25CF  " + {"on": "Tracking", "starting": "Starting", "error": "Error", "off": "Off"}[s],
                            text_color={"on": ACCENT, "starting": WARN, "error": DANGER, "off": MUTED}[s])
        if bool(self.power.get()) != on:
            (self.power.select if on else self.power.deselect)()
        self.hide_btn.configure(text="Show preview" if S["hide_preview"] else "Hide preview", state="normal" if on else "disabled")
        self.next_btn.configure(state="normal" if s == "on" else "disabled")

        if s == "on" and not S["hide_preview"]:
            if self.frame is not None and visible and self.page == "tracking":
                self.draw_frame(self.frame)
        else:
            msg = {"off": "Camera is off.\nTurn on hand tracking to control the cursor.",
                   "starting": "Starting camera...", "error": self.eerr or "Camera unavailable.",
                   "on": "Preview hidden.\nThe camera is still tracking."}[s]
            if self.photo is not None or self.pv_lbl.cget("text") != msg:
                self.pv_lbl.config(image="", text=msg, fg=DANGER if s == "error" else DIM)
                self.photo = None
        self.root.after(33, self.tick)

    def draw_frame(self, fr):
        import cv2
        h, w = fr.shape[:2]
        bw, bh = max(self.pv.winfo_width() - 6, 120), max(self.pv.winfo_height() - 6, 90)
        scale = min(bw / w, bh / h)
        small = cv2.resize(fr, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".png", small)
        if ok:
            self.photo = self.tk.PhotoImage(data=base64.b64encode(buf.tobytes()))
            self.pv_lbl.config(image=self.photo, text="")

    def run(self):
        self.root.mainloop()


# =============================================================================
# Entry point
# =============================================================================
def single_instance():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser(description="Air Mouse for PC")
    ap.add_argument("--minimized", action="store_true", help="start hidden in the tray")
    ap.add_argument("--setup", action="store_true", help="install dependencies and exit")
    ap.add_argument("--no-admin", action="store_true", help="do not ask for administrator rights")
    ap.add_argument("--install-startup", action="store_true", help="start automatically with Windows")
    ap.add_argument("--remove-startup", action="store_true", help="remove the autostart entry")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if args.remove_startup:
        set_autostart(False); say("Autostart removed."); return
    if args.install_startup:
        say("Autostart enabled." if set_autostart(True) else "Could not enable autostart."); return
    if args.setup:
        bootstrap(force=True); return

    bootstrap()
    setup_logging(args.debug)

    # Ask for administrator rights once (UAC). Declining just continues without them.
    if (sys.platform == "win32" and S["admin"] and not args.no_admin and not args.minimized and not is_admin()):
        if relaunch_as_admin():
            return
        log("Administrator rights were declined; continuing without them.", "warn")

    if sys.platform == "win32":          # real pixel coordinates on scaled (125% / 150%) displays
        try: ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try: ctypes.windll.user32.SetProcessDPIAware()
            except Exception: pass

    guard = single_instance()
    if guard is None:
        say("Air Mouse is already running (check the system tray).")
        try:
            import tkinter
            from tkinter import messagebox
            r = tkinter.Tk(); r.withdraw()
            messagebox.showinfo("Air Mouse", "Air Mouse is already running.\nLook for it in the system tray."); r.destroy()
        except Exception:
            pass
        return
    App(start_hidden=args.minimized).run()


if __name__ == "__main__":
    main()
