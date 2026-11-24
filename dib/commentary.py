"""Value-only records, the Vietnamese line loader and the GUI-free engine.

``ShotReport`` is the immutable shot payload produced by :mod:`dib.bot`.
``CommentaryEngine`` consumes those reports plus an injected line mapping. The
engine itself performs no file I/O and imports no GUI or platform modules;
:func:`load_commentary_lines` only reads a JSON asset. The overlay owns
visibility, display and the clock passed to :meth:`CommentaryEngine.comment`.
"""
from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config


@dataclass(frozen=True)
class ShotReport:
    """One settled shot, assembled by :class:`dib.bot.BotController`.

    ``rail_hits_before_pot`` is the rail count up to the first potted icon, or
    the whole shot's rail count when no icon was potted. ``is_combo`` is true
    when the first potted icon is not the first ball the cue contacted.
    """

    shooter: str
    pocketed: int
    scratched: bool
    hit_any_ball: bool
    rail_hits_before_pot: int
    is_combo: bool
    power: float
    aim_seconds: float
    shooter_streak: int
    miss_streak: int
    human_score: int
    bot_score: int
    remaining_balls: int
    bot_mode: str


# Commentary policy. The overlay passes ``now`` in seconds; the engine never
# reads a GUI flag or a global setting on its own.
# Hard floor: a shorter constructor value cannot bypass the 3.5 s pause.
COOLDOWN_SECONDS = 3.5
NORMAL_TURN_THRESHOLD = 2
NORMAL_CHANCE = 0.65
OVERTHINK_SECONDS = 12.0
MULTI_POT_MIN = 2
STREAK_TRIGGER = 3
ENDGAME_REMAINING = 2
DEFAULT_PERSONA = 'easy'

# Stable persona keys consumed by the injected line mapping. s2-asset should
# emit ``{group: {persona: [line, ...]}}`` with these persona keys.
PERSONA_BY_MODE = {
    'easy': 'easy',
    'medium': 'medium',
    'hard': 'hard',
    'extreme': 'extreme',
}

# Highest priority first. ``finished`` precedes ``endgame`` because a cleared
# rack also satisfies ``remaining_balls <= ENDGAME_REMAINING``.
URGENT_GROUPS = (
    'finished', 'scratch', 'airball', 'multi_pot', 'shooter_streak',
    'miss_streak', 'overthink', 'lead_change', 'endgame',
)
NORMAL_GROUPS = ('single_pot', 'ordinary_miss')

# Broad buckets are accepted so an asset file may use either per-event groups
# or just ``urgent``/``normal``. Canonical names stay the documented contract.
GROUP_ALIASES = {
    'finished': ('finish', 'game_over'),
    'scratch': ('scratched',),
    'airball': ('air_ball', 'no_contact'),
    'multi_pot': ('multi_pots', 'double_pot', 'double_pots'),
    'overthink': ('overthink_miss',),
    'shooter_streak': ('streak', 'hot_streak'),
    'miss_streak': ('cold_streak',),
    'lead_change': ('lead_flip',),
    'endgame': ('end_game', 'last_balls'),
    'single_pot': ('pot', 'potted'),
    'ordinary_miss': ('miss',),
}

COMMENTARY_ASSET_NAME = 'commentary_vi.json'
_COMMENTARY_CACHE: dict[str, dict[str, dict[str, list[str]]]] = {}


def _copy_bank(bank):
    return {group: {persona: list(lines) for persona, lines in personas.items()}
            for group, personas in bank.items()}


def load_commentary_lines(path=None, *, use_cache=True):
    """Read a ``{group: {persona: [line, ...]}}`` bank from a JSON asset.

    The default source is ``config.ASSETS / 'commentary_vi.json'``. Missing,
    unreadable or malformed files return an empty mapping instead of raising.
    Results are cached per path unless ``use_cache=False``; callers get a fresh
    copy, so mutating it cannot corrupt the cache. No GUI or platform calls.
    """
    try:
        target = Path(path) if path is not None else config.ASSETS / COMMENTARY_ASSET_NAME
        cache_key = str(target)
    except (TypeError, ValueError, OSError):
        return {}
    if use_cache and cache_key in _COMMENTARY_CACHE:
        return _copy_bank(_COMMENTARY_CACHE[cache_key])

    bank = {}
    try:
        payload = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        pass
    else:
        if isinstance(payload, Mapping):
            for group, personas in payload.items():
                if not isinstance(group, str) or not group.strip():
                    continue
                if not isinstance(personas, Mapping):
                    continue
                by_persona = {}
                for persona, value in personas.items():
                    if not isinstance(persona, str) or not persona.strip():
                        continue
                    lines = [line.strip() for line in value
                             if isinstance(line, str) and line.strip()] \
                        if isinstance(value, (list, tuple)) else []
                    if lines:
                        by_persona[persona.strip()] = lines
                if by_persona:
                    bank[group.strip()] = by_persona
    if use_cache:
        _COMMENTARY_CACHE[cache_key] = bank
    return _copy_bank(bank)


