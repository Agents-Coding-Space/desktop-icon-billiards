"""Per-pixel-alpha desktop child windows that render the interactive table.

The Explorer icon layer is replaced by one layered child window per desktop icon
plus table layers for the rail, white ball, cue, and charge indicator. Icon artwork is extracted
through the Shell, so the game uses the user's real thumbnails and labels.
"""
from __future__ import annotations

import ctypes
import math
import os
import platform
import queue
import struct
import threading
import time
from collections import OrderedDict
from ctypes import wintypes as W

import comtypes
import win32api
import win32con
import win32gui
import win32process
from PIL import Image, ImageDraw, ImageFont

from . import diag
from .bot import BotController
from .config import CREDIT, REPOSITORY_URL
from .art import credit_layout, charge_art, cue_art, guide_art, pocket_art, pocket_particles, table_art, white_art
from .art import round_actions_art, round_actions_layout
from .balls import MAX_BALLS, ball_summary, fill_sources, priority_order
from .display import dpi_context, monitors, window_dpi
from .shell import ShellDesktop
from .table import MIN_SHOT_CHARGE, BilliardsMotion
from .wallpaper import primary_wallpaper

BALL_CANVAS = 128
INTRO_DURATION = 1.25
INTRO_STAGGER = 0.025
RING_LEVELS = 6
MAX_IMPACT = .23  # the largest ``impact`` value the table produces
GUIDE_INTERVAL = 1 / 30  # the aim guide is redrawn at most this often
LUMA_CELL = 8  # wallpaper pixels per cell of the brightness map
WS_EX_LAYERED = 0x80000
WS_EX_NOACTIVATE = 0x08000000
SM_SWAPBUTTON = 23


class ImageFactory(comtypes.IUnknown):
    _iid_ = comtypes.GUID('{bcc18b79-ba16-442f-80c4-8a59c30c463b}')
    _methods_ = []


def _unpremultiply(image):
    """Shell bitmaps use premultiplied BGRA; Pillow needs straight alpha."""
    data = bytearray(image.tobytes())
    for index in range(3, len(data), 4):
        alpha = data[index]
        if 0 < alpha < 255:
            scale = 255 / alpha
            data[index - 3] = min(255, round(data[index - 3] * scale))
            data[index - 2] = min(255, round(data[index - 2] * scale))
            data[index - 1] = min(255, round(data[index - 1] * scale))
    return Image.frombytes('RGBA', image.size, bytes(data))


def artwork(identity, size):
    """Extract the Shell thumbnail for one desktop item as straight-alpha RGBA."""
    factory = ctypes.POINTER(ImageFactory)()
    create = ctypes.windll.shell32.SHCreateItemFromParsingName
    create.argtypes = [W.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(comtypes.GUID),
                       ctypes.POINTER(ctypes.POINTER(ImageFactory))]
    hr = create(identity, None, ctypes.byref(ImageFactory._iid_), ctypes.byref(factory))
    if hr < 0:
        raise OSError(f'Cannot load Shell artwork: {hr & 0xffffffff:08x}')
    bitmap = W.HBITMAP()
    ShellDesktop._invoke(factory, 3, [W.SIZE, W.UINT, ctypes.POINTER(W.HBITMAP)],
                         W.SIZE(size, size), 0, ctypes.byref(bitmap))
    try:
        return _unpremultiply(_bitmap_image(bitmap.value))
    finally:
        win32gui.DeleteObject(bitmap.value)


