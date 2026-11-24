"""Behaviour tests for the hot-seat solo bot."""
import math
import queue
import random
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import dib.bot as bot_module
from dib.bot import (
    BREAK_CHARGE_MAX,
    BREAK_CHARGE_MIN,
    CLUSTER_RADIUS_FACTOR,
    MODES,
    SETTINGS,
    BotController,
    cluster_density,
)
from dib.overlay import BilliardsOverlay
from dib.table import BALL_IMPULSE, POCKET_ENTRY_ALIGNMENT, ROLLING_DRAG, BilliardsMotion

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
        self.assertEqual((SETTINGS['extreme']['aim_error'], SETTINGS['extreme']['power_error']),
                         (0.002, 0.005))

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
        # Extreme now carries a small power error, so charge can dip below the
        # deterministic 0.22 floor into the shared 0.15 clamp.
        self.assertTrue(0.15 <= game.charge <= 1.0)
        self.assertGreater(game.power, 0.0)


class PocketEntryGateTests(unittest.TestCase):
    """Direct pots use the same pocket-entry geometry as the rolling engine."""

    @staticmethod
    def _single_target_game():
        game = make_table()
        game.sunk = set(range(2, 16))
        game.break_shot = False
        game.state = 'ready'
        return game

    def test_grazing_rail_candidate_is_rejected(self):
        game = self._single_target_game()
        left, top, right, _ = game.bounds
        mid = (left + right) / 2
        px, py = game.pockets[1]
        self.assertEqual((px, py), (mid, top))
        target = game.icons[0]
        # A ball rolling almost parallel past the mouth stays below the entry threshold.
        graze_x, graze_y = px - 20, top + target.radius + 4
        graze_towards = math.hypot(px - graze_x, py - graze_y)
        self.assertLess((px - graze_x) / graze_towards, POCKET_ENTRY_ALIGNMENT)
        self.assertFalse(BotController._pocket_entry_ok(
            game, graze_x, graze_y, 1., 0., px, py, target.radius))
        target.x, target.y = px - 300, top + target.radius + 4
        towards = math.hypot(px - target.x, py - target.y)
        ux, uy = (px - target.x) / towards, (py - target.y) / towards
        self.assertEqual(game._boundary_event((target.x, target.y), (ux, uy), target.radius)[1], 'rail')
        self.assertFalse(BotController._pocket_entry_ok(
            game, target.x, target.y, ux, uy, px, py, target.radius))

        # With that mouth as the only pocket, the bot must fall back to a nudge.
        game.pockets = ((px, py),)
        game.white.x = target.x - ux * 220 - uy * 40
        game.white.y = target.y - uy * 220 + ux * 40
        plan = BotController(random.Random(1)).plan_shot(game)
        self.assertIsNotNone(plan)
        contact = target.radius + game.white.radius
        grazing_ghost = (target.x - ux * contact, target.y - uy * contact)
        self.assertGreater(math.dist(plan[1], grazing_ghost), 5.)

    def test_corner_mouth_path_accepted_by_engine_is_allowed(self):
        game = self._single_target_game()
        left, top, _, _ = game.bounds
        px, py = game.pockets[0]
        target = game.icons[0]
        target.x, target.y = left + 272, top + 132
        dx, dy = px - target.x, py - target.y
        distance = math.hypot(dx, dy)
        ux, uy = dx / distance, dy / distance
        entry = game._ray_circle(target.x, target.y, ux, uy, px, py,
                                 game.pocket_radius + target.radius * .55)
        rail_x = ((game.bounds[2] - target.radius if ux > 0 else left + target.radius) - target.x) / ux
        rail_y = ((game.bounds[3] - target.radius if uy > 0 else top + target.radius) - target.y) / uy
        self.assertIsNotNone(entry)
        self.assertGreater(entry, min(rail_x, rail_y))  # only the open mouth lets the engine accept it
        self.assertEqual(game._boundary_event((target.x, target.y), (ux, uy), target.radius)[1], 'pocket')
        self.assertTrue(BotController._pocket_entry_ok(
            game, target.x, target.y, ux, uy, px, py, target.radius))

    def test_ordinary_direct_pot_is_still_selected(self):
        game = self._single_target_game()
        px, py = game.pockets[1]
        target = game.icons[0]
        target.x, target.y = px, py + 150
        game.white.x, game.white.y = px, py + 350
        game.pockets = ((px, py),)
        self.assertTrue(BotController._pocket_entry_ok(
            game, target.x, target.y, 0., -1., px, py, target.radius))
        plan = BotController(random.Random(1)).plan_shot(game)
        self.assertEqual(len(plan), 3)
        expected_ghost = (target.x, target.y + target.radius + game.white.radius)
        self.assertAlmostEqual(plan[1][0], expected_ghost[0], delta=1.)
        self.assertAlmostEqual(plan[1][1], expected_ghost[1], delta=1.)


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


