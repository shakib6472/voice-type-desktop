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
import re
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

# Type words while you are still speaking instead of waiting for the pause.
# Google keeps revising what it heard, so the tail of the line gets rewritten
# as you go. Only the characters that actually changed are retyped, and
# Backspace deletes exactly one character in both Windows and Chromium text
# boxes, Bangla conjuncts included. Set to False to go back to waiting for the
# full phrase, which is quieter but takes about two seconds longer.
TYPE_INTERIM = True

# Add a space after every finished phrase.
ADD_TRAILING_SPACE = True

# How long the text Voice Bridge typed stays trustworthy. After this much
# quiet the caret has probably moved, so those characters are never
# backspaced over again. This is what stops a stalled phrase from eating
# something you typed by hand minutes later.
TYPED_TEXT_STALE_SEC = 15

# Only type the part of an interim result that Google has stopped changing,
# and only up to the last finished word. Google rewrites the tail of what it
# heard several times a second, and typing every revision makes the line jump
# about. Set to False to type every revision the moment it arrives.
STABLE_INTERIM = True

# Google never punctuates Bangla. Voice Bridge finishes a phrase exactly where
# you stop speaking, which is usually where a daari belongs, so it puts one
# there. Set to False if you would rather add punctuation yourself.
ADD_END_MARK = True
END_MARK = {"bn": "।", "en": "."}

# End a phrase with a question mark when it reads like a question.
DETECT_QUESTIONS = True

# Words that ask something. The first group means a question wherever it
# appears. The second group only means one at the edges of the phrase, because
# "কি" in the middle of a sentence is usually not a question at all.
QUESTION_WORDS = {
    "bn": ("কেন", "কোথায়", "কোথা", "কখন", "কীভাবে", "কিভাবে", "কেমন",
           "কারা", "কাকে", "কতটা", "কতটুকু", "কতগুলো", "কোনটা", "কোনগুলো",
           "নাকি"),
    "en": (),
}
QUESTION_EDGE_WORDS = {
    "bn": ("কি", "কী", "কে", "কত", "কার", "কোন"),
    "en": ("what", "why", "how", "when", "where", "who", "whom", "which",
           "whose", "is", "are", "am", "was", "were", "do", "does", "did",
           "can", "could", "will", "would", "should", "shall", "may", "might",
           "have", "has", "had"),
}

# Put a comma in front of these when they join two halves of a sentence.
ADD_INNER_COMMAS = True
COMMA_BEFORE = {
    "bn": ("কিন্তু", "তবে", "যদিও", "তবুও", "অর্থাৎ", "কারণ"),
    "en": ("but", "however", "although", "because"),
}

# Say one of these words and the punctuation mark is typed instead of the word.
# Nothing here may produce Enter or a newline, so a chat message can never be
# sent by accident.
SPOKEN_PUNCTUATION = {
    # "দাড়ি" (beard) and "কমা" (to reduce) are left out on purpose: they are
    # ordinary words, and turning them into punctuation would quietly mangle
    # sentences like "তার দাড়ি অনেক বড়". Add them here if you want them.
    "bn": {
        "দাঁড়ি": "।", "পূর্ণচ্ছেদ": "।",
        "কমা চিহ্ন": ",", "প্রশ্নবোধক": "?", "প্রশ্ন চিহ্ন": "?",
        "বিস্ময়বোধক": "!", "বিস্ময় চিহ্ন": "!",
        "কোলন": ":", "সেমিকোলন": ";", "হাইফেন": "-",
    },
    "en": {
        "comma": ",", "full stop": ".", "period": ".",
        "question mark": "?", "exclamation mark": "!",
        "colon": ":", "semicolon": ";", "hyphen": "-",
    },
}

# Number of characters sent to Windows per SendInput call.
CHUNK_SIZE = 100

# Milliseconds to wait between chunks. Raise to 5 or 10 if a slow app
# (some Electron apps) drops characters.
CHUNK_DELAY_MS = 0

# Open the recognizer window automatically on startup.
AUTO_OPEN_WINDOW = True

# Size of the Voice Bridge window, in pixels. Chrome keeps about 36 pixels of
# this for its own title bar, so the page itself gets a little less. This
# applies the first time only: after that Chrome remembers whatever size you
# drag the window to, which is usually what you want.
WINDOW_SIZE = (250, 136)

# Closing the Voice Bridge window quits Voice Bridge. Because the Start Menu
# shortcut runs without a console, this is the only way to stop it. Seconds to
# wait after the window disappears before quitting, and how long to wait for
# the window to appear at all before giving up.
QUIT_GRACE_SEC = 2
STARTUP_WINDOW_TIMEOUT_SEC = 30

# Windows whose title contains this are treated as Voice Bridge's own, and
# are never used as a typing target.
WINDOW_TITLE_MARKER = "Voice Bridge"

# Seconds to wait for a copy that is shutting down before starting a new one.
# Closing the window and immediately clicking the shortcut lands here.
SINGLE_INSTANCE_WAIT_SEC = 35

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
user32.IsWindowVisible.argtypes = (wintypes.HWND,)

ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = (ENUM_WINDOWS_PROC, wintypes.LPARAM)

kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

ERROR_ALREADY_EXISTS = 183
SINGLE_INSTANCE_NAME = "Local\\VoiceBridge.SingleInstance"


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
        if CHUNK_DELAY_MS:
            time.sleep(CHUNK_DELAY_MS / 1000.0)


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


def find_own_window():
    """The browser window showing the Voice Bridge page, if it is open."""
    found = []

    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd) and is_own_window(hwnd):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(ENUM_WINDOWS_PROC(visit), 0)
    return found[0] if found else None


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


# --------------------------- one copy at a time ---------------------------

def claim_single_instance():
    """Make sure only one Voice Bridge runs at a time.

    Clicking the shortcut while Voice Bridge was already running used to start
    a second copy. Windows happily lets both bind port 8756, so the second one
    served a window whose Ctrl+Alt+M could never be registered, and pressing
    the hotkey did nothing. Now the second copy brings the first one's window
    to the front and quits.

    If the first copy is on its way out, which is what happens when you close
    the window and click the shortcut a second later, wait for it to finish
    and then take over.
    """
    deadline = time.monotonic() + SINGLE_INSTANCE_WAIT_SEC
    while True:
        handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_NAME)
        if handle and ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
            return handle
        if handle:
            kernel32.CloseHandle(handle)

        hwnd = find_own_window()
        if hwnd:
            log("Voice Bridge is already running. Showing its window.")
            force_focus(hwnd)
            return None

        if time.monotonic() >= deadline:
            fatal("Another copy of Voice Bridge is running but has no window.\n\n"
                  "End the pythonw.exe task in Task Manager and try again.")
            return None

        time.sleep(0.25)


# --------------------------- typing pipeline ---------------------------

type_lock = threading.Lock()

# What is currently on screen for the phrase being spoken, where it went, and
# when it was written. The timestamp matters: if nothing has been typed for a
# while, the caret has almost certainly moved and those characters must never
# be backspaced over, because by then they may be someone else's text.
typed_text = ""
typed_hwnd = None
typed_at = 0.0

# The last interim result, used to work out which part Google has settled on.
last_interim = ""

PUNCTUATION_CHARS = "।,.?!:;…-"


def language_family(lang):
    """bn-BD and bn-IN are both "bn"."""
    return (lang or DEFAULT_LANG).split("-")[0].lower()


def apply_spoken_punctuation(text, family):
    """Turn spoken words like "পূর্ণচ্ছেদ" or "comma" into the mark itself."""
    table = SPOKEN_PUNCTUATION.get(family)
    if not table:
        return text
    for phrase in sorted(table, key=len, reverse=True):
        text = re.sub(r"(?<!\S)" + re.escape(phrase) + r"(?!\S)", table[phrase], text)
    # Punctuation belongs against the word before it, never after a space.
    return re.sub(r"\s+([" + re.escape(PUNCTUATION_CHARS) + r"])", r"\1", text)


def words_of(text):
    return [w.strip(PUNCTUATION_CHARS) for w in text.split() if w.strip(PUNCTUATION_CHARS)]


def looks_like_question(text, family):
    """Does this phrase ask something?

    Some words mean a question wherever they appear. Others, "কি" above all,
    only do at the edges of the phrase, since in the middle they usually mean
    something else entirely.
    """
    words = words_of(text)
    if not words:
        return False
    if any(w in QUESTION_WORDS.get(family, ()) for w in words):
        return True
    edge = QUESTION_EDGE_WORDS.get(family, ())
    if words[0] in edge:
        return True
    return family != "en" and words[-1] in edge


def add_inner_commas(text, family):
    """A comma in front of the words that join two halves of a sentence."""
    joiners = COMMA_BEFORE.get(family, ())
    if not joiners:
        return text
    words = text.split()
    for i in range(1, len(words)):
        if words[i] in joiners and words[i - 1][-1:] not in tuple(PUNCTUATION_CHARS):
            words[i - 1] += ","
    return " ".join(words)


def finish_phrase(text, lang):
    """The finished form of a phrase: punctuation applied, daari added."""
    family = language_family(lang)
    out = apply_spoken_punctuation(text.strip(), family).strip()
    if not out:
        return ""
    if ADD_INNER_COMMAS:
        out = add_inner_commas(out, family)
    if ADD_END_MARK and out[-1] not in PUNCTUATION_CHARS:
        if DETECT_QUESTIONS and looks_like_question(out, family):
            out += "?"
        else:
            out += END_MARK.get(family, "")
    if ADD_TRAILING_SPACE:
        out += " "
    return out


def settled_part(text):
    """The part of an interim result Google has stopped changing.

    Only what the last two results agree on is typed, cut back to the last
    finished word. Google rewrites the tail of what it heard several times a
    second, and typing every revision is what made the line jump about.
    """
    global last_interim
    agreed = text[:common_prefix_len(last_interim, text)]
    last_interim = text
    cut = agreed.rfind(" ")
    return agreed[:cut] if cut > 0 else ""


