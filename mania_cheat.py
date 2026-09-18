"""
osu!mania pixel-trigger engine — Interception v1.0.1 edition

Runtime input path:
    Physical keyboard
        -> Interception kernel driver
        -> interception.dll
        -> this program

Behavior:
    - Physical Q/S/L/P is intercepted and blocked from reaching the game.
    - Holding a lane continuously watches that receptor.
    - ANY sampled luminance >= 12 emits a synthetic tap.
    - The lane re-arms as soon as luminance drops below 12.
    - Keep holding the physical key to hit every new bright pulse.
    - Releasing the physical key resets that lane.
    - Numpad + or Shift+= exits.

Important:
    - This version DOES NOT use SendInput for gameplay input.
    - This version DOES NOT use WH_KEYBOARD_LL for gameplay input.
    - Physical interception and synthetic taps both go through Interception.
    - Place the correct-architecture interception.dll from Interception v1.0.1
      beside this script, or leave it in a standard release directory such as
      library/x64/interception.dll.
    - The Interception driver itself must already be installed.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import struct
import threading
import time
import tkinter as tk
from pathlib import Path


# ============================================================
# CONFIG
# ============================================================

# Set-1 keyboard scan codes used by Interception.
LANES = (
    ("q", 0x10, "cyan"),
    ("s", 0x1F, "magenta"),
    ("l", 0x26, "lime"),
    ("p", 0x19, "yellow"),
)

# Brightness detection.
# Fixed threshold by design: while a lane is physically held, any sampled
# receptor luminance >= 12 fires that lane.  No adaptive baseline or
# per-lane threshold adjustment is used.
LUM_THRESHOLD = 12
SAMPLE_RADIUS = 1             # 1 = 3x3 sample; brightest pixel wins.

# Runtime behavior.
IDLE_WAIT_SECONDS = 0.050
INTERCEPTION_WAIT_MS = 25
THREAD_PRIORITY_ABOVE_NORMAL = 1

# Position persistence.
CONFIG_PATH = Path.home() / ".osu_mania_pixel_engine.json"

# Written into KEYBOARD_INPUT_DATA.ExtraInformation for generated strokes.
SYNTHETIC_INFORMATION = 0xC0110A57


# ============================================================
# WIN32 / GDI
# ============================================================

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
winmm = ctypes.WinDLL("winmm", use_last_error=True)

c_void_p = ctypes.c_void_p

try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    pass

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", wt.BYTE),
        ("rgbGreen", wt.BYTE),
        ("rgbRed", wt.BYTE),
        ("rgbReserved", wt.BYTE),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", RGBQUAD * 1),
    ]


kernel32.GetCurrentThread.restype = c_void_p
kernel32.SetThreadPriority.argtypes = [c_void_p, ctypes.c_int]
kernel32.SetThreadPriority.restype = wt.BOOL
kernel32.SwitchToThread.restype = wt.BOOL

user32.GetDC.argtypes = [c_void_p]
user32.GetDC.restype = c_void_p
user32.ReleaseDC.argtypes = [c_void_p, c_void_p]
user32.ReleaseDC.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int

gdi32.CreateCompatibleDC.argtypes = [c_void_p]
gdi32.CreateCompatibleDC.restype = c_void_p

gdi32.CreateDIBSection.argtypes = [
    c_void_p,
    ctypes.POINTER(BITMAPINFO),
    wt.UINT,
    ctypes.POINTER(c_void_p),
    c_void_p,
    wt.DWORD,
]
gdi32.CreateDIBSection.restype = c_void_p

gdi32.SelectObject.argtypes = [c_void_p, c_void_p]
gdi32.SelectObject.restype = c_void_p

gdi32.BitBlt.argtypes = [
    c_void_p,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    c_void_p,
    ctypes.c_int,
    ctypes.c_int,
    wt.DWORD,
]
gdi32.BitBlt.restype = wt.BOOL

gdi32.DeleteDC.argtypes = [c_void_p]
gdi32.DeleteDC.restype = wt.BOOL
gdi32.DeleteObject.argtypes = [c_void_p]
gdi32.DeleteObject.restype = wt.BOOL


# ============================================================
# INTERCEPTION v1.0.1 API
# ============================================================

# InterceptionKeyState
INTERCEPTION_KEY_DOWN = 0x00
INTERCEPTION_KEY_UP = 0x01
INTERCEPTION_KEY_E0 = 0x02
INTERCEPTION_KEY_E1 = 0x04

# InterceptionFilterKeyState
# Header defines DOWN as KEY_UP (1) and UP as KEY_UP << 1 (2).
INTERCEPTION_FILTER_KEY_NONE = 0x0000
INTERCEPTION_FILTER_KEY_DOWN = 0x0001
INTERCEPTION_FILTER_KEY_UP = 0x0002
INTERCEPTION_FILTER_KEY_ALL_EVENTS = (
    INTERCEPTION_FILTER_KEY_DOWN | INTERCEPTION_FILTER_KEY_UP
)

# Relevant scan codes.
SCANCODE_LSHIFT = 0x2A
SCANCODE_RSHIFT = 0x36
SCANCODE_EQUAL_PLUS = 0x0D
SCANCODE_NUMPAD_PLUS = 0x4E

INTERCEPTION_MAX_KEYBOARD = 10


class InterceptionKeyStroke(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("state", ctypes.c_ushort),
        ("information", ctypes.c_uint),
    ]


# C structure is exactly 8 bytes in the official header.
if ctypes.sizeof(InterceptionKeyStroke) != 8:
    raise RuntimeError(
        "Unexpected InterceptionKeyStroke layout: "
        f"{ctypes.sizeof(InterceptionKeyStroke)} bytes"
    )


LANE_NAMES = tuple(item[0] for item in LANES)
LANE_SCANCODES = tuple(item[1] for item in LANES)
LANE_COLORS = tuple(item[2] for item in LANES)
LANE_COUNT = len(LANES)
SCANCODE_TO_INDEX = {code: i for i, code in enumerate(LANE_SCANCODES)}


class InterceptionError(RuntimeError):
    pass


def _interception_dll_candidates() -> list[Path]:
    """Return likely v1.0.1 DLL locations, best match first."""
    arch = "x64" if struct.calcsize("P") == 8 else "x86"
    script_dir = Path(__file__).resolve().parent
    cwd = Path.cwd()

    candidates: list[Path] = []

    env_path = os.environ.get("INTERCEPTION_DLL")
    if env_path:
        candidates.append(Path(env_path).expanduser())

    for root in (script_dir, cwd):
        candidates.extend(
            (
                root / "interception.dll",
                root / "Interception.dll",
                root / "library" / arch / "interception.dll",
                root / "Interception" / "library" / arch / "interception.dll",
                root / "Interception_v1.0.1" / "library" / arch / "interception.dll",
            )
        )

    # Preserve order while removing duplicates.
    seen: set[str] = set()
    out: list[Path] = []
    for path in candidates:
        key = str(path.resolve(strict=False)).lower()
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def _load_interception_dll() -> tuple[ctypes.CDLL, str]:
    """
    Load Interception using cdecl calling convention.

    Interception's exported API is ordinary C, not WINAPI/stdcall, so CDLL is
    intentional here. Using WinDLL can corrupt calls on 32-bit Python.
    """
    attempted: list[str] = []

    for path in _interception_dll_candidates():
        attempted.append(str(path))
        if not path.is_file():
            continue

        try:
            return ctypes.CDLL(str(path)), str(path)
        except OSError as exc:
            raise InterceptionError(
                f"Found Interception DLL at {path}, but Windows could not load it: {exc}. "
                "Make sure its x86/x64 architecture matches Python."
            ) from exc

    # Last chance: Windows DLL search path / PATH.
    try:
        return ctypes.CDLL("interception.dll"), "interception.dll (Windows search path)"
    except OSError as exc:
        joined = "\n  ".join(attempted)
        raise InterceptionError(
            "interception.dll was not found. Put the v1.0.1 DLL matching your "
            "Python architecture beside this script (usually library/x64/interception.dll).\n"
            f"Searched:\n  {joined}"
        ) from exc


def _configure_interception_api(dll: ctypes.CDLL) -> None:
    dll.interception_create_context.argtypes = []
    dll.interception_create_context.restype = c_void_p

    dll.interception_destroy_context.argtypes = [c_void_p]
    dll.interception_destroy_context.restype = None

    # Predicate is a C function pointer exported by the same DLL.
    dll.interception_set_filter.argtypes = [
        c_void_p,
        c_void_p,
        ctypes.c_ushort,
    ]
    dll.interception_set_filter.restype = None

    dll.interception_wait_with_timeout.argtypes = [
        c_void_p,
        wt.DWORD,
    ]
    dll.interception_wait_with_timeout.restype = ctypes.c_int

    dll.interception_receive.argtypes = [
        c_void_p,
        ctypes.c_int,
        c_void_p,
        ctypes.c_uint,
    ]
    dll.interception_receive.restype = ctypes.c_int

    dll.interception_send.argtypes = [
        c_void_p,
        ctypes.c_int,
        c_void_p,
        ctypes.c_uint,
    ]
    dll.interception_send.restype = ctypes.c_int

    dll.interception_is_keyboard.argtypes = [ctypes.c_int]
    dll.interception_is_keyboard.restype = ctypes.c_int


# ============================================================
# SHARED STATE
# ============================================================

# CPython list element assignments are atomic under the GIL. This keeps the
# scanner hot path lock-free. Interception send itself has a small dedicated
# lock because normal pass-through and generated taps can originate from
# different Python threads.
phys_down = [False] * LANE_COUNT
generation = [0] * LANE_COUNT
lane_device = [0] * LANE_COUNT

stop_event = threading.Event()
wake_event = threading.Event()
scanner_ready = threading.Event()
input_ready = threading.Event()

scanner_info: dict[str, object] = {}
scanner_error: list[BaseException] = []
input_error: list[BaseException] = []


# ============================================================
# INTERCEPTION INPUT ENGINE
# ============================================================

class InterceptionEngine:
    def __init__(self):
        self.dll, self.dll_path = _load_interception_dll()
        _configure_interception_api(self.dll)

        self.context = self.dll.interception_create_context()
        if not self.context:
            raise InterceptionError(
                "Interception could not create a driver context. The DLL loaded, "
                "but the kernel driver is unavailable. Install the Interception "
                "v1.0.1 driver with its command-line installer as Administrator "
                "and reboot Windows."
            )

        self._send_lock = threading.Lock()
        self._closed = False
        self._shift_down = False

        # Pre-build each synthetic down/up pair once.
        self._tap_packets = tuple(
            (InterceptionKeyStroke * 2)(
                InterceptionKeyStroke(
                    code=scan,
                    state=INTERCEPTION_KEY_DOWN,
                    information=SYNTHETIC_INFORMATION,
                ),
                InterceptionKeyStroke(
                    code=scan,
                    state=INTERCEPTION_KEY_UP,
                    information=SYNTHETIC_INFORMATION,
                ),
            )
            for scan in LANE_SCANCODES
        )

        keyboard_predicate = ctypes.cast(
            self.dll.interception_is_keyboard,
            c_void_p,
        )

        self.dll.interception_set_filter(
            self.context,
            keyboard_predicate,
            INTERCEPTION_FILTER_KEY_ALL_EVENTS,
        )

    def _send(self, device: int, strokes, count: int) -> bool:
        if self._closed or not device:
            return False

        with self._send_lock:
            sent = self.dll.interception_send(
                self.context,
                device,
                ctypes.cast(strokes, c_void_p),
                count,
            )

        return sent == count

    def passthrough(self, device: int, stroke: InterceptionKeyStroke) -> bool:
        return self._send(device, ctypes.byref(stroke), 1)

    def send_tap(self, lane_index: int) -> bool:
        device = lane_device[lane_index]
        if not device:
            return False

        packet = self._tap_packets[lane_index]
        return self._send(device, packet, 2)

    @staticmethod
    def _is_up(stroke: InterceptionKeyStroke) -> bool:
        return bool(stroke.state & INTERCEPTION_KEY_UP)

    def _update_shift(self, stroke: InterceptionKeyStroke) -> None:
        if stroke.code in (SCANCODE_LSHIFT, SCANCODE_RSHIFT):
            self._shift_down = not self._is_up(stroke)

    def _is_quit_press(self, stroke: InterceptionKeyStroke) -> bool:
        if self._is_up(stroke):
            return False

        if stroke.code == SCANCODE_NUMPAD_PLUS:
            return True

        return stroke.code == SCANCODE_EQUAL_PLUS and self._shift_down

    def run(self) -> None:
        """
        Intercept keyboard input at the driver layer.

        Q/S/L/P are consumed here and never passed through physically.
        Every other keyboard event is forwarded immediately and unchanged.
        """
        try:
            try:
                kernel32.SetThreadPriority(
                    kernel32.GetCurrentThread(),
                    THREAD_PRIORITY_ABOVE_NORMAL,
                )
            except Exception:
                pass

            input_ready.set()

            while not stop_event.is_set():
                device = self.dll.interception_wait_with_timeout(
                    self.context,
                    INTERCEPTION_WAIT_MS,
                )

                if not device:
                    continue

                if not self.dll.interception_is_keyboard(device):
                    continue

                stroke = InterceptionKeyStroke()
                received = self.dll.interception_receive(
                    self.context,
                    device,
                    ctypes.byref(stroke),
                    1,
                )

                if received <= 0:
                    continue

                # Track shift before checking the =/+ scan code. The shift-down
                # event arrives before the plus-key event in normal use.
                self._update_shift(stroke)

                lane_index = SCANCODE_TO_INDEX.get(stroke.code)

                if lane_index is not None:
                    is_up = self._is_up(stroke)

                    # Q/S/L/P are deliberately NOT forwarded. This replaces
                    # the old WH_KEYBOARD_LL blocking path with driver-level
                    # interception.
                    if is_up:
                        if phys_down[lane_index]:
                            phys_down[lane_index] = False
                            generation[lane_index] += 1
                            wake_event.set()
                    else:
                        if not phys_down[lane_index]:
                            lane_device[lane_index] = device
                            phys_down[lane_index] = True
                            generation[lane_index] += 1
                            wake_event.set()

                    continue

                # Non-lane keys always retain their normal behavior.
                self.passthrough(device, stroke)

                if self._is_quit_press(stroke):
                    stop_event.set()
                    wake_event.set()
                    break

        except BaseException as exc:
            input_error.append(exc)
            stop_event.set()
            wake_event.set()
            scanner_ready.set()
            input_ready.set()

        finally:
            for i in range(LANE_COUNT):
                if phys_down[i]:
                    phys_down[i] = False
                    generation[i] += 1
            wake_event.set()

    def close(self) -> None:
        if self._closed:
            return

        self._closed = True

        if self.context:
            try:
                keyboard_predicate = ctypes.cast(
                    self.dll.interception_is_keyboard,
                    c_void_p,
                )
                self.dll.interception_set_filter(
                    self.context,
                    keyboard_predicate,
                    INTERCEPTION_FILTER_KEY_NONE,
                )
            except Exception:
                pass

            self.dll.interception_destroy_context(self.context)
            self.context = None


# ============================================================
# POSITION SAVE / LOAD
# ============================================================

def load_saved_positions() -> dict[str, dict[str, int]]:
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        positions = raw.get("positions", {})
        out: dict[str, dict[str, int]] = {}

        for name in LANE_NAMES:
            item = positions.get(name)
            if (
                isinstance(item, dict)
                and isinstance(item.get("x"), int)
                and isinstance(item.get("y"), int)
            ):
                out[name] = {"x": item["x"], "y": item["y"]}

        return out
    except Exception:
        return {}


def save_positions(positions: dict[str, dict[str, int]]) -> None:
    try:
        payload = {
            "version": 3,
            "input_backend": "Interception-v1.0.1",
            "positions": positions,
        }
        CONFIG_PATH.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"Warning: could not save receptor positions: {exc}")


# ============================================================
# CALIBRATION GUI
# ============================================================

class SetupGUI:
    BOX_HALF = 18

    def __init__(self, saved: dict[str, dict[str, int]]):
        self.vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        self.vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        self.vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        self.vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)

        if self.vw <= 0 or self.vh <= 0:
            self.vx = 0
            self.vy = 0
            self.vw = 1920
            self.vh = 1080

        self.root = tk.Tk()
        self.root.attributes("-topmost", True)
        self.root.overrideredirect(True)

        try:
            self.root.wm_attributes("-transparentcolor", "black")
        except tk.TclError:
            pass

        self.root.geometry(f"{self.vw}x{self.vh}{self.vx:+d}{self.vy:+d}")
        self.root.focus_force()

        self.c = tk.Canvas(
            self.root,
            width=self.vw,
            height=self.vh,
            bg="black",
            highlightthickness=0,
            cursor="crosshair",
        )
        self.c.pack(fill="both", expand=True)

        self.targets: dict[str, str] = {}
        self.results: dict[str, dict[str, int]] | None = {}

        self.c.create_text(
            self.vw // 2,
            max(60, self.vh // 5),
            text=(
                "DRAG EACH MARKER OVER ITS RECEPTOR\n"
                "ENTER = start   |   NUMPAD + / SHIFT+= / ESC = quit"
            ),
            fill="white",
            font=("Arial", 20, "bold"),
            justify="center",
        )

        if saved:
            self.c.create_text(
                self.vw // 2,
                max(120, self.vh // 5 + 70),
                text="Saved positions loaded — adjust only if needed",
                fill="#d0d0d0",
                font=("Arial", 11),
            )

        default_start = self.vw // 2 - ((LANE_COUNT - 1) * 50)

        for i, (name, _scan, color) in enumerate(LANES):
            if name in saved:
                x = saved[name]["x"] - self.vx
                y = saved[name]["y"] - self.vy
                if not (0 <= x < self.vw and 0 <= y < self.vh):
                    x = default_start + i * 100
                    y = self.vh // 2
            else:
                x = default_start + i * 100
                y = self.vh // 2

            tag = f"lane_{name}"

            self.c.create_rectangle(
                x - self.BOX_HALF,
                y - self.BOX_HALF,
                x + self.BOX_HALF,
                y + self.BOX_HALF,
                outline=color,
                width=3,
                fill="black",
                tags=(tag, "drag"),
            )

            self.c.create_text(
                x,
                y,
                text=name.upper(),
                fill=color,
                font=("Arial", 15, "bold"),
                tags=(tag, "drag"),
            )

            self.c.create_oval(
                x - 1,
                y - 1,
                x + 1,
                y + 1,
                outline=color,
                fill=color,
                tags=(tag, "drag"),
            )

            self.targets[name] = tag

        self._drag_tag: str | None = None
        self._drag_x = 0
        self._drag_y = 0

        self.c.tag_bind("drag", "<ButtonPress-1>", self._drag_start)
        self.c.tag_bind("drag", "<B1-Motion>", self._drag_move)
        self.c.tag_bind("drag", "<ButtonRelease-1>", self._drag_end)

        self.root.bind("<Return>", self._accept)
        self.root.bind("<KP_Add>", self._quit)
        self.root.bind("<plus>", self._quit)
        self.root.bind("<Escape>", self._quit)

    def _drag_start(self, event) -> None:
        current = self.c.find_withtag("current")
        if not current:
            return

        for tag in self.c.gettags(current[0]):
            if tag.startswith("lane_"):
                self._drag_tag = tag
                self._drag_x = event.x
                self._drag_y = event.y
                return

    def _drag_move(self, event) -> None:
        if not self._drag_tag:
            return

        dx = event.x - self._drag_x
        dy = event.y - self._drag_y

        bbox = self.c.bbox(self._drag_tag)
        if bbox:
            left, top, right, bottom = bbox
            if left + dx < 0:
                dx = -left
            elif right + dx >= self.vw:
                dx = self.vw - 1 - right

            if top + dy < 0:
                dy = -top
            elif bottom + dy >= self.vh:
                dy = self.vh - 1 - bottom

        self.c.move(self._drag_tag, dx, dy)
        self._drag_x = event.x
        self._drag_y = event.y

    def _drag_end(self, _event) -> None:
        self._drag_tag = None

    def _accept(self, _event=None) -> None:
        out: dict[str, dict[str, int]] = {}

        for name, tag in self.targets.items():
            items = self.c.find_withtag(tag)
            if len(items) < 2:
                continue

            x, y = self.c.coords(items[1])
            out[name] = {
                "x": int(round(x)) + self.vx,
                "y": int(round(y)) + self.vy,
            }

        if len(out) != LANE_COUNT:
            return

        self.results = out
        self.root.destroy()

    def _quit(self, _event=None) -> None:
        self.results = None
        self.root.destroy()

    def run(self) -> dict[str, dict[str, int]] | None:
        self.root.mainloop()
        return self.results


# ============================================================
# FAST SCREEN CAPTURE
# ============================================================

class ReceptorCapture:
    """Capture all receptor pixels with one BitBlt into a 32-bit DIB."""

    def __init__(self, coords: tuple[tuple[int, int], ...]):
        radius = max(0, int(SAMPLE_RADIUS))
        xs = [p[0] for p in coords]
        ys = [p[1] for p in coords]

        self.left = min(xs) - radius
        self.top = min(ys) - radius
        self.width = max(xs) - min(xs) + 1 + radius * 2
        self.height = max(ys) - min(ys) + 1 + radius * 2

        self.src = user32.GetDC(None)
        if not self.src:
            raise OSError("GetDC(None) failed")

        self.mem = gdi32.CreateCompatibleDC(self.src)
        if not self.mem:
            user32.ReleaseDC(None, self.src)
            raise OSError("CreateCompatibleDC failed")

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = self.width
        bmi.bmiHeader.biHeight = -self.height
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB

        bits = c_void_p()
        self.bitmap = gdi32.CreateDIBSection(
            self.src,
            ctypes.byref(bmi),
            DIB_RGB_COLORS,
            ctypes.byref(bits),
            None,
            0,
        )

        if not self.bitmap or not bits.value:
            gdi32.DeleteDC(self.mem)
            user32.ReleaseDC(None, self.src)
            raise OSError("CreateDIBSection failed")

        self.old_bitmap = gdi32.SelectObject(self.mem, self.bitmap)

        pixel_count = self.width * self.height
        pixel_array_type = ctypes.c_uint32 * pixel_count
        self.pixels = pixel_array_type.from_address(bits.value)

        lane_offsets: list[tuple[int, ...]] = []

        for x, y in coords:
            cx = x - self.left
            cy = y - self.top
            offsets: list[int] = []

            for dy in range(-radius, radius + 1):
                py = cy + dy
                if not (0 <= py < self.height):
                    continue
                row = py * self.width

                for dx in range(-radius, radius + 1):
                    px = cx + dx
                    if 0 <= px < self.width:
                        offsets.append(row + px)

            lane_offsets.append(tuple(offsets))

        self.lane_offsets = tuple(lane_offsets)
        self.closed = False

    def update(self) -> bool:
        return bool(
            gdi32.BitBlt(
                self.mem,
                0,
                0,
                self.width,
                self.height,
                self.src,
                self.left,
                self.top,
                SRCCOPY,
            )
        )

    def luminance(self, lane_index: int) -> int:
        best = 0
        pixels = self.pixels

        for offset in self.lane_offsets[lane_index]:
            value = pixels[offset]
            b = value & 0xFF
            g = (value >> 8) & 0xFF
            r = (value >> 16) & 0xFF
            lum = (77 * r + 150 * g + 29 * b) >> 8
            if lum > best:
                best = lum

        return best

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True

        if self.mem and self.old_bitmap:
            gdi32.SelectObject(self.mem, self.old_bitmap)
        if self.bitmap:
            gdi32.DeleteObject(self.bitmap)
        if self.mem:
            gdi32.DeleteDC(self.mem)
        if self.src:
            user32.ReleaseDC(None, self.src)

        self.bitmap = None
        self.mem = None
        self.src = None

    def __enter__(self) -> "ReceptorCapture":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


# ============================================================
# SCANNER
# ============================================================

def scanner(
    coords: tuple[tuple[int, int], ...],
    interception: InterceptionEngine,
) -> None:
    capture: ReceptorCapture | None = None

    try:
        try:
            kernel32.SetThreadPriority(
                kernel32.GetCurrentThread(),
                THREAD_PRIORITY_ABOVE_NORMAL,
            )
        except Exception:
            pass

        capture = ReceptorCapture(coords)

        # Fixed threshold: no calibration/baseline can silently raise it.
        scanner_info["threshold"] = LUM_THRESHOLD
        scanner_info["capture_size"] = (capture.width, capture.height)
        scanner_ready.set()

        latched = [False] * LANE_COUNT
        seen_generation = generation.copy()

        while not stop_event.is_set():
            if not any(phys_down):
                for i in range(LANE_COUNT):
                    latched[i] = False
                    seen_generation[i] = generation[i]

                wake_event.wait(IDLE_WAIT_SECONDS)
                wake_event.clear()
                continue

            if not capture.update():
                kernel32.SwitchToThread()
                continue

            for i in range(LANE_COUNT):
                gen = generation[i]

                if gen != seen_generation[i]:
                    latched[i] = False
                    seen_generation[i] = gen

                if not phys_down[i]:
                    latched[i] = False
                    continue

                lum = capture.luminance(i)

                if latched[i]:
                    # Re-arm immediately once the sampled receptor is below
                    # the fixed threshold. The next >=12 pulse can fire even
                    # while the physical key is still being held.
                    if lum < LUM_THRESHOLD:
                        latched[i] = False
                    continue

                if lum >= LUM_THRESHOLD:
                    # State check prevents a late tap after key release.
                    if phys_down[i] and generation[i] == gen:
                        if interception.send_tap(i):
                            latched[i] = True

            kernel32.SwitchToThread()

    except BaseException as exc:
        scanner_error.append(exc)
        scanner_ready.set()
        stop_event.set()
        wake_event.set()

    finally:
        if capture is not None:
            capture.close()


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    saved = load_saved_positions()
    positions = SetupGUI(saved).run()

    if not positions:
        return

    save_positions(positions)

    coords = tuple(
        (positions[name]["x"], positions[name]["y"])
        for name in LANE_NAMES
    )

    print("\nReceptors:")
    for name, (x, y) in zip(LANE_NAMES, coords):
        print(f"  {name.upper()} -> ({x}, {y})")

    timer_started = False
    interception: InterceptionEngine | None = None
    input_thread: threading.Thread | None = None
    scan_thread: threading.Thread | None = None

    try:
        # Initialize the driver backend before the scanner so failures are
        # immediate and no user-mode input fallback is silently used.
        interception = InterceptionEngine()
        print(f"\nInterception DLL: {interception.dll_path}")
        print("Input backend: Interception driver (capture + injection)")

        if winmm.timeBeginPeriod(1) == 0:
            timer_started = True

        input_thread = threading.Thread(
            target=interception.run,
            name="interception-input",
            daemon=True,
        )
        input_thread.start()

        if not input_ready.wait(1.0):
            raise InterceptionError("Interception input thread did not initialize")
        if input_error:
            raise input_error[0]

        scan_thread = threading.Thread(
            target=scanner,
            args=(coords, interception),
            name="receptor-scanner",
            daemon=True,
        )
        scan_thread.start()

        while not scanner_ready.wait(0.05):
            if scanner_error or input_error:
                break

        if input_error:
            raise input_error[0]
        if scanner_error:
            raise scanner_error[0]

        threshold = scanner_info.get("threshold", LUM_THRESHOLD)
        capture_size = scanner_info.get("capture_size", ("?", "?"))

        print(f"Capture rectangle: {capture_size[0]}x{capture_size[1]}")
        print(f"Fixed luminance threshold: >= {threshold}")

        print("\nRunning. Hold Q/S/L/P to continuously watch those lanes.")
        print("Physical Q/S/L/P are blocked by the Interception driver.")
        print("Generated taps are sent with interception_send().")
        print("While held: lum >= 12 fires; lum < 12 re-arms immediately.")
        print("Numpad + or Shift+= exits.\n")

        # No Windows message hook/loop is needed anymore. The Interception
        # input worker owns keyboard capture and signals stop_event on exit.
        while not stop_event.wait(0.10):
            if input_error:
                raise input_error[0]
            if scanner_error:
                raise scanner_error[0]

    except KeyboardInterrupt:
        pass

    except BaseException as exc:
        print(f"Error: {exc}")

    finally:
        stop_event.set()
        wake_event.set()

        if input_thread and input_thread.is_alive():
            input_thread.join(timeout=0.5)

        if scan_thread and scan_thread.is_alive():
            scan_thread.join(timeout=0.5)

        if interception is not None:
            interception.close()

        if timer_started:
            winmm.timeEndPeriod(1)

        for i in range(LANE_COUNT):
            phys_down[i] = False
            lane_device[i] = 0

        print("Exit.")


if __name__ == "__main__":
    main()
