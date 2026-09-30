"""The camera's battery: read from its OSC state only while the preview runs, and shown only while fresh."""

import ast
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import requests

import camera_connection as cc
from sandbox import setUpModule, tearDownModule  # noqa: F401

APP = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def osc_session(reply=None, error=None):
    """A requests.Session stand-in whose post answers with `reply` as JSON, or raises `error`."""
    http = MagicMock()
    http.__enter__.return_value = http
    if error:
        http.post.side_effect = error
    else:
        http.post.return_value.json.return_value = reply
    return http


class ReadBatteryTests(unittest.TestCase):
    def read(self, **kwargs):
        http = osc_session(**kwargs)
        with patch.object(cc.requests, 'Session', return_value=http):
            return cc.CameraConnectionManager._read_battery(), http

    def test_the_osc_battery_level_becomes_a_percent(self):
        percent, http = self.read(reply={'fingerprint': 'x', 'state': {'batteryLevel': 0.82}})
        self.assertEqual(percent, 82)
        self.assertEqual(http.post.call_args.args[0], f'http://{cc.CAMERA_IP}/osc/state')
        self.assertEqual(http.post.call_args.kwargs['json'], {})
        self.assertFalse(http.trust_env)

    def test_a_reply_without_a_usable_level_gives_no_reading(self):
        for reply in ({'state': {}}, {}, {'state': {'batteryLevel': 'full'}}, {'state': {'batteryLevel': 1.5}},
                      {'state': {'batteryLevel': -0.1}}, {'state': None}):
            self.assertIsNone(self.read(reply=reply)[0], reply)

    def test_an_unreachable_camera_gives_no_reading(self):
        self.assertIsNone(self.read(error=requests.ConnectTimeout('no answer'))[0])


class CheckBatteryTests(unittest.TestCase):
    def setUp(self):
        self.stream = Mock()
        self.stream.live.return_value = True
        self.stream.error.return_value = None
        self.manager = cc.CameraConnectionManager(self.stream, session=Mock())
        self.addCleanup(self.manager.close)
        self.manager.link_expected = True
        self.clock = Clock()
        for p in (patch.object(cc.time, 'monotonic', self.clock),
                  patch.object(cc.CameraConnectionManager, '_read_battery', return_value=82)):
            p.start()
            self.addCleanup(p.stop)
        self.reads = cc.CameraConnectionManager._read_battery

    def test_a_live_preview_is_read_at_once_then_every_battery_read_s(self):
        self.manager._check_battery()
        self.assertEqual(self.manager.status()['battery'], {'percent': 82, 'age_s': 0.0})
        self.clock.now += cc.BATTERY_READ_S - 1
        self.manager._check_battery()
        self.assertEqual(self.reads.call_count, 1)
        self.clock.now += 1
        self.manager._check_battery()
        self.assertEqual(self.reads.call_count, 2)

    def test_no_read_while_the_preview_is_stopped_and_an_old_reading_stops_showing(self):
        self.manager._check_battery()
        self.stream.live.return_value = False
        self.clock.now += cc.BATTERY_READ_S
        self.manager._check_battery()
        self.assertEqual(self.reads.call_count, 1)
        self.assertEqual(self.manager.status()['battery']['percent'], 82)
        self.clock.now += cc.BATTERY_FRESH_S - cc.BATTERY_READ_S
        self.assertIsNone(self.manager.status()['battery'])

    def test_a_failed_read_waits_battery_read_s_before_trying_again(self):
        self.reads.return_value = None
        self.manager._check_battery()
        self.manager._check_battery()
        self.assertEqual(self.reads.call_count, 1)
        self.assertIsNone(self.manager.status()['battery'])

    def test_a_lost_or_ended_link_forgets_the_reading_and_reads_nothing(self):
        self.manager._check_battery()
        self.manager.link = 'the camera Wi-Fi is down'
        self.manager._check_battery()
        self.assertIsNone(self.manager.battery)
        self.manager.link, self.manager.link_expected = '', False
        self.manager._check_battery()
        self.assertEqual(self.reads.call_count, 1)
        self.manager.link_expected = True
        self.manager._check_battery()
        self.assertEqual(self.reads.call_count, 2, 'the next connect reads at once')

    def test_a_disconnect_forgets_the_reading_at_once(self):
        self.manager._check_battery()
        with patch.object(cc.subprocess, 'run'):
            self.manager.operations[self.manager.disconnect()]['future'].result(timeout=3)
        self.assertIsNone(self.manager.status()['battery'])

    def test_a_failed_connect_forgets_the_reading(self):
        self.manager._check_battery()
        self.manager._link_ends({'kind': 'connect'})
        self.assertIsNone(self.manager.battery)


class CallSiteTests(unittest.TestCase):
    def test_the_link_watcher_checks_the_battery_every_tick(self):
        tree = ast.parse((APP / 'camera_connection.py').read_text())
        watch = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_watch_link')
        calls = {ast.unparse(n.func) for n in ast.walk(watch) if isinstance(n, ast.Call)}
        self.assertIn('self._check_battery', calls)

    def test_every_status_carries_the_battery(self):
        manager = cc.CameraConnectionManager(Mock(), session=Mock())
        self.addCleanup(manager.close)
        manager.battery = (55, cc.time.monotonic())
        self.assertEqual(manager.status()['battery']['percent'], 55)
        with patch.object(cc.subprocess, 'run'):
            op = manager.disconnect()
            manager.operations[op]['future'].result(timeout=3)
        manager.battery = (54, cc.time.monotonic())
        self.assertEqual(manager.status(op)['battery']['percent'], 54)


if __name__ == '__main__':
    unittest.main()