def common_prefix_len(a, b):
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[i] == b[i]:
        i += 1
    return i


def retype(new_text):
    """Turn what is already on screen into new_text with as few keystrokes as
    possible, leaving the part that did not change alone.

    Backspace removes exactly one character in both Windows edit controls and
    Chromium text boxes, Bangla vowel signs and conjuncts included, so
    counting characters is safe here.
    """
    global typed_text, typed_at
    keep = common_prefix_len(typed_text, new_text)
    press_backspace(len(typed_text) - keep)
    type_text(new_text[keep:])
    typed_text = new_text
    typed_at = time.monotonic()


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


def handle_text(text, is_final, lang=None):
    global typed_text, typed_hwnd, last_interim

    if not is_final and not TYPE_INTERIM:
        return {"result": "ignored"}

    with type_lock:
        ok, message = prepare_target()
        if not ok:
            # Nothing was typed, so what is on screen is still exactly what
            # typed_text says. Forgetting it here would make the next write
            # repeat the whole phrase.
            log("  ! " + message)
            return {"result": "blocked", "message": message}

        # Two things make the remembered text untrustworthy: a different
        # window, and time. Either way we forget it rather than backspace over
        # characters that may no longer be ours.
        current = user32.GetForegroundWindow()
        if current != typed_hwnd:
            typed_text = ""
            typed_hwnd = current
        elif typed_text and time.monotonic() - typed_at > TYPED_TEXT_STALE_SEC:
            typed_text = ""

        try:
            if is_final:
                last_interim = ""
                out = finish_phrase(text, lang)
                if not out:
                    return {"result": "empty", "message": message}
                retype(out)
                typed_text = ""          # the phrase is committed
                log("  -> [%s] %s" % (message, out.strip()))
                return {"result": "typed", "message": message}

            settled = settled_part(text) if STABLE_INTERIM else text.strip()
            # Only write when there is something new. A shorter settled string
            # that still matches the screen means Google simply has not caught
            # up yet, and erasing back to it would be pure flicker.
            if settled and not typed_text.startswith(settled):
                retype(settled)
            return {"result": "interim", "message": message}

        except Exception as exc:
            # A write failed part way through, so what is on screen is now
            # anybody's guess. Forget it rather than backspace blindly.
            typed_text = ""
            last_interim = ""
            msg = str(exc) or exc.__class__.__name__
            log("  ! " + msg)
            return {"result": "error", "message": msg}


# --------------------------- SSE broadcast ---------------------------

sse_clients = []
sse_lock = threading.Lock()

# Things the user has to know about, such as a hotkey another app already
# owns. These used to go only to the log file, where nobody saw them.
warnings = []


def warn(message):
    log("  ! " + message)
    if message not in warnings:
        warnings.append(message)
    broadcast({"cmd": "warn", "message": message})


def broadcast(payload):
    data = json.dumps(payload)
    with sse_lock:
        clients = list(sse_clients)
    for q in clients:
        q.put(data)


# --------------------------- shutdown watchdog ---------------------------

def watch_window():
    """Quit once the Voice Bridge window is gone.

    This watches the window itself rather than the page's connection. A reload
    drops the connection for a moment but keeps the window, and a closed window
    is noticed straight away instead of whenever the dead connection is finally
    detected. That matters because clicking the shortcut right after closing
    the window has to find the old copy already gone.
    """
    seen = False
    gone_since = None
    waited = 0.0
    while True:
        if find_own_window():
            seen = True
            gone_since = None
        elif seen:
            if gone_since is None:
                gone_since = time.monotonic()
            elif time.monotonic() - gone_since > QUIT_GRACE_SEC:
                log("Voice Bridge window closed. Quitting.")
                time.sleep(0.1)
                os._exit(0)
        else:
            waited += 0.5
            if waited > STARTUP_WINDOW_TIMEOUT_SEC:
                log("The Voice Bridge window never opened. Quitting so the "
                    "next launch can start cleanly.")
                os._exit(1)
        time.sleep(0.5)


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
                "warnings": warnings,
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
            result = handle_text(data.get("text", ""), bool(data.get("final")),
                                 data.get("lang"))
            self._send_bytes(200, json.dumps(result).encode("utf-8"),
                             "application/json; charset=utf-8")
            return

        if path == "/state":
            listening = bool(data.get("listening"))
            lang = data.get("lang", "")
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
            warn("%s. The %s hotkey will not work until you fix it in "
                 "voicebridge.py." % (exc, label))
            continue
        if not user32.RegisterHotKey(None, hotkey_id, mods, key):
            warn("The %s hotkey %s is already taken by another app. Change it "
                 "in voicebridge.py and restart." % (label, spec.upper()))

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

single_instance = None


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
    # Held for as long as Voice Bridge runs. Closing it would let a second
    # copy start, so keep the reference alive.
    global single_instance
    single_instance = claim_single_instance()
    if single_instance is None:
        return

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