@dataclass(frozen=True)
class CommentaryEvent:
    """Classification of one settled shot: provider group and urgency."""

    group: str
    urgent: bool

    @property
    def kind(self):
        return 'urgent' if self.urgent else 'normal'


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _score_sign(report):
    """Return 1 when the human leads, -1 when the bot leads, else 0."""
    human = _num(getattr(report, 'human_score', 0))
    bot = _num(getattr(report, 'bot_score', 0))
    return (human > bot) - (human < bot)


def classify_event(report, previous_score_sign=None):
    """Classify a report. ``previous_score_sign`` enables lead-change detection.

    ``ShotReport`` carries no round-state flag, so ``finished`` is inferred
    from an empty rack (``remaining_balls <= 0``).
    """
    previous = 0 if previous_score_sign is None else previous_score_sign
    remaining = int(_num(getattr(report, 'remaining_balls', 0)))
    pocketed = int(_num(getattr(report, 'pocketed', 0)))
    aim = _num(getattr(report, 'aim_seconds', 0.0))
    streak = int(_num(getattr(report, 'shooter_streak', 0)))
    misses = int(_num(getattr(report, 'miss_streak', 0)))
    sign = _score_sign(report)

    if remaining <= 0:
        group = 'finished'
    elif bool(getattr(report, 'scratched', False)):
        group = 'scratch'
    elif not bool(getattr(report, 'hit_any_ball', False)):
        group = 'airball'
    elif pocketed >= MULTI_POT_MIN:
        group = 'multi_pot'
    elif streak >= STREAK_TRIGGER:
        group = 'shooter_streak'
    elif misses >= STREAK_TRIGGER:
        group = 'miss_streak'
    elif aim > OVERTHINK_SECONDS and pocketed == 0:
        group = 'overthink'
    elif previous not in (0, sign) and sign != 0 and sign == -previous:
        group = 'lead_change'
    elif remaining <= ENDGAME_REMAINING:
        group = 'endgame'
    else:
        return CommentaryEvent('single_pot' if pocketed > 0 else 'ordinary_miss', False)
    return CommentaryEvent(group, True)


def _normalise_key(value):
    return str(value).strip().lower().replace('-', '_').replace(' ', '_').replace('/', '_')


def _clean_lines(value):
    """Keep only non-blank strings; tolerate malformed provider entries."""
    if value is None or isinstance(value, (str, bytes, Mapping)):
        return (value.strip(),) if isinstance(value, str) and value.strip() else ()
    try:
        iterator = iter(value)
    except TypeError:
        return ()
    return tuple(line for line in iterator if isinstance(line, str) and line.strip())


def _mapping_get(mapping, key):
    if not isinstance(mapping, Mapping):
        return None
    if key in mapping:
        return mapping[key]
    for existing, value in mapping.items():
        if _normalise_key(existing) == key:
            return value
    return None


def _as_rng(rng):
    if isinstance(rng, random.Random):
        return rng
    if isinstance(rng, (int, str)) and not isinstance(rng, bool):
        return random.Random(rng)
    if rng is not None and hasattr(rng, 'random') and hasattr(rng, 'shuffle'):
        return rng
    return random.Random()


class _ShuffleBag:
    """Draw every line once per cycle, then reshuffle a fresh cycle."""

    __slots__ = ('lines', 'remaining')

    def __init__(self, lines):
        self.lines = tuple(lines)
        self.remaining = []

    def draw(self, rng, avoid=None):
        if not self.lines:
            return None
        if not self.remaining:
            pool = list(self.lines)
            if avoid is not None and len(pool) > 1 and avoid in pool:
                pool.remove(avoid)
            rng.shuffle(pool)
            self.remaining = pool
        return self.remaining.pop() if self.remaining else None


