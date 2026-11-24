"""Behaviour tests for the manual desktop billiards table."""
import math
import unittest
from unittest.mock import patch

from dib.table import BilliardsMotion, RAIL_REBOUND


class BilliardsMotionTests(unittest.TestCase):
    def test_two_to_one_table_fits_small_portrait_ultrawide_and_offset_displays(self):
        for area in ((0, 0, 640, 440), (0, 0, 1366, 728), (0, 0, 1920, 1040),
                     (0, 0, 2560, 1528), (0, 0, 3840, 2080), (0, 0, 5120, 1400),
                     (-1080, -300, 0, 1580), (3840, 200, 6400, 1728)):
            with self.subTest(area=area):
                game = BilliardsMotion(area, [(64, 64)] * 45)
                left, top, right, bottom = game.bounds
                self.assertEqual(right - left, 2 * (bottom - top))
                self.assertAlmostEqual((left + right) / 2, (area[0] + area[2]) / 2, delta=.5)
                self.assertAlmostEqual((top + bottom) / 2, (area[1] + area[3]) / 2, delta=.5)
                self.assertGreaterEqual(left - game.pocket_radius, area[0])
                self.assertGreaterEqual(top - game.pocket_radius, area[1])
                self.assertLessEqual(right + game.pocket_radius, area[2])
                self.assertLessEqual(bottom + game.pocket_radius, area[3])
                for ball in game.bodies:
                    self.assertTrue(left + ball.radius <= ball.x <= right - ball.radius)
                    self.assertTrue(top + ball.radius <= ball.y <= bottom - ball.radius)

    def test_flags_and_public_attributes_match_the_manual_contract(self):
        game = BilliardsMotion((0, 0, 1600, 1000), [(64, 64)] * 3)
        self.assertTrue(game.desktop)
        self.assertTrue(game.manual)
        self.assertEqual(len(game.pockets), 6)
        self.assertEqual(len(game.bodies), 1 + len(game.icons))
        self.assertIs(game.bodies[0], game.white)
        self.assertEqual(game.state, 'ready')
        self.assertIsNotNone(game.aim)
        self.assertEqual(game.poses, game._poses())
        for attribute in ('sizes', 'bounds', 'pocket_radius', 'icons', 'exits', 'charge'):
            self.assertTrue(hasattr(game, attribute), attribute)
        self.assertTrue(callable(game.reset))
        self.assertTrue(callable(game.step))

    def test_sinking_icon_slides_into_the_pocket_shrinking_and_fading(self):
        work = (-1920, -120, 0, 960)
        game = BilliardsMotion(work, [(64, 64)] * 2)
        game.state = 'rolling'
        px, py = game.pockets[0]
        game.icons[0].x, game.icons[0].y = px + 30, py + 20
        game.step(.01, 0)
        self.assertIn(1, game.sunk)
        self.assertNotIn(game.icons[0], game.active_bodies())
        x, y, fade, scale, spin = game.exit_visual(1)
        self.assertEqual((fade, scale, spin), (0, 1, 0))
        previous = (math.dist((x, y), (px, py)), 1.)
        for frame in range(1, 11):
            game.step(.05, frame * .05)
            visual = game.exit_visual(1)
            if visual is None:
                break
            x, y, fade, scale, spin = visual
            self.assertTrue(work[0] < x < work[2] and work[1] < y < work[3])
            distance = math.dist((x, y), (px, py))
            self.assertLessEqual(distance, previous[0] + 1e-9)   # only ever moves towards the pocket
            self.assertLessEqual(scale, previous[1] + 1e-9)      # only ever shrinks
            previous = (distance, scale)
        self.assertLess(previous[0], 1.0)
        self.assertLess(previous[1], .5)
        for frame in range(8):
            game.step(.05, .6 + frame * .05)
        self.assertIsNone(game.exit_visual(1))

    def test_cue_length_charge_and_stroke_approach(self):
        # A tall work area means the cue is long enough to reach its 440 px cap.
        game = BilliardsMotion((0, 0, 2400, 1400), [(64, 64)] * 2)
        game.aim_at(game.white.x, game.white.y + 200)
        tip, tail = game.cue()
        self.assertAlmostEqual(tip[1] - tail[1], min(440, game.height * .48), delta=1)
        self.assertAlmostEqual(tip[0], game.white.x, delta=.01)
        distance = game.white.y - tip[1]
        self.assertAlmostEqual(distance, game.white.radius + 12, delta=.5)
        game.charge = 1.
        charged_tip = game.cue()[0]
        self.assertAlmostEqual(game.white.y - charged_tip[1], game.white.radius + 46, delta=.5)

        game.charge = 0.
        game.state = 'stroke'
        game.stroke = .041
        drawn_tip = game.cue()[0]
        self.assertGreater(drawn_tip[1], tip[1])
        game.state = 'rolling'
        self.assertIsNone(game.cue())

    def test_manual_shoot_uses_desktop_power_multiplier(self):
        game = BilliardsMotion((0, 0, 1600, 1000), [(64, 64)] * 2)
        game.aim = (1., 0.)
        self.assertTrue(game.shoot(1))
        self.assertEqual(game.state, 'stroke')
        self.assertFalse(game.shoot(1))
        game.step(.05, 0.)
        self.assertEqual(game.white.vx, 0)
        game.step(.05, .05)
        expected = game.height * (.62 + 2.55) * 1.58
        self.assertAlmostEqual(game.white.vx, expected, delta=expected * .01)
        self.assertEqual(game.white.vy, 0)

    def test_zero_charge_is_a_small_nudge_and_full_power_is_unchanged(self):
        from dib.overlay import TablePointer
        game = BilliardsMotion((0, 0, 1600, 1000), [(64, 64)] * 2)
        pointer = TablePointer()
        start_x = game.white.x
        pointer.update(game, 9., start_x + 200, game.white.y, False, True)
        pointer.update(game, 10., start_x + 200, game.white.y, True, True)
        pointer.update(game, 10., start_x + 200, game.white.y, False, True)
        self.assertLess(game.power, game.height * .2)
        for frame in range(180):
            game.step(1 / 60, 10 + (frame + 1) / 60)
        self.assertEqual(game.state, 'ready')
        self.assertGreater(game.white.x - start_x, 0)
        self.assertLess(game.white.x - start_x, game.height * .06)
        self.assertAlmostEqual(game._shot_speed(1), game.height * (.62 + 2.55) * 1.58)
        speeds = [game._shot_speed(value / 100) for value in range(101)]
        self.assertTrue(all(a < b for a, b in zip(speeds, speeds[1:])))

    def test_half_charge_uses_half_of_the_speed_range(self):
        game = BilliardsMotion((0, 0, 1600, 1000), [(64, 64)] * 2)
        minimum, maximum = game._shot_speed(0), game._shot_speed(1)
        game.shoot(.5)
        self.assertAlmostEqual(game.power, minimum + (maximum - minimum) * .5)
        self.assertEqual(game._shot_speed(-1), minimum)
        self.assertEqual(game._shot_speed(2), maximum)

    def test_pointer_charges_linearly_in_one_and_a_half_seconds(self):
        from dib.overlay import TablePointer
        game = BilliardsMotion((0, 0, 1600, 1000), [(64, 64)] * 2)
        pointer = TablePointer()
        x, y = game.white.x + 200, game.white.y
        pointer.update(game, 9., x, y, False, True)
        pointer.update(game, 10., x, y, True, True)
        for now, expected in ((10.75, .5), (11.5, 1.)):
            pointer.update(game, now, x, y, True, True)
            self.assertAlmostEqual(game.charge, expected)
        pointer.update(game, 11.5, x, y, False, True)
        self.assertEqual(game.state, 'stroke')
        self.assertAlmostEqual(game.power, game._shot_speed(1))

    def test_pointer_cycles_and_releases_at_the_current_power(self):
        from dib.overlay import TablePointer
        for release, expected in ((2.25, .5), (3., 0.), (4.5, 1.), (6.75, .5)):
            with self.subTest(release=release):
                game = BilliardsMotion((0, 0, 1600, 1000), [(64, 64)] * 2)
                pointer = TablePointer()
                x, y = game.white.x + 200, game.white.y
                pointer.update(game, 0., x, y, False, True)
                pointer.update(game, 0., x, y, True, True)
                for elapsed, power, falling in ((.75, .5, False), (1.5, 1., True), (2.25, .5, True)):
                    if elapsed <= release:
                        pointer.update(game, elapsed, x, y, True, True)
                        self.assertAlmostEqual(game.charge, power)
                        self.assertEqual(pointer.charge_falling, falling)
                pointer.update(game, release, x, y, False, True)
                self.assertEqual(game.state, 'stroke')
                self.assertAlmostEqual(game.power, game._shot_speed(expected))
                self.assertEqual(game.shots, 1)
                self.assertIsNone(pointer.charge_origin)

    def test_release_outside_table_cancels_cycle_and_new_press_starts_at_zero(self):
        from dib.overlay import TablePointer
        game = BilliardsMotion((0, 0, 1600, 1000), [(64, 64)] * 2)
        pointer = TablePointer()
        x, y = game.white.x + 200, game.white.y
        pointer.update(game, 0., x, y, False, True)
        pointer.update(game, 1., x, y, True, True)
        pointer.update(game, 3., x, y, False, False)
        self.assertEqual((game.state, game.shots, game.charge), ('ready', 0, 0.))
        pointer.update(game, 4., x, y, True, True)
        self.assertEqual(game.charge, 0.)
        self.assertFalse(pointer.charge_falling)

    def test_manual_shoot_collision_chain_rebounds_and_rearms(self):
        game = BilliardsMotion((0, 0, 1600, 1000), [(64, 64)] * 2)
        # Put two balls in the same line so the first must pass its impulse on.
        for index, body in enumerate(game.icons):
            body.x = game.white.x + 250 + index * (game.radius * 2 + 8)
            body.y = game.white.y
        game.aim = game._aim()
        self.assertTrue(game.shoot(.9))
        tip_before = game.cue()[0][0]
        game.step(.04, .04)
        self.assertGreater(game.cue()[0][0], tip_before)
        self.assertEqual(game.white.vx, 0)
        second = game.icons[1]
        moved = False
        reflected = False
        for frame in range(460):
            game.step(.012, .45 + frame * .012)
            moved |= second.vx > 50
            reflected |= moved and second.vx < -30
        self.assertTrue(moved)
        self.assertTrue(reflected)
        self.assertEqual(game.state, 'ready')
        self.assertIsNotNone(game.cue())
        self.assertTrue(game.shoot(.7))

    def test_rack_and_moving_balls_stay_inside_main_table(self):
        for count in (1, 61, 300):
            with self.subTest(count=count):
                game = BilliardsMotion((-1600, -250, 0, 750), [(128, 76)] * count)
                self.assertTrue(game.shoot(1))
                for frame in range(100):
                    game.step(.016, frame * .016)
                    left, top, right, bottom = game.bounds
                    for body in game.active_bodies():
                        self.assertLessEqual(left + body.radius, body.x)
                        self.assertLessEqual(body.x, right - body.radius)
                        self.assertLessEqual(top + body.radius, body.y)
                        self.assertLessEqual(body.y, bottom - body.radius)

    def test_white_scratch_returns_ball_in_hand_and_can_be_replaced(self):
        game = BilliardsMotion((0, 0, 1600, 1000), [(64, 64)] * 3)
        self.assertEqual(len(game.pockets), 6)
        origin = (game.white.x, game.white.y)
        game.state = 'rolling'
        game.white.x, game.white.y = game.pockets[0]
        game.step(.01, 0)
        self.assertTrue(game.white_sunk)
        self.assertEqual(game.state, 'return_white')
        self.assertFalse(game.shoot(1))
        self.assertFalse(game.place_white(*game.pockets[0]))
        for frame in range(20):
            game.step(.05, frame * .05)
        # The white is returned automatically when a free spot exists.
        self.assertEqual(game.state, 'ready')
        self.assertFalse(game.white_sunk)
        self.assertEqual((game.white.x, game.white.y), origin)

        # A full table leaves the white in hand until the player places it.
        game._auto_place = lambda: False
        game.state = 'rolling'
        game.white.x, game.white.y = game.pockets[1]
        game.step(.01, 0)
        for frame in range(20):
            game.step(.05, frame * .05)
        self.assertEqual(game.state, 'ball_in_hand')
        self.assertFalse(game.shoot(1))
        left, top, right, bottom = game.bounds
        self.assertFalse(game.place_white(*game.pockets[1]))
        self.assertFalse(game.place_white(left - 10, top + game.height * .3))
        self.assertTrue(game.place_white(left + game.width * .25, top + game.height * .3))
        self.assertEqual(game.state, 'ready')
        self.assertTrue(game.shoot(.8))

    def test_round_counts_icons_and_freezes_time_until_restart(self):
        game = BilliardsMotion((0, 0, 1600, 1000), [(64, 64)] * 2)
        game.step(.01, 5)
        self.assertEqual(game.round_seconds, 0)  # aiming before the first shot is not timed
        self.assertTrue(game.shoot(1, 10))
        game.state = 'rolling'
        game.white.x, game.white.y = game.pockets[0]
        game.icons[0].x, game.icons[0].y = game.pockets[1]
        game.step(.01, 16)
        self.assertEqual(game.sunk, {0, 1})
        self.assertEqual(game.pocketed_icons, 1)
        self.assertEqual(game.round_seconds, 6)

        game.state = 'rolling'
        game.icons[1].x, game.icons[1].y = game.pockets[2]
        game.step(.01, 24)
        self.assertEqual(game.state, 'finished')
        self.assertEqual(game.pocketed_icons, 2)
        self.assertFalse(game.shoot(.8))
        game.step(.05, 60)
        self.assertEqual(game.state, 'finished')
        self.assertEqual(game.round_seconds, 14)

        game.reset()
        self.assertEqual(game.sunk, set())
        self.assertEqual(game.state, 'ready')
        self.assertEqual(game.round_seconds, 0)
        self.assertTrue(game.break_shot)
        self.assertEqual(len(game.bodies), 3)
        game.step(.01, 61)
        self.assertTrue(game.shoot(1, 61))
        game.step(.01, 63)
        self.assertEqual(game.round_seconds, 2)

    def test_round_timer_is_integer_milliseconds_from_first_shot_to_clear(self):
        game = BilliardsMotion((0, 0, 1600, 1000), [(64, 64)] * 2)
        serial = game.round_serial
        self.assertEqual((game.play_ms, game.shots), (0, 0))
        game.step(.01, 100.0)
        self.assertEqual(game.play_ms, 0)
        self.assertTrue(game.shoot(1, 100.0123))
        self.assertEqual(game.shots, 1)
        game.state = 'rolling'
        game.step(.01, 101.0)
        self.assertEqual(game.play_ms, 988)  # 101.0 - 100.0123 = 0.9877 s
        for icon, pocket in zip(game.icons, game.pockets[1:]):
            icon.x, icon.y = pocket
        game.state = 'rolling'
        game.step(.01, 102.5)
        self.assertEqual(game.state, 'finished')
        self.assertEqual(game.play_ms, 2488)
        self.assertIsInstance(game.play_ms, int)
        game.step(.05, 500.0)
        self.assertEqual(game.play_ms, 2488)  # frozen after the clear

        game.reset()
        self.assertEqual((game.play_ms, game.shots), (0, 0))
        self.assertEqual(game.round_serial, serial + 1)

    def test_hidden_time_does_not_count_towards_the_round(self):
        game = BilliardsMotion((0, 0, 1600, 1000), [(64, 64)] * 2)
        game.shoot(1, 10.0)
        game.state = 'rolling'
        game.step(.01, 12.0)
        self.assertEqual(game.play_ms, 2000)
        game.pause(12.0)
        game.pause(30.0)                      # a second pause call does not move the pause point
        game.resume(612.0)                    # ten minutes later
        game.step(.01, 613.0)
        self.assertEqual(game.play_ms, 3000)  # 2 s before + 1 s after the pause
        game.resume(700.0)                    # resuming without a pause is a no-op
        game.step(.01, 614.0)
        self.assertEqual(game.play_ms, 4000)

    def test_pausing_before_the_first_shot_or_after_the_clear_changes_nothing(self):
        game = BilliardsMotion((0, 0, 1600, 1000), [(64, 64)] * 2)
        game.pause(1.0)
        game.resume(50.0)
        self.assertEqual(game.play_ms, 0)
        game.shoot(1, 60.0)
        game.state = 'rolling'
        for icon, pocket in zip(game.icons, game.pockets[1:]):
            icon.x, icon.y = pocket
        game.step(.01, 65.0)
        self.assertEqual(game.state, 'finished')
        game.pause(66.0)
        game.resume(900.0)
        self.assertEqual(game.play_ms, 5000)

    def test_only_accepted_shots_are_counted(self):
        game = BilliardsMotion((0, 0, 1600, 1000), [(64, 64)] * 2)
        self.assertTrue(game.shoot(1, 1.0))
        self.assertFalse(game.shoot(1, 1.1))  # already in the stroke
        self.assertEqual(game.shots, 1)

    def test_aim_guide_predicts_both_directions_after_a_cut_shot(self):
        game = BilliardsMotion((0, 0, 1600, 1000), [(64, 64)] * 2)
        target, blocker = game.icons
        target.x, target.y = game.white.x + 240, game.white.y + 15
        contact_radius = target.radius + game.white.radius
        forward = math.sqrt(contact_radius ** 2 - 15 ** 2)
        direction = forward / contact_radius, 15 / contact_radius
        blocker.x = target.x + 220 * direction[0]
        blocker.y = target.y + 220 * direction[1]
        game.aim_at(target.x, game.white.y)
        game.charge = .35  # Charge does not change the direction guide.
        incoming, white, outgoing = game.aim_guide()
        self.assertEqual([segment[0] for segment in (incoming, white, outgoing)], ['incoming', 'white', 'target'])
        self.assertAlmostEqual(incoming[2][0], target.x - forward)
        self.assertEqual(white[1], incoming[2])
        self.assertLess(white[2][1], game.white.y)
        self.assertEqual(outgoing[1], (target.x, target.y))
        self.assertAlmostEqual(math.dist(outgoing[2], (blocker.x, blocker.y)), target.radius + blocker.radius)
        game.shoot(.5)
        self.assertEqual(game.aim_guide(), ())

    def test_aim_guide_rebounds_to_next_contact_independent_of_charge(self):
        game = BilliardsMotion((0, 0, 4200, 700), [(64, 64)])
        left, _, right, _ = game.bounds
        game.white.x = right - 180
        game.icons[0].y = game.white.y - 150
        game.aim_at(right, game.white.y)
        baseline = game.aim_preview()
        for charge in (0., .25, .5, 1., .75, 0.):
            with self.subTest(charge=charge):
                game.charge = charge
                self.assertEqual(game.aim_preview(), baseline)
                incoming, rebound = game.aim_guide()
                self.assertAlmostEqual(incoming[2][0], right - game.white.radius)
                self.assertEqual(rebound[0], 'white')
                self.assertEqual(rebound[1], incoming[2])
                self.assertEqual(rebound[2][1], game.white.y)
                self.assertAlmostEqual(rebound[2][0], left + game.white.radius)

    def test_unheld_aim_reaches_ball_and_charge_does_not_change_collision_directions(self):
        game = BilliardsMotion((0, 0, 1920, 1080), [(64, 64)])
        target = game.icons[0]
        target.x, target.y = game.white.x + 400, game.white.y + 25
        game.aim_at(target.x, game.white.y)
        baseline = game.aim_preview()
        self.assertEqual([role for role, _, _ in baseline[0]], ['incoming', 'white', 'target'])
        self.assertEqual([role for role, _, _ in baseline[1]], ['ghost', 'target'])
        for charge in (.01, .3, .8, 1., .5, 0.):
            game.charge = charge
            self.assertEqual(game.aim_preview(), baseline)
        for state in ('stroke', 'rolling', 'finished'):
            game.state = state
            self.assertEqual(game.aim_preview(), ((), ()))

    def test_cut_shot_direction_matches_guide_at_different_frame_rates(self):
        for fps in (30, 60, 144):
            for offset in (15, 50, 65, 68):
                with self.subTest(fps=fps, offset=offset):
                    game = BilliardsMotion((0, 0, 1600, 1000), [(64, 64)])
                    target = game.icons[0]
                    target.x, target.y = game.white.x + 240, game.white.y + offset
                    game.aim, game.charge = (1., 0.), 1.
                    line = next(line for line in game.aim_guide() if line[0] == 'target')
                    expected = math.atan2(line[2][1] - line[1][1], line[2][0] - line[1][0])
                    game.shoot(1)
                    for frame in range(fps):
                        game.step(1 / fps, frame / fps)
                        if math.hypot(target.vx, target.vy) > 0:
                            break
                    self.assertGreater(math.hypot(target.vx, target.vy), 0)
                    self.assertAlmostEqual(math.atan2(target.vy, target.vx), expected, delta=.005)

    def test_ball_grazing_a_middle_pocket_is_not_sucked_in(self):
        for work in ((0, 0, 1600, 1000), (0, 0, 1366, 728)):
            for pocket_index, velocity in ((1, (700, 0)), (4, (-700, 0))):
                for sign in (1, -1):
                    with self.subTest(work=work, pocket=pocket_index, sign=sign):
                        game = BilliardsMotion(work, [(64, 64)] * 15)
                        game.sunk = set(range(2, 16))
                        ball = game.icons[0]
                        px, py = game.pockets[pocket_index]
                        ball.x, ball.y = px - sign * 150, py + sign * ball.radius
                        ball.vx, ball.vy = velocity[0] * sign, velocity[1]
                        game.state = 'rolling'
                        now = 0.
                        for frame in range(120):
                            now += 1 / 60
                            game.step(1 / 60, now)
                            if 1 in game.sunk:
                                break
                        self.assertNotIn(1, game.sunk)

    def test_a_ball_aimed_at_a_middle_pocket_still_sinks(self):
        game = BilliardsMotion((0, 0, 1600, 1000), [(64, 64)] * 15)
        game.sunk = set(range(2, 16))
        ball = game.icons[0]
        px, py = game.pockets[1]
        ball.x, ball.y = px, py + 250
        ball.vx, ball.vy = 0, -900
        game.state = 'rolling'
        now = 0.
        for frame in range(120):
            now += 1 / 60
            game.step(1 / 60, now)
            if 1 in game.sunk:
                break
        self.assertIn(1, game.sunk)

    def test_all_corner_mouths_accept_paths_predicted_to_sink_on_small_screens(self):
        for work in ((0, 0, 1366, 728), (-1024, 0, 0, 728), (0, 0, 1920, 1080)):
            for corner in (0, 2, 3, 5):
                for fps in (30, 144):
                    with self.subTest(work=work, corner=corner, fps=fps):
                        game = BilliardsMotion(work, [(64, 64)] * 15)
                        game.sunk = set(range(2, 16))
                        ball = game.icons[0]
                        px, py = game.pockets[corner]
                        dx = 1 if corner in (0, 3) else -1
                        dy = 1 if corner in (0, 2) else -1
                        ball.x, ball.y = px + dx * 150, py + dy * 150
                        velocity = (-dx * 700, -dy * 700)
                        _, kind, _, _ = game._guide_leg((ball.x, ball.y), velocity, ball.radius, [])
                        self.assertEqual(kind, 'pocket')
                        ball.vx, ball.vy = velocity
                        game.state = 'rolling'
                        for frame in range(fps):
                            game.step(1 / fps, frame / fps)
                            if 1 in game.sunk:
                                break
                        self.assertIn(1, game.sunk)

    def test_reset_restores_the_original_rack(self):
        game = BilliardsMotion((0, 0, 1600, 1000), [(64, 64), (128, 76), (48, 96)])
        original = [(body.x, body.y) for body in game.bodies]
        original_poses = list(game.poses)
        game.shoot(1)
        for frame in range(30):
            game.step(.016, frame * .016)
        game.sunk.add(1)
        game.white_sunk = True
        game.reset()
        self.assertEqual(game.state, 'ready')
        self.assertEqual(game.sunk, set())
        self.assertFalse(game.white_sunk)
        self.assertTrue(game.break_shot)
        self.assertEqual([(body.x, body.y) for body in game.bodies], original)
        self.assertEqual(game.poses, original_poses)

    def test_aim_at_ignores_states_other_than_ready(self):
        game = BilliardsMotion((0, 0, 1600, 1000), [(64, 64)] * 2)
        game.aim_at(game.white.x + 100, game.white.y)
        self.assertAlmostEqual(game.aim[0], 1., delta=.01)
        self.assertAlmostEqual(game.aim[1], 0., delta=.01)
        game.state = 'rolling'
        game.aim_at(game.white.x - 100, game.white.y)
        self.assertAlmostEqual(game.aim[0], 1., delta=.01)


