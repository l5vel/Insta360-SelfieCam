import concurrent.futures
import contextlib
import fcntl
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
import uuid
from urllib.parse import urlparse

import requests

import station

LOG = logging.getLogger('uvicorn.error.camera')

# This station's camera adapter, NetworkManager profile, camera address and serial, from station.toml.
_CAMERA = {key: station.get('camera', key) for key in ('wifi_interface', 'nm_profile_uuid', 'ip', 'serial')}
INTERFACE, PROFILE, CAMERA_IP, SERIAL = (value for value, _ in _CAMERA.values())


def station_problem(camera):
    """Why every camera operation but a disconnect refuses, '' when station.toml names all it needs."""
    why = next((why for _, why in camera.values() if why), '')
    serial = camera['serial'][0]
    if not why and not re.fullmatch('[0-9A-Z]{6}', serial):
        why = f'station.toml [camera] serial {serial!r} is not the six characters after "X5 " in its name'
    return why


def wake_beacon(serial):
    """The X5's Bluetooth wake beacon: Apple-format manufacturer data carrying the camera's serial."""
    return (bytes([0x02, 0x15, 0x09, 0x4f, 0x52, 0x42, 0x49, 0x54, 0x09, 0xff, 0x0f, 0x00])
            + serial.encode('ascii', 'replace') + bytes([0x00, 0x00, 0x00, 0x00, 0xe4, 0x01]))


STATION_PROBLEM = station_problem(_CAMERA)
WAKE_BEACON = wake_beacon(SERIAL)
CAMERA_BT_NAME = f'X5 {SERIAL}'
# How often a connected camera link is checked between operations.
LINK_CHECK_S = 3.0
# While the preview runs the battery is read this often, and a reading older than BATTERY_FRESH_S is not shown.
BATTERY_READ_S = 30.0
BATTERY_FRESH_S = 60.0
SYS_NET = Path('/sys/class/net')
# The beacon runs this long, the camera's Wi-Fi may appear this long after it, checked this often.
WAKE_BEACON_S = 60.0
WAKE_SETTLE_S = 20.0
WAKE_POLL_S = 3.0
SCAN_SPAN_S = 20
# What a station user sees when a connect fails; the operator's reason goes in the detail.
NOT_READY = "The camera isn't ready. Please try again in a minute, or ask for help."
BUSY_RETRY = "The camera Wi-Fi was busy. Please try again."
# The helper's error when another scan held the adapter for its whole wait (scripts/selfie-camera-control).
HELPER_BUSY = 'stayed busy with another scan'
# The adapter helper from scripts/install-camera-helper.sh: check runs as this app, reset and scan through sudo.
HELPER = '/usr/local/libexec/selfie-camera-control'
RESET_TIMEOUT_S = 90
SCAN_TIMEOUT_S = {'scan': 90, 'sweep': 130}
# A one-shot check of the camera's API waits this long, past one retransmitted SYN.
GATE_PROBE_S = 2.0
# Every scan request on the adapter, from this app, the helper or a test script, holds this lock.
SCAN_LOCK = Path('/run/lock/selfie-camera-scan.lock')
SCAN_LOCK_WAIT_S = 45
# nmcli's exit status when the connection to take down is not active (nmcli(1), EXIT STATUS).
NMCLI_NOT_ACTIVE = 10


def _boottime():
    """Seconds on NetworkManager's clock, CLOCK_BOOTTIME."""
    return time.clock_gettime(time.CLOCK_BOOTTIME)


@contextlib.contextmanager
def _scan_lock():
    """Exclusive hold on SCAN_LOCK; opens without O_CREAT first, since fs.protected_regular refuses it on another user's file."""
    try:
        fd = os.open(SCAN_LOCK, os.O_RDONLY)
    except FileNotFoundError:
        fd = os.open(SCAN_LOCK, os.O_RDONLY | os.O_CREAT, 0o644)
    try:
        end = time.monotonic() + SCAN_LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise TimeoutError(f'another scan held {SCAN_LOCK} for {SCAN_LOCK_WAIT_S} s')
                time.sleep(0.2)
        yield
    finally:
        os.close(fd)


class CameraError(RuntimeError):
    """str() is for whoever uses the station; detail is for the operator and the log."""

    def __init__(self, message='', detail=''):
        super().__init__(message)
        self.detail = detail


