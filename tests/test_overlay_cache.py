"""Collision artwork is reused without changing pixels or shared layers."""
from collections import OrderedDict
import queue
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from dib.overlay import BALL_CANVAS, RING_LEVELS, BilliardsOverlay


class CollisionCacheTests(unittest.TestCase):
    def test_colliding_sprites_reuse_layers_without_mutating_them(self):
        overlay = object.__new__(BilliardsOverlay)
        overlay.table = SimpleNamespace(radius=35)
        overlay.frame_cache = OrderedDict()
        overlay.frame_cache_bytes = 0
        overlay.ring_frames = {level: overlay.ring_layer(level) for level in range(1, RING_LEVELS + 1)}
        originals = {level: frame.tobytes() for level, frame in overlay.ring_frames.items()}
        source = Image.new('RGBA', (64, 64), (80, 140, 200, 180))
        with patch.object(overlay, 'ring_layer', side_effect=AssertionError('redrawing a cached ring')):
            for level in range(1, RING_LEVELS + 1):
                for identity in range(45):
                    frame = overlay.sprite_frame(source, identity, 64, 64, identity, ring=level)
                    plain = overlay.sprite_frame(source, identity, 64, 64, identity).copy()
                    plain.alpha_composite(overlay.ring_frames[level])
                    self.assertEqual(frame.tobytes(), plain.tobytes())
        self.assertEqual(originals, {level: frame.tobytes() for level, frame in overlay.ring_frames.items()})
        self.assertEqual(sum(frame.width * frame.height * 4 for frame in overlay.ring_frames.values()),
                         RING_LEVELS * BALL_CANVAS * BALL_CANVAS * 4)


class ResizeRollbackTests(unittest.TestCase):
    def overlay(self):
        overlay = object.__new__(BilliardsOverlay)
        overlay.window_mode = True
        overlay.parent = 123
        overlay.resize_request = (800, 400)
        overlay.primary_bounds = overlay.geometry = (0, 0, 600, 300)
        overlay.table = SimpleNamespace(work_area=(0, 0, 600, 300))
        overlay.table_windows = {'rail': 11}
        overlay.items = [{'id': 'icon-0'}]
        overlay.felt_visible = True
        overlay.sources = [Image.new('RGBA', (32, 32), 'red')]
        overlay.templates = [object()]
        overlay.clear_table = Mock(side_effect=AssertionError('clear_table before art is ready'))
        overlay.draw_table = Mock(side_effect=AssertionError('draw_table before art is ready'))
        return overlay

    def test_failed_prepare_keeps_old_windows_art_and_bounds(self):
        overlay = self.overlay()
        old_table, old_sources = overlay.table, list(overlay.sources)
        with patch('dib.overlay.win32gui.GetClientRect', return_value=(0, 0, 600, 300)), \
                patch.object(overlay, 'prepare', side_effect=RuntimeError('COM teardown')), \
                patch('dib.overlay.diag.log') as log:
            overlay.apply_resize()
        self.assertIs(overlay.table, old_table)
        self.assertEqual(overlay.table_windows, {'rail': 11})
        self.assertEqual(overlay.sources, old_sources)
        self.assertEqual(overlay.primary_bounds, (0, 0, 600, 300))
        self.assertEqual(overlay.geometry, (0, 0, 600, 300))
        self.assertIsNone(overlay.resize_request)
        overlay.clear_table.assert_not_called()
        overlay.draw_table.assert_not_called()
        log.assert_called_once()

    def test_speculative_state_does_not_mutate_live_art(self):
        overlay = self.overlay()
        candidate = overlay.speculative_state()
        candidate.sources.append(Image.new('RGBA', (8, 8), 'blue'))
        candidate.templates.append(object())
        candidate.rail_frame = ('new rail', (1, 2))
        self.assertEqual(len(overlay.sources), 1)
        self.assertEqual(len(overlay.templates), 1)
        self.assertFalse(hasattr(overlay, 'rail_frame'))
        overlay.commit_art_state(candidate)
        self.assertEqual(overlay.rail_frame, ('new rail', (1, 2)))
        self.assertEqual(len(overlay.sources), 2)

    def test_successful_rebuild_clears_table_only_after_prepare(self):
        overlay = self.overlay()
        events = []
        fresh = object()
        overlay.table.rescaled = Mock(return_value=fresh)
        overlay.clear_table = Mock(side_effect=lambda: events.append('clear'))
        overlay.draw_table = Mock(side_effect=lambda: events.append('draw'))
        overlay.felt_visible = False
        overlay.set_felt = Mock(side_effect=lambda visible: events.append('felt'))

        def fake_prepare(candidate):
            events.append('prepare')
            candidate.rail_frame = ('rail', (1, 2))
            candidate.pocket_frame = ('pockets', (3, 4))

        with patch('dib.overlay.win32gui.GetClientRect', return_value=(0, 0, 600, 300)), \
                patch.object(BilliardsOverlay, 'prepare', fake_prepare), \
                patch('dib.overlay.diag.log'):
            overlay.apply_resize()
        self.assertEqual(events, ['prepare', 'clear', 'draw', 'felt'])
        overlay.set_felt.assert_called_once_with(False)
        self.assertIs(overlay.table, fresh)
        self.assertEqual(overlay.rail_frame, ('rail', (1, 2)))
        self.assertEqual(overlay.primary_bounds, (0, 0, 800, 400))