if __name__ == '__main__':
    unittest.main()


class WindowResizeTests(unittest.TestCase):
    def test_rescaled_keeps_round_state_and_maps_positions(self):
        game = BilliardsMotion((0, 0, 1000, 500), [(64, 64)] * 12)
        game.shoot(0.8, 1.0)
        game.sunk.add(3)
        game.scores_marker = 'ignored'
        old_serial, old_shots = game.round_serial, game.shots
        old_x = game.white.x / game.width
        fresh = game.rescaled((0, 0, 1600, 800))
        self.assertIsNot(fresh, game)
        self.assertEqual(fresh.round_serial, old_serial)
        self.assertEqual(fresh.shots, old_shots)
        self.assertEqual(fresh.sunk, {3})
        self.assertEqual(fresh.state, 'rolling')
        self.assertAlmostEqual(fresh.white.x / fresh.width, old_x, delta=1e-6)
        self.assertEqual(fresh.work_area, (0, 0, 1600, 800))
        self.assertEqual(game.work_area, (0, 0, 1000, 500))

    def test_rescaled_same_area_returns_self(self):
        game = BilliardsMotion((0, 0, 800, 400), [(64, 64)] * 8)
        self.assertIs(game.rescaled((0, 0, 800, 400)), game)

    def test_rescaled_copies_sunk_set_instead_of_aliasing_it(self):
        game = BilliardsMotion((0, 0, 1000, 500), [(64, 64)] * 12)
        game.sunk.add(3)
        fresh = game.rescaled((0, 0, 1600, 800))
        self.assertEqual(fresh.sunk, {3})
        self.assertIsNot(fresh.sunk, game.sunk)
        fresh.sunk.add(4)
        fresh.exits[1] = (1., 2., 3., 4., 0.)
        self.assertEqual(game.sunk, {3})
        game.sunk.add(5)
        self.assertEqual(fresh.sunk, {3, 4})


