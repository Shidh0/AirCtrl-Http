#!/usr/bin/env python3
"""
Air Mouse host (PC side).

Serves the phone web app (index.html) over HTTP and receives mouse/keyboard
commands over a WebSocket. Every connection must authenticate with the PIN
stored in config.json before any command is accepted.

Usage:
    python control.py                 start the server
    python control.py --show-pin      print the PIN and the phone pairing link
    python control.py --reset-pin     generate a new PIN
    python control.py --no-pin        start WITHOUT asking for a PIN (this run only)
    python control.py --pin-off       never ask for a PIN again (saved in config.json)
    python control.py --pin-on        ask for the PIN again
    python control.py --debug         verbose logging
"""
import argparse
import asyncio
import atexit
import ctypes
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from urllib.parse import urlsplit

VERSION = "2.1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
INDEX_PATH = os.path.join(BASE_DIR, "index.html")

DEFAULT_CONFIG = {"http_port": 8000, "ws_port": 9999, "token": "", "require_pin": True}

# --- limits -----------------------------------------------------------------
MAX_CLIENTS = 4              # simultaneous authenticated phones
AUTH_TIMEOUT = 5.0           # seconds a new socket has to send its PIN
MAX_MESSAGE_BYTES = 4096     # largest accepted WebSocket frame
RATE_PER_SEC = 400           # sustained commands/sec per client
RATE_BURST = 800
QUEUE_MAX = 1000
MAX_TEXT_LEN = 200           # characters per "type" command
MAX_HOTKEY_KEYS = 4
AUTH_MAX_FAILURES = 5        # failures before an IP is locked out
AUTH_LOCKOUT_SECONDS = 300
STATS_INTERVAL = 600         # seconds between summary log lines

TOKEN_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L
TOKEN_LENGTH = 8

# Filled in by load_dependencies() so that --show-pin works without a display.
websockets = None
mouse_ctl = None
kb_ctl = None
KEY_MAP = {}
BUTTON_MAP = {}


# =============================================================================
# Logging
# =============================================================================
class SafeRotatingFileHandler(RotatingFileHandler):
    """Rotating handler that keeps logging even if rotation fails.

    On Windows rotation raises PermissionError when another process (e.g. a
    second instance or a text editor) holds the file open. Plain
    RotatingFileHandler then loses every following record.
    """

    _retry_after = 0.0

    def shouldRollover(self, record):
        if time.time() < self._retry_after:
            return False
        return super().shouldRollover(record)

    def doRollover(self):
        try:
            super().doRollover()
        except Exception:
            self._retry_after = time.time() + 60
            try:
                if self.stream is None:
                    self.stream = self._open()
            except Exception:
                pass


def _pick_log_path():
    for directory in (BASE_DIR, os.path.join(tempfile.gettempdir(), "AirMouse")):
        try:
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, "server.log")
            with open(path, "a", encoding="utf-8"):
                pass
            return path
        except OSError:
            continue
    return None


