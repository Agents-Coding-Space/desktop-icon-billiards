"""GUI-free tests for the immutable shot report pipeline."""
import json
import queue
import random
import re
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from dib import config
from dib.bot import BotController
from dib.commentary import (
    _COMMENTARY_CACHE,
    COMMENTARY_ASSET_NAME,
    COOLDOWN_SECONDS,
    NORMAL_CHANCE,
    NORMAL_GROUPS,
    NORMAL_TURN_THRESHOLD,
    PERSONA_BY_MODE,
    URGENT_GROUPS,
    CommentaryEngine,
    ShotReport,
    classify_event,
    load_commentary_lines,
)
from dib.overlay import BilliardsOverlay, TablePointer
from dib.table import BilliardsMotion

WORK_AREA = (0, 0, 1600, 1000)
ICONS = [(64, 64)] * 15

REPORT_FIELDS = (
    'shooter', 'pocketed', 'scratched', 'hit_any_ball', 'rail_hits_before_pot',
    'is_combo', 'power', 'aim_seconds', 'shooter_streak', 'miss_streak',
    'human_score', 'bot_score', 'remaining_balls', 'bot_mode',
)


def make_table():
    return BilliardsMotion(WORK_AREA, list(ICONS))


def make_bot(mode='easy', rng=None):
    bot = BotController(rng or random.Random(1))
    bot.mode = mode
    return bot


def drain(pending):
    out = []
    while True:
        try:
            out.append(pending.get_nowait())
        except queue.Empty:
            return out


def make_report(**overrides):
    values = dict(
        shooter='human', pocketed=0, scratched=False, hit_any_ball=True,
        rail_hits_before_pot=0, is_combo=False, power=0.5, aim_seconds=1.0,
        shooter_streak=0, miss_streak=0, human_score=0, bot_score=0,
        remaining_balls=10, bot_mode='easy',
    )
    values.update(overrides)
    return ShotReport(**values)


ALL_GROUPS = (*URGENT_GROUPS, *NORMAL_GROUPS)
GROUP_LINES = {group: [f'{group}-line'] for group in ALL_GROUPS}


def make_engine(lines=None, *, rng=1, **kwargs):
    return CommentaryEngine(GROUP_LINES if lines is None else lines, rng=rng, **kwargs)


def start_shot(table, bot, aim=0.0, power=0.7):
    table.shoot(power, table.clock)
    bot.note_shot_input(aim, power)
    bot.sync(table, table.clock)


def settle(table, bot, now, *, sunk=(), white_sunk=False, finished=False):
    table.sunk.update(sunk)
    table.white_sunk = white_sunk
    table.state = 'finished' if finished else 'ready'
    bot.sync(table, now)


class ShotReportShapeTests(unittest.TestCase):
    def test_dataclass_is_frozen_with_the_locked_field_order(self):
        self.assertEqual(tuple(field.name for field in fields(ShotReport)), REPORT_FIELDS)
        report = ShotReport('human', 0, False, False, 0, False, 0.5, 1.0,
                            0, 0, 0, 0, 15, 'easy')
        with self.assertRaises(FrozenInstanceError):
            report.pocketed = 1


