import json
import os
from pathlib import Path
import tempfile
import unittest
import time
from unittest.mock import Mock, patch

from openstick_device import ClickDetector, Daemon, DEFAULTS, INPUT_EVENT, PAGES, ScreenState, save_config, validate_config


class ClickTests(unittest.TestCase):
    def test_single_is_delayed(self):
        detector = ClickDetector()
        self.assertEqual(detector.event(1, 1), [])
        self.assertEqual(detector.event(0, 1.1), [])
        self.assertEqual(detector.tick(1.4), [])
        self.assertEqual(detector.tick(1.46), ["single_click"])
        self.assertEqual(detector.tick(2), [])

    def test_double_does_not_also_single(self):
        detector = ClickDetector()
        result = []
        for value, now in [(1, 1), (0, 1.08), (1, 1.2), (0, 1.28)]:
            result += detector.event(value, now)
        result += detector.tick(2)
        self.assertEqual(result, ["double_click"])

    def test_two_separated_clicks(self):
        detector = ClickDetector()
        result = []
        for value, now in [(1, 1), (0, 1.1), (1, 2), (0, 2.1)]:
            result += detector.event(value, now)
        result += detector.tick(3)
        self.assertEqual(result, ["single_click", "single_click"])

    def test_bounce_and_repeat(self):
        for events in [[(1, 1), (0, 1.01)], [(1, 1), (2, 1.05), (2, 1.08), (0, 1.1)]]:
            detector = ClickDetector()
            result = []
            for value, now in events:
                result += detector.event(value, now)
            self.assertEqual(result + detector.tick(5), [] if events[1][0] == 0 else ["single_click"])

    def test_hold_opens_once_and_release_never_clicks(self):
        detector = ClickDetector()
        self.assertEqual(detector.event(1, 1), [])
        self.assertEqual(detector.tick(2), [])
        self.assertEqual(detector.tick(2.21), ["hold"])
        self.assertEqual(detector.event(2, 2.3), [])
        self.assertEqual(detector.tick(3), [])
        self.assertEqual(detector.event(0, 3.1), ["hold_release"])
        self.assertEqual(detector.tick(5), [])

    def test_queued_hold_and_pending_short_click(self):
        detector = ClickDetector()
        detector.event(1, 1)
        detector.event(0, 1.1)
        detector.event(1, 1.2)
        self.assertEqual(detector.event(0, 3), ["hold", "hold_release"])
        self.assertEqual(detector.tick(4), [])

    def test_triple_click_is_double_then_single(self):
        detector = ClickDetector()
        result = []
        for value, now in [(1, 1), (0, 1.06), (1, 1.16), (0, 1.22), (1, 1.32), (0, 1.38)]:
            result += detector.event(value, now)
        self.assertEqual(result + detector.tick(2), ["double_click", "single_click"])


class StateTests(unittest.TestCase):
    def test_sleep_wake_and_timeout_disabled(self):
        state = ScreenState(DEFAULTS.copy(), 10)
        state.tick(69)
        self.assertTrue(state.awake)
        state.tick(70)
        self.assertFalse(state.awake)
        state.action("display.toggle", 71)
        self.assertTrue(state.awake)
        state.config["screen_sleep_seconds"] = 0
        state.tick(100000)
        self.assertTrue(state.awake)

    def test_pages_wrap_and_wake(self):
        state = ScreenState(DEFAULTS.copy(), 0)
        state.action("display.sleep", 1)
        state.action("display.previous_page", 2)
        self.assertEqual(state.status(2)["page"], PAGES[-1])
        self.assertTrue(state.awake)
        state.action("display.next_page", 3)
        self.assertEqual(state.status(3)["page"], "overview")
        for index in range(len(PAGES)):
            self.assertEqual(state.status(3)["page"], PAGES[index])
            state.action("display.next_page", 3)
        self.assertEqual(state.status(3)["page"], "overview")

    def test_power_prompt_wakes_and_times_out_without_poweroff(self):
        state = ScreenState({**DEFAULTS, "screen_sleep_seconds": 1}, 0)
        state.action("display.sleep", 1)
        state.request_power_off(2)
        state.tick(11)
        self.assertTrue(state.awake)
        self.assertIsNotNone(state.power_deadline)
        state.tick(12)
        self.assertIsNone(state.power_deadline)
        self.assertFalse(state.power_off_pending)
        self.assertTrue(state.awake)
        state.tick(13)
        self.assertFalse(state.awake)

    def test_status_does_not_extend_sleep(self):
        state = ScreenState(DEFAULTS.copy(), 0)
        self.assertEqual(state.status(59)["sleep_in_seconds"], 1)
        state.tick(61)
        self.assertFalse(state.awake)

    def test_validation(self):
        for key, value in [("screen_sleep_seconds", -1), ("screen_sleep_seconds", True),
                           ("screen_sleep_seconds", 86401), ("double_click_ms", 50),
                           ("single_click", "reboot"), ("unknown", 1)]:
            with self.assertRaises(ValueError):
                validate_config({key: value})
        self.assertEqual(validate_config({}), DEFAULTS)