class PocketMouthWallTests(unittest.TestCase):
    """A pocket opens one rail (or a corner); the other rails still rebound."""

    def _game(self):
        return BilliardsMotion((0, 0, 1600, 1000), [(64, 64)] * 2)

    def test_side_pocket_mouth_clamps_the_perpendicular_rail(self):
        game = self._game()
        ball = game.white
        left, top, right, _ = game.bounds
        mid = (left + right) / 2
        ball.x, ball.y = left + ball.radius - 2, top + 120
        ball.vx, ball.vy = -300., 0.
        with patch.object(game, '_boundary_event', return_value=(4., 'pocket', (mid, top))):
            game._walls(ball)
        self.assertAlmostEqual(ball.x, left + ball.radius)
        self.assertAlmostEqual(ball.vx, 300. * RAIL_REBOUND)
        self.assertAlmostEqual(ball.y, top + 120)
        self.assertEqual(ball.vy, 0.)

    def test_side_pocket_mouth_leaves_the_open_axis_unclamped(self):
        game = self._game()
        ball = game.white
        left, top, right, _ = game.bounds
        mid = (left + right) / 2
        ball.x, ball.y = mid, top + ball.radius - 2
        ball.vx, ball.vy = 0., -300.
        with patch.object(game, '_boundary_event', return_value=(4., 'pocket', (mid, top))):
            game._walls(ball)
        self.assertAlmostEqual(ball.y, top + ball.radius - 2)
        self.assertEqual(ball.vy, -300.)

    def test_corner_pocket_mouth_keeps_both_rails_open(self):
        game = self._game()
        ball = game.white
        left, top, _, _ = game.bounds
        ball.x, ball.y = left + ball.radius - 2, top + ball.radius - 2
        ball.vx, ball.vy = -300., -300.
        with patch.object(game, '_boundary_event', return_value=(4., 'pocket', (left, top))):
            game._walls(ball)
        self.assertLess(ball.x, left + ball.radius)
        self.assertLess(ball.y, top + ball.radius)
        self.assertEqual((ball.vx, ball.vy), (-300., -300.))

    def test_rails_without_a_mouth_still_rebound_both_axes(self):
        game = self._game()
        ball = game.white
        left, top, _, _ = game.bounds
        ball.x, ball.y = left + ball.radius - 2, top + ball.radius - 2
        ball.vx, ball.vy = -300., -300.
        with patch.object(game, '_boundary_event', return_value=(4., 'rail', (True, True))):
            game._walls(ball)
        self.assertAlmostEqual(ball.x, left + ball.radius)
        self.assertAlmostEqual(ball.y, top + ball.radius)
        self.assertAlmostEqual(ball.vx, 300. * RAIL_REBOUND)
        self.assertAlmostEqual(ball.vy, 300. * RAIL_REBOUND)

    def test_real_side_and_corner_shots_still_sink(self):
        for work, pocket_index in (((0, 0, 1600, 1000), 1), ((0, 0, 1600, 1000), 0)):
            with self.subTest(pocket=pocket_index):
                game = BilliardsMotion(work, [(64, 64)] * 15)
                game.sunk = set(range(2, 16))
                ball = game.icons[0]
                px, py = game.pockets[pocket_index]
                ball.x, ball.y = px, py + 150
                ball.vx, ball.vy = 0., -900.
                game.state = 'rolling'
                for frame in range(120):
                    game.step(1 / 60, frame / 60)
                    if 1 in game.sunk:
                        break
                self.assertIn(1, game.sunk)