def _bitmap_image(bitmap):
    """Copy an unselected Shell bitmap into a top-down, 32-bit BGRA buffer."""
    get_bits = ctypes.WinDLL('gdi32', use_last_error=True).GetDIBits
    get_bits.argtypes = [W.HDC, W.HBITMAP, W.UINT, W.UINT, ctypes.c_void_p, ctypes.c_void_p, W.UINT]
    get_bits.restype = ctypes.c_int
    header_format = '<IiiHHIIiiII'
    header = ctypes.create_string_buffer(struct.calcsize(header_format))
    struct.pack_into('<I', header, 0, ctypes.sizeof(header))
    dc = win32gui.CreateCompatibleDC(0)
    try:
        if not get_bits(dc, bitmap, 0, 0, None, header, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        _, width, height, *_ = struct.unpack(header_format, header)
        height = abs(height)
        if width <= 0 or height <= 0:
            raise ValueError('Invalid Shell bitmap dimensions')
        pixels = ctypes.create_string_buffer(width * height * 4)
        struct.pack_into(header_format, header, 0, ctypes.sizeof(header),
                         width, -height, 1, 32, 0, ctypes.sizeof(pixels), 0, 0, 0, 0)
        if get_bits(dc, bitmap, 0, height, pixels, header, 0) != height:
            raise ctypes.WinError(ctypes.get_last_error())
        return Image.frombytes('RGBA', (width, height), pixels.raw, 'raw', 'BGRA')
    finally:
        win32gui.DeleteDC(dc)


def desktop_listview():
    """Choose a full-size icon list owned by the desktop's Explorer process."""
    user32 = ctypes.windll.user32
    user32.GetShellWindow.argtypes = []
    user32.GetShellWindow.restype = W.HWND
    desktop = user32.GetShellWindow()
    if not desktop:
        raise RuntimeError('Cannot find the Windows desktop window; wait for the desktop to finish loading and try again.')
    shell_pid = win32process.GetWindowThreadProcessId(desktop)[1]
    primary = next(m[2] for m in monitors() if m[4])
    found = []

    def covers_primary(hwnd):
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        x0, y0 = win32gui.ClientToScreen(hwnd, (left, top))
        x1, y1 = win32gui.ClientToScreen(hwnd, (right, bottom))
        return x0 <= primary[0] < primary[2] <= x1 and y0 <= primary[1] < primary[3] <= y1

    def visit(hwnd, _):
        if win32gui.GetClassName(hwnd) == 'SHELLDLL_DefView' and covers_primary(hwnd):
            child = 0
            while True:
                child = win32gui.FindWindowEx(hwnd, child, 'SysListView32', None)
                if not child:
                    break
                if covers_primary(child):
                    found.append((hwnd, child))
        return True

    def top(hwnd, _):
        # Explorer's FolderBrowser also contains DefView/ListView controls, but is
        # not a desktop. Do not select it just because it contains more items.
        if (win32gui.GetClassName(hwnd) in ('Progman', 'WorkerW')
                and win32process.GetWindowThreadProcessId(hwnd)[1] == shell_pid):
            win32gui.EnumChildWindows(hwnd, visit, None)
        return True

    with dpi_context(-4):
        win32gui.EnumWindows(top, None)
    if not found:
        raise RuntimeError('Cannot find the Explorer desktop icon window covering the primary screen; make sure the Windows desktop has loaded.')

    def score(pair):
        try:
            return bool(win32gui.IsWindowVisible(pair[1])), listview_count(pair[1])
        except win32gui.error:
            return False, 0

    chosen = max(found, key=score)
    diag.log(f'desktop selection: shell_pid={shell_pid} candidates={found} chosen={chosen}')
    return chosen


def listview_count(hwnd):
    """LVM_GETITEMCOUNT of the desktop list view."""
    return int(win32gui.SendMessage(hwnd, 0x1004, 0, 0))


def item_rects(hwnd, indexes):
    """Read the icon and label rectangles for list-view ``indexes``.

    An entry is ``None`` when the list view cannot report that item (index beyond its
    count, a changed desktop, or a third-party desktop manager); the caller skips it
    instead of abandoning the whole game.
    """
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    kernel.OpenProcess.restype = W.HANDLE
    kernel.VirtualAllocEx.argtypes = [W.HANDLE, ctypes.c_void_p, ctypes.c_size_t, W.DWORD, W.DWORD]
    kernel.VirtualAllocEx.restype = ctypes.c_void_p
    kernel.WriteProcessMemory.argtypes = [W.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
    kernel.ReadProcessMemory.argtypes = kernel.WriteProcessMemory.argtypes
    kernel.VirtualFreeEx.argtypes = [W.HANDLE, ctypes.c_void_p, ctypes.c_size_t, W.DWORD]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    pid = win32process.GetWindowThreadProcessId(hwnd)[1]

    def failure(operation):
        code = ctypes.get_last_error()
        diag.log(f'icon bounds: {operation} failed winerror={code} target_pid={pid} hwnd={hwnd} access=0x38')
        message = f'Failed to read desktop icon positions ({operation}, Windows error {code}).'
        if code == 5:
            message += 'Windows denied access to the desktop process; make sure the game runs in the same logon session as the desktop and check your security software block log.'
        return ctypes.WinError(code, message)

    process = kernel.OpenProcess(0x38, False, pid)
    if not process:
        raise failure('OpenProcess')
    pointer = kernel.VirtualAllocEx(process, None, ctypes.sizeof(W.RECT), 0x3000, 4)
    try:
        if not pointer:
            raise failure('VirtualAllocEx')
        count = listview_count(hwnd)
        result = []
        for index in indexes:
            row = [] if 0 <= index < count else None
            for kind in (1, 2):
                if row is None:
                    break
                rect = W.RECT(kind, 0, 0, 0)
                if not kernel.WriteProcessMemory(process, pointer, ctypes.byref(rect), ctypes.sizeof(rect), None):
                    raise failure('WriteProcessMemory')
                if not win32gui.SendMessage(hwnd, 0x100E, index, pointer):
                    row = None
                    break
                if not kernel.ReadProcessMemory(process, pointer, ctypes.byref(rect), ctypes.sizeof(rect), None):
                    raise failure('ReadProcessMemory')
                row.append((rect.left, rect.top, rect.right, rect.bottom))
            if row is not None and not (row[0][2] > row[0][0] and row[0][3] > row[0][1]):
                row = None  # an empty icon rectangle cannot be drawn
            result.append(row)
        return result
    finally:
        if pointer:
            kernel.VirtualFreeEx(process, pointer, 0, 0x8000)
        kernel.CloseHandle(process)


def map_rect(rect, source_origin, target_origin):
    dx = source_origin[0] - target_origin[0]
    dy = source_origin[1] - target_origin[1]
    return (rect[0] + dx, rect[1] + dy, rect[2] + dx, rect[3] + dy)


def _load_font(size):
    try:
        return ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', size)
    except OSError:
        return ImageFont.load_default(size)


class LayerSurface:
    """Reuse one top-down BGRA DIB and DC for each desktop child."""

    def __init__(self, hwnd, size, x, y):
        self.hwnd = hwnd
        self.size = size
        self.bitmap = None
        self.old = None
        self.last_frame = None
        self.dc = win32gui.CreateCompatibleDC(0)
        self.bits = ctypes.c_void_p()
        try:
            width, height = size
            header = ctypes.create_string_buffer(struct.pack('<IiiHHIIiiII', 40, width, -height, 1, 32, 0,
                                                             width * height * 4, 0, 0, 0, 0))
            create = ctypes.windll.gdi32.CreateDIBSection
            create.argtypes = [W.HDC, ctypes.c_void_p, W.UINT, ctypes.POINTER(ctypes.c_void_p), W.HANDLE, W.DWORD]
            create.restype = W.HBITMAP
            self.bitmap = create(self.dc, header, 0, ctypes.byref(self.bits), None, 0)
            if not self.bitmap:
                raise ctypes.WinError()
            self.old = win32gui.SelectObject(self.dc, self.bitmap)
            self.point = (x, y)
        except Exception:
            self.close()
            raise

    def present(self, frame, x=None, y=None):
        if x is not None:
            # Layered child windows take coordinates relative to their parent.
            self.point = (x, y)
        if frame is not self.last_frame:
            data = frame.convert('RGBa').tobytes('raw', 'BGRa')
            ctypes.memmove(self.bits, data, len(data))
            self.last_frame = frame
        win32gui.UpdateLayeredWindow(self.hwnd, 0, self.point, self.size, self.dc, (0, 0), 0, (0, 0, 255, 1), 2)

    def close(self):
        if self.old:
            win32gui.SelectObject(self.dc, self.old)
            self.old = None
        if self.bitmap:
            win32gui.DeleteObject(self.bitmap)
            self.bitmap = None
        if self.dc:
            win32gui.DeleteDC(self.dc)
            self.dc = None


class TablePointer:
    """Turn the held left mouse button into aim, charge and a shot."""

    def __init__(self):
        self.was_down = True
        self.held_since = None
        self.charge_origin = None
        self.charge_falling = False

    def update(self, game, now, x, y, down, over_table):
        if over_table:
            game.aim_at(x, y)
        if down and not self.was_down and over_table:
            if game.state == 'ball_in_hand':
                game.place_white(x, y)
            elif game.state == 'ready':
                self.held_since = now
                self.charge_origin = (x, y)
        if self.held_since is not None:
            phase = (max(0., now - self.held_since) / 1.5) % 2
            self.charge_falling = phase >= 1
            power = 1 - abs(phase - 1)
            game.charge = power
            if not down:
                self.held_since = None
                self.charge_origin = None
                if over_table:
                    game.shoot(max(MIN_SHOT_CHARGE, power), now)
                else:
                    game.charge = 0.0
        self.was_down = down


class BilliardsOverlay:
    """Own the layered windows and the printable-level state of the table."""

    # Class defaults also cover lightweight test stubs built with __new__.
    window_mode = False
    resize_request = None
    move_drag = None
    window_position = None
    on_desktop = False

    def __init__(self, desktop, items, *, window_parent=None, titlebar_height=0):
        self.desktop = desktop
        self.titlebar_height = titlebar_height
        self.window_mode = window_parent is not None
        self.resize_request = None
        # Own list: the chosen icons and the padding clones are collected here and must never
        # reach the layout snapshot that the desktop restore reads (``items`` is that list).
        self.items = []
        self.desktop_total = len(items)
        self.real_count = 0
        self.padded = 0
        self.skipped = 0
        self.ball_info = ''
        self.blocked_by = None
        self.last_blocked = None
        self.credit_links = []
        self.credit_pressed = None
        self.credit_was_down = True
        self.link_requests = queue.SimpleQueue()
        # Bot mode changes are queued on Tk and applied on the animation thread.
        self.bot_commands = queue.SimpleQueue()
        self.bot_command_serial = 0
        self.bot_ack_serial = 0
        self.bot_ack_mode = 'off'
        self.on_desktop = False
        self.round_actions = queue.SimpleQueue()
        self.round_actions_key = None
        self.round_action_hover = None
        self.round_action_pressed = None
        self.round_action_was_down = True
        self.luma_map = None
        # Set from the UI thread, applied by the animation thread inside draw().
        self.want_hidden = False
        self.hidden = False
        self.want_felt = True
        self.felt_visible = True
        self.aim_visible = True
        self.table = None
        self.windows = []
        self.surfaces = {}
        self.sources = []
        self.templates = []
        self.anchors = []
        self.drawn = []
        self.poses = []
        self.transition = None
        self.restart_requested = threading.Event()
        self.pointer = TablePointer()
        self.bot = BotController()
        self.input_suspended = False
        self.input_pause_owned = False
        self.table_windows = {}
        self.guide_enabled = True
        self.guide_key = None
        self.guide_drawn = 0.0
        self.cue_key = None
        self.charge_key = None
        self.white_frame = None
        self.white_texture = None
        self.white_requests = queue.SimpleQueue()
        self.white_position = None
        if self.window_mode:
            self.parent, self.listview = window_parent, 0
            left, top, right, bottom = win32gui.GetClientRect(window_parent)
            self.geometry = (left, top, right, bottom)
            self.window_icon_size = max(16, min(64, round((right - left) / 20)))
            self.grip_size = 18
        else:
            self.parent, self.listview = desktop_listview()
            self.geometry = desktop.get_geometry()
        self.monitor_state = monitors()
        self.monitor_checked = 0.0
        self.fonts = []
        list_origin = win32gui.ClientToScreen(self.listview, (0, 0)) if self.listview else             win32gui.ClientToScreen(self.parent, (0, 0))
        parent_origin = win32gui.ClientToScreen(self.parent, (0, 0))
        self.log_environment(list_origin, parent_origin)
        chosen = self.choose_icons(items)
        self.real_count = len(chosen)
        self.items = [items[index] for index, _ in chosen]
        self.rects = [[map_rect(rect, list_origin, parent_origin) for rect in pair] for _, pair in chosen]
        primary = next(m for m in self.monitor_state if m[4])
        self.primary_screen = primary[1]
        self.primary_work = primary[2]
        self.primary_bounds = ((0, 0, right, bottom) if self.window_mode else
                               map_rect(primary[2], (0, 0), parent_origin))
        self.charge_font = _load_font(max(12, round(14 * primary[3] / 96)))
        left, top, right, bottom = win32gui.GetClientRect(self.parent)
        x0, y0, x1, y1 = self.primary_bounds
        if not self.window_mode and not (left <= x0 < x1 <= right and top <= y0 < y1 <= bottom):
            raise RuntimeError('The drawing window does not cover the primary screen; your original desktop icons were kept.')
        self.empty_frame = Image.new('RGBA', (1, 1))
        for icon, _ in self.rects:
            center = (round((icon[0] + icon[2]) / 2), round((icon[1] + icon[3]) / 2))
            screen = win32gui.ClientToScreen(self.parent, center)
            monitor = win32api.MonitorFromPoint(screen, win32con.MONITOR_DEFAULTTONEAREST)
            info = win32api.GetMonitorInfo(monitor)
            dpi = next(m[3] for m in self.monitor_state if m[0] == info['Device'])
            self.fonts.append(_load_font(max(9, round(12 * dpi / 96))))
        self.was_visible = True if self.window_mode else win32gui.IsWindowVisible(self.listview)
        if not self.was_visible:
            raise RuntimeError('Desktop icons could not be shown automatically; auto-show failed. Check the desktop icon window and restart the game.')
        dpi_hwnd = self.parent if self.window_mode else self.listview
        try:
            dpi = window_dpi(dpi_hwnd)
        except OSError:
            dpi = next((m[3] for m in self.monitor_state if m[4]), 96)
        self.font = _load_font(max(12, round(12 * dpi / 96)))
        self.classname = 'DesktopIconBilliardsOverlay'
        window_class = win32gui.WNDCLASS()
        window_class.hInstance = win32api.GetModuleHandle(None)
        window_class.lpszClassName = self.classname
        window_class.lpfnWndProc = win32gui.DefWindowProc
        try:
            win32gui.RegisterClass(window_class)
        except win32gui.error as exc:
            if exc.winerror != 1410:
                raise

    def choose_icons(self, items):
        """Find desktop icons with readable positions; artwork is checked before choosing balls.

        Returns ``[(shell_index, (icon_rect, label_rect)), ...]`` in desktop order.
        """
        order = priority_order(items)
        if self.window_mode:
            # No icon is hidden in window mode; positions are only used to size labels.
            size = getattr(self, 'window_icon_size', 48)
            return [(index, ((0, 0, size, size), (0, 0, size, max(10, size // 3))))
                    for index in order[:MAX_BALLS]]
        chosen, skipped, position = [], [], 0
        while position < len(order):
            batch = order[position:position + MAX_BALLS]
            position += len(batch)
            for index, pair in zip(batch, item_rects(self.listview, batch)):
                if pair is None:
                    skipped.append(index)
                else:
                    chosen.append((index, pair))
        self.skipped = len(skipped)
        diag.log(f'icons: shell={len(items)} listview={listview_count(self.listview) if self.listview else 0} '
                 f'chosen={len(chosen)} unreadable={skipped[:20]}')
        if not chosen:
            raise RuntimeError('No icons were retrieved. Make sure the desktop has usable icons and they are shown.')
        chosen.sort(key=lambda row: row[0])
        return chosen

    def log_environment(self, list_origin, parent_origin):
        """Record the display and Explorer facts that multi-monitor bugs depend on."""
        try:
            user32 = ctypes.windll.user32
            user32.GetWindowDpiAwarenessContext.argtypes = [W.HWND]
            user32.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
            user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int

            def describe(name, hwnd, origin):
                awareness = user32.GetAwarenessFromDpiAwarenessContext(user32.GetWindowDpiAwarenessContext(hwnd))
                return (f'{name}: hwnd={hwnd} class={win32gui.GetClassName(hwnd)} visible={win32gui.IsWindowVisible(hwnd)} '
                        f'client={win32gui.GetClientRect(hwnd)} window={win32gui.GetWindowRect(hwnd)} '
                        f'screen-origin={origin} dpi-awareness(0=unaware,1=system,2=per-monitor)={awareness}')

            lines = [f'windows: {platform.platform()}',
                     f'virtual screen / monitors: {self.geometry}']
            lines += [f'monitor device={m[0]} rect={m[1]} work={m[2]} dpi={m[3]} primary={m[4]}'
                      for m in self.monitor_state]
            lines += [describe('game parent' if self.window_mode else 'desktop parent', self.parent, parent_origin)]
            if self.listview:
                lines += [describe('desktop listview', self.listview, list_origin)]
            lines += [f'swapped mouse buttons: {bool(win32api.GetSystemMetrics(SM_SWAPBUTTON))}']
            diag.log(*lines)
        except Exception as exc:  # diagnostics only
            diag.log(f'environment logging failed: {exc!r}')

    # -- preparation -------------------------------------------------------
    # Art rebuilt by ``prepare()``; the live table/window state is deliberately absent.
    ART_FIELDS = ('items', 'rects', 'fonts', 'sources', 'templates', 'anchors', 'drawn',
                  'poses', 'intro_sources', 'ring_frames', 'frame_cache', 'frame_cache_bytes',
                  'rail_frame', 'pocket_frame', 'credit_links', 'luma_map', 'skipped',
                  'real_count', 'padded', 'ball_info', 'original_items', 'original_rects',
                  'original_fonts', 'original_sources')

    def speculative_state(self):
        """Clone art fields so a failed rebuild cannot touch live windows or art."""
        state = object.__new__(BilliardsOverlay)
        state.__dict__.update(self.__dict__)
        for name in ('items', 'rects', 'fonts', 'sources', 'templates', 'anchors', 'drawn',
                     'poses', 'original_items', 'original_rects', 'original_fonts',
                     'original_sources'):
            if name in self.__dict__:
                state.__dict__[name] = list(self.__dict__[name])
        return state

    def commit_art_state(self, state):
        """Publish a successfully built speculative state to the live overlay."""
        for name in self.ART_FIELDS:
            if name in state.__dict__:
                setattr(self, name, state.__dict__[name])

    def prepare(self):
        # Run in a worker: thumbnail extraction can access disk / Shell extensions.
        comtypes.CoInitialize()
        try:
            self.load_icon_sources()
            self.build_templates()
            screens = [monitor[1] for monitor in self.monitor_state]
            desktop_bounds = (min(rect[0] for rect in screens), min(rect[1] for rect in screens),
                              max(rect[2] for rect in screens), max(rect[3] for rect in screens))
            wallpaper = primary_wallpaper(self.primary_screen, desktop_bounds)
            if wallpaper is not None:
                wallpaper = wallpaper.crop(map_rect(self.primary_work, (0, 0), self.primary_screen[:2]))
                # Coarse brightness map: the aim guide picks a palette that contrasts with it.
                self.luma_map = wallpaper.convert('L').resize(
                    (max(1, wallpaper.width // LUMA_CELL), max(1, wallpaper.height // LUMA_CELL)),
                    Image.Resampling.BOX)
            credit = CREDIT
            self.rail_frame = table_art(self.table, wallpaper, credit)
            _, rows = credit_layout(self.table, credit)
            self.credit_links = [(row[3], REPOSITORY_URL) for row in rows]
            icon_sizes = sorted(min(icon[2] - icon[0], icon[3] - icon[1]) for icon, _ in self.rects)
            icon_size = icon_sizes[len(icon_sizes) // 2]
            if self.window_mode:
                # Scale the recycle-bin pocket art to the table, not the real desktop icon size.
                icon_size = max(12, min(round(self.table.tile * 1.1), round(self.table.pocket_radius * 1.6)))
            recycle_bin = artwork('::{645FF040-5081-101B-9F08-00AA002F954E}', icon_size)
            recycle_bin.thumbnail((icon_size, icon_size), Image.Resampling.LANCZOS)
            self.pocket_frame = pocket_art(self.table, recycle_bin)
        finally:
            comtypes.CoUninitialize()

    def _pad_icon_sources(self):
        """Rebuild the working arrays from the filtered baselines, then pad once."""
        self.items = list(self.original_items)
        self.rects = list(self.original_rects)
        self.fonts = list(self.original_fonts)
        self.sources = list(self.original_sources)
        self.real_count = len(self.sources)
        extra = fill_sources(self.real_count)
        for index in extra:
            self.items.append(dict(self.items[index]))
            self.rects.append(self.rects[index])
            self.fonts.append(self.fonts[index])
            self.sources.append(self.sources[index].copy())
        self.padded = len(extra)
        self.ball_info = ball_summary(self.desktop_total, self.real_count, self.padded)

    def load_icon_sources(self):
        """Filter artwork once, then rebuild padded arrays from that original set."""
        if hasattr(self, 'original_sources'):
            self._pad_icon_sources()
            return

        import pywintypes

        loaded = []
        for index in priority_order(self.items):
            item = self.items[index]
            icon, _ = self.rects[index]
            size = min(icon[2] - icon[0], icon[3] - icon[1])
            source = None
            for requested in dict.fromkeys((max(128, size), size)):
                try:
                    candidate = artwork(item['id'], requested)
                    candidate.thumbnail((requested, requested), Image.Resampling.LANCZOS)
                    if not candidate.getchannel('A').getbbox():
                        raise ValueError('empty icon artwork')
                    source = candidate
                    break
                except (OSError, ValueError, pywintypes.com_error, comtypes.COMError) as error:
                    diag.log(f'icon artwork attempt failed: index={index} size={requested} '
                             f'error={type(error).__name__} '
                             f'code={getattr(error, "hresult", getattr(error, "winerror", None))}')
            if source is None:
                self.skipped += 1
                continue
            loaded.append((index, source))
            if len(loaded) == MAX_BALLS:
                break
        if not loaded:
            raise RuntimeError('No icons were retrieved. Make sure the desktop has usable icons and they are shown.')
        loaded.sort(key=lambda row: row[0])
        # Baselines are filtered, never cloned: cropping replaces list entries.
        self.original_items = [self.items[index] for index, _ in loaded]
        self.original_rects = [self.rects[index] for index, _ in loaded]
        self.original_fonts = [self.fonts[index] for index, _ in loaded]
        self.original_sources = [source for _, source in loaded]
        # Rebuild working arrays from the baselines on every load, including the
        # first one. A resize therefore never pads an already padded list.
        self._pad_icon_sources()
        diag.log(f'balls: {self.ball_info}; unreadable={self.skipped}')

    def build_templates(self):
        """Build label canvases and the resting pose each icon starts from."""
        self.frame_cache = OrderedDict()
        self.frame_cache_bytes = 0
        # Reset every array before a rebuild; never append onto art from a
        # previous size (that grew the arrays and desynchronised the poses).
        self.templates = []
        self.anchors = []
        self.drawn = []
        desktop_sizes = []
        for index, (source, (icon, label)) in enumerate(zip(self.sources, self.rects)):
            desktop_source = source.copy()
            desktop_source.thumbnail((icon[2] - icon[0], icon[3] - icon[1]), Image.Resampling.LANCZOS)
            left = min(icon[0], label[0]) - 40
            top = icon[1] - 48
            frame = Image.new('RGBA', (max(icon[2], label[2]) - left + 40, max(icon[3], label[3]) - top + 12))
            font = self.fonts[index] if index < len(self.fonts) else self.font
            text = self.items[index]['name']
            draw = ImageDraw.Draw(frame)
            max_width = max(20, label[2] - label[0])
            lines = []
            line = ''
            for char in text:
                if draw.textlength(line + char, font=font) > max_width and line:
                    lines.append(line)
                    line = ''
                line += char
            if line:
                lines.append(line)
            line_count = max(1, min(2, (label[3] - label[1]) // max(1, font.size + 2)))
            for line_index, line in enumerate(lines[:line_count]):
                if line_index == line_count - 1 and len(lines) > line_count:
                    while line and draw.textlength(line + '…', font=font) > max_width:
                        line = line[:-1]
                    line += '…'
                tx = (label[0] + label[2]) / 2 - left
                ty = label[1] - top + line_index * (font.size + 2)
                draw.text((tx + 1, ty + 1), line, font=font, anchor='mt', fill=(0, 0, 0, 210), stroke_width=1)
                draw.text((tx, ty), line, font=font, anchor='mt', fill='white')
            bounds = source.getchannel('A').getbbox()
            if not bounds:
                raise RuntimeError('The system returned an empty icon')
            desktop_bounds = desktop_source.getchannel('A').getbbox()
            if not desktop_bounds:
                raise RuntimeError('The system returned an empty icon')
            center = ((icon[0] + icon[2]) / 2 - left - desktop_source.width / 2
                      + (desktop_bounds[0] + desktop_bounds[2]) / 2)
            bottom = icon[3] - top - desktop_source.height + desktop_bounds[3]
            desktop_sizes.append((desktop_bounds[2] - desktop_bounds[0], desktop_bounds[3] - desktop_bounds[1]))
            self.sources[index] = source.crop(bounds)
            self.templates.append(frame)
            self.anchors.append((left, top, center, bottom))
            self.drawn.append(None)
        self.poses = []
        for (width, height), (left, top, center, bottom) in zip(desktop_sizes, self.anchors):
            # Resting pose at the real desktop position, before the rack is built.
            self.poses.append((left + center, top + bottom - height / 2,
                               width, height, 0, 0, 1.0))
        self.table = BilliardsMotion(self.primary_bounds, [source.size for source in self.sources])
        self.intro_sources = self.sources
        self.sources = []
        for source in self.intro_sources:
            ratio = self.table.tile / max(source.size)
            size = (max(1, round(source.width * ratio)), max(1, round(source.height * ratio)))
            self.sources.append(source.resize(size, Image.Resampling.LANCZOS))
        # All icons share the collision radius. Prepare the six immutable layers
        # once so simultaneous impacts never resize the same artwork per ball.
        self.ring_frames = {level: self.ring_layer(level) for level in range(1, RING_LEVELS + 1)}

    def request_resize(self, width, height):
        """Queued from the Tk thread; applied by the animation thread."""
        width = max(320, int(width) // 2 * 2)
        height = width // 2 if self.window_mode else max(240, int(height))
        self.resize_request = (width, height)

    def apply_resize(self):
        size, self.resize_request = self.resize_request, None
        if size is None or not self.window_mode:
            return
        width, height = size
        if win32gui.GetClientRect(self.parent)[2:] == (width, height) and self.table.work_area == (0, 0, width, height):
            return
        old = self.table
        previous_bounds, previous_geometry = self.primary_bounds, self.geometry
        self.primary_bounds = (0, 0, width, height)
        self.geometry = (0, 0, width, height)
        candidate = self.speculative_state()
        try:
            candidate.prepare()  # rebuilds table art and rail frames for the new size
        except Exception:
            # Nothing has been published or destroyed yet: old art stays live.
            self.primary_bounds, self.geometry = previous_bounds, previous_geometry
            diag.log('window resize art refresh failed; kept previous table')
            return
        self.commit_art_state(candidate)
        # Only after a successful build may the old table windows go away.
        self.clear_table()
        self.table = old.rescaled(self.primary_bounds)
        self.drawn = [None] * len(self.items)
        self.cue_key = self.guide_key = self.charge_key = self.white_position = None
        self.white_frame = None
        self.draw_table()  # present the new rail and pockets on fresh windows
        if not self.felt_visible:
            self.set_felt(False)  # fresh rail windows must honour hidden-felt mode
        diag.log(f'window resized to {width}x{height}; round state preserved')

    # -- lifecycle ---------------------------------------------------------
    def show(self, snapshot, journal):
        from .shell import atomic_json

        now = time.perf_counter()
        screen = self.read_pointer(now)
        if screen is None:
            return False  # Keep real icons visible while the input desktop is unavailable.
        try:
            for _ in self.items:
                hwnd = win32gui.CreateWindowEx(
                    WS_EX_LAYERED | 0x20 | WS_EX_NOACTIVATE, self.classname, '', win32con.WS_CHILD,
                    0, 0, 1, 1, self.parent, 0, win32api.GetModuleHandle(None), None)
                self.windows.append(hwnd)
            if not self.window_mode:
                self.transition = {'elapsed': 0.0, 'origins': [list(pose) for pose in self.poses]}
            else:
                self.transition = None  # desktop-to-rack fly-in is meaningless in a game window
            self.draw(now, 0.016, screen)
            if not self.window_mode:
                snapshot['hidden_listview'] = self.listview
                atomic_json(journal, snapshot)
                win32gui.ShowWindow(self.listview, win32con.SW_HIDE)
            for hwnd in self.windows:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
            self.stack_table_windows()
        except Exception:
            self.close()
            raise

    def request_restart(self):
        self.restart_requested.set()

    def request_bot_cycle(self):
        """Queue a mode change from Tk; the animation thread applies it."""
        self.bot_command_serial += 1
        self.bot_commands.put(self.bot_command_serial)
        return self.bot_command_serial

    def consume_bot_commands(self):
        """Apply queued bot commands on the animation thread, in arrival order."""
        changed = False
        while True:
            try:
                serial = self.bot_commands.get_nowait()
            except queue.Empty:
                break
            mode = self.bot.cycle()
            if mode != 'off':
                # Fresh slate so scores never leak across difficulty switches.
                self.bot.scores = {'human': 0, 'bot': 0}
            self.bot_ack_serial = serial
            self.bot_ack_mode = mode
            changed = True
        return changed

    def request_white_texture(self, texture):
        self.white_requests.put(texture.copy() if texture is not None else None)

    def set_hidden(self, hidden, now):
        """Hide every game window and show the real icons again (or the reverse), pausing the round."""
        shown = win32con.SW_HIDE if hidden else win32con.SW_SHOWNOACTIVATE
        if hidden:
            for hwnd in [*self.windows, *self.table_windows.values()]:
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            if not self.window_mode:
                win32gui.ShowWindow(self.listview, win32con.SW_SHOWNA)
            self.table.pause(now)
        else:
            if not self.window_mode:
                win32gui.ShowWindow(self.listview, win32con.SW_HIDE)
            for hwnd in self.windows:
                win32gui.ShowWindow(hwnd, shown)
            pockets = self.table_windows.get('pockets')
            if pockets and not self.want_felt:
                win32gui.ShowWindow(pockets, shown)
            rail = self.table_windows.get('rail')
            if rail and self.want_felt:
                win32gui.ShowWindow(rail, shown)
            # Forget what each layer last showed so the next frames re-show cue, guide and white ball.
            self.cue_key = self.guide_key = self.charge_key = self.white_position = None
            self.round_actions_key = None
            self.drawn = [None] * len(self.items)
            self.pointer = TablePointer()
            self.stack_table_windows()
            self.table.resume(now)
        self.hidden = hidden
        diag.log(f'game {"hidden" if hidden else "shown again"}')

    def set_felt(self, visible):
        """Show or hide the table frame and wallpaper panel, keeping the wastebaskets; the balls keep rolling on the desktop."""
        rail = self.table_windows.get('rail')
        if rail is None:
            return
        if visible:
            win32gui.ShowWindow(rail, win32con.SW_SHOWNOACTIVATE)
            self.position_layer(rail, win32con.HWND_BOTTOM)
        else:
            win32gui.ShowWindow(rail, win32con.SW_HIDE)
        pockets = self.table_windows.get('pockets')
        if pockets:
            win32gui.ShowWindow(pockets, win32con.SW_HIDE if visible else win32con.SW_SHOWNOACTIVATE)
        self.felt_visible = visible
        self.cue_key = self.guide_key = self.charge_key = None
        for part in ('cue', 'guide', 'charge'):
            if part in self.table_windows:
                win32gui.ShowWindow(self.table_windows[part], win32con.SW_HIDE)

    def draw(self, now, dt, screen=None):
        if self.window_mode and (win32gui.IsIconic(self.parent) or not win32gui.IsWindowVisible(self.parent)):
            self.table.pause(now)
            return
        if self.window_mode and self.resize_request is not None:
            self.apply_resize()
        # A lock/UAC/session transition can temporarily deny input-desktop access.
        # Do not query the changing display or advance physics until it returns.
        if screen is None:
            screen = self.read_pointer(now)
        if screen is None:
            return
        if not self.window_mode and self.desktop.get_geometry() != self.geometry:
            raise RuntimeError('Display layout changed; the game was stopped and the desktop restored')
        if not self.window_mode and now - self.monitor_checked >= 0.5:
            self.monitor_checked = now
            if monitors() != self.monitor_state:
                raise RuntimeError('Screen layout or DPI scaling changed; the desktop was restored, please start again.')
        if self.transition is None and self.want_hidden != self.hidden:
            self.set_hidden(self.want_hidden, now)
        if self.hidden:
            return  # the real desktop is showing: draw and simulate nothing
        if self.transition is None and self.want_felt != self.felt_visible:
            self.set_felt(self.want_felt)
        if self.transition is not None:
            self.draw_intro(now, dt)
        else:
            if self.restart_requested.is_set():
                self.restart_requested.clear()
                self.table.reset()
                self.pointer = TablePointer()
                self.drawn = [None] * len(self.items)
            self.update_pointer(now, screen)
            self.table.step(dt, now)
            self.draw_balls()
        shift = bool(win32api.GetAsyncKeyState(win32con.VK_SHIFT) & 0x8000)
        self.aim_visible = self.felt_visible or (self.on_desktop and self.keyboard_focus_valid() and shift)
        self.draw_table()
        self.draw_guide()
        self.draw_charge()
        self.draw_round_actions()

    def keyboard_focus_valid(self):
        """True only when the game (or the desktop in desktop mode) owns focus."""
        try:
            foreground = win32gui.GetForegroundWindow()
            if not foreground:
                return False
            focus_pid = win32process.GetWindowThreadProcessId(foreground)[1]
            if self.window_mode:
                root = win32gui.GetAncestor(self.parent, win32con.GA_ROOT) or self.parent
                return foreground in (root, self.parent) or focus_pid == os.getpid()
            return (win32gui.GetClassName(foreground) in ('Progman', 'WorkerW')
                    and focus_pid == win32process.GetWindowThreadProcessId(self.parent)[1])
        except win32gui.error:
            return False

    def read_pointer(self, now):
        """Suspend only an access-denied cursor read; unrelated failures stay visible."""
        try:
            screen = win32gui.GetCursorPos()
        except win32gui.error as error:
            if error.winerror != 5:
                raise
            if not self.input_suspended:
                self.input_suspended = True
                self.input_pause_owned = self.table.paused_at is None
                if self.input_pause_owned:
                    self.table.pause(now)
                self.pointer = TablePointer()
                self.table.charge = 0.
                self.credit_pressed = None
                self.credit_was_down = True
                self.round_action_pressed = None
                self.round_action_was_down = True
                diag.log('cursor access denied; round paused and pending input cancelled')
            return None
        if self.input_suspended:
            if self.input_pause_owned:
                self.table.resume(now)
            self.input_suspended = self.input_pause_owned = False
            diag.log('cursor access restored; round resumed, waiting for a fresh press')
        return screen

    def update_window_drag(self, screen):
        """Right-button drag moves the borderless window; Tk never sees those clicks."""
        right = bool(win32api.GetAsyncKeyState(win32con.VK_RBUTTON) & 0x8000)
        if not right:
            self.move_drag = None
            return False
        try:
            root = win32gui.GetAncestor(self.parent, win32con.GA_ROOT) or self.parent
            if right and self.move_drag is None:
                left, top, _, _ = win32gui.GetWindowRect(root)
                self.move_drag = (screen[0] - left, screen[1] - top)
                self.pointer = TablePointer()  # never turn a drag into a shot
            if self.move_drag is None:
                return False
            dx, dy = self.move_drag
            x, y = screen[0] - dx, screen[1] - dy
            win32gui.SetWindowPos(root, 0, x, y, 0, 0,
                                  win32con.SWP_NOSIZE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
        except (win32gui.error, TypeError, ValueError):
            self.move_drag = None
            return False  # a transient HWND must never kill the game loop
        self.window_position = (x, y)
        return True

    def update_pointer(self, now, screen=None):
        if screen is None:
            screen = self.read_pointer(now)
            if screen is None:
                return
        if self.window_mode and self.update_window_drag(screen):
            return
        x, y = win32gui.ScreenToClient(self.parent, screen)
        left, top, right, bottom = self.table.bounds
        on_desktop = False
        blocked = None
        # Persist for the aim gate: a stale SHIFT from another app must not draw the cue.
        self.on_desktop = False
        if self.window_mode:
            grip = getattr(self, 'grip_size', 0)
            outside_grip = not (x > right - grip and y > bottom - grip)
            on_desktop = (left <= x <= right and self.titlebar_height <= y <= bottom
                          and outside_grip and win32gui.IsWindowVisible(self.parent))
            blocked = None
        elif left <= x <= right and top <= y <= bottom:
            hwnd = win32gui.WindowFromPoint(screen)
            top_window = win32gui.GetAncestor(hwnd, win32con.GA_ROOT) if hwnd else 0
            if top_window:
                name = win32gui.GetClassName(top_window)
                on_desktop = name in ('Progman', 'WorkerW')
                if not on_desktop and win32process.GetWindowThreadProcessId(top_window)[1] != os.getpid():
                    title = win32gui.GetWindowText(top_window)[:30]
                    blocked = (title or name, name)  # the player sees the title, the log keeps the class
        self.on_desktop = on_desktop
        self.blocked_by = blocked[0] if blocked else None
        if blocked != self.last_blocked:
            self.last_blocked = blocked
            if blocked:
                diag.log(f'mouse over the table is blocked by another window: {blocked[0]} (class {blocked[1]})')
        # GetAsyncKeyState reports the physical buttons; with swapped buttons the player's
        # primary (logical left) button is the physical right one.
        button = win32con.VK_RBUTTON if win32api.GetSystemMetrics(SM_SWAPBUTTON) else win32con.VK_LBUTTON
        down = bool(win32api.GetAsyncKeyState(button) & 0x8000)
        if any(l <= x < r and t <= y < b for (l, t, r, b), _ in self.credit_links):
            hwnd = win32gui.WindowFromPoint(screen)
            root = win32gui.GetAncestor(hwnd, win32con.GA_ROOT) if hwnd else 0
            over_credit = bool(root and win32gui.GetClassName(root) in ('Progman', 'WorkerW'))
        else:
            over_credit = False
        self.update_credit_pointer(x, y, down, over_credit)
        self.update_round_action_pointer(x, y, down, on_desktop)
        self.consume_bot_commands()
        if self.bot.enabled:
            self.bot.update(self.table, now)
        human_down = down and not self.bot.bot_turn
        human_over = on_desktop and not self.bot.bot_turn
        self.pointer.update(self.table, now, x, y, human_down, human_over)

    def update_round_action_pointer(self, x, y, down, on_desktop):
        serial = self.table.round_serial
        target = None
        if self.table.state == 'finished' and on_desktop:
            _, rows = round_actions_layout(self.table)
            target = next((action for action, _, (l, t, r, b) in rows if l <= x < r and t <= y < b), None)
        self.round_action_hover = target
        if down and not self.round_action_was_down:
            self.round_action_pressed = (serial, target) if target else None
        elif not down and self.round_action_was_down:
            if target and self.round_action_pressed == (serial, target):
                self.round_actions.put((serial, target))
            self.round_action_pressed = None
        if not target:
            self.round_action_pressed = None
        self.round_action_was_down = down

    def update_credit_pointer(self, x, y, down, on_desktop):
        target = next((url for (l, t, r, b), url in self.credit_links
                       if l <= x < r and t <= y < b), None) if self.felt_visible and on_desktop else None
        if down and not self.credit_was_down:
            self.credit_pressed = target
        elif not down and self.credit_was_down:
            if target and target == self.credit_pressed:
                self.link_requests.put(target)
            self.credit_pressed = None
        if not target:
            self.credit_pressed = None
        self.credit_was_down = down

    # -- drawing -----------------------------------------------------------
    def draw_intro(self, now, dt):
        """Fly the real icons from their desktop positions into the rack."""
        transition = self.transition
        transition['elapsed'] += max(0, min(0.05, dt))
        for index, (hwnd, origin) in enumerate(zip(self.windows, transition['origins'])):
            target = self.table.poses[index]
            t = max(0, min(1, (transition['elapsed'] - (index % 7) * INTRO_STAGGER) / INTRO_DURATION))
            ease = t * t * t * (10 + t * (-15 + 6 * t))
            values = [start + (end - start) * ease for start, end in zip(origin, target)]
            dx = target[0] - origin[0]
            dy = target[1] - origin[1]
            distance = max(1, math.hypot(dx, dy))
            arc = math.sin(math.pi * t) ** 2 * min(65, distance * 0.10) * (1 if index % 2 else -1)
            values[0] -= dy / distance * arc
            values[1] += dx / distance * arc
            self.poses[index] = tuple(values)
            cx, cy, width, height, angle, _shift, label = values
            width = max(1, round(width))
            height = max(1, round(height))
            template = self.templates[index]
            source = self.intro_sources[index]
            left, top, center, bottom = self.anchors[index]
            sprite_extent = math.ceil(math.hypot(width, height)) + 4
            frame = Image.new('RGBA', (max(math.ceil(template.width * 2), sprite_extent),
                                       max(math.ceil(template.height * 2), sprite_extent)))
            mx, my = frame.width // 2, frame.height // 2
            if label > 0:
                labels = template.copy()
                labels.putalpha(labels.getchannel('A').point(lambda value: round(value * label)))
                frame.alpha_composite(labels, (round(mx - center), round(my - bottom + height / 2)))
            picture = source.resize((width, height), Image.Resampling.BICUBIC)
            picture = picture.rotate(angle, Image.Resampling.BICUBIC, expand=True)
            frame.alpha_composite(picture, (round(mx - picture.width / 2), round(my - picture.height / 2)))
            self.present(hwnd, frame, round(cx - mx), round(cy - my))
        if transition['elapsed'] >= INTRO_DURATION + 6 * INTRO_STAGGER:
            self.transition = None
            self.drawn = [None] * len(self.items)

    def draw_balls(self):
        for index, (hwnd, values) in enumerate(zip(self.windows, self.table.poses)):
            self.poses[index] = values
            cx, cy, width, height, angle, _shift, _label = values
            angle = round(angle)
            alpha = 0 if index + 1 in self.table.sunk else 255
            effect = self.table.exit_visual(index + 1) if not alpha else None
            fade = 0.0
            if effect:
                cx, cy, fade, scale, spin = effect
                width = max(1, round(width * scale))
                height = max(1, round(height * scale))
                angle = round(angle + spin)
                alpha = round(255 * (1 - fade) ** 1.35)
                self.poses[index] = (cx, cy, width, height, angle, 0, 0.0)
            # A faint ring flashes at the collision radius while a ball is being hit.
            impact = self.table.icons[index].impact
            ring = min(RING_LEVELS, math.ceil(impact / MAX_IMPACT * RING_LEVELS)) if alpha == 255 and impact > .012 else 0
            key = ((round(cx), round(cy), width, height, angle, alpha, round(fade * 40), ring)
                   if alpha or effect else (0,))
            if self.drawn[index] == key:
                continue
            frame = self.sprite_frame(self.sources[index], index, width, height, angle, alpha, ring)
            if fade:
                frame = pocket_particles(frame, index, fade)
            self.present(hwnd, frame, round(cx - BALL_CANVAS / 2), round(cy - BALL_CANVAS / 2))
            self.drawn[index] = key

    def sprite_frame(self, source, identity, width, height, angle, alpha=255, ring=0):
        key = (identity, width, height, angle, BALL_CANVAS, alpha, ring)
        if key in self.frame_cache:
            self.frame_cache.move_to_end(key)
            return self.frame_cache[key]
        frame = Image.new('RGBA', (BALL_CANVAS, BALL_CANVAS))
        if alpha:
            picture = source.resize((width, height), Image.Resampling.BICUBIC)
            picture = picture.rotate(angle, Image.Resampling.BICUBIC, expand=True)
            if alpha < 255:
                picture.putalpha(picture.getchannel('A').point([value * alpha // 255 for value in range(256)]))
            frame.alpha_composite(picture, ((BALL_CANVAS - picture.width) // 2,
                                            (BALL_CANVAS - picture.height) // 2))
            if ring:
                frame.alpha_composite(self.ring_frames[ring])
        self.frame_cache[key] = frame
        self.frame_cache_bytes += BALL_CANVAS * BALL_CANVAS * 4
        while len(self.frame_cache) > 256 or self.frame_cache_bytes > 16 * 1024 * 1024:
            _, old = self.frame_cache.popitem(last=False)
            self.frame_cache_bytes -= old.width * old.height * 4
        return frame

    def ring_layer(self, level):
        """The collision circle of one ball, drawn very faintly (``level`` 1..RING_LEVELS)."""
        scale = 3
        layer = Image.new('RGBA', (BALL_CANVAS * scale, BALL_CANVAS * scale))
        radius = self.table.radius * scale
        centre = BALL_CANVAS * scale / 2
        box = (centre - radius, centre - radius, centre + radius, centre + radius)
        alpha = round(28 + 12 * level)
        draw = ImageDraw.Draw(layer)
        draw.ellipse(box, fill=(190, 255, 232, alpha // 5), outline=(190, 255, 232, alpha), width=2 * scale)
        return layer.resize((BALL_CANVAS, BALL_CANVAS), Image.Resampling.LANCZOS)

    def table_window(self, part):
        hwnd = win32gui.CreateWindowEx(
            WS_EX_LAYERED | 0x20 | WS_EX_NOACTIVATE, self.classname, '', win32con.WS_CHILD,
            0, 0, 1, 1, self.parent, 0, win32api.GetModuleHandle(None), None)
        self.table_windows[part] = hwnd
        return hwnd

    @staticmethod
    def position_layer(hwnd, after, flags=0):
        win32gui.SetWindowPos(hwnd, after, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE | flags)

    def stack_table_windows(self):
        """Keep the wallpaper behind the icons and the shot controls above them."""
        self.position_layer(self.table_windows['guide'], win32con.HWND_BOTTOM)
        self.position_layer(self.table_windows['pockets'], win32con.HWND_BOTTOM)
        self.position_layer(self.table_windows['rail'], win32con.HWND_BOTTOM)
        for part in ('white', 'cue', 'charge', 'round_actions'):
            if part in self.table_windows:
                self.position_layer(self.table_windows[part], win32con.HWND_TOP)

    def draw_table(self):
        table = self.table
        if 'rail' not in self.table_windows:
            hwnd = self.table_window('rail')
            self.present(hwnd, *self.rail_frame)
            self.rail_frame = None
            self.position_layer(hwnd, win32con.HWND_BOTTOM, win32con.SWP_SHOWWINDOW)
            pockets = self.table_window('pockets')
            self.present(pockets, *self.pocket_frame)
            self.pocket_frame = None
            self.position_layer(pockets, win32con.HWND_BOTTOM)
            self.position_layer(hwnd, win32con.HWND_BOTTOM)
            self.table_window('guide')
        white = table.white
        while not self.white_requests.empty():
            self.white_texture = self.white_requests.get_nowait()
            self.white_frame = None
        if 'white' not in self.table_windows:
            self.table_window('white')
        if self.white_frame is None:
            self.white_frame = white_art(white.radius, shaded=True, texture=self.white_texture)
            self.white_position = None
        hwnd = self.table_windows['white']
        if table.white_sunk:
            if self.white_position is not None:
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            self.white_position = None
        else:
            position = (round(white.x - self.white_frame.width / 2), round(white.y - self.white_frame.height / 2))
            if self.white_position != position:
                self.present(hwnd, self.white_frame, *position)
                if self.white_position is None:
                    self.position_layer(hwnd, win32con.HWND_TOP,
                                        win32con.SWP_SHOWWINDOW | win32con.SWP_NOZORDER)
                self.white_position = position
        cue = table.cue() if self.aim_visible else None
        if cue is None:
            if self.cue_key is not None:
                win32gui.ShowWindow(self.table_windows['cue'], win32con.SW_HIDE)
                self.cue_key = None
            return
        key = tuple(round(value) for point in cue for value in point)
        if key == self.cue_key:
            return
        hwnd = self.table_windows.get('cue') or self.table_window('cue')
        self.present(hwnd, *cue_art(cue, thickness=1.65))
        if self.cue_key is None:
            self.position_layer(hwnd, win32con.HWND_TOP,
                                win32con.SWP_SHOWWINDOW | win32con.SWP_NOZORDER)
        self.cue_key = key

    def background_luma(self, x, y):
        """Wallpaper brightness (0-255) under a table point, or None without a readable wallpaper."""
        if self.luma_map is None:
            return None
        origin_x, origin_y = self.table.work_area[:2]
        px, py = int((x - origin_x) // LUMA_CELL), int((y - origin_y) // LUMA_CELL)
        width, height = self.luma_map.size
        return self.luma_map.getpixel((px, py)) if 0 <= px < width and 0 <= py < height else None

    def draw_guide(self):
        lines, circles = self.table.aim_preview() if self.aim_visible and self.guide_enabled and self.transition is None else ((), ())
        rounded = [(role, (round(x0), round(y0)), (round(x1), round(y1)))
                   for role, (x0, y0), (x1, y1) in lines]
        lines_key = tuple((role, start, end) for role, start, end in rounded if start != end)
        key = (lines_key, tuple((role, (round(x), round(y)), round(radius)) for role, (x, y), radius in circles))             if lines_key else None
        if key == self.guide_key:
            return
        clock = time.perf_counter()
        if key is not None and clock - self.guide_drawn < GUIDE_INTERVAL:
            return  # redrawing the guide is the costliest step: cap it, the next frame catches up
        hwnd = self.table_windows['guide']
        if key is None:
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
        else:
            self.present(hwnd, *guide_art(key[0], key[1], self.background_luma))
            self.guide_drawn = clock
            if self.guide_key is None:
                self.position_layer(hwnd, win32con.HWND_TOP, win32con.SWP_SHOWWINDOW | win32con.SWP_NOZORDER)
        self.guide_key = key

    def draw_charge(self):
        origin = self.pointer.charge_origin
        if origin is None or self.table.state != 'ready':
            if self.charge_key is not None:
                win32gui.ShowWindow(self.table_windows['charge'], win32con.SW_HIDE)
                self.charge_key = None
            return

        percent = round(self.table.charge * 100)
        key = (origin, percent, self.pointer.charge_falling)
        if key == self.charge_key:
            return
        frame = charge_art(percent, self.charge_font, self.pointer.charge_falling)
        width, height = frame.size

        left, top, right, bottom = self.primary_bounds
        x, y = origin
        gap = self.charge_font.size
        x = x + gap if x + gap + width <= right else x - gap - width
        y = y + gap if y + gap + height <= bottom else y - gap - height
        hwnd = self.table_windows.get('charge') or self.table_window('charge')
        self.present(hwnd, frame, max(left, x), max(top, y))
        if self.charge_key is None:
            self.position_layer(hwnd, win32con.HWND_TOP, win32con.SWP_SHOWWINDOW)
        self.charge_key = key

    def draw_round_actions(self):
        if self.table.state != 'finished':
            if self.round_actions_key is not None:
                win32gui.ShowWindow(self.table_windows['round_actions'], win32con.SW_HIDE)
                self.round_actions_key = None
            return
        pressed = self.round_action_pressed[1] if self.round_action_pressed else None
        key = (self.table.round_serial, self.round_action_hover, pressed)
        if key == self.round_actions_key:
            return
        hwnd = self.table_windows.get('round_actions') or self.table_window('round_actions')
        self.present(hwnd, *round_actions_art(self.table, self.round_action_hover, pressed))
        self.position_layer(hwnd, win32con.HWND_TOP, win32con.SWP_SHOWWINDOW)
        self.round_actions_key = key

    def clear_table(self):
        for hwnd in self.table_windows.values():
            surface = self.surfaces.pop(hwnd, None)
            if surface:
                surface.close()
            if win32gui.IsWindow(hwnd):
                win32gui.DestroyWindow(hwnd)
        self.table_windows.clear()
        self.guide_key = None
        self.guide_drawn = 0.0
        self.cue_key = None
        self.charge_key = None
        self.white_position = None
        self.round_actions_key = None

    def present(self, hwnd, frame, x, y):
        if not self.felt_visible and hwnd in (self.table_windows.get('cue'),
                                            self.table_windows.get('guide'),
                                            self.table_windows.get('charge')):
            frame = frame.copy()
            frame.putalpha(frame.getchannel('A').point([value // 2 for value in range(256)]))
        left, top, right, bottom = self.primary_bounds
        x0, y0 = max(left, x), max(top, y)
        x1, y1 = min(right, x + frame.width), min(bottom, y + frame.height)
        if x0 >= x1 or y0 >= y1:
            frame, x, y = self.empty_frame, left, top
        elif (x0, y0, x1, y1) != (x, y, x + frame.width, y + frame.height):
            frame = frame.crop((x0 - x, y0 - y, x1 - x, y1 - y))
            x, y = x0, y0
        if hwnd in self.surfaces and self.surfaces[hwnd].size != frame.size:
            self.surfaces.pop(hwnd).close()
        if hwnd not in self.surfaces:
            self.surfaces[hwnd] = LayerSurface(hwnd, frame.size, x, y)
        self.surfaces[hwnd].present(frame, x, y)

    def close(self):
        if not self.window_mode and self.was_visible and win32gui.IsWindow(self.listview):
            win32gui.ShowWindow(self.listview, win32con.SW_SHOWNA)
        self.clear_table()
        for surface in self.surfaces.values():
            surface.close()
        self.surfaces = {}
        for hwnd in self.windows:
            if win32gui.IsWindow(hwnd):
                win32gui.DestroyWindow(hwnd)
        self.windows = []
        if hasattr(self, 'frame_cache'):
            self.frame_cache.clear()
            self.frame_cache_bytes = 0
        if hasattr(self, 'ring_frames'):
            self.ring_frames.clear()