def setup_logging(debug=False):
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    log_path = _pick_log_path()
    if log_path:
        fh = SafeRotatingFileHandler(
            log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)

    # pythonw.exe has no console: sys.stderr is None there.
    stream = sys.stderr
    if stream is not None and hasattr(stream, "isatty") and stream.isatty():
        ch = logging.StreamHandler(stream)
        ch.setFormatter(fmt)
        root.addHandler(ch)

    logging.raiseExceptions = False      # a logging failure must never crash the host
    logging.captureWarnings(True)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    def _excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        logging.critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    def _thread_hook(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        logging.critical(
            "Uncaught exception in thread %s", name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _excepthook
    threading.excepthook = _thread_hook
    atexit.register(lambda: logging.info("Air Mouse host stopped"))
    return log_path


_limited_last = {}


def log_limited(key, level, msg, *args, interval=10.0):
    """Log at most once per `interval` seconds per key (stops error floods)."""
    now = time.monotonic()
    last, suppressed = _limited_last.get(key, (0.0, 0))
    if now - last < interval:
        _limited_last[key] = (last, suppressed + 1)
        return
    if suppressed:
        msg = f"{msg} (+{suppressed} similar suppressed)"
    _limited_last[key] = (now, 0)
    logging.log(level, msg, *args)


def safe(value, limit=60):
    """Printable, truncated repr of untrusted input for log lines."""
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


# =============================================================================
# Config / PIN
# =============================================================================
def generate_token():
    return "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))


def normalize_token(value):
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def format_token(token):
    return f"{token[:4]}-{token[4:]}"


def _write_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, CONFIG_PATH)


def parse_bool(value, default=True):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("0", "false", "off", "no", "n"):
            return False
        if text in ("1", "true", "on", "yes", "y"):
            return True
    return default


def set_pin_required(required):
    """Persist the 'ask for a PIN' switch in config.json."""
    cfg = load_config()
    cfg["require_pin"] = bool(required)
    _write_config(cfg)
    return cfg


def load_config(reset_token=False):
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            cfg.update(loaded)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        logging.warning("config.json unreadable (%s); using defaults", exc)

    for key in ("http_port", "ws_port"):
        try:
            port = int(cfg[key])
            if not 1 <= port <= 65535:
                raise ValueError
            cfg[key] = port
        except (TypeError, ValueError):
            cfg[key] = DEFAULT_CONFIG[key]

    token = normalize_token(cfg.get("token", ""))
    changed = False
    if reset_token or len(token) != TOKEN_LENGTH or any(c not in TOKEN_ALPHABET for c in token):
        token = generate_token()
        changed = True
    cfg["token"] = token
    cfg["require_pin"] = parse_bool(cfg.get("require_pin", True), True)
    if changed or not os.path.exists(CONFIG_PATH):
        try:
            _write_config(cfg)
        except OSError as exc:
            logging.error("Could not save config.json: %s", exc)
    return cfg


# =============================================================================
# Networking helpers
# =============================================================================
CGNAT = ipaddress.ip_network("100.64.0.0/10")  # Tailscale and similar


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))     # no packet is sent for UDP connect
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def ip_allowed(ip_text):
    """Only LAN, loopback and Tailscale clients may talk to the host."""
    try:
        ip = ipaddress.ip_address(ip_text.split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip in CGNAT)