class BotReportTests(unittest.TestCase):
    def test_one_report_per_shot_even_with_repeated_settled_syncs(self):
        table, bot = make_table(), make_bot()
        reports = queue.SimpleQueue()
        bot.report_sink = reports.put
        for _ in range(2):
            bot.turn = 'human'
            start_shot(table, bot, aim=1.5, power=0.8)
            settle(table, bot, 1.0)
            settle(table, bot, 1.1)
            settle(table, bot, 1.2)
        emitted = drain(reports)
        self.assertEqual(len(emitted), 2)
        self.assertEqual([report.shooter for report in emitted], ['human', 'human'])
        self.assertEqual([report.power for report in emitted], [0.8, 0.8])

    def test_scratch_report_keeps_scratch_after_the_cue_is_respotted(self):
        table, bot = make_table(), make_bot()
        reports = queue.SimpleQueue()
        bot.report_sink = reports.put
        start_shot(table, bot)
        settle(table, bot, 1.0, sunk=(0, 2), white_sunk=True)
        table.sunk.discard(0)
        table.white_sunk = False
        table.state = 'ready'
        bot.sync(table, 1.2)
        report = drain(reports)[0]
        self.assertTrue(report.scratched)
        self.assertEqual(report.pocketed, 1)
        self.assertEqual(report.remaining_balls, 14)

    def test_scratch_emits_at_return_white_without_a_duplicate_at_ready(self):
        table, bot = make_table(), make_bot()
        reports = queue.SimpleQueue()
        bot.report_sink = reports.put
        start_shot(table, bot)
        table.white_sunk = True
        table.state = 'return_white'
        bot.sync(table, 1.0)
        emitted = drain(reports)
        self.assertEqual(len(emitted), 1)
        self.assertTrue(emitted[0].scratched)
        table.state = 'ball_in_hand'
        bot.sync(table, 1.1)
        table.state = 'ready'
        bot.sync(table, 1.2)
        self.assertEqual(drain(reports), [])

    def test_final_shot_emits_from_the_finished_state(self):
        table, bot = make_table(), make_bot()
        reports = queue.SimpleQueue()
        bot.report_sink = reports.put
        start_shot(table, bot)
        settle(table, bot, 2.0, sunk=range(1, 16), finished=True)
        report = drain(reports)[0]
        self.assertEqual((report.pocketed, report.remaining_balls), (15, 0))
        self.assertEqual((report.human_score, report.bot_score), (15, 0))
        settle(table, bot, 2.2, finished=True)
        self.assertEqual(len(drain(reports)), 0)

    def test_airball_and_rail_telemetry_map_without_a_pot(self):
        table, bot = make_table(), make_bot()
        reports = queue.SimpleQueue()
        bot.report_sink = reports.put
        start_shot(table, bot)
        table.hit_any_ball = False
        table.first_target_hit = None
        table.rail_hits = 3
        table.rails_before_first_pot = None
        settle(table, bot, 1.0)
        report = drain(reports)[0]
        self.assertEqual((report.hit_any_ball, report.rail_hits_before_pot), (False, 3))
        self.assertFalse(report.is_combo)

    def test_combo_and_rails_before_first_pot_map_from_u2_fields(self):
        table, bot = make_table(), make_bot()
        reports = queue.SimpleQueue()
        bot.report_sink = reports.put
        start_shot(table, bot)
        table.hit_any_ball = True
        table.first_target_hit = 1
        table.pocket_order = [2]
        table.rail_hits = 5
        table.rails_before_first_pot = 2
        settle(table, bot, 1.0, sunk=(2,))
        report = drain(reports)[0]
        self.assertTrue(report.is_combo)
        self.assertEqual(report.rail_hits_before_pot, 2)

    def test_direct_pot_is_not_a_combo(self):
        table, bot = make_table(), make_bot()
        reports = queue.SimpleQueue()
        bot.report_sink = reports.put
        start_shot(table, bot)
        table.first_target_hit = 1
        table.pocket_order = [1]
        settle(table, bot, 1.0, sunk=(1,))
        self.assertFalse(drain(reports)[0].is_combo)

    def test_streaks_follow_consecutive_report_outcomes(self):
        table, bot = make_table(), make_bot()
        reports = queue.SimpleQueue()
        bot.report_sink = reports.put
        outcomes = [(1,), (2,), (), (3,)]
        for index, sunk in enumerate(outcomes, 1):
            bot.turn = 'human'
            start_shot(table, bot)
            settle(table, bot, index * 2.0, sunk=sunk)
        emitted = drain(reports)
        self.assertEqual([report.shooter_streak for report in emitted], [1, 2, 0, 1])
        self.assertEqual([report.miss_streak for report in emitted], [0, 0, 1, 0])

    def test_bot_shot_uses_its_configured_decision_delay_as_aim_seconds(self):
        table, bot = make_table(), make_bot(mode='easy', rng=random.Random(4))
        reports = queue.SimpleQueue()
        bot.report_sink = reports.put
        bot.sync(table, 0.0)
        bot.turn = 'bot'
        self.assertFalse(bot.update(table, 0.0))
        self.assertGreater(bot._next_action_aim, 0.0)
        self.assertTrue(bot.update(table, 5.0))
        settle(table, bot, 6.0)
        report = drain(reports)[0]
        self.assertEqual(report.shooter, 'bot')
        self.assertEqual(report.aim_seconds, bot._next_action_aim)
        self.assertEqual(report.bot_mode, 'easy')

    def test_report_holds_values_only(self):
        table, bot = make_table(), make_bot()
        reports = queue.SimpleQueue()
        bot.report_sink = reports.put
        start_shot(table, bot)
        settle(table, bot, 1.0)
        report = drain(reports)[0]
        self.assertFalse(any(value is table for value in vars(report).values()))
        table.hit_any_ball = True
        table.rail_hits = 99
        table.pocket_order = [7]
        self.assertEqual((report.hit_any_ball, report.rail_hits_before_pot, report.is_combo),
                         (False, 0, False))

    def test_new_round_purges_queued_reports(self):
        table, bot = make_table(), make_bot()
        reports = queue.SimpleQueue()
        bot.report_sink = reports.put
        overlay = object.__new__(BilliardsOverlay)
        overlay.shot_reports = reports
        overlay.bot = bot
        bot.report_reset_sink = overlay.reset_shot_reports
        start_shot(table, bot)
        settle(table, bot, 1.0)
        self.assertEqual(len(drain(reports)), 1)
        start_shot(table, bot)
        table.reset()
        bot.sync(table, 2.0)
        self.assertEqual(overlay.drain_shot_reports(), [])
        self.assertEqual((bot._shooter_streak, bot._miss_streak), (0, 0))


