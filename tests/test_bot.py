"""Behaviour tests for the hot-seat solo bot."""
import math
import queue
import random
import threading
import unittest
from types import SimpleNamespace

from dib.bot import MODES, SETTINGS, BotController
from dib.overlay import BilliardsOverlay
from dib.table import BALL_IMPULSE, ROLLING_DRAG, BilliardsMotion

WORK_AREA = (0, 0, 1600, 1000)
ICONS = [(64, 64)] * 15


def make_table():
    return BilliardsMotion(WORK_AREA, list(ICONS))


class CycleTests(unittest.TestCase):
    def test_cycle_order(self):
        bot = BotController(random.Random(1))
        seen = [bot.mode]
        for _ in range(len(MODES)):
            seen.append(bot.cycle())
        self.assertEqual(seen, ['off', 'easy', 'medium', 'hard', 'extreme', 'off'])

    def test_disabled_bot_never_shoots_and_scores_nothing(self):
        table = make_table()
        bot = BotController(random.Random(1))
        for now in range(0, 6000, 16):
            self.assertFalse(bot.update(table, now / 1000))
        self.assertEqual(table.shots, 0)

    def test_status_and_result_text(self):
        bot = BotController(random.Random(1))
        self.assertEqual(bot.status_text(), 'Bot Off')
        bot.mode = 'hard'
        bot.scores = {'human': 3, 'bot': 7}
        self.assertIn('Bot Hard', bot.status_text())
        self.assertIn('3', bot.status_text())
        self.assertEqual(bot.result_text(), 'Bot wins 3-7')


class QueuedCommandTests(unittest.TestCase):
    def test_queued_cycles_apply_in_order_on_the_consumer(self):
        overlay = object.__new__(BilliardsOverlay)
        overlay.bot = BotController()
        overlay.bot.scores = {'human': 3, 'bot': 7}
        overlay.bot_commands = queue.SimpleQueue()
        overlay.bot_command_serial = overlay.bot_ack_serial = 0
        overlay.bot_ack_mode = 'off'
        overlay.request_bot_cycle()
        overlay.request_bot_cycle()
        self.assertEqual(overlay.bot.mode, 'off')
        overlay.consume_bot_commands()
        self.assertEqual(overlay.bot.mode, 'medium')
        self.assertEqual(overlay.bot.scores, {'human': 0, 'bot': 0})
        self.assertEqual(overlay.bot_ack_serial, 2)
        self.assertEqual(overlay.bot_ack_mode, 'medium')
        self.assertFalse(overlay.consume_bot_commands())


class SyncTests(unittest.TestCase):
    def test_human_shot_keeps_break_and_passes_turn(self):
        table = make_table()
        bot = BotController(random.Random(1))
        self.assertEqual(bot.turn, 'human')
        self.assertFalse(bot.bot_turn)
        table.aim_at(table.white.x + 1, table.white.y)
        bot.mode = 'extreme'
        table.shoot(0.8, 0.0)
        bot.sync(table, 0.0)
        table.state = 'ready'
        bot.sync(table, 0.0)
        self.assertEqual(bot.turn, 'bot')
        self.assertTrue(bot.bot_turn)

    def test_pocketed_icons_are_credited_to_the_shooter(self):
        table = make_table()
        bot = BotController(random.Random(1))
        bot.mode = 'extreme'
        table.shoot(0.9, 0.0)
        bot.sync(table, 0.0)
        self.assertEqual(bot.scores, {'human': 0, 'bot': 0})
        # Pretend this shot (taken by the human) sinks two icons.
        table.sunk.update({1, 2})
        bot.sync(table, 1.0)
        self.assertEqual(bot.scores['human'], 2)
        self.assertEqual(bot.scores['bot'], 0)
        self.assertEqual(bot.scores['human'], 2)

    def test_new_round_resets_state(self):
        table = make_table()
        bot = BotController(random.Random(1))
        bot.mode = 'easy'
        table.shoot(0.7, 0.0)
        bot.sync(table, 0.0)
        table.reset()
        bot.sync(table, 0.0)
        self.assertEqual((bot.turn, bot.scores), ('human', {'human': 0, 'bot': 0}))