class ComboAndKickTests(unittest.TestCase):
    """U5: two-ball plant candidates and one-rail cue kicks, without level tuning."""

    @staticmethod
    def _play_shot(game, plan):
        game.aim_at(*plan[1])
        game.shoot(plan[2], 0.0)
        now = 0.0
        for _ in range(1500):
            now += 1 / 60
            game.step(1 / 60, now)
            if game.state not in ('rolling', 'stroke'):
                break

    @staticmethod
    def _plant_table(with_blocker=False):
        game = BilliardsMotion(WORK_AREA, [(64, 64)] * (5 if with_blocker else 4))
        game.sunk = set(range(5 if with_blocker else 4, 16))
        game.break_shot = False
        game.state = 'ready'
        game.white.x, game.white.y = 730, 500
        driver, receiver, stopper = game.icons[:3]
        # Receiver is a short roll from the top-middle pocket; the driver stands
        # behind it, and the stopper kills the driver's own roll to the bottom.
        driver.x, driver.y = 800, 355
        receiver.x, receiver.y = 800, 260
        stopper.x, stopper.y = 800, 540
        if with_blocker:
            game.icons[3].x, game.icons[3].y = 760, 470
        return game

    def test_plant_sends_the_receiver_into_the_pocket(self):
        game = self._plant_table()
        driver, receiver = game.icons[:2]
        ghost = (driver.x, driver.y + driver.radius + game.white.radius)
        plants = BotController._plant_candidates(game)
        self.assertTrue(any(math.dist(candidate[1], ghost) < 1. for candidate in plants))
        plan = BotController(random.Random(1)).plan_shot(game)
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan), 3)
        self.assertLess(math.dist(plan[1], ghost), 1.)
        self.assertTrue(0.22 <= plan[2] <= 1.0)
        self._play_shot(game, plan)
        self.assertEqual(game.first_target_hit, 1)
        self.assertIn(2, game.sunk)
        self.assertEqual(game.pocket_order[0], 2)

    def test_plant_blocked_by_an_intervening_ball_is_rejected(self):
        game = self._plant_table(with_blocker=True)
        driver = game.icons[0]
        plant_ghost = (driver.x, driver.y + driver.radius + game.white.radius)
        for candidate in BotController._plant_candidates(game):
            self.assertGreater(math.dist(candidate[1], plant_ghost), 5.)
        plan = BotController(random.Random(1)).plan_shot(game, banks=True)
        self.assertIsNotNone(plan)
        self.assertGreater(math.dist(plan[1], plant_ghost), 5.)

    @staticmethod
    def _kick_table(with_blocker=False):
        game = BilliardsMotion(WORK_AREA, [(64, 64)] * (4 if with_blocker else 3))
        game.sunk = set(range(4 if with_blocker else 3, 16))
        game.break_shot = False
        game.state = 'ready'
        game.white.x, game.white.y = 750, 600
        target, blocker = game.icons[:2]
        target.x, target.y = 750, 300
        blocker.x, blocker.y = 750, 520  # hides the target on the direct line
        if with_blocker:
            game.icons[2].x, game.icons[2].y = 1200, 394  # blocks the reflected leg
            game.pockets = (game.pockets[0],)
        return game

    def test_kick_reaches_the_hidden_target_past_the_blocker(self):
        game = self._kick_table()
        target, blocker = game.icons[:2]
        px, py = game.pockets[0]
        dx, dy = px - target.x, py - target.y
        length = math.hypot(dx, dy)
        ux, uy = dx / length, dy / length
        ghost = (target.x - ux * (target.radius + game.white.radius),
                 target.y - uy * (target.radius + game.white.radius))
        self.assertFalse(game._clear(game.white.x, game.white.y, *ghost, [blocker], game.white.radius))
        kicks = BotController._kick_candidates(game, target, game.pockets[0], [blocker])
        self.assertTrue(kicks)
        left, _, right, _ = game.bounds
        # The aim point is the rebound itself, not the off-table mirror target.
        self.assertTrue(all(left < candidate[1][0] < right for candidate in kicks))
        self.assertTrue(any(abs(candidate[1][0] - (right - game.white.radius)) < 1.
                            for candidate in kicks))
        bot = BotController(random.Random(1))
        plan = bot.plan_shot(game, banks=True)
        self.assertIsNotNone(plan)
        self.assertIn(plan[1], [candidate[1] for candidate in kicks])
        self.assertNotEqual(plan[1], bot.plan_shot(game, banks=False)[1])
        self._play_shot(game, plan)
        self.assertEqual(game.first_target_hit, 1)
        self.assertGreaterEqual(game.rail_hits, 1)
        self.assertNotIn(2, game.sunk)

    def test_kick_with_a_blocked_rail_leg_is_rejected(self):
        game = self._kick_table(with_blocker=True)
        target, blocker, rail_blocker = game.icons[:3]
        self.assertEqual(BotController._kick_candidates(
            game, target, game.pockets[0], [blocker, rail_blocker]), [])
        plan = BotController(random.Random(1)).plan_shot(game, banks=True)
        self.assertIsNotNone(plan)
        left, _, right, _ = game.bounds
        self.assertNotAlmostEqual(plan[1][0], right - game.white.radius, delta=1.)


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

    def test_escape_shot_ignores_a_blocked_ball(self):
        game = make_table()
        game.sunk = set(range(4, 15))
        white = game.white
        cx, cy = 800, 500
        white.x, white.y = cx, cy
        blocker, far, near, extra = game.icons[:4]
        blocker.x, blocker.y = cx + 180, cy
        far.x, far.y = cx + 360, cy
        near.x, near.y = cx - 360, cy
        extra.x, extra.y = cx - 180, cy
        far_ghost = (far.x - far.radius - white.radius, far.y)
        self.assertFalse(game._clear(white.x, white.y, *far_ghost,
                                     [b for b in game.icons if b is not far], white.radius))
        shot = BotController._escape_shot(game)
        self.assertIsNotNone(shot)
        self.assertGreater(math.dist(shot[1], far_ghost), 50.)
        self.assertLess(shot[1][0], cx)

    def test_cluster_density_counts_only_nearby_active_balls(self):
        game = make_table()
        game.sunk = set(range(4, 15))
        cx, cy = 800, 500
        centre, close_a, close_b, far = game.icons[:4]
        centre.x, centre.y = cx, cy
        close_a.x, close_a.y = cx + 70, cy
        close_b.x, close_b.y = cx, cy + 70
        far.x, far.y = cx + 600, cy + 600
        self.assertEqual(cluster_density(game, centre), 2)
        self.assertEqual(cluster_density(game, far), 0)
        self.assertEqual(BotController.cluster_density(game, centre), 2)
        self.assertLess(70., CLUSTER_RADIUS_FACTOR * centre.radius)
        game.sunk.add(2)
        self.assertEqual(cluster_density(game, centre), 1)

    def test_dense_cluster_escape_uses_a_strong_rim_break(self):
        game = make_table()
        game.sunk = set(range(4, 15))
        game.white.x, game.white.y = 400, 500
        centre, right, diagonal, below = game.icons[:4]
        centre.x, centre.y = 800, 500
        right.x, right.y = 870, 500
        diagonal.x, diagonal.y = 870, 570
        below.x, below.y = 800, 570
        shot = BotController._escape_shot(game)
        self.assertIsNotNone(shot)
        self.assertGreaterEqual(cluster_density(game, centre), 2)
        self.assertGreaterEqual(shot[2], BREAK_CHARGE_MIN)
        self.assertLessEqual(shot[2], BREAK_CHARGE_MAX)
        self.assertLess(math.dist(shot[1], (centre.x, centre.y)),
                        centre.radius + game.white.radius + 1.)
        self.assertLess(shot[1][0], centre.x)  # rim behind the cluster edge
        self.assertLess(shot[1][1], centre.y)

    def test_escape_shot_falls_back_to_safety_when_break_hits_a_pocket(self):
        game = make_table()
        game.sunk = set(range(4, 15))
        game.white.x, game.white.y = 450, 470
        dense, neighbour_a, neighbour_b, safe = game.icons[:4]
        dense.x, dense.y = 250, 270
        neighbour_a.x, neighbour_a.y = 130, 210
        neighbour_b.x, neighbour_b.y = 190, 150
        safe.x, safe.y = 800, 700
        self.assertGreaterEqual(cluster_density(game, dense), 2)
        dx, dy = dense.x - game.white.x, dense.y - game.white.y
        approach = math.hypot(dx, dy)
        ux, uy = dx / approach, dy / approach
        gx = dense.x - ux * (dense.radius + game.white.radius)
        gy = dense.y - uy * (dense.radius + game.white.radius)
        danger, _ = BotController._cue_pocket_danger(
            game, gx, gy, gx - game.white.x, gy - game.white.y, ux, uy)
        self.assertGreater(danger, 0.)
        shot = BotController._escape_shot(game)
        self.assertIsNotNone(shot)
        self.assertGreater(shot[1][0], 700.)  # parked beside the isolated ball
        self.assertLess(shot[2], BREAK_CHARGE_MIN)


