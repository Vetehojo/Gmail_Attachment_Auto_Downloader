import os
import tempfile
import unittest
from unittest import mock

import watchdog
from job_queue import JobQueue


class WatchdogPauseTest(unittest.TestCase):
    def test_pause_state_is_read_from_queue(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "jobs.sqlite3")
            queue = JobQueue(db)
            queue.set_metadata(watchdog.PAUSED_KEY, "1")
            with mock.patch.object(watchdog, "QUEUE_DB", db):
                self.assertTrue(watchdog.is_paused())

    def test_restart_is_suppressed_while_paused(self):
        state = {"restart_times": [], "stale_count": 2}
        with mock.patch.object(watchdog, "is_paused", return_value=True), \
             mock.patch.object(watchdog, "start_monitor") as start_monitor, \
             mock.patch.object(watchdog, "stop_monitor") as stop_monitor:
            watchdog.perform_restart(state, "test", 1000, kill_existing=True)
        start_monitor.assert_not_called()
        stop_monitor.assert_not_called()
        self.assertEqual(0, state["stale_count"])


if __name__ == "__main__":
    unittest.main()
