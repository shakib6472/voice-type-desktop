"""
Voice Bridge - system wide voice typing for Windows, optimized for Bangla.

How it works:
  1. This script serves a tiny local page at http://127.0.0.1:8756/
  2. You open that page in Chrome. The page uses Chrome's built in Web Speech
     API (the same Google engine the "Voice In" extension uses) to recognize
     speech. Recognition quality for bn-BD is therefore identical to Voice In.
  3. The page POSTs recognized text back to this script.
  4. This script injects the text into your target window using SendInput with
     KEYEVENTF_UNICODE. No clipboard, no Enter key, so nothing gets sent by
     accident.

The target window is whatever app you last used before switching to Voice
Bridge. If Voice Bridge itself has focus when text arrives, focus is handed
back to the target first, so you can watch the mic window while dictating.

Requirements: Windows 10/11, Python 3.9+, Google Chrome or Edge.
No pip packages needed. Standard library only.
"""

import ctypes
import io
import json
import os
import subprocess
import queue
import sys
import threading
import time
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ============================== CONFIG ==============================

PORT = 8756

# Recognition language. bn-BD = Bangla (Bangladesh). bn-IN also works.
DEFAULT_LANG = "bn-BD"

# Second language, toggled with the language hotkey.
ALT_LANG = "en-US"

# Global hotkeys. Write them as text, for example:
#   "ctrl+alt+m"   "ctrl+shift+d"   "alt+f9"   "win+alt+space"
#   "pause"        "scrolllock"     "f9"
# Single keys like "pause" or "scrolllock" almost never clash with anything.
HOTKEY_TOGGLE = "ctrl+alt+m"   # start / stop dictation
HOTKEY_LANG = "ctrl+alt+l"     # switch between DEFAULT_LANG and ALT_LANG

# When Voice Bridge has focus, hand focus back to the target window before
# typing. Turn off if you would rather nothing be typed in that situation.
RESTORE_FOCUS = True

# Type partial results while you are still speaking, then correct them.
# Feels faster, but Bangla interim results change a lot, so you will see
# characters being erased and rewritten. Try False first.
TYPE_INTERIM = False

# Add a space after every finished phrase.
ADD_TRAILING_SPACE = True

# Number of characters sent to Windows per SendInput call.
CHUNK_SIZE = 100

# Milliseconds to wait between chunks. Raise to 5 or 10 if a slow app
# (some Electron apps) drops characters.
CHUNK_DELAY_MS = 0

# Open the recognizer window automatically on startup.
AUTO_OPEN_WINDOW = True

# Width and height of the Voice Bridge window, in pixels.
WINDOW_SIZE = (300, 110)

# Closing the Voice Bridge window quits Voice Bridge. Because the Start Menu
# shortcut runs without a console, this is the only way to stop it. A page
# reload also drops the connection briefly, so wait this long before acting.
QUIT_GRACE_SEC = 6

# Windows whose title contains this are treated as Voice Bridge's own, and
# are never used as a typing target.
WINDOW_TITLE_MARKER = "Voice Bridge"

# ====================================================================

if sys.platform != "win32":
    sys.exit("Voice Bridge only runs on Windows.")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "voicebridge.log")


def log(message):
    """Print when a console exists, and always keep a copy on disk.

    The Start Menu shortcut runs pythonw.exe, which has no console at all,
    so the log file is the only place messages can be read afterwards.
    """
    line = time.strftime("%H:%M:%S  ") + str(message)
    if sys.stdout is not None:
        try:
            print(line)
        except Exception:
            pass
    try:
        with io.open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def fatal(message):
    """Report a startup failure even when there is no console to print to."""
    log("FATAL  " + message)
    user32.MessageBoxW(None, message, "Voice Bridge", 0x10)
    sys.exit(1)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_BACK = 0x08
SW_RESTORE = 9

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class INPUT_UNION(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT))


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = (("type", wintypes.DWORD), ("u", INPUT_UNION))


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.BringWindowToTop.argtypes = (wintypes.HWND,)
user32.IsWindow.argtypes = (wintypes.HWND,)
user32.IsIconic.argtypes = (wintypes.HWND,)
user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)


