"""The camera adapter helper: check as any user; reset, scan and boot as root, on the camera's own adapter only."""

import ast
import fcntl
import io
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

APP = Path(__file__).resolve().parents[1]
HELPER = APP / 'scripts/selfie-camera-control'
IFACE = 'wlxtest0'
SUBNET = '192.0.2.0/24'

IW_OUTPUT = f"""BSS 02:00:00:00:00:01(on {IFACE})
\tlast seen: 95126.201s [boottime]
\tTSF: 10948815 usec (0d, 00:00:10)
\tfreq: 5745.0
\tbeacon interval: 100 TUs
\tsignal: -41.00 dBm
\tlast seen: 40 ms ago
\tInformation elements from Probe Response frame:
\tSSID: X5 TEST01.OSC
\tSupported rates: 6.0* 9.0 12.0* 18.0 24.0* 36.0 48.0 54.0
BSS 02:00:00:00:00:03(on {IFACE})
\tfreq: 5805.0
\tsignal: -52.00 dBm
\tlast seen: 250000 ms ago
\tSSID: level5_
"""


class FakeTime:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        self.now += 0.01
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def fake_sys(root, present=True, authorized='1', bound=True, iface=True):
    """A /sys tree holding the adapter at 1-6 beside another USB device."""
    devices = root / 'bus/usb/devices'
    hub = devices / 'usb1'
    hub.mkdir(parents=True)
    (hub / 'idVendor').write_text('1d6b\n')
    (hub / 'idProduct').write_text('0002\n')
    (root / 'drivers/mt76x2u').mkdir(parents=True)
    (root / 'module/mt76x2u').mkdir(parents=True)
    (root / 'class/net').mkdir(parents=True)
    if not present:
        return None
    dev = devices / '1-6'
    (dev / '1-6:1.0').mkdir(parents=True)
    for name, value in (('idVendor', '0e8d'), ('idProduct', '7612'), ('authorized', authorized),
                        ('busnum', '1'), ('devnum', '9')):
        (dev / name).write_text(value + '\n')
    if bound:
        (dev / '1-6:1.0/driver').symlink_to(root / 'drivers/mt76x2u')
    if iface:
        make_iface(root)
    return dev


def make_iface(root):
    (root / f'bus/usb/devices/1-6/1-6:1.0/net/{IFACE}').mkdir(parents=True, exist_ok=True)
    (root / f'class/net/{IFACE}').mkdir(parents=True, exist_ok=True)


class HelperCase(unittest.TestCase):
    def setUp(self):
        self.helper = runpy.run_path(str(HELPER))
        self.g = self.helper['main'].__globals__
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.sys = self.root / 'sys'
        self.calls = []
        self.nm_state = '30 (disconnected)'
        self.time = FakeTime()
        p = patch.dict(self.g, {'SYS': self.sys, 'SCAN_LOCK': self.root / 'scan.lock', 'time': self.time,
                                'INTERFACE': IFACE, 'CAMERA_SUBNET': SUBNET})
        p.start()
        self.addCleanup(p.stop)

    def fake_run(self, answers=None):
        """subprocess.run as the helper sees it; answers maps a command's first word to (code, stdout, stderr)."""
        answers = answers or {}

        def run(args, **kwargs):
            self.calls.append(list(args))
            if args[:2] == ['nmcli', '-g']:
                return subprocess.CompletedProcess(args, 0, self.nm_state + '\n', '')
            answer = answers.get(args[0], (0, '', ''))
            if callable(answer):
                answer = answer(args)
            if args[0] == 'iw' and answer[0] == 0:
                self.time.now += 5
            return subprocess.CompletedProcess(args, *answer)
        return patch('subprocess.run', side_effect=run)

    def main(self, command, euid=0):
        out = io.StringIO()
        with patch('os.geteuid', return_value=euid), patch('sys.argv', ['helper', command]), \
                patch('sys.stdout', out):
            code = self.helper['main']()
        return code, out.getvalue()