class OverlayQueueTests(unittest.TestCase):
    def make_overlay(self):
        overlay = object.__new__(BilliardsOverlay)
        overlay.window_mode = False
        overlay.parent = 1
        overlay.titlebar_height = 0
        overlay.table = make_table()
        overlay.bot = make_bot(mode='off')
        overlay.shot_reports = queue.SimpleQueue()
        overlay.bot.report_sink = overlay.shot_reports.put
        overlay.bot.report_reset_sink = overlay.reset_shot_reports
        overlay.pointer = TablePointer()
        overlay.credit_links = []
        overlay.felt_visible = True
        overlay.credit_was_down = True
        overlay.credit_pressed = None
        overlay.last_blocked = overlay.blocked_by = None
        overlay.round_action_hover = None
        overlay.round_action_pressed = None
        overlay.round_action_was_down = True
        overlay.bot_commands = queue.SimpleQueue()
        overlay.bot_command_serial = overlay.bot_ack_serial = 0
        overlay.bot_ack_mode = 'off'
        return overlay

    def test_pointer_aim_seconds_reach_the_report(self):
        overlay = self.make_overlay()
        table, bot = overlay.table, overlay.bot
        x, y = 900, 500
        with mock.patch('dib.overlay.win32gui.ScreenToClient', return_value=(x, y)), \
                mock.patch('dib.overlay.win32gui.WindowFromPoint', return_value=1), \
                mock.patch('dib.overlay.win32gui.GetAncestor', return_value=1), \
                mock.patch('dib.overlay.win32gui.GetClassName', return_value='Progman'), \
                mock.patch('dib.overlay.win32api.GetSystemMetrics', return_value=0), \
                mock.patch('dib.overlay.win32api.GetAsyncKeyState', side_effect=(0, 0x8000, 0)):
            overlay.update_pointer(9.5, (x, y))
            overlay.update_pointer(10.0, (x, y))
            overlay.update_pointer(12.5, (x, y))
        self.assertAlmostEqual(overlay.pointer.aim_seconds, 2.5)
        self.assertEqual(table.shots, 1)
        bot.sync(table, 12.6)
        table.state = 'ready'
        bot.sync(table, 12.7)
        emitted = overlay.drain_shot_reports()
        self.assertEqual(len(emitted), 1)
        self.assertAlmostEqual(emitted[0].aim_seconds, 2.5)
        self.assertAlmostEqual(emitted[0].power, overlay.pointer.shot_power)

    def test_drain_preserves_order_and_reset_clears_queue(self):
        overlay = self.make_overlay()
        reports = overlay.shot_reports
        first = ShotReport('human', 1, False, True, 0, False, .5, 1., 1, 0, 1, 0, 14, 'easy')
        second = ShotReport('bot', 0, False, False, 2, False, .4, .3, 0, 1, 1, 0, 14, 'easy')
        reports.put(first)
        reports.put(second)
        self.assertEqual(overlay.drain_shot_reports(), [first, second])
        reports.put(first)
        overlay.bot._shooter_streak = 3
        overlay.reset_shot_reports()
        self.assertEqual(overlay.drain_shot_reports(), [])
        self.assertEqual(overlay.bot._shooter_streak, 0)

    def test_bot_mode_change_clears_stale_reports(self):
        overlay = self.make_overlay()
        overlay.shot_reports.put(ShotReport('human', 0, False, False, 0, False, 0., 0.,
                                            0, 0, 0, 0, 15, 'off'))
        self.assertTrue(overlay.consume_bot_commands() is False)
        overlay.request_bot_cycle()
        self.assertTrue(overlay.consume_bot_commands())
        self.assertEqual(overlay.drain_shot_reports(), [])

    def test_restart_discards_queued_reports_and_report_state(self):
        overlay = self.make_overlay()
        overlay.desktop = SimpleNamespace(get_geometry=mock.Mock(return_value=WORK_AREA))
        overlay.geometry = WORK_AREA
        overlay.monitor_state = ()
        overlay.monitor_checked = 10.0
        overlay.resize_request = None
        overlay.want_hidden = overlay.hidden = False
        overlay.want_felt = False
        overlay.table_windows = {}
        overlay.transition = None
        overlay.items = []
        overlay.restart_requested = mock.Mock()
        overlay.restart_requested.is_set.return_value = True
        overlay.table.reset = mock.Mock()
        overlay.table.step = mock.Mock()
        overlay.update_pointer = mock.Mock()
        for name in ('draw_balls', 'draw_table', 'draw_guide', 'draw_charge', 'draw_round_actions'):
            setattr(overlay, name, mock.Mock())
        overlay.shot_reports.put(make_report(pocketed=1))
        overlay.bot._shooter_streak = 3
        with mock.patch('dib.overlay.win32api.GetAsyncKeyState', return_value=0):
            overlay.draw(10.0, 0.016, (100, 100))
        overlay.restart_requested.clear.assert_called_once_with()
        overlay.table.reset.assert_called_once_with()
        self.assertEqual(overlay.drain_shot_reports(), [])
        self.assertEqual((overlay.bot._shooter_streak, overlay.bot._miss_streak), (0, 0))