# --------------------------- keyboard input ---------------------------

def _unicode_events(code):
    down = INPUT(type=INPUT_KEYBOARD)
    down.ki = KEYBDINPUT(wVk=0, wScan=code, dwFlags=KEYEVENTF_UNICODE, time=0, dwExtraInfo=0)
    up = INPUT(type=INPUT_KEYBOARD)
    up.ki = KEYBDINPUT(
        wVk=0, wScan=code, dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, time=0, dwExtraInfo=0
    )
    return down, up


def _vk_events(vk):
    down = INPUT(type=INPUT_KEYBOARD)
    down.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=0, time=0, dwExtraInfo=0)
    up = INPUT(type=INPUT_KEYBOARD)
    up.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0, dwExtraInfo=0)
    return down, up


def _send(events):
    if not events:
        return
    array = (INPUT * len(events))(*events)
    sent = user32.SendInput(len(events), array, ctypes.sizeof(INPUT))
    if sent != len(events):
        code = ctypes.get_last_error()
        raise OSError(
            "Windows blocked the keystrokes (error %d). This usually means the "
            "target app runs as administrator. Run Voice Bridge as administrator too." % code
        )


def type_text(text):
    if not text:
        return
    units = text.encode("utf-16-le")
    codes = [units[i] | (units[i + 1] << 8) for i in range(0, len(units), 2)]
    for start in range(0, len(codes), CHUNK_SIZE):
        events = []
        for code in codes[start:start + CHUNK_SIZE]:
            down, up = _unicode_events(code)
            events.append(down)
            events.append(up)
        _send(events)
        if CHUNK_DELAY_MS:
            time.sleep(CHUNK_DELAY_MS / 1000.0)


def press_backspace(count):
    if count <= 0:
        return
    for start in range(0, count, CHUNK_SIZE):
        events = []
        for _ in range(min(CHUNK_SIZE, count - start)):
            down, up = _vk_events(VK_BACK)
            events.append(down)
            events.append(up)
        _send(events)


# --------------------------- window tracking ---------------------------

def window_title(hwnd):
    if not hwnd:
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def is_own_window(hwnd):
    return WINDOW_TITLE_MARKER in window_title(hwnd)


target = {"hwnd": None, "title": ""}


def watch_foreground():
    """Remember the last real app the user was working in."""
    while True:
        try:
            hwnd = user32.GetForegroundWindow()
            title = window_title(hwnd)
            if hwnd and title and not is_own_window(hwnd):
                if hwnd != target["hwnd"] or title != target["title"]:
                    target["hwnd"] = hwnd
                    target["title"] = title
                    broadcast({"cmd": "target", "title": title})
        except Exception:
            pass
        time.sleep(0.25)


def force_focus(hwnd):
    """Give keyboard focus back to a window we do not own."""
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)

    current = user32.GetForegroundWindow()
    if current == hwnd:
        return True

    this_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(current, None)
    target_tid = user32.GetWindowThreadProcessId(hwnd, None)

    attached_fg = user32.AttachThreadInput(this_tid, fg_tid, True) if fg_tid else False
    attached_tg = user32.AttachThreadInput(this_tid, target_tid, True) if target_tid else False
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached_fg:
            user32.AttachThreadInput(this_tid, fg_tid, False)
        if attached_tg:
            user32.AttachThreadInput(this_tid, target_tid, False)

    time.sleep(0.06)
    return user32.GetForegroundWindow() == hwnd


# --------------------------- typing pipeline ---------------------------

type_lock = threading.Lock()
pending_interim_len = 0


def prepare_target():
    """Make sure a non Voice Bridge window has focus. Returns (ok, message)."""
    current = user32.GetForegroundWindow()
    if current and not is_own_window(current):
        return True, window_title(current)

    if not RESTORE_FOCUS:
        return False, "Voice Bridge is focused. Click into your app."

    hwnd = target["hwnd"]
    if not hwnd or not user32.IsWindow(hwnd):
        return False, "No target yet. Click into the app you want to type in."

    if not force_focus(hwnd):
        return False, "Could not switch to " + (target["title"] or "the target window")

    return True, target["title"]


