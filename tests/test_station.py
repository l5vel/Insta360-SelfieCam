"""Site values come from station.toml, and camera operations refuse, naming the missing value, while it is incomplete."""

import ast
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException

import camera_connection as cc
import station
from sandbox import setUpModule, tearDownModule  # noqa: F401
import main

APP = Path(__file__).resolve().parents[1]
MISSING = 'station.toml has no [camera] serial'


class StationFileTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'station.toml'

    def test_a_set_value_is_read(self):
        self.path.write_text('[camera]\nip = " 192.0.2.1 "\n')
        self.assertEqual(station.get('camera', 'ip', self.path), ('192.0.2.1', ''))

    def test_a_missing_file_names_the_example_to_copy(self):
        value, why = station.get('camera', 'ip', self.path)
        self.assertEqual(value, '')
        self.assertIn('station.toml is missing; copy station.example.toml', why)

    def test_a_missing_blank_or_non_string_value_is_named(self):
        for text in ('', '[camera]\n', '[camera]\nip = ""\n', '[camera]\nip = 5\n', 'camera = "x"\n'):
            self.path.write_text(text)
            self.assertEqual(station.get('camera', 'ip', self.path), ('', 'station.toml has no [camera] ip'), text)

    def test_a_file_that_is_not_toml_is_named(self):
        self.path.write_text('[camera\n')
        value, why = station.get('camera', 'ip', self.path)
        self.assertEqual(value, '')
        self.assertIn('could not be read', why)

    def test_the_example_lists_every_value_the_app_and_recorder_read(self):
        example = tomllib.loads((APP / 'station.example.toml').read_text())
        wanted = {('camera', key) for key in cc._CAMERA}
        for path in (APP / 'main.py', APP / 'scripts/camera_recorder.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if (isinstance(node, ast.Call) and ast.unparse(node.func) == 'station.get'
                        and all(isinstance(a, ast.Constant) for a in node.args)):
                    wanted.add(tuple(a.value for a in node.args))
        self.assertIn(('email', 'sender'), wanted)
        for section, key in wanted:
            self.assertIn(key, example.get(section, {}), f'[{section}] {key}')


class StationProblemTests(unittest.TestCase):
    SET = {key: ('x', '') for key in ('wifi_interface', 'nm_profile_uuid', 'ip')}

    def test_a_serial_that_is_not_six_capitals_or_digits_is_named(self):
        self.assertEqual(cc.station_problem({**self.SET, 'serial': ('TEST01', '')}), '')
        for serial in ('TEST0', 'TEST012', 'test01', 'X5 TEST01'):
            self.assertIn('is not the six characters after "X5 "',
                          cc.station_problem({**self.SET, 'serial': (serial, '')}), serial)

    def test_a_missing_value_is_named_before_the_serial_is_judged(self):
        self.assertEqual(cc.station_problem({**self.SET, 'ip': ('', 'station.toml has no [camera] ip'),
                                             'serial': ('', MISSING)}), 'station.toml has no [camera] ip')

    def test_the_module_judges_every_camera_value_it_uses(self):
        tree = ast.parse((APP / 'camera_connection.py').read_text())
        assigned = {ast.unparse(n.targets[0]): ast.unparse(n.value) for n in tree.body if isinstance(n, ast.Assign)}
        self.assertEqual(assigned['STATION_PROBLEM'], 'station_problem(_CAMERA)')
        self.assertEqual(assigned['WAKE_BEACON'], 'wake_beacon(SERIAL)')
        self.assertEqual(set(cc._CAMERA), {'wifi_interface', 'nm_profile_uuid', 'ip', 'serial'})