class CommentaryEngine:
    """Decide whether one settled shot deserves a commentary line.

    ``lines`` maps ``group -> persona -> sequence[str]``. Groups are the event
    names in :data:`URGENT_GROUPS` and :data:`NORMAL_GROUPS`; personas come from
    :data:`PERSONA_BY_MODE`. A group may also map straight to a line list, and
    the broad ``urgent``/``normal`` groups are tried after the specific one.

    Call :meth:`comment` once per settled report. ``now`` must be a monotonic
    seconds value from the caller; omit it to use the injected ``clock``
    (``time.monotonic`` by default). The engine returns a line or ``None`` and
    never raises for unknown groups, personas or empty line lists.
    """

    def __init__(self, lines=None, *, rng=None, clock: Callable[[], float] | None = None,
                 cooldown=COOLDOWN_SECONDS, normal_chance=NORMAL_CHANCE,
                 normal_turns=NORMAL_TURN_THRESHOLD, fallback_line=None):
        self.lines: Mapping[str, Any] = lines if isinstance(lines, Mapping) else {}
        self._rng = _as_rng(rng)
        self._clock = clock if callable(clock) else time.monotonic
        self.cooldown = max(COOLDOWN_SECONDS, _num(cooldown, COOLDOWN_SECONDS))
        self.normal_chance = min(1.0, max(0.0, _num(normal_chance, NORMAL_CHANCE)))
        self.normal_turns = max(1, int(_num(normal_turns, NORMAL_TURN_THRESHOLD)))
        self.fallback_line = (fallback_line.strip() if isinstance(fallback_line, str)
                              and fallback_line.strip() else None)
        self.reset()

    def reset(self):
        """Start a new round with fresh state.

        Bags are cleared rather than carried over: a round gets a fresh
        shuffled pool. Cooldown, turn counter, score-order memory and the
        last-line dedupe are cleared with them, so a new round can speak at
        ``now`` even if the previous round ended moments earlier.
        """
        self._bags = {}
        self._last_line = None
        self._last_comment_at = None
        self._turns_since_last_comment = 0
        self._previous_score_sign = 0

    @property
    def turns_since_last_comment(self):
        return self._turns_since_last_comment

    def classify(self, report):
        """Classify ``report`` using the last seen score order."""
        return classify_event(report, self._previous_score_sign)

    def comment(self, report, now=None, *, enabled=True, hidden=False, disabled=False,
                bot_mode=None, bot_on=None) -> str | None:
        """Return a line for ``report``, or ``None`` when silence is correct.

        ``enabled`` is the bot-on flag, ``hidden`` the overlay visibility flag
        and ``disabled`` an explicit commentary opt-out. ``bot_on`` is an alias
        for ``enabled`` for callers that name the flag after the bot setting.
        ``bot_mode`` can override the mode stored in the report. Silenced
        shots still advance the turn counter and score-order memory. An urgent
        shot arriving inside the cooldown is dropped, not queued.
        """
        event = self.classify(report)
        self._previous_score_sign = _score_sign(report)
        self._turns_since_last_comment += 1

        if bot_on is not None:
            enabled = enabled and bool(bot_on)
        mode = bot_mode if bot_mode is not None else getattr(report, 'bot_mode', '')
        mode = str(mode).strip().lower()
        if disabled or not enabled or hidden or mode == 'off':
            return None

        moment = self._clock() if now is None else now
        moment = _num(moment)
        if self._last_comment_at is not None and moment - self._last_comment_at < self.cooldown:
            return None
        if not event.urgent:
            if self._turns_since_last_comment < self.normal_turns:
                return None
            if self._rng.random() >= self.normal_chance:
                return None

        persona = PERSONA_BY_MODE.get(mode, _normalise_key(mode) or DEFAULT_PERSONA)
        line = self._draw(event.group, event.urgent, persona)
        if line is None:
            return None
        self._last_comment_at = moment
        self._turns_since_last_comment = 0
        self._last_line = line
        return line

    def handle(self, *args, **kwargs):
        """Alias for :meth:`comment` at call sites that feed events."""
        return self.comment(*args, **kwargs)

    __call__ = comment

    def _draw(self, group, urgent, persona):
        broad = 'urgent' if urgent else 'normal'
        for candidate_group in self._group_candidates(group, broad):
            for candidate_persona in self._persona_candidates(persona):
                lines = self._lines_for(candidate_group, candidate_persona)
                if not lines:
                    continue
                key = (candidate_group, candidate_persona)
                bag = self._bags.get(key)
                if bag is None:
                    bag = self._bags[key] = _ShuffleBag(lines)
                line = bag.draw(self._rng, self._last_line)
                if line is not None:
                    return line
        return self.fallback_line

    @staticmethod
    def _group_candidates(group, broad):
        keys = [group, *GROUP_ALIASES.get(group, ()), broad]
        seen = set()
        candidates = []
        for key in keys:
            normalised = _normalise_key(key)
            if normalised and normalised not in seen:
                seen.add(normalised)
                candidates.append(normalised)
        return candidates

    @staticmethod
    def _persona_candidates(persona):
        normalised = _normalise_key(persona)
        return (normalised,) if normalised else ()

    def _lines_for(self, group, persona):
        node = _mapping_get(self.lines, group)
        if node is None:
            return ()
        if not isinstance(node, Mapping):
            return _clean_lines(node)
        return _clean_lines(_mapping_get(node, persona))
