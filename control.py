import asyncio
import json
import logging
import os
import queue
import socket
import sys
import threading
import ctypes
from http.server import HTTPServer, SimpleHTTPRequestHandler
import websockets
import pynput.mouse as mouse_ctl
import pynput.keyboard as kb_ctl

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.log")
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

mouse = mouse_ctl.Controller()
keyboard = kb_ctl.Controller()

cmd_queue = queue.Queue()

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
}

BUTTON_MAP = {
    "left": mouse_ctl.Button.left,
    "right": mouse_ctl.Button.right,
    "middle": mouse_ctl.Button.middle,
}

def get_screen_resolution():
    try:
        user32 = ctypes.windll.user32
        return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    except Exception:
        return 1920, 1080

def input_worker():
    while True:
        try:
            data = cmd_queue.get()
            
            if data.get("cmd") == "move":
                while not cmd_queue.empty():
                    try:
                        next_cmd = cmd_queue.get_nowait()
                        if next_cmd.get("cmd") == "move":
                            data = next_cmd
                            cmd_queue.task_done()
                        else:
                            cmd_queue.put(next_cmd)
                            break
                    except queue.Empty:
                        break

            cmd = data.get("cmd")

            if cmd == "move":
                mouse.position = (int(data["x"]), int(data["y"]))
            elif cmd == "move_rel":
                mouse.move(int(data.get("dx", 0)), int(data.get("dy", 0)))
            elif cmd == "click":
                btn = BUTTON_MAP.get(data.get("button", "left"), mouse_ctl.Button.left)
                mouse.click(btn, 1)
            elif cmd == "doubleclick":
                mouse.click(mouse_ctl.Button.left, 2)
            elif cmd == "mousedown":
                btn = BUTTON_MAP.get(data.get("button", "left"), mouse_ctl.Button.left)
                mouse.press(btn)
            elif cmd == "mouseup":
                btn = BUTTON_MAP.get(data.get("button", "left"), mouse_ctl.Button.left)
                mouse.release(btn)
            elif cmd == "scroll":
                mouse.scroll(0, int(data.get("amount", 0)))
            elif cmd == "press":
                key_str = data.get("key")
                target_key = KEY_MAP.get(key_str, key_str)
                keyboard.press(target_key)
                keyboard.release(target_key)
            elif cmd == "hotkey":
                keys = [KEY_MAP.get(k, k) for k in data.get("keys", [])]
                for k in keys:
                    keyboard.press(k)
                for k in reversed(keys):
                    keyboard.release(k)
                    
            cmd_queue.task_done()
        except Exception as e:
            logging.error(f"Worker Error: {e}")

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

class DirectoryHTTPHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        directory = os.path.dirname(os.path.abspath(__file__))
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, format, *args):
        logging.info(f"HTTP: {format % args}")

def run_http_server(port=8000):
    server = HTTPServer(('0.0.0.0', port), DirectoryHTTPHandler)
    logging.info(f"HTTP Server serving on http://0.0.0.0:{port}")
    server.serve_forever()

async def handle_websocket(websocket):
    client_ip = websocket.remote_address[0]
    logging.info(f"Client connected from {client_ip}")
    
    sw, sh = get_screen_resolution()
    try:
        await websocket.send(json.dumps({"type": "init", "screen_w": sw, "screen_h": sh}))
    except Exception:
        pass
    
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                cmd_queue.put_nowait(data) # Non-blocking queue insertion
            except Exception as e:
                logging.error(f"Command Error: {e}")

    except websockets.exceptions.ConnectionClosed:
        logging.info(f"Client disconnected ({client_ip})")

async def main_ws():
    logging.info("WebSocket Server serving on ws://0.0.0.0:9999")
    async with websockets.serve(handle_websocket, "0.0.0.0", 9999):
        await asyncio.Future()

if __name__ == "__main__":
    local_ip = get_local_ip()
    logging.info(f"Starting Air Mouse Host...")
    logging.info(f"Access Web App on Phone: http://{local_ip}:8000")
    
    threading.Thread(target=input_worker, daemon=True).start()

    threading.Thread(target=run_http_server, args=(8000,), daemon=True).start()

    try:
        asyncio.run(main_ws())
    except KeyboardInterrupt:
        logging.info("Server manually stopped.")