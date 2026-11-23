"""Windows system-tray icon for Desktop Icon Billiards.

The icon lives on the Tk thread, like :class:`dib.hotkeys.Hotkeys`, so Tk's own
message loop dispatches its window messages. A right-click (or left-click) opens
a small menu: show the toolbar, restart the round, or quit and restore the
desktop. The icon is removed in :meth:`SystemTrayIcon.close` before the app
destroys its window, so Explorer never keeps a stale tray entry.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as W

import win32con
import win32gui

from . import diag
from .config import ICON_PATH, PRODUCT_NAME

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
TRAY_MESSAGE = win32con.WM_USER + 0x5A31
CMD_SHOW, CMD_RESTART, CMD_QUIT, CMD_MINIMIZE, CMD_PIN = 1, 2, 3, 4, 5
MENU_ITEMS = ((CMD_SHOW, 'Show toolbar'),
              (CMD_MINIMIZE, 'Hide game window'),
              (CMD_RESTART, 'Restart round'),
              (CMD_PIN, 'Toggle pin'),
              (CMD_QUIT, 'Quit and restore desktop'))
MENU_ACTIONS = {CMD_SHOW: 'show', CMD_RESTART: 'restart', CMD_QUIT: 'quit',
                CMD_MINIMIZE: 'minimize', CMD_PIN: 'pin'}
# Shell sends NIN_SELECT on a left click, NIN_KEYSELECT on keyboard activation.
NIN_SELECT, NIN_KEYSELECT = 0x400, 0x403
_CLICK_EVENTS = (NIN_SELECT, NIN_KEYSELECT, win32con.WM_LBUTTONUP,
                 win32con.WM_LBUTTONDBLCLK, win32con.WM_RBUTTONUP)


class NOTIFYICONDATAW(ctypes.Structure):
    """Vista+ layout. ``cbSize`` is always set from ``sizeof`` for this process."""

    _fields_ = [
        ('cbSize', W.DWORD),
        ('hWnd', W.HWND),
        ('uID', W.UINT),
        ('uFlags', W.UINT),
        ('uCallbackMessage', W.UINT),
        ('hIcon', W.HICON),
        ('szTip', W.WCHAR * 128),
        ('dwState', W.DWORD),
        ('dwStateMask', W.DWORD),
        ('szInfo', W.WCHAR * 256),
        ('uVersion', W.UINT),
        ('szInfoTitle', W.WCHAR * 64),
        ('dwInfoFlags', W.DWORD),
        ('guidItem', ctypes.c_byte * 16),
        ('hBalloonIcon', W.HICON),
    ]


_shell_notify = ctypes.windll.shell32.Shell_NotifyIconW
_shell_notify.argtypes = [W.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
_shell_notify.restype = W.BOOL


def build_icon_data(hwnd, hicon, tip):
    data = NOTIFYICONDATAW()
    data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    data.hWnd = hwnd
    data.uID = 1
    data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    data.uCallbackMessage = TRAY_MESSAGE
    data.hIcon = hicon
    data.szTip = tip[:127]
    return data


class SystemTrayIcon:
    """Own the hidden message window and the shell notification icon."""

    def __init__(self, actions, *, tip=PRODUCT_NAME, icon_path=ICON_PATH, notify=_shell_notify):
        self.actions = dict(actions)
        self.notify = notify
        self.class_name = f'DesktopIconBilliardsTray-{id(self)}'
        self.hwnd = None
        self.hicon = None
        self.installed = False
        self._data = None
        window_class = win32gui.WNDCLASS()
        window_class.hInstance = win32api_module_handle()
        window_class.lpszClassName = self.class_name
        window_class.lpfnWndProc = self._window_proc
        win32gui.RegisterClass(window_class)
        # HWND_MESSAGE keeps the tray icon off the taskbar and out of Z-order.
        self.hwnd = win32gui.CreateWindow(self.class_name, '', 0, 0, 0, 0, 0, -3, 0,
                                          window_class.hInstance, None)
        self.hicon = self._load_icon(icon_path)
        self._data = build_icon_data(self.hwnd, self.hicon, tip)
        self.installed = bool(self.notify(NIM_ADD, ctypes.pointer(self._data)))
        if not self.installed:
            diag.log('system tray icon could not be installed; hotkey and toolbar still work')
        else:
            diag.log('system tray icon installed')

    @staticmethod
    def _load_icon(path):
        try:
            return win32gui.LoadImage(0, str(path), win32con.IMAGE_ICON, 16, 16,
                                      win32con.LR_LOADFROMFILE)
        except (win32gui.error, OSError):
            return win32gui.LoadIcon(0, win32con.IDI_APPLICATION)

    def _window_proc(self, hwnd, message, wparam, lparam):
        if message == TRAY_MESSAGE and (lparam & 0xFFFF) in _CLICK_EVENTS:
            try:
                self._show_menu()
            except Exception:
                # Never let a tray pop-up take down the game loop.
                import traceback
                diag.log('tray menu failed:', traceback.format_exc())
            return 0
        return win32gui.DefWindowProc(hwnd, message, wparam, lparam)

    def _show_menu(self):
        menu = win32gui.CreatePopupMenu()
        try:
            for command, label in MENU_ITEMS:
                win32gui.AppendMenu(menu, win32con.MF_STRING, command, label)
            x, y = win32gui.GetCursorPos()
            try:
                win32gui.SetForegroundWindow(self.hwnd)
            except win32gui.error:
                pass
            command = win32gui.TrackPopupMenu(
                menu, win32con.TPM_RIGHTBUTTON | win32con.TPM_RETURNCMD,
                x, y, 0, self.hwnd, None)
        finally:
            win32gui.DestroyMenu(menu)
            win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)
        if command:
            self.dispatch(command)

    def dispatch(self, command):
        action = self.actions.get(MENU_ACTIONS.get(command))
        if action is not None:
            action()

    def refresh(self, tip=None, icon_path=None):
        """Update tooltip and/or icon after install (e.g. when the round ends)."""
        if not self.installed:
            return
        if tip is not None:
            self._data.szTip = tip[:127]
        if icon_path is not None:
            new_icon = self._load_icon(icon_path)
            if new_icon != self.hicon:
                self._data.hIcon = new_icon
                old, self.hicon = self.hicon, new_icon
                if old:
                    win32gui.DestroyIcon(old)
        self._data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        self.notify(NIM_MODIFY, ctypes.pointer(self._data))

    def close(self):
        if self.installed:
            self.notify(NIM_DELETE, ctypes.pointer(self._data))
            self.installed = False
        if self.hwnd is not None:
            if win32gui.IsWindow(self.hwnd):
                win32gui.DestroyWindow(self.hwnd)
            win32gui.UnregisterClass(self.class_name, win32api_module_handle())
            self.hwnd = None
        if self.hicon:
            win32gui.DestroyIcon(self.hicon)
            self.hicon = None


def win32api_module_handle():
    import win32api
    return win32api.GetModuleHandle(None)
