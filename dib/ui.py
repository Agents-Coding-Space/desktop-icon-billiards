"""Tk window, game lifecycle, and single-instance handling."""
from __future__ import annotations

import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import webbrowser

import tkinter as tk
import win32api
import win32con
import win32gui
from PIL import Image, ImageTk
from tkinter import messagebox, ttk

from . import config, diag
from .display import dpi_context
from .history import MatchHistory
from .hotkeys import Hotkeys
from .tray import SystemTrayIcon
from .instance import acquire_instance, claim_instance_mutex, publish_version
from .overlay import BilliardsOverlay
from .panel import FONT as PANEL_FONT
from .panel import MainPanel, ResultDialog, format_duration, show_update_dialog
from .paths import runtime_directory
from .leaderboard import LeaderboardDialog
from .rank import RankApi, RankController, RankSettings, average_seconds, data_directory
from .runner import BilliardsRunner
from .shell import LayoutSession, ShellDesktop
from .update import fetch_update, release_entries
from .white_ball import WhiteBallDialog, read_texture


RANK_CONSENT = (
    'When enabled, each cleared table submits to the ranking server:\n'
    '  • round time, icon count, shot count\n'
    '  • an anonymous device id (salted hash of the system id, not reversible)\n\n'
    'Icon names, desktop captures and personal file data are never uploaded. You can turn it off anytime in the main window.\n\n'
    'Enable Global Ranking?'
)
RANK_INTRO = 'Now you can race against global players to clear the table fastest.\n\n' + RANK_CONSENT


_last_log = {}


def _write_log(name, text):
    text = text.encode('utf-8')[:diag.MAX_BYTES].decode('utf-8', errors='ignore')
    # Error/callback traces must never be suppressed by the duplicate filter:
    # the same traceback repeated minutes later is still fresh evidence.
    if name in ('error.log', 'callback.log'):
        stamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
        try:
            with (runtime_directory() / name).open('a', encoding='utf-8') as stream:
                stream.write(f'\n--- {stamp} ---\n{text}\n')
        except OSError:
            pass
        return
    now = time.monotonic()
    previous = _last_log.get(name)
    if previous is not None and previous[0] == text and now - previous[1] < 60:
        return
    try:
        (runtime_directory() / name).write_text(text, encoding='utf-8')
        _last_log[name] = (text, now)
    except OSError:
        pass


def describe_error(error):
    """The error text plus where it was raised (``file.py:line in function``).

    A bare message such as "'NoneType' object is not iterable" says nothing on its own; the
    location lets a player's screenshot pin the bug down. The last frame inside this package wins.
    """
    frames = traceback.extract_tb(error.__traceback__)
    frame = next((f for f in reversed(frames) if 'dib' in f.filename.replace('\\', '/').split('/')), None)
    frame = frame or (frames[-1] if frames else None)
    where = f'{os.path.basename(frame.filename)}:{frame.lineno} in {frame.name}' if frame else ''
    text = f'{error}\n\nWhere: {where}' if where else str(error)
    return f'{text}\n\nDetails in %TEMP%\\DesktopIconBilliards\\error.log'


def bot_eye_parts(bot, finished=False):
    """Three spaced fields for the hidden-felt badge: mode, turn, score."""
    if bot is None or not bot.enabled:
        return ('', '', '')
    if finished:
        return ('RESULT', '', bot.result_text())
    return (bot.label().upper(), 'BOT' if bot.bot_turn else 'YOU',
            f'{bot.scores["human"]} : {bot.scores["bot"]}')


def bot_eye_text(bot, finished=False):
    """Compact text form (kept for logs/tests); the badge draws spaced fields."""
    mode, turn, score = bot_eye_parts(bot, finished)
    if not mode:
        return ''
    return f'{mode} · {turn} · {score}' if turn else f'{mode} · {score}'


