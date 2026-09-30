"""The station shuts itself off 2 minutes after its last use with the arm out, bringing the arm home along its poses."""

import asyncio
import re
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sandbox import setUpModule, tearDownModule  # noqa: F401
import main

APP = Path(__file__).resolve().parents[1]


async def asgi(method, path):
    """One request through the whole app, middleware included; the response status."""
    sent = []
    scope = {'type': 'http', 'method': method, 'path': path, 'raw_path': path.encode(), 'query_string': b'',
             'headers': [], 'client': ('198.51.100.30', 5000), 'server': ('127.0.0.1', 8000), 'scheme': 'http',
             'http_version': '1.1', 'root_path': ''}

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(message):
        sent.append(message)

    await main.app(scope, receive, send)
    return next(m['status'] for m in sent if m['type'] == 'http.response.start')


class IdleCheckTests(unittest.TestCase):
    def setUp(self):
        self.controller = Mock(**{'status.return_value': {'done': True}})
        for name, value in (('controller', self.controller), ('_last_activity', 1000.0)):
            p = patch.object(main, name, value)
            p.start()
            self.addCleanup(p.stop)

    def check(self, now, deployed=True):
        with patch.object(main.arm_control, 'deployed', return_value=deployed):
            main.check_idle(now)

    def test_two_minutes_after_the_last_request_with_the_arm_out_the_station_shuts_off_once(self):
        with self.assertLogs('uvicorn.error.arm', level='WARNING') as logs:
            self.check(1120.0)
        self.check(1121.0)
        self.controller.disconnect.assert_called_once_with(main.home_and_release_arm, client='the 2-minute idle shutoff')
        self.assertIn('bringing the arm home along its poses', logs.output[0])

    def test_nothing_happens_before_two_minutes(self):
        self.check(1119.0)
        self.controller.disconnect.assert_not_called()

    def test_nothing_happens_while_the_arm_is_home_or_with_another_program(self):
        self.check(5000.0, deployed=False)
        self.controller.disconnect.assert_not_called()

    def test_camera_work_in_progress_restarts_the_count(self):
        self.controller.status.return_value = {'done': False}
        self.check(1500.0)
        self.controller.status.return_value = {'done': True}
        self.check(1619.0)
        self.controller.disconnect.assert_not_called()
        with self.assertLogs('uvicorn.error.arm', level='WARNING'):
            self.check(1620.0)
        self.controller.disconnect.assert_called_once()

    def test_going_home_retraces_the_selfie_poses_then_releases_the_arm(self):
        order = []
        with patch.object(main.arm_control, 'move_arm_home', side_effect=lambda: order.append('home along the poses')), \
                patch.object(main.arm_control, 'arm_release', side_effect=lambda: order.append('release')):
            main.home_and_release_arm()
        self.assertEqual(order, ['home along the poses', 'release'])


class ActivityTests(unittest.TestCase):
    def test_a_post_counts_as_use_and_a_status_poll_does_not(self):
        controller = Mock(**{'status.return_value': {'status': 'idle', 'done': True}})
        with patch.object(main, '_last_activity', 0.0), patch.object(main, 'controller', controller):
            self.assertEqual(asyncio.run(asgi('GET', '/status')), 200)
            self.assertEqual(main._last_activity, 0.0)
            self.assertEqual(asyncio.run(asgi('POST', '/activity')), 204)
            self.assertGreater(main._last_activity, 0.0)


class WatchTests(unittest.TestCase):
    def test_the_app_runs_the_check_from_start_to_shutdown(self):
        checked = threading.Event()

        async def run():
            async with main.lifespan(main.app):
                self.assertTrue(await asyncio.to_thread(checked.wait, 2))
                watchers = [t for t in threading.enumerate() if t.name == 'idle-shutoff']
                self.assertEqual(len(watchers), 1)
                return watchers[0]

        with patch.object(main, 'IDLE_CHECK_S', 0.01), patch.object(main, 'controller', Mock()), \
                patch.object(main, 'check_idle', side_effect=lambda now: checked.set()):
            watcher = asyncio.run(run())
        watcher.join(2)
        self.assertFalse(watcher.is_alive())


class PageTests(unittest.TestCase):
    def test_the_page_times_out_with_the_app_and_tells_it_while_someone_is_using_it(self):
        page = (APP / 'index.html').read_text()
        timeout_ms = int(re.search(r'idleTimeoutMs: (\d+)', page)[1])
        self.assertEqual(timeout_ms / 1000, main.IDLE_SHUTOFF_S)
        self.assertEqual(main.IDLE_SHUTOFF_S, 120.0, 'the page and the app shut off after 2 minutes')
        reset = page[page.index('function resetIdleTimer()'):page.index('function clearIdleTimer()')]
        self.assertIn("fetch('/activity', {method: 'POST'", reset)
        self.assertIn('executeDisconnect(true)', reset)


class PageSyncTests(unittest.TestCase):
    def setUp(self):
        self.page = (APP / 'index.html').read_text()

    def function(self, name):
        start = self.page.index(f'function {name}(')
        return self.page[start:self.page.index('\n    }\n', start)]

    def test_a_late_timer_asks_the_app_before_it_disconnects_or_says_anything(self):
        reset = self.function('resetIdleTimer')
        timeout = reset[reset.index('setTimeout('):]
        self.assertLess(timeout.index('await syncWithApp()'), timeout.index('executeDisconnect(true)'))
        self.assertNotIn("'error'", timeout)

    def test_the_page_shows_the_apps_own_ending_when_the_station_was_already_disconnected(self):
        sync = self.function('syncWithApp')
        self.assertIn("fetch('/status'", sync)
        self.assertIn("data.kind !== 'disconnect' || !data.done", sync)
        self.assertIn("setView('IDLE')", sync)
        self.assertIn("setStatus('connect', data.message", sync)
        start = self.page.index("addEventListener('visibilitychange'")
        listener = self.page[start:self.page.index('});', start)]
        self.assertIn("document.visibilityState === 'visible') syncWithApp()", listener)


if __name__ == '__main__':
    unittest.main()
