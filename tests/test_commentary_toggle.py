"""Commentary toggle: persisted choice, Tk/animation-thread hand-off, no Tk window."""
import json
import queue
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import win32con

from dib import ui
from dib.bot import BotController
from dib.overlay import (
    WS_EX_LAYERED,
    WS_EX_NOACTIVATE,
    BilliardsOverlay,
)
from dib.ui import DesktopBilliardsApp


class FakeSettings:
    """Behaves like RankSettings for get/update without touching disk or Tk."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.updated = []

    def get(self, key, default=None):
        return self.values.get(key, default)

    def update(self, **kwargs):
        self.updated.append(dict(kwargs))
        self.values.update(kwargs)


def guard_overlay():
    """Mock that rejects any write to animation-thread commentary state from Tk."""
    overlay = mock.Mock(spec_set=['request_commentary_toggle',
                                  'commentary_ack_serial', 'commentary_ack_enabled'])
    overlay.commentary_ack_serial = 0
    overlay.commentary_ack_enabled = True
    return overlay


def real_overlay():
    overlay = object.__new__(BilliardsOverlay)
    overlay.bot = BotController()
    overlay.bot_commands = queue.SimpleQueue()
    overlay.bot_command_serial = overlay.bot_ack_serial = 0
    overlay.bot_ack_mode = 'off'
    overlay.commentary_commands = queue.SimpleQueue()
    overlay.commentary_command_serial = overlay.commentary_ack_serial = 0
    overlay.commentary_ack_enabled = True
    overlay.commentary_enabled = True
    overlay.commentary_engine = mock.Mock()
    overlay.commentary_state = None
    overlay.commentary_visible = False
    overlay.commentary_mode = 'off'
    overlay.table_windows = {}
    overlay.felt_visible = True
    overlay.hidden = False
    overlay.table = SimpleNamespace(bounds=(0, 0, 200, 100), round_serial=0, state='ready')
    overlay.shot_reports = queue.SimpleQueue()
    overlay.parent = 1
    overlay.window_mode = False
    overlay.blocked_by = overlay.last_blocked = None
    return overlay


def ui_app(values=None, overlay=None):
    app = DesktopBilliardsApp.__new__(DesktopBilliardsApp)
    app.settings = FakeSettings(values)
    app.commentary_enabled = bool(app.settings.get('commentary', True))
    app._commentary_ack_serial = 0
    app.panel = mock.Mock()
    app.runner = SimpleNamespace(overlay=overlay)
    return app


class CommentarySettingTests(unittest.TestCase):
    def test_missing_key_defaults_to_true_without_writing_settings(self):
        app = ui_app(values={})
        self.assertTrue(app.commentary_enabled)
        self.assertEqual(app.settings.updated, [])

    def test_saved_false_is_loaded(self):
        app = ui_app(values={'commentary': False})
        self.assertFalse(app.commentary_enabled)

    def test_toggle_persists_and_only_queues_the_value(self):
        app = ui_app(values={'commentary': True}, overlay=guard_overlay())
        overlay = app.runner.overlay
        app.toggle_commentary()
        self.assertFalse(app.commentary_enabled)
        self.assertEqual(app.settings.updated, [{'commentary': False}])
        overlay.request_commentary_toggle.assert_called_once_with(False)
        app.toggle_commentary()
        self.assertTrue(app.commentary_enabled)
        self.assertEqual(app.settings.updated, [{'commentary': False}, {'commentary': True}])
        self.assertEqual(overlay.request_commentary_toggle.call_args_list,
                         [mock.call(False), mock.call(True)])

    def test_toggle_without_a_runner_still_persists(self):
        app = ui_app(values={'commentary': True})
        app.runner = None
        app.toggle_commentary()
        self.assertFalse(app.commentary_enabled)
        self.assertEqual(app.settings.updated, [{'commentary': False}])

    def test_pill_follows_the_ack_not_the_toggle(self):
        app = ui_app(values={'commentary': True}, overlay=guard_overlay())
        overlay = app.runner.overlay
        app.sync_commentary_ack(overlay)
        app.panel.set_look.assert_not_called()  # serial 0 is the idle state
        app.toggle_commentary()
        app.panel.set_look.assert_not_called()  # queueing does not repaint
        overlay.commentary_ack_serial = 1
        overlay.commentary_ack_enabled = False
        app.sync_commentary_ack(overlay)
        app.panel.set_look.assert_called_once_with('commentary', 'Bình luận Off', 'off')
        app.panel.set_look.reset_mock()
        app.sync_commentary_ack(overlay)
        app.panel.set_look.assert_not_called()  # same serial: no repaint
        overlay.commentary_ack_serial = 2
        overlay.commentary_ack_enabled = True
        app.sync_commentary_ack(overlay)
        app.panel.set_look.assert_called_once_with('commentary', 'Bình luận On', 'on')

    def test_pill_without_overlay_uses_the_saved_choice(self):
        app = ui_app(values={'commentary': False})
        app.runner = None
        app.refresh_commentary_button()
        app.panel.set_look.assert_called_once_with('commentary', 'Bình luận Off', 'off')

    def test_bot_off_does_not_overwrite_the_saved_choice(self):
        overlay = real_overlay()
        app = ui_app(values={'commentary': False}, overlay=overlay)
        app.toggle_commentary()  # save On and queue it
        self.assertTrue(overlay.consume_commentary_commands())
        for _ in range(5):  # off -> easy -> medium -> hard -> extreme -> off
            overlay.request_bot_cycle()
        overlay.consume_bot_commands()
        self.assertEqual(overlay.bot.mode, 'off')
        self.assertTrue(overlay.commentary_enabled)
        self.assertTrue(app.commentary_enabled)
        self.assertEqual(app.settings.updated, [{'commentary': True}])
        app.round_snapshot = None
        app.refresh_bot_button()
        self.assertTrue(all(call.args[0] != 'commentary'
                            for call in app.panel.set_look.call_args_list))


class CommentaryQueueTests(unittest.TestCase):
    def test_requests_are_applied_in_arrival_order_with_matching_acks(self):
        overlay = real_overlay()
        self.assertEqual(overlay.request_commentary_toggle(False), 1)
        self.assertEqual(overlay.request_commentary_toggle(True), 2)
        self.assertTrue(overlay.commentary_enabled)  # Tk never writes the live value
        self.assertEqual(overlay.commentary_ack_serial, 0)
        self.assertTrue(overlay.consume_commentary_commands())
        self.assertEqual((overlay.commentary_enabled, overlay.commentary_ack_serial,
                          overlay.commentary_ack_enabled), (True, 2, True))
        self.assertEqual(overlay.request_commentary_toggle(False), 3)
        self.assertTrue(overlay.consume_commentary_commands())
        self.assertEqual((overlay.commentary_enabled, overlay.commentary_ack_serial,
                          overlay.commentary_ack_enabled), (False, 3, False))
        self.assertFalse(overlay.consume_commentary_commands())

    def test_disabling_hides_an_active_bubble_immediately(self):
        overlay = real_overlay()
        overlay.commentary_visible = True
        overlay.commentary_state = ('old line', 1.0, 2.0)
        overlay.table_windows = {'commentary': 42}
        overlay.request_commentary_toggle(False)
        with mock.patch('dib.overlay.win32gui.ShowWindow') as show:
            overlay.consume_commentary_commands()
        show.assert_called_once_with(42, mock.ANY)
        self.assertFalse(overlay.commentary_visible)
        self.assertIsNone(overlay.commentary_state)
        self.assertFalse(overlay.commentary_ack_enabled)

    def test_bubble_stays_hidden_while_bot_is_off_even_when_enabled(self):
        overlay = real_overlay()
        overlay.commentary_visible = True
        overlay.commentary_state = ('old line', 1.0, 2.0)
        overlay.table_windows = {'commentary': 42}
        overlay.commentary_engine.reset = mock.Mock()
        with mock.patch('dib.overlay.win32gui.ShowWindow'):
            overlay.draw_commentary(3.0)
        self.assertFalse(overlay.commentary_visible)
        self.assertIsNone(overlay.commentary_state)

    def test_animation_thread_consumes_queued_toggles_from_update_pointer(self):
        overlay = real_overlay()
        overlay.credit_links = []
        overlay.pointer = SimpleNamespace(update=mock.Mock())
        overlay.update_credit_pointer = mock.Mock()
        overlay.update_round_action_pointer = mock.Mock()
        overlay.request_commentary_toggle(False)
        with mock.patch('dib.overlay.win32gui.ScreenToClient', return_value=(10, 10)), \
                mock.patch('dib.overlay.win32gui.WindowFromPoint', return_value=0), \
                mock.patch('dib.overlay.win32api.GetSystemMetrics', return_value=0), \
                mock.patch('dib.overlay.win32api.GetAsyncKeyState', return_value=0):
            overlay.update_pointer(1.0, (10, 10))
        self.assertFalse(overlay.commentary_enabled)
        self.assertEqual(overlay.commentary_ack_serial, 1)


class CommentaryUiJsonTests(unittest.TestCase):
    """Persist the toggle through the real RankSettings-backed ui.json, without Tk."""

    @staticmethod
    def settings(folder):
        with mock.patch.object(ui, 'data_directory', return_value=Path(folder)):
            return DesktopBilliardsApp.make_settings()

    @staticmethod
    def app_with(settings, overlay=None):
        app = ui_app(values={}, overlay=overlay)
        app.settings = settings
        app.commentary_enabled = bool(settings.get('commentary', True))
        return app

    def test_missing_commentary_key_defaults_to_true(self):
        with TemporaryDirectory() as folder:
            settings = self.settings(folder)
            self.assertEqual(settings.path, Path(folder) / 'ui.json')
            self.assertFalse(settings.path.exists())
            self.assertTrue(self.app_with(settings).commentary_enabled)

    def test_toggle_round_trips_through_ui_json(self):
        with TemporaryDirectory() as folder:
            settings = self.settings(folder)
            app = self.app_with(settings, guard_overlay())
            self.assertTrue(app.commentary_enabled)
            app.toggle_commentary()
            reloaded = self.settings(folder)
            self.assertIs(reloaded.get('commentary', True), False)
            self.assertIs(json.loads(settings.path.read_text(encoding='utf-8'))['commentary'], False)
            self.assertFalse(self.app_with(reloaded).commentary_enabled)
            self.app_with(reloaded).toggle_commentary()
            self.assertIs(self.settings(folder).get('commentary', True), True)


class CommentaryLayerTests(unittest.TestCase):
    def test_bubble_layer_flags_and_top_z_order(self):
        overlay = real_overlay()
        overlay.classname = 'DIBCommentaryLayerTest'
        overlay.table_windows = {}
        overlay.table.work_area = (0, 0, 200, 100)
        overlay.bot.mode = 'easy'
        overlay.commentary_mode = 'easy'
        overlay.commentary_state = ('Bình luận thử', 3.0, 10.0)
        overlay.present = mock.Mock()
        overlay.background_luma = mock.Mock(return_value=None)
        with mock.patch('dib.overlay.win32api.GetModuleHandle', return_value=7), \
                mock.patch('dib.overlay.win32gui.CreateWindowEx', return_value=42) as create, \
                mock.patch('dib.overlay.win32gui.SetWindowPos') as set_pos:
            overlay.draw_commentary(3.0)
        self.assertEqual(create.call_args.args[0], WS_EX_LAYERED | 0x20 | WS_EX_NOACTIVATE)
        self.assertEqual(create.call_args.args[3], win32con.WS_CHILD)
        self.assertTrue(overlay.commentary_visible)
        self.assertEqual(set_pos.call_args.args[0], 42)
        self.assertEqual(set_pos.call_args.args[1], win32con.HWND_TOP)
        self.assertTrue(set_pos.call_args.args[6] & win32con.SWP_SHOWWINDOW)


if __name__ == '__main__':
    unittest.main()