class PlaythroughTests(unittest.TestCase):
    def test_extreme_bot_game_completes_and_scores_sum_to_icon_count(self):
        table = make_table()
        bot = BotController(random.Random(7))
        bot.mode = 'extreme'
        frame = 1 / 60
        now = 0.0
        for _ in range(30000):
            now += frame
            if table.state == 'ready' and bot.turn == 'human' and not table.break_shot:
                # Stand-in for the human player: a firm, well-aimed shot at the nearest icon.
                targets = [body for index, body in enumerate(table.icons, 1) if index not in table.sunk]
                if not targets:
                    break
                target = min(targets, key=lambda body: (body.x - table.white.x) ** 2 + (body.y - table.white.y) ** 2)
                table.aim_at(target.x, target.y)
                table.shoot(0.85, now)
            elif table.state == 'ready' and bot.turn == 'human' and table.break_shot:
                table.aim_at(table.white.x + 1, table.white.y + 0.02)
                table.shoot(0.9, now)
            bot.update(table, now)
            table.step(frame, now)
            if table.state == 'finished':
                break
        bot.sync(table, now)
        self.assertEqual(bot.scores['human'] + bot.scores['bot'], table.pocketed_icons)

    def test_extreme_bot_waits_and_shoots_on_its_turn(self):
        table = make_table()
        bot = BotController(random.Random(3))
        bot.mode = 'extreme'
        table.aim_at(table.white.x + 1, table.white.y)
        table.shoot(0.9, 0.0)  # human breaks
        table.state = 'ready'  # simulate the stroke rolling and stopping
        bot.sync(table, 0.0)
        self.assertTrue(bot.bot_turn)
        shots_before = table.shots
        fired = False
        now = 0.2
        for _ in range(900):
            now += 1 / 60
            table.step(1 / 60, now)
            if bot.update(table, now):
                fired = True
                break
        self.assertTrue(fired)
        self.assertEqual(table.shots, shots_before + 1)

class ShotPlanningTests(unittest.TestCase):
    def test_difficulty_knobs_are_monotonic(self):
        levels = ('easy', 'medium', 'hard', 'extreme')
        for key in ('aim_error', 'power_error'):
            values = [SETTINGS[level][key] for level in levels]
            self.assertEqual(values, sorted(values, reverse=True))
        delays = [max(SETTINGS[level]['delay']) for level in levels]
        self.assertEqual(delays, sorted(delays, reverse=True))
        self.assertEqual((SETTINGS['extreme']['aim_error'], SETTINGS['extreme']['power_error']), (0, 0))

    def test_shot_power_grows_with_distance_and_is_clamped(self):
        game = make_table()
        near = BotController.shot_power(game, 600, 600, 1.0)
        far = BotController.shot_power(game, 1200, 1200, 1.0)
        self.assertGreater(far, near)
        self.assertEqual(BotController.shot_power(game, 10000, 10000, 1.0), 1.0)
        self.assertGreaterEqual(near, 0.22)

    def test_shot_power_round_trip_matches_table_shot_speed(self):
        game = make_table()
        decel = game.height * ROLLING_DRAG
        for charge in (0.3, 0.55, 0.9):
            with self.subTest(charge=charge):
                speed = game.height * (.12 + 3.05 * charge) * 1.58
                cue_distance = (speed / 1.06) ** 2 / (2 * decel)
                self.assertAlmostEqual(
                    BotController.shot_power(game, cue_distance, 0.0, 0.0), charge, places=9)

    def test_post_collision_direction_uses_normalized_incoming_and_impulse(self):
        qx, qy, length = BotController._post_collision(3.0, 4.0, 1.0, 0.0)
        # Incoming unit (0.6, 0.8), projected on the (1, 0) contact normal.
        self.assertAlmostEqual(qx, 0.6 - BALL_IMPULSE * 0.6)
        self.assertAlmostEqual(qy, 0.8)
        self.assertAlmostEqual(length, math.hypot(qx, qy))
        self.assertAlmostEqual(BotController._post_collision(0., 0., 1., 0.)[2], 0.)

    def test_extreme_plans_a_direct_pocket_shot(self):
        game = make_table()
        game.sunk = {2}
        game.break_shot = False
        game.state = 'ready'
        pocket_x, pocket_y = game.pockets[1]
        target = game.icons[0]
        target.x, target.y = pocket_x, pocket_y + 100
        game.white.x, game.white.y = pocket_x, pocket_y + 300
        bot = BotController(random.Random(1))
        bot.mode = 'extreme'
        bot.sync(game, 0.0)  # initialise the round counters
        bot.turn = 'bot'
        plan = bot.plan_shot(game)
        self.assertIsNotNone(plan)
        self.assertLess(abs(plan[1][0] - pocket_x), 1.0)
        self.assertGreater(plan[1][1], target.y - 1.0)  # ghost behind the target
        self.assertFalse(bot.update(game, 0.0))  # first call only sets the delay
        self.assertTrue(bot.update(game, 1.0))
        self.assertEqual(game.state, 'stroke')
        self.assertTrue(0.22 <= game.charge <= 1.0)
        self.assertGreater(game.power, 0.0)


