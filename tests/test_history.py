"""Persistent bot match history."""
import tempfile
import unittest
from pathlib import Path

from dib.history import MatchHistory, MAX_ENTRIES
from dib.rank import RankSettings


class MatchHistoryTests(unittest.TestCase):
    def make(self):
        with tempfile.TemporaryDirectory() as folder:
            yield MatchHistory(RankSettings(Path(folder) / 'history.json'))

    def test_records_win_loss_draw_and_summary(self):
        for history in self.make():
            self.assertEqual(history.text(), '')
            history.record(3, 1, 'hard')
            history.record(1, 4, 'easy')
            history.record(2, 2, 'medium')
            self.assertEqual(history.summary(), (1, 1, 1, 3))
            self.assertEqual(history.text(), 'History 1W-1L-1D')
            self.assertEqual([e['outcome'] for e in history.entries()], ['win', 'loss', 'draw'])
            self.assertEqual(history.entries()[0]['mode'], 'hard')

    def test_history_is_persistent_and_capped(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'history.json'
            first = MatchHistory(RankSettings(path))
            for i in range(MAX_ENTRIES + 3):
                first.record(i % 3, 1, 'normal')
            second = MatchHistory(RankSettings(path))
            self.assertEqual(len(second.entries()), MAX_ENTRIES)
            self.assertEqual(second.summary()[3], MAX_ENTRIES)


if __name__ == '__main__':
    unittest.main()