class ClusterScenarioTests(unittest.TestCase):
    """U8: five fixed layouts measured through plan_shot() and real physics.

    Layouts are constants; only the bot's documented aim/power errors use a
    fixed seed. Deterministic cells use level flags with rollout and errors
    disabled; the informational pass keeps the real level flags, including the
    bounded rollout. All shots settle on a clone via clone_light()/step().
    """

    LEVELS = ('easy', 'medium', 'hard', 'extreme')
    FRAME_LIMIT = 1500
    RNG_SEED = 11
    LAYOUTS = {
        # Primary target hidden behind a blocker; one pocket, reflected legs blocked.
        'blocked_snooker': {
            'count': 4,
            'positions': ((750, 300), (750, 520), (1200, 394), (300, 300)),
            'white': (750, 600),
            'pockets': (0,),
        },
        # Isolated ball on the top-middle pocket axis.
        'direct_pot': {
            'count': 1,
            'positions': ((800, 298),),
            'white': (800, 498),
        },
        # Driver/receiver plant above the cue, with a stopper behind the driver.
        'combo_plant': {
            'count': 4,
            'positions': ((800, 355), (800, 260), (800, 540), (1100, 700)),
            'white': (730, 500),
        },
        # Target hidden behind a blocker; a rail rebound is the only pot line.
        'kick': {
            'count': 3,
            'positions': ((750, 300), (750, 520), (300, 300)),
            'white': (750, 600),
        },
        # Four-ball tight group; no clear pot until the group is broken.
        'dense_cluster': {
            'count': 4,
            'positions': ((800, 500), (870, 500), (870, 570), (800, 570)),
            'white': (400, 500),
        },
    }
    EXPECTED_POCKETS = {
        'blocked_snooker': (0, 0, 0, 0),
        'direct_pot': (1, 1, 1, 1),
        'combo_plant': (0, 0, 1, 1),
        'kick': (0, 0, 0, 0),
        'dense_cluster': (0, 1, 1, 1),
    }
    # The firm rim break that clears the dense cluster also sends the cue into
    # a nearby pocket. Scratches are measured, not hidden by re-spotting.
    EXPECTED_SCRATCHES = {
        'blocked_snooker': (0, 0, 0, 0),
        'direct_pot': (0, 0, 0, 0),
        'combo_plant': (0, 0, 0, 0),
        'kick': (0, 0, 0, 0),
        'dense_cluster': (0, 1, 1, 1),
    }

    @classmethod
    def _make_game(cls, name):
        spec = cls.LAYOUTS[name]
        game = BilliardsMotion(WORK_AREA, [(64, 64)] * spec['count'])
        game.sunk = set(range(spec['count'] + 1, 16))
        game.break_shot = False
        game.state = 'ready'
        for body, (x, y) in zip(game.icons, spec['positions']):
            body.x, body.y = x, y
        game.white.x, game.white.y = spec['white']
        if 'pockets' in spec:
            game.pockets = tuple(game.pockets[index] for index in spec['pockets'])
        return game

    @staticmethod
    def _active_icons(game):
        return [body for index, body in enumerate(game.icons, 1) if index not in game.sunk]

    @classmethod
    def _ghost_crosses_blocker(cls, game, ghost):
        """True when another active ball covers the cue-to-ghost segment."""
        contact = game.icons[0].radius + game.white.radius + 3.
        target = min(cls._active_icons(game),
                     key=lambda body: math.hypot(body.x - ghost[0], body.y - ghost[1]),
                     default=None)
        if target and math.hypot(target.x - ghost[0], target.y - ghost[1]) <= contact:
            blockers = [body for body in cls._active_icons(game) if body is not target]
        else:
            blockers = cls._active_icons(game)
        return not game._clear(game.white.x, game.white.y, ghost[0], ghost[1],
                               blockers, game.white.radius)

    @classmethod
    def _planned_game(cls, name, level, *, deterministic):
        game = cls._make_game(name)
        bot = BotController(random.Random(cls.RNG_SEED))
        bot.mode = level
        flags = None
        if deterministic:
            flags = dict(SETTINGS[level], rollout_k=0, aim_error=0., power_error=0.)
        started = time.perf_counter()
        plan = bot.plan_shot(game, flags=flags)
        return game, bot, plan, (time.perf_counter() - started) * 1000.

    @staticmethod
    def _finish_rollout(bot, game, timeout=0.6):
        deadline = time.monotonic() + timeout
        while bot._rollout_pending and time.monotonic() < deadline:
            time.sleep(0.002)
        bot._drain_rollout(game)

    @classmethod
    def _simulate(cls, game, plan, level, *, noisy):
        """Strike a clone with the planned line and settle it in bounded frames."""
        clone = game.clone_light()
        ghost_x, ghost_y = plan[1]
        dx, dy = ghost_x - clone.white.x, ghost_y - clone.white.y
        charge = plan[2]
        if noisy:
            # Same error model as BotController._shoot, with a fixed RNG seed.
            rng = random.Random(cls.RNG_SEED)
            angle = math.atan2(dy, dx) + rng.gauss(0., SETTINGS[level]['aim_error'])
            dx, dy = math.cos(angle), math.sin(angle)
            charge = min(1., max(.15, charge + rng.gauss(0., SETTINGS[level]['power_error'])))
        clone.aim_at(clone.white.x + dx * 100., clone.white.y + dy * 100.)
        if not clone.shoot(charge, None):
            raise AssertionError('clone rejected the planned shot')
        initial = clone.pocketed_icons
        started = time.perf_counter()
        now = 0.
        frames = 0
        for frames in range(1, cls.FRAME_LIMIT + 1):
            now += 1 / 60
            clone.step(1 / 60, now)
            if clone.state not in ('stroke', 'rolling'):
                break
        return {
            'pocketed': clone.pocketed_icons - initial,
            'scratches': int(clone.white_sunk),
            'frames': frames,
            'state': clone.state,
            'crosses': cls._ghost_crosses_blocker(game, plan[1]),
            'sim_ms': (time.perf_counter() - started) * 1000.,
        }

    def test_fixed_layout_metrics_are_deterministic_and_bounded(self):
        for name in self.LAYOUTS:
            expected = self.EXPECTED_POCKETS[name]
            scratches = self.EXPECTED_SCRATCHES[name]
            for level, expected_potted, expected_scratches in zip(
                    self.LEVELS, expected, scratches):
                with self.subTest(scenario=name, level=level):
                    game, _bot, plan, _ms = self._planned_game(
                        name, level, deterministic=True)
                    if plan is None:
                        self.assertEqual((expected_potted, expected_scratches), (0, 0))
                        continue
                    self.assertEqual(len(plan), 3)
                    self.assertTrue(.15 <= plan[2] <= 1.)
                    self.assertFalse(self._ghost_crosses_blocker(game, plan[1]))
                    result = self._simulate(game, plan, level, noisy=False)
                    self.assertIn(result['state'], ('ready', 'finished', 'return_white',
                                                    'ball_in_hand'))
                    self.assertLessEqual(result['frames'], self.FRAME_LIMIT)
                    self.assertEqual(result['pocketed'], expected_potted)
                    self.assertEqual(result['scratches'], expected_scratches)

    def test_tier_specific_plan_geometry(self):
        def plans_for(name):
            return {level: self._planned_game(name, level, deterministic=True)[2]
                    for level in self.LEVELS}

        blocked = plans_for('blocked_snooker')
        self.assertEqual([plan is not None for plan in blocked.values()],
                         [False, False, False, True])
        bottom = self._make_game('blocked_snooker').bounds[3]
        rail = bottom - self._make_game('blocked_snooker').white.radius
        self.assertAlmostEqual(blocked['extreme'][1][1], rail, delta=1.)

        direct = plans_for('direct_pot')
        self.assertTrue(all(plan is not None for plan in direct.values()))
        for plan in direct.values():
            self.assertLess(math.dist(plan[1], direct['easy'][1]), 1e-6)

        plant = plans_for('combo_plant')
        driver = self._make_game('combo_plant').icons[0]
        game = self._make_game('combo_plant')
        plant_ghost = (driver.x, driver.y + driver.radius + game.white.radius)
        for level in ('hard', 'extreme'):
            self.assertLess(math.dist(plant[level][1], plant_ghost), 1.)
        for level in ('easy', 'medium'):
            self.assertGreater(math.dist(plant[level][1], plant_ghost), 50.)

        kick = plans_for('kick')
        self.assertEqual([plan is not None for plan in kick.values()],
                         [False, False, True, True])
        self.assertGreater(math.dist(kick['hard'][1], kick['extreme'][1]), 50.)
        bottom = self._make_game('kick').bounds[3]
        rail = bottom - self._make_game('kick').white.radius
        self.assertAlmostEqual(kick['extreme'][1][1], rail, delta=1.)

        dense = plans_for('dense_cluster')
        self.assertIsNone(dense['easy'])
        for level in ('medium', 'hard', 'extreme'):
            self.assertGreaterEqual(dense[level][2], BREAK_CHARGE_MIN)

    def test_benchmark_metrics_log(self):
        """Log measured numbers only; timing and seeded errors stay informational."""
        print('\nU8 cluster benchmark (measured; no FPS claim):')
        print('scenario level det_pot det_scratch det_cross '
              'noisy_pot noisy_scratch noisy_cross frames plan_ms sim_ms')
        for name in self.LAYOUTS:
            for level in self.LEVELS:
                game, bot, plan, plan_ms = self._planned_game(
                    name, level, deterministic=True)
                det = self._simulate(game, plan, level, noisy=False) if plan else None
                noisy_game, noisy_bot, _initial_plan, _ = self._planned_game(
                    name, level, deterministic=False)
                self._finish_rollout(noisy_bot, noisy_game)
                self.assertTrue(det is None or not det['crosses'])
                # Re-plan after draining the worker so the logged line is the
                # pick gameplay would use, without dispatching a second rollout.
                noisy_plan = noisy_bot.plan_shot(
                    noisy_game, flags=dict(SETTINGS[level], rollout_k=0))
                noisy = (self._simulate(noisy_game, noisy_plan, level, noisy=True)
                         if noisy_plan else None)
                sim_ms = '-' if noisy is None else f"{noisy['sim_ms']:.2f}"
                print(f"{name:16s} {level:8s} "
                      f"{'-' if det is None else det['pocketed']:>8} "
                      f"{'-' if det is None else det['scratches']:>11} "
                      f"{'-' if det is None else det['crosses']:>10} "
                      f"{'-' if noisy is None else noisy['pocketed']:>10} "
                      f"{'-' if noisy is None else noisy['scratches']:>13} "
                      f"{'-' if noisy is None else noisy['crosses']:>11} "
                      f"{'-' if noisy is None else noisy['frames']:>6} "
                      f"{plan_ms:8.2f} {sim_ms:>7}")


