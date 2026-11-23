"""Mock-only regression tests for runner shutdown and diagnostics."""
import unittest
from unittest.mock import Mock, patch

from dib.runner import BilliardsRunner


class RunnerStopTests(unittest.TestCase):
    def test_stop_timeout_logs_without_name_error(self):
        runner = object.__new__(BilliardsRunner)
        runner.stopping = Mock()
        runner.thread = Mock()
        runner.thread.is_alive.return_value = True

        with patch('dib.runner.diag.log') as log:
            runner.stop()

        runner.stopping.set.assert_called_once_with()
        runner.thread.join.assert_called_once_with(12)
        runner.thread.is_alive.assert_called_once_with()
        log.assert_called_once_with('animation thread did not exit within 12s; continuing shutdown')


if __name__ == '__main__':
    unittest.main()
