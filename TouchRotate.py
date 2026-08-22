import ctypes
from ctypes import wintypes
import sys

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ---- constants ----
WM_INPUT = 0x00FF
WM_DESTROY = 0x0002
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIDEV_INPUTSINK = 0x00000100
RIDEV_NOLEGACY = 0x00000030  # RIDEV_NOLEGACY | RIDEV_CAPTUREMOUSE-ish behavior for mouse usage
RIDEV_REMOVE = 0x00000001
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_UP, VK_RIGHT, VK_DOWN, VK_LEFT = 0x26, 0x27, 0x28, 0x25
VK_P = 0x50

orientation = 0
enabled = True
ROTATE_SIGN = 1

# internal tracked cursor position - we own this instead of repeatedly
# calling GetCursorPos, which avoids racing against any OS-side lag
cursor_x = 0
cursor_y = 0

SPI_GETMOUSESPEED = 0x0070
SPI_GETMOUSE = 0x0003


def get_sensitivity_scale():
    """Approximate Windows' own pointer-speed feel so the remapped
    cursor doesn't feel slower/faster or less smooth than normal."""
    speed = wintypes.DWORD(0)
    user32.SystemParametersInfoW(SPI_GETMOUSESPEED, 0, ctypes.byref(speed), 0)
    base = max(1, min(20, speed.value)) / 10.0  # 10 = Windows default

    mouse_params = (ctypes.c_int * 3)()
    user32.SystemParametersInfoW(SPI_GETMOUSE, 0, ctypes.byref(mouse_params), 0)
    enhance_precision = mouse_params[2] == 1
    return base, enhance_precision


SENS_BASE, ENHANCE_PRECISION = get_sensitivity_scale()


def apply_sensitivity(dx, dy):
    mag = (dx * dx + dy * dy) ** 0.5
    accel = 1.0
    if ENHANCE_PRECISION and mag > 0:
        # smooth approximation of Windows' acceleration curve - not an
        # exact match, but noticeably closer than flat 1:1 passthrough
        accel = 1.0 + min(mag / 18.0, 1.6)
    scale = SENS_BASE * accel
    return dx * scale, dy * scale


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    ]


class RAWMOUSE(ctypes.Structure):
    _fields_ = [
        ("usFlags", wintypes.USHORT),
        ("usButtonFlags", wintypes.USHORT),
        ("usButtonData", wintypes.USHORT),
        ("ulRawButtons", wintypes.ULONG),
        ("lLastX", wintypes.LONG),
        ("lLastY", wintypes.LONG),
        ("ulExtraInformation", wintypes.ULONG),
    ]


class RAWINPUT(ctypes.Structure):
    _fields_ = [
        ("header", RAWINPUTHEADER),
        ("mouse", RAWMOUSE),
    ]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def rotate(dx, dy, orient, sign):
    if orient == 0:
        return dx, dy
    if orient == 1:
        return sign * dy, -sign * dx
    if orient == 2:
        return -dx, -dy
    return -sign * dy, sign * dx


def register_raw_mouse(nolegacy: bool, hwnd) -> bool:
    rid = RAWINPUTDEVICE()
    rid.usUsagePage = 0x01
    rid.usUsage = 0x02
    rid.dwFlags = (RIDEV_INPUTSINK | RIDEV_NOLEGACY) if nolegacy else RIDEV_INPUTSINK
    rid.hwndTarget = hwnd
    ok = user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(rid))
    return bool(ok)


def unregister_raw_mouse():
    rid = RAWINPUTDEVICE()
    rid.usUsagePage = 0x01
    rid.usUsage = 0x02
    rid.dwFlags = RIDEV_REMOVE
    rid.hwndTarget = None
    user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(rid))


WNDPROCTYPE = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


def make_wndproc():
    def wndproc(hwnd, msg, wparam, lparam):
        global orientation, enabled
        if msg == WM_INPUT:
            handle_raw_input(lparam)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    return WNDPROCTYPE(wndproc)


frac_x = 0.0
frac_y = 0.0


def sync_cursor_pos():
    """Re-read the real cursor position once (on start / orientation
    change / pause-resume) so our internal tracker can't drift from
    reality if something else moved the cursor while we weren't."""
    global cursor_x, cursor_y, frac_x, frac_y
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    cursor_x, cursor_y = pt.x, pt.y
    frac_x = frac_y = 0.0


