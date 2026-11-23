"""Mock-only regression tests for runner shutdown and diagnostics."""
import threading
import time
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


class ShowRequestTests(unittest.TestCase):
    def test_request_show_and_consume_share_one_coherent_pair(self):
        runner = object.__new__(BilliardsRunner)
        runner.show_request = None
        runner.show_lock = threading.Lock()
        stop = threading.Event()
        mismatches = []
        seen = []

        def producer():
            value = 0
            while not stop.is_set():
                value += 1
                runner.request_show(('snapshot', value), ('journal', value))

        def consumer():
            while not stop.is_set():
                request = runner.consume_show_request()
                if request is None:
                    continue
                (snapshot_tag, snapshot_value), (journal_tag, journal_value) = request
                seen.append(snapshot_value)
                if (snapshot_tag, journal_tag, snapshot_value) != ('snapshot', 'journal', journal_value):
                    mismatches.append(request)

        threads = [threading.Thread(target=producer, daemon=True),
                   threading.Thread(target=consumer, daemon=True)]
        for thread in threads:
            thread.start()
        time.sleep(0.1)
        stop.set()
        for thread in threads:
            thread.join(3)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertFalse(mismatches)
        self.assertGreater(len(seen), 0)

    def test_consume_clears_the_pending_request_once(self):
        runner = object.__new__(BilliardsRunner)
        runner.show_request = None
        runner.show_lock = threading.Lock()
        self.assertIsNone(runner.consume_show_request())
        runner.request_show('snap', 'journal')
        self.assertEqual(runner.consume_show_request(), ('snap', 'journal'))
        self.assertIsNone(runner.consume_show_request())


if __name__ == '__main__':
    unittest.main()
