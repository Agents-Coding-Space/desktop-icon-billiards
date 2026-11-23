"""Select real desktop lists and retain the cause of process access failures."""
from contextlib import ExitStack
from types import SimpleNamespace
import ctypes
import unittest
import win32con
from unittest.mock import Mock, patch

from dib.overlay import desktop_listview, item_rects
from dib.shell import ShellDesktop
from dib.ui import (DESKTOP_ITEMS_POLL_ATTEMPTS, DesktopBilliardsApp,
                    ensure_desktop_icons_visible)


class DesktopWindowTests(unittest.TestCase):
    def test_folder_browser_tiny_views_and_other_processes_cannot_win(self):
        classes = {100: 'Progman', 200: '_cls_shellbrowser_', 300: 'WorkerW', 400: 'WorkerW',
                   101: 'SHELLDLL_DefView', 201: 'SHELLDLL_DefView',
                   301: 'SHELLDLL_DefView', 401: 'SHELLDLL_DefView'}
        lists = {101: [110, 111], 201: [210], 301: [310], 401: [410]}
        counts = {110: 0, 111: 85, 210: 99, 310: 200, 410: 150}
        user32 = SimpleNamespace(GetShellWindow=Mock(return_value=100))

        def find(parent, previous, *_):
            children = lists[parent]
            index = children.index(previous) + 1 if previous else 0
            return children[index] if index < len(children) else 0

        with ExitStack() as stack:
            patches = {
                'ctypes.windll.user32': user32,
                'dib.overlay.monitors': Mock(return_value=(('primary', (0, 0, 2160, 1440),
                                                           (0, 0, 2160, 1368), 144, True),)),
                'dib.overlay.dpi_context': Mock(return_value=ExitStack()),
                'dib.overlay.win32gui.EnumWindows': lambda fn, arg: [fn(h, arg) for h in (200, 300, 400, 100)],
                'dib.overlay.win32gui.EnumChildWindows': lambda h, fn, arg: fn(h + 1, arg),
                'dib.overlay.win32gui.GetClassName': lambda h: classes[h],
                'dib.overlay.win32process.GetWindowThreadProcessId': lambda h: (1, 99 if h == 300 else 10),
                'dib.overlay.win32gui.FindWindowEx': find,
                'dib.overlay.win32gui.GetClientRect': lambda h: (0, 0, 1, 1) if h in (401, 410) else (0, 0, 4080, 1440),
                'dib.overlay.win32gui.ClientToScreen': lambda h, pt: (pt[0] - 1920, pt[1]),
                'dib.overlay.win32gui.IsWindowVisible': lambda h: True,
                'dib.overlay.listview_count': lambda h: counts[h],
                'dib.overlay.diag.log': Mock(),
            }
            for name, value in patches.items():
                stack.enter_context(patch(name, value))
            self.assertEqual(desktop_listview(), (101, 111))

    def test_denied_open_preserves_error_code_and_records_target(self):
        kernel = Mock()
        kernel.OpenProcess.return_value = 0
        with patch('dib.overlay.ctypes.WinDLL', return_value=kernel), \
                patch('dib.overlay.ctypes.get_last_error', return_value=5), \
                patch('dib.overlay.win32process.GetWindowThreadProcessId', return_value=(1, 123)), \
                patch('dib.overlay.diag.log') as log:
            with self.assertRaises(PermissionError) as caught:
                item_rects(456, [0])
        self.assertEqual(caught.exception.winerror, 5)
        self.assertIn('OpenProcess', str(caught.exception))
        self.assertIn('target_pid=123', log.call_args.args[0])
        kernel.VirtualAllocEx.assert_not_called()