class Step2PipelineTests(unittest.TestCase):
    """Drive settled telemetry through the queue, engine and the shipped bank."""

    def test_telemetry_to_queue_to_real_asset_line_in_one_pipeline(self):
        table, bot = make_table(), make_bot(mode='easy')
        pending = queue.SimpleQueue()
        bot.report_sink = pending.put
        bank = load_commentary_lines(use_cache=False)
        engine = CommentaryEngine(bank, rng=random.Random(1))

        start_shot(table, bot, aim=1.2, power=0.8)
        table.hit_any_ball = True
        table.first_target_hit = 2
        table.pocket_order = [2, 3]
        table.rail_hits = 4
        table.rails_before_first_pot = 1
        settle(table, bot, 1.0, sunk=(2, 3))
        settle(table, bot, 1.1, sunk=(2, 3))

        start_shot(table, bot, aim=13.0, power=0.6)
        table.hit_any_ball = False
        table.first_target_hit = None
        settle(table, bot, 2.0)
        settle(table, bot, 2.1)

        emitted = drain(pending)
        self.assertEqual(len(emitted), 2)
        multi, airball = emitted
        self.assertEqual((multi.pocketed, multi.is_combo, multi.rail_hits_before_pot), (2, False, 1))
        self.assertEqual((multi.human_score, multi.bot_score, multi.remaining_balls), (2, 0, 13))
        self.assertEqual((multi.shooter_streak, multi.miss_streak, multi.bot_mode), (1, 0, 'easy'))
        self.assertAlmostEqual(multi.aim_seconds, 1.2)
        self.assertAlmostEqual(multi.power, 0.8)
        self.assertEqual((airball.pocketed, airball.hit_any_ball, airball.miss_streak), (0, False, 1))
        self.assertTrue(all(not isinstance(value, (list, dict, set, tuple))
                            for value in vars(multi).values()))
        table.rail_hits = 99
        table.pocket_order = [7]
        bot.scores['human'] = 99
        self.assertEqual((multi.rail_hits_before_pot, multi.is_combo, multi.human_score),
                         (1, False, 2))

        self.assertEqual(engine.classify(multi).group, 'multi_pot')
        self.assertIn(engine.comment(multi, 0.0), bank['multi_pot']['easy'])
        self.assertEqual(engine.classify(airball).group, 'airball')
        self.assertIsNone(engine.comment(airball, 0.0))
        self.assertIn(engine.comment(airball, COOLDOWN_SECONDS), bank['airball']['easy'])

    def test_report_scores_and_streaks_follow_alternating_shooters(self):
        table, bot = make_table(), make_bot(mode='easy')
        pending = queue.SimpleQueue()
        bot.report_sink = pending.put
        for index, (sunk, hit) in enumerate((((1,), True), ((), False), ((2,), True), ((), False))):
            start_shot(table, bot)
            table.hit_any_ball = hit
            table.first_target_hit = 1 if hit else None
            settle(table, bot, 2.0 * (index + 1), sunk=sunk)

        emitted = drain(pending)
        self.assertEqual(len(emitted), 4)
        actual = [(report.shooter, report.human_score, report.bot_score,
                   report.shooter_streak, report.miss_streak) for report in emitted]
        self.assertEqual(actual, [
            ('human', 1, 0, 1, 0),
            ('human', 1, 0, 0, 1),
            ('bot', 1, 1, 1, 0),
            ('bot', 1, 1, 0, 1),
        ])

    def test_silenced_shots_advance_turn_counter_and_score_memory(self):
        normal = make_report(pocketed=1)
        engine = make_engine(rng=random.Random(1))
        engine.comment(normal, 0.0, hidden=True)
        engine.comment(normal, 0.0, enabled=False)
        self.assertEqual(engine.turns_since_last_comment, 2)
        self.assertEqual(engine.comment(normal, 0.0), 'single_pot-line')

        engine = make_engine()
        engine.comment(make_report(human_score=3, bot_score=1), 0.0, disabled=True)
        flipped = make_report(human_score=2, bot_score=4)
        self.assertEqual(engine.classify(flipped).group, 'lead_change')
        self.assertEqual(engine.comment(flipped, 0.0), 'lead_change-line')

    def test_normal_probability_threshold_and_constructor_clamps(self):
        report = make_report(pocketed=1)
        self.assertEqual((NORMAL_CHANCE, NORMAL_TURN_THRESHOLD), (0.65, 2))
        for draw, expected in ((0.64, 'single_pot-line'), (0.65, None)):
            with self.subTest(draw=draw):
                engine = make_engine(rng=random.Random(1))
                engine._rng.random = mock.Mock(return_value=draw)
                self.assertIsNone(engine.comment(report, 0.0))
                self.assertEqual(engine.comment(report, 0.0), expected)

        strict = CommentaryEngine(GROUP_LINES, rng=1, cooldown=0, normal_chance=2, normal_turns=0)
        self.assertEqual((strict.cooldown, strict.normal_chance, strict.normal_turns),
                         (COOLDOWN_SECONDS, 1.0, 1))
        self.assertEqual(strict.comment(report, 0.0), 'single_pot-line')

    def test_malformed_provider_entries_do_not_raise(self):
        report = make_report(scratched=True)
        lines = {'scratch': {
            'easy': 'flat-string',
            'medium': 42,
            'hard': {'nested': 'ignored'},
            'extreme': (None, 7, ' ', 'extreme-line'),
        }}
        engine = CommentaryEngine(lines, rng=1)
        self.assertEqual(engine.comment(report, 0.0, bot_mode='easy'), 'flat-string')
        self.assertIsNone(engine.comment(report, COOLDOWN_SECONDS, bot_mode='medium'))
        self.assertIsNone(engine.comment(report, 2 * COOLDOWN_SECONDS, bot_mode='hard'))
        self.assertEqual(engine.comment(report, 3 * COOLDOWN_SECONDS, bot_mode='extreme'),
                         'extreme-line')
        self.assertIsNone(CommentaryEngine(['not', 'a', 'mapping'], rng=1).comment(report, 0.0))


