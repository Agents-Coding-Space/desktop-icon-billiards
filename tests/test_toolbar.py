"""The compact toolbar and hotkeys must survive a withdrawn Tk window."""
from contextlib import contextmanager
from types import SimpleNamespace

import tkinter as tk
import unittest
from unittest import mock

import win32con
import win32gui

from dib import config
from dib.config import fmt_pos
from dib.bot import BotController
from dib.hotkeys import Hotkeys
from dib.panel import MainPanel
from dib.ui import DesktopBilliardsApp, bot_eye_text


class ToolbarTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.addCleanup(self.root.destroy)

    def test_only_exit_hotkey_is_enabled(self):
        hotkeys = Hotkeys()
        self.addCleanup(hotkeys.close)
        self.assertNotIn(config.HIDE_HOTKEY_ID, hotkeys.registered)
        self.assertIn(config.HOTKEY_ID, hotkeys.registered)
        self.root.withdraw()
        for identifier in (config.HIDE_HOTKEY_ID, config.HOTKEY_ID):
            win32gui.PostMessage(hotkeys.hwnd, win32con.WM_HOTKEY, identifier, 0)
        self.root.after(100, self.root.quit)
        self.root.mainloop()
        self.assertEqual(self.root.state(), 'withdrawn')
        self.assertEqual(hotkeys.pending.get_nowait(), config.HOTKEY_ID)
        self.assertTrue(hotkeys.pending.empty())

    def test_all_actions_are_visible_in_one_borderless_row(self):
        calls = []
        names = ('restart', 'toggle_guide', 'toggle_rank', 'leaderboard', 'toggle_felt',
                 'check_updates', 'quit', 'white_ball', 'toggle_bot', 'toggle_window', 'toggle_pin')
        callbacks = {name: lambda name=name: calls.append(name) for name in names}
        panel = MainPanel(self.root, config.VERSION, callbacks, config.CREDIT)
        self.root.update()
        self.assertTrue(self.root.overrideredirect())
        self.assertEqual(set(panel.pills),
                         {'restart', 'guide', 'rank', 'leaderboard', 'felt', 'update', 'quit',
                          'white', 'bot', 'window', 'pin', 'minimize'})
        positions = [pill.winfo_rooty() for pill in panel.pills.values()]
        self.assertLessEqual(max(positions) - min(positions), 2)
        self.assertLess(self.root.winfo_reqheight(), 48)
        for pill in panel.pills.values():
            self.assertTrue(pill.winfo_ismapped())
            pill._release(SimpleNamespace(x=1, y=1))
        self.assertNotIn('hide', panel.pills)
        self.assertEqual(set(calls), set(names))
        self.assertEqual(len(calls), len(panel.pills) - 1)  # Min is bound directly.

    def test_minimize_parks_half_the_strip_above_screen_and_restores(self):
        callbacks = {name: (lambda name=name: None) for name in
                     ('restart', 'toggle_guide', 'toggle_rank', 'leaderboard', 'toggle_felt',
                      'check_updates', 'quit', 'white_ball', 'toggle_bot', 'toggle_window', 'toggle_pin')}
        panel = MainPanel(self.root, config.VERSION, callbacks)
        self.root.update()
        self.root.geometry('+100+80')
        original_y = self.root.winfo_y()
        panel.minimize()
        self.root.update()
        self.assertTrue(panel.minimized)
        self.assertLess(self.root.winfo_y(), 0)
        panel.restore()
        self.root.update()
        self.assertFalse(panel.minimized)
        self.assertEqual(self.root.winfo_y(), original_y)


    def test_bot_refresh_writes_score_to_the_panels_label(self):
        """Regression: the score belongs to MainPanel, not the app object."""
        callbacks = {name: (lambda name=name: None) for name in
                     ('restart', 'toggle_guide', 'toggle_rank', 'leaderboard', 'toggle_felt',
                      'check_updates', 'donate', 'quit', 'open_credit', 'white_ball', 'toggle_bot', 'toggle_window', 'toggle_pin')}
        panel = MainPanel(self.root, config.VERSION, callbacks, config.CREDIT)
        bot = BotController()
        bot.mode = 'easy'
        app = DesktopBilliardsApp.__new__(DesktopBilliardsApp)
        app.panel = panel
        app.runner = SimpleNamespace(overlay=SimpleNamespace(bot=bot))
        app.round_snapshot = None
        app.refresh_bot_button()
        self.assertEqual(panel.pills['bot'].cget('text'), 'Easy')
        self.assertEqual(panel.remaining_title.get(), 'You 0 - Bot 0')
        bot.mode = 'off'
        app.refresh_bot_button()
        self.assertEqual(panel.pills['bot'].cget('text'), 'Bot Off')
        self.assertEqual(panel.remaining_title.get(), 'Left')


    def test_hidden_felt_eye_shows_mode_turn_and_score(self):
        bot = BotController()
        self.assertEqual(bot_eye_text(bot), '')
        bot.mode = 'medium'
        self.assertEqual(bot_eye_text(bot), 'MEDIUM · YOU · 0 : 0')
        bot.turn = 'bot'
        bot.scores = {'human': 2, 'bot': 5}
        self.assertEqual(bot_eye_text(bot), 'MEDIUM · BOT · 2 : 5')
        self.assertEqual(bot_eye_text(bot, finished=True), 'RESULT · Bot wins 2-5')


    def test_hidden_felt_eye_badge_updates_with_the_bot(self):
        bot = BotController()
        bot.mode = 'hard'
        app = DesktopBilliardsApp.__new__(DesktopBilliardsApp)
        app.root = self.root
        app.toolbar_eye = None
        app.toolbar_region = (100, 100, 300, 140)
        app.round_snapshot = None
        app.runner = SimpleNamespace(overlay=SimpleNamespace(bot=bot))
        self.addCleanup(lambda: app.toolbar_eye and app.toolbar_eye.destroy())
        app.show_toolbar_eye()
        self.assertEqual(int(app.toolbar_eye_canvas.cget('width')), 260)
        texts = [app.toolbar_eye_canvas.itemcget(i, 'text')
                 for i in app.toolbar_eye_canvas.find_withtag('score')]
        self.assertEqual(texts, ['HARD', 'YOU', '0 : 0'])
        bot.turn = 'bot'
        bot.scores = {'human': 1, 'bot': 3}
        app.paint_toolbar_eye()
        texts = [app.toolbar_eye_canvas.itemcget(i, 'text')
                 for i in app.toolbar_eye_canvas.find_withtag('score')]
        self.assertEqual(texts, ['HARD', 'BOT', '1 : 3'])
        bot.mode = 'off'
        app.show_toolbar_eye()
        self.assertEqual(int(app.toolbar_eye_canvas.cget('width')), 36)


    def test_window_configure_debounces_to_the_final_geometry(self):
        app = DesktopBilliardsApp.__new__(DesktopBilliardsApp)
        app.closing = False
        app.root = mock.Mock()
        app.root.after.return_value = 'after-id'
        window = app.game_window = mock.Mock()
        window.winfo_width.return_value = 1200
        window.winfo_height.return_value = 600
        window.winfo_x.return_value = -10
        window.winfo_y.return_value = 20
        app.window_size = (1000, 500)
        app.settings = mock.Mock()
        app.runner = SimpleNamespace(overlay=SimpleNamespace(request_resize=mock.Mock()))
        event = SimpleNamespace(widget=window, width=1000, height=500)
        app._window_configured(event)
        app._window_configured(event)
        self.assertEqual(app.root.after.call_count, 2)
        self.assertEqual(app.root.after.call_args.args, (150, app._apply_window_geometry))
        app.root.after_cancel.assert_called_with('after-id')
        app.settings.update.assert_not_called()
        app.runner.overlay.request_resize.assert_not_called()

        window.winfo_width.return_value = 1400
        window.winfo_height.return_value = 700
        app._apply_window_geometry()
        self.assertIsNone(app._window_configure_after)
        app.settings.update.assert_called_once_with(win_x=-10, win_y=20, win_w=1400, win_h=700)
        app.runner.overlay.request_resize.assert_called_once_with(1400, 700)

    def test_pending_geometry_callback_is_cancelled_with_the_window(self):
        app = DesktopBilliardsApp.__new__(DesktopBilliardsApp)
        app.root = mock.Mock()
        app._window_configure_after = 'after-id'
        app.game_window = mock.Mock()
        app.window_score_labels = ()
        app._window_score_parts = None
        app.destroy_game_window()
        app.root.after_cancel.assert_called_once_with('after-id')
        self.assertIsNone(app._window_configure_after)
        self.assertIsNone(app.game_window)

    def test_toggle_bot_only_queues_the_mode_change(self):
        app = DesktopBilliardsApp.__new__(DesktopBilliardsApp)
        bot = BotController()
        overlay = SimpleNamespace(bot=bot, request_bot_cycle=mock.Mock())
        app.runner = SimpleNamespace(overlay=overlay)
        app.toggle_bot()
        overlay.request_bot_cycle.assert_called_once_with()
        self.assertEqual(bot.mode, 'off')

    def test_tray_toggle_shows_and_hides_only_the_game_window(self):
        app = DesktopBilliardsApp.__new__(DesktopBilliardsApp)
        window = app.game_window = mock.Mock()
        window.winfo_id.return_value = 1234
        app.window_mode = True
        with mock.patch('dib.ui.win32gui.IsWindow', return_value=True), \
             mock.patch('dib.ui.win32gui.GetAncestor', return_value=5678), \
             mock.patch('dib.ui.win32gui.IsWindowVisible', return_value=True), \
             mock.patch('dib.ui.win32gui.IsIconic', return_value=False):
            window.winfo_ismapped.return_value = True
            app.toggle_game_window()
            window.withdraw.assert_called_once_with()
            window.deiconify.assert_not_called()
            window.winfo_ismapped.return_value = False
            app.toggle_game_window()
            window.deiconify.assert_called_once_with()
            window.lift.assert_called_once_with()
            window.withdraw.assert_called_once_with()
        window.reset_mock()
        app.window_mode = False
        window.winfo_ismapped.return_value = True
        with mock.patch('dib.ui.win32gui.IsWindow', return_value=True), \
             mock.patch('dib.ui.win32gui.GetAncestor', return_value=5678), \
             mock.patch('dib.ui.win32gui.IsWindowVisible', return_value=True), \
             mock.patch('dib.ui.win32gui.IsIconic', return_value=False):
            app.toggle_game_window()
        window.withdraw.assert_called_once_with()

    def test_geometry_keeps_zero_and_negative_coordinates(self):
        self.assertEqual(fmt_pos(0, 0), '+0+0')
        self.assertEqual(fmt_pos(0, -8), '+0+-8')
        self.assertEqual(fmt_pos(-40, 12), '+-40+12')
        with mock.patch('dib.ui.win32api.MonitorFromPoint', return_value=1), \
             mock.patch('dib.ui.win32api.GetMonitorInfo',
                        return_value={'Work': (0, 0, 1920, 1080)}):
            self.assertEqual(DesktopBilliardsApp.badge_geometry(36, 28), '36x28+1876+8')

    def test_window_size_is_hard_locked_to_two_to_one(self):
        self.assertEqual(DesktopBilliardsApp.locked_window_size(1000, 700, 200, 0), (1000, 500))
        self.assertEqual(DesktopBilliardsApp.locked_window_size(1000, 700, 0, 200), (1400, 700))
        for width, height in ((801, 700), (320, 160), (1001, 333)):
            locked = DesktopBilliardsApp.locked_window_size(width, height)
            self.assertEqual(locked[0], locked[1] * 2)
            self.assertEqual(locked[0] % 2, 0)