class CheckTests(HelperCase):
    def problem(self):
        with self.fake_run():
            return self.helper['problem']()

    def test_check_names_each_layer_that_is_not_ready(self):
        fake_sys(self.sys, present=False)
        self.assertIn('not on the USB bus', self.problem())

    def test_an_unauthorized_adapter_is_named(self):
        fake_sys(self.sys, authorized='0')
        self.assertEqual(self.problem(), 'USB device 1-6 is not authorized')

    def test_an_adapter_without_its_driver_is_named(self):
        fake_sys(self.sys, bound=False)
        self.assertEqual(self.problem(), 'USB device 1-6 is not bound to mt76x2u')

    def test_a_driver_that_made_no_interface_is_named(self):
        fake_sys(self.sys, iface=False)
        self.assertEqual(self.problem(), f'mt76x2u did not create {IFACE}')

    def test_an_interface_networkmanager_cannot_use_is_named(self):
        fake_sys(self.sys)
        for state in ('10 (unmanaged)', '20 (unavailable)'):
            self.nm_state = state
            self.assertEqual(self.problem(), f'NetworkManager has {IFACE} {state}')

    def test_check_needs_no_root_and_prints_ready(self):
        fake_sys(self.sys)
        with self.fake_run():
            self.assertEqual(self.main('check', euid=1000), (0, 'ready\n'))

    def test_check_exits_1_with_the_reason(self):
        fake_sys(self.sys, iface=False)
        with self.fake_run():
            self.assertEqual(self.main('check', euid=1000), (1, f'mt76x2u did not create {IFACE}\n'))


class RootCommandTests(HelperCase):
    def test_root_commands_refuse_without_root_and_run_nothing(self):
        for command in ('reset', 'scan', 'sweep', 'isolate', 'boot'):
            with patch('subprocess.run') as run, self.assertRaisesRegex(RuntimeError, 'needs root'):
                self.main(command, euid=1000)
            run.assert_not_called()

    def test_an_unrecognized_command_runs_nothing(self):
        for command in ('scan-limit', 'wake', 'arbitrary'):
            with patch('subprocess.run') as run, self.assertRaises(RuntimeError):
                self.main(command)
            run.assert_not_called()


