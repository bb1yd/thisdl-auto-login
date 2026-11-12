import threading
import unittest
from unittest.mock import Mock

from thisdl_login import monitor


class MonitorTests(unittest.TestCase):
    def test_sleep_is_acknowledged_and_wake_triggers_check(self):
        obj = monitor.MacNotifications.__new__(monitor.MacNotifications)
        obj.io = Mock()
        obj.worker = Mock()
        obj.power_root = 12
        obj._power_changed(None, 0, monitor.SYSTEM_CAN_SLEEP, 123)
        obj._power_changed(None, 0, monitor.SYSTEM_WILL_SLEEP, 456)
        obj._power_changed(None, 0, monitor.SYSTEM_HAS_POWERED_ON, None)
        self.assertEqual(obj.io.IOAllowPowerChange.call_count, 2)
        obj.io.IOAllowPowerChange.assert_any_call(12, 123)
        obj.io.IOAllowPowerChange.assert_any_call(12, 456)
        obj.worker.notify.assert_called_once()

    def test_only_changed_snapshot_triggers_network_check(self):
        obj = monitor.MacNotifications.__new__(monitor.MacNotifications)
        obj.worker = Mock()
        obj.previous = [{"interface": "en0"}]
        obj._snapshot = Mock(side_effect=[[{"interface": "en0"}], [{"interface": "en1"}]])
        obj._network_changed(None, None, None)
        obj.worker.notify.assert_not_called()
        obj._network_changed(None, None, None)
        obj.worker.notify.assert_called_once()

    def test_event_runs_and_retries(self):
        done = threading.Event()
        calls = []

        def check():
            calls.append(1)
            if len(calls) == 3:
                done.set()

        worker = monitor.RetryWorker(check, retry_delays=(0, 0.01, 0.01))
        threading.Thread(target=worker.run, daemon=True).start()
        worker.notify()
        self.assertTrue(done.wait(1))
        self.assertEqual(len(calls), 3)

    def test_success_stops_retries_and_keeps_new_events(self):
        # Stop at the next idle wait so the assertions need no timing guesses.
        class Idle(Exception):
            pass

        for new_event in (False, True):
            with self.subTest(new_event=new_event):
                worker = monitor.RetryWorker(retry_delays=(0, 0, 0))

                def check():
                    if new_event and worker._check.call_count == 1:
                        worker.notify()
                    return True

                worker._check = Mock(side_effect=check)
                original_wait = worker._condition.wait_for

                def wait_for(predicate, timeout=None):
                    if timeout is None and not predicate():
                        raise Idle()
                    return original_wait(predicate, timeout)

                worker.notify()
                worker._condition.wait_for = wait_for
                with self.assertRaises(Idle):
                    worker.run()
                self.assertEqual(worker._check.call_count, 2 if new_event else 1)


if __name__ == "__main__":
    unittest.main()