class BallInHandTests(unittest.TestCase):
    """U7: the scratch shooter's opponent places the cue, through the real chain.

    Every test drives shot -> scratch -> return_white -> ball_in_hand -> ready
    with the overlay's frame order (bot sync before table.step) and never sets
    ``bot.turn`` by hand. Turn handover stays in ``sync()`` at 'ready'.
    """

    FRAME = 1 / 60
    MAX_FRAMES = 600

    @staticmethod
    def _scratch_game():
        game = make_table()
        game.sunk = set(range(2, 16))
        game.break_shot = False
        # One isolated target directly below the top-middle pocket: the bot's
        # axis placement is a straight pot, the default grid is a thin cut.
        px, py = game.pockets[1]
        game.icons[0].x, game.icons[0].y = px, py + 120
        return game

    @staticmethod
    def _axis_alignment(game, x, y):
        target = game.icons[0]
        px, py = game.pockets[1]
        dx, dy = px - target.x, py - target.y
        wx, wy = x - target.x, y - target.y
        return abs((dx * wx + dy * wy) / (math.hypot(dx, dy) * math.hypot(wx, wy)))

    def _strike(self, game, bot, now, shooter):
        """Take a harmless side rail shot; the caller marks the scratch."""
        self.assertEqual(bot.turn, shooter)
        game.aim_at(game.white.x + 200, game.white.y)
        charge = .15  # short side rail roll: never reaches the isolated target
        self.assertTrue(game.shoot(charge, now))
        bot.note_shot_input(0.0, charge)
        bot.sync(game, now)  # installs the placement hook, then reads the shot
        self.assertEqual(game.white_placer, bot.place_ball_in_hand)
        self.assertEqual(bot._last_shooter, shooter)
        self.assertTrue(bot._shot_pending)

    def _settle(self, game, bot, now):
        """Step frames like the overlay: sync first, physics second."""
        seen = set()
        for _ in range(self.MAX_FRAMES):
            now += self.FRAME
            bot.sync(game, now)
            seen.add(game.state)
            game.step(self.FRAME, now)
            seen.add(game.state)
            if game.state == 'ready':
                # The next sync observes the settled frame and hands the turn
                # over, exactly as the overlay's following frame does.
                bot.sync(game, now)
                return now, seen
        self.fail(f'shot never settled: {game.state}')

    @staticmethod
    def _track_placement(bot):
        """Record table state, stale turn, owner and result at each hook call."""
        real = bot.place_ball_in_hand
        events = []

        def tracking(table):
            state, turn, owner = table.state, bot.turn, bot._ball_in_hand_owner(table)
            handled = real(table)
            events.append({'state': state, 'turn': turn,
                           'owner': owner, 'handled': handled})
            return handled

        bot.place_ball_in_hand = tracking
        return events

    def _scratch_chain(self, mode, shooter):
        game = self._scratch_game()
        bot = BotController(random.Random(2))
        bot.mode = mode
        events = self._track_placement(bot)
        now = 0.0
        if shooter == 'bot':
            # Earn the bot turn with a normal human miss, handover at 'ready'.
            self._strike(game, bot, now, 'human')
            now, _ = self._settle(game, bot, now)
            self.assertEqual(bot.turn, 'bot')
        with mock.patch.object(game, '_auto_place', side_effect=game._auto_place) as auto:
            self._strike(game, bot, now, shooter)
            game.white_sunk = True  # cue drops: the scratch being tested
            now, seen = self._settle(game, bot, now)
        return game, bot, auto, events, seen

    def test_human_scratch_uses_bot_placement_then_passes_turn(self):
        for mode in ('medium', 'hard', 'extreme'):
            with self.subTest(mode=mode):
                game, bot, auto, events, seen = self._scratch_chain(mode, 'human')
                self.assertIn('return_white', seen)
                # ``turn`` is still the scratch shooter's at hook time.
                self.assertEqual(events, [{'state': 'ball_in_hand', 'turn': 'human',
                                           'owner': 'bot', 'handled': True}])
                auto.assert_not_called()
                self.assertEqual(game.state, 'ready')
                self.assertNotIn(0, game.sunk)
                self.assertFalse(game.white_sunk)
                # Engine still accepts the chosen point on a fresh ball-in-hand table.
                probe = self._scratch_game()
                probe.state = 'ball_in_hand'
                self.assertTrue(probe.place_white(game.white.x, game.white.y))
                placed = self._axis_alignment(game, game.white.x, game.white.y)
                default = self._axis_alignment(
                    game, game.bounds[0] + game.width * .20,
                    game.bounds[1] + game.height * .5)
                self.assertGreater(placed, .95)
                self.assertGreater(placed, default + .2)
                self.assertEqual(bot.turn, 'bot')  # handover only at 'ready'

    def test_human_scratch_on_easy_keeps_default_auto_place(self):
        game, bot, auto, events, _seen = self._scratch_chain('easy', 'human')
        self.assertEqual(events, [{'state': 'ball_in_hand', 'turn': 'human',
                                   'owner': 'bot', 'handled': False}])
        auto.assert_called_once_with()
        left, top, _, _ = game.bounds
        self.assertAlmostEqual(game.white.x, left + game.width * .20, places=6)
        self.assertAlmostEqual(game.white.y, top + game.height * .5, places=6)
        self.assertEqual(bot.turn, 'bot')

    def test_bot_scratch_uses_default_placement_and_keeps_human_turn(self):
        for mode in ('easy', 'medium', 'hard', 'extreme'):
            with self.subTest(mode=mode):
                game, bot, auto, events, seen = self._scratch_chain(mode, 'bot')
                self.assertIn('return_white', seen)
                self.assertEqual(events, [{'state': 'ball_in_hand', 'turn': 'bot',
                                           'owner': 'human', 'handled': False}])
                auto.assert_called_once_with()
                self.assertEqual(game.state, 'ready')
                left, top, _, _ = game.bounds
                self.assertAlmostEqual(game.white.x, left + game.width * .20, places=6)
                self.assertAlmostEqual(game.white.y, top + game.height * .5, places=6)
                self.assertEqual(bot.turn, 'human')

    def test_no_valid_sampled_point_falls_back_to_auto_place(self):
        game = self._scratch_game()
        bot = BotController(random.Random(3))
        bot.mode = 'hard'
        events = self._track_placement(bot)
        now = 0.0
        self._strike(game, bot, now, 'human')
        game.white_sunk = True
        bot.sync(game, now)
        game.state = 'return_white'
        game.ready_for = .8
        with mock.patch.object(game, 'place_white', return_value=False) as place:
            with mock.patch.object(game, '_auto_place', return_value=True) as auto:
                game.step(self.FRAME, now + self.FRAME)
        self.assertEqual(game.state, 'ball_in_hand')
        self.assertEqual(bot._ball_in_hand_owner(game), 'bot')
        self.assertEqual(bot.turn, 'human')  # turn still names the shooter
        self.assertEqual(events, [{'state': 'ball_in_hand', 'turn': 'human',
                                   'owner': 'bot', 'handled': True}])
        self.assertGreaterEqual(place.call_count, 24)
        self.assertLessEqual(place.call_count, 36)
        auto.assert_called_once_with()


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