class ScanTests(HelperCase):
    def scan(self, iw):
        with self.fake_run({'wpa_cli': (0, 'OK\n', ''), 'iw': iw}):
            return self.main('scan')

    def test_scan_clears_the_restriction_then_scans_the_camera_channels_on_the_camera_adapter(self):
        self.scan((0, IW_OUTPUT, ''))
        self.assertEqual(self.calls[0], ['wpa_cli', '-i', IFACE, 'set', 'freq_list', ''])
        self.assertEqual(self.calls[1], ['iw', 'dev', IFACE, 'scan', 'freq', '5180', '5200', '5220', '5240',
                                         '5745', '5765', '5785', '5805', '5825'])

    def test_sweep_scans_every_channel_on_the_camera_adapter(self):
        with self.fake_run({'wpa_cli': (0, 'OK\n', ''), 'iw': (0, IW_OUTPUT, '')}):
            self.assertEqual(self.main('sweep'), (0, '02:00:00:00:00:01\t5745\tX5 TEST01.OSC\n'))
        self.assertEqual(self.calls[:2], [['wpa_cli', '-i', IFACE, 'set', 'freq_list', ''], ['iw', 'dev', IFACE, 'scan']])

    def test_scan_prints_only_networks_heard_during_this_scan(self):
        self.assertEqual(self.scan((0, IW_OUTPUT, '')), (0, '02:00:00:00:00:01\t5745\tX5 TEST01.OSC\n'))

    def test_scan_waits_out_another_scan_on_the_adapter(self):
        tries = iter([(240, '', 'command failed: Device or resource busy (-16)')] * 2 + [(0, IW_OUTPUT, '')])
        code, out = self.scan(lambda args: next(tries))
        self.assertEqual(code, 0)
        self.assertEqual(sum(1 for c in self.calls if c[0] == 'iw'), 3)
        self.assertIn('X5 TEST01.OSC', out)

    def test_scan_waits_out_a_full_networkmanager_sweep(self):
        busy_until = self.time.now + 20

        def iw(args):
            if self.time.now < busy_until:
                return 240, '', 'command failed: Device or resource busy (-16)'
            return 0, IW_OUTPUT, ''
        code, out = self.scan(iw)
        self.assertEqual(code, 0)
        self.assertIn('X5 TEST01.OSC', out)

    def test_scan_gives_up_after_30_s_of_busy(self):
        with self.assertRaisesRegex(RuntimeError, 'stayed busy with another scan for 30 s'):
            self.scan((240, '', 'command failed: Device or resource busy (-16)'))

    def test_scan_stops_at_any_other_iw_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'Network is down'):
            self.scan((240, '', 'command failed: Network is down (-100)'))
        self.assertEqual(sum(1 for c in self.calls if c[0] == 'iw'), 1)

    def test_a_restriction_that_will_not_clear_is_reported_and_the_scan_still_runs(self):
        err = io.StringIO()
        with self.fake_run({'wpa_cli': (0, 'FAIL\n', ''), 'iw': (0, IW_OUTPUT, '')}), patch('sys.stderr', err):
            code, out = self.main('scan')
        self.assertIn('did not clear the scan restriction: FAIL', err.getvalue())
        self.assertIn('X5 TEST01.OSC', out)

    def test_scan_and_sweep_hold_the_shared_lock_and_wait_for_another_holder(self):
        fd = os.open(self.g['SCAN_LOCK'], os.O_RDONLY | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        for command in ('scan', 'sweep'):
            with self.fake_run({'wpa_cli': (0, 'OK\n', ''), 'iw': (0, IW_OUTPUT, '')}), \
                    self.assertRaisesRegex(RuntimeError, 'another scan held'):
                self.main(command)
            self.assertEqual(self.calls, [], command)


class ResetTests(HelperCase):
    def test_reset_reauthorizes_reloads_the_driver_and_hands_the_adapter_back(self):
        dev = fake_sys(self.sys, authorized='0', iface=False)

        def modprobe(args):
            if args[1:] == ['mt76x2u']:
                make_iface(self.sys)
            return 0, '', ''
        port_reset = Mock()
        with self.fake_run({'modprobe': modprobe}), patch.dict(self.g, {'port_reset': port_reset}):
            self.assertEqual(self.main('reset'), (0, 'ready\n'))
        self.assertEqual((dev / 'authorized').read_text(), '1')
        self.assertEqual([c for c in self.calls if c[0] == 'modprobe'],
                         [['modprobe', '-r', 'mt76x2u'], ['modprobe', 'mt76x2u']])
        self.assertIn(['nmcli', 'device', 'set', IFACE, 'managed', 'yes'], self.calls)
        port_reset.assert_not_called()

    def test_reset_port_resets_only_when_the_reload_brings_no_interface(self):
        dev = fake_sys(self.sys, iface=False)
        port_reset = Mock(side_effect=lambda d: make_iface(self.sys))
        with self.fake_run(), patch.dict(self.g, {'port_reset': port_reset}):
            self.assertEqual(self.main('reset'), (0, 'ready\n'))
        port_reset.assert_called_once_with(dev)

    def test_reset_refuses_an_adapter_that_is_off_the_bus_and_touches_nothing(self):
        fake_sys(self.sys, present=False)
        with self.fake_run(), self.assertRaisesRegex(RuntimeError, 'not on the USB bus'):
            self.main('reset')
        self.assertEqual(self.calls, [])

    def test_a_reset_that_leaves_the_adapter_broken_says_so(self):
        fake_sys(self.sys, iface=False)
        with self.fake_run(), patch.dict(self.g, {'port_reset': Mock()}), \
                self.assertRaisesRegex(RuntimeError, f'still not ready after a reset: mt76x2u did not create {IFACE}'):
            self.main('reset')


class BootTests(HelperCase):
    def boot(self, problems):
        reset = Mock()
        answers = iter(problems)
        with patch.dict(self.g, {'reset': reset, 'wait_ready': lambda s: next(answers),
                                 'problem': lambda: next(answers)}):
            return self.main('boot'), reset

    def test_a_ready_adapter_at_boot_is_left_alone(self):
        (code, out), reset = self.boot([''])
        self.assertEqual((code, out.splitlines()[-1]), (0, 'adapter ready'))
        reset.assert_not_called()

    def test_a_broken_adapter_at_boot_is_reset_until_ready(self):
        (code, out), reset = self.boot(['no interface', 'no interface', ''])
        self.assertEqual(code, 0)
        self.assertEqual(reset.call_count, 2)

    def test_boot_gives_up_after_two_resets_and_asks_for_a_replug(self):
        with self.assertRaisesRegex(RuntimeError, 'still not ready after two resets: no interface; unplug'):
            self.boot(['no interface'] * 3)


class IsolateTests(HelperCase):
    def test_isolate_makes_the_camera_subnet_unreachable_and_touches_nothing_else(self):
        with self.fake_run():
            code, out = self.main('isolate')
        self.assertEqual(self.calls, [['ip', 'route', 'replace', 'unreachable', '192.0.2.0/24', 'metric', '4000']])
        self.assertEqual((code, out), (0, f'192.0.2.0/24 is unreachable except through {IFACE}\n'))

    def test_boot_isolates_before_it_waits_for_the_adapter_and_carries_on_when_that_fails(self):
        seen = []
        with patch.dict(self.g, {'wait_ready': lambda s: seen.append(len(self.calls)) or '', 'problem': lambda: ''}), \
                self.fake_run({'ip': (2, '', 'RTNETLINK answers: Operation not permitted')}):
            code, out = self.main('boot')
        self.assertEqual((code, seen), (0, [1]))
        self.assertEqual(self.calls[0][:4], ['ip', 'route', 'replace', 'unreachable'])
        self.assertIn('could not isolate 192.0.2.0/24: RTNETLINK answers: Operation not permitted', out)
        self.assertEqual(out.splitlines()[-1], 'adapter ready')


class InstallTests(unittest.TestCase):
    def test_the_install_isolates_the_camera_subnet_and_keeps_the_camera_profile_to_the_camera(self):
        path = APP / 'scripts/install-camera-helper.sh'
        script = path.read_text()
        self.assertIn('\n/usr/local/libexec/selfie-camera-control isolate\n', script)
        self.assertIn('ipv4.never-default yes ipv4.ignore-auto-dns yes ipv4.ignore-auto-routes yes', script)
        subprocess.run(['bash', '-n', str(path)], check=True)

    def test_the_install_puts_the_configured_copy_in_place_never_the_blank_source(self):
        lines = (APP / 'scripts/install-camera-helper.sh').read_text().splitlines()
        self.assertIn('/usr/bin/python3 -I "$source_dir/selfie-camera-control" configure "$station_file" > "$helper_file"',
                      lines)
        installs = [line for line in lines if line.startswith('install ') and 'libexec/selfie-camera-control' in line]
        self.assertEqual(installs, ['install -o root -g root -m 0755 "$helper_file" /usr/local/libexec/selfie-camera-control'])
        self.assertIn('station_file=$(dirname -- "$source_dir")/station.toml', lines)


class BlankCopyTests(unittest.TestCase):
    def test_the_repo_copy_names_no_adapter_and_refuses_every_command(self):
        helper = runpy.run_path(str(HELPER))
        for command in ('check', 'reset', 'scan', 'sweep', 'isolate', 'boot'):
            with patch('os.geteuid', return_value=0), patch('sys.argv', ['helper', command]), \
                    patch('subprocess.run') as run, self.assertRaisesRegex(RuntimeError, 'names no adapter'):
                helper['main']()
            run.assert_not_called()


class ConfigureTests(HelperCase):
    def station(self, iface=IFACE, ip='192.0.2.1'):
        path = self.root / 'station.toml'
        path.write_text(f'[camera]\nwifi_interface = "{iface}"\nip = "{ip}"\n')
        return path

    def test_configure_writes_the_adapter_and_subnet_into_a_copy_and_changes_nothing_else(self):
        fake_sys(self.sys)
        copy = self.helper['configure'](self.station())
        self.assertEqual(copy.replace(f"INTERFACE = {IFACE!r}", "INTERFACE = ''")
                         .replace(f"CAMERA_SUBNET = {SUBNET!r}", "CAMERA_SUBNET = ''"), HELPER.read_text())
        path = self.root / 'configured'
        path.write_text(copy)
        configured = runpy.run_path(str(path))
        self.assertEqual((configured['INTERFACE'], configured['CAMERA_SUBNET']), (IFACE, SUBNET))

    def test_the_install_reads_the_adapter_back_from_the_configured_copy(self):
        fake_sys(self.sys)
        path = self.root / 'configured'
        path.write_text(self.helper['configure'](self.station()))
        lookup = next(line for line in (APP / 'scripts/install-camera-helper.sh').read_text().splitlines()
                      if line.startswith('iface=$(sed'))
        self.assertTrue(lookup.endswith(' "$helper_file")'), lookup)
        expression = lookup.split('sed -n ', 1)[1].split(' "$helper_file', 1)[0].strip('"')
        found = subprocess.run(['sed', '-n', expression, str(path)], capture_output=True, text=True, check=True)
        self.assertEqual(found.stdout.strip(), IFACE)

    def test_configure_refuses_any_interface_the_adapter_did_not_make(self):
        fake_sys(self.sys)
        (self.sys / 'class/net/wlp5s0').mkdir(parents=True)
        for iface in ('wlp5s0', '', '..', f'{IFACE}/..'):
            with self.assertRaisesRegex(RuntimeError, "is not the camera adapter's"):
                self.helper['configure'](self.station(iface=iface))

    def test_configure_refuses_while_the_adapter_is_unplugged(self):
        fake_sys(self.sys, present=False)
        with self.assertRaisesRegex(RuntimeError, 'made nothing'):
            self.helper['configure'](self.station())

    def test_configure_refuses_a_camera_address_that_is_not_ipv4(self):
        fake_sys(self.sys)
        for ip in ('', 'camera.local', '192.0.2'):
            with self.assertRaisesRegex(RuntimeError, 'is not an IPv4 address'):
                self.helper['configure'](self.station(ip=ip))

    def test_the_sudo_rule_allows_only_reset_scan_and_sweep(self):
        rule = next(line for line in (APP / 'scripts/install-camera-helper.sh').read_text().splitlines()
                    if 'NOPASSWD' in line)
        granted = rule.split('NOPASSWD: ', 1)[1].split('\\n', 1)[0]
        self.assertEqual(granted, '/usr/local/libexec/selfie-camera-control reset, '
                                  '/usr/local/libexec/selfie-camera-control scan, '
                                  '/usr/local/libexec/selfie-camera-control sweep')

    def test_the_boot_check_never_holds_up_boot(self):
        unit = (APP / 'scripts/selfie-camera-adapter.service').read_text()
        self.assertIn('Type=exec\n', unit)
        self.assertIn('ExecStart=/usr/local/libexec/selfie-camera-control boot\n', unit)
        self.assertNotIn('Before=', unit)

    def test_the_app_and_the_helper_share_one_scan_lock(self):
        def lock(path):
            tree = ast.parse(path.read_text())
            node = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                        and getattr(n.targets[0], 'id', '') == 'SCAN_LOCK')
            return ast.literal_eval(node.value.args[0])
        self.assertEqual(lock(HELPER), lock(APP / 'camera_connection.py'))