class DesktopBilliardsApp:
    """Own the Tk window, the layout journal and the animation runner."""

    def __init__(self, root, instance_handle):
        self.root = root
        self.instance_handle = instance_handle
        self.runner = None
        self.session = None
        self.watchdog = None
        self.donation = None
        self.closing = False
        self.deadline = 0.0
        self.journal = runtime_directory() / 'layout.json'
        root.title(config.WINDOW_TITLE)
        root.resizable(False, False)
        try:
            root.iconbitmap(str(config.ICON_PATH))
        except tk.TclError:
            pass
        ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-2))
        self.rank = self.make_rank()
        self.rank_offered = False
        self.guide_enabled = True
        self.leaderboard_dialog = None
        self.result_dialog = None
        self.result_serial = None
        self.result_attempt_serial = None
        self.result_attempts = 0
        self.result_retry_at = 0.
        self.restart_after_serial = None
        self.round_snapshot = None
        self.result_observation = None
        self.update_results = queue.SimpleQueue()
        self.settings = self.make_settings()
        self.history = MatchHistory()
        self._history_serial = None
        self.white_ball_dialog = None
        self.white_texture = None
        self.white_texture_path = self.settings.path.parent / 'white-ball.png'
        if self.white_texture_path.exists():
            try:
                self.white_texture = read_texture(self.white_texture_path)
            except (OSError, ValueError, Image.DecompressionBombError):
                _write_log('white-ball.log', traceback.format_exc())
        self.felt_enabled = bool(self.settings.get('felt', True))
        self.window_mode = bool(self.settings.get('window_mode', False))
        self.game_window = None
        self.window_size = None
        self.window_pinned = False
        self.window_controls = ()
        self.stealth_announced = False
        self.toolbar_hidden = False
        self.toolbar_eye = None
        self.toolbar_region = None
        self.toolbar_hover_since = None
        self.toolbar_leave_since = None
        self.panel = panel = MainPanel(root, config.VERSION, {
            'restart': self.restart, 'toggle_guide': self.toggle_guide, 'toggle_rank': self.toggle_rank,
            'check_updates': lambda: self.check_updates(manual=True),
            'quit': self.close, 'toggle_felt': self.toggle_felt,
            'leaderboard': self.show_leaderboard,
            'white_ball': self.show_white_ball,
            'toggle_bot': self.toggle_bot,
            'toggle_window': self.toggle_window_mode,
            'toggle_pin': self.toggle_pin,
        })
        publish_version(root, config.VERSION)
        self.time_title, self.time_value = panel.time_title, panel.time_value
        self.count_title, self.count_value = panel.count_title, panel.count_value
        self.remaining_value = panel.remaining_value
        self.refresh_buttons()
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.bind('<Escape>', lambda event: self.close())
        self.place_window()
        self.hotkeys = Hotkeys()
        self.tray = None
        try:
            self.tray = SystemTrayIcon({'show': self.show_toolbar,
                                        'restart': self.restart,
                                        'quit': self.close,
                                        'minimize': self.hide_game_window,
                                        'pin': self.toggle_pin})
        except Exception:
            _write_log('tray.log', traceback.format_exc())
        root.after(20, self.start)
        root.after(1500, self.check_updates)

    @staticmethod
    def make_rank():
        """Ranking is optional: a missing or invalid server address just disables it."""
        if config.RANK_URL:
            try:
                api = RankApi(config.RANK_URL, config.VERSION)
                return RankController(api, RankSettings(data_directory() / 'rank.json'))
            except (ValueError, OSError):
                _write_log('rank.log', traceback.format_exc())
        return RankController(None, None)

    @staticmethod
    def make_settings():
        """Window layout and position, and the table-frame switch (kept across runs)."""
        try:
            return RankSettings(data_directory() / 'ui.json')
        except OSError:
            return RankSettings(runtime_directory() / 'ui.json')

    def refresh_rank_button(self):
        if not self.rank.available:
            self.panel.set_look('rank', 'Rank —', 'off', enabled=False)
            return
        on = self.rank.enabled
        self.panel.set_look('rank', 'Rank On' if on else 'Rank Off', 'on' if on else 'off', enabled=True)

    def refresh_buttons(self):
        self.refresh_rank_button()
        self.panel.set_look('guide', 'Guide On' if self.guide_enabled else 'Guide Off',
                            'on' if self.guide_enabled else 'off')
        self.panel.set_look('felt', 'Felt On' if self.felt_enabled else 'Felt Off')
        self.panel.set_look('window', 'Window' if self.window_mode else 'Desktop',
                            'on' if self.window_mode else 'ghost')
        self.panel.set_look('pin', 'Pinned' if self.window_pinned else 'Pin',
                            'on' if self.window_pinned else 'ghost')
        self.refresh_bot_button()
        self.refresh_window_score()

    def toggle_bot(self):
        overlay = self.runner.overlay if getattr(self, 'runner', None) is not None else None
        if overlay is None:
            return
        mode = overlay.bot.cycle()
        # Fresh slate so scores never leak across difficulty switches mid-round.
        if mode != 'off':
            overlay.bot.scores = {'human': 0, 'bot': 0}
        self.refresh_bot_button()

    def refresh_bot_button(self):
        panel = getattr(self, 'panel', None)
        if panel is None:
            return
        overlay = self.runner.overlay if getattr(self, 'runner', None) is not None else None
        if overlay is None:
            panel.set_look('bot', 'Bot Off', 'ghost')
            panel.remaining_title.set('Left')
            return
        bot = getattr(overlay, 'bot', None)
        if bot is None:
            panel.set_look('bot', 'Bot Off', 'ghost')
            panel.remaining_title.set('Left')
            return
        text = bot.label() if bot.enabled else 'Bot Off'
        if self.table_finished():
            text = bot.result_text()
        panel.set_look('bot', text, 'on' if bot.enabled and not self.table_finished() else 'ghost')
        panel.remaining_title.set(bot.score_text() if bot.enabled else 'Left')
        self.paint_toolbar_eye()

    def table_finished(self):
        return bool(self.round_snapshot is not None and self.round_snapshot.state == 'finished')

    # -- toolbar position --------------------------------------------------
    def place_window(self):
        root = self.root
        root.update_idletasks()
        width, height = root.winfo_reqwidth(), root.winfo_reqheight()
        monitor = win32api.MonitorFromPoint((0, 0), win32con.MONITOR_DEFAULTTOPRIMARY)
        left, top, right, bottom = win32api.GetMonitorInfo(monitor)['Work']
        x, y = self.settings.get('bar_x'), self.settings.get('bar_y')
        if not (isinstance(x, int) and isinstance(y, int)):
            x, y = left + max(0, (right - left - width) // 2), top + 2
        else:
            monitor = win32api.MonitorFromPoint((x, y), win32con.MONITOR_DEFAULTTONEAREST)
            left, top, right, bottom = win32api.GetMonitorInfo(monitor)['Work']
        x = max(left, min(x, right - width))
        y = max(top, min(y, bottom - height))
        root.geometry(f'+{x}+{y}')

    def remember_position(self):
        if self.root.state() == 'normal':
            self.settings.update(bar_x=self.root.winfo_x(), bar_y=self.root.winfo_y())

    def offer_rank(self):
        """Ask once, on the first run after the feature exists, whether to join the ranking."""
        if self.closing or not self.rank.needs_prompt:
            return
        self.rank.mark_prompted()
        if messagebox.askyesno('Global Ranking', RANK_INTRO, parent=self.root):
            self.rank.set_enabled(True)
        self.refresh_rank_button()

    def toggle_rank(self):
        if not self.rank.available:
            return
        if self.rank.enabled:
            self.rank.set_enabled(False)
        elif messagebox.askokcancel('Global Ranking', RANK_CONSENT, parent=self.root):
            self.rank.set_enabled(True)
        self.refresh_rank_button()

    def show_donation(self):
        if self.donation is not None and self.donation.winfo_exists():
            self.donation.lift()
            return
        try:
            with Image.open(config.ASSETS / 'donation.jpg') as source:
                image = source.convert('RGB')
            image.thumbnail((340, 340), Image.Resampling.LANCZOS)
        except OSError:
            messagebox.showwarning('Donate', 'Unable to open the donation image right now.', parent=self.root)
            return
        self.donation = dialog = tk.Toplevel(self.root)
        dialog.title('Donate')
        dialog.transient(self.root)
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=14, style='Panel.TFrame')
        body.pack()
        ttk.Label(body, text='Scan the WeChat code to support — thank you!', style='Table.TLabel').pack(pady=(0, 10))
        photo = ImageTk.PhotoImage(image, master=dialog)
        label = ttk.Label(body, image=photo)
        label.image = photo
        label.pack()
        ttk.Label(body, text='Donations are voluntary support for this project and include no extra features or dedicated support.',
                  style='Table.TLabel', wraplength=340, justify='center').pack(pady=(10, 0))
        ttk.Button(body, text='Close', command=dialog.destroy, style='Table.TButton').pack(pady=(10, 0))
        dialog.bind('<Escape>', lambda event: dialog.destroy())

    def show_white_ball(self):
        if self.white_ball_dialog is not None and self.white_ball_dialog.window.winfo_exists():
            self.white_ball_dialog.window.lift()
            return
        self.white_ball_dialog = WhiteBallDialog(self.root, self.white_texture_path,
                                                self.white_texture, self.set_white_texture)

    def set_white_texture(self, texture):
        self.white_texture = texture
        if self.runner is not None:
            self.runner.overlay.request_white_texture(texture)

    def check_updates(self, manual=False):
        if self.closing or not self.panel.is_enabled('update'):
            return
        self.panel.set_look('update', 'Checking…', enabled=False)

        def fetch():
            try:
                info = fetch_update(config.UPDATE_URL, config.VERSION, source='manual' if manual else 'startup')
            except Exception:
                _write_log('update.log', traceback.format_exc())
                self.update_results.put((False, None))
            else:
                self.update_results.put((True, info))

        threading.Thread(target=fetch, name='Billiards-update', daemon=True).start()
        self.root.after(100, self.finish_update_check, manual)

    def finish_update_check(self, manual):
        if self.closing:
            return
        try:
            success, info = self.update_results.get_nowait()
        except queue.Empty:
            self.root.after(100, self.finish_update_check, manual)
            return
        self.panel.set_look('update', 'Check Updates', enabled=True)
        if info:
            self.show_update(info)
        elif manual:
            messagebox.showinfo(config.PRODUCT_NAME, 'Already up to date' if success else 'Check failed, please try again later',
                                parent=self.root)

    def show_update(self, info):
        def download(dialog):
            if webbrowser.open(info['download_url']):
                dialog.destroy()
            else:
                messagebox.showerror('Could not open download', 'Please check your default browser settings.', parent=dialog)

        show_update_dialog(self.root, config.VERSION, info, release_entries(info), download)

    # -- in-window score badge --------------------------------------------
    def refresh_window_score(self):
        """Reuse the hidden-felt badge inside the transparent game window."""
        labels = getattr(self, 'window_score_labels', None)
        if not labels or not any(label.winfo_exists() for label in labels):
            return
        overlay = getattr(getattr(self, 'runner', None), 'overlay', None)
        parts = bot_eye_parts(getattr(overlay, 'bot', None), self.table_finished())
        if parts == getattr(self, '_window_score_parts', None):
            return
        self._window_score_parts = parts
        for label, value in zip(labels, parts):
            if label.winfo_exists():
                label.configure(text=value)

    # -- game lifecycle ----------------------------------------------------
    # -- resizable window mode ---------------------------------------------
    def create_game_window(self):
        """Borderless transparent window with tiny controls, drag and resize."""
        if self.game_window is not None:
            return self.game_window
        top = tk.Toplevel(self.root)
        top.withdraw()
        top.overrideredirect(True)
        top.configure(bg='#010203')
        top.attributes('-transparentcolor', '#010203')
        self.window_pinned = bool(self.settings.get('window_pinned', False))
        top.attributes('-topmost', self.window_pinned)
        x, y = self.settings.get('win_x'), self.settings.get('win_y')
        width = self.settings.get('win_w') or 1000
        height = self.settings.get('win_h') or 560
        top.geometry(f'{max(320, width)}x{max(160, height)}+{x or 120}+{y or 100}')
        handle = tk.Label(top, text='⋮⋮', font=('Segoe UI', 11, 'bold'),
                          fg='#e1e9eb', bg='#1b2f2a', cursor='fleur')
        handle.place(relx=0, rely=0, anchor='nw')
        handle.bind('<ButtonPress-1>', self._window_drag_start)
        handle.bind('<B1-Motion>', self._window_drag_move)
        score_badge = tk.Frame(top, bg='#f4f7f6', highlightbackground='#9fb2ad',
                               highlightthickness=1)
        score_badge.place(relx=0.5, rely=0, anchor='n', y=2)
        self.window_score_labels = []
        for width, anchor in ((9, 'w'), (7, 'center'), (13, 'e')):
            label = tk.Label(score_badge, text='', font=('Segoe UI', 10, 'bold'),
                             fg='#101820', bg='#f4f7f6', width=width, anchor=anchor)
            label.pack(side='left', padx=3)
            self.window_score_labels.append(label)
        self._window_score_parts = None
        controls = tk.Frame(top, bg='#010203')
        controls.place(relx=1, rely=0, anchor='ne')
        for text, command in (('–', top.withdraw), ('P', self.toggle_pin), ('×', self.close)):
            button = tk.Label(controls, text=text, font=('Segoe UI', 10, 'bold'),
                              fg='#e1e9eb', bg='#1b2f2a', width=2, cursor='hand2')
            button.pack(side='left', padx=(0, 2))
            button.bind('<ButtonRelease-1>', lambda event, cmd=command: cmd())
        grip = tk.Frame(top, width=18, height=18, bg='#8fa9a2', cursor='bottom_right_corner')
        grip.place(relx=1, rely=1, anchor='se')
        grip.bind('<ButtonPress-1>', self._window_resize_start)
        grip.bind('<B1-Motion>', self._window_resize_move)
        top.bind('<Configure>', self._window_configured)
        self.window_controls = (handle, score_badge, controls, grip)
        self.game_window = top
        top.deiconify()
        top.update_idletasks()  # apply geometry before the overlay measures the client area
        return top

    def raise_window_controls(self):
        flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE
        for widget in getattr(self, 'window_controls', ()):
            if not widget.winfo_exists():
                continue
            hwnd = self._widget_hwnd(widget)
            if hwnd and win32gui.IsWindow(hwnd):
                try:
                    win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 0, 0, 0, 0, flags)
                except win32gui.error:
                    pass

    def hide_game_window(self):
        if self.game_window is not None:
            self.game_window.withdraw()

    def toggle_pin(self):
        if self.game_window is None:
            return
        self.window_pinned = not self.window_pinned
        self.game_window.attributes('-topmost', self.window_pinned)
        self.settings.update(window_pinned=self.window_pinned)
        self.refresh_buttons()

    def destroy_game_window(self):
        self.window_score_labels = ()
        self._window_score_parts = None
        if self.game_window is not None:
            self.game_window.destroy()
            self.game_window = None

    @staticmethod
    def _widget_hwnd(widget):
        try:
            return int(widget.winfo_id())
        except (ValueError, TypeError, tk.TclError):
            return 0

    def _window_drag_start(self, event):
        self._window_drag_offset = (event.x_root - self.game_window.winfo_x(),
                                    event.y_root - self.game_window.winfo_y())

    def _window_drag_move(self, event):
        offset = getattr(self, '_window_drag_offset', None)
        if offset is None:
            return
        self.game_window.geometry(f'+{event.x_root - offset[0]}+{event.y_root - offset[1]}')

    @staticmethod
    def locked_window_size(width, height, dx=0, dy=0):
        """Keep the borderless game window at a hard 2:1 ratio."""
        width, height = max(320, int(width)), max(160, int(height))
        # Follow whichever axis the player dragged more.
        if abs(dx) >= abs(dy) * 2:
            return width // 2 * 2, (width // 2 * 2) // 2
        if abs(dy) >= abs(dx) * 2:
            width = height * 2
            return width // 2 * 2, (width // 2 * 2) // 2
        return width // 2 * 2, (width // 2 * 2) // 2

    def _window_resize_start(self, event):
        self._window_resize_origin = (event.x_root, event.y_root,
                                      self.game_window.winfo_width(), self.game_window.winfo_height())

    def _window_resize_move(self, event):
        origin = getattr(self, '_window_resize_origin', None)
        if origin is None:
            return
        x, y, width, height = origin
        dx, dy = event.x_root - x, event.y_root - y
        width, height = self.locked_window_size(width + dx, height + dy, dx, dy)
        self.game_window.geometry(f'{width}x{height}')

    def _window_configured(self, event):
        if event.widget is not self.game_window:
            return
        size = (event.width, event.height)
        if size == self.window_size:
            return
        self.window_size = size
        self.settings.update(win_x=self.game_window.winfo_x(), win_y=self.game_window.winfo_y(),
                             win_w=size[0], win_h=size[1])
        overlay = getattr(getattr(self, 'runner', None), 'overlay', None)
        if overlay is not None:
            overlay.request_resize(*size)

    def remember_window_position(self):
        window = getattr(self, 'game_window', None)
        if window is not None and window.winfo_exists():
            self.settings.update(win_x=window.winfo_x(), win_y=window.winfo_y(),
                                 win_w=window.winfo_width(), win_h=window.winfo_height())

    def toggle_window_mode(self):
        self.remember_window_position()
        if self.runner is not None:
            self.restore()
        self.destroy_game_window()
        self.window_mode = not self.window_mode
        self.settings.update(window_mode=self.window_mode)
        self.refresh_buttons()
        if self.window_mode:
            self.create_game_window()
        self.start()

    def start(self):
        if self.closing:
            return
        diag.start_session(config.VERSION)
        try:
            desktop = ShellDesktop()
            if self.window_mode:
                # Real desktop icons stay visible: this is a normal game window.
                self.session = None
                items = desktop.items()
                for item in items:
                    item['name'] = desktop.display_name(item['id'])
                window = self.create_game_window()
                parent = self._widget_hwnd(window)
                with dpi_context(-4):
                    overlay = BilliardsOverlay(desktop, items, window_parent=parent,
                                               titlebar_height=30)
                    overlay.guide_enabled = self.guide_enabled
                    overlay.want_felt = self.felt_enabled
                    overlay.request_white_texture(self.white_texture)
            else:
                # Recover a journal left behind by an earlier crash before starting.
                self.session = LayoutSession(desktop, self.journal)
                self.session.restore()
                self.session = LayoutSession(desktop, self.journal)
                items = self.session.begin(overlay_only=True)
                for item in items:
                    item['name'] = desktop.display_name(item['id'])
                with dpi_context(-4):
                    overlay = BilliardsOverlay(desktop, items)
                    overlay.guide_enabled = self.guide_enabled
                    overlay.want_felt = self.felt_enabled
                    overlay.request_white_texture(self.white_texture)
                self.watchdog = self.spawn_watchdog()
            self.runner = BilliardsRunner(overlay)
            self.runner.start()
            self.deadline = time.monotonic() + 30
            self.poll()
        except Exception as exc:
            self.fail(exc)

    def spawn_watchdog(self):
        frozen = bool(getattr(sys, 'frozen', False))
        command = [sys.executable]
        if not frozen:
            command.append(str(config.BASE / 'app.pyw'))
        command.extend(['--watchdog', str(self.journal), self.session.snapshot['session_id'], str(os.getpid())])
        environment = {**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT': '1'}
        return subprocess.Popen(command, env=environment,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                                cwd=str(runtime_directory()))

    def poll(self):
        if self.closing:
            return
        try:
            while True:
                try:
                    identifier = self.hotkeys.pending.get_nowait()
                except queue.Empty:
                    break
                if identifier == config.HOTKEY_ID:
                    self.close()
                    return
            if self.runner is None:
                return
            if self.runner.error:
                raise self.runner.error
            if not self.runner.shown.is_set():
                if self.window_mode:
                    if self.runner.prepared.is_set():
                        self.runner.request_show(self.runner.overlay.table.snapshot(), self.journal)
                else:
                    if time.monotonic() > self.deadline:
                        raise RuntimeError('Timed out waiting for the desktop restore watchdog.')
                    ready = self.journal.with_name(self.journal.stem + '.watchdog.json')
                    if ready.exists():
                        data = json.loads(ready.read_text(encoding='utf-8'))
                        if (data.get('session_id') == self.session.snapshot['session_id']
                                and self.runner.prepared.is_set()):
                            if not data.get('ready'):
                                raise RuntimeError('Unable to start the desktop restore watchdog.')
                            self.runner.request_show(self.session.snapshot, self.journal)
            else:
                if self.window_mode:
                    self.raise_window_controls()
                    self.refresh_window_score()
                    current_overlay = self.runner.overlay
                    position = getattr(current_overlay, 'window_position', None)
                    if position is not None:
                        current_overlay.window_position = None
                        self.settings.update(win_x=position[0], win_y=position[1])
                if not self.rank_offered:
                    self.rank_offered = True
                    self.root.after(800, self.offer_rank)
                if not self.runner.overlay.input_suspended:
                    self.update_toolbar()
                overlay = self.runner.overlay
                try:
                    while True:
                        url = overlay.link_requests.get_nowait()
                        webbrowser.open(url)
                except queue.Empty:
                    pass
                except Exception:
                    _write_log('links.log', traceback.format_exc())
                snapshots = self.runner.take_snapshots()
                if snapshots:
                    self.observe_snapshots(overlay, snapshots)
                    if self.handle_round_actions(overlay, snapshots[-1]):
                        return
            self.root.after(50, self.poll)
        except Exception as exc:
            self.fail(exc)

    def handle_round_actions(self, overlay, snapshot):
        while True:
            try:
                serial, action = overlay.round_actions.get_nowait()
            except queue.Empty:
                return False
            if snapshot.state != 'finished' or serial != snapshot.round_serial:
                continue
            if self.restart_after_serial is not None and serial <= self.restart_after_serial:
                continue
            if action == 'again':
                self.play_again()
                return False
            if action == 'quit':
                self.close()
                return self.closing

    def observe_snapshots(self, overlay, snapshots):
        """Use one batch for ranking and its latest immutable state for the UI."""
        table = snapshots[-1]
        self.round_snapshot = table
        # Enabling ranking mid-round must not replay pre-consent start events.
        self.refresh_bot_button()
        ranked = snapshots if self.rank.serial is not None else [table]
        rank_failed = False
        for snapshot in ranked:
            rank_failed = False
            try:
                self.rank.observe(snapshot)
            except Exception:
                rank_failed = True
                _write_log('rank.log', traceback.format_exc())
        minutes, seconds = divmod(table.round_seconds, 60)
        self.time_value.set(f'{minutes:02d}:{seconds:02d}')
        total, pocketed = table.icon_count, table.pocketed_icons
        finished = table.state == 'finished'
        observation = (table.round_serial, finished)
        if observation != self.result_observation:
            self.result_observation = observation
            diag.log(f'round observed: serial={table.round_serial} finished={finished} '
                     f'pocketed={pocketed}/{total} toolbar_hidden={self.toolbar_hidden}')
        self.time_title.set('Total Time' if finished else 'Round Time')
        self.count_title.set('Total Balls' if finished else 'Icons Sunk')
        self.count_value.set(str(total) if finished else f'{pocketed}/{total}')
        self.remaining_value.set(str(total - pocketed))
        try:
            self.update_result_dialog(overlay, table, rank_failed=rank_failed)
        except Exception:
            _write_log('result.log', traceback.format_exc())

    def restart(self):
        if self.runner and self.runner.shown.is_set():
            if self.round_snapshot is not None:
                self.restart_after_serial = self.round_snapshot.round_serial
            self.runner.overlay.request_restart()

    def toggle_guide(self):
        self.guide_enabled = not self.guide_enabled
        self.refresh_buttons()
        if self.runner:
            self.runner.overlay.guide_enabled = self.guide_enabled

    def toggle_felt(self):
        """Show or hide the table frame and wallpaper panel, keeping the wastebaskets."""
        self.felt_enabled = not self.felt_enabled
        self.stealth_announced = False
        self.settings.update(felt=self.felt_enabled)
        self.refresh_buttons()
        if self.runner:
            self.runner.overlay.want_felt = self.felt_enabled

    def reveal_toolbar(self, event=None):
        button = win32con.VK_RBUTTON if win32api.GetSystemMetrics(23) else win32con.VK_LBUTTON
        if not win32api.GetAsyncKeyState(button) & 0x8000:
            self.show_toolbar()

    def active_bot(self):
        overlay = getattr(self, 'runner', None)
        overlay = getattr(overlay, 'overlay', None) if overlay is not None else None
        return getattr(overlay, 'bot', None)

    @staticmethod
    def badge_geometry(width, height):
        """Pin the hidden-mode badge to the top-right of the primary work area."""
        monitor = win32api.MonitorFromPoint((0, 0), win32con.MONITOR_DEFAULTTOPRIMARY)
        left, top, right, bottom = win32api.GetMonitorInfo(monitor)['Work']
        return f'{width}x{height}+{max(left, right - width - 8)}+{top + 8}'

    def show_toolbar_eye(self):
        if self.toolbar_eye is None:
            eye = self.toolbar_eye = tk.Toplevel(self.root)
            eye.withdraw()
            eye.overrideredirect(True)
            eye.attributes('-topmost', True)
            eye.attributes('-transparentcolor', '#010203')
            eye.attributes('-alpha', .92)
            wide = bool(bot_eye_text(self.active_bot(), self.table_finished()))
            width, height = (260, 36) if wide else (36, 28)
            canvas = tk.Canvas(eye, width=width, height=height, bg='#010203',
                               highlightthickness=0, bd=0)
            canvas.pack()
            if wide:
                # Light pill + black Segoe UI text: readable on any wallpaper, no
                # overlap with a drawn eye (the whole badge restores the toolbar).
                canvas.create_rectangle(1, 1, width - 2, height - 2, fill='#f4f7f6',
                                        outline='#9fb2ad', width=1)
                positions = ((10, 5), (104, 5), (176, 5))
                for index, (x, y) in enumerate(positions):
                    canvas.create_text(x, y, text='', tags=('score', f'part{index}'),
                                       anchor='nw', font=('Segoe UI', 11, 'bold'), fill='#101820')
            else:
                shape = (5, 14, 11, 7, 18, 5, 25, 7, 31, 14, 25, 21, 18, 23, 11, 21)
                canvas.create_polygon(*shape, smooth=True, fill='#263d45', outline='#e1e9eb', width=2)
                canvas.create_oval(14, 10, 22, 18, fill='#e1e9eb', outline='')
            canvas.bind('<Enter>', self.reveal_toolbar)
            canvas.bind('<ButtonRelease-1>', self.reveal_toolbar)
            self.toolbar_eye_canvas = canvas
        # Rebuild the badge when the bot is toggled while the felt is hidden.
        wide_now = bool(bot_eye_text(self.active_bot(), self.table_finished()))
        if bool(getattr(self, '_eye_wide', False)) != wide_now:
            self.toolbar_eye.destroy()
            self.toolbar_eye = None
            self.toolbar_eye_canvas = None
            self._eye_parts = None
            self._eye_wide = wide_now
            return self.show_toolbar_eye()
        self._eye_wide = wide_now
        self.paint_toolbar_eye()
        width, height = (260, 36) if wide_now else (36, 28)
        self.toolbar_eye.geometry(self.badge_geometry(width, height))
        self.toolbar_eye.deiconify()

    def paint_toolbar_eye(self):
        canvas = getattr(self, 'toolbar_eye_canvas', None)
        if canvas is None:
            return
        parts = bot_eye_parts(self.active_bot(), self.table_finished())
        if parts == getattr(self, '_eye_parts', None):
            return  # no Tk redraw for identical text: keeps the badge steady
        self._eye_parts = parts
        if not parts[0]:
            for item in canvas.find_withtag('score'):
                canvas.delete(item)
            return
        for index, value in enumerate(parts):
            items = canvas.find_withtag(f'part{index}')
            if items:
                canvas.itemconfigure(items[0], text=value)
            else:
                canvas.create_text(10 + index * (72 if index < 2 else 70), 5, text=value,
                                   tags=('score', f'part{index}'), anchor='nw',
                                   font=('Segoe UI', 11, 'bold'), fill='#101820')

    def show_toolbar(self):
        panel = getattr(self, 'panel', None)
        if panel is not None:
            panel.restore()
        if self.toolbar_eye is not None:
            self.toolbar_eye.withdraw()
        if self.toolbar_hidden:
            self.root.deiconify()
            self.toolbar_hidden = False
        self.toolbar_hover_since = self.toolbar_leave_since = None

    def update_toolbar(self):
        """Keep the original toolbar area discoverable without hiding game windows."""
        if self.felt_enabled:
            self.show_toolbar()
            return
        if not self.stealth_announced:
            self.stealth_announced = True
            self.show_toolbar()
            messagebox.showinfo('Hidden Felt Mode',
                                'Hold Shift: show cue and guide line\n'
                                'Hover the eye: restore the toolbar\n'
                                'Ctrl+Alt+X: quit the game',  parent=self.root)
            self.toolbar_leave_since = time.monotonic()
        # Keep dialogs and toolbar button interactions accessible.
        if self.root.grab_current() is not None or any(
                isinstance(child, tk.Toplevel) and child is not self.toolbar_eye and child.winfo_viewable()
                for child in self.root.winfo_children()):
            self.show_toolbar()
            return
        now = time.monotonic()
        if not self.toolbar_hidden:
            x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
            self.toolbar_region = (x, y, x + self.root.winfo_width(), y + self.root.winfo_height())
        if self.toolbar_region is None:
            return
        x, y = self.root.winfo_pointerxy()
        left, top, right, bottom = self.toolbar_region
        inside = left <= x < right and top <= y < bottom
        button = win32con.VK_RBUTTON if win32api.GetSystemMetrics(23) else win32con.VK_LBUTTON
        if win32api.GetAsyncKeyState(button) & 0x8000:
            self.toolbar_hover_since = self.toolbar_leave_since = None
            return
        if inside:
            self.toolbar_leave_since = None
            if self.toolbar_hidden:
                if self.toolbar_hover_since is None:
                    self.toolbar_hover_since = now
                elif now - self.toolbar_hover_since >= .3:
                    self.show_toolbar()
        else:
            self.toolbar_hover_since = None
            if not self.toolbar_hidden:
                if self.toolbar_leave_since is None:
                    self.toolbar_leave_since = now
                elif now - self.toolbar_leave_since >= 1:
                    self.remember_position()
                    self.root.withdraw()
                    self.toolbar_hidden = True
                    self.show_toolbar_eye()

    # -- round result ------------------------------------------------------
    def update_result_dialog(self, overlay, table, *, rank_failed=False):
        """Acknowledge a round only after its result window renders successfully."""
        serial = table.round_serial
        finished = table.state == 'finished'
        restarting = self.restart_after_serial is not None and serial <= self.restart_after_serial
        if self.restart_after_serial is not None and serial > self.restart_after_serial:
            self.restart_after_serial = None
        dialog = self.result_dialog
        if dialog is not None:
            if not dialog.alive():
                self.result_dialog = None  # User dismissal stays acknowledged for this round.
            elif restarting or not finished or serial != self.result_serial:
                diag.log(f'result closing for round change: serial={serial} result_serial={self.result_serial}')
                dialog.close()
                self.result_dialog = None
        if restarting or not finished:
            return
        if self.result_attempt_serial != serial:
            self.result_attempt_serial = serial
            self.result_attempts = 0
            self.result_retry_at = 0.
        creating = self.result_serial != serial
        if not creating and self.result_dialog is None:
            return  # An explicitly closed window must not reopen on the next poll.
        now = time.monotonic()
        if self.result_attempts >= 2 or now < self.result_retry_at:
            return
        try:
            rank_view = ({'phase': 'error', 'message': 'Ranking temporarily unavailable; your round result is still shown.'}
                         if rank_failed else self.rank.view(table))
        except Exception:
            _write_log('rank.log', traceback.format_exc())
            rank_view = {'phase': 'error', 'message': 'Ranking temporarily unavailable; your round result is still shown.'}
        bot = getattr(overlay, 'bot', None)
        history = getattr(self, 'history', None)
        if history is not None and bot is not None and bot.enabled and serial != self._history_serial:
            self._history_serial = serial
            history.record(bot.scores['human'], bot.scores['bot'], bot.mode)
        view = {
            'time': format_duration(table.play_ms),
            'average': f'{average_seconds(table.play_ms, table.icon_count):.2f}',
            'balls': str(table.icon_count), 'shots': str(table.shots),
            'note': overlay.ball_info, 'rank': rank_view,
            'winner': bot.result_text() if bot is not None and bot.enabled else '',
            'history': history.text() if history is not None and bot is not None and bot.enabled else '',
        }
        dialog = self.result_dialog
        try:
            if creating:
                diag.log(f'result opening: serial={serial} root_state={self.root.state()}')
                dialog = ResultDialog(self.root, {'again': self.play_again,
                                                 'enable_rank': self.enable_rank_from_dialog,
                                                 'leaderboard': self.show_leaderboard,
                                                 },
                                      '', '')
            dialog.update(view)
        except Exception:
            self.result_attempts += 1
            self.result_retry_at = now + 1.
            if creating and dialog is not None:
                dialog.close()
            raise
        else:
            if creating:
                self.result_dialog = dialog
                self.result_serial = serial
                diag.log(f'result created and rendered: serial={serial}')
            self.result_attempts = 0

    @staticmethod
    def open_credit():
        webbrowser.open(config.CREDIT_URL)

    def play_again(self):
        if self.result_dialog is not None:
            self.result_dialog.close()
            self.result_dialog = None
        self.restart()

    def show_leaderboard(self):
        if self.leaderboard_dialog is not None and self.leaderboard_dialog.alive():
            self.leaderboard_dialog.top.lift()
            self.leaderboard_dialog.top.focus_set()
            return
        self.leaderboard_dialog = LeaderboardDialog(self.root, self.rank.fetch_rankings)

    def enable_rank_from_dialog(self):
        if self.rank.available and not self.rank.enabled:
            self.toggle_rank()

    def fail(self, error):
        self.show_toolbar()
        try:
            self.restore()
        except Exception as exc:
            messagebox.showerror(config.PRODUCT_NAME, f'{describe_error(error)}\n\n{exc}', parent=self.root)
            return
        _write_log('error.log', traceback.format_exc())
        messagebox.showerror(config.PRODUCT_NAME, describe_error(error), parent=self.root)
        self.close()

    def restore(self):
        if self.runner:
            self.runner.stop()
            self.runner = None
        if self.session:
            self.session.restore()
            self.session = None
        if self.window_mode:
            self.destroy_game_window()

    def close(self):
        if self.closing:
            return
        try:
            self.restore()
        except Exception as exc:
            messagebox.showwarning(config.PRODUCT_NAME, f'Desktop restore did not finish: {exc}\n\nPlease retry exiting.',
                                   parent=self.root)
            return
        self.closing = True
        self.rank.close()
        self.remember_position()
        if self.tray is not None:
            self.tray.close()
            self.tray = None
        self.hotkeys.close()
        self.destroy_game_window()
        ctypes.windll.kernel32.CloseHandle(self.instance_handle)
        self.root.destroy()


def main():
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    app = None
    desktop_guard = None
    try:
        handle = acquire_instance(config.VERSION)
        desktop_guard = claim_instance_mutex(config.DESKTOP_GUARD)
        if desktop_guard is None:
            ctypes.windll.kernel32.CloseHandle(handle)
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo(config.PRODUCT_NAME,
                                '“Desktop Bounce” is running.\nPlease exit it first, then open Desktop Icon Billiards.',
                                parent=root)
            root.destroy()
            return
        root = tk.Tk()
        def callback_error(kind, error, trace):
            text = ''.join(traceback.format_exception(kind, error, trace))
            _write_log('callback.log', text)
            diag.log('Tk callback failed:', text)
        root.report_callback_exception = callback_error
        app = DesktopBilliardsApp(root, handle)
        root.mainloop()
    except SystemExit:
        raise
    except Exception as error:
        _write_log('error.log', traceback.format_exc())
        if app is not None:
            try:
                app.close()
            except Exception:
                pass
        else:
            try:
                messagebox.showerror(config.PRODUCT_NAME, f'Startup failed: {describe_error(error)}')
            except Exception:
                pass
        raise
    finally:
        if desktop_guard:
            ctypes.windll.kernel32.CloseHandle(desktop_guard)