class DesktopIconAutoShowTests(unittest.TestCase):
    def test_hidden_listview_is_shown_only_on_resolved_hwnd(self):
        desktop = Mock()
        with patch('dib.ui.desktop_listview', return_value=(100, 111)) as resolve, \
                patch('dib.ui.win32gui.IsWindow', return_value=True) as is_window, \
                patch('dib.ui.win32gui.IsWindowVisible', side_effect=[False, True]) as visible, \
                patch('dib.ui.win32gui.ShowWindow') as show:
            ensure_desktop_icons_visible(desktop)
        desktop.show_icons.assert_called_once_with()
        resolve.assert_called_once_with()
        is_window.assert_called_once_with(111)
        show.assert_called_once_with(111, win32con.SW_SHOWNA)
        self.assertEqual(visible.call_count, 2)

    def test_visible_listview_never_toggles(self):
        desktop = Mock()
        with patch('dib.ui.desktop_listview', return_value=(100, 111)), \
                patch('dib.ui.win32gui.IsWindow', return_value=True), \
                patch('dib.ui.win32gui.IsWindowVisible', return_value=True) as visible, \
                patch('dib.ui.win32gui.ShowWindow') as show:
            ensure_desktop_icons_visible(desktop)
        desktop.show_icons.assert_called_once_with()
        visible.assert_called_once_with(111)
        show.assert_not_called()

    def test_repeated_launch_with_visible_icons_does_not_toggle(self):
        desktop = object.__new__(ShellDesktop)
        desktop.get_flags = Mock(return_value=0x1)
        desktop.set_flags = Mock()
        with patch('dib.ui.desktop_listview', return_value=(100, 111)), \
                patch('dib.ui.win32gui.IsWindow', return_value=True), \
                patch('dib.ui.win32gui.IsWindowVisible', return_value=True) as visible, \
                patch('dib.ui.win32gui.ShowWindow') as show:
            ensure_desktop_icons_visible(desktop)
            ensure_desktop_icons_visible(desktop)
        self.assertEqual(desktop.get_flags.call_count, 2)
        desktop.set_flags.assert_not_called()
        self.assertEqual(visible.call_count, 2)
        show.assert_not_called()

    def test_missing_or_invalid_hwnd_raises_without_toggling(self):
        for hwnd in (0, 999):
            with self.subTest(hwnd=hwnd):
                desktop = Mock()
                with patch('dib.ui.desktop_listview', return_value=(100, hwnd)), \
                        patch('dib.ui.win32gui.IsWindow', return_value=False), \
                        patch('dib.ui.win32gui.ShowWindow') as show:
                    with self.assertRaisesRegex(RuntimeError, 'valid desktop icon window'):
                        ensure_desktop_icons_visible(desktop)
                desktop.show_icons.assert_called_once_with()
                show.assert_not_called()

    def test_failed_showwindow_raises_without_bypassing_guard(self):
        desktop = Mock()
        with patch('dib.ui.desktop_listview', return_value=(100, 111)), \
                patch('dib.ui.win32gui.IsWindow', return_value=True), \
                patch('dib.ui.win32gui.IsWindowVisible', side_effect=[False, False]), \
                patch('dib.ui.win32gui.ShowWindow') as show:
            with self.assertRaisesRegex(RuntimeError, 'desktop icon window is still hidden'):
                ensure_desktop_icons_visible(desktop)
        show.assert_called_once_with(111, win32con.SW_SHOWNA)