class U6TierAndRolloutTests(unittest.TestCase):
    """U6: level tiering, independent clones and bounded background rollout."""

    TIER_KEYS = ('cluster', 'safety', 'plants', 'banks', 'target_bank', 'rollout_k', 'lookahead')

    @staticmethod
    def _open_game():
        game = make_table()
        game.sunk = set(range(2, 16))
        game.break_shot = False
        game.state = 'ready'
        px, py = game.pockets[1]
        target = game.icons[0]
        target.x, target.y = px, py + 150
        game.white.x, game.white.y = px, py + 350
        return game

    @staticmethod
    def _wait_rollout(bot, game, timeout=2.0):
        deadline = time.monotonic() + timeout
        while bot._rollout_pending and time.monotonic() < deadline:
            time.sleep(0.002)
        bot._drain_rollout(game)

    def test_level_tier_matrix(self):
        expected = {
            'easy': (False, False, False, False, False, 0, False),
            'medium': (True, False, False, False, False, 0, False),
            'hard': (True, False, True, False, True, 5, False),
            'extreme': (True, True, True, True, True, 12, True),
        }
        for level, flags in expected.items():
            with self.subTest(level=level):
                actual = tuple(SETTINGS[level][key] for key in self.TIER_KEYS)
                self.assertEqual(actual, flags)

    def test_easy_calls_no_cluster_plant_bank_kick_or_rollout(self):
        bot = BotController(random.Random(1))
        bot.mode = 'easy'
        game = self._open_game()
        forbidden = {'side_effect': AssertionError('easy must not use this layer')}
        with mock.patch.object(BotController, '_plant_candidates', **forbidden), \
                mock.patch.object(BotController, '_bank_candidates', **forbidden), \
                mock.patch.object(BotController, '_kick_candidates', **forbidden), \
                mock.patch.object(BotController, '_prepare_rollout', **forbidden) as rollout:
            plan = bot.plan_shot(game, flags=SETTINGS['easy'])
        rollout.assert_not_called()
        self.assertTrue(plan is None or len(plan) == 3)

    def test_medium_uses_cluster_break_but_no_combo_or_rollout(self):
        bot = BotController(random.Random(1))
        bot.mode = 'medium'
        game = self._open_game()
        game.pockets = ()  # no pot: force the U4 escape layer
        with mock.patch.object(BotController, '_plant_candidates',
                               side_effect=AssertionError('medium has no combo')), \
                mock.patch.object(BotController, '_prepare_rollout',
                                  side_effect=AssertionError('medium has no rollout')), \
                mock.patch.object(BotController, '_escape_shot', return_value=None,
                                  autospec=True) as escape:
            bot.plan_shot(game, flags=SETTINGS['medium'])
        escape.assert_called_once()
        self.assertTrue(escape.call_args.kwargs['cluster'])
        self.assertFalse(escape.call_args.kwargs['safety'])

    def test_hard_uses_plants_and_target_banks_without_kick(self):
        bot = BotController(random.Random(1))
        bot.mode = 'hard'
        game = self._open_game()
        with mock.patch.object(BotController, '_plant_candidates', return_value=[]) as plants, \
                mock.patch.object(BotController, '_bank_candidates', return_value=[]) as banks, \
                mock.patch.object(BotController, '_kick_candidates', \
                                  side_effect=AssertionError('hard has no kick')), \
                mock.patch.object(BotController, '_prepare_rollout', return_value=False) as rollout:
            bot.plan_shot(game, flags=SETTINGS['hard'])
        plants.assert_called_once()
        banks.assert_called()
        rollout.assert_called_once()

    def test_extreme_uses_every_layer(self):
        bot = BotController(random.Random(1))
        bot.mode = 'extreme'
        game = self._open_game()
        with mock.patch.object(BotController, '_plant_candidates', return_value=[]) as plants, \
                mock.patch.object(BotController, '_bank_candidates', return_value=[]) as banks, \
                mock.patch.object(BotController, '_kick_candidates', return_value=[]) as kicks, \
                mock.patch.object(BotController, '_prepare_rollout', return_value=False) as rollout:
            bot.plan_shot(game, flags=SETTINGS['extreme'])
        plants.assert_called_once()
        banks.assert_called()
        kicks.assert_called()
        rollout.assert_called_once()

    def test_rollout_jobs_are_clones_and_live_table_is_untouched(self):
        bot = BotController(random.Random(1))
        game = self._open_game()
        positions = [(body.x, body.y) for body in game.bodies]
        sunk = set(game.sunk)
        errors = []

        def fake_worker(generation, serial, state, shots, jobs, results, **kwargs):
            try:
                self.assertTrue(jobs)
                for _candidate, clone in jobs:
                    self.assertIsNot(clone.white, game.white)
                    self.assertEqual(len(clone.bodies), len(game.bodies))
                    for cloned_body, live_body in zip(clone.bodies, game.bodies, strict=True):
                        self.assertIsNot(cloned_body, live_body)
            except AssertionError as exc:
                errors.append(repr(exc))
            results.put((generation, serial, state, shots, None, None, 0.0))

        with mock.patch.object(bot_module, 'rollout_worker', fake_worker):
            started = bot._prepare_rollout(game, SETTINGS['extreme'])
            self.assertTrue(started)
            self._wait_rollout(bot, game)
        self.assertFalse(errors, errors)
        self.assertEqual([(body.x, body.y) for body in game.bodies], positions)
        self.assertEqual(game.sunk, sunk)
        self.assertEqual(game.shots, 0)

    def test_real_worker_never_mutates_live_table(self):
        bot = BotController(random.Random(1))
        game = self._open_game()
        before = [(body.x, body.y, body.vx, body.vy) for body in game.bodies]
        sunk = set(game.sunk)
        start = (game.shots, game.state, game.round_serial)
        self.assertTrue(bot._prepare_rollout(game, SETTINGS['extreme']))
        self._wait_rollout(bot, game)
        self.assertEqual([(body.x, body.y, body.vx, body.vy) for body in game.bodies], before)
        self.assertEqual(game.sunk, sunk)
        self.assertEqual((game.shots, game.state, game.round_serial), start)

    def test_stale_results_are_discarded_and_fresh_pick_is_used(self):
        bot = BotController(random.Random(1))
        game = self._open_game()
        flags = dict(SETTINGS['hard'], rollout_k=0)
        candidates = bot._candidates(game, flags=flags)
        self.assertTrue(candidates)
        key = (bot._rollout_generation, game.round_serial, game.state, game.shots)

        bot._rollout_results.put((key[0] - 1,) + key[1:] + (candidates[-1], 1e9, 0.01))
        bot._drain_rollout(game)
        self.assertIsNone(bot._rollout_pick)

        bogus = (9999.0, (12.0, 34.0), 0.5)
        bot._rollout_results.put(key + (bogus, 1e9, 0.01))
        plan = bot.plan_shot(game, flags=flags)
        self.assertNotEqual(plan[1], bogus[1])
        bot._rollout_pick = None

        bot._rollout_results.put(key + (candidates[-1], 1e9, 0.01))
        self.assertEqual(bot.plan_shot(game, flags=flags)[1], candidates[-1][1])

    def test_worker_start_failure_falls_back_deterministically(self):
        class FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError('no threads in this environment')

        bot = BotController(random.Random(1))
        game = self._open_game()
        with mock.patch.object(bot_module.threading, 'Thread', FailingThread):
            started = bot._prepare_rollout(game, SETTINGS['extreme'])
            plan = bot.plan_shot(game, flags=SETTINGS['extreme'])
        self.assertFalse(started)
        self.assertEqual(bot._rollout_fallback, 'thread-start-failed')
        self.assertTrue(plan is None or len(plan) == 3)

    def test_benchmark_log(self):
        """Emit measured numbers only. No frame-rate or shot-time claim is made."""
        time.sleep(0.2)  # let earlier tests' bounded workers finish before timing
        game = self._open_game()
        t0 = time.perf_counter()
        for _ in range(50):
            game.clone_light()
        clone_ms = (time.perf_counter() - t0) * 1000 / 50
        deterministic = BotController(random.Random(1))
        t0 = time.perf_counter()
        deterministic.plan_shot(game, flags=dict(SETTINGS['extreme'], rollout_k=0))
        direct_ms = (time.perf_counter() - t0) * 1000
        background = BotController(random.Random(1))
        t0 = time.perf_counter()
        plan = background.plan_shot(game, flags=SETTINGS['extreme'])
        dispatch_ms = (time.perf_counter() - t0) * 1000
        self._wait_rollout(background, game, timeout=0.5)
        self.assertTrue(plan is None or len(plan) == 3)
        print(f'\nU6 benchmark (informational): clone_light {clone_ms:.3f} ms, '
              f'plan_shot deterministic {direct_ms:.3f} ms, '
              f'plan_shot with rollout dispatch {dispatch_ms:.3f} ms, '
              f'worker {background._rollout_last_ms:.1f} ms, '
              f'budget {bot_module.ROLLOUT_TIME_BUDGET:.2f} s / '
              f'{bot_module.ROLLOUT_MAX_STEPS} steps')

    def test_zero_time_budget_returns_without_a_pick(self):
        bot = BotController(random.Random(1))
        game = self._open_game()
        with mock.patch.object(bot_module, 'ROLLOUT_TIME_BUDGET', 0.0):
            started = bot._prepare_rollout(game, SETTINGS['hard'])
            self.assertTrue(started)
            self._wait_rollout(bot, game)
        self.assertLessEqual(bot._rollout_last_ms, 25.0)
        self.assertIsNone(bot._rollout_pick)


if __name__ == '__main__':
    unittest.main()
