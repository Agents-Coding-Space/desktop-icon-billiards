"""Hot-seat solo bot: the human and the bot alternate shots on the same table.

Rules (deliberately simple, documented in the README):

- The human always breaks. Pocket an icon and you keep the turn; miss or scratch
  and the turn passes. A scratched cue ball is re-spotted by the engine.
- Every pocketed icon scores 1 point for whoever took that shot.
- Difficulty controls three knobs: aim error (radians, std. dev. of a Gaussian
  offset from the ideal direction), power error (std. dev. of the Gaussian offset
  from the ideal charge) and decision delay (seconds, uniform wait between aiming
  and striking). Levels also unlock tactical layers and a bounded physics rollout;
  Extreme keeps a small residual error instead of playing a perfectly exact line.
"""
from __future__ import annotations

import math
import queue
import random
import threading
import time

from .commentary import ShotReport
from .table import BALL_IMPULSE, MIN_DIRECT_ALIGNMENT, POCKET_ENTRY_ALIGNMENT, RAIL_REBOUND, ROLLING_DRAG

MODES = ('off', 'easy', 'medium', 'hard', 'extreme')
# aim_error: radians; power_error: charge 0..1; delay: seconds range before the strike.
# cluster/safety: U4 escape layers. plants: U5 combo layer. banks: U5 cue kick.
# target_bank: U3 target-ball cushion bank. rollout_k/lookahead: U6 bounded rollout.
SETTINGS = {
    'easy': {'aim_error': 0.14, 'power_error': 0.15, 'delay': (1.0, 1.8),
             'cluster': False, 'safety': False, 'plants': False, 'banks': False,
             'target_bank': False, 'rollout_k': 0, 'lookahead': False},
    'medium': {'aim_error': 0.055, 'power_error': 0.07, 'delay': (0.65, 1.2),
               'cluster': True, 'safety': False, 'plants': False, 'banks': False,
               'target_bank': False, 'rollout_k': 0, 'lookahead': False},
    'hard': {'aim_error': 0.018, 'power_error': 0.025, 'delay': (0.35, 0.7),
             'cluster': True, 'safety': False, 'plants': True, 'banks': False,
             'target_bank': True, 'rollout_k': 5, 'lookahead': False},
    'extreme': {'aim_error': 0.002, 'power_error': 0.005, 'delay': (0.18, 0.35),
                'cluster': True, 'safety': True, 'plants': True, 'banks': True,
                'target_bank': True, 'rollout_k': 12, 'lookahead': True},
}
LABELS = {'off': 'Bot Off', 'easy': 'Easy', 'medium': 'Medium', 'hard': 'Hard', 'extreme': 'Extreme'}

# Escape fallback: a visible ball inside a tight group is worth a firm break.
CLUSTER_RADIUS_FACTOR = 3.5
DENSE_CLUSTER_MIN = 2
BREAK_CHARGE_MIN, BREAK_CHARGE_MAX = 0.65, 0.85
CLUSTER_BONUS_PER_BALL = 120.0  # pixel-scale preference for breaking a bigger group
PARK_BONUS_FACTOR = 0.6         # pixel-scale reward for cue clearance after a nudge
SAFETY_ROLL_FACTOR = 0.22       # sampled cue travel after contact, as a share of table height
SCORE_EPSILON = 1e-9            # mirrored racks score equal; keep the first candidate

# Combo/plant: cue -> driver A -> receiver B -> pocket. Both transfers need a
# real forward component, and A must stay close enough to B that the second
# contact is a carom rather than two unrelated shots.
PLANT_TRANSFER_ALIGNMENT = .45
PLANT_MAX_SEPARATION_FACTOR = 5.0  # in receiver radii
PLANT_SCORE_FACTOR = .8            # cushion banks use 1.1; two-ball rolls stay preferred
# Kick: one rail rebound before cue/target contact. Gated by the existing
# ``banks`` flag; U6 owns any per-level retuning.
KICK_SCORE_FACTOR = 2.0            # a rebound is less reliable than a target-ball bank

# U6 rollout: hard bounds for the background physics check and its value model.
ROLLOUT_TIME_BUDGET = 0.16          # seconds of wall time for one worker run
ROLLOUT_MAX_STEPS = 90             # simulated frames (60 Hz) per candidate
ROLLOUT_POT_VALUE = 1000.0         # value of one extra pocketed icon
ROLLOUT_SCRATCH_PENALTY = 400.0    # value lost when the cue is sunk
ROLLOUT_SPREAD_WEIGHT = 40.0       # reward for leaving the rack spread out
ROLLOUT_POSITION_WEIGHT = 60.0     # reward for the best next direct pot

# U7 ball-in-hand: medium follows the easiest target/pocket axis; hard and
# extreme sample 6 lines x 6 distances = 36 axis points. Every point is offered
# to ``table.place_white`` so bounds, pockets and overlap stay engine-owned.
PLACEMENT_EASY_MODES = frozenset(('off', 'easy'))
PLACEMENT_MEDIUM_FACTORS = (3.0, 4.2, 5.4)
PLACEMENT_SAMPLE_FACTORS = (2.5, 3.2, 3.9, 4.6, 5.3, 6.0)
PLACEMENT_MEDIUM_LINES = 1
PLACEMENT_SAMPLE_LINES = 6
PLACEMENT_POSITION_FACTOR = .05    # pixel-scale tie-break for the next shot


def other(player):
    return 'bot' if player == 'human' else 'human'


def cluster_density(table, target):
    """Count active balls around ``target`` within ``CLUSTER_RADIUS_FACTOR`` radii."""
    reach = CLUSTER_RADIUS_FACTOR * target.radius
    return sum(1 for index, body in enumerate(table.icons, 1)
               if body is not target and index not in table.sunk
               and math.hypot(body.x - target.x, body.y - target.y) <= reach)


