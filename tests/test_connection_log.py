"""Camera connection problems are logged and named: who cut a connect short, and a link that dies between operations."""

import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import camera_connection as cc
from sandbox import setUpModule, tearDownModule  # noqa: F401


class GatedStream:
    def __init__(self):
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.failure = None

    def start(self, **kwargs):
        pass

    def wait_ready(self, deadline):
        self.entered.set()
        while not self.gate.wait(.01):
            deadline.remaining(1)

    def stop(self):
        pass

    def error(self):
        return self.failure


class ConnectionLogTests(unittest.TestCase):
    def setUp(self):
        self.stream = GatedStream()
        self.manager = cc.CameraConnectionManager(self.stream, session=Mock())
        self.addCleanup(self.manager.close)
        for target, value in ((cc.subprocess, "run"), (cc.CameraConnectionManager, "_is_api_reachable")):
            p = patch.object(target, value, return_value=True if value == "_is_api_reachable" else Mock())
            p.start()
            self.addCleanup(p.stop)

    def finish(self, op_id):
        return self.manager.operations[op_id]["future"].result(timeout=3)

    def test_a_disconnect_that_cuts_a_connect_short_names_who_sent_it(self):
        connect = self.manager.connect(client="198.51.100.104")
        self.assertTrue(self.stream.entered.wait(2))
        with self.assertLogs("uvicorn.error.camera", level="INFO") as logs:
            disconnect = self.manager.disconnect(client="198.51.100.49")
            cut = self.finish(connect)
            done = self.finish(disconnect)
        self.assertEqual(cut["stage"], "cancelled")
        self.assertEqual(cut["message"], "Cancelled by a disconnect from 198.51.100.49.")
        self.assertEqual(done["message"], "Camera disconnected by 198.51.100.49.")
        self.assertTrue(any("cancelled by a disconnect from 198.51.100.49" in line for line in logs.output))

    def test_every_operation_logs_who_asked_for_it(self):
        with self.assertLogs("uvicorn.error.camera", level="INFO") as logs:
            self.stream.gate.set()
            self.finish(self.manager.connect(client="198.51.100.251"))
        self.assertTrue(any("connect requested by 198.51.100.251" in line for line in logs.output))

    def test_a_link_that_goes_bad_is_logged_once_and_its_recovery_once(self):
        self.manager.link_expected = True
        problems = iter(["", "the camera Wi-Fi is down", "the camera Wi-Fi is down", ""])
        with patch.object(self.manager, "_link_problem", side_effect=lambda: next(problems)), \
                self.assertLogs("uvicorn.error.camera", level="INFO") as logs:
            for _ in range(2):
                self.manager._check_link()
            self.assertEqual(self.manager.status()["link"], "the camera Wi-Fi is down")
            for _ in range(2):
                self.manager._check_link()
        lost = [line for line in logs.output if "camera link lost" in line]
        back = [line for line in logs.output if "camera link back" in line]
        self.assertEqual((len(lost), len(back)), (1, 1))
        self.assertEqual(self.manager.status()["link"], "")

    def test_no_link_is_watched_before_a_connect_is_ready(self):
        with patch.object(self.manager, "_link_problem", return_value="anything"):
            self.manager._check_link()
        self.assertEqual(self.manager.link, "")

    def test_the_link_problem_names_the_layer_that_failed(self):
        with tempfile.TemporaryDirectory() as root, patch.object(cc, "SYS_NET", Path(root)):
            self.assertIn("gone from the USB bus", self.manager._link_problem())
            iface = Path(root) / cc.INTERFACE
            iface.mkdir()
            (iface / "operstate").write_text("dormant\n")
            self.assertEqual(self.manager._link_problem(), "the camera Wi-Fi is dormant")
            (iface / "operstate").write_text("up\n")
            with patch.object(self.manager, "_is_api_reachable", return_value=False):
                self.assertIn("does not answer", self.manager._link_problem())
            self.stream.failure = "No camera video received for 10 seconds."
            self.assertEqual(self.manager._link_problem(), self.stream.failure)

    def test_a_failed_connect_leaves_no_link_to_watch(self):
        self.manager.link_expected = True

        def fail(deadline):
            raise cc.CameraError("preview never came")
        with patch.object(self.stream, "wait_ready", side_effect=fail), \
                self.assertLogs("uvicorn.error.camera", level="ERROR") as logs:
            self.finish(self.manager.connect(client="x"))
        self.assertFalse(self.manager.link_expected)
        self.assertTrue(any("failed" in line for line in logs.output))