class GuardTests(unittest.TestCase):
    """Structural guards: the helper can only lift a scan restriction, and only on the camera's adapter."""

    def setUp(self):
        self.tree = ast.parse(HELPER.read_text())

    def argv_lists(self, first):
        return [n for n in ast.walk(self.tree) if isinstance(n, ast.List) and n.elts
                and isinstance(n.elts[0], ast.Constant) and n.elts[0].value == first]

    def test_the_only_freq_list_the_helper_can_set_is_empty(self):
        writes = [n for n in ast.walk(self.tree) if isinstance(n, ast.List)
                  and any(isinstance(e, ast.Constant) and e.value == 'freq_list' for e in n.elts)]
        self.assertTrue(writes)
        for node in writes:
            values = [e.value for e in node.elts if isinstance(e, ast.Constant)]
            self.assertEqual(values[values.index('freq_list') + 1], '')

    def test_the_only_route_the_helper_writes_is_the_unreachable_camera_subnet(self):
        lists = self.argv_lists('ip')
        self.assertEqual([[getattr(e, 'value', getattr(e, 'id', None)) for e in n.elts] for n in lists],
                         [['ip', 'route', 'replace', 'unreachable', 'CAMERA_SUBNET', 'metric', 'ISOLATION_METRIC']])
        constants = {n.targets[0].id: ast.literal_eval(n.value) for n in self.tree.body
                     if isinstance(n, ast.Assign)
                     and getattr(n.targets[0], 'id', '') in ('INTERFACE', 'CAMERA_SUBNET', 'ISOLATION_METRIC')}
        self.assertEqual((constants['INTERFACE'], constants['CAMERA_SUBNET']), ('', ''),
                         'the installer writes the adapter and subnet into its copy')
        self.assertGreater(int(constants['ISOLATION_METRIC']), 600, "above NetworkManager's metric for a Wi-Fi link")

    def test_every_radio_command_names_the_camera_adapter(self):
        for first in ('iw', 'wpa_cli', 'nmcli'):
            lists = self.argv_lists(first)
            self.assertTrue(lists, first)
            for node in lists:
                self.assertTrue(any(isinstance(e, ast.Name) and e.id == 'INTERFACE' for e in node.elts),
                                ast.unparse(node))
        self.assertNotIn('wlp5s0', HELPER.read_text())


class SandboxTests(unittest.TestCase):
    def test_every_test_module_that_loads_the_app_imports_the_sandbox(self):
        for path in sorted((APP / 'tests').glob('test_*.py')):
            tree = ast.parse(path.read_text())
            loads = any(isinstance(n, ast.Import) and any(a.name == 'camera_connection' for a in n.names)
                        or isinstance(n, ast.ImportFrom) and n.module == 'camera_connection'
                        for n in ast.walk(tree))
            sandboxed = any(isinstance(n, ast.ImportFrom) and n.module == 'sandbox'
                            and {'setUpModule', 'tearDownModule'} <= {a.name for a in n.names}
                            for n in tree.body)
            self.assertTrue(sandboxed or not loads, path.name)


if __name__ == '__main__':
    unittest.main()