def rollout_value(clone, initial_potted, *, lookahead=False):
    """Value a simulated shot from the settled (or step-capped) clone state.

    Higher is better. Every term is bounded so a scratch cannot be masked by a
    very large cluster-spread reward. ``lookahead=True`` adds a 1-ply positional
    term: the best direct pot available from where the cue stopped.
    """
    value = (clone.pocketed_icons - initial_potted) * ROLLOUT_POT_VALUE
    if clone.white_sunk:
        value -= ROLLOUT_SCRATCH_PENALTY
    active = [body for index, body in enumerate(clone.icons, 1) if index not in clone.sunk]
    if not active:
        return value
    gaps = []
    for body in active:
        nearest = min((math.hypot(body.x - other.x, body.y - other.y)
                       for other in active if other is not body), default=2 * body.radius)
        gaps.append(nearest / max(1.0, body.radius))
    value += ROLLOUT_SPREAD_WEIGHT * (sum(gaps) / len(gaps))
    if lookahead and clone.state in ('ready', 'finished'):
        best = None
        for target in active:
            dx, dy = target.x - clone.white.x, target.y - clone.white.y
            distance = math.hypot(dx, dy)
            if distance < 1:
                continue
            ux, uy = dx / distance, dy / distance
            for px, py in clone.pockets:
                pd = math.hypot(px - target.x, py - target.y)
                if pd < 1:
                    continue
                tx, ty = (px - target.x) / pd, (py - target.y) / pd
                if tx * ux + ty * uy <= MIN_DIRECT_ALIGNMENT:
                    continue
                others = [body for body in active if body is not target]
                score = (distance + pd) / max(0.28, tx * ux + ty * uy)
                if clone._clear(clone.white.x, clone.white.y,
                                target.x - tx * (target.radius + clone.white.radius),
                                target.y - ty * (target.radius + clone.white.radius),
                                others, clone.white.radius):
                    best = score if best is None else min(best, score)
        if best is not None:
            value += ROLLOUT_POSITION_WEIGHT / (1.0 + best / max(1.0, clone.height))
    return value


def rollout_worker(generation, round_serial, table_state, shot_count, jobs, results,
                   *, budget, max_steps, lookahead):
    """Simulate candidate shots on private clones; post one aggregated result.

    This function never receives the live table. ``jobs`` holds independent
    ``clone_light()`` copies already aimed and struck by the animation thread.
    The time and step caps are hard; an unfinished shot is still scored from its
    partial state so the worker always returns inside the budget.
    """
    # Yield once before the CPU-bound loop: without this, Thread.start() can
    # block the caller for a whole GIL slice while the worker begins stepping.
    time.sleep(0.0005)
    started = time.monotonic()
    deadline = started + max(0.0, budget)
    best = None
    try:
        for candidate, clone in jobs:
            if time.monotonic() >= deadline:
                break
            initial_potted = clone.pocketed_icons
            now = clone.clock
            frames = 0
            while (clone.state in ('stroke', 'rolling') and frames < max_steps
                   and (frames % 16 or time.monotonic() < deadline)):
                now += 1 / 60
                clone.step(1 / 60, now)
                frames += 1
                if frames % 16 == 0:
                    time.sleep(0)  # let the animation thread keep its frames
            value = rollout_value(clone, initial_potted, lookahead=lookahead)
            if best is None or value > best[0]:
                best = (value, candidate, frames)
    except Exception:
        best = None  # a failed background calculation must never kill the game
    finally:
        elapsed = time.monotonic() - started
        results.put((generation, round_serial, table_state, shot_count,
                     best[1] if best else None, best[0] if best else None, elapsed))