class CommentaryEngineTests(unittest.TestCase):
    def test_each_urgent_condition_bypasses_the_turn_threshold(self):
        cases = {
            'scratch': make_report(scratched=True),
            'airball': make_report(hit_any_ball=False),
            'multi_pot': make_report(pocketed=2),
            'overthink': make_report(aim_seconds=12.1),
            'shooter_streak': make_report(shooter_streak=3),
            'miss_streak': make_report(miss_streak=3),
            'endgame': make_report(remaining_balls=2),
            'finished': make_report(remaining_balls=0, pocketed=15),
        }
        for group, report in cases.items():
            with self.subTest(group=group):
                event = classify_event(report)
                self.assertTrue(event.urgent)
                self.assertEqual((event.group, event.kind), (group, 'urgent'))
                self.assertEqual(make_engine().comment(report, 0.0), f'{group}-line')

    def test_lead_change_is_urgent_only_after_an_opposite_score_order(self):
        engine = make_engine()
        first = make_report(human_score=3, bot_score=2)
        second = make_report(human_score=3, bot_score=4)
        self.assertIsNone(engine.comment(first, 0.0))
        self.assertEqual(classify_event(first).group, 'ordinary_miss')
        self.assertEqual(engine.classify(second).group, 'lead_change')
        self.assertEqual(engine.comment(second, 0.0), 'lead_change-line')
        self.assertFalse(classify_event(make_report(human_score=3, bot_score=4),
                                        0).urgent)

    def test_normal_line_waits_two_turns_and_is_probabilistic(self):
        report = make_report(pocketed=1)
        for seed, expected in ((1, 'single_pot-line'), (2, None)):
            with self.subTest(seed=seed):
                engine = make_engine(rng=random.Random(seed))
                self.assertIsNone(engine.comment(report, 0.0))
                self.assertEqual(engine.turns_since_last_comment, 1)
                self.assertEqual(engine.comment(report, 0.0), expected)
                self.assertEqual(engine.turns_since_last_comment, 0 if expected else 2)

    def test_normal_miss_uses_the_other_group(self):
        engine = make_engine(rng=random.Random(1))
        self.assertIsNone(engine.comment(make_report(), 0.0))
        self.assertEqual(engine.comment(make_report(), 0.0), 'ordinary_miss-line')

    def test_cooldown_blocks_all_events_including_urgent(self):
        engine = make_engine()
        report = make_report(scratched=True)
        self.assertEqual(engine.comment(report, 0.0), 'scratch-line')
        self.assertIsNone(engine.comment(report, COOLDOWN_SECONDS - 0.01))
        self.assertEqual(engine.comment(report, COOLDOWN_SECONDS), 'scratch-line')

        short = make_engine(cooldown=0.5)
        self.assertEqual(short.comment(report, 0.0), 'scratch-line')
        self.assertIsNone(short.comment(report, 1.0))  # the 3.5 s floor holds

    def test_shuffle_bag_exhausts_before_repeating_and_drops_the_last_line(self):
        engine = make_engine({'scratch': {'easy': ['a', 'b', 'c']}}, rng=random.Random(7))
        report = make_report(scratched=True)
        first_cycle = [engine.comment(report, index * COOLDOWN_SECONDS) for index in range(3)]
        self.assertEqual(set(first_cycle), {'a', 'b', 'c'})
        fourth = engine.comment(report, 3 * COOLDOWN_SECONDS)
        self.assertIn(fourth, {'a', 'b', 'c'})
        self.assertNotEqual(fourth, first_cycle[-1])

    def test_bags_are_independent_per_group_and_persona(self):
        lines = {
            'scratch': {'easy': ['s1', 's2'], 'hard': ['h1', 'h2']},
            'airball': {'easy': ['a1', 'a2']},
        }
        engine = make_engine(lines, rng=random.Random(3))
        reports = (
            make_report(scratched=True),
            make_report(hit_any_ball=False),
            make_report(scratched=True),
            make_report(hit_any_ball=False),
        )
        drawn = [engine.comment(report, index * COOLDOWN_SECONDS)
                 for index, report in enumerate(reports)]
        self.assertEqual((drawn[0], drawn[2]), ('s1', 's2'))
        self.assertEqual((drawn[1], drawn[3]), ('a1', 'a2'))
        hard_line = engine.comment(make_report(scratched=True, bot_mode='hard'),
                                   4 * COOLDOWN_SECONDS)
        self.assertIn(hard_line, ('h1', 'h2'))

    def test_bot_mode_selects_the_documented_persona(self):
        self.assertEqual(PERSONA_BY_MODE, {
            'easy': 'easy', 'medium': 'medium', 'hard': 'hard', 'extreme': 'extreme',
        })
        lines = {
            'scratch': {
                'easy': ['e1', 'e2'], 'medium': ['m1', 'm2'],
                'hard': ['h1', 'h2'], 'extreme': ['x1', 'x2'],
            },
        }
        engine = make_engine(lines, rng=random.Random(5))
        expected = {'easy': ('e1', 'e2'), 'medium': ('m1', 'm2'),
                    'hard': ('h1', 'h2'), 'extreme': ('x1', 'x2')}
        for index, (mode, options) in enumerate(expected.items()):
            with self.subTest(mode=mode):
                report = make_report(scratched=True, bot_mode=mode)
                self.assertIn(engine.comment(report, index * COOLDOWN_SECONDS), options)

    def test_silence_flags_suppress_output_without_consuming_cooldown(self):
        report = make_report(scratched=True)
        cases = (
            {'enabled': False}, {'hidden': True}, {'disabled': True},
            {'bot_on': False}, {'bot_mode': 'off'},
        )
        for flags in cases:
            with self.subTest(flags=flags):
                engine = make_engine()
                self.assertIsNone(engine.comment(report, 0.0, **flags))
        engine = make_engine()
        self.assertIsNone(engine.comment(report, 0.0, hidden=True))
        self.assertEqual(engine.comment(report, 0.0), 'scratch-line')

    def test_missing_group_persona_and_empty_lists_never_raise(self):
        report = make_report(scratched=True)
        for lines in ({}, {'miss': {'easy': ['x']}}, {'scratch': {}},
                      {'scratch': {'easy': []}}, {'scratch': {'hard': ['x']}}):
            with self.subTest(lines=lines):
                engine = CommentaryEngine(lines, rng=1)
                self.assertIsNone(engine.comment(report, 0.0))
        engine = CommentaryEngine({}, rng=1, fallback_line='Nice shot.')
        self.assertEqual(engine.comment(report, 0.0), 'Nice shot.')

    def test_broad_group_and_flat_group_provider_shapes_are_accepted(self):
        report = make_report(scratched=True)
        engine = CommentaryEngine({'urgent': {'easy': ['broad']}}, rng=1)
        self.assertEqual(engine.comment(report, 0.0), 'broad')
        flat = CommentaryEngine({'scratch': ['flat']}, rng=1)
        self.assertEqual(flat.comment(report, 0.0), 'flat')

    def test_reset_clears_cooldown_bags_turn_counter_and_dedupe(self):
        engine = make_engine(rng=random.Random(11))
        urgent = make_report(scratched=True)
        self.assertEqual(engine.comment(urgent, 10.0), 'scratch-line')
        self.assertIsNone(engine.comment(urgent, 11.0))
        engine.reset()
        self.assertEqual(engine._bags, {})
        self.assertEqual(engine.comment(urgent, 10.0), 'scratch-line')

        paced = make_engine(rng=random.Random(1))
        normal = make_report(pocketed=1)
        self.assertIsNone(paced.comment(normal, 0.0))
        self.assertEqual(paced.comment(normal, 0.0), 'single_pot-line')
        paced.reset()
        self.assertEqual(paced.turns_since_last_comment, 0)
        self.assertIsNone(paced.comment(normal, 100.0))