def enable_dpi_awareness():
    """Use real pixels so the cursor lands exactly where the phone says on scaled displays."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def get_screen_resolution():
    try:
        user32 = ctypes.windll.user32
        return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    except Exception:
        return 1920, 1080


SCREEN = {"w": 1920, "h": 1080}


# =============================================================================
# Command validation
# =============================================================================
def _num(value, lo, hi):
    n = float(value)
    if not math.isfinite(n):
        raise ValueError("not finite")
    return int(max(lo, min(hi, n)))


def _fnum(value, lo, hi):
    n = float(value)
    if not math.isfinite(n):
        raise ValueError("not finite")
    return max(lo, min(hi, n))


def resolve_key(name):
    if not isinstance(name, str):
        return None
    if name in KEY_MAP:
        return KEY_MAP[name]
    if len(name) == 1:
        return name
    return None


def sanitize_command(data):
    """Return a clean command dict, or None if the message is invalid."""
    if not isinstance(data, dict):
        return None
    cmd = data.get("cmd")
    try:
        if cmd == "move":
            return {"cmd": "move",
                    "x": _num(data["x"], 0, SCREEN["w"] - 1),
                    "y": _num(data["y"], 0, SCREEN["h"] - 1)}
        if cmd == "move_rel":
            return {"cmd": "move_rel",
                    "dx": _num(data.get("dx", 0), -2000, 2000),
                    "dy": _num(data.get("dy", 0), -2000, 2000)}
        if cmd in ("click", "mousedown", "mouseup"):
            button = data.get("button", "left")
            if button not in BUTTON_MAP:
                return None
            return {"cmd": cmd, "button": button}
        if cmd == "doubleclick":
            return {"cmd": "doubleclick"}
        if cmd == "scroll":      # amounts are wheel notches and may be fractional (smooth scrolling)
            return {"cmd": "scroll",
                    "amount": _fnum(data.get("amount", 0), -50, 50),
                    "h": _fnum(data.get("h", 0), -50, 50)}
        if cmd == "zoom":        # Ctrl + wheel (pinch to zoom)
            return {"cmd": "zoom", "amount": _fnum(data.get("amount", 0), -20, 20)}
        if cmd == "press":
            key = resolve_key(data.get("key"))
            return None if key is None else {"cmd": "press", "key": key}
        if cmd == "hotkey":
            names = data.get("keys")
            if not isinstance(names, list) or not 1 <= len(names) <= MAX_HOTKEY_KEYS:
                return None
            keys = [resolve_key(k) for k in names]
            return None if any(k is None for k in keys) else {"cmd": "hotkey", "keys": keys}
        if cmd == "type":
            text = data.get("text", "")
            if not isinstance(text, str):
                return None
            text = "".join(c for c in text[:MAX_TEXT_LEN] if c.isprintable() or c in "\n\t")
            return {"cmd": "type", "text": text} if text else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return None


# =============================================================================
# Command queue + worker
# =============================================================================
class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.received = self.executed = self.dropped = self.rejected = self.errors = 0

    def add(self, name, n=1):
        with self._lock:
            setattr(self, name, getattr(self, name) + n)

    def snapshot(self):
        with self._lock:
            return (self.received, self.executed, self.dropped, self.rejected, self.errors)


STATS = Stats()
DROPPABLE = {"move", "move_rel", "scroll", "zoom"}


class CommandQueue:
    """FIFO that merges consecutive motion commands without reordering."""

    def __init__(self, maxlen=QUEUE_MAX):
        self._dq = deque()
        self._cv = threading.Condition()
        self._maxlen = maxlen

    def put(self, cmd):
        name = cmd["cmd"]
        with self._cv:
            if self._dq:
                last = self._dq[-1]
                if last["cmd"] == name:
                    if name == "move":
                        self._dq[-1] = cmd
                        self._cv.notify()
                        return
                    if name == "move_rel":
                        last["dx"] = max(-5000, min(5000, last["dx"] + cmd["dx"]))
                        last["dy"] = max(-5000, min(5000, last["dy"] + cmd["dy"]))
                        return
                    if name == "scroll":
                        last["amount"] = max(-100, min(100, last["amount"] + cmd["amount"]))
                        last["h"] = max(-100, min(100, last["h"] + cmd["h"]))
                        return
                    if name == "zoom":
                        last["amount"] = max(-40, min(40, last["amount"] + cmd["amount"]))
                        return
            if len(self._dq) >= self._maxlen and name in DROPPABLE:
                STATS.add("dropped")
                return
            self._dq.append(cmd)
            self._cv.notify()

    def get(self):
        with self._cv:
            while not self._dq:
                self._cv.wait()
            return self._dq.popleft()


_mouse_event_ready = False


def _win_mouse_event(flags, data):
    """Raw wheel event so scrolling can be smaller than one full notch."""
    global _mouse_event_ready
    fn = ctypes.windll.user32.mouse_event
    if not _mouse_event_ready:
        fn.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_size_t]
        fn.restype = None
        _mouse_event_ready = True
    fn(flags, 0, 0, data & 0xFFFFFFFF, 0)


class InputWorker(threading.Thread):
    def __init__(self, queue_):
        super().__init__(name="input-worker", daemon=True)
        self._queue = queue_
        self.mouse = mouse_ctl.Controller()
        self.keyboard = kb_ctl.Controller()
        self._held = set()
        self._rem = [0.0, 0.0]          # fractional scroll carried over (non-Windows)

    def _wheel(self, vertical, horizontal=0.0):
        """Scroll by (possibly fractional) wheel notches. Positive = up / right."""
        if sys.platform == "win32":
            for amount, flag in ((vertical, 0x0800), (horizontal, 0x1000)):   # WHEEL, HWHEEL
                delta = int(round(amount * 120))
                if delta:
                    _win_mouse_event(flag, delta)
            return
        self._rem[0] += horizontal
        self._rem[1] += vertical
        dx, dy = int(self._rem[0]), int(self._rem[1])
        if dx or dy:
            self._rem[0] -= dx
            self._rem[1] -= dy
            self.mouse.scroll(dx, dy)

    def run(self):
        logging.info("Input worker started")
        while True:
            cmd = self._queue.get()
            try:
                self._execute(cmd)
                STATS.add("executed")
            except Exception as exc:
                STATS.add("errors")
                log_limited("worker", logging.ERROR, "Input error on %s: %s",
                            cmd.get("cmd"), exc, interval=10)

    def _execute(self, c):
        name = c["cmd"]
        if name == "move":
            self.mouse.position = (c["x"], c["y"])
        elif name == "move_rel":
            self.mouse.move(c["dx"], c["dy"])
        elif name == "click":
            self.mouse.click(BUTTON_MAP[c["button"]], 1)
        elif name == "doubleclick":
            self.mouse.click(mouse_ctl.Button.left, 2)
        elif name == "mousedown":
            btn = BUTTON_MAP[c["button"]]
            self.mouse.press(btn)
            self._held.add(btn)
        elif name == "mouseup":
            btn = BUTTON_MAP[c["button"]]
            self.mouse.release(btn)
            self._held.discard(btn)
        elif name == "scroll":
            self._wheel(c["amount"], c["h"])
        elif name == "zoom":
            self.keyboard.press(kb_ctl.Key.ctrl)
            try:
                self._wheel(c["amount"])
            finally:
                self.keyboard.release(kb_ctl.Key.ctrl)
        elif name == "press":
            self.keyboard.press(c["key"])
            self.keyboard.release(c["key"])
        elif name == "hotkey":
            pressed = []
            try:
                for k in c["keys"]:
                    self.keyboard.press(k)
                    pressed.append(k)
            finally:
                for k in reversed(pressed):    # never leave Ctrl/Alt stuck down
                    try:
                        self.keyboard.release(k)
                    except Exception:
                        pass
        elif name == "type":
            self.keyboard.type(c["text"])
        elif name == "release_all":
            for btn in list(self._held):
                try:
                    self.mouse.release(btn)
                except Exception:
                    pass
            self._held.clear()


# =============================================================================
# HTTP server (serves only index.html)
# =============================================================================
class AppHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"   # on Windows this would allow port hijacking

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        logging.error("HTTP error from %s", client_address[0], exc_info=True)


class AppHandler(BaseHTTPRequestHandler):
    server_version = "AirMouse"
    sys_version = ""
    timeout = 10

    def log_message(self, fmt, *args):
        logging.info("HTTP %s - %s", self.client_address[0], fmt % args)

    def _respond(self, code, body=b"", ctype="text/plain; charset=utf-8", head=False):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if not head and body:
                self.wfile.write(body)
        except (ConnectionError, TimeoutError):
            pass

    def _serve(self, head):
        if not ip_allowed(self.client_address[0]):
            logging.warning("HTTP request from non-local address %s refused", self.client_address[0])
            return self._respond(403, b"Forbidden", head=head)
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(INDEX_PATH, "rb") as f:
                    body = f.read()
            except OSError as exc:
                logging.error("Cannot read index.html: %s", exc)
                return self._respond(404, b"index.html not found next to control.py", head=head)
            return self._respond(200, body, "text/html; charset=utf-8", head)
        if path == "/favicon.ico":
            return self._respond(204, head=head)
        return self._respond(404, b"Not found", head=head)

    def do_GET(self):
        self._serve(False)

    def do_HEAD(self):
        self._serve(True)


# =============================================================================
# WebSocket server
# =============================================================================
class TokenBucket:
    def __init__(self, rate, burst):
        self.rate, self.burst = rate, burst
        self.tokens = float(burst)
        self.stamp = time.monotonic()

    def allow(self):
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class AppState:
    def __init__(self, cfg, queue_):
        self.cfg = cfg
        self.token = cfg["token"]
        self.require_pin = cfg.get("require_pin", True)
        self.queue = queue_
        self.clients = set()
        self.failures = {}       # ip -> [count, blocked_until]


STATE = None


def client_ip(ws):
    addr = getattr(ws, "remote_address", None)
    ip = addr[0] if addr else ""
    return ip[7:] if ip.startswith("::ffff:") else ip


def header(ws, name):
    req = getattr(ws, "request", None)            # websockets >= 13
    headers = getattr(req, "headers", None) if req is not None else None
    if headers is None:
        headers = getattr(ws, "request_headers", None)   # legacy API
    try:
        return headers.get(name) if headers is not None else None
    except Exception:
        return None


def origin_ok(ws):
    """Reject browser pages from other sites (cross-site WebSocket hijacking)."""
    origin = header(ws, "Origin")
    if not origin or origin == "null":
        return True
    try:
        origin_host = (urlsplit(origin).hostname or "").lower()
        host_header = (header(ws, "Host") or "").lower()
        host = host_header.rsplit(":", 1)[0].strip("[]") if ":" in host_header else host_header
    except ValueError:
        return False
    return origin_host in (host, "localhost", "127.0.0.1", "::1")


def auth_blocked(ip):
    entry = STATE.failures.get(ip)
    return bool(entry and entry[1] > time.monotonic())


def auth_failed(ip):
    now = time.monotonic()
    if len(STATE.failures) > 256:
        STATE.failures = {k: v for k, v in STATE.failures.items() if v[1] > now}
    entry = STATE.failures.setdefault(ip, [0, 0.0])
    entry[0] += 1
    if entry[0] >= AUTH_MAX_FAILURES:
        entry[0] = 0
        entry[1] = now + AUTH_LOCKOUT_SECONDS
        logging.warning("Too many wrong PINs from %s; blocked for %d s", ip, AUTH_LOCKOUT_SECONDS)


def check_auth(raw):
    """Return "ok", "empty" (no PIN was sent) or "bad"."""
    if not isinstance(raw, str) or len(raw) > 256:
        return "bad"
    try:
        msg = json.loads(raw)
    except ValueError:
        return "bad"
    if not isinstance(msg, dict) or msg.get("cmd") != "auth":
        return "bad"
    if not STATE.require_pin:        # PIN switched off: any local device may connect
        return "ok"
    candidate = normalize_token(msg.get("token", ""))[:64]
    if not candidate:
        return "empty"
    return "ok" if hmac.compare_digest(candidate.encode(), STATE.token.encode()) else "bad"


async def _close(ws, code, reason):
    try:
        await ws.close(code, reason)
    except Exception:
        pass


async def handle_websocket(ws):
    ip = client_ip(ws)
    if not ip_allowed(ip):
        logging.warning("WebSocket from non-local address %s refused", ip)
        return await _close(ws, 1008, "forbidden")
    if auth_blocked(ip):
        logging.info("Blocked client %s tried to connect", ip)
        return await _close(ws, 4429, "too many attempts")
    if not origin_ok(ws):
        logging.warning("Rejected %s: Origin %s does not match host", ip, safe(header(ws, "Origin")))
        return await _close(ws, 1008, "bad origin")
    if len(STATE.clients) >= MAX_CLIENTS:
        logging.warning("Rejected %s: too many clients", ip)
        return await _close(ws, 1013, "busy")

    authed = False
    started = time.monotonic()
    try:
        raw = await asyncio.wait_for(ws.recv(), AUTH_TIMEOUT)
        result = check_auth(raw)
        if result != "ok":
            if result == "bad" and STATE.require_pin:
                auth_failed(ip)
                logging.warning("Wrong PIN from %s", ip)
            else:      # the phone just has no PIN saved yet; do not count it as an attack
                logging.info("%s connected without a PIN", ip)
            try:
                await ws.send(json.dumps({"type": "auth_failed", "pin_required": STATE.require_pin}))
            except Exception:
                pass
            return await _close(ws, 4401, "bad pin")

        STATE.failures.pop(ip, None)
        authed = True
        STATE.clients.add(ws)
        SCREEN["w"], SCREEN["h"] = get_screen_resolution()
        await ws.send(json.dumps({"type": "init", "screen_w": SCREEN["w"],
                                  "screen_h": SCREEN["h"], "version": VERSION,
                                  "pin_required": STATE.require_pin}))
        logging.info("Client connected: %s (%d active)", ip, len(STATE.clients))

        bucket = TokenBucket(RATE_PER_SEC, RATE_BURST)
        async for message in ws:
            STATS.add("received")
            if not isinstance(message, str) or len(message) > MAX_MESSAGE_BYTES:
                STATS.add("rejected")
                continue
            if not bucket.allow():
                STATS.add("dropped")
                log_limited(("rate", ip), logging.WARNING, "Rate limit hit by %s", ip, interval=30)
                continue
            try:
                cmd = sanitize_command(json.loads(message))
            except ValueError:
                cmd = None
            if cmd is None:
                STATS.add("rejected")
                log_limited(("bad", ip), logging.WARNING,
                            "Ignored invalid command from %s: %s", ip, safe(message), interval=30)
                continue
            STATE.queue.put(cmd)
    except asyncio.TimeoutError:
        logging.warning("Client %s did not authenticate within %.0fs", ip, AUTH_TIMEOUT)
        await _close(ws, 4408, "auth timeout")
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception:
        logging.exception("Unexpected error with client %s", ip)
    finally:
        if authed:
            STATE.clients.discard(ws)
            STATE.queue.put({"cmd": "release_all"})     # no stuck mouse buttons
            logging.info("Client disconnected: %s after %.0fs (%d active)",
                         ip, time.monotonic() - started, len(STATE.clients))


async def stats_reporter():
    last = None
    while True:
        await asyncio.sleep(STATS_INTERVAL)
        snap = STATS.snapshot()
        if snap != last:
            logging.info("Stats: received=%d executed=%d dropped=%d rejected=%d errors=%d clients=%d",
                         *snap, len(STATE.clients))
            last = snap


async def ws_main(port):
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(
        lambda _l, ctx: logging.error("Asyncio: %s", ctx.get("message"),
                                      exc_info=ctx.get("exception")))
    asyncio.create_task(stats_reporter())
    started = False
    backoff = 1
    while True:
        try:
            async with websockets.serve(handle_websocket, "0.0.0.0", port,
                                        max_size=MAX_MESSAGE_BYTES, compression=None):
                if not started:
                    logging.info("WebSocket server listening on ws://0.0.0.0:%d", port)
                    started = True
                backoff = 1
                await asyncio.Future()
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            if not started:
                logging.critical("Cannot open WebSocket port %d: %s. Is Air Mouse already running?",
                                 port, exc)
                raise SystemExit(1)
            logging.error("WebSocket server error: %s", exc)
        except Exception:
            logging.exception("WebSocket server crashed")
        logging.info("Restarting WebSocket server in %ds", backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


# =============================================================================
# Startup
# =============================================================================
def load_dependencies():
    global websockets, mouse_ctl, kb_ctl, KEY_MAP, BUTTON_MAP
    try:
        import websockets as _ws
        import websockets.exceptions  # noqa: F401  (used in the handler)
        import pynput.mouse as _mouse
        import pynput.keyboard as _kb
    except Exception as exc:
        logging.critical("Missing or broken dependency: %s. Run Setup.py or "
                         "'%s -m pip install websockets pynput'", exc, sys.executable)
        raise SystemExit(1)
    websockets, mouse_ctl, kb_ctl = _ws, _mouse, _kb

    KEY_MAP = {
        "volumeup": kb_ctl.Key.media_volume_up,
        "volumedown": kb_ctl.Key.media_volume_down,
        "nexttrack": kb_ctl.Key.media_next,
        "prevtrack": kb_ctl.Key.media_previous,
        "playpause": kb_ctl.Key.media_play_pause,
        "mute": kb_ctl.Key.media_volume_mute,
        "f11": kb_ctl.Key.f11,
        "ctrl": kb_ctl.Key.ctrl,
        "alt": kb_ctl.Key.alt,
        "shift": kb_ctl.Key.shift,
        "win": kb_ctl.Key.cmd,
        "enter": kb_ctl.Key.enter,
        "backspace": kb_ctl.Key.backspace,
        "space": kb_ctl.Key.space,
        "esc": kb_ctl.Key.esc,
        "tab": kb_ctl.Key.tab,
        "up": kb_ctl.Key.up,
        "down": kb_ctl.Key.down,
        "left": kb_ctl.Key.left,
        "right": kb_ctl.Key.right,
        "delete": kb_ctl.Key.delete,
        "home": kb_ctl.Key.home,
        "end": kb_ctl.Key.end,
        "pageup": kb_ctl.Key.page_up,
        "pagedown": kb_ctl.Key.page_down,
        "f5": kb_ctl.Key.f5,
    }
    BUTTON_MAP = {
        "left": mouse_ctl.Button.left,
        "right": mouse_ctl.Button.right,
        "middle": mouse_ctl.Button.middle,
    }


def out(text=""):
    if sys.stdout is not None:      # None under pythonw.exe
        print(text)


def print_pairing(cfg):
    ip = get_local_ip()
    if not cfg.get("require_pin", True):
        out("PIN:  OFF  (anyone on your local network can connect)")
        out(f"Open on your phone:  http://{ip}:{cfg['http_port']}/")
        out("Turn the PIN back on with:  python control.py --pin-on")
        return
    out(f"PIN:  {format_token(cfg['token'])}")
    out(f"Open on your phone:  http://{ip}:{cfg['http_port']}/#pin={cfg['token']}")
    out("(The link fills in the PIN for you. Keep both private.)")


def main():
    parser = argparse.ArgumentParser(description="Air Mouse host")
    parser.add_argument("--show-pin", action="store_true", help="print the PIN and pairing link")
    parser.add_argument("--reset-pin", action="store_true", help="generate a new PIN")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--no-pin", action="store_true",
                        help="start without asking for a PIN (this run only)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--pin-off", action="store_true",
                       help="stop asking for a PIN, saved in config.json")
    group.add_argument("--pin-on", action="store_true", help="ask for the PIN again")
    args = parser.parse_args()

    if args.show_pin or args.reset_pin or args.pin_off or args.pin_on:
        cfg = load_config(reset_token=args.reset_pin)
        if args.pin_off or args.pin_on:
            cfg = set_pin_required(args.pin_on)
            out("PIN is now " + ("required." if args.pin_on else "OFF. Restart Air Mouse for it to take effect."))
        if args.reset_pin:
            out("New PIN generated. Restart Air Mouse for it to take effect.")
        print_pairing(cfg)
        return

    log_path = setup_logging(args.debug)
    logging.info("=" * 60)
    logging.info("Air Mouse host %s starting (Python %s, %s, pid %d)",
                 VERSION, sys.version.split()[0], sys.platform, os.getpid())
    logging.info("Log file: %s", log_path or "(none - could not create a log file)")

    cfg = load_config()
    if args.no_pin:
        cfg["require_pin"] = False
    if not cfg["require_pin"]:
        logging.warning("PIN is OFF: any device on your local network can control this PC.")
    enable_dpi_awareness()
    load_dependencies()

    SCREEN["w"], SCREEN["h"] = get_screen_resolution()
    logging.info("Screen resolution: %dx%d", SCREEN["w"], SCREEN["h"])

    try:
        http_server = AppHTTPServer(("0.0.0.0", cfg["http_port"]), AppHandler)
    except OSError as exc:
        logging.critical("Cannot open HTTP port %d: %s. Is Air Mouse already running?",
                         cfg["http_port"], exc)
        raise SystemExit(1)

    queue_ = CommandQueue()
    try:
        worker = InputWorker(queue_)
    except Exception:
        logging.critical("Cannot control mouse/keyboard", exc_info=True)
        raise SystemExit(1)

    global STATE
    STATE = AppState(cfg, queue_)

    worker.start()
    threading.Thread(target=http_server.serve_forever, name="http-server", daemon=True).start()

    ip = get_local_ip()
    if cfg["require_pin"]:
        logging.info("Phone web app: http://%s:%d  (run 'python control.py --show-pin' for the PIN)",
                     ip, cfg["http_port"])
    else:
        logging.info("Phone web app: http://%s:%d  (no PIN needed)", ip, cfg["http_port"])

    try:
        asyncio.run(ws_main(cfg["ws_port"]))
    except KeyboardInterrupt:
        logging.info("Stopped by user")
    finally:
        http_server.shutdown()


if __name__ == "__main__":
    main()
