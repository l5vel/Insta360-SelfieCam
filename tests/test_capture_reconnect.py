"""Take Picture reconnects a camera that stopped answering before its countdown, and no capture leaves a camera connection open."""

import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

import camera_connection as cc
from sandbox import setUpModule, tearDownModule  # noqa: F401

APP = Path(__file__).resolve().parents[1]


class Stream:
    def __init__(self, destination=None):
        self.destination = destination
        self.calls = []

    def start(self, **kwargs):
        self.calls.append(('start', kwargs))

    def wait_ready(self, deadline):
        pass

    def stop(self):
        self.calls.append(('stop', {}))

    def error(self):
        return None


class Response:
    def __init__(self, payload=None, chunks=()):
        self.payload, self.chunks = payload, chunks
        self.ok, self.status_code, self.text = True, 200, ''

    def json(self):
        return self.payload

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        return iter(self.chunks)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class EnsureTests(unittest.TestCase):
    def setUp(self):
        self.stream = Stream(destination=('198.51.100.7', 7005))
        self.manager = cc.CameraConnectionManager(self.stream, session=Mock())
        self.addCleanup(self.manager.close)

    def ensure(self, reachable, connect):
        with patch.object(cc.CameraConnectionManager, '_is_api_reachable', return_value=reachable), \
                patch.object(cc.CameraConnectionManager, '_connect', side_effect=connect, autospec=True) as full:
            op_id = self.manager.connect(client='198.51.100.30', ensure=True)
            result = self.manager.operations[op_id]['future'].result(timeout=3)
        return result, full

    def test_a_camera_that_answers_is_left_alone(self):
        result, full = self.ensure(True, None)
        self.assertEqual((result['stage'], result['message']), ('linked', 'Camera connected.'))
        full.assert_not_called()
        self.assertEqual(self.stream.calls, [])

    def test_a_camera_that_stopped_answering_is_reconnected_to_the_previews_own_destination(self):
        def ready(manager, op, srt_ip, srt_port):
            manager._stage(op, 'ready', 'Camera connected and preview ready.')

        with self.assertLogs('uvicorn.error.camera', level='WARNING') as logs:
            result, full = self.ensure(False, ready)
        full.assert_called_once()
        self.assertEqual(full.call_args.args[2:], ('198.51.100.7', 7005))
        self.assertEqual(result['stage'], 'ready')
        self.assertIn('reconnecting', result['timings'])
        self.assertTrue(any('did not answer before a capture; reconnecting' in line for line in logs.output))

    def test_a_reconnect_that_fails_ends_in_the_station_message_with_the_reason_kept(self):
        def fail(manager, op, srt_ip, srt_port):
            raise cc.CameraError(cc.NOT_READY, 'The camera did not wake over Bluetooth.')

        with self.assertLogs('uvicorn.error.camera', level='WARNING'):
            result, _ = self.ensure(False, fail)
        self.assertEqual((result['stage'], result['message'], result['detail']),
                         ('error', cc.NOT_READY, 'The camera did not wake over Bluetooth.'))


class CaptureConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.http = Mock()
        self.stream = Stream()
        self.manager = cc.CameraConnectionManager(self.stream, image_path=Path(self.tmp.name) / 'latest.jpg',
                                                  session=self.http)
        self.addCleanup(self.manager.close)
        p = patch.object(cc.CameraConnectionManager, '_is_api_reachable', return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def capture(self):
        op_id = cc.CameraConnectionManager._submit(self.manager, 'capture', self.manager._capture)['id']
        return self.manager.operations[op_id]['future'].result(timeout=3)

    def test_a_capture_closes_its_camera_connections_when_it_ends(self):
        self.http.post.return_value = Response({'state': 'done', 'results': {'fileUrl': f'http://{cc.CAMERA_IP}/x.jpg'}})
        self.http.get.return_value = Response(chunks=[b'jpeg'])
        closes_before = self.http.close.call_count
        result = self.capture()
        self.assertEqual(result['stage'], 'captured')
        self.assertEqual(self.http.close.call_count - closes_before, 1)
        self.assertEqual((Path(self.tmp.name) / 'latest.jpg').read_bytes(), b'jpeg')

    def test_a_capture_that_fails_closes_them_too(self):
        self.http.post.side_effect = requests.ConnectionError('Connection reset by peer')
        closes_before = self.http.close.call_count
        with self.assertLogs('uvicorn.error.camera', level='ERROR'):
            result = self.capture()
        self.assertEqual(result['stage'], 'error')
        self.assertEqual(self.http.close.call_count - closes_before, 1)


class DisconnectTests(unittest.TestCase):
    def setUp(self):
        self.manager = cc.CameraConnectionManager(Stream(), session=Mock())
        self.addCleanup(self.manager.close)

    def disconnect(self, nmcli_error):
        with patch.object(cc.subprocess, 'run', side_effect=nmcli_error):
            op_id = self.manager.disconnect(client='198.51.100.30')
            return self.manager.operations[op_id]['future'].result(timeout=3)

    def test_a_camera_link_already_down_disconnects_without_a_warning(self):
        down = cc.subprocess.CalledProcessError(cc.NMCLI_NOT_ACTIVE, ['nmcli'], stderr="Error: 'X5' is not an active connection.")
        result = self.disconnect(down)
        self.assertEqual((result['message'], result['result']), ('Camera disconnected by 198.51.100.30.', {'warnings': []}))

    def test_any_other_networkmanager_failure_is_still_reported(self):
        result = self.disconnect(cc.subprocess.CalledProcessError(8, ['nmcli'], stderr='Error: NetworkManager is not running.'))
        self.assertEqual(result['message'], 'Camera disconnected by 198.51.100.30. '
                                            'NetworkManager disconnect: Error: NetworkManager is not running.')


class BusyAdapterTests(unittest.TestCase):
    def setUp(self):
        self.manager = cc.CameraConnectionManager(Stream(), session=Mock())
        self.addCleanup(self.manager.close)

    def test_a_scan_that_cannot_get_the_adapter_ends_the_connect_with_a_retry_prompt(self):
        busy = (False, 'the adapter stayed busy with another scan for 30 s')
        with patch.object(cc.CameraConnectionManager, '_is_api_reachable', return_value=False), \
                patch.object(cc.CameraConnectionManager, '_preflight'), \
                patch.object(cc.CameraConnectionManager, '_helper', return_value=busy), \
                patch.object(cc.CameraConnectionManager, '_camera_heard', side_effect=AssertionError('fell back')), \
                self.assertLogs('uvicorn.error.camera', level='WARNING'):
            op_id = self.manager.connect(client='198.51.100.30')
            result = self.manager.operations[op_id]['future'].result(timeout=3)
        self.assertEqual((result['stage'], result['message']), ('error', cc.BUSY_RETRY))
        self.assertIn('stayed busy with another scan for 30 s', result['detail'])

    def test_a_helper_that_cannot_run_for_another_reason_still_falls_back_to_networkmanager(self):
        missing = (False, '/usr/local/libexec/selfie-camera-control is not installed')
        with patch.object(cc.CameraConnectionManager, '_helper', return_value=missing), \
                patch.object(cc.CameraConnectionManager, '_camera_heard', return_value=True) as fallback, \
                self.assertLogs('uvicorn.error.camera', level='WARNING'):
            self.assertTrue(self.manager._look())
        fallback.assert_called_once_with(rescan=True)

    def test_the_helper_still_says_what_the_app_listens_for(self):
        helper = (APP / 'scripts/selfie-camera-control').read_text()
        busy_line = next(line for line in helper.splitlines() if 'raise RuntimeError' in line and 'busy' in line)
        self.assertIn(cc.HELPER_BUSY, busy_line)


class EndpointTests(unittest.TestCase):
    def test_the_connect_endpoint_passes_ensure_to_the_controller(self):
        import main
        request = SimpleNamespace(client=SimpleNamespace(host='198.51.100.30'))
        with patch.object(main, 'controller', Mock(**{'connect.return_value': 'op1'})) as controller:
            self.assertEqual(main.connect_camera(request, client_ip='', ensure=True), {'operation_id': 'op1'})
            main.connect_camera(request, client_ip='198.51.100.7')
        self.assertEqual(controller.connect.call_args_list[0].kwargs, {'client': '198.51.100.30', 'ensure': True})
        self.assertEqual(controller.connect.call_args_list[1].kwargs, {'client': '198.51.100.30', 'ensure': False})


class PageTests(unittest.TestCase):
    def setUp(self):
        page = (APP / 'index.html').read_text()
        start = page.index('async function takeSelfie()')
        self.body = page[start:page.index('\n    }\n', start)]

    def test_the_page_checks_the_camera_after_the_arm_and_before_the_countdown(self):
        steps = [self.body.index(s) for s in ("fetch('/position-arm", "cameraOperation('/connect?ensure=true'",
                                               'runCountdown(', "fetch('/capture'")]
        self.assertEqual(steps, sorted(steps))

    def test_every_reconnect_stage_and_its_failure_reach_the_screen(self):
        callback = self.body[self.body.index("cameraOperation('/connect?ensure=true'"):self.body.index('if (!link)')]
        self.assertRegex(callback, r"setStatus\('capture', data\.message")
        self.assertIn('showStreamMessage(data.message)', callback)
        failure = self.body[self.body.index('} catch (err) {'):]
        self.assertTrue(re.search(r"setStatus\('capture', parseError\(err\), 'error'\)", failure))
        self.assertIn('showStreamMessage(parseError(err))', failure)


if __name__ == '__main__':
    unittest.main()
