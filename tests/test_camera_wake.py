"""A connect that finds the camera's Wi-Fi off wakes it over Bluetooth first, without the station user doing anything.

Whoever uses the station only ever sees a plain message; what the operator has to do goes in the detail.
"""

import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import camera_connection as cc
from sandbox import setUpModule, tearDownModule  # noqa: F401


class ReadyStream:
    def start(self, **kwargs):
        pass

    def wait_ready(self, deadline):
        pass

    def stop(self):
        pass

    def error(self):
        return None


class WakeFlowTests(unittest.TestCase):
    def setUp(self):
        self.manager = cc.CameraConnectionManager(ReadyStream(), session=Mock())
        self.addCleanup(self.manager.close)
        self.stages = []
        stage = self.manager._stage

        def record(op, name, message, detail=""):
            self.stages.append((name, message, detail))
            stage(op, name, message, detail)
        for name, value in (("WAKE_BEACON_S", 0.3), ("WAKE_SETTLE_S", 0.1), ("WAKE_POLL_S", 0.01)):
            p = patch.object(cc, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.reachable = iter([False] + [True] * 50)
        self.beacon = object()
        self.start_beacon = Mock(return_value=(self.beacon, ""))
        self.stop_beacon = Mock()
        for target, value in (("_stage", record), ("_is_api_reachable", lambda: next(self.reachable)),
                              ("_start_beacon", self.start_beacon), ("_stop_beacon", self.stop_beacon),
                              ("_camera_advertising", Mock(return_value=True))):
            p = patch.object(self.manager, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(cc.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", ""))
        self.run = p.start()
        self.addCleanup(p.stop)

    def connect(self, heard, advertising=True, swept=False):
        self.manager._camera_advertising.return_value = advertising
        self.sweep = Mock(return_value=swept)

        def look(command="scan"):
            return self.sweep() if command == "sweep" else heard()
        with patch.object(self.manager, "_look", side_effect=look), \
                self.assertLogs("uvicorn.error.camera", level="INFO"):
            op = self.manager.connect(client="198.51.100.169")
            return self.manager.operations[op]["future"].result(timeout=5)

    def activated(self):
        return any("up" in call.args[0] for call in self.run.call_args_list)

    def test_the_camera_comes_up_only_on_its_own_adapter(self):
        self.connect(lambda: True)
        up = [call.args[0] for call in self.run.call_args_list if "up" in call.args[0]]
        self.assertEqual(len(up), 1)
        self.assertEqual(up[0][up[0].index("ifname") + 1], cc.INTERFACE)

    def test_a_camera_already_on_the_air_connects_without_a_beacon(self):
        result = self.connect(lambda: True)
        self.assertEqual(result["stage"], "ready")
        self.start_beacon.assert_not_called()

    def test_a_sleeping_camera_is_woken_and_then_connected(self):
        heard = iter([False, False, True])
        result = self.connect(lambda: next(heard))
        self.assertEqual(result["stage"], "ready")
        self.assertIn(("waking", "Waking the camera; this can take up to two minutes...",
                       "The camera Wi-Fi is not on the air; sending the Bluetooth wake beacon."), self.stages)
        self.stop_beacon.assert_called_once_with(self.beacon)
        self.assertTrue(self.activated())

    def test_a_camera_that_woke_without_wifi_tells_the_operator_to_switch_it_on(self):
        result = self.connect(lambda: False, advertising=True)
        self.assertEqual((result["stage"], result["message"]), ("error", cc.NOT_READY))
        self.assertIn("woke over Bluetooth, but its Wi-Fi stayed off", result["detail"])
        self.sweep.assert_called_once_with()
        self.stop_beacon.assert_called_once_with(self.beacon)
        self.assertFalse(self.activated())

    def test_a_camera_awake_over_bluetooth_gets_one_full_sweep_that_can_find_it(self):
        result = self.connect(lambda: False, advertising=True, swept=True)
        self.assertEqual(result["stage"], "ready")
        self.sweep.assert_called_once_with()
        self.assertIn("sweeping", [name for name, _, _ in self.stages])
        self.assertTrue(self.activated())

    def test_the_sweep_comes_only_after_the_bluetooth_check(self):
        order = []
        self.manager._camera_advertising.side_effect = lambda: order.append("bluetooth") or True
        with patch.object(self.manager, "_look",
                          side_effect=lambda command="scan": order.append("sweep") if command == "sweep" else False), \
                self.assertLogs("uvicorn.error.camera", level="INFO"):
            op = self.manager.connect(client="198.51.100.169")
            self.manager.operations[op]["future"].result(timeout=5)
        self.assertEqual(order, ["bluetooth", "sweep"])

    def test_a_camera_asleep_over_bluetooth_gets_no_sweep(self):
        result = self.connect(lambda: False, advertising=False)
        self.sweep.assert_not_called()
        self.assertIn("did not wake over Bluetooth", result["detail"])

    def test_an_unknown_bluetooth_state_still_gets_the_sweep(self):
        self.connect(lambda: False, advertising=None)
        self.sweep.assert_called_once_with()

    def test_a_camera_that_did_not_wake_asks_for_its_power_button(self):
        result = self.connect(lambda: False, advertising=False)
        self.assertEqual(result["message"], cc.NOT_READY)
        self.assertIn("did not wake over Bluetooth. Press its power button", result["detail"])

    def test_an_unknown_bluetooth_state_names_both_hand_actions(self):
        result = self.connect(lambda: False, advertising=None)
        self.assertIn("If the camera is on, switch its Wi-Fi on; if not, press its power button",
                      result["detail"])

    def test_a_beacon_that_could_not_be_sent_says_why(self):
        self.start_beacon.return_value = (None, "Failed to register advertisement: org.bluez.Error.Failed")
        result = self.connect(lambda: False)
        self.assertEqual(result["message"], cc.NOT_READY)
        self.assertIn("could not be sent (Failed to register advertisement", result["detail"])

    def test_a_disconnect_during_the_wake_stops_the_beacon(self):
        cc.WAKE_BEACON_S = 5.0
        waking = threading.Event()

        def heard(command="scan"):
            waking.set()
            return False
        with patch.object(self.manager, "_look", side_effect=heard), \
                self.assertLogs("uvicorn.error.camera", level="INFO"):
            op = self.manager.connect(client="198.51.100.169")
            self.assertTrue(waking.wait(2))
            self.manager.disconnect(client="198.51.100.49")
            result = self.manager.operations[op]["future"].result(timeout=5)
        self.assertEqual(result["stage"], "cancelled")
        self.stop_beacon.assert_called_once_with(self.beacon)

    def test_any_other_connect_failure_shows_the_user_a_plain_message(self):
        def activation(args, **kwargs):
            if "up" in args:
                raise subprocess.CalledProcessError(4, args, "", "Error: Timeout expired (25 seconds)")
            return subprocess.CompletedProcess(args, 0, "", "")
        self.run.side_effect = activation
        with patch.object(self.manager, "_why_not_associated", side_effect=lambda error: error):
            result = self.connect(lambda: True)
        self.assertEqual(result["message"], cc.NOT_READY)
        self.assertEqual(result["detail"], "NetworkManager activation failed: Error: Timeout expired (25 seconds)")


class PreflightTests(unittest.TestCase):
    """Connect checks the camera's USB Wi-Fi adapter first, and resets it once through the helper when it is not ready."""

    def setUp(self):
        self.manager = cc.CameraConnectionManager(ReadyStream(), session=Mock())
        self.addCleanup(self.manager.close)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.helper = str(Path(folder.name) / "selfie-camera-control")
        Path(self.helper).write_text("")
        self.stages, self.calls, self.checks = [], [], []
        self.reset_answer = (0, "ready\n", "")
        stage = self.manager._stage

        def record(op, name, message, detail=""):
            self.stages.append(name)
            stage(op, name, message, detail)
        self.reachable = iter([False] + [True] * 50)
        self.look = Mock(side_effect=lambda: self.calls.append(["look"]) or True)
        for target, value in (("_stage", record), ("_is_api_reachable", lambda: next(self.reachable)),
                              ("_look", self.look)):
            p = patch.object(self.manager, target, value)
            p.start()
            self.addCleanup(p.stop)
        for target, name, value in ((cc, "HELPER", self.helper), (cc.subprocess, "run", Mock(side_effect=self.network))):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def network(self, args, **kwargs):
        self.calls.append(list(args))
        if args == [cc.HELPER, "check"]:
            if not os.path.exists(cc.HELPER):
                raise FileNotFoundError(cc.HELPER)
            code, out = self.checks.pop(0)
            return subprocess.CompletedProcess(args, code, out, "")
        if args == ["sudo", "-n", cc.HELPER, "reset"]:
            return subprocess.CompletedProcess(args, *self.reset_answer)
        return subprocess.CompletedProcess(args, 0, "", "")

    def connect(self):
        with self.assertLogs("uvicorn.error.camera", level="INFO"):
            op = self.manager.connect(client="198.51.100.169")
            return self.manager.operations[op]["future"].result(timeout=5)

    def resets(self):
        return [c for c in self.calls if c == ["sudo", "-n", cc.HELPER, "reset"]]

    def activated(self):
        return any("up" in c for c in self.calls)

    def test_a_ready_adapter_is_checked_once_and_never_reset(self):
        self.checks = [(0, "ready\n")]
        self.assertEqual(self.connect()["stage"], "ready")
        self.assertEqual(self.calls.count([cc.HELPER, "check"]), 1)
        self.assertEqual(self.resets(), [])
        self.assertIn("checking_adapter", self.stages)

    def test_a_broken_adapter_is_reset_once_and_the_connect_goes_on(self):
        self.checks = [(1, "mt76x2u did not create wlxtest0\n"), (0, "ready\n")]
        self.assertEqual(self.connect()["stage"], "ready")
        self.assertEqual(len(self.resets()), 1)
        self.assertEqual([c for c in self.calls if c[-1] in ("check", "reset")],
                         [[cc.HELPER, "check"], ["sudo", "-n", cc.HELPER, "reset"], [cc.HELPER, "check"]])
        self.assertIn("resetting_adapter", self.stages)

    def test_an_adapter_a_reset_cannot_fix_asks_for_a_replug_and_goes_no_further(self):
        self.checks = [(1, "mt76x2u did not create wlxtest0\n")] * 2
        result = self.connect()
        self.assertEqual((result["stage"], result["message"]), ("error", cc.NOT_READY))
        self.assertIn("(mt76x2u did not create wlxtest0), and a reset did not bring it back. Unplug the adapter",
                      result["detail"])
        self.look.assert_not_called()
        self.assertFalse(self.activated())

    def test_a_reset_that_sudo_refuses_is_named(self):
        self.checks = [(1, "USB device 1-6 is not bound to mt76x2u\n")] * 2
        self.reset_answer = (1, "", "sudo: a password is required")
        self.assertIn("it could not be reset (sudo: a password is required)", self.connect()["detail"])

    def test_a_missing_helper_names_the_install_command_and_resets_nothing(self):
        os.remove(self.helper)
        result = self.connect()
        self.assertEqual(result["message"], cc.NOT_READY)
        self.assertIn("sudo bash scripts/install-camera-helper.sh", result["detail"])
        self.assertEqual(self.resets(), [])
        self.look.assert_not_called()

    def test_an_existing_camera_link_skips_the_adapter_check(self):
        self.reachable = iter([True] * 50)
        self.assertEqual(self.connect()["stage"], "ready")
        self.assertFalse(any(cc.HELPER in c for c in self.calls))

    def test_the_adapter_check_comes_before_any_scan_or_activation(self):
        self.checks = [(0, "ready\n")]
        self.connect()
        steps = [c[-1] if c[-1] in ("check", "look") else "up" if "up" in c else None for c in self.calls]
        steps = [s for s in steps if s]
        self.assertEqual(steps, ["check", "look", "up"])


class FastScanTests(unittest.TestCase):
    """The helper's kernel scan of the camera's channels decides; a full NetworkManager scan stands in when it cannot run."""

    def setUp(self):
        self.manager = cc.CameraConnectionManager(ReadyStream(), session=Mock())
        self.addCleanup(self.manager.close)
        self.scan_answer = (0, "", "")
        self.profile = "X5 TEST01.OSC\n02:00:00:00:00:01\n"
        self.calls = []
        self.full = Mock(return_value="full scan")
        for target, name, value in ((cc.subprocess, "run", Mock(side_effect=self.network)),
                                    (self.manager, "_camera_heard", self.full)):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def network(self, args, **kwargs):
        self.calls.append(list(args))
        if args[:3] == ["sudo", "-n", cc.HELPER]:
            return subprocess.CompletedProcess(args, *self.scan_answer)
        if "connection" in args and "show" in args:
            return subprocess.CompletedProcess(args, 0, self.profile, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def test_the_pinned_camera_in_the_fast_scan_counts_and_its_channel_is_logged(self):
        self.scan_answer = (0, "02:00:00:00:00:03\t5805\tlevel5_\n02:00:00:00:00:01\t5745\tX5 TEST01.OSC\n", "")
        with self.assertLogs("uvicorn.error.camera", level="INFO") as logs:
            self.assertIs(self.manager._look(), True)
        self.assertTrue(any("heard on 5745 MHz" in line for line in logs.output))
        self.assertIn(["sudo", "-n", cc.HELPER, "scan"], self.calls)
        self.full.assert_not_called()

    def test_a_fast_scan_without_the_camera_reads_as_off(self):
        self.scan_answer = (0, "02:00:00:00:00:03\t5805\tlevel5_\n", "")
        self.assertIs(self.manager._look(), False)
        self.full.assert_not_called()

    def test_the_camera_name_at_another_address_does_not_count_while_one_is_pinned(self):
        self.scan_answer = (0, "AA:BB:CC:DD:EE:FF\t5745\tX5 TEST01.OSC\n", "")
        self.assertIs(self.manager._look(), False)

    def test_without_a_pinned_address_the_name_decides(self):
        self.profile = "X5 TEST01.OSC\n\n"
        self.scan_answer = (0, "AA:BB:CC:DD:EE:FF\t5180\tX5 TEST01.OSC\n", "")
        with self.assertLogs("uvicorn.error.camera", level="INFO"):
            self.assertIs(self.manager._look(), True)

    def test_a_fast_scan_that_cannot_run_falls_back_and_says_so_once_and_again_when_it_returns(self):
        self.scan_answer = (1, "", "sudo: a password is required")
        with self.assertLogs("uvicorn.error.camera", level="INFO") as logs:
            self.assertEqual(self.manager._look(), "full scan")
            self.assertEqual(self.manager._look(), "full scan")
            self.scan_answer = (0, "02:00:00:00:00:01\t5745\tX5 TEST01.OSC\n", "")
            self.assertIs(self.manager._look(), True)
        down = [line for line in logs.output if "helper scan unavailable (sudo: a password is required)" in line]
        self.assertEqual(len(down), 1)
        self.assertTrue(any("helper scan running again" in line for line in logs.output))
        self.assertEqual(self.full.call_args_list, [unittest.mock.call(rescan=True)] * 2)

    def test_the_sweep_asks_the_helper_for_every_channel_and_falls_back_the_same_way(self):
        self.scan_answer = (0, "02:00:00:00:00:01\t5500\tX5 TEST01.OSC\n", "")
        with self.assertLogs("uvicorn.error.camera", level="INFO"):
            self.assertIs(self.manager._look("sweep"), True)
        sweep = [c for c in cc.subprocess.run.call_args_list if c.args[0] == ["sudo", "-n", cc.HELPER, "sweep"]]
        self.assertEqual(len(sweep), 1)
        self.assertEqual(sweep[0].kwargs["timeout"], cc.SCAN_TIMEOUT_S["sweep"])
        self.scan_answer = (1, "", "sudo: a password is required")
        with self.assertLogs("uvicorn.error.camera", level="WARNING"):
            self.assertEqual(self.manager._look("sweep"), "full scan")

    def test_every_connect_logs_its_own_fallback(self):
        self.manager.helper_scan_down = "sudo: a password is required"
        self.scan_answer = (1, "", "sudo: a password is required")
        reachable = iter([False] + [True] * 10)
        with patch.object(self.manager, "_is_api_reachable", side_effect=lambda timeout=0.5: next(reachable)), \
                patch.object(self.manager, "_preflight"), \
                self.assertLogs("uvicorn.error.camera", level="WARNING") as logs:
            op = self.manager.connect(client="198.51.100.30")
            self.assertEqual(self.manager.operations[op]["future"].result(timeout=5)["stage"], "ready")
        self.assertTrue(any("helper scan unavailable (sudo: a password is required)" in line for line in logs.output))

    def test_an_unreadable_profile_cannot_tell(self):
        self.scan_answer = (0, "02:00:00:00:00:01\t5745\tX5 TEST01.OSC\n", "")

        def run(args, **kwargs):
            if "connection" in args:
                raise subprocess.CalledProcessError(10, args, "", "no such connection")
            return self.network(args, **kwargs)
        cc.subprocess.run.side_effect = run
        self.assertIsNone(self.manager._look())


class FakeBluetoothctl:
    def __init__(self, said):
        self.stdout = iter(said)
        self.stdin = self
        self.written = ""
        self.returncode = None

    def write(self, text):
        self.written += text

    def flush(self):
        pass

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -9


class BeaconTests(unittest.TestCase):
    def setUp(self):
        self.manager = cc.CameraConnectionManager(ReadyStream(), session=Mock())
        self.addCleanup(self.manager.close)

    def start(self, said):
        fake = FakeBluetoothctl(said)
        with patch.object(cc.subprocess, "Popen", return_value=fake):
            return fake, self.manager._start_beacon()

    def test_a_registered_beacon_carries_the_camera_wake_data(self):
        fake, (proc, why) = self.start(["[bluetooth]# advertise on\n", "Advertising object registered\n"])
        self.assertIs(proc, fake)
        self.assertEqual(why, "")
        self.assertIn("manufacturer 0x004c 0x02 0x15 0x09 0x4f 0x52 0x42 0x49 0x54 0x09 0xff 0x0f 0x00 "
                      "0x54 0x45 0x53 0x54 0x30 0x31 0x00 0x00 0x00 0x00 0xe4 0x01\nback\nadvertise on\n",
                      fake.written)

    def test_a_refused_beacon_is_stopped_and_names_the_refusal(self):
        fake, (proc, why) = self.start(["Failed to register advertisement: org.bluez.Error.Failed\n"])
        self.assertIsNone(proc)
        self.assertEqual(why, "Failed to register advertisement: org.bluez.Error.Failed")
        self.assertTrue(fake.written.endswith("advertise off\nquit\n"))


class AdvertisingTests(unittest.TestCase):
    def setUp(self):
        self.manager = cc.CameraConnectionManager(ReadyStream(), session=Mock())
        self.addCleanup(self.manager.close)

    def scan(self, scan_output, fail=False):
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            if fail:
                raise subprocess.TimeoutExpired(args, 5)
            if args[1] == "devices":
                return subprocess.CompletedProcess(args, 0, "Device 02:00:00:00:00:02 X5 TEST01\n"
                                                          "Device 02:00:00:00:00:04 SOLIX C1000 Gen 2\n", "")
            return subprocess.CompletedProcess(args, 0, scan_output if "scan" in args else "", "")
        with patch.object(cc.subprocess, "run", side_effect=run):
            return self.manager._camera_advertising(), calls

    def test_only_a_live_advertisement_counts_after_the_cache_is_cleared(self):
        seen, calls = self.scan("[NEW] Device 02:00:00:00:00:02 X5 TEST01\n")
        self.assertTrue(seen)
        self.assertIn(["bluetoothctl", "remove", "02:00:00:00:00:02"], calls)
        self.assertNotIn(["bluetoothctl", "remove", "02:00:00:00:00:04"], calls)

    def test_a_scan_without_the_camera_reads_as_asleep(self):
        self.assertFalse(self.scan("[NEW] Device 02:00:00:00:00:04 SOLIX C1000 Gen 2\n")[0])

    def test_a_scan_that_fails_cannot_tell(self):
        self.assertIsNone(self.scan("", fail=True)[0])


if __name__ == "__main__":
    unittest.main()