def handle_raw_input(lparam):
    global orientation, enabled, cursor_x, cursor_y, frac_x, frac_y
    size = wintypes.UINT(0)
    user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
    if size.value == 0:
        return
    buf = ctypes.create_string_buffer(size.value)
    if user32.GetRawInputData(lparam, RID_INPUT, buf, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER)) != size.value:
        return
    raw = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
    if raw.header.dwType != RIM_TYPEMOUSE:
        return

    dx, dy = raw.mouse.lLastX, raw.mouse.lLastY
    if dx == 0 and dy == 0:
        return

    if not enabled or orientation == 0:
        rdx, rdy = float(dx), float(dy)
    else:
        rdx, rdy = rotate(dx, dy, orientation, ROTATE_SIGN)
        rdx, rdy = float(rdx), float(rdy)

    rdx, rdy = apply_sensitivity(rdx, rdy)

    # accumulate fractional pixels so slow/precise strokes aren't lost
    # to integer truncation (keeps small movements smooth)
    frac_x += rdx
    frac_y += rdy
    step_x = int(frac_x)
    step_y = int(frac_y)
    frac_x -= step_x
    frac_y -= step_y
    if step_x == 0 and step_y == 0:
        return

    cursor_x += step_x
    cursor_y += step_y
    user32.SetCursorPos(cursor_x, cursor_y)


def check_hotkeys(hwnd):
    global orientation, enabled
    ctrl = user32.GetAsyncKeyState(VK_CONTROL) & 0x8000
    alt = user32.GetAsyncKeyState(VK_MENU) & 0x8000
    if ctrl and alt:
        new_orientation = None
        if user32.GetAsyncKeyState(VK_UP) & 0x8000:
            new_orientation = 0
        elif user32.GetAsyncKeyState(VK_RIGHT) & 0x8000:
            new_orientation = 1
        elif user32.GetAsyncKeyState(VK_DOWN) & 0x8000:
            new_orientation = 2
        elif user32.GetAsyncKeyState(VK_LEFT) & 0x8000:
            new_orientation = 3
        if new_orientation is not None and new_orientation != orientation:
            orientation = new_orientation
            sync_cursor_pos()
            print("Orientation:", ["normal", "90 CW", "180", "90 CCW"][orientation])
        elif user32.GetAsyncKeyState(VK_P) & 0x8000:
            enabled = not enabled
            sync_cursor_pos()
            print("Enabled:", enabled)


def main():
    hInstance = kernel32.GetModuleHandleW(None)
    class_name = "TouchpadRotateHiddenWindow"
    wndproc = make_wndproc()

    wc = WNDCLASS()
    wc.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p)
    wc.hInstance = hInstance
    wc.lpszClassName = class_name
    user32.RegisterClassW(ctypes.byref(wc))

    hwnd = user32.CreateWindowExW(0, class_name, "TouchpadRotate", 0, 0, 0, 0, 0, None, None, hInstance, None)
    if not hwnd:
        print("Failed to create window:", ctypes.GetLastError())
        return

    if not register_raw_mouse(True, hwnd):
        print("Failed to register raw input (NOLEGACY). Error:", ctypes.GetLastError())
        print("Your cursor should still work normally since registration failed.")
        return

    sync_cursor_pos()
    print(f"Sensitivity: speed_scale={SENS_BASE:.2f} enhance_precision={ENHANCE_PRECISION}")
    print("Running. Ctrl+Alt+Arrow to set orientation, Ctrl+Alt+P to pause, Ctrl+C to quit.")

    msg = wintypes.MSG()
    hotkey_cooldown = 0
    try:
        while True:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            if hotkey_cooldown <= 0:
                before = orientation, enabled
                check_hotkeys(hwnd)
                if (orientation, enabled) != before:
                    hotkey_cooldown = 20  # ~300ms debounce so one press doesn't retrigger
            else:
                hotkey_cooldown -= 1
            kernel32.Sleep(15)
    except KeyboardInterrupt:
        pass
    finally:
        unregister_raw_mouse()
        print("Restored normal mouse behavior. Exiting.")


if __name__ == "__main__":
    if sys.platform != "win32":
        print("This script only works on Windows.")
        sys.exit(1)
    main()
