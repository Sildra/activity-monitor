"""System-tray icon for the standalone Windows watcher (ctypes only).

Shows the watcher's connection to the backend as a coloured disc:

- green  - the last event delivery succeeded.
- red    - the last event delivery failed (backend unreachable).
- gray   - nothing has been delivered yet (starting up).

Clicking (or right-clicking) the icon opens a menu with the current state and
an `Exit` entry that stops the watcher via the `on_exit` callback. The icon
lives on its own thread with a Win32 message loop; `set_state` is safe to call
from any thread (it marshals changes onto the icon thread with `PostMessage`).
"""

from __future__ import annotations

import ctypes
import math
import threading
from collections.abc import Callable
from ctypes import wintypes as wt

shell32 = ctypes.WinDLL("shell32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

UINT_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM)

NIM_ADD = 0x0
NIM_MODIFY = 0x1
NIM_DELETE = 0x2

NIF_MESSAGE = 0x1
NIF_ICON = 0x2
NIF_TIP = 0x4

WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1  # lParam: mouse message that hit the icon
WM_TRAY_SET_STATE = WM_APP + 2  # state itself lives on the icon; the message is just a nudge
WM_TRAY_QUIT = WM_APP + 3

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_RBUTTONUP = 0x0205
WM_LBUTTONUP = 0x0202
WM_CONTEXTMENU = 0x007B

MF_STRING = 0x0
MF_GRAYED = 0x1
MF_SEPARATOR = 0x800
TPM_RIGHTBUTTON = 0x2
TPM_RETURNCMD = 0x100

EXIT_COMMAND = 1

ERROR_CLASS_ALREADY_EXISTS = 1410

ICON_SIZE = 32

STATE_UNKNOWN = 0
STATE_CONNECTED = 1
STATE_DISCONNECTED = 2

STATE_COLORS = {STATE_UNKNOWN: (140, 140, 140), STATE_CONNECTED: (46, 160, 67), STATE_DISCONNECTED: (200, 60, 60)}
STATE_LABELS = {STATE_UNKNOWN: "starting", STATE_CONNECTED: "connected", STATE_DISCONNECTED: "disconnected"}


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uTimeoutOrVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", ctypes.c_ubyte * 16),
        ("hBalloonIcon", wt.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HANDLE),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


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
        ("rgbBlue", ctypes.c_ubyte),
        ("rgbGreen", ctypes.c_ubyte),
        ("rgbRed", ctypes.c_ubyte),
        ("rgbReserved", ctypes.c_ubyte),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wt.BOOL),
        ("xHotspot", wt.DWORD),
        ("yHotspot", wt.DWORD),
        ("hbmMask", wt.HBITMAP),
        ("hbmColor", wt.HBITMAP),
    ]


shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wt.BOOL
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wt.WORD
user32.CreateWindowExW.argtypes = [
    wt.DWORD,
    wt.LPCWSTR,
    wt.LPCWSTR,
    wt.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wt.HWND,
    wt.HMENU,
    wt.HINSTANCE,
    ctypes.c_void_p,
]
user32.CreateWindowExW.restype = wt.HWND
user32.DefWindowProcW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, ctypes.c_uint, ctypes.c_uint]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.TranslateMessage.restype = wt.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.PostMessageW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
user32.DestroyWindow.argtypes = [wt.HWND]
user32.DestroyWindow.restype = wt.BOOL
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.CreatePopupMenu.argtypes = []
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, UINT_PTR, wt.LPCWSTR]
user32.AppendMenuW.restype = wt.BOOL
user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID]
user32.TrackPopupMenu.restype = wt.UINT
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.GetCursorPos.restype = wt.BOOL
user32.DestroyMenu.argtypes = [wt.HMENU]
user32.DestroyMenu.restype = wt.BOOL
user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
user32.CreateIconIndirect.restype = wt.HICON
user32.DestroyIcon.argtypes = [wt.HICON]
user32.DestroyIcon.restype = wt.BOOL
gdi32.CreateDIBSection.argtypes = [
    wt.HDC,
    ctypes.POINTER(BITMAPINFO),
    wt.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wt.HANDLE,
    wt.DWORD,
]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, wt.LPVOID]
gdi32.CreateBitmap.restype = wt.HBITMAP
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteObject.restype = wt.BOOL
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE


def make_disc_icon(rgb: tuple[int, int, int]) -> wt.HICON:
    """Build a 32px filled disc with a dark outline; the shape lives in the alpha channel."""
    xor = bytearray(ICON_SIZE * ICON_SIZE * 4)
    center = (ICON_SIZE - 1) / 2

    for y in range(ICON_SIZE):
        for x in range(ICON_SIZE):
            distance = math.hypot(x - center, y - center)

            if distance <= 13.5:
                color, alpha = rgb, 255
            elif distance <= 14.5:
                color, alpha = (64, 64, 64), 255  # outline
            else:
                color, alpha = (0, 0, 0), 0

            # DIB rows are bottom-up.
            offset = ((ICON_SIZE - 1 - y) * ICON_SIZE + x) * 4
            xor[offset : offset + 4] = bytes((color[2], color[1], color[0], alpha))

    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = ICON_SIZE
    info.bmiHeader.biHeight = ICON_SIZE
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32

    bits = ctypes.c_void_p()
    color_bitmap = gdi32.CreateDIBSection(None, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)

    if not color_bitmap:
        raise OSError(ctypes.get_last_error())

    try:
        ctypes.memmove(bits, bytes(xor), len(xor))
        # A zeroed mask defers transparency to the colour bitmap's alpha channel.
        mask_bitmap = gdi32.CreateBitmap(ICON_SIZE, ICON_SIZE, 1, 1, bytes(ICON_SIZE * ICON_SIZE // 8))

        if not mask_bitmap:
            raise OSError(ctypes.get_last_error())

        try:
            icon = user32.CreateIconIndirect(
                ctypes.byref(ICONINFO(fIcon=True, hbmMask=mask_bitmap, hbmColor=color_bitmap))
            )

            if not icon:
                raise OSError(ctypes.get_last_error())
        finally:
            gdi32.DeleteObject(mask_bitmap)
    finally:
        gdi32.DeleteObject(color_bitmap)

    return icon


class TrayIcon:
    """Tray icon reflecting the watcher's connection state, on its own message-loop thread."""

    def __init__(self, caption: str, *, on_exit: Callable[[], None] | None = None) -> None:
        self._caption = caption
        self._on_exit = on_exit
        self._wndproc = WNDPROC(self._handle)
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._hwnd: int | None = None
        self._state = STATE_UNKNOWN
        self._icons: dict[int, wt.HICON] = {}
        self._thread = threading.Thread(target=self._run, name="tray-icon", daemon=True)

    # -- public API -----------------------------------------------------------

    def start(self) -> None:
        """Create the icon window and start pumping its messages."""
        self._thread.start()
        self._ready.wait()

        if self._error is not None:
            raise self._error

    def set_state(self, connected: bool | None) -> None:
        """Show the connection state (`None` = nothing delivered yet). Safe from any thread."""
        code = STATE_UNKNOWN if connected is None else STATE_CONNECTED if connected else STATE_DISCONNECTED

        with self._lock:
            if code == self._state:
                return

            self._state = code
            hwnd = self._hwnd

        if hwnd is not None:
            user32.PostMessageW(hwnd, WM_TRAY_SET_STATE, 0, 0)

    def stop(self) -> None:
        """Remove the icon and stop the message loop."""
        with self._lock:
            hwnd = self._hwnd

        if hwnd is not None:
            user32.PostMessageW(hwnd, WM_TRAY_QUIT, 0, 0)

        if self._thread.ident is not None:
            self._thread.join(timeout=5)

    # -- icon thread ----------------------------------------------------------

    def _run(self) -> None:
        try:
            self._loop()
        except Exception as error:  # noqa: BLE001 - surfaced by start() on the caller's thread
            self._error = error
        finally:
            with self._lock:
                hwnd = self._hwnd
                self._hwnd = None

            if hwnd is not None:
                self._notify(hwnd, NIM_DELETE)

            for icon in self._icons.values():
                user32.DestroyIcon(icon)

            self._icons.clear()
            self._ready.set()

    def _loop(self) -> None:
        instance = kernel32.GetModuleHandleW(None)
        klass = WNDCLASSW(lpfnWndProc=self._wndproc, hInstance=instance, lpszClassName="ActivityMonitorTrayIcon")

        if not user32.RegisterClassW(ctypes.byref(klass)) and ctypes.get_last_error() != ERROR_CLASS_ALREADY_EXISTS:
            raise OSError(ctypes.get_last_error())

        hwnd = user32.CreateWindowExW(0, klass.lpszClassName, self._caption, 0, 0, 0, 0, 0, None, None, instance, None)

        if not hwnd:
            raise OSError(ctypes.get_last_error())

        with self._lock:
            self._hwnd = hwnd

        self._apply_state(hwnd, add=True)
        self._ready.set()

        message = wt.MSG()

        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

    def _handle(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if message == WM_TRAY_SET_STATE:
            self._apply_state(hwnd, add=False)
            return 0

        if message == WM_TRAY_QUIT:
            user32.DestroyWindow(hwnd)
            return 0

        if message == WM_TRAYICON and lparam in (WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU):
            self._menu(hwnd)
            return 0

        if message == WM_DESTROY:
            self._notify(hwnd, NIM_DELETE)
            user32.PostQuitMessage(0)
            return 0

        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _menu(self, hwnd: int) -> None:
        menu = user32.CreatePopupMenu()

        if not menu:
            return

        try:
            with self._lock:
                state = self._state

            user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, f"{self._caption} — {STATE_LABELS[state]}")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, EXIT_COMMAND, "Exit")
            user32.SetForegroundWindow(hwnd)

            point = wt.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            command = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, point.x, point.y, 0, hwnd, None)
            user32.PostMessageW(hwnd, WM_NULL, 0, 0)  # let the menu dismiss properly

            if command == EXIT_COMMAND:
                if self._on_exit is not None:
                    self._on_exit()

                user32.DestroyWindow(hwnd)
        finally:
            user32.DestroyMenu(menu)

    # -- state display --------------------------------------------------------

    def _apply_state(self, hwnd: int, *, add: bool) -> None:
        """Place (first call) or update the icon so it shows the current state."""
        with self._lock:
            state = self._state
            icon = self._icons.get(state)

        if icon is None:
            icon = make_disc_icon(STATE_COLORS[state])
            self._icons[state] = icon

        nid = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=hwnd, uID=1)
        nid.uFlags = NIF_ICON | NIF_TIP | (NIF_MESSAGE if add else 0)
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = icon
        nid.szTip = f"{self._caption} — {STATE_LABELS[state]}"
        shell32.Shell_NotifyIconW(NIM_ADD if add else NIM_MODIFY, ctypes.byref(nid))

    def _notify(self, hwnd: int, message: int) -> None:
        nid = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=hwnd, uID=1)
        shell32.Shell_NotifyIconW(message, ctypes.byref(nid))