def handle_text(text, is_final):
    global pending_interim_len

    if not is_final and not TYPE_INTERIM:
        return {"result": "ignored"}

    with type_lock:
        ok, message = prepare_target()
        if not ok:
            pending_interim_len = 0
            log("  ! " + message)
            return {"result": "blocked", "message": message}

        try:
            if is_final:
                press_backspace(pending_interim_len)
                pending_interim_len = 0
                out = text.strip()
                if not out:
                    return {"result": "empty"}
                if ADD_TRAILING_SPACE and not out.endswith(" "):
                    out += " "
                type_text(out)
                log("  -> [%s] %s" % (message, out.strip()))
                return {"result": "typed", "message": message}

            press_backspace(pending_interim_len)
            out = text.strip()
            type_text(out)
            pending_interim_len = len(out)
            return {"result": "interim", "message": message}

        except Exception as exc:
            pending_interim_len = 0
            msg = str(exc) or exc.__class__.__name__
            log("  ! " + msg)
            return {"result": "error", "message": msg}


def reset_interim():
    global pending_interim_len
    with type_lock:
        pending_interim_len = 0


# --------------------------- SSE broadcast ---------------------------

sse_clients = []
sse_lock = threading.Lock()


def broadcast(payload):
    data = json.dumps(payload)
    with sse_lock:
        clients = list(sse_clients)
    for q in clients:
        q.put(data)


# --------------------------- shutdown watchdog ---------------------------

def watch_window():
    """Quit once the Voice Bridge window is gone for good."""
    seen = False
    empty_since = None
    while True:
        with sse_lock:
            count = len(sse_clients)
        if count:
            seen = True
            empty_since = None
        elif seen:
            if empty_since is None:
                empty_since = time.monotonic()
            elif time.monotonic() - empty_since > QUIT_GRACE_SEC:
                log("Voice Bridge window closed. Quitting.")
                time.sleep(0.1)
                os._exit(0)
        time.sleep(1.0)


# --------------------------- HTTP server ---------------------------

