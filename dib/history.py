"""Persistent bot-vs-player match history.

Stored under ``%APPDATA%\\DesktopIconBilliards\\history.json`` as a small JSON list.
Each completed round is recorded once; old entries are capped so the file stays tiny.
"""
from __future__ import annotations

from datetime import datetime

from .rank import RankSettings, data_directory

MAX_ENTRIES = 50


class MatchHistory:
    """Thin wrapper around a JSON settings file holding match results."""

    def __init__(self, settings=None):
        self.settings = settings or RankSettings(data_directory() / 'history.json')

    def entries(self):
        value = self.settings.get('matches', [])
        return value if isinstance(value, list) else []

    def record(self, human, bot, mode='', *, when=None):
        """Append a finished round. Returns the stored entry."""
        if human > bot:
            outcome = 'win'
        elif bot > human:
            outcome = 'loss'
        else:
            outcome = 'draw'
        entry = {'at': (when or datetime.now().astimezone()).isoformat(timespec='seconds'),
                 'human': int(human), 'bot': int(bot), 'outcome': outcome, 'mode': str(mode)}
        matches = [item for item in self.entries() if isinstance(item, dict)]
        matches.append(entry)
        self.settings.update(matches=matches[-MAX_ENTRIES:])
        return entry

    def summary(self):
        """Return ``(wins, losses, draws, total)``."""
        wins = losses = draws = 0
        for item in self.entries():
            if not isinstance(item, dict):
                continue
            outcome = item.get('outcome')
            if outcome == 'win':
                wins += 1
            elif outcome == 'loss':
                losses += 1
            elif outcome == 'draw':
                draws += 1
        return wins, losses, draws, wins + losses + draws

    def text(self):
        wins, losses, draws, total = self.summary()
        return f'History {wins}W-{losses}L-{draws}D' if total else ''