class Cancelled(CameraError):
    pass


class Deadline:
    """Tracks the remaining time for an operation and supports cancellation."""
    def __init__(self, seconds, cancel_event):
        self.end = time.monotonic() + seconds
        self.cancel = cancel_event

    def remaining(self, maximum_seconds):
        if self.cancel.is_set():
            raise Cancelled('Operation cancelled.')

        time_left = self.end - time.monotonic()
        value = min(maximum_seconds, time_left)

        if value <= 0:
            raise CameraError('Operation deadline exceeded.')
        return value

    def pause(self, seconds):
        self.cancel.wait(self.remaining(seconds))
        self.remaining(1)


class CameraConnectionManager:
    """Serializes blocking camera operations to prevent hardware contention."""

    def __init__(self, stream, image_path='static/latest.jpg', session=None):
        self.stream = stream
        self.image_path = Path(image_path)

        self.http = session or requests.Session()
        self.http.trust_env = False  # Avoid proxy issues on LAN

        self.lock = threading.RLock()
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix='camera'
        )

        self.operations = {}
        self.current = None
        self.lease = None
        self.closed = False
        self.link_expected = False   # a connect reached ready and no disconnect has run since
        self.link = ''               # why the connected link is unhealthy, '' when it is fine
        self.helper_scan_down = ''   # why the helper's scan last could not run, '' when it ran
        self.battery = None          # (percent, monotonic time read) from the camera's OSC state
        self._battery_tried = float('-inf')
        self._watch_stop = threading.Event()

    def start(self):
        """Acquires a file lock to ensure exclusive access to the camera hardware."""
        runtime_dir = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'))
        runtime_dir.mkdir(parents=True, exist_ok=True)
        path = runtime_dir / 'selfie-camera.lock'

        fd = os.open(str(path), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise CameraError('Camera is already owned by another app/daemon.')
        self.lease = fd
        threading.Thread(target=self._watch_link, name='camera-link', daemon=True).start()

    def close(self):
        """Gracefully shuts down the executor and active network streams."""
        self._watch_stop.set()
        with self.lock:
            self.closed = True
            for op in self.operations.values():
                op['cancel'].set()

        self.executor.shutdown(wait=True, cancel_futures=True)
        self.stream.stop()
        self.http.close()

        if self.lease is not None:
            os.close(self.lease)
            self.lease = None

    def _stage(self, op, stage, message, detail=''):
        """Updates the current operational stage and records timing."""
        now = time.monotonic()
        with self.lock:
            previous = op['stage']
            elapsed = round(now - op['stage_at'], 3)
            op['timings'][previous] = op['timings'].get(previous, 0) + elapsed

            op.update(stage=stage, message=message, detail=detail, stage_at=now)

        LOG.info('camera op=%s stage=%s elapsed=%.3fs %s%s',
                 op['id'], stage, now - op['started'], message, f' ({detail})' if detail else '')

    def status(self, operation_id=None):
        """Returns the current state and progress of a requested operation."""
        with self.lock:
            op = self.operations.get(operation_id or self.current)
            if not op:
                if operation_id:
                    raise KeyError(operation_id)
                return {'status': 'idle', 'stage': 'idle', 'message': 'Waiting to connect...', 'done': True,
                        'link': self.link, 'battery': self._battery_status()}

            result = {key: op[key] for key in ('id', 'kind', 'stage', 'message', 'done', 'result')}
            result['detail'] = op.get('detail', '')
            result['status'] = op['stage']
            result['timings'] = dict(op['timings'])
            result['link'] = self.link
            result['battery'] = self._battery_status()

            finish_time = op.get('finished') or time.monotonic()
            result['elapsed'] = round(finish_time - op['started'], 3)

            if op['done'] and op['stage'] == 'ready':
                error = self.stream.error()
                if error:
                    result.update(status='error', stage='error', message=f'Preview failed: {error}')

            return result

    def _submit(self, kind, action, client=''):
        """Queues a new camera operation if the manager is not currently busy."""
        with self.lock:
            if self.closed:
                raise CameraError('Camera controller is shutting down.')

            current = self.operations.get(self.current)
            if current and not current['done']:
                if kind == current['kind']:
                    return current
                if kind != 'disconnect':
                    raise CameraError(f"Camera is busy: {current['kind']}.")
                current['cancelled_by'] = client or 'an unnamed client'
                current['cancel'].set()
                LOG.warning('camera op=%s %s cancelled by a disconnect from %s',
                            current['id'], current['kind'], current['cancelled_by'])

            op_id = uuid.uuid4().hex
            op = {
                'id': op_id,
                'kind': kind,
                'stage': 'queued',
                'message': f'{kind.capitalize()} queued.',
                'done': False,
                'result': None,
                'cancel': threading.Event(),
                'client': client,
                'started': time.monotonic(),
                'stage_at': time.monotonic(),
                'timings': {}
            }

            self.current = op_id
            self.operations[op_id] = op

            # Prune old completed operations
            for key in list(self.operations)[:-32]:
                if self.operations[key]['done']:
                    del self.operations[key]

            LOG.info('camera op=%s %s requested by %s', op_id, kind, client or 'an unnamed client')
            op['future'] = self.executor.submit(self._execute, op, action)
            return op

    def _execute(self, op, action):
        """Executes an action within the standardized exception handling wrapper."""
        try:
            if STATION_PROBLEM and op['kind'] != 'disconnect':
                raise CameraError(NOT_READY, STATION_PROBLEM)
            result = action(op)
            with self.lock:
                op['result'] = result
        except Cancelled as exc:
            by = op.get('cancelled_by')
            self._stage(op, 'cancelled', f'Cancelled by a disconnect from {by}.' if by else str(exc))
            self._link_ends(op)
        except Exception as exc:
            LOG.exception('camera op=%s failed', op['id'])
            message, detail = str(exc), getattr(exc, 'detail', '')
            if op['kind'] == 'connect' and not detail:
                message, detail = NOT_READY, message
            self._stage(op, 'error', message, detail)
            self._link_ends(op)
        finally:
            with self.lock:
                op['finished'] = time.monotonic()
                op['done'] = True
        return self.status(op['id'])

    # --- PUBLIC API ---

    def connect(self, srt_ip='', srt_port=7003, client='', ensure=False):
        action = self._ensure if ensure else (lambda op: self._connect(op, srt_ip, srt_port))
        return self._submit('connect', action, client)['id']

    def disconnect(self, home_arm=None, client=''):
        return self._submit('disconnect', lambda op: self._disconnect(op, home_arm), client)['id']

    def capture(self, client=''):
        op = self._submit('capture', self._capture, client)
        result = op['future'].result()
        if result['stage'] in ('error', 'cancelled'):
            raise CameraError(result['message'])
        return result['result']

    def resume(self):
        return self._submit('resume', self._resume)['id']

    # --- LINK WATCH ---

    def _link_ends(self, op):
        """A connect that failed or was cancelled leaves no link to watch."""
        if op['kind'] == 'connect':
            self.link_expected, self.link, self.battery = False, '', None

    def _link_problem(self):
        """Why the connected camera link is unhealthy, or '' when it is fine."""
        iface = SYS_NET / INTERFACE
        if not iface.exists():
            return f'the camera Wi-Fi adapter {INTERFACE} is gone from the USB bus'
        state = (iface / 'operstate').read_text().strip()
        if state != 'up':
            return f'the camera Wi-Fi is {state}'
        if not self._is_api_reachable():
            return f'the camera at {CAMERA_IP} does not answer'
        return self.stream.error() or ''

    def _check_link(self):
        """Log the link once each time it goes bad, changes reason, or recovers."""
        problem = self._link_problem() if self.link_expected else ''
        if problem == self.link:
            return
        if problem:
            LOG.warning('camera link lost: %s', problem)
        elif self.link_expected:
            LOG.info('camera link back (it was: %s)', self.link)
        self.link = problem

    def _watch_link(self):
        while not self._watch_stop.wait(LINK_CHECK_S):
            try:
                self._check_link()
                self._check_battery()
            except Exception:
                LOG.exception('camera link check failed')

    # --- BATTERY ---

    def _check_battery(self):
        """Read the battery while the preview runs, at most every BATTERY_READ_S; forget it once the link is gone."""
        if not self.link_expected or self.link:
            self.battery, self._battery_tried = None, float('-inf')
            return
        now = time.monotonic()
        if not self.stream.live() or now - self._battery_tried < BATTERY_READ_S:
            return
        self._battery_tried = now
        percent = self._read_battery()
        if percent is not None:
            self.battery = (percent, now)

    @staticmethod
    def _read_battery():
        """The camera's battery in percent from its OSC state, or None when it did not say."""
        try:
            with requests.Session() as http:
                http.trust_env = False
                level = float(http.post(f'http://{CAMERA_IP}/osc/state', json={},
                                        timeout=GATE_PROBE_S).json()['state']['batteryLevel'])
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            LOG.info('camera battery not read: %s', exc)
            return None
        if not 0.0 <= level <= 1.0:
            LOG.info('camera battery not read: batteryLevel %r is outside 0..1', level)
            return None
        return round(level * 100)

    def _battery_status(self):
        """{'percent', 'age_s'} while the last reading is fresh, else None."""
        if self.battery is None:
            return None
        age = time.monotonic() - self.battery[1]
        return {'percent': self.battery[0], 'age_s': round(age, 1)} if age < BATTERY_FRESH_S else None

    # --- INTERNAL WORKFLOWS ---

    def _is_api_reachable(self, timeout=0.5) -> bool:
        """Determines if the camera is reachable via the routing table and API port."""
        try:
            with socket.create_connection((CAMERA_IP, 80), timeout=timeout):
                return True
        except OSError:
            return False

    @staticmethod
    def _nm_property(path, interface, name):
        out = subprocess.run(['busctl', 'get-property', 'org.freedesktop.NetworkManager', path,
                              f'org.freedesktop.NetworkManager.{interface}', name],
                             check=True, capture_output=True, text=True, timeout=5).stdout.split()
        return int(out[1])

    @staticmethod
    def _ident():
        """(SSID, pinned BSSID) of the camera's connection profile."""
        ident = subprocess.run(
            ['nmcli', '-e', 'no', '-g', '802-11-wireless.ssid,802-11-wireless.bssid',
             'connection', 'show', 'uuid', PROFILE],
            check=True, capture_output=True, text=True, timeout=5).stdout.split('\n')
        return (ident + ['', ''])[0].strip(), (ident + ['', ''])[1].strip().upper()

    def _heard(self, rescan):
        """(camera SSID, pinned BSSID, {BSSID: SSID} of its networks heard in the latest scan), or None."""
        asked = _boottime()
        try:
            ssid, bssid = self._ident()
            with _scan_lock() if rescan else contextlib.nullcontext():
                listed = subprocess.run(
                    ['nmcli', '-e', 'no', '-t', '-f', 'BSSID,SSID,DBUS-PATH', 'device', 'wifi', 'list',
                     'ifname', INTERFACE, '--rescan', 'yes' if rescan else 'no'],
                    check=True, capture_output=True, text=True, timeout=20).stdout.splitlines()
            device = subprocess.run(['nmcli', '-g', 'GENERAL.DBUS-PATH', 'device', 'show', INTERFACE],
                                    check=True, capture_output=True, text=True, timeout=5).stdout.strip()
            last_scan = self._nm_property(device, 'Device.Wireless', 'LastScan') / 1000
        except TimeoutError as exc:
            LOG.warning('camera scan skipped: %s', exc)
            return None
        except (subprocess.SubprocessError, OSError, ValueError, IndexError):
            return None
        if rescan and last_scan < asked:
            return None
        since = asked - 1 if rescan else last_scan - SCAN_SPAN_S
        heard = {}
        for line in listed:
            if len(line) <= 18 or ':' not in line[18:]:
                continue
            ap_bssid, (ap_ssid, path) = line[:17].upper(), line[18:].rsplit(':', 1)
            if ap_ssid != ssid and ap_bssid != bssid:
                continue
            try:
                if self._nm_property(path, 'AccessPoint', 'LastSeen') >= since:
                    heard[ap_bssid] = ap_ssid
            except (subprocess.SubprocessError, OSError, ValueError, IndexError):
                continue
        return ssid, bssid, heard

    def _camera_heard(self, rescan=True):
        """Whether the camera's own network was heard in the latest scan; None when nmcli cannot tell."""
        scan = self._heard(rescan)
        if scan is None or not scan[0]:
            return None
        ssid, bssid, heard = scan
        return bssid in heard if bssid else ssid in heard.values()

    def _why_not_associated(self, nm_error):
        """What the adapter's latest scan says about the camera's network, else the NetworkManager error."""
        scan = self._heard(rescan=False)
        if scan is None or not scan[0]:
            return nm_error
        ssid, bssid, heard = scan
        if bssid in heard if bssid else ssid in heard.values():
            return nm_error
        elsewhere = [b for b, s in heard.items() if s == ssid]
        if elsewhere:
            return (f"The camera's Wi-Fi ({ssid}) is on the air at {elsewhere[0]}, but its "
                    f"connection profile is pinned to {bssid}.")
        return (f"The camera's Wi-Fi ({ssid}) is not on the air. Turn the camera on and switch "
                f"its Wi-Fi on, then connect again.")

    # --- ADAPTER HELPER ---

    @staticmethod
    def _helper(command, root=False, timeout=10):
        """(True, stdout) when the camera helper's command succeeded, else (False, why not)."""
        args = (['sudo', '-n', HELPER] if root else [HELPER]) + [command]
        try:
            done = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return False, f'{HELPER} is not installed'
        except (subprocess.SubprocessError, OSError) as exc:
            return False, f'{command} did not finish: {exc}'
        if done.returncode:
            return False, done.stderr.strip() or done.stdout.strip() or f'{command} exited {done.returncode}'
        return True, done.stdout

    @staticmethod
    def _helper_interface():
        """The adapter named in the installed helper, or None when its file cannot be read."""
        try:
            found = re.search(r"^INTERFACE = '(.*)'$", Path(HELPER).read_text(), re.M)
        except OSError:
            return None
        return found.group(1) if found else None

    def _preflight(self, op, deadline):
        """Connect only: the camera's USB Wi-Fi adapter must be ready, after one reset through the helper if it was not."""
        self._stage(op, 'checking_adapter', 'Checking the camera Wi-Fi adapter...')
        installed = self._helper_interface()
        if installed is not None and installed != INTERFACE:
            raise CameraError(NOT_READY, f"The camera helper {HELPER} runs on {installed or 'no adapter'}, but "
                                         f"station.toml names {INTERFACE}. Reinstall it with: sudo bash "
                                         f"scripts/install-camera-helper.sh")
        ready, why = self._helper('check')
        if ready:
            return
        if not os.path.exists(HELPER):
            raise CameraError(NOT_READY, f"The camera helper {HELPER} is not installed, so the camera's USB Wi-Fi "
                                         f"adapter cannot be checked. Install it with: sudo bash "
                                         f"scripts/install-camera-helper.sh")
        LOG.warning('camera op=%s Wi-Fi adapter not ready (%s); resetting it', op['id'], why)
        self._stage(op, 'resetting_adapter', 'Resetting the camera Wi-Fi adapter...', why)
        reset, reset_why = self._helper('reset', root=True, timeout=deadline.remaining(RESET_TIMEOUT_S))
        ready, why = self._helper('check')
        if ready:
            LOG.info('camera op=%s Wi-Fi adapter ready after a reset', op['id'])
            return
        outcome = 'a reset did not bring it back' if reset else f'it could not be reset ({reset_why})'
        LOG.warning('camera op=%s Wi-Fi adapter still not ready (%s); %s', op['id'], why, outcome)
        raise CameraError(NOT_READY, f"The camera's USB Wi-Fi adapter is not working ({why}), and {outcome}. "
                                     f"Unplug the adapter and plug it back in, then connect again.")

    def _scan_channels(self, command):
        """{BSSID: (MHz, SSID)} heard by the helper's kernel scan ('scan' the camera's channels, 'sweep' every one), or None."""
        ran, out = self._helper(command, root=True, timeout=SCAN_TIMEOUT_S[command])
        if not ran and HELPER_BUSY in out:
            LOG.warning('camera helper scan could not get the adapter (%s); ending the connect for a retry', out)
            raise CameraError(BUSY_RETRY, f"The camera's USB Wi-Fi adapter {out}. NetworkManager scans it back to back "
                                          f"for about 3 minutes after a disconnect; connect again.")
        if not ran:
            if out != self.helper_scan_down:
                LOG.warning('camera helper scan unavailable (%s); using full NetworkManager scans', out)
            self.helper_scan_down = out
            return None
        if self.helper_scan_down:
            LOG.info('camera helper scan running again')
        self.helper_scan_down = ''
        heard = {}
        for line in out.splitlines():
            fields = line.split('\t')
            if len(fields) == 3:
                heard[fields[0].upper()] = (fields[1], fields[2])
        return heard

    def _look(self, command='scan'):
        """Whether the camera's network is on the air, by the helper's scan or sweep, else a NetworkManager scan; None when neither can tell."""
        heard = self._scan_channels(command)
        if heard is None:
            return self._camera_heard(rescan=True)
        try:
            ssid, bssid = self._ident()
        except (subprocess.SubprocessError, OSError):
            return None
        if not ssid:
            return None
        for ap_bssid, (mhz, ap_ssid) in heard.items():
            if ap_bssid == bssid if bssid else ap_ssid == ssid:
                LOG.info('camera Wi-Fi heard on %s MHz', mhz)
                return True
        return False

    # --- BLUETOOTH WAKE ---

    def _start_beacon(self):
        """Start advertising the wake beacon through BlueZ: (process, '') once registered, else (None, why not)."""
        data = ' '.join(f'0x{b:02x}' for b in WAKE_BEACON)
        try:
            proc = subprocess.Popen(['bluetoothctl'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
        except OSError as exc:
            return None, f'bluetoothctl could not start: {exc}'
        out = []
        threading.Thread(target=lambda: out.extend(proc.stdout), name='camera-beacon-out', daemon=True).start()
        try:
            proc.stdin.write(f'menu advertise\nmanufacturer 0x004c {data}\nback\nadvertise on\n')
            proc.stdin.flush()
        except OSError as exc:
            self._stop_beacon(proc)
            return None, f'bluetoothctl did not take the beacon: {exc}'
        end = time.monotonic() + 5
        while time.monotonic() < end:
            said = ''.join(out)
            if 'Advertising object registered' in said:
                return proc, ''
            if 'Failed to register advertisement' in said:
                break
            time.sleep(0.1)
        self._stop_beacon(proc)
        failed = [line.strip() for line in out if 'Failed' in line]
        return None, failed[-1] if failed else 'bluetoothd did not confirm the beacon'

    @staticmethod
    def _stop_beacon(proc):
        if proc is None:
            return
        try:
            proc.stdin.write('advertise off\nquit\n')
            proc.stdin.flush()
            proc.wait(timeout=5)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait(timeout=5)

    def _camera_advertising(self):
        """Whether the camera advertises over Bluetooth now; None when bluetoothctl cannot tell."""
        try:
            known = subprocess.run(['bluetoothctl', 'devices'], stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=5).stdout
            for line in known.splitlines():
                parts = line.split(' ', 2)
                if len(parts) == 3 and parts[2].strip() == CAMERA_BT_NAME:
                    subprocess.run(['bluetoothctl', 'remove', parts[1]], stdin=subprocess.DEVNULL,
                                   capture_output=True, timeout=5)
            scan = subprocess.run(['bluetoothctl', '--timeout', '8', 'scan', 'on'], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=15).stdout
        except (subprocess.SubprocessError, OSError):
            return None
        return CAMERA_BT_NAME in scan

    def _wait_heard(self, seconds, deadline):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            deadline.pause(WAKE_POLL_S)
            if self._look():
                return True
        return False

    def _wake(self, op, deadline):
        """Beacon until the camera's Wi-Fi is heard; otherwise say which hand action the camera needs."""
        self._stage(op, 'waking', 'Waking the camera; this can take up to two minutes...',
                    'The camera Wi-Fi is not on the air; sending the Bluetooth wake beacon.')
        beacon, why = self._start_beacon()
        if beacon is None:
            LOG.warning('camera op=%s wake beacon not sent: %s', op['id'], why)
            raise CameraError(NOT_READY, f"The camera's Wi-Fi is not on the air, and the Bluetooth wake could "
                                         f"not be sent ({why}). Press the camera's power button, switch its Wi-Fi "
                                         f"on, then connect again.")
        try:
            if self._wait_heard(WAKE_BEACON_S, deadline):
                LOG.info('camera op=%s Wi-Fi on the air during the Bluetooth wake', op['id'])
                return
        finally:
            self._stop_beacon(beacon)
        if self._wait_heard(WAKE_SETTLE_S, deadline):
            LOG.info('camera op=%s Wi-Fi on the air after the Bluetooth wake', op['id'])
            return
        awake = self._camera_advertising()
        LOG.warning('camera op=%s Bluetooth wake: Wi-Fi never appeared, camera advertising=%s', op['id'], awake)
        if awake is not False:
            deadline.remaining(1)
            self._stage(op, 'sweeping', 'Looking for the camera Wi-Fi on every channel...',
                        'The camera Wi-Fi was not heard on its usual channels; sweeping every channel once.')
            if self._look('sweep'):
                LOG.info('camera op=%s Wi-Fi heard in the full sweep', op['id'])
                return
        if awake:
            raise CameraError(NOT_READY, 'The camera woke over Bluetooth, but its Wi-Fi stayed off. Switch its '
                                         'Wi-Fi on at the camera, then connect again.')
        if awake is False:
            raise CameraError(NOT_READY, 'The camera did not wake over Bluetooth. Press its power button, switch '
                                         'its Wi-Fi on, then connect again.')
        raise CameraError(NOT_READY, "The camera's Wi-Fi did not come on after a Bluetooth wake. If the camera "
                                     "is on, switch its Wi-Fi on; if not, press its power button. Then connect again.")

    def _connect(self, op, srt_ip, srt_port):
        """Main connection sequence: Wi-Fi -> API -> RTMP Stream."""
        deadline = Deadline(200, op['cancel'])
        self.helper_scan_down = ''
        self._stage(op, 'checking', 'Checking existing camera connection...')

        if not self._is_api_reachable():
            self._preflight(op, deadline)
            self._stage(op, 'associating', 'Connecting to camera Wi-Fi...')
            if self._look() is False:
                self._wake(op, deadline)
                self._stage(op, 'associating', 'Connecting to the camera...',
                            'Camera Wi-Fi is on the air after the Bluetooth wake.')

            try:
                subprocess.run(
                    ['nmcli', '--wait', '25', 'connection', 'up', 'uuid', PROFILE, 'ifname', INTERFACE],
                    check=True, capture_output=True, text=True, timeout=35
                )
            except subprocess.TimeoutExpired:
                raise CameraError(self._why_not_associated('NetworkManager connection attempt timed out.'))
            except subprocess.CalledProcessError as exc:
                error_msg = exc.stderr.strip() or exc.stdout.strip()
                raise CameraError(self._why_not_associated(f'NetworkManager activation failed: {error_msg}'))

        self._stage(op, 'waiting_for_camera', 'Network linked; waiting for camera API...')
        api_timeout = min(deadline.end, time.monotonic() + 15)

        while not self._is_api_reachable():
            if time.monotonic() >= api_timeout:
                raise CameraError('Wi-Fi connected, but camera API is unreachable.')
            deadline.pause(0.5)

        self._stage(op, 'starting_preview', 'Starting preview; waiting for stream...')
        try:
            self.stream.start(srt_ip=srt_ip, srt_port=srt_port)
            self.stream.wait_ready(deadline)
            deadline.remaining(1)
        except Exception:
            self.stream.stop()
            raise

        self._stage(op, 'ready', 'Camera connected and preview ready.')
        self.link_expected = True

    def _ensure(self, op):
        """Before a capture: leave a camera that answers alone, and reconnect one that does not to the preview's own destination."""
        self._stage(op, 'checking', 'Checking the camera connection...')
        if self._is_api_reachable(timeout=GATE_PROBE_S):
            self._stage(op, 'linked', 'Camera connected.')
            return
        LOG.warning('camera op=%s the camera did not answer before a capture; reconnecting', op['id'])
        self._stage(op, 'reconnecting', 'The camera connection dropped. Reconnecting...')
        srt_ip, srt_port = self.stream.destination or ('', 7003)
        self._connect(op, srt_ip, srt_port)

    def _resume(self, op):
        """Restarts the RTMP stream assuming Wi-Fi is already active."""
        deadline = Deadline(20, op['cancel'])
        self._stage(op, 'starting_preview', 'Restarting camera preview...')

        if not self._is_api_reachable(timeout=GATE_PROBE_S):
            raise CameraError('Camera API unreachable. Please reconnect from the start.')

        srt_ip, srt_port = self.stream.destination or ('', 7003)
        try:
            self.stream.start(srt_ip=srt_ip, srt_port=srt_port)
            self.stream.wait_ready(deadline)
            deadline.remaining(1)
        except Exception:
            self.stream.stop()
            raise

        self._stage(op, 'ready', 'Preview ready.')
        self.link_expected = True
        return {'status': 'success'}

    def _disconnect(self, op, home_arm):
        """Tears down the stream and network connections safely."""
        self._stage(op, 'disconnecting', 'Stopping preview and disconnecting camera...')
        self.link_expected, self.link, self.battery = False, '', None

        self.stream.stop()
        warnings = []

        try:
            if not PROFILE:
                raise CameraError(STATION_PROBLEM)
            subprocess.run(
                ['nmcli', '--wait', '10', 'connection', 'down', 'uuid', PROFILE],
                check=True, capture_output=True, text=True, timeout=15
            )
        except subprocess.CalledProcessError as exc:
            if exc.returncode != NMCLI_NOT_ACTIVE:
                warnings.append(f'NetworkManager disconnect: {exc.stderr.strip() or exc}')
        except Exception as exc:
            warnings.append(f'NetworkManager disconnect: {exc}')

        if home_arm:
            try:
                home_arm()
            except Exception as exc:
                warnings.append(f'Arm: {exc}')

        msg = f"Camera disconnected by {op['client']}." if op.get('client') else 'Camera disconnected.'
        if warnings:
            msg += f" {'; '.join(warnings)}"

        self._stage(op, 'idle', msg)
        return {'warnings': warnings}

    def _capture(self, op):
        """Triggers the camera shutter and downloads the resulting file."""
        deadline = Deadline(40, op['cancel'])
        self._stage(op, 'capturing', 'Capturing image...')

        if not self._is_api_reachable(timeout=GATE_PROBE_S):
            raise CameraError('Camera is unreachable. Connect before capturing.')

        self.stream.stop()
        try:
            self._take_picture(deadline)
        finally:
            self.http.close()
        self._stage(op, 'captured', 'Photo captured.')
        return {'status': 'success', 'lan_path': f'/static/latest.jpg?t={time.time_ns()}'}

    def _take_picture(self, deadline):
        """Shutter, completion poll and download; the caller closes their keep-alive connection."""
        def osc_post(endpoint, payload):
            response = self.http.post(
                f'http://{CAMERA_IP}/osc/commands/{endpoint}',
                json=payload,
                timeout=deadline.remaining(4)
            )
            if not response.ok:
                said = response.text.strip()[:300] or 'no reason given'
                raise CameraError(f"The camera refused {payload.get('name', endpoint)} "
                                  f"(HTTP {response.status_code}): {said}")
            return response.json()

        # Trigger Shutter
        result = osc_post('execute', {'name': 'camera.takePicture'})

        # Poll for completion
        while result.get('state') != 'done':
            if result.get('state') == 'error':
                raise CameraError(f'Camera rejected capture: {result.get("error")}')

            command_id = result.get('id')
            if command_id is None:
                raise CameraError('Camera did not return a capture command ID.')

            deadline.pause(0.4)
            result = osc_post('status', {'id': command_id})
            result.setdefault('id', command_id)

        url = result.get('results', {}).get('fileUrl', '')
        parsed = urlparse(url)
        if parsed.scheme != 'http' or parsed.hostname != CAMERA_IP:
            raise CameraError('Camera returned an unexpected image URL.')

        # Download High-Res Image
        self.image_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.image_path.with_suffix('.tmp')

        try:
            with self.http.get(url, timeout=deadline.remaining(4), stream=True, allow_redirects=False) as response:
                response.raise_for_status()
                if response.status_code != 200:
                    raise CameraError('Camera image download failed.')

                with temporary.open('wb') as output:
                    for chunk in response.iter_content(65536):
                        deadline.remaining(1)
                        output.write(chunk)

            temporary.replace(self.image_path)
        finally:
            temporary.unlink(missing_ok=True)
