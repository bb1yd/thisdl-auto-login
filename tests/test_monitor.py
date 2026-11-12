import threading
import subprocess
import unittest
from unittest.mock import Mock, patch

from thisdl_login import monitor


class MonitorTests(unittest.TestCase):
    def run_until_idle(self, worker):
        # Stop at the next idle wait so assertions need no timing guesses.
        class Idle(Exception):
            pass

        original_wait = worker._condition.wait_for

        def wait_for(predicate, timeout=None):
            if timeout is None and not predicate():
                raise Idle()
            return original_wait(predicate, timeout)

        worker.notify()
        with patch.object(worker._condition, "wait_for", side_effect=wait_for):
            with self.assertRaises(Idle):
                worker.run()

    def test_close_window_targets_only_current_users_assistant(self):
        with patch.object(monitor.os, "getuid", return_value=501), \
             patch.object(monitor.pwd, "getpwuid", return_value=Mock(pw_name="test-user")) as get_user, \
             patch.object(monitor.subprocess, "run", return_value=Mock(returncode=0)) as run:
            self.assertTrue(monitor.close_login_window())
        get_user.assert_called_once_with(501)
        run.assert_called_once_with(
            ["/usr/bin/killall", "-TERM", "-u", "test-user", "Captive Network Assistant"],
            capture_output=True, timeout=3,
        )

    def test_close_window_handles_absent_app_and_command_failures(self):
        for result in (Mock(returncode=1), OSError("denied"),
                       subprocess.TimeoutExpired("killall", 3)):
            with self.subTest(result=result):
                with patch.object(monitor.pwd, "getpwuid", return_value=Mock(pw_name="test-user")), \
                     patch.object(monitor.subprocess, "run") as run:
                    if isinstance(result, Exception):
                        run.side_effect = result
                    else:
                        run.return_value = result
                    self.assertFalse(monitor.close_login_window())

    def test_dismissal_requires_authentication_without_external_network_probe(self):
        for authenticated in (False, True):
            with self.subTest(authenticated=authenticated):
                check, close = Mock(return_value=authenticated), Mock()
                worker = monitor.RetryWorker(check, retry_delays=(0, 0, 0), on_success=close)
                with patch.object(monitor.auth, "build_opener", side_effect=AssertionError("unexpected network probe")):
                    self.run_until_idle(worker)
                self.assertEqual(check.call_count, 1 if authenticated else 3)
                self.assertEqual(close.call_count, int(authenticated))

    def test_network_event_during_login_does_not_lose_dismissal(self):
        close = Mock(return_value=True)
        worker = monitor.RetryWorker(retry_delays=(0, 0, 0), on_success=close)

        def check():
            if worker._check.call_count == 1:
                worker.notify()
                return True
            # After authentication there is no longer a revisit page.
            return False

        worker._check = Mock(side_effect=check)
        self.run_until_idle(worker)
        close.assert_called_once()
        self.assertEqual(worker._check.call_count, 4)

    def test_late_window_is_retried_without_another_login(self):
        check = Mock(return_value=True)
        close = Mock(side_effect=[False, False, True])
        worker = monitor.RetryWorker(check, retry_delays=(0, 0, 0), on_success=close)
        with patch.object(monitor.time, "sleep") as sleep:
            self.run_until_idle(worker)
        self.assertEqual(close.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        check.assert_called_once()

    def test_absent_window_retries_are_bounded(self):
        check = Mock(return_value=True)
        close = Mock(return_value=False)
        worker = monitor.RetryWorker(check, retry_delays=(0, 0, 0), on_success=close,
                                     dismiss_delays=(0, 0, 0))
        self.run_until_idle(worker)
        self.assertEqual(close.call_count, 3)
        check.assert_called_once()

    def test_dismissal_failure_does_not_repeat_successful_login(self):
        check = Mock(return_value=True)
        close = Mock(side_effect=OSError("failed to close"))
        worker = monitor.RetryWorker(check, retry_delays=(0, 0, 0), on_success=close)
        self.run_until_idle(worker)
        check.assert_called_once()
        close.assert_called_once()

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
        for new_event in (False, True):
            with self.subTest(new_event=new_event):
                worker = monitor.RetryWorker(retry_delays=(0, 0, 0))

                def check():
                    if new_event and worker._check.call_count == 1:
                        worker.notify()
                    return True

                worker._check = Mock(side_effect=check)
                self.run_until_idle(worker)
                self.assertEqual(worker._check.call_count, 2 if new_event else 1)


if __name__ == "__main__":
    unittest.main()