class FakeRenderer:
    device = "test-framebuffer"

    def render(self, state):
        pass


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.daemon = Daemon(self.path / "config.json", self.path / "control.sock", FakeRenderer())
        self.addCleanup(self.daemon.selector.close)

    def test_config_survives_restart(self):
        config = {**DEFAULTS, "screen_sleep_seconds": 42, "double_click": "none"}
        self.daemon.dispatch({"op": "configure", "config": config})
        self.assertEqual(json.loads((self.path / "config.json").read_text()), config)
        self.assertEqual((self.path / "config.json").stat().st_mode & 0o777, 0o600)
        second = Daemon(self.path / "config.json", self.path / "second.sock", FakeRenderer())
        self.addCleanup(second.selector.close)
        self.assertEqual(second.status()["config"], config)

    def test_polling_does_not_wake(self):
        self.daemon.dispatch({"op": "display.sleep"})
        for _ in range(5):
            self.assertFalse(self.daemon.dispatch({"op": "status"})["awake"])

    def test_unknown_operations_rejected(self):
        for operation in ("reboot", "hook", "shell", "configure; reboot", "poweroff", "hold", "hold_release", "double_click"):
            with self.assertRaises(ValueError):
                self.daemon.dispatch({"op": operation})

    def test_missing_hook_reports_error_without_crashing(self):
        self.daemon.state.config["single_click"] = "hook"
        self.daemon.gesture("single_click")
        self.assertTrue(self.daemon.last_macro_error)
        self.assertEqual(self.daemon.status()["clicks"]["single_click"], 1)

    def test_config_symlink_is_not_followed(self):
        victim = self.path / "untouched"
        victim.write_text("original")
        (self.path / "config.tmp").symlink_to(victim)
        with self.assertRaises(OSError):
            save_config(self.path / "config.json", DEFAULTS)
        self.assertEqual(victim.read_text(), "original")

    def test_queued_input_uses_event_timestamps(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        self.daemon.input_fd = read_fd
        now = time.monotonic()
        data = bytearray()
        for value, when in [(1, now - 0.30), (0, now - 0.24), (1, now - 0.14), (0, now - 0.08)]:
            seconds = int(when)
            data += INPUT_EVENT.pack(seconds, int((when - seconds) * 1e6), 1, 116, value)
        os.write(write_fd, data)
        self.daemon.read_input()
        self.assertEqual(self.daemon.state.clicks, {"single_click": 0, "double_click": 1})

    def test_dropped_input_is_not_a_macro(self):
        self.daemon.gesture("hold")
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        self.daemon.input_fd = read_fd
        data = b"".join(INPUT_EVENT.pack(sec, 0, kind, code, value) for sec, kind, code, value in [
            (1, 1, 116, 1), (1, 0, 3, 0), (2, 1, 116, 0), (2, 0, 0, 0)])
        os.write(write_fd, data)
        self.daemon.read_input()
        self.assertEqual(self.daemon.detector.tick(5), [])
        self.assertEqual(self.daemon.state.clicks, {"single_click": 0, "double_click": 0})
        self.assertIsNone(self.daemon.state.power_deadline)

    @patch("openstick_device.subprocess.Popen")
    def test_queued_physical_hold_release_and_confirmation(self, popen):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        self.daemon.input_fd = read_fd
        now = time.monotonic()
        events = [(1, now - 3), (0, now - 1.6), (1, now - 0.3),
                  (0, now - 0.24), (1, now - 0.14), (0, now - 0.08)]
        data = bytearray()
        for value, when in events:
            seconds = int(when)
            data += INPUT_EVENT.pack(seconds, int((when - seconds) * 1e6), 1, 116, value)
        os.write(write_fd, data)
        self.daemon.read_input()
        popen.assert_called_once()
        self.assertTrue(self.daemon.state.power_off_pending)
        self.assertEqual(self.daemon.state.clicks, {"single_click": 0, "double_click": 0})

    @patch("openstick_device.subprocess.Popen")
    def test_hold_requires_release_and_explicit_double_confirmation(self, popen):
        self.daemon.gesture("hold")
        self.assertTrue(self.daemon.state.awake)
        self.daemon.gesture("double_click")
        popen.assert_not_called()
        self.daemon.gesture("hold_release")
        self.daemon.gesture("double_click")
        self.assertTrue(self.daemon.state.power_off_pending)
        self.assertEqual(popen.call_args.args[0], ["/bin/systemctl", "--no-block", "poweroff"])
        self.assertEqual(self.daemon.state.clicks, {"single_click": 0, "double_click": 0})
        self.daemon.gesture("double_click")
        popen.assert_called_once()

    @patch("openstick_device.subprocess.Popen")
    def test_single_cancel_overrides_custom_hook(self, popen):
        self.daemon.state.config["single_click"] = "hook"
        self.daemon.gesture("hold")
        self.daemon.gesture("hold_release")
        self.daemon.gesture("single_click")
        self.assertIsNone(self.daemon.state.power_deadline)
        popen.assert_not_called()
        self.assertEqual(self.daemon.state.clicks["single_click"], 0)

    @patch("openstick_device.subprocess.Popen")
    def test_expired_confirmation_does_not_shutdown(self, popen):
        self.daemon.gesture("hold")
        self.daemon.gesture("hold_release")
        self.daemon.state.power_deadline = time.monotonic() - 1
        self.daemon.gesture("double_click")
        popen.assert_not_called()
        self.assertFalse(self.daemon.state.power_off_pending)

    @patch("openstick_device.subprocess.Popen", side_effect=OSError("not available"))
    def test_poweroff_launch_failure_is_visible_and_retryable(self, popen):
        self.daemon.gesture("hold")
        self.daemon.gesture("hold_release")
        self.daemon.gesture("double_click")
        self.assertTrue(self.daemon.state.power_off_error)
        self.assertFalse(self.daemon.state.power_off_pending)
        self.assertIsNotNone(self.daemon.state.power_deadline)

    def test_poweroff_failed_or_timed_out_command(self):
        for code in (1, None):
            with self.subTest(code=code):
                self.daemon.state.power_off_pending = True
                process = Mock()
                process.poll.return_value = code
                self.daemon.poweroff = process
                self.daemon.poweroff_deadline = 0
                self.daemon.check_poweroff(time.monotonic())
                self.assertFalse(self.daemon.state.power_off_pending)
                self.assertTrue(self.daemon.state.power_off_error)
                self.assertIsNone(self.daemon.poweroff)
                if code is None:
                    process.kill.assert_called_once()

    def test_poweroff_success_keeps_shutdown_screen_visible(self):
        process = Mock()
        process.poll.return_value = 0
        self.daemon.poweroff = process
        self.daemon.state.power_off_pending = True
        self.daemon.check_poweroff(time.monotonic())
        self.assertTrue(self.daemon.state.power_off_pending)
        self.assertFalse(self.daemon.state.power_off_error)

    def test_prompt_status_contains_no_telemetry_or_credentials(self):
        self.daemon.gesture("hold")
        status = self.daemon.dispatch({"op": "status"})
        self.assertTrue(status["power_confirmation"])
        self.assertNotIn("wifi", status)
        self.assertNotIn("password", json.dumps(status))


if __name__ == "__main__":
    unittest.main()