class CaptureTests(unittest.TestCase):
    """A picture the camera refuses names the camera's own reason; the one-shot checks wait past a lost SYN."""

    def setUp(self):
        self.session = Mock()
        self.manager = cc.CameraConnectionManager(GatedStream(), session=self.session)
        self.addCleanup(self.manager.close)
        now = time.monotonic()
        self.op = {"id": "test", "stage": "queued", "stage_at": now, "started": now, "timings": {},
                   "cancel": threading.Event()}
        p = patch.object(self.manager, "_stage")
        p.start()
        self.addCleanup(p.stop)

    def test_a_refused_picture_names_the_cameras_reason(self):
        refusal = requests.Response()
        refusal.status_code, refusal.url = 400, "http://192.0.2.1/osc/commands/execute"
        refusal._content = (b'{"name": "camera.takePicture", "state": "error", '
                            b'"error": {"code": "disabledCommand", "message": "Camera is busy"}}')
        self.session.post.return_value = refusal
        with patch.object(self.manager, "_is_api_reachable", return_value=True):
            with self.assertRaises(cc.CameraError) as refused:
                self.manager._capture(self.op)
        self.assertIn("The camera refused camera.takePicture (HTTP 400)", str(refused.exception))
        self.assertIn("disabledCommand", str(refused.exception))

    def test_capture_and_resume_check_the_camera_past_one_lost_syn(self):
        waited = []

        def connect(address, timeout):
            waited.append(timeout)
            raise OSError("no route to host")
        with patch.object(cc.socket, "create_connection", side_effect=connect):
            with self.assertRaisesRegex(cc.CameraError, "unreachable"):
                self.manager._capture(self.op)
            with self.assertRaisesRegex(cc.CameraError, "unreachable"):
                self.manager._resume(self.op)
        self.assertEqual(waited, [cc.GATE_PROBE_S, cc.GATE_PROBE_S])
        self.assertGreaterEqual(cc.GATE_PROBE_S, 1.5)


class AccessLogTests(unittest.TestCase):
    def setUp(self):
        import main
        self.quiet = main.QuietPolls()

    def record(self, method, path, code):
        return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                                 ("203.0.113.4:5", method, path, "1.1", code), None)

    def test_successful_polls_are_dropped_and_everything_else_kept(self):
        self.assertFalse(self.quiet.filter(self.record("GET", "/status", 200)))
        self.assertFalse(self.quiet.filter(self.record("GET", "/status?operation_id=abc", 200)))
        self.assertFalse(self.quiet.filter(self.record("GET", "/stream/equirec", 200)))
        self.assertTrue(self.quiet.filter(self.record("GET", "/status", 404)))
        self.assertTrue(self.quiet.filter(self.record("POST", "/connect", 202)))
        self.assertTrue(self.quiet.filter(self.record("POST", "/disconnect", 202)))

    def test_the_filter_is_on_uvicorns_access_logger(self):
        import main
        self.assertTrue(any(isinstance(f, main.QuietPolls)
                            for f in logging.getLogger("uvicorn.access").filters))


class FakeNetwork:
    """nmcli and busctl as the scan check sees them: which networks exist and when each was last heard."""

    def __init__(self, networks, last_scan_ms=100_000, fail=False):
        self.networks, self.last_scan_ms, self.fail = networks, last_scan_ms, fail

    def __call__(self, args, **kwargs):
        done = lambda out: cc.subprocess.CompletedProcess(args, 0, out, "")
        if self.fail and args[0] == "nmcli":
            raise cc.subprocess.CalledProcessError(8, args, "", "nmcli is not running")
        if "connection" in args and "show" in args:
            return done("X5 TEST01.OSC\n02:00:00:00:00:01\n")
        if "list" in args:
            return done("".join(f"{b}:{s}:/ap/{i}\n" for i, (b, s, _) in enumerate(self.networks)))
        if "GENERAL.DBUS-PATH" in args:
            return done("/dev/cam\n")
        if args[0] == "busctl" and args[-1] == "LastScan":
            return done(f"x {self.last_scan_ms}\n")
        if args[0] == "busctl" and args[-1] == "LastSeen":
            return done(f"i {self.networks[int(args[3].rsplit('/', 1)[1])][2]}\n")
        return done("")