class CloneLightTests(unittest.TestCase):
    """A clone is a scratch round: no shared mutable state and no UI callbacks."""

    @staticmethod
    def _state(table):
        return (table.state, table.shots, table.clock, table.round_serial,
                tuple(sorted(table.sunk)), dict(table.exits), tuple(table.aim),
                tuple((body.x, body.y, body.vx, body.vy) for body in table.bodies))

    @staticmethod
    def _settle(table, start):
        for frame in range(900):
            table.step(1 / 60, start + (frame + 1) / 60)
            if table.state in ('ready', 'finished'):
                return

    def test_clone_copies_bodies_and_mutable_collections_without_aliasing(self):
        game = BilliardsMotion((0, 0, 1600, 1000), [(64, 64)] * 4)
        game.sunk.add(2)
        game.exits[3] = (1., 2., 3., 4., .1)
        game.white.vx, game.white.vy = 11., -7.
        clone = game.clone_light()
        self.assertIsNot(clone, game)
        self.assertIsNot(clone.white, game.white)
        self.assertIs(clone.bodies[0], clone.white)
        self.assertIsNot(clone.sunk, game.sunk)
        self.assertIsNot(clone.exits, game.exits)
        self.assertIsNot(clone.poses, game.poses)
        self.assertIsNot(clone.sizes, game.sizes)
        self.assertIsNot(clone.rack, game.rack)
        for source, copied in zip(game.bodies, clone.bodies):
            self.assertIsNot(source, copied)
            self.assertEqual((source.x, source.y, source.vx, source.vy, source.angle, source.impact),
                             (copied.x, copied.y, copied.vx, copied.vy, copied.angle, copied.impact))
        clone.white.vx = 999.
        clone.icons[0].x += 50
        clone.sunk.add(4)
        clone.exits[3] = (0., 0., 0., 0., 0.)
        clone.poses.clear()
        clone.sizes.clear()
        self.assertEqual((game.white.vx, game.icons[0].x), (11., game.rack[0][0]))
        self.assertEqual(game.sunk, {2})
        self.assertEqual(game.exits[3], (1., 2., 3., 4., .1))
        self.assertEqual(len(game.poses), 4)
        self.assertEqual(len(game.sizes), 4)

    def test_clone_has_no_snapshot_sink_and_never_publishes_a_reset(self):
        snapshots = []
        game = BilliardsMotion((0, 0, 1600, 1000), [(64, 64)] * 4)
        game.snapshot_sink = snapshots.append
        with patch.object(BilliardsMotion, '__init__', side_effect=AssertionError('__init__ called')):
            clone = game.clone_light()
        self.assertIsNone(clone.snapshot_sink)
        serial = game.round_serial
        clone.reset()
        self.assertEqual(snapshots, [])
        self.assertEqual(game.round_serial, serial)
        self.assertEqual(clone.round_serial, serial + 1)

    def test_clone_can_shoot_and_settle_without_changing_the_source(self):
        game = BilliardsMotion((0, 0, 1600, 1000), [(64, 64)] * 3)
        game.sunk.add(3)
        clone = game.clone_light()
        before = self._state(game)
        self.assertTrue(clone.shoot(.8, 1.))
        self._settle(clone, 1.)
        self.assertIn(clone.state, ('ready', 'finished'))
        self.assertEqual(self._state(game), before)
        self.assertEqual(clone.sunk, {3})

    def test_clone_works_after_rescaled_and_after_reset(self):
        game = BilliardsMotion((0, 0, 1200, 600), [(64, 64)] * 3)
        game.shoot(.6, 1.)
        game.sunk.add(2)
        clone = game.clone_light()
        before = self._state(game)

        scaled = clone.rescaled((0, 0, 1600, 800))
        self.assertIsNot(scaled, clone)
        self.assertIsNone(scaled.snapshot_sink)
        self._settle(scaled, 1.)
        self.assertIn(scaled.state, ('ready', 'finished'))

        clone.reset()
        self.assertTrue(clone.shoot(.7, 20.))
        self._settle(clone, 20.)
        self.assertIn(clone.state, ('ready', 'finished'))
        self.assertEqual(self._state(game), before)


