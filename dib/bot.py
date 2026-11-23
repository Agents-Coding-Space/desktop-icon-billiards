"""Hot-seat solo bot: the human and the bot alternate shots on the same table.

Rules (deliberately simple, documented in the README):

- The human always breaks. Pocket an icon and you keep the turn; miss or scratch
  and the turn passes. A scratched cue ball is re-spotted by the engine.
- Every pocketed icon scores 1 point for whoever took that shot.
- Difficulty controls three knobs: aim error (radians, std. dev. of a Gaussian
  offset from the ideal direction), power error (std. dev. of the Gaussian offset
  from the ideal charge) and decision delay (seconds, uniform wait between aiming
  and striking). Every level now evaluates all target/pocket lines; Extreme plays
  the best clear shot with no error and just enough power to reach the pocket.
"""
from __future__ import annotations

import math
import random

from .table import BALL_IMPULSE, MIN_DIRECT_ALIGNMENT, RAIL_REBOUND, ROLLING_DRAG

MODES = ('off', 'easy', 'medium', 'hard', 'extreme')
# aim_error: radians; power_error: charge 0..1; delay: seconds range before the strike.
SETTINGS = {
    'easy': {'aim_error': 0.14, 'power_error': 0.15, 'delay': (1.0, 1.8)},
    'medium': {'aim_error': 0.055, 'power_error': 0.07, 'delay': (0.65, 1.2)},
    'hard': {'aim_error': 0.018, 'power_error': 0.025, 'delay': (0.35, 0.7)},
    'extreme': {'aim_error': 0.0, 'power_error': 0.0, 'delay': (0.18, 0.35), 'banks': True},
}
LABELS = {'off': 'Bot Off', 'easy': 'Easy', 'medium': 'Medium', 'hard': 'Hard', 'extreme': 'Extreme'}


def other(player):
    return 'bot' if player == 'human' else 'human'


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
    def sync(self, table, now):
        """Track shots/pockets to attribute points; call once per frame with the live table."""
        if self._round != table.round_serial:
            self._round = table.round_serial
            self._shots_seen = 0
            self._pocketed_seen = table.pocketed_icons
            self._last_shooter = 'human'
            self._shot_pending = False
            self._shot_gained = self._shot_scratch = 0
            self._next_action_at = None
            self.turn = 'human'
            self.scores = {'human': 0, 'bot': 0}
        # Billiard turn rule: pocket a ball and you keep shooting; miss (or scratch) and you pass.
        while table.shots > self._shots_seen:
            self._last_shooter = self.turn
            self._shot_pending = True
            self._shot_gained = 0
            self._shot_scratch = False
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
        if self._shot_pending and table.state == 'ready':
            if self._shot_scratch or self._shot_gained == 0:
                self.turn = other(self.turn)
            self._shot_pending = False
            self._next_action_at = None

    def update(self, table, now):
        """Take the bot's shot when it is the bot's turn and the table is ready."""
        self.sync(table, now)
        if not self.bot_turn:
            return False
        if table.state != 'ready':
            return False
        settings = SETTINGS[self.mode]
        if self._next_action_at is None:
            self._next_action_at = now + self.rng.uniform(*settings['delay'])
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
    def _escape_shot(table):
        """Fallback when every pot is blocked: nudge the nearest safe ball."""
        white = table.white
        targets = [body for index, body in enumerate(table.icons, 1) if index not in table.sunk]
        if not targets:
            return None
        best = None
        for target in targets:
            dx, dy = target.x - white.x, target.y - white.y
            distance = math.hypot(dx, dy)
            if distance < 1:
                continue
            ux, uy = dx / distance, dy / distance
            gx, gy = target.x - ux * (target.radius + white.radius), target.y - uy * (target.radius + white.radius)
            # Keep the cue ball away from pocket centres after the nudge.
            danger = 0.
            for px, py in table.pockets:
                ahead = (px - gx) * ux + (py - gy) * uy
                if ahead > 0 and abs((px - gx) * uy - (py - gy) * ux) < table.pocket_radius + white.radius:
                    danger += table.height
            score = danger + distance
            if best is None or score < best[0]:
                best = (score, (gx, gy), BotController.shot_power(table, distance, 0.0, 0.5))
        return best

    def plan_shot(self, table, *, banks=False):
        """Return the best clear line as ``(score, ghost, power)``.

        ``banks=True`` also considers one-cushion target-ball banks.
        """
        white = table.white
        left, top, right, bottom = table.bounds
        targets = [body for index, body in enumerate(table.icons, 1) if index not in table.sunk]
        best = None
        for target in targets:
            for px, py in table.pockets:
                dx, dy = px - target.x, py - target.y
                pocket_distance = math.hypot(dx, dy)
                if pocket_distance < 1:
                    continue
                ux, uy = dx / pocket_distance, dy / pocket_distance
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
                # Avoid leaving the cue ball rolling into a pocket after the contact.
                qx, qy, qlen = BotController._post_collision(wx, wy, ux, uy)
                if qlen > 1e-9:
                    for cx, cy in table.pockets:
                        ahead = (cx - gx) * qx + (cy - gy) * qy
                        if ahead > 0:
                            offset = abs((cx - gx) * qy - (cy - gy) * qx) / qlen
                            if offset < table.pocket_radius + white.radius:
                                score += table.height * 1.5
                candidate = (score, (gx, gy), self.shot_power(table, cue_distance,
                                                              pocket_distance, alignment))
                if best is None or candidate[0] < best[0]:
                    best = candidate
        if banks:
            for target in targets:
                others = [body for body in targets if body is not target]
                for pocket in table.pockets:
                    for candidate in self._bank_candidates(table, target, pocket, others):
                        if best is None or candidate[0] < best[0]:
                            best = candidate
        return best or self._escape_shot(table)

    # -- internals --------------------------------------------------------
    def _shoot(self, table, settings, now):
        x, y = table.white.x, table.white.y
        if table.break_shot:
            dx, dy = 1.0, 0.022
            distance = table.width
            ideal = 0.85
        else:
            plan = self.plan_shot(table, banks=bool(settings.get('banks')))
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
        return bool(table.shoot(power, now))