class BotController:
    """Decide when and how the bot shoots. The table remains the single source of truth."""

    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.mode = 'off'
        self.turn = 'human'
        self.scores = {'human': 0, 'bot': 0}
        self._round = None
        self._shots_seen = 0
        self._pocketed_seen = 0
        self._last_shooter = 'human'
        self._shot_pending = False
        self._shot_gained = 0
        self._shot_scratch = False
        self._next_action_at = None
        self._rollout_results = queue.SimpleQueue()
        self._rollout_generation = 0
        self._rollout_pending = False
        self._rollout_pick = None
        self._rollout_last_ms = 0.0
        self._rollout_fallback = None
        # Step 2: one immutable report per settled shot, delivered through a sink.
        self.report_sink = None
        self.report_reset_sink = None
        self._next_action_aim = 0.0
        self._pending_aim_seconds = 0.0
        self._pending_power = 0.0
        self._shot_aim_seconds = 0.0
        self._shot_power = 0.0
        self._shooter_streak = 0
        self._miss_streak = 0
        self._streak_shooter = None
        self._reported_shot = None

    def note_shot_input(self, aim_seconds, power):
        """Record input metadata for the next shot seen by :meth:`sync`.

        The overlay calls this after a human release; :meth:`_shoot` calls it
        for bot shots with the configured decision delay. Values are consumed
        once, when the shot counter advances.
        """
        try:
            self._pending_aim_seconds = max(0.0, float(aim_seconds))
        except (TypeError, ValueError):
            self._pending_aim_seconds = 0.0
        try:
            self._pending_power = min(1.0, max(0.0, float(power)))
        except (TypeError, ValueError):
            self._pending_power = 0.0

    def reset_report_state(self):
        """Forget streaks and pending input without touching physics state."""
        self._pending_aim_seconds = self._pending_power = 0.0
        self._shot_aim_seconds = self._shot_power = 0.0
        self._next_action_aim = 0.0
        self._shooter_streak = self._miss_streak = 0
        self._streak_shooter = None
        self._reported_shot = None

    def _emit_shot_report(self, table):
        """Build one frozen report from settled table telemetry and turn state."""
        shooter = self._last_shooter
        pocketed = max(0, int(self._shot_gained))
        scratched = bool(self._shot_scratch)
        successful = pocketed > 0 and not scratched
        if successful:
            self._shooter_streak = (self._shooter_streak + 1
                                    if self._streak_shooter == shooter else 1)
            self._streak_shooter = shooter
            self._miss_streak = 0
        else:
            self._shooter_streak = 0
            self._streak_shooter = None
            self._miss_streak += 1

        first_hit = getattr(table, 'first_target_hit', None)
        pocket_order = getattr(table, 'pocket_order', ()) or ()
        is_combo = bool(pocket_order and first_hit is not None and pocket_order[0] != first_hit)
        rails_before = getattr(table, 'rails_before_first_pot', None)
        rail_hits = int(getattr(table, 'rail_hits', 0) or 0)
        rail_before_pot = rail_hits if rails_before is None else int(rails_before)
        icon_count = getattr(table, 'icon_count', None)
        if icon_count is None:
            icon_count = len(getattr(table, 'icons', ()) or ())
        remaining = max(0, int(icon_count) - int(getattr(table, 'pocketed_icons', 0) or 0))

        report = ShotReport(
            shooter=shooter,
            pocketed=pocketed,
            scratched=scratched,
            hit_any_ball=bool(getattr(table, 'hit_any_ball', False)),
            rail_hits_before_pot=rail_before_pot,
            is_combo=is_combo,
            power=self._shot_power,
            aim_seconds=self._shot_aim_seconds,
            shooter_streak=self._shooter_streak,
            miss_streak=self._miss_streak,
            human_score=self.scores['human'],
            bot_score=self.scores['bot'],
            remaining_balls=remaining,
            bot_mode=self.mode,
        )
        self._reported_shot = (self._round, self._shots_seen)
        if self.report_sink is not None:
            self.report_sink(report)

    # -- configuration ---------------------------------------------------
    def cycle(self):
        """Advance off -> easy -> medium -> hard -> extreme -> off. Returns the new mode."""
        index = MODES.index(self.mode)
        self.mode = MODES[(index + 1) % len(MODES)]
        return self.mode

    @property
    def enabled(self):
        return self.mode != 'off'

    @property
    def bot_turn(self):
        return self.enabled and self.turn == 'bot'

    def label(self):
        return LABELS[self.mode]

    def score_text(self):
        return f'You {self.scores["human"]} - Bot {self.scores["bot"]}'

    def status_text(self):
        if not self.enabled:
            return 'Bot Off'
        if self.turn == 'bot':
            return f'Bot {self.label()} · {self.score_text()}'
        return f'You (Bot {self.label()}) · {self.score_text()}'

    def result_text(self):
        human, bot = self.scores['human'], self.scores['bot']
        if human > bot:
            return f'You win {human}-{bot}'
        if bot > human:
            return f'Bot wins {human}-{bot}'
        return f'Draw {human}-{bot}'

    # -- per-frame update ------------------------------------------------
    def _attach_placement(self, table):
        """Install the optional ball-in-hand hook without touching the GUI loop."""
        if self.mode == 'off':
            return
        try:
            if getattr(table, 'white_placer', None) != self.place_ball_in_hand:
                table.white_placer = self.place_ball_in_hand
        except (AttributeError, TypeError):
            pass

    def sync(self, table, now):
        """Track shots/pockets to attribute points; call once per frame with the live table."""
        self._attach_placement(table)
        if self._round != table.round_serial:
            # A caller may have recorded input for a shot taken just before the
            # first sync of a new round; keep that pending pair for this call.
            pending_input = (self._pending_aim_seconds, self._pending_power)
            self._round = table.round_serial
            self._shots_seen = 0
            self._pocketed_seen = table.pocketed_icons
            self._last_shooter = 'human'
            self._shot_pending = False
            self._shot_gained = self._shot_scratch = 0
            self._next_action_at = None
            self.turn = 'human'
            self.scores = {'human': 0, 'bot': 0}
            self._rollout_generation += 1
            self._rollout_pending = False
            self._rollout_pick = None
            self._rollout_results = queue.SimpleQueue()
            self.reset_report_state()
            if self.report_reset_sink is not None:
                self.report_reset_sink()
            if getattr(table, 'shots', 0) > 0:
                self._pending_aim_seconds, self._pending_power = pending_input
        # Billiard turn rule: pocket a ball and you keep shooting; miss (or scratch) and you pass.
        while table.shots > self._shots_seen:
            self._last_shooter = self.turn
            self._shot_pending = True
            self._shot_gained = 0
            self._shot_scratch = False
            self._shot_aim_seconds = self._pending_aim_seconds
            self._shot_power = self._pending_power
            self._pending_aim_seconds = self._pending_power = 0.0
            self._shots_seen += 1
            self._next_action_at = None
        sunk = table.pocketed_icons
        if sunk > self._pocketed_seen and self._shot_pending:
            gained = sunk - self._pocketed_seen
            self._shot_gained += gained
            if self.enabled:
                self.scores[self._last_shooter] += gained
        self._pocketed_seen = sunk
        if getattr(table, 'white_sunk', False):
            self._shot_scratch = True
        # Physics ends in 'ready', 'return_white' (scratch) or 'finished';
        # 'ball_in_hand' is the same settled shot awaiting cue placement.
        if self._shot_pending and table.state in ('ready', 'return_white', 'ball_in_hand', 'finished'):
            key = (self._round, self._shots_seen)
            if self._reported_shot != key:
                self._emit_shot_report(table)
            # Turn handover stays at 'ready', as before; the scratch report is
            # already out while the cue waits to be re-spotted.
            if table.state in ('ready', 'finished'):
                if self._shot_scratch or self._shot_gained == 0:
                    self.turn = other(self.turn)
                self._shot_pending = False
                self._next_action_at = None

    # -- ball in hand -----------------------------------------------------
    def _ball_in_hand_owner(self, table):
        """Return the player entitled to place the cue for the settled scratch.

        The engine fires ``white_placer`` on the return_white -> ball_in_hand
        transition, while ``turn`` still names the shooter. Using ``bot_turn``
        here would let a scratching bot place for the human (and vice versa).
        ``_last_shooter`` is recorded by ``sync()`` when the shot counter
        advances and is the shooter of the shot that just settled; billiards
        gives ball-in-hand to the opponent. Return None when no scratched shot
        is pending, so the default re-spot wins.
        """
        if not (self._shot_pending and self._shot_scratch
                and getattr(table, 'white_sunk', False)):
            return None
        return other(self._last_shooter)

    def place_ball_in_hand(self, table):
        """Engine hook: place the cue when the bot is the placer after a scratch.

        Returns True when the hook handled the re-spot, so ``table.step`` leaves
        ``_auto_place()`` alone. Easy (and Bot Off) deliberately return False.
        """
        if self._ball_in_hand_owner(table) != 'bot':
            return False
        if self.mode in PLACEMENT_EASY_MODES:
            return False
        if getattr(table, 'state', None) != 'ball_in_hand':
            return False
        if self.mode == 'medium':
            factors, lines = PLACEMENT_MEDIUM_FACTORS, PLACEMENT_MEDIUM_LINES
        else:
            factors, lines = PLACEMENT_SAMPLE_FACTORS, PLACEMENT_SAMPLE_LINES
        best = None
        for x, y in self._placement_points(table, factors, lines):
            score = self._placement_score(table, x, y)
            if score is None:
                continue
            if best is None or score < best[0]:
                best = (score, x, y)
        if best is not None and table.place_white(best[1], best[2]):
            return True
        table._auto_place()
        return True

    @staticmethod
    def _placement_points(table, factors, lines):
        """Yield unique points behind the nearest target/pocket lines."""
        targets = [body for index, body in enumerate(table.icons, 1) if index not in table.sunk]
        if not targets:
            return
        pairs = []
        for target in targets:
            for px, py in table.pockets:
                distance = math.hypot(px - target.x, py - target.y)
                if distance < 1:
                    continue
                pairs.append((distance, target, (px - target.x) / distance, (py - target.y) / distance))
        pairs.sort(key=lambda item: item[0])
        seen = set()
        for _, target, ux, uy in pairs[:lines]:
            for factor in factors:
                x = target.x - ux * factor * target.radius
                y = target.y - uy * factor * target.radius
                key = (round(x, 3), round(y, 3))
                if key in seen:
                    continue
                seen.add(key)
                yield x, y

    def _placement_score(self, table, x, y):
        """Validate one point with ``place_white`` and value its best shot."""
        wx, wy = table.white.x, table.white.y
        sunk = set(table.sunk)
        white_sunk, state = table.white_sunk, table.state
        ready_for, aim = table.ready_for, table.aim
        if not table.place_white(x, y):
            return None
        try:
            flags = SETTINGS[self.mode]
            candidates = self._candidates(table, flags=flags)
            if candidates:
                score = min(candidate[0] for candidate in candidates)
            else:
                escape = self._escape_shot(table, cluster=bool(flags.get('cluster')),
                                           safety=bool(flags.get('safety')))
                score = escape[0] if escape else float('inf')
            active = [body for index, body in enumerate(table.icons, 1) if index not in table.sunk]
            if active:
                nearest = min(math.hypot(x - body.x, y - body.y) for body in active)
                score += PLACEMENT_POSITION_FACTOR * nearest
            return score
        finally:
            table.white.x, table.white.y = wx, wy
            table.sunk = sunk
            table.white_sunk = white_sunk
            table.state = state
            table.ready_for = ready_for
            table.aim = aim

    def update(self, table, now):
        """Take the bot's shot when it is the bot's turn and the table is ready."""
        self.sync(table, now)
        if not self.bot_turn:
            return False
        if table.state != 'ready':
            return False
        settings = SETTINGS[self.mode]
        self._drain_rollout(table)
        if self._next_action_at is None:
            self._next_action_aim = self.rng.uniform(*settings['delay'])
            self._next_action_at = now + self._next_action_aim
            # Give the bounded worker the whole decision delay to finish; the
            # animation thread does not wait for its result.
            self._prepare_rollout(table, settings)
            return False
        if now < self._next_action_at:
            return False
        self._next_action_at = None
        return self._shoot(table, settings, now)

    # -- shot planning -----------------------------------------------------
    @staticmethod
    def shot_power(table, cue_distance, target_distance=0.0, alignment=0.0):
        """Charge needed for the cue to reach the contact and send the target to the pocket.

        Mirrors the table's linear rolling drag and ``_shot_speed`` conversion, so
        Extreme no longer over-hits every straight shot.
        """
        height = max(1.0, table.height)
        decel = height * ROLLING_DRAG
        target_speed = math.sqrt(max(0.0, 2 * decel * target_distance))
        transfer = max(0.35, abs(alignment) * BALL_IMPULSE)
        cue_at_contact = target_speed / transfer if target_distance else 0.0
        speed = math.sqrt(2 * decel * cue_distance + cue_at_contact ** 2) * 1.06
        charge = (speed / (height * 1.58) - 0.12) / 3.05
        return min(1.0, max(0.22, charge))

    @staticmethod
    def cluster_density(table, target):
        """Expose the module helper through the bot namespace."""
        return cluster_density(table, target)

    @staticmethod
    def _pocket_entry_ok(table, x, y, ux, uy, px, py, radius):
        """Mirror ``table._boundary_event``'s gate for one ball-to-pocket path.

        ``(ux, uy)`` is the unit direction of the ball at ``(x, y)``; ``radius``
        is that ball's radius. A path only counts when it enters the pocket mouth
        before reaching the inset rail, and when it actually heads into the
        pocket. At a corner an open mouth can override the alignment gate, as
        it does in the engine.
        """
        direction = math.hypot(ux, uy)
        if direction <= 1e-9:
            return False
        ux, uy = ux / direction, uy / direction
        entry = table._ray_circle(x, y, ux, uy, px, py, table.pocket_radius + radius * .55)
        if entry is None:
            return False
        left, top, right, bottom = table.bounds
        rail_x = ((right - radius if ux > 0 else left + radius) - x) / ux if abs(ux) > 1e-6 else math.inf
        rail_y = ((bottom - radius if uy > 0 else top + radius) - y) / uy if abs(uy) > 1e-6 else math.inf
        rail = max(0., min(rail_x, rail_y))
        edge_x = ((right if ux > 0 else left) - x) / ux if abs(ux) > 1e-6 else math.inf
        edge_y = ((bottom if uy > 0 else top) - y) / uy if abs(uy) > 1e-6 else math.inf
        edge = min(edge_x, edge_y)
        corner_mouth = (px in (left, right) and math.isfinite(edge) and edge >= 0
                        and (x + ux * edge - px) ** 2 + (y + uy * edge - py) ** 2
                        <= table.pocket_radius ** 2)
        towards = math.hypot(px - x, py - y)
        aligned = ((ux * (px - x) + uy * (py - y)) / towards) if towards > 1e-6 else 1.0
        if corner_mouth:
            aligned = max(aligned, POCKET_ENTRY_ALIGNMENT)
        if aligned < POCKET_ENTRY_ALIGNMENT:
            return False
        return entry <= rail or corner_mouth

    @staticmethod
    def _post_collision(wx, wy, ux, uy):
        """Unit cue direction after a ``BALL_IMPULSE`` hit on contact normal ``(ux, uy)``.

        Returns ``(qx, qy, length)``. The length is needed to compare a pocket's
        sideways offset with its forward distance on the same scale.
        """
        incoming = math.hypot(wx, wy)
        if incoming <= 1e-9:
            return 0., 0., 0.
        nx, ny = wx / incoming, wy / incoming
        dot = nx * ux + ny * uy
        qx, qy = nx - BALL_IMPULSE * dot * ux, ny - BALL_IMPULSE * dot * uy
        return qx, qy, math.hypot(qx, qy)

    @staticmethod
    def _bank_candidates(table, target, pocket, others):
        """Cushion-bank lines for one target/pocket, using the mirror-pocket method."""
        white = table.white
        left, top, right, bottom = table.bounds
        r = target.radius
        contact = r + white.radius
        px, py = pocket
        rails = (
            ('v', left + r, 2 * (left + r) - px, py),
            ('v', right - r, 2 * (right - r) - px, py),
            ('h', top + r, px, 2 * (top + r) - py),
            ('h', bottom - r, px, 2 * (bottom - r) - py),
        )
        candidates = []
        for axis, line, vx, vy in rails:
            source = target.x if axis == 'v' else target.y
            virtual = vx if axis == 'v' else vy
            span = virtual - source
            if abs(span) < 1e-6 or (source - line) * span >= 0:
                continue  # the target and virtual pocket must be on opposite sides
            fraction = (line - source) / span
            if not 0.0 < fraction < 1.0:
                continue
            hx = target.x + fraction * (vx - target.x)
            hy = target.y + fraction * (vy - target.y)
            if not (left + r <= hx <= right - r and top + r <= hy <= bottom - r):
                continue
            leg1 = math.hypot(hx - target.x, hy - target.y)
            leg2 = math.hypot(px - hx, py - hy)
            if leg1 < r or leg2 < r:
                continue
            if not (table._clear(target.x, target.y, hx, hy, others, r)
                    and table._clear(hx, hy, px, py, others, r)):
                continue
            path = math.hypot(vx - target.x, vy - target.y)
            ux, uy = (vx - target.x) / path, (vy - target.y) / path
            gx, gy = target.x - ux * contact, target.y - uy * contact
            if not (left + white.radius <= gx <= right - white.radius
                    and top + white.radius <= gy <= bottom - white.radius):
                continue
            wx, wy = gx - white.x, gy - white.y
            cue_distance = math.hypot(wx, wy)
            if cue_distance < 1:
                continue
            alignment = (wx * ux + wy * uy) / cue_distance
            if alignment <= 0.42:
                continue
            if not table._clear(white.x, white.y, gx, gy, others, white.radius):
                continue
            # Cushion rebound costs speed: the second leg needs more initial energy.
            effective = leg1 + leg2 / (RAIL_REBOUND * RAIL_REBOUND)
            score = ((cue_distance + effective / max(0.5, alignment)) / max(0.28, alignment)
                     + table.height * 1.1)  # direct rolls stay preferred when available
            qx, qy, qlen = BotController._post_collision(wx, wy, ux, uy)
            if qlen > 1e-9:
                for cx, cy in table.pockets:
                    ahead = (cx - gx) * qx + (cy - gy) * qy
                    if ahead > 0:
                        offset = abs((cx - gx) * qy - (cy - gy) * qx) / qlen
                        if offset < table.pocket_radius + white.radius:
                            score += table.height * 1.5
            candidates.append((score, (gx, gy), BotController.shot_power(table, cue_distance,
                                                                 effective, alignment)))
        return candidates

    @staticmethod
    def _plant_candidates(table):
        """Cue -> driver A -> receiver B -> pocket candidates (combos/plants).

        ``driver`` sits near ``receiver``; the cue strikes the driver on the
        A->B normal so the driver then sends B along its pocket line. Each leg
        is checked with ``table._clear`` and the final B->pocket leg reuses the
        U3 pocket-entry gate. Returned ghosts belong to A, the ball the cue
        actually strikes.
        """
        white = table.white
        left, top, right, bottom = table.bounds
        targets = [body for index, body in enumerate(table.icons, 1) if index not in table.sunk]
        if len(targets) < 2:
            return []
        unique = {}
        for receiver in targets:
            for driver in targets:
                if driver is receiver:
                    continue
                separation = math.hypot(receiver.x - driver.x, receiver.y - driver.y)
                if separation > PLANT_MAX_SEPARATION_FACTOR * receiver.radius:
                    continue
                contact_ab = driver.radius + receiver.radius
                if separation < contact_ab * .9:
                    continue
                nx, ny = (receiver.x - driver.x) / separation, (receiver.y - driver.y) / separation
                bgx, bgy = receiver.x - nx * contact_ab, receiver.y - ny * contact_ab
                blockers = [body for body in targets if body is not receiver and body is not driver]
                for px, py in table.pockets:
                    dx, dy = px - receiver.x, py - receiver.y
                    pocket_distance = math.hypot(dx, dy)
                    if pocket_distance < 1:
                        continue
                    ux, uy = dx / pocket_distance, dy / pocket_distance
                    transfer_ab = nx * ux + ny * uy
                    if transfer_ab < PLANT_TRANSFER_ALIGNMENT:
                        continue
                    if not BotController._pocket_entry_ok(
                            table, receiver.x, receiver.y, ux, uy, px, py, receiver.radius):
                        continue
                    if not (table._clear(receiver.x, receiver.y, px, py, blockers, receiver.radius)
                            and table._clear(driver.x, driver.y, bgx, bgy, blockers, driver.radius)):
                        continue
                    contact_ca = driver.radius + white.radius
                    gx, gy = driver.x - nx * contact_ca, driver.y - ny * contact_ca
                    if not (left + white.radius <= gx <= right - white.radius
                            and top + white.radius <= gy <= bottom - white.radius):
                        continue
                    wx, wy = gx - white.x, gy - white.y
                    cue_distance = math.hypot(wx, wy)
                    if cue_distance < 1:
                        continue
                    transfer_cue = (wx * nx + wy * ny) / cue_distance
                    if transfer_cue < PLANT_TRANSFER_ALIGNMENT:
                        continue
                    if not table._clear(white.x, white.y, gx, gy, blockers, white.radius):
                        continue
                    danger, (qx, qy) = BotController._cue_pocket_danger(
                        table, gx, gy, wx, wy, nx, ny)
                    transfer = min(transfer_cue, transfer_ab)
                    driver_roll = math.hypot(bgx - driver.x, bgy - driver.y)
                    effective = driver_roll + pocket_distance / max(.45, transfer_ab ** 2)
                    score = ((cue_distance + effective / max(.5, transfer)) / max(.28, transfer)
                             + table.height * PLANT_SCORE_FACTOR + danger)
                    charge = BotController.shot_power(table, cue_distance, effective, transfer)
                    candidate = (score, (gx, gy), charge)
                    key = (round(gx), round(gy))
                    if key not in unique or candidate[0] < unique[key][0]:
                        unique[key] = candidate
        return list(unique.values())

    @staticmethod
    def _kick_candidates(table, target, pocket, others):
        """One-rail cue rebounds that reach ``target``'s ghost and pot it.

        The cue is mirrored across each inset rail; the straight line from the
        mirror to the target ghost fixes the bounce point. Both cue legs and
        the target's pot line must be clear, so a target hidden behind a
        cluster is only used when the reflected path actually sees it. The
        returned ghost is the rail contact point: aiming at it is collinear
        with aiming at the mirrored target and keeps the aim inside the table.
        """
        white = table.white
        left, top, right, bottom = table.bounds
        r = target.radius
        contact = r + white.radius
        px, py = pocket
        dx, dy = px - target.x, py - target.y
        pocket_distance = math.hypot(dx, dy)
        if pocket_distance < 1:
            return []
        ux, uy = dx / pocket_distance, dy / pocket_distance
        if not BotController._pocket_entry_ok(table, target.x, target.y, ux, uy, px, py, r):
            return []
        gx, gy = target.x - ux * contact, target.y - uy * contact
        if not (left + white.radius <= gx <= right - white.radius
                and top + white.radius <= gy <= bottom - white.radius):
            return []
        if table._clear(white.x, white.y, gx, gy, others, white.radius):
            return []  # a direct line sees the target: no rebound needed
        rails = (('v', left + white.radius), ('v', right - white.radius),
                 ('h', top + white.radius), ('h', bottom - white.radius))
        unique = {}
        for axis, line in rails:
            if axis == 'v':
                mx, my = 2 * line - white.x, white.y
                span = gx - mx
                if abs(span) < 1e-6 or (gx - line) * (white.x - line) <= 0:
                    continue
                fraction = (line - mx) / span
                if not 0. < fraction < 1.:
                    continue
                bx, by = line, my + fraction * (gy - my)
                if not (top + white.radius <= by <= bottom - white.radius):
                    continue
            else:
                mx, my = white.x, 2 * line - white.y
                span = gy - my
                if abs(span) < 1e-6 or (gy - line) * (white.y - line) <= 0:
                    continue
                fraction = (line - my) / span
                if not 0. < fraction < 1.:
                    continue
                bx, by = mx + fraction * (gx - mx), line
                if not (left + white.radius <= bx <= right - white.radius):
                    continue
            if any(math.hypot(bx - cx, by - cy) < table.pocket_radius + white.radius
                   for cx, cy in table.pockets):
                continue  # the cue would drop in the mouth instead of rebounding
            leg1 = math.hypot(bx - white.x, by - white.y)
            leg2 = math.hypot(gx - bx, gy - by)
            if leg1 < white.radius or leg2 < white.radius:
                continue
            if not (table._clear(white.x, white.y, bx, by, others, white.radius)
                    and table._clear(bx, by, gx, gy, others, white.radius)
                    and table._clear(target.x, target.y, px, py, others, r)):
                continue
            ix, iy = (gx - bx) / leg2, (gy - by) / leg2
            alignment = ix * ux + iy * uy
            if alignment <= MIN_DIRECT_ALIGNMENT:
                continue
            cue_distance = leg1 + leg2
            effective = pocket_distance / (RAIL_REBOUND * RAIL_REBOUND)
            score = ((cue_distance + effective / max(.5, alignment)) / max(.28, alignment)
                     + table.height * KICK_SCORE_FACTOR)
            danger, _ = BotController._cue_pocket_danger(table, gx, gy, gx - bx, gy - by, ux, uy)
            score += danger
            candidate = (score, (bx, by), BotController.shot_power(
                table, cue_distance, effective, alignment))
            key = (round(bx), round(by))
            if key not in unique or candidate[0] < unique[key][0]:
                unique[key] = candidate
        return list(unique.values())

    @staticmethod
    def _cue_pocket_danger(table, gx, gy, wx, wy, ux, uy):
        """Pocket exposure and unit direction of the cue after contact.

        ``(wx, wy)`` runs from the cue to the ghost; ``(ux, uy)`` is the contact
        normal. This mirrors the post-collision gate used by the pot layers.
        """
        qx, qy, qlen = BotController._post_collision(wx, wy, ux, uy)
        danger = 0.
        if qlen > 1e-9:
            qx, qy = qx / qlen, qy / qlen
            for px, py in table.pockets:
                ahead = (px - gx) * qx + (py - gy) * qy
                if ahead > 0 and abs((px - gx) * qy - (py - gy) * qx) < table.pocket_radius + table.white.radius:
                    danger += table.height
        return danger, (qx, qy) if qlen > 1e-9 else (0., 0.)

    @staticmethod
    def _parking_clearance(table, gx, gy, qx, qy, others):
        """Clearance of the approximated cue stop to the nearest ball or pocket."""
        roll = SAFETY_ROLL_FACTOR * max(1.0, table.height)
        sx, sy = gx + qx * roll, gy + qy * roll
        gaps = [math.hypot(px - sx, py - sy) - table.pocket_radius
                for px, py in table.pockets]
        gaps.extend(math.hypot(body.x - sx, body.y - sy) - body.radius for body in others)
        return min(gaps, default=0.)

    @staticmethod
    def _cluster_rim_ghost(table, target, others, white, contact):
        """Ghost on the cluster rim, reached from outside the group.

        The outward normal is the target's offset from the neighbour centroid,
        so the contact normal points back into the cluster and breaks it up.
        ``None`` means the rim is unreachable and the plain ghost must be used.
        """
        reach = CLUSTER_RADIUS_FACTOR * target.radius
        neighbours = [body for body in others
                      if math.hypot(body.x - target.x, body.y - target.y) <= reach]
        if not neighbours:
            return None
        cx = sum(body.x for body in neighbours) / len(neighbours)
        cy = sum(body.y for body in neighbours) / len(neighbours)
        nx, ny = target.x - cx, target.y - cy
        length = math.hypot(nx, ny)
        if length < 1e-6:
            return None
        nx, ny = nx / length, ny / length
        gx, gy = target.x + nx * contact, target.y + ny * contact
        left, top, right, bottom = table.bounds
        if not (left + white.radius <= gx <= right - white.radius
                and top + white.radius <= gy <= bottom - white.radius):
            return None
        if not table._clear(white.x, white.y, gx, gy, others, white.radius):
            return None
        return gx, gy, -nx, -ny

    @staticmethod
    def _escape_shot(table, *, cluster=True, safety=True):
        """Break a visible tight cluster, or park the cue with a gentle nudge.

        Every candidate needs a clear cue-to-ghost line: the target itself is
        excluded from the obstacle list, every other active ball is included.
        Scores keep the ``danger + distance`` shape; break and parking terms are
        bonuses in the same pixel units. A dangerous break is discarded rather
        than merely penalised, so the safety candidate can take over.
        """
        white = table.white
        left, top, right, bottom = table.bounds
        targets = [body for index, body in enumerate(table.icons, 1) if index not in table.sunk]
        if not targets:
            return None
        nudge_best = None
        break_best = None
        for target in targets:
            others = [body for body in targets if body is not target]
            dx, dy = target.x - white.x, target.y - white.y
            distance = math.hypot(dx, dy)
            if distance < 1:
                continue
            ux, uy = dx / distance, dy / distance
            contact = target.radius + white.radius
            gx, gy = target.x - ux * contact, target.y - uy * contact
            if not (left + white.radius <= gx <= right - white.radius
                    and top + white.radius <= gy <= bottom - white.radius):
                continue
            if not table._clear(white.x, white.y, gx, gy, others, white.radius):
                continue

            if safety:
                wx, wy = gx - white.x, gy - white.y
                danger, (qx, qy) = BotController._cue_pocket_danger(table, gx, gy, wx, wy, ux, uy)
                clearance = BotController._parking_clearance(table, gx, gy, qx, qy, others)
                nudge = (danger + distance - PARK_BONUS_FACTOR * clearance,
                         (gx, gy), BotController.shot_power(table, distance, 0.0, 0.5))
                if nudge_best is None or nudge[0] < nudge_best[0] - SCORE_EPSILON:
                    nudge_best = nudge
            if not cluster:
                continue
            density = BotController.cluster_density(table, target)
            if density < DENSE_CLUSTER_MIN:
                continue
            bx, by, nx, ny = gx, gy, ux, uy
            rim = BotController._cluster_rim_ghost(table, target, others, white, contact)
            if rim is not None:
                bx, by, nx, ny = rim
            bxw, byw = bx - white.x, by - white.y
            cue_distance = math.hypot(bxw, byw)
            if cue_distance < 1:
                continue
            approach = (bxw * nx + byw * ny) / cue_distance
            if approach <= MIN_DIRECT_ALIGNMENT:
                continue
            break_danger, _ = BotController._cue_pocket_danger(table, bx, by, bxw, byw, nx, ny)
            if break_danger > 0:
                continue
            # shot_power() already models drag; clamp its result into the break
            # band instead of changing the shared power curve or its inverse.
            effective = CLUSTER_RADIUS_FACTOR * target.radius
            charge = min(BREAK_CHARGE_MAX, max(BREAK_CHARGE_MIN,
                        BotController.shot_power(table, cue_distance, effective, approach)))
            breaking = (break_danger + distance - CLUSTER_BONUS_PER_BALL * density,
                        (bx, by), charge)
            if break_best is None or breaking[0] < break_best[0] - SCORE_EPSILON:
                break_best = breaking
        return (break_best if cluster else None) or (nudge_best if safety else None)

    def _candidates(self, table, *, flags):
        """Deterministic candidate list, ordered direct -> plant -> bank -> kick.

        Candidate code from U3-U5 is unchanged; ``flags`` only decides which
        layers a level may use. ``banks`` stays the cue-kick switch.
        """
        white = table.white
        left, top, right, bottom = table.bounds
        targets = [body for index, body in enumerate(table.icons, 1) if index not in table.sunk]
        candidates = []
        for target in targets:
            for px, py in table.pockets:
                dx, dy = px - target.x, py - target.y
                pocket_distance = math.hypot(dx, dy)
                if pocket_distance < 1:
                    continue
                ux, uy = dx / pocket_distance, dy / pocket_distance
                if not BotController._pocket_entry_ok(table, target.x, target.y, ux, uy,
                                                      px, py, target.radius):
                    continue
                contact = target.radius + white.radius
                gx, gy = target.x - ux * contact, target.y - uy * contact
                if not (left + white.radius <= gx <= right - white.radius
                        and top + white.radius <= gy <= bottom - white.radius):
                    continue
                wx, wy = gx - white.x, gy - white.y
                cue_distance = math.hypot(wx, wy)
                if cue_distance < 1:
                    continue
                alignment = (wx * ux + wy * uy) / cue_distance
                if alignment <= MIN_DIRECT_ALIGNMENT:  # too thin: the target barely moves forward
                    continue
                others = [body for body in targets if body is not target]
                if not (table._clear(white.x, white.y, gx, gy, others, white.radius)
                        and table._clear(target.x, target.y, px, py, others, target.radius)):
                    continue
                # Short paths and full cuts are reliable; thin cuts and long rolls are not.
                score = (cue_distance + pocket_distance / max(0.5, alignment)) / max(0.28, alignment)
                qx, qy, qlen = BotController._post_collision(wx, wy, ux, uy)
                if qlen > 1e-9:
                    for cx, cy in table.pockets:
                        ahead = (cx - gx) * qx + (cy - gy) * qy
                        if ahead > 0:
                            offset = abs((cx - gx) * qy - (cy - gy) * qx) / qlen
                            if offset < table.pocket_radius + white.radius:
                                score += table.height * 1.5
                candidates.append((score, (gx, gy), self.shot_power(
                    table, cue_distance, pocket_distance, alignment)))
        if flags.get('plants'):
            candidates.extend(self._plant_candidates(table))
        if flags.get('target_bank'):
            for target in targets:
                others = [body for body in targets if body is not target]
                for pocket in table.pockets:
                    candidates.extend(self._bank_candidates(table, target, pocket, others))
        if flags.get('banks'):
            for target in targets:
                others = [body for body in targets if body is not target]
                for pocket in table.pockets:
                    candidates.extend(self._kick_candidates(table, target, pocket, others))
        return candidates

    @staticmethod
    def _tactics(flags, *, banks=False):
        """Resolve the explicit call path used by legacy `plan_shot(banks=...)`."""
        if flags is not None:
            return flags
        return {'plants': True, 'target_bank': bool(banks), 'banks': bool(banks),
                'cluster': True, 'safety': True, 'rollout_k': 0, 'lookahead': False}

    def plan_shot(self, table, *, banks=None, flags=None):
        """Return the best clear line as ``(score, ghost, power)``.

        Passing ``banks`` explicitly keeps the legacy call path (plant + target
        bank + cue kick). Gameplay passes ``flags`` from :data:`SETTINGS`; tier
        gates select the layers, and a fresh rollout pick can override the line.
        """
        if flags is None and banks is None and self.mode in SETTINGS:
            flags = SETTINGS[self.mode]
        used = self._tactics(flags, banks=bool(banks))
        self._drain_rollout(table)
        candidates = self._candidates(table, flags=used)
        best = None
        for candidate in candidates:
            if best is None or candidate[0] < best[0]:
                best = candidate
        if best is None:
            best = self._escape_shot(table, cluster=used.get('cluster', True),
                                     safety=used.get('safety', True))
        pick = self._fresh_rollout_pick(table, candidates)
        if used.get('rollout_k', 0):
            self._prepare_rollout(table, used, candidates)
        return pick or best

    # -- bounded background rollout ---------------------------------------
    def _prepare_rollout(self, table, flags, candidates=None):
        """Fire one bounded worker with independent clones; never block the caller."""
        k = int(flags.get('rollout_k', 0) or 0)
        if k <= 0 or self._rollout_pending or table.state != 'ready':
            return False
        candidates = self._candidates(table, flags=flags) if candidates is None else candidates
        if not candidates:
            return False
        jobs = []
        for candidate in sorted(candidates, key=lambda item: item[0])[:k]:
            clone = table.clone_light()
            clone.aim_at(*candidate[1])
            if not clone.shoot(candidate[2], None):
                continue
            # Poses are render-only; skipping them keeps the worker inside budget.
            clone._poses = lambda: ()
            jobs.append((candidate, clone))
        if not jobs:
            return False
        self._rollout_generation += 1
        generation = self._rollout_generation
        self._rollout_pending = True
        worker = threading.Thread(
            target=rollout_worker,
            args=(generation, table.round_serial, table.state, table.shots, jobs,
                  self._rollout_results),
            kwargs={'budget': ROLLOUT_TIME_BUDGET, 'max_steps': ROLLOUT_MAX_STEPS,
                    'lookahead': bool(flags.get('lookahead'))},
            name='bot-rollout', daemon=True)
        try:
            worker.start()
        except RuntimeError:
            # Restricted test environments may refuse threads; deterministic plan stands.
            self._rollout_pending = False
            self._rollout_fallback = 'thread-start-failed'
            return False
        return True

    def _drain_rollout(self, table):
        """Consume worker results, keeping only results for the current table state."""
        while True:
            try:
                message = self._rollout_results.get_nowait()
            except queue.Empty:
                return
            generation, serial, state, shots, candidate, value, elapsed = message
            self._rollout_last_ms = elapsed * 1000.0
            if generation != self._rollout_generation:
                continue  # superseded by a newer request
            if self._rollout_pending:
                self._rollout_pending = False
            if (serial, state, shots) != (table.round_serial, table.state, table.shots):
                continue  # table moved on while the worker was busy
            if candidate is not None:
                self._rollout_pick = (generation, serial, state, shots, candidate, value)

    def _fresh_rollout_pick(self, table, candidates):
        """Return a rollout pick only if it belongs to this exact table state."""
        pick = self._rollout_pick
        if pick is None:
            return None
        generation, serial, state, shots, candidate, value = pick
        if generation != self._rollout_generation:
            return None
        if (serial, state, shots) != (table.round_serial, table.state, table.shots):
            return None
        return candidate if candidate in candidates else None

    # -- internals --------------------------------------------------------
    def _shoot(self, table, settings, now):
        x, y = table.white.x, table.white.y
        if table.break_shot:
            dx, dy = 1.0, 0.022
            distance = table.width
            ideal = 0.85
        else:
            plan = self.plan_shot(table, flags=settings)
            if plan is None:
                targets = [body for index, body in enumerate(table.icons, 1) if index not in table.sunk]
                target = min(targets, key=lambda body: (body.x - x) ** 2 + (body.y - y) ** 2, default=None)
                if target is None:
                    return False
                dx, dy = target.x - x, target.y - y
                distance = max(1.0, math.hypot(dx, dy))
                ideal = self.shot_power(table, distance, distance * 0.5, 0.8)
            else:
                shot, ideal = plan[1], plan[2]
                dx, dy = shot[0] - x, shot[1] - y
                distance = max(1.0, math.hypot(dx, dy))
        # Rotate the ideal direction by a Gaussian offset scaled by aim error.
        if settings['aim_error'] > 0:
            angle = math.atan2(dy, dx) + self.rng.gauss(0.0, settings['aim_error'])
            dx, dy = math.cos(angle), math.sin(angle)
        else:
            length = max(1e-6, math.hypot(dx, dy))
            dx, dy = dx / length, dy / length
        table.aim_at(x + dx, y + dy)
        power = ideal + (self.rng.gauss(0.0, settings['power_error']) if settings['power_error'] else 0.0)
        power = min(1.0, max(0.15, power))
        table.charge = power
        self.note_shot_input(self._next_action_aim, power)
        return bool(table.shoot(power, now))