class IncompleteStationTests(unittest.TestCase):
    def setUp(self):
        self.stream = Mock(destination=None)
        self.stream.error.return_value = None
        self.manager = cc.CameraConnectionManager(self.stream, session=Mock())
        self.addCleanup(self.manager.close)
        for p in (patch.object(cc, 'STATION_PROBLEM', MISSING),
                  patch.object(cc.CameraConnectionManager, '_is_api_reachable', return_value=True)):
            p.start()
            self.addCleanup(p.stop)
        run = patch.object(cc.subprocess, 'run')
        self.run = run.start()
        self.addCleanup(run.stop)

    def finish(self, op_id):
        return self.manager.operations[op_id]['future'].result(timeout=3)

    def assertLoggedError(self, logs, reason):
        self.assertTrue(any('stage=error' in line and reason in line for line in logs.output), logs.output)

    def test_connect_reconnect_and_resume_refuse_and_name_the_missing_value(self):
        for start in (self.manager.connect, lambda: self.manager.connect(ensure=True), self.manager.resume):
            with self.assertLogs('uvicorn.error.camera', 'INFO') as logs:
                done = self.finish(start())
            self.assertEqual((done['stage'], done['message'], done['detail']), ('error', cc.NOT_READY, MISSING))
            self.assertLoggedError(logs, MISSING)
        self.run.assert_not_called()
        self.stream.start.assert_not_called()

    def test_capture_refuses_without_asking_the_camera(self):
        with self.assertLogs('uvicorn.error.camera', 'INFO') as logs, self.assertRaises(cc.CameraError) as refused:
            self.manager.capture()
        self.assertEqual(str(refused.exception), cc.NOT_READY)
        self.assertLoggedError(logs, MISSING)
        self.manager.http.post.assert_not_called()

    def test_disconnect_still_brings_the_arm_home_and_says_why_it_left_the_wifi_alone(self):
        home = Mock()
        with patch.object(cc, 'PROFILE', ''):
            done = self.finish(self.manager.disconnect(home_arm=home))
        home.assert_called_once_with()
        self.run.assert_not_called()
        self.assertEqual(done['stage'], 'idle')
        self.assertIn(MISSING, done['message'])


class HelperMismatchTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.helper = Path(folder.name) / 'selfie-camera-control'
        self.manager = cc.CameraConnectionManager(Mock(), session=Mock())
        self.addCleanup(self.manager.close)

    def installed(self, iface):
        self.helper.write_text(f"#!/usr/bin/python3 -I\nINTERFACE = {iface!r}\n")
        return patch.object(cc, 'HELPER', str(self.helper))

    def test_a_helper_installed_for_another_adapter_stops_the_connect_and_names_the_reinstall(self):
        for iface, named in (('wlxother0', 'wlxother0'), ('', 'no adapter')):
            with self.installed(iface), patch.object(cc.subprocess, 'run') as run, \
                    patch.object(cc.CameraConnectionManager, '_is_api_reachable', return_value=False), \
                    self.assertLogs('uvicorn.error.camera', 'INFO') as logs:
                done = self.manager.operations[self.manager.connect()]['future'].result(timeout=3)
            self.assertEqual((done['stage'], done['message']), ('error', cc.NOT_READY))
            self.assertTrue(any('stage=error' in line and f'runs on {named}' in line for line in logs.output))
            self.assertIn(f'runs on {named}, but station.toml names {cc.INTERFACE}', done['detail'])
            self.assertIn('sudo bash scripts/install-camera-helper.sh', done['detail'])
            run.assert_not_called()

    def test_a_helper_for_this_adapter_goes_on_to_check_it(self):
        op = {'id': 't', 'stage': 'queued', 'stage_at': 0.0, 'started': 0.0, 'timings': {}}
        with self.installed(cc.INTERFACE), \
                patch.object(cc.CameraConnectionManager, '_helper', return_value=(True, 'ready')) as helper:
            self.manager._preflight(op, cc.Deadline(5, threading.Event()))
        helper.assert_called_once_with('check')


class ReleaseArmEndpointTests(unittest.TestCase):
    def test_the_release_endpoint_lets_go_where_the_arm_stands_and_says_what_happened(self):
        request = Mock(client=Mock(host='198.51.100.49'))
        for held in (True, False):
            with patch.object(main.arm_control, 'release_where_it_stands', return_value=held) as release:
                self.assertEqual(main.release_arm(request), {'released': held})
            release.assert_called_once_with()


class EmailSenderTests(unittest.TestCase):
    def test_email_refuses_with_the_reason_when_no_sender_is_set(self):
        with patch.multiple(main, GMAIL_USERNAME='', GMAIL_USERNAME_UNSET='station.toml has no [email] sender',
                            GMAIL_PASSWORD='set'), patch.object(main.smtplib, 'SMTP_SSL') as smtp, \
                self.assertRaises(HTTPException) as refused:
            main.trigger_email(email='someone@example.com', image='/static/latest.jpg')
        self.assertEqual((refused.exception.status_code, refused.exception.detail),
                         (503, 'station.toml has no [email] sender'))
        smtp.assert_not_called()


if __name__ == '__main__':
    unittest.main()