class BotQueueTests(unittest.TestCase):
    def make_overlay(self):
        overlay = object.__new__(BilliardsOverlay)
        from dib.bot import BotController
        overlay.bot = BotController()
        overlay.bot_commands = queue.SimpleQueue()
        overlay.bot_command_serial = overlay.bot_ack_serial = 0
        overlay.bot_ack_mode = 'off'
        overlay.table = SimpleNamespace(bounds=(0, 0, 200, 100), round_serial=0)
        overlay.parent = 1
        overlay.last_blocked = overlay.blocked_by = None
        overlay.window_mode = False
        overlay.credit_links = []
        overlay.pointer = SimpleNamespace(update=Mock())
        overlay.update_credit_pointer = Mock()
        overlay.update_round_action_pointer = Mock()
        return overlay

    def test_queued_cycles_are_applied_only_by_the_animation_thread(self):
        overlay = self.make_overlay()
        overlay.request_bot_cycle()
        overlay.request_bot_cycle()
        self.assertEqual(overlay.bot.mode, 'off')
        self.assertEqual(overlay.bot_ack_serial, 0)
        seen = []
        overlay.bot.update = Mock(side_effect=lambda table, now: seen.append(overlay.bot.mode))
        with patch('dib.overlay.win32gui.ScreenToClient', return_value=(10, 10)), \
                patch('dib.overlay.win32gui.WindowFromPoint', return_value=0), \
                patch('dib.overlay.win32api.GetSystemMetrics', return_value=0), \
                patch('dib.overlay.win32api.GetAsyncKeyState', return_value=0):
            overlay.update_pointer(1.0, (10, 10))
        self.assertEqual(seen, ['medium'])
        self.assertEqual(overlay.bot_ack_serial, 2)
        self.assertEqual(overlay.bot_ack_mode, 'medium')
        self.assertEqual(overlay.bot.scores, {'human': 0, 'bot': 0})
        overlay.bot.update.assert_called_once_with(overlay.table, 1.0)


class AimVisibilityTests(unittest.TestCase):
    def make_overlay(self):
        overlay = object.__new__(BilliardsOverlay)
        overlay.window_mode = False
        overlay.desktop = SimpleNamespace(get_geometry=Mock(return_value=(0, 0, 1920, 1080)))
        overlay.geometry = (0, 0, 1920, 1080)
        overlay.monitor_state = ()
        overlay.monitor_checked = 1.0
        overlay.resize_request = None
        overlay.want_hidden = overlay.hidden = False
        overlay.want_felt = True
        overlay.felt_visible = False
        overlay.transition = None
        overlay.restart_requested = Mock()
        overlay.restart_requested.is_set.return_value = False
        overlay.table_windows = {}
        overlay.table = SimpleNamespace(step=Mock())
        overlay.pointer = SimpleNamespace()
        overlay.on_desktop = False
        overlay.update_pointer = Mock()
        overlay.draw_balls = Mock()
        overlay.draw_table = Mock()
        overlay.draw_guide = Mock()
        overlay.draw_charge = Mock()
        overlay.draw_round_actions = Mock()
        return overlay

    def test_shift_without_desktop_focus_never_reveals_the_aim(self):
        overlay = self.make_overlay()
        with patch('dib.overlay.win32api.GetAsyncKeyState', return_value=0x8000), \
                patch('dib.overlay.win32gui.GetForegroundWindow', return_value=99), \
                patch('dib.overlay.win32process.GetWindowThreadProcessId', return_value=(0, 4321)), \
                patch('dib.overlay.os.getpid', return_value=1234), \
                patch('dib.overlay.win32gui.GetClassName', return_value='OtherApp'):
            self.assertFalse(overlay.keyboard_focus_valid())
            overlay.draw(1.0, 0.016, (100, 100))
            self.assertFalse(overlay.aim_visible)
            # Even a stale on_desktop flag cannot override missing keyboard focus.
            overlay.on_desktop = True
            overlay.draw(1.0, 0.016, (100, 100))
            self.assertFalse(overlay.aim_visible)
            overlay.on_desktop = False
            overlay.felt_visible = True
            overlay.draw(1.0, 0.016, (100, 100))
            self.assertTrue(overlay.aim_visible)

    def test_shift_on_desktop_reveals_the_aim(self):
        overlay = self.make_overlay()
        overlay.on_desktop = True
        with patch('dib.overlay.win32api.GetAsyncKeyState', return_value=0x8000), \
                patch.object(overlay, 'keyboard_focus_valid', return_value=True):
            overlay.draw(1.0, 0.016, (100, 100))
        self.assertTrue(overlay.aim_visible)


class TableVisibilityTests(unittest.TestCase):
    def test_surface_toggle_switches_between_round_pockets_and_recycle_bins(self):
        overlay = object.__new__(BilliardsOverlay)
        overlay.table_windows = {'rail': 11, 'pockets': 12}
        with patch('dib.overlay.win32gui.ShowWindow') as show, \
                patch.object(overlay, 'position_layer'):
            overlay.set_felt(False)
            self.assertFalse(overlay.felt_visible)
            overlay.set_felt(True)
            self.assertTrue(overlay.felt_visible)
        self.assertEqual([call.args for call in show.call_args_list],
                         [(11, 0), (12, 4), (11, 4), (12, 0)])


if __name__ == '__main__':
    unittest.main()