INDEX_PATH = os.path.join(BASE_DIR, "recognizer.html")
ICON_PATH = os.path.join(BASE_DIR, "favicon.ico")
PNG_PATH = os.path.join(BASE_DIR, "icon.png")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send_bytes(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self._send_bytes(404, b"not found", "text/plain; charset=utf-8")
            return
        self._send_bytes(200, body, ctype)

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            self._send_file(INDEX_PATH, "text/html; charset=utf-8")
            return

        if path == "/favicon.ico":
            self._send_file(ICON_PATH, "image/x-icon")
            return

        if path == "/icon.png":
            self._send_file(PNG_PATH, "image/png")
            return

        if path == "/config":
            body = json.dumps({
                "defaultLang": DEFAULT_LANG,
                "altLang": ALT_LANG,
                "typeInterim": TYPE_INTERIM,
                "hotkeyToggle": HOTKEY_TOGGLE,
                "hotkeyLang": HOTKEY_LANG,
                "target": target["title"],
            }).encode("utf-8")
            self._send_bytes(200, body, "application/json; charset=utf-8")
            return

        if path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            q = queue.Queue()
            with sse_lock:
                sse_clients.append(q)
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        data = q.get(timeout=15)
                        self.wfile.write(("data: " + data + "\n\n").encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                with sse_lock:
                    if q in sse_clients:
                        sse_clients.remove(q)
            return

        self._send_bytes(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            self._send_bytes(400, b"bad json", "text/plain; charset=utf-8")
            return

        if path == "/text":
            result = handle_text(data.get("text", ""), bool(data.get("final")))
            self._send_bytes(200, json.dumps(result).encode("utf-8"),
                             "application/json; charset=utf-8")
            return

        if path == "/state":
            listening = bool(data.get("listening"))
            lang = data.get("lang", "")
            if not listening:
                reset_interim()
            line = ("  LISTENING  " if listening else "  stopped    ") + lang
            if listening:
                line += "   target: " + (target["title"] or "none")
            log(line)
            self._send_bytes(200, b'{"ok":true}', "application/json; charset=utf-8")
            return

        self._send_bytes(404, b"not found", "text/plain; charset=utf-8")


# --------------------------- hotkeys ---------------------------

NAMED_KEYS = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27,
    "down": 0x28, "pause": 0x13, "scrolllock": 0x91, "capslock": 0x14,
    "numlock": 0x90, "printscreen": 0x2C, "apps": 0x5D, "menu": 0x5D,
    "`": 0xC0, "backtick": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD,
    "\\": 0xDC, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}


def parse_hotkey(spec):
    """Turn 'ctrl+alt+m' into (modifier flags, virtual key code)."""
    mods = MOD_NOREPEAT
    key = None
    for part in spec.lower().split("+"):
        part = part.strip()
        if not part:
            continue
        if part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part in ("win", "windows"):
            mods |= MOD_WIN
        elif part in NAMED_KEYS:
            key = NAMED_KEYS[part]
        elif part[0] == "f" and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            key = 0x70 + int(part[1:]) - 1
        elif len(part) == 1 and (part.isalpha() or part.isdigit()):
            key = ord(part.upper())
        else:
            raise ValueError("Unknown key in hotkey: " + part)
    if key is None:
        raise ValueError("No main key in hotkey: " + spec)
    return mods, key


def hotkey_loop():
    plan = ((1, HOTKEY_TOGGLE, "start/stop"), (2, HOTKEY_LANG, "language"))
    for hotkey_id, spec, label in plan:
        try:
            mods, key = parse_hotkey(spec)
        except ValueError as exc:
            log("  ! " + str(exc))
            continue
        if not user32.RegisterHotKey(None, hotkey_id, mods, key):
            log("  ! Hotkey '%s' (%s) is already taken by another app. "
                "Edit the CONFIG block and restart." % (spec, label))

    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == WM_HOTKEY:
            if msg.wParam == 1:
                broadcast({"cmd": "toggle"})
            elif msg.wParam == 2:
                broadcast({"cmd": "lang"})
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


# --------------------------- startup ---------------------------

def find_browser():
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def open_window():
    url = "http://127.0.0.1:%d/" % PORT
    browser = find_browser()
    profile = os.path.join(os.path.expanduser("~"), ".voice-bridge-profile")
    if browser:
        # --use-fake-ui-for-media-stream would auto-accept the microphone, but
        # Chrome then shows an "unsupported command-line flag" banner on every
        # launch. Allowing the mic once is cleaner; the dedicated profile
        # remembers it forever.
        subprocess.Popen([
            browser,
            "--app=" + url,
            "--window-size=%d,%d" % WINDOW_SIZE,
            "--user-data-dir=" + profile,
            "--no-first-run",
            "--no-default-browser-check",
        ])
    else:
        import webbrowser
        webbrowser.open(url)


def main():
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        fatal("Voice Bridge could not start on port %d.\n\nIt is probably "
              "already running. Check the taskbar for the Voice Bridge "
              "window.\n\n%s" % (PORT, exc))
        return
    server.daemon_threads = True

    log("Voice Bridge  http://127.0.0.1:%d/" % PORT)
    log("  start / stop      %s" % HOTKEY_TOGGLE)
    log("  switch language   %s" % HOTKEY_LANG)
    log("  languages         %s  /  %s" % (DEFAULT_LANG, ALT_LANG))
    log("  close the Voice Bridge window to quit")

    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=watch_foreground, daemon=True).start()

    if AUTO_OPEN_WINDOW:
        time.sleep(0.4)
        open_window()
        threading.Thread(target=watch_window, daemon=True).start()

    try:
        hotkey_loop()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        log(traceback.format_exc())
        fatal("Voice Bridge stopped unexpectedly.\n\nDetails are in\n" + LOG_PATH)