class DesktopStartupAutoShowTests(unittest.TestCase):
    def make_app(self, window_mode):
        app = object.__new__(DesktopBilliardsApp)
        app.closing = False
        app.window_mode = window_mode
        app.journal = 'layout.json'
        app.guide_enabled = True
        app.felt_enabled = False
        app.white_texture = object()
        return app

    def patch_start(self, stack, shell_factory):
        stack.enter_context(patch('dib.ui.diag.start_session'))
        shells = stack.enter_context(patch('dib.ui.ShellDesktop', side_effect=shell_factory))
        sleep = stack.enter_context(patch('dib.ui.time.sleep'))
        stack.enter_context(patch('dib.ui.BilliardsOverlay'))
        stack.enter_context(patch('dib.ui.BilliardsRunner'))
        stack.enter_context(patch('dib.ui.dpi_context'))
        stack.enter_context(patch.object(DesktopBilliardsApp, 'poll'))
        stack.enter_context(patch.object(DesktopBilliardsApp, 'spawn_watchdog'))
        fail = stack.enter_context(patch.object(DesktopBilliardsApp, 'fail'))
        fail.side_effect = lambda error: (_ for _ in ()).throw(error)
        return shells, sleep

    @staticmethod
    def ready_desktop(items=None):
        desktop = Mock()
        desktop.items.return_value = (items if items is not None
                                      else [{'id': 'item', 'x': 1, 'y': 2}])
        desktop.display_name.return_value = 'Item'
        return desktop

    def test_desktop_mode_shows_icons_before_begin(self):
        app = self.make_app(window_mode=False)
        initial = Mock()
        ready = self.ready_desktop()
        events = []
        sessions = []
        layouts = []
        created = []

        def shell_factory():
            desktop = initial if not created else ready
            created.append(desktop)
            events.append('shell')
            return desktop

        def layout_factory(desktop, journal):
            layouts.append(desktop)
            events.append('session')
            session = Mock()
            session.restore.side_effect = lambda: events.append('restore')
            session.begin.side_effect = lambda **kw: (
                events.append('begin'), ready.items.return_value)[1]
            sessions.append(session)
            return session

        def ensure(seen):
            events.append('ensure')
            self.assertIs(seen, initial)

        with ExitStack() as stack:
            shells, _ = self.patch_start(stack, shell_factory)
            stack.enter_context(patch('dib.ui.LayoutSession', side_effect=layout_factory))
            ensure_mock = stack.enter_context(
                patch('dib.ui.ensure_desktop_icons_visible', side_effect=ensure))
            app.start()

        self.assertEqual(events, ['shell', 'session', 'restore', 'ensure', 'shell', 'session', 'begin'])
        self.assertEqual(shells.call_count, 2)
        ensure_mock.assert_called_once_with(initial)
        self.assertEqual(layouts, [initial, ready])
        self.assertEqual(len(sessions), 2)
        sessions[0].restore.assert_called_once_with()
        sessions[1].begin.assert_called_once_with(overlay_only=True)
        sessions[1].restore.assert_not_called()

    def test_reopens_shell_view_until_icons_appear(self):
        app = self.make_app(window_mode=False)
        initial = Mock()
        empty = self.ready_desktop([])
        ready = self.ready_desktop()
        created = []
        layouts = []
        sequence = [initial, empty, ready]

        def shell_factory():
            desktop = sequence[len(created)]
            created.append(desktop)
            return desktop

        def layout_factory(desktop, journal):
            session = Mock()
            session.begin.return_value = ready.items.return_value
            layouts.append((desktop, session))
            return session

        with ExitStack() as stack:
            shells, sleep = self.patch_start(stack, shell_factory)
            stack.enter_context(patch('dib.ui.LayoutSession', side_effect=layout_factory))
            ensure = stack.enter_context(patch('dib.ui.ensure_desktop_icons_visible'))
            app.start()

        self.assertEqual(created, [initial, empty, ready])
        self.assertEqual(shells.call_count, 3)
        self.assertEqual(sleep.call_count, 1)
        ensure.assert_called_once_with(initial)
        self.assertEqual([desktop for desktop, _ in layouts], [initial, ready])
        layouts[1][1].begin.assert_called_once_with(overlay_only=True)

    def test_empty_shell_view_times_out_before_begin(self):
        app = self.make_app(window_mode=False)
        initial = Mock()
        empty = self.ready_desktop([])
        created = []

        def shell_factory():
            desktop = initial if not created else empty
            created.append(desktop)
            return desktop

        with ExitStack() as stack:
            shells, sleep = self.patch_start(stack, shell_factory)
            layout = stack.enter_context(patch('dib.ui.LayoutSession'))
            ensure = stack.enter_context(patch('dib.ui.ensure_desktop_icons_visible'))
            with self.assertRaisesRegex(RuntimeError, 'shell view stayed empty'):
                app.start()

        self.assertEqual(shells.call_count, 1 + DESKTOP_ITEMS_POLL_ATTEMPTS)
        self.assertEqual(sleep.call_count, DESKTOP_ITEMS_POLL_ATTEMPTS - 1)
        ensure.assert_called_once_with(initial)
        self.assertEqual(layout.call_count, 1)
        layout.return_value.begin.assert_not_called()

    def test_com_exception_from_reopen_is_not_retried(self):
        app = self.make_app(window_mode=False)
        initial = Mock()
        failure = OSError('Shell COM HRESULT 80070422')
        created = []

        def shell_factory():
            if not created:
                desktop = initial
            elif len(created) == 1:
                desktop = self.ready_desktop([])
            else:
                raise failure
            created.append(desktop)
            return desktop

        with ExitStack() as stack:
            shells, sleep = self.patch_start(stack, shell_factory)
            layout = stack.enter_context(patch('dib.ui.LayoutSession'))
            stack.enter_context(patch('dib.ui.ensure_desktop_icons_visible'))
            with self.assertRaises(OSError) as caught:
                app.start()

        self.assertIs(caught.exception, failure)
        self.assertEqual(shells.call_count, 3)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(layout.call_count, 1)
        layout.return_value.begin.assert_not_called()

    def test_window_mode_does_not_show_icons_or_start_layout(self):
        app = self.make_app(window_mode=True)
        desktop = self.ready_desktop()
        with ExitStack() as stack:
            self.patch_start(stack, lambda: desktop)
            layout = stack.enter_context(patch('dib.ui.LayoutSession'))
            ensure = stack.enter_context(patch('dib.ui.ensure_desktop_icons_visible'))
            stack.enter_context(patch.object(DesktopBilliardsApp, 'create_game_window',
                                             return_value=object()))
            stack.enter_context(patch.object(DesktopBilliardsApp, '_widget_hwnd',
                                             return_value=123))
            app.start()

        ensure.assert_not_called()
        desktop.show_icons.assert_not_called()
        layout.assert_not_called()