class CommentaryAssetTests(unittest.TestCase):
    """Check the Vietnamese bank on disk and its GUI-free loader."""

    asset_path = config.ASSETS / COMMENTARY_ASSET_NAME
    personas = tuple(PERSONA_BY_MODE.values())

    def read_bank(self):
        return json.loads(self.asset_path.read_text(encoding='utf-8'))

    def test_json_is_utf8_with_the_required_groups_and_personas(self):
        payload = self.read_bank()
        self.assertIsInstance(payload, dict)
        missing_groups = set(ALL_GROUPS) - set(payload)
        self.assertEqual(missing_groups, set(), f'missing groups: {sorted(missing_groups)}')
        for group in ALL_GROUPS:
            with self.subTest(group=group):
                node = payload[group]
                self.assertIsInstance(node, dict)
                missing_personas = set(self.personas) - set(node)
                self.assertEqual(missing_personas, set(),
                                 f'{group} missing personas: {sorted(missing_personas)}')
                for persona in self.personas:
                    lines = node[persona]
                    self.assertIsInstance(lines, list)
                    self.assertTrue(lines, f'{group}/{persona} is empty')
                    self.assertTrue(all(isinstance(line, str) and line.strip() for line in lines))
                    self.assertEqual(len(lines), len(set(lines)),
                                     f'duplicate line in {group}/{persona}')
                    for line in lines:
                        sentences = [part for part in re.split(r'[.!?]+', line) if part.strip()]
                        self.assertLessEqual(len(sentences), 2,
                                             f'line exceeds two sentences in {group}/{persona}: {line!r}')

    def test_line_counts_are_measured_not_assumed(self):
        payload = self.read_bank()
        counts = {group: {persona: len(payload[group][persona]) for persona in self.personas}
                  for group in ALL_GROUPS}
        total = sum(count for by_persona in counts.values() for count in by_persona.values())
        for group, by_persona in counts.items():
            for persona, count in by_persona.items():
                self.assertGreaterEqual(count, 2, f'{group}/{persona}: {count} lines; counts={counts}')
        self.assertGreaterEqual(total, 80, f'measured total={total}; counts={counts}')
        self.assertLessEqual(total, 120, f'measured total={total}; counts={counts}')

    def test_loader_reads_the_real_asset(self):
        bank = load_commentary_lines(use_cache=False)
        self.assertIsInstance(bank, dict)
        self.assertEqual(bank, self.read_bank())

    def test_loader_from_config_assets_returns_the_118_line_bank(self):
        bank = load_commentary_lines(use_cache=False)
        total = sum(len(lines) for personas in bank.values() for lines in personas.values())
        self.assertEqual(config.ASSETS / COMMENTARY_ASSET_NAME, self.asset_path)
        self.assertEqual(total, 118)

    def test_loader_returns_empty_mapping_for_missing_or_malformed_files(self):
        with TemporaryDirectory() as folder:
            missing = Path(folder) / 'missing.json'
            self.assertEqual(load_commentary_lines(missing, use_cache=False), {})
            broken = Path(folder) / 'broken.json'
            broken.write_text('{"finished": ', encoding='utf-8')
            self.assertEqual(load_commentary_lines(broken, use_cache=False), {})
            wrong_shape = Path(folder) / 'wrong.json'
            wrong_shape.write_text('["not", "a", "bank"]', encoding='utf-8')
            self.assertEqual(load_commentary_lines(wrong_shape, use_cache=False), {})
            bad_lines = Path(folder) / 'bad-lines.json'
            bad_lines.write_text('{"scratch": {"easy": ["ok", 7, "  "]}}', encoding='utf-8')
            self.assertEqual(load_commentary_lines(bad_lines, use_cache=False),
                             {'scratch': {'easy': ['ok']}})

    def test_loader_cache_is_per_path_and_can_be_bypassed(self):
        with TemporaryDirectory() as folder:
            target = Path(folder) / 'bank.json'
            target.write_text('{"scratch": {"easy": ["cached"]}}', encoding='utf-8')
            try:
                first = load_commentary_lines(target)
                self.assertEqual(first, {'scratch': {'easy': ['cached']}})
                first['scratch']['easy'].append('mutated')
                self.assertEqual(load_commentary_lines(target), {'scratch': {'easy': ['cached']}})
                target.write_text('{"scratch": {"easy": ["fresh"]}}', encoding='utf-8')
                self.assertEqual(load_commentary_lines(target, use_cache=False),
                                 {'scratch': {'easy': ['fresh']}})
            finally:
                _COMMENTARY_CACHE.pop(str(target), None)


if __name__ == '__main__':
    unittest.main()