class ScanTests(unittest.TestCase):
    """The camera's network counts only when the latest scan heard it; the list keeps stale entries."""

    NM_ERROR = "NetworkManager activation failed: Error: Timeout expired (25 seconds)"

    def setUp(self):
        self.manager = cc.CameraConnectionManager(GatedStream(), session=Mock())
        self.addCleanup(self.manager.close)
        self.asked = 95.0
        p = patch.object(cc, "_boottime", lambda: self.asked)
        p.start()
        self.addCleanup(p.stop)

    def check(self, networks, fail=False):
        with patch.object(cc.subprocess, "run", side_effect=FakeNetwork(networks, fail=fail)):
            return self.manager._camera_heard(), self.manager._why_not_associated(self.NM_ERROR)

    def test_a_camera_heard_early_in_a_long_scan_counts(self):
        self.asked = 87.0
        heard, why = self.check([("02:00:00:00:00:01", "X5 TEST01.OSC", 88)])
        self.assertTrue(heard, "heard at 88 s, after the 87 s request, 12 s before the scan finished")
        self.assertEqual(why, self.NM_ERROR)

    def test_no_scan_after_the_request_says_it_cannot_tell(self):
        self.asked = 105.0
        heard, _ = self.check([("02:00:00:00:00:01", "X5 TEST01.OSC", 100)])
        self.assertIsNone(heard)

    def rescan_in_thread(self):
        listed, result = threading.Event(), {}
        fake = FakeNetwork([("02:00:00:00:00:01", "X5 TEST01.OSC", 100)])

        def run(args, **kwargs):
            if "list" in args:
                listed.set()
            return fake(args, **kwargs)
        patcher = patch.object(cc.subprocess, "run", side_effect=run)
        patcher.start()
        self.addCleanup(patcher.stop)
        worker = threading.Thread(target=lambda: result.update(heard=self.manager._camera_heard()))
        worker.start()
        return listed, result, worker

    def test_a_rescan_waits_for_another_scan_on_the_adapter_to_finish(self):
        with cc._scan_lock():
            listed, result, worker = self.rescan_in_thread()
            self.assertFalse(listed.wait(0.5), "the rescan went out while another scan held the lock")
        worker.join(5)
        self.assertTrue(listed.is_set())
        self.assertTrue(result["heard"])

    def test_a_rescan_that_never_gets_the_lock_is_skipped_and_logged(self):
        with patch.object(cc, "SCAN_LOCK_WAIT_S", 0.3), cc._scan_lock(), \
                self.assertLogs("uvicorn.error.camera", level="WARNING") as logs:
            listed, result, worker = self.rescan_in_thread()
            worker.join(5)
        self.assertFalse(listed.is_set())
        self.assertIsNone(result["heard"])
        self.assertTrue(any("camera scan skipped: another scan held" in line for line in logs.output))

    def test_reading_the_cached_list_takes_no_lock(self):
        with cc._scan_lock(), patch.object(cc.subprocess, "run",
                                           side_effect=FakeNetwork([("02:00:00:00:00:01", "X5 TEST01.OSC", 100)])):
            start = time.monotonic()
            self.assertEqual(self.manager._why_not_associated(self.NM_ERROR), self.NM_ERROR)
            self.assertLess(time.monotonic() - start, 0.5)

    def test_a_camera_heard_in_the_latest_scan_keeps_networkmanagers_reason(self):
        heard, why = self.check([("02:00:00:00:00:03", "OtherNet", 100), ("02:00:00:00:00:01", "X5 TEST01.OSC", 100)])
        self.assertTrue(heard)
        self.assertEqual(why, self.NM_ERROR)

    def test_a_stale_entry_for_a_camera_that_went_quiet_counts_as_off(self):
        heard, why = self.check([("02:00:00:00:00:01", "X5 TEST01.OSC", 40)])
        self.assertFalse(heard)
        self.assertIn("is not on the air", why)

    def test_a_camera_that_is_absent_is_named_as_off(self):
        heard, why = self.check([("02:00:00:00:00:03", "OtherNet", 100)])
        self.assertFalse(heard)
        self.assertEqual(why, "The camera's Wi-Fi (X5 TEST01.OSC) is not on the air. Turn the camera on and "
                              "switch its Wi-Fi on, then connect again.")

    def test_a_camera_heard_at_another_address_names_the_pinned_one(self):
        heard, why = self.check([("AA:BB:CC:DD:EE:FF", "X5 TEST01.OSC", 100)])
        self.assertFalse(heard)
        self.assertIn("on the air at AA:BB:CC:DD:EE:FF, but its connection profile is pinned to "
                      "02:00:00:00:00:01", why)

    def test_an_unreadable_scan_says_it_cannot_tell(self):
        heard, why = self.check([], fail=True)
        self.assertIsNone(heard)
        self.assertEqual(why, self.NM_ERROR)

if __name__ == "__main__":
    unittest.main()