class GameWindowVisibilityTests(unittest.TestCase):
    """Tk mapping alone cannot distinguish withdrawn from natively minimized."""

    def _app(self, mapped=True):
        app = DesktopBilliardsApp.__new__(DesktopBilliardsApp)
        window = app.game_window = mock.Mock()
        window.winfo_id.return_value = 1234
        window.winfo_ismapped.return_value = mapped
        app.window_mode = True
        return app, window

    @contextmanager
    def _win32(self, valid=True, visible=True, iconic=False):
        with mock.patch('dib.ui.win32gui.IsWindow', return_value=valid) as is_window, \
             mock.patch('dib.ui.win32gui.GetAncestor', return_value=5678) as get_ancestor, \
             mock.patch('dib.ui.win32gui.IsWindowVisible', return_value=visible) as is_visible, \
             mock.patch('dib.ui.win32gui.IsIconic', return_value=iconic) as is_iconic, \
             mock.patch('dib.ui.win32gui.ShowWindow') as show_window:
            yield SimpleNamespace(is_window=is_window, get_ancestor=get_ancestor,
                                  is_visible=is_visible, is_iconic=is_iconic,
                                  show_window=show_window)

    def test_native_state_overrides_tk_mapping_in_visibility_check(self):
        app, _window = self._app()
        with self._win32() as win32:
            self.assertTrue(app.game_window_visible())
        win32.get_ancestor.assert_called_once_with(1234, win32con.GA_ROOT)
        win32.is_visible.assert_called_once_with(5678)
        win32.is_iconic.assert_called_once_with(5678)

    def test_visible_window_is_withdrawn(self):
        app, window = self._app()
        with self._win32() as win32:
            app.toggle_game_window()
        window.withdraw.assert_called_once_with()
        window.deiconify.assert_not_called()
        win32.show_window.assert_not_called()

    def test_natively_hidden_window_is_deiconified_without_restore(self):
        app, window = self._app()
        with self._win32(visible=False) as win32:
            self.assertFalse(app.game_window_visible())
            app.toggle_game_window()
        window.deiconify.assert_called_once_with()
        window.lift.assert_called_once_with()
        window.withdraw.assert_not_called()
        win32.show_window.assert_not_called()

    def test_withdrawn_tk_window_is_deiconified_without_restore(self):
        app, window = self._app(mapped=False)
        with self._win32() as win32:
            self.assertFalse(app.game_window_visible())
            app.toggle_game_window()
        window.deiconify.assert_called_once_with()
        window.lift.assert_called_once_with()
        win32.show_window.assert_not_called()

    def test_iconic_window_is_restored_before_deiconify(self):
        app, window = self._app()
        events = []
        window.deiconify.side_effect = lambda: events.append('deiconify')
        with self._win32(iconic=True) as win32:
            win32.show_window.side_effect = lambda hwnd, cmd: events.append(('show', hwnd, cmd))
            self.assertFalse(app.game_window_visible())
            app.toggle_game_window()
        self.assertEqual(events, [('show', 5678, win32con.SW_RESTORE), 'deiconify'])
        window.lift.assert_called_once_with()
        window.withdraw.assert_not_called()

    def test_invalid_hwnd_makes_no_native_visibility_or_restore_calls(self):
        app, window = self._app()
        with self._win32(valid=False) as win32:
            self.assertFalse(app.game_window_visible())
            app.toggle_game_window()
        win32.get_ancestor.assert_not_called()
        win32.is_visible.assert_not_called()
        win32.is_iconic.assert_not_called()
        win32.show_window.assert_not_called()
        window.deiconify.assert_not_called()
        window.lift.assert_not_called()
        window.withdraw.assert_not_called()

    def test_destroyed_tk_window_does_not_crash_tray_toggle(self):
        app, window = self._app(mapped=False)
        window.deiconify.side_effect = tk.TclError('window was destroyed')
        with self._win32(visible=False) as win32:
            app.toggle_game_window()
        window.lift.assert_not_called()
        win32.show_window.assert_not_called()

    def test_destroyed_tk_window_does_not_crash_tray_hide(self):
        app, window = self._app()
        window.withdraw.side_effect = tk.TclError('window was destroyed')
        with self._win32():
            app.toggle_game_window()
        window.deiconify.assert_not_called()

    def test_missing_game_window_is_a_noop(self):
        app = DesktopBilliardsApp.__new__(DesktopBilliardsApp)
        app.game_window = None
        app.window_mode = True
        with self._win32() as win32:
            self.assertFalse(app.game_window_visible())
            app.toggle_game_window()
        win32.is_window.assert_not_called()
        win32.show_window.assert_not_called()

    def test_window_mode_false_still_toggles_an_existing_window(self):
        app, window = self._app()
        app.window_mode = False
        with self._win32() as win32:
            app.toggle_game_window()
        window.withdraw.assert_called_once_with()
        win32.show_window.assert_not_called()
        with self._win32(visible=False) as win32:
            app.toggle_game_window()
        window.deiconify.assert_called_once_with()
        window.lift.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