class TelemetryTests(unittest.TestCase):
    """Per-shot physical telemetry used by the planner and the foul/combo checks."""

    @staticmethod
    def _game(icon_count=1):
        return BilliardsMotion((0, 0, 1600, 1000), [(64, 64)] * icon_count)

    @staticmethod
    def _settle(table, start=1.):
        for frame in range(900):
            table.step(1 / 60, start + (frame + 1) / 60)
            if table.state in ('ready', 'finished'):
                return

    def test_air_shot_has_no_contact_no_rail_and_no_pot(self):
        game = self._game()
        game.aim = (1., 0.)
        self.assertTrue(game.shoot(0., 1.))
        self._settle(game)
        self.assertEqual(game.state, 'ready')
        self.assertFalse(game.hit_any_ball)
        self.assertIsNone(game.first_target_hit)
        self.assertEqual(game.rail_hits, 0)
        self.assertEqual(game.pocket_order, [])
        self.assertIsNone(game.rails_before_first_pot)

    def test_rail_only_shot_counts_a_real_rebound(self):
        game = self._game(2)
        game.aim = (0., -1.)
        self.assertTrue(game.shoot(.3, 1.))
        self._settle(game)
        self.assertGreaterEqual(game.rail_hits, 1)
        self.assertFalse(game.hit_any_ball)
        self.assertEqual(game.pocket_order, [])

    def test_first_target_hit_uses_the_body_index_of_the_cue_target(self):
        game = self._game(2)
        game.aim = (1., 0.)
        self.assertTrue(game.shoot(.7, 1.))
        self._settle(game)
        self.assertTrue(game.hit_any_ball)
        self.assertEqual(game.first_target_hit, 1)

    def test_one_continuous_contact_is_recorded_once_across_substeps(self):
        game = self._game()
        game.state = 'rolling'
        target = game.icons[0]
        game.white.vx = 300.
        target.x = game.white.x + game.white.radius + target.radius - 1
        target.y = game.white.y
        with patch.object(game, '_record_ball_contact',
                          wraps=game._record_ball_contact) as recorded:
            game._collide(game.white, target)
            for frame in range(30):
                game.step(1 / 240, 1 + frame / 240)
        self.assertEqual(recorded.call_count, 1)
        self.assertTrue(game.hit_any_ball)
        self.assertEqual(game.first_target_hit, 1)

    def test_pocket_order_and_rails_before_first_pot_are_chronological(self):
        game = self._game(2)
        game.state = 'rolling'
        game.rail_hits = 2
        near, far = game.icons[1], game.icons[0]
        near_px, near_py = game.pockets[1]
        far_px, far_py = game.pockets[0]
        near.x, near.y, near.vx, near.vy = near_px, near_py + 60, 0., -900.
        far.x, far.y, far.vx, far.vy = far_px, far_py + 150, 0., -900.
        for frame in range(120):
            game.step(1 / 60, 1 + frame / 60)
            if len(game.pocket_order) == 2:
                break
        self.assertEqual(game.pocket_order, [2, 1])
        self.assertEqual(game.rails_before_first_pot, 2)

    def test_new_shoot_resets_all_shot_telemetry(self):
        game = self._game()
        game.state = 'ready'
        game.hit_any_ball = True
        game.first_target_hit = 1
        game.rail_hits = 4
        game.rails_before_first_pot = 3
        game.pocket_order.extend([1])
        game._contact_guard.add((0, 1))
        self.assertTrue(game.shoot(.5, 1.))
        self.assertFalse(game.hit_any_ball)
        self.assertIsNone(game.first_target_hit)
        self.assertEqual(game.rail_hits, 0)
        self.assertIsNone(game.rails_before_first_pot)
        self.assertEqual(game.pocket_order, [])
        self.assertEqual(game._contact_guard, set())

    def test_clone_copies_telemetry_without_aliasing_its_collections(self):
        game = self._game(2)
        game.hit_any_ball = True
        game.first_target_hit = 2
        game.rail_hits = 3
        game.rails_before_first_pot = 1
        game.pocket_order.extend([2, 1])
        game._contact_guard.add((0, 2))
        clone = game.clone_light()
        self.assertEqual((clone.hit_any_ball, clone.first_target_hit, clone.rail_hits,
                          clone.rails_before_first_pot, clone.pocket_order, clone._contact_guard),
                         (True, 2, 3, 1, [2, 1], {(0, 2)}))
        clone.pocket_order.append(1)
        clone._contact_guard.add((1, 2))
        clone.rail_hits += 1
        self.assertEqual(game.pocket_order, [2, 1])
        self.assertEqual(game._contact_guard, {(0, 2)})
        self.assertEqual(game.rail_hits, 3)