class BankShotTests(unittest.TestCase):
    def test_extreme_enables_banks_and_lower_levels_do_not(self):
        self.assertTrue(SETTINGS['extreme'].get('banks'))
        for level in ('easy', 'medium', 'hard'):
            self.assertFalse(SETTINGS[level].get('banks', False))

    def test_extreme_banks_into_the_pocket_when_no_direct_line_exists(self):
        game = BilliardsMotion(WORK_AREA, [(64, 64)] * 2)
        game.sunk = {2}  # only icon 1 is on the table
        game.break_shot = False
        game.state = 'ready'
        target = game.icons[0]
        # Deterministic layout: the direct lines are blocked, but a one-cushion
        # line into the top-middle pocket is clear.
        game.white.x, game.white.y = 870.4519272479546, 723.8849171369695
        target.x, target.y = 979.3015133595009, 251.04136598480218
        bot = BotController(random.Random(1))
        bot.mode = 'extreme'
        self.assertNotEqual(bot.plan_shot(game, banks=False)[1], (995.7795796403582, 317.26198914438646))
        plan = bot.plan_shot(game, banks=True)
        self.assertIsNotNone(plan)
        self.assertAlmostEqual(plan[1][0], 995.7795796403582, delta=1.0)
        self.assertAlmostEqual(plan[1][1], 317.26198914438646, delta=1.0)
        game.aim_at(*plan[1])
        game.shoot(plan[2], 0.0)
        now = 0.0
        for _ in range(700):
            now += 1 / 60
            game.step(1 / 60, now)
            if game.state not in ('rolling', 'stroke'):
                break
        self.assertIn(1, game.sunk)
        self.assertEqual(game.state, 'finished')

    def test_bank_power_pays_for_the_cushion_loss(self):
        game = make_table()
        direct = BotController.shot_power(game, 500, 800, 0.9)
        bank = BotController.shot_power(game, 500, 800 / (0.88 ** 2), 0.9)
        self.assertGreater(bank, direct)


class CrowdedTableTests(unittest.TestCase):
    def test_crowded_rack_still_yields_a_deliberate_shot(self):
        game = make_table()
        # Shuffle positions so the rack is congested and many lines are blocked.
        for index, ball in enumerate(game.bodies):
            ball.x = 280 + (index * 137) % 1000
            ball.y = 220 + (index * 91) % 480
        bot = BotController(random.Random(5))
        bot.mode = 'extreme'
        plan = bot.plan_shot(game, banks=True)
        self.assertIsNotNone(plan)
        self.assertGreaterEqual(len(plan), 3)
        self.assertTrue(0.22 <= plan[2] <= 1.0)

    def test_escape_shot_is_symmetric_left_and_right(self):
        game = make_table()
        game.sunk = set(range(3, 16))
        white = game.white
        left, top, right, bottom = game.bounds
        cx, cy = (left + right) / 2, (top + bottom) / 2
        first, second = game.icons[:2]
        white.x, white.y = cx, cy
        first.x, first.y = cx - 200, cy
        second.x, second.y = cx + 200, cy
        shot = BotController._escape_shot(game)
        self.assertIsNotNone(shot)
        # Mirror the rack (including its iteration order) around the cue ball.
        first.x, second.x = cx + 200, cx - 200
        mirrored = BotController._escape_shot(game)
        self.assertIsNotNone(mirrored)
        self.assertAlmostEqual(shot[1][0] + mirrored[1][0], 2 * cx, delta=1e-6)
        self.assertAlmostEqual(shot[1][1], mirrored[1][1], delta=1e-6)

    def test_escape_shot_prefers_a_safe_nudge(self):
        game = make_table()
        bot = BotController(random.Random(2))
        escape = bot._escape_shot(game)
        self.assertIsNotNone(escape)
        self.assertTrue(0.22 <= escape[2] <= 1.0)



class StubSyncTests(unittest.TestCase):
    def test_sync_uses_stub_snapshot_safely(self):
        stub = SimpleNamespace(round_serial=9, shots=0, pocketed_icons=0, state='ready')
        bot = BotController(random.Random(1))
        bot.sync(stub, 0.0)
        stub.shots = 1
        bot.sync(stub, 1.0)
        self.assertEqual(bot.turn, 'bot')

    def test_sync_does_not_hang_when_shot_counter_drops(self):
        bot = BotController(random.Random(1))
        stub = SimpleNamespace(round_serial=4, shots=3, pocketed_icons=0,
                               state='ready', white_sunk=False)
        bot.sync(stub, 0.0)
        stub.shots = 1
        done = threading.Event()
        errors = []

        def worker():
            try:
                bot.sync(stub, 0.1)
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self.assertTrue(done.wait(2), 'sync hung after the shot counter dropped')
        self.assertFalse(errors)
        self.assertEqual(bot._shots_seen, 3)


if __name__ == '__main__':
    unittest.main()
