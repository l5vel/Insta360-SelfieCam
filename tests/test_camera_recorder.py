"""The passive camera-link recorder and its timeline: parsers, clocks, filters, and that it only reads."""

import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import camera_recorder as rec  # noqa: E402
import camera_timeline as tl  # noqa: E402

IFACE = 'wlxtest0'
_patches = [patch.object(rec, 'CAM_IP', '192.0.2.1')]


def setUpModule():
    for p in _patches:
        p.start()


def tearDownModule():
    for p in _patches:
        p.stop()


DEV = '/org/freedesktop/NetworkManager/Devices/8'
R, M, B = 1_790_700_000_000_000_000, 3_000_000_000_000, 3_000_500_000_000
CAM_SSID_BYTES = ', '.join(f'0x{b:02x}' for b in b'X5 TEST01.OSC')


def stamp(wall_ns, iso=False):
    """A local-time stamp as iw (space) or ip -ts (T) prints it."""
    sep = 'T' if iso else ' '
    return time.strftime(f'%Y-%m-%d{sep}%H:%M:%S', time.localtime(wall_ns // 10**9)) + f'.{wall_ns // 1000 % 10**6:06d}'


def record(src, boot=B, **fields):
    return {'rt': R + (boot - B), 'mono': M + (boot - B), 'boot': boot, 'src': src, **fields}


class ListSink:
    def __init__(self):
        self.rows = []

    def write(self, src, /, **fields):
        self.rows.append((src, fields))


def genl(seq, kind, payload=b'', flags=2):
    body = struct.pack('=BBH', 0, 0, 0) + payload
    return struct.pack('=IHHII', 16 + len(body), kind, flags, seq, 0) + body


def done(seq, err=0, kind=rec.NLMSG_DONE):
    return struct.pack('=IHHII', 20, kind, 2, seq, 0) + struct.pack('=i', err)


def diag_msg(dst, dport=80, state=1, lastsnd=20000, lastrcv=19000, retransmits=0, total=0, inode=4242):
    """An inet_diag_msg for 192.0.2.4:53732 -> dst:dport with an INET_DIAG_INFO tcp_info."""
    head = bytearray(72)
    head[0], head[1] = socket.AF_INET, state
    struct.pack_into('!HH', head, 4, 53732, dport)
    head[8:12], head[24:28] = socket.inet_aton('192.0.2.4'), socket.inet_aton(dst)
    struct.pack_into('=I', head, 68, inode)
    info = bytearray(104)
    info[2] = retransmits
    struct.pack_into('=IIII', info, 44, lastsnd, 0, lastrcv, lastrcv)
    struct.pack_into('=I', info, 68, 9671)
    struct.pack_into('=I', info, 100, total)
    return bytes(head) + rec.nla(rec.INET_DIAG_INFO, bytes(info))


class FakeNetlink:
    """Answers the family lookup and every dump with an empty result; remembers each socket's protocol and requests."""

    def __init__(self, *args):
        self.protocol = args[2] if len(args) > 2 else None
        self.sent, self.replies = [], []

    def settimeout(self, t):
        pass

    def bind(self, addr):
        pass

    def send(self, data):
        _, kind, flags, seq, _ = struct.unpack_from('=IHHII', data)
        self.sent.append((kind, data[16], flags))
        if kind == rec.GENL_ID_CTRL:
            self.replies.append(genl(seq, rec.GENL_ID_CTRL, rec.nla(rec.CTRL_ATTR_FAMILY_ID, struct.pack('=H', 0x1c))))
        else:
            self.replies.append(done(seq))

    def recv(self, n):
        return self.replies.pop(0)

    def close(self):
        pass


class Nl80211Parsing(unittest.TestCase):
    def test_station_fields_decode_with_signed_signal_and_nested_rates(self):
        info = (rec.nla(7, struct.pack('=b', -45)) + rec.nla(29, struct.pack('=Q', 5357))
                + rec.nla(18, struct.pack('=I', 2)) + rec.nla(42, struct.pack('=Q', 123456789))
                + rec.nla(8 | 0x8000, rec.nla(5, struct.pack('=I', 8667))))
        attrs = rec.parse_attrs(rec.nla(6, bytes.fromhex('020000000001')) + rec.nla(21 | 0x8000, info))
        self.assertEqual(rec.station_entry(attrs), {'bssid': '02:00:00:00:00:01', 'signal_dbm': -45, 'beacon_rx': 5357,
                                                    'beacon_loss': 2, 'assoc_at_boot_ns': 123456789, 'tx_mbps': 866.7})

    def test_bss_fields_carry_ssid_both_tsfs_and_last_seen(self):
        ies = bytes([0, 13]) + b'X5 TEST01.OSC' + bytes([1, 1, 0x8c])
        bss = (rec.nla(1, bytes.fromhex('020000000001')) + rec.nla(2, struct.pack('=I', 5240))
               + rec.nla(3, struct.pack('=Q', 1000)) + rec.nla(13, struct.pack('=Q', 2000))
               + rec.nla(6, ies) + rec.nla(14, b'') + rec.nla(9, struct.pack('=I', 1))
               + rec.nla(15, struct.pack('=Q', 777)) + rec.nla(7, struct.pack('=i', -4100)))
        e = rec.bss_entry(rec.parse_attrs(rec.nla(47, bss)))
        self.assertEqual((e['ssid'], e['freq'], e['tsf_us'], e['beacon_tsf_us'], e['presp'], e['status'],
                          e['last_seen_boot_ns'], e['signal_mbm']), ('X5 TEST01.OSC', 5240, 1000, 2000, True, 1, 777, -4100))

    def test_a_dump_reads_every_message_of_its_own_sequence_until_done(self):
        nl = object.__new__(rec.Nl80211)
        nl.seq, nl.family = 0, 0x1c
        sta = rec.nla(6, bytes(6)) + rec.nla(21, rec.nla(7, struct.pack('=b', -50)))
        sock = FakeNetlink()
        sock.send = lambda data: sock.sent.append(data)
        sock.replies = [genl(1, 0x1c, sta) + genl(0, 0x1c, sta) + genl(1, 0x1c, sta), done(1)]
        nl.sock = sock
        self.assertEqual(len(nl.stations(7)), 2)
        size, kind, flags, seq, _ = struct.unpack_from('=IHHII', sock.sent[0])
        self.assertEqual((kind, flags, seq, sock.sent[0][16]), (0x1c, rec.NLM_F_REQUEST | rec.NLM_F_DUMP, 1, rec.CMD_GET_STATION))
        self.assertEqual(rec.parse_attrs(sock.sent[0][20:])[rec.ATTR_IFINDEX], struct.pack('=I', 7))

    def test_a_kernel_error_is_raised_not_read_as_empty(self):
        nl = object.__new__(rec.Nl80211)
        nl.seq, nl.family = 0, 0x1c
        sock = FakeNetlink()
        sock.send = lambda data: None
        sock.replies = [done(1, err=-19, kind=rec.NLMSG_ERROR)]
        nl.sock = sock
        with self.assertRaises(OSError) as caught:
            nl.scan_cache(7)
        self.assertEqual(caught.exception.errno, 19)


class SockDiagReads(unittest.TestCase):
    def test_tcp_info_decodes_the_idle_times_and_retransmits(self):
        s = rec.tcp_entry(diag_msg('192.0.2.1', state=2, lastsnd=313978, lastrcv=313426, retransmits=1, total=4))
        self.assertEqual(s, {'local': '192.0.2.4:53732', 'peer': '192.0.2.1:80', 'state': 'SYN-SENT', 'inode': 4242,
                             'lastsnd_ms': 313978, 'lastrcv_ms': 313426, 'lastack_ms': 313426, 'unacked': 0,
                             'rtt_ms': 9.671, 'retrans': '1/4'})

    def test_a_dump_keeps_only_sockets_to_the_camera_and_asks_for_tcp_info(self):
        d = object.__new__(rec.SockDiag)
        d.seq, sent = 0, []
        d.sock = FakeNetlink()
        d.sock.send = sent.append
        d.sock.replies = [struct.pack('=IHHII', 16 + 72 + 108, 20, 2, 1, 0) + diag_msg('192.0.2.1')
                          + struct.pack('=IHHII', 16 + 72 + 108, 20, 2, 1, 0) + diag_msg('198.51.100.9'), done(1)]
        got = d.tcp_to('192.0.2.1')
        self.assertEqual([s['peer'] for s in got], ['192.0.2.1:80'])
        _, kind, flags, _, _ = struct.unpack_from('=IHHII', sent[0])
        family, proto, ext, _, states = struct.unpack_from('=BBBBI', sent[0], 16)
        self.assertEqual((kind, flags, family, proto, ext, states),
                         (20, rec.NLM_F_REQUEST | rec.NLM_F_DUMP, socket.AF_INET, socket.IPPROTO_TCP, 2, 0xFFFFFFFF))


class Redaction(unittest.TestCase):
    def test_secrets_in_every_spelling_are_redacted(self):
        for text, secret in [('nmcli con modify cam wifi-sec.psk Hunter22', 'Hunter22'),
                             ('802-11-wireless-security.psk:Hunter22', 'Hunter22'),
                             ('nmcli dev wifi connect X5 password Hunter22', 'Hunter22'),
                             ('Environment=GMAIL_APP_PASSWORD=abcd efgh', 'abcd'),
                             ('ANKER_PASS="Hunter22"', 'Hunter22'), ('psk=Hunter22', 'Hunter22')]:
            self.assertNotIn(secret, rec.redact(text), text)

    def test_ordinary_lines_pass_through(self):
        for text in ('Secrets were required, but not provided', 'wlx: CTRL-EVENT-CONNECTED - Connection to 02:00',
                     'psk-flags: 0'):
            self.assertEqual(rec.redact(text), text)


class RecorderFilters(unittest.TestCase):
    def setUp(self):
        self.sink = ListSink()
        self.r = rec.Recorder(self.sink, IFACE)

    def test_wlp5s0_rssi_noise_is_counted_and_camera_events_queue_a_cache_read(self):
        self.r.on_iw('[2026-09-29 14:36:37.205764]: wlp5s0 (phy #1): CQM event: RSSI (-35 dBm) went above threshold')
        self.r.on_iw(f'[2026-09-29 14:36:38.000001]: {IFACE} (phy #0): scan finished: 5180 5200, ""')
        self.r.on_iw('[2026-09-29 14:36:39.000001]: wlp5s0 (phy #1): disconnected (local request) reason: 3')
        self.assertEqual(self.r.dropped, {'iw': 1})
        lines = [f['line'] for _, f in self.sink.rows]
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith('scan finished: 5180 5200, ""') and lines[1].endswith('reason: 3'))
        self.assertEqual(self.r.dumps.get_nowait(), 'scan finished')
        self.assertTrue(self.r.dumps.empty())

    def test_journal_drops_other_ifaces_signal_noise_redacts_and_keeps_the_cursor(self):
        noise = {'MESSAGE': 'wlp5s0: CTRL-EVENT-SIGNAL-CHANGE above=1 signal=-33', '__CURSOR': 'c1'}
        mine = {'MESSAGE': f'{IFACE}: CTRL-EVENT-SIGNAL-CHANGE above=0 signal=-80', '__CURSOR': 'c2',
                '__MONOTONIC_TIMESTAMP': '12'}
        secret = {'MESSAGE': 'base3 : COMMAND=/usr/bin/nmcli con modify cam wifi-sec.psk Hunter22', '__CURSOR': 'c3',
                  'SYSLOG_IDENTIFIER': 'sudo'}
        raw = {'MESSAGE': list(b'mt76x2u 1-6:1.0: \xff'), '__CURSOR': 'c4'}
        for entry in (noise, mine, secret, raw):
            self.r.on_journal(json.dumps(entry))
        self.r.on_journal('not json password Hunter22')
        written = [f['MESSAGE'] for _, f in self.sink.rows]
        self.assertEqual(self.r.dropped, {'journal': 1})
        self.assertEqual(written[0], mine['MESSAGE'])
        self.assertNotIn('Hunter22', ' '.join(written))
        self.assertTrue(written[2].startswith('mt76x2u 1-6:1.0: '))
        self.assertEqual(self.r.journal_cursor, 'c4')
        self.assertIn('--after-cursor=c4', self.r.journal_argv())

    def test_ip_keeps_only_the_camera_adapter_and_subnet(self):
        for line in ('[2026-09-29T14:36:35.800679] 198.51.100.110 dev enp6s0 lladdr 02:01 REACHABLE',
                     f'[2026-09-29T14:36:36.000001] 7: {IFACE}: <NO-CARRIER> state DOWN',
                     '[2026-09-29T14:36:37.000001] 192.0.2.1 dev wlxother lladdr 02:00 STALE'):
            self.r.on_ip(line)
        self.assertEqual(len(self.sink.rows), 2)
        self.assertEqual(self.r.dropped, {'ip': 1})

    def test_networkmanager_lines_are_kept_only_when_they_concern_the_camera(self):
        self.r.nm_path, self.r.ssid = DEV, 'X5 TEST01.OSC'
        keep = [f"{DEV}: org.freedesktop.NetworkManager.Device.StateChanged (uint32 100, uint32 70, uint32 0)",
                f"{DEV}: org.freedesktop.DBus.Properties.PropertiesChanged ('org.freedesktop.NetworkManager.Device.Wireless',"
                " {'LastScan': <int64 3968760>}, @as [])",
                "/org/freedesktop: org.freedesktop.DBus.ObjectManager.InterfacesAdded (objectpath "
                f"'/org/freedesktop/NetworkManager/AccessPoint/500', {{'org.freedesktop.NetworkManager.AccessPoint': "
                f"{{'Ssid': <[byte {CAM_SSID_BYTES}]>, 'Frequency': <uint32 5745>}}}})",
                f"{DEV}: org.freedesktop.NetworkManager.Device.Wireless.AccessPointAdded (objectpath "
                "'/org/freedesktop/NetworkManager/AccessPoint/500',)",
                "/org/freedesktop/NetworkManager/AccessPoint/500: org.freedesktop.DBus.Properties.PropertiesChanged "
                "('org.freedesktop.NetworkManager.AccessPoint', {'Strength': <byte 0x39>, 'LastSeen': <6131>}, @as [])",
                "/org/freedesktop/NetworkManager/ActiveConnection/5: org.freedesktop.NetworkManager.Connection.Active."
                "StateChanged (uint32 2, uint32 0)",
                "/org/freedesktop/NetworkManager/DHCP4Config/2: org.freedesktop.DBus.Properties.PropertiesChanged "
                "('org.freedesktop.NetworkManager.DHCP4Config', {'Options': <{}>}, @as [])"]
        drop = [f"{DEV}: org.freedesktop.DBus.Properties.PropertiesChanged ('org.freedesktop.NetworkManager.Device',"
                " {'State': <uint32 40>}, @as [])",
                f"{DEV}: org.freedesktop.DBus.Properties.PropertiesChanged ('org.freedesktop.NetworkManager.Device.Statistics',"
                " {'RxBytes': <uint64 113>}, @as [])",
                f"{DEV}: org.freedesktop.NetworkManager.Device.Wireless.AccessPointAdded (objectpath "
                "'/org/freedesktop/NetworkManager/AccessPoint/501',)",
                "/org/freedesktop/NetworkManager/AccessPoint/500: org.freedesktop.DBus.Properties.PropertiesChanged "
                "('org.freedesktop.NetworkManager.AccessPoint', {'Strength': <byte 0x40>}, @as [])",
                "/org/freedesktop/NetworkManager/AccessPoint/55: org.freedesktop.DBus.Properties.PropertiesChanged "
                "('org.freedesktop.NetworkManager.AccessPoint', {'Strength': <byte 0x52>, 'LastSeen': <3513>}, @as [])",
                "/org/freedesktop/NetworkManager/Devices/5: org.freedesktop.NetworkManager.Device.StateChanged "
                "(uint32 30, uint32 100, uint32 3)",
                "/org/freedesktop/NetworkManager/ActiveConnection/5: org.freedesktop.DBus.Properties.PropertiesChanged "
                "('org.freedesktop.NetworkManager.Connection.Active', {'StateFlags': <uint32 68>}, @as [])",
                "/org/freedesktop/NetworkManager: org.freedesktop.DBus.Properties.PropertiesChanged "
                "('org.freedesktop.NetworkManager', {'ActiveConnections': <[objectpath '/a']>}, @as [])",
                "The name org.freedesktop.NetworkManager is owned by :1.8"]
        for line in keep + drop:
            self.r.on_nm(line)
        self.assertEqual([f['line'] for _, f in self.sink.rows], keep)
        self.assertEqual(self.r.dropped, {'nm': len(drop)})

    def test_a_device_added_rereads_the_camera_device(self):
        with patch.object(rec.Recorder, 'nm_device') as reread:
            self.r.on_nm("/org/freedesktop/NetworkManager: org.freedesktop.NetworkManager.DeviceAdded "
                         "(objectpath '/org/freedesktop/NetworkManager/Devices/9',)")
        reread.assert_called_once()
        self.assertEqual(len(self.sink.rows), 1)

    def test_sockets_waiting_out_time_wait_are_left_out(self):
        with patch.object(rec, 'socket_owner', return_value='') as owner:
            self.r.note_sockets([{'local': '192.0.2.4:1', 'peer': '192.0.2.1:80', 'inode': 0, 'state': 'TIME-WAIT'}])
        self.assertEqual([f['sockets'] for _, f in self.sink.rows], [[]])
        owner.assert_not_called()

    def test_sockets_are_written_on_open_state_retransmit_idle_reuse_and_close(self):
        cam = {'local': '192.0.2.4:34556', 'peer': '192.0.2.1:80', 'inode': 7}
        with patch.object(rec, 'socket_owner', return_value='uvicorn pid 2496') as owner:
            for state, lastsnd, retrans in (('ESTAB', 100, '0/0'), ('ESTAB', 20000, '0/0'), ('ESTAB', 40000, '0/0'),
                                            ('ESTAB', 12, '0/0'), ('ESTAB', 500, '0/1'), ('CLOSE-WAIT', 900, '0/1')):
                self.r.note_sockets([{**cam, 'state': state, 'lastsnd_ms': lastsnd, 'retrans': retrans}])
            self.r.note_sockets([])
        rows = [f for _, f in self.sink.rows]
        self.assertEqual([(len(f['sockets']), f['reused']) for f in rows],
                         [(1, []), (1, ['192.0.2.4:34556 -> 192.0.2.1:80']), (1, []), (1, []), (0, [])])
        self.assertEqual([f['sockets'][0]['state'] for f in rows[:4]], ['ESTAB', 'ESTAB', 'ESTAB', 'CLOSE-WAIT'])
        self.assertEqual(rows[0]['sockets'][0]['owner'], 'uvicorn pid 2496')
        owner.assert_called_once_with(7)


class StationMoments(unittest.TestCase):
    def sample(self, rx, assoc=5, loss=0, failed=0):
        return [{'bssid': 'f2', 'beacon_rx': rx, 'assoc_at_boot_ns': assoc, 'beacon_loss': loss, 'tx_failed': failed}]

    def test_samples_become_join_stall_resume_loss_heartbeat_and_leave(self):
        w = rec.StationWatch()
        script = [(0.0, self.sample(0, assoc=0)), (0.2, self.sample(1)), (0.4, self.sample(3)), (0.6, self.sample(3)),
                  (0.8, self.sample(3)), (1.0, self.sample(3)), (1.2, self.sample(3)), (1.4, self.sample(5)),
                  (1.6, self.sample(6, loss=1)), (11.0, self.sample(90, loss=1)), (11.2, [])]
        got = [(t, w.events(s, t)) for t, s in script]
        self.assertEqual([(t, e) for t, e in got if e], [
            (0.0, ['joined']), (0.2, ['associated']), (1.0, ['beacon stall']), (1.4, ['beacons resumed']),
            (1.6, ['beacon loss']), (11.0, ['heartbeat']), (11.2, ['left'])])

    def test_no_stall_is_read_from_an_adapter_that_reports_no_beacon_count(self):
        w = rec.StationWatch()
        events = [w.events([{'bssid': 'f2', 'assoc_at_boot_ns': 5}], t / 5) for t in range(10)]
        self.assertNotIn('beacon stall', sum(events, []))

    def test_the_loop_writes_a_stall_with_its_length_and_reads_the_cache_at_once(self):
        samples = [self.sample(0, assoc=0), self.sample(1)] + [self.sample(2)] * 30

        class FakeNl:
            reads = []

            def __init__(self):
                pass

            def stations(self, ifindex):
                return samples.pop(0) if len(samples) > 1 else samples[0]

            def scan_cache(self, ifindex):
                FakeNl.reads.append(time.monotonic())
                return []

            def close(self):
                pass

        sink = ListSink()
        r = rec.Recorder(sink, IFACE)
        with patch.object(rec, 'Nl80211', FakeNl), patch.object(rec.socket, 'if_nametoindex', lambda n: 7):
            t = threading.Thread(target=r.netlink_loop)
            t.start()
            end = time.monotonic() + 5
            while time.monotonic() < end and not any(f.get('trigger') == 'beacon stall' for _, f in sink.rows):
                time.sleep(0.05)
            r.stop.set()
            t.join(2)
        stall = [f for s, f in sink.rows if s == 'station' and 'beacon stall' in f.get('events', [])]
        self.assertEqual(len(stall), 1)
        self.assertGreaterEqual(stall[0]['stall_s'], rec.STALL_S)
        after = [f for _, f in sink.rows[sink.rows.index(('station', stall[0])):] if f.get('trigger')]
        self.assertEqual(after[0]['trigger'], 'beacon stall')


class RecorderStart(unittest.TestCase):
    def test_the_recorder_refuses_to_start_without_its_station_values_and_writes_nothing(self):
        for missing in ({'CAM_IFACE': '', 'CAM_IFACE_UNSET': 'station.toml has no [camera] wifi_interface'},
                        {'CAM_IP': '', 'CAM_IP_UNSET': 'station.toml has no [camera] ip'}):
            with tempfile.TemporaryDirectory() as tmp, patch.multiple(rec, **{'CAM_IFACE': IFACE, **missing}), \
                    patch('sys.stderr') as err, patch.object(rec, 'Recorder') as recorder:
                with self.assertRaises(SystemExit) as stop:
                    rec.main(['--out', f'{tmp}/out', '--seconds', '0.01'])
                self.assertFalse(Path(tmp, 'out').exists())
            self.assertEqual(stop.exception.code, 2)
            self.assertIn(next(v for k, v in missing.items() if k.endswith('_UNSET')), ''.join(
                str(c.args[0]) for c in err.write.call_args_list))
            recorder.assert_not_called()


class RecorderOnlyReads(unittest.TestCase):
    ALLOWED ={('iw', 'event'), ('ip', '-ts'), ('gdbus', 'monitor'), ('gdbus', 'call'), ('nmcli', '-t'),
               ('nmcli', '-e'), ('journalctl', '-f')}

    def test_every_command_and_netlink_request_is_a_read(self):
        argvs, nls = [], []

        class FakeProc:
            pid, stdout = 0, iter(())

            def __init__(self, argv, **kw):
                argvs.append(list(argv))

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

            terminate = kill = lambda self: None

        def fake_run(argv, **kw):
            argvs.append(list(argv))
            return rec.subprocess.CompletedProcess(argv, 0, '', '')

        def fake_socket(*a):
            nls.append(FakeNetlink(*a))
            return nls[-1]

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(rec.subprocess, 'Popen', FakeProc), patch.object(rec.subprocess, 'run', fake_run), \
                patch.object(rec.socket, 'socket', fake_socket), patch.object(rec.socket, 'if_nametoindex', lambda n: 7), \
                patch.object(rec, 'SOCKET_IDLE_HZ', 20.0):
            r = rec.Recorder(rec.Sink(f'{tmp}/r.jsonl'), IFACE)
            r.start()
            r.on_iw(f'[2026-09-29 14:36:38.000001]: {IFACE} (phy #0): disconnected (by AP) reason: 4')
            end = time.monotonic() + 3
            while time.monotonic() < end and not (any(c == 32 for s in nls for _, c, _ in s.sent)
                                                  and any(k == 20 for s in nls for k, _, _ in s.sent)):
                time.sleep(0.05)
            r.close()
        self.assertEqual([t.name for t in r.threads if t.is_alive()], [])
        self.assertEqual({s.protocol for s in nls}, {4, 16}, 'only sock_diag and generic netlink sockets')
        sent = {(kind, cmd) for s in nls for kind, cmd, _ in s.sent}
        self.assertIn((0x1c, 32), sent)
        self.assertIn((20, socket.AF_INET), sent)
        self.assertEqual(sent - {(16, 3), (0x1c, 17), (0x1c, 32), (20, socket.AF_INET)}, set(),
                         'nl80211 GET_STATION 17, GET_SCAN 32 and sock_diag dumps only')
        self.assertTrue(all(flags & rec.NLM_F_DUMP for s in nls for kind, _, flags in s.sent if kind != 16))
        self.assertTrue(argvs)
        for argv in argvs:
            self.assertIn(tuple(argv[:2]), self.ALLOWED, argv)
            self.assertNotIn('wlp5s0', ' '.join(argv), argv)
            if argv[0] == 'gdbus' and argv[1] == 'call':
                method = argv[argv.index('--method') + 1]
                self.assertTrue(method.endswith(('GetDeviceByIpIface', 'GetManagedObjects')), argv)
            if argv[0] == 'nmcli':
                self.assertEqual(argv[-4:-2] if argv[-3] == 'uuid' else argv[-2:], ['connection', 'show'], argv)
                self.assertNotIn('--show-secrets', argv)


class TimelineClocks(unittest.TestCase):
    def setUp(self):
        self.t = tl.Timeline()
        self.t.feed(record('recorder', event='camera', iface=IFACE, ssid='X5 TEST01.OSC', bssid='02:00:00:00:00:01'))
        self.t.events.clear()

    def texts(self, key_only=False):
        return [e[3] for e in self.t.events if e[4] or not key_only]

    def test_iw_and_ip_stamps_land_on_the_boot_clock(self):
        self.t.feed(record('iw', line=f'[{stamp(R - 1_500_000_000)}]: {IFACE} (phy #0): scan started'))
        self.t.feed(record('ip', line=f'[{stamp(R - 250_000_000, iso=True)}] 7: {IFACE}: state UP'))
        first, second = self.t.events
        self.assertEqual(first[:4], (B - 1_500_000_000, R - 1_500_000_000, 'iw', 'scan started'))
        self.assertEqual(second[0], B - 250_000_000)

    def test_journal_uses_journald_monotonic_time(self):
        self.t.feed(record('journal', MESSAGE='activated', _SYSTEMD_UNIT='NetworkManager.service',
                           __MONOTONIC_TIMESTAMP=str((M - 2_000_000_000) // 1000)))
        self.assertEqual(self.t.events[0][:3], (B - 2_000_000_000, R - 2_000_000_000, 'nm-log'))

    def test_lines_with_one_stamp_keep_the_order_they_were_written_in(self):
        for line in ('Traceback (most recent call last):', '  File "camera_connection.py", line 249', 'CameraError: x'):
            self.t.feed(record('journal', MESSAGE=line, _SYSTEMD_UNIT='selfie.service', __MONOTONIC_TIMESTAMP=str(M // 1000)))
        rendered = tl.render(self.t.events, show_all=True)
        self.assertEqual([r.split('app', 1)[1].strip() for r in rendered],
                         ['Traceback (most recent call last):', 'File "camera_connection.py", line 249', 'CameraError: x'])

    def test_a_tsf_zero_is_confirmed_only_by_a_new_frame_that_agrees_within_20_ms(self):
        cam = {'bssid': '02:00:00:00:00:01', 'ssid': 'X5 TEST01.OSC', 'freq': 5745, 'presp': True}
        zero = B - 13_000_000_000
        read_a = {**cam, 'last_seen_boot_ns': zero + 7_444_000_000, 'tsf_us': 7_444_000, 'beacon_tsf_us': 8_704_000}
        read_b = {**cam, 'last_seen_boot_ns': zero + 12_698_000_000, 'tsf_us': 7_444_000, 'beacon_tsf_us': 12_698_000}
        self.t.feed(record('scan', trigger='scan finished', bss=[read_a]))
        self.t.feed(record('scan', trigger='scan finished', bss=[read_a]))
        self.assertFalse(any('confirmed by' in x for x in self.texts()), 'the same frame read twice confirms nothing')
        expected = (f'TSF zero at {tl.clock(zero - 1_260_000_000 + R - B)} (beacon) or {tl.clock(zero + R - B)} '
                    '(probe response), unconfirmed')
        self.assertEqual(sum(expected in x for x in self.texts()), 1)
        self.t.feed(record('scan', trigger='connected to', bss=[read_b]))
        confirmed = [e for e in self.t.events if 'confirmed by two cache reads' in e[3]]
        self.assertEqual([e[0] for e in confirmed], [zero])

    def test_one_beacon_read_twice_is_one_frame_and_confirms_nothing(self):
        cam = {'bssid': '02:00:00:00:00:01', 'ssid': 'X5 TEST01.OSC', 'freq': 5745}
        frame = {**cam, 'last_seen_boot_ns': B - 5_000_000_000, 'beacon_tsf_us': 2_000_000}
        for _ in range(3):
            self.t.feed(record('scan', trigger='beacon loss', bss=[frame]))
        self.assertFalse(any('confirmed by' in e[3] for e in self.t.events))
        self.t.feed(record('scan', trigger='beacon loss', bss=[{**frame, 'last_seen_boot_ns': B - 4_000_000_000,
                                                                 'beacon_tsf_us': 3_000_000}]))
        self.assertEqual([e[0] for e in self.t.events if 'confirmed by' in e[3]], [B - 7_000_000_000])

    def test_the_steady_pairing_wins_when_the_other_wanders_by_tens_of_ms(self):
        cam = {'bssid': '02:00:00:00:00:01', 'ssid': 'X5 TEST01.OSC', 'freq': 5745, 'presp': True}
        zero = B - 30_000_000_000
        for seen, beacon_off in ((988_000_000, -1_265_000_000), (23_556_000_000, -1_327_000_000)):
            self.t.feed(record('scan', trigger='scan finished', bss=[{**cam, 'last_seen_boot_ns': zero + seen,
                                                                     'tsf_us': seen // 1000,
                                                                     'beacon_tsf_us': (seen - beacon_off) // 1000}]))
        confirmed = [e for e in self.t.events if 'confirmed by two cache reads' in e[3]]
        self.assertEqual([e[0] for e in confirmed], [zero])

    def test_a_new_tsf_count_is_reported_with_the_one_it_replaced(self):
        cam = {'bssid': '02:00:00:00:00:01', 'ssid': 'X5 TEST01.OSC', 'freq': 5745}
        for zero in (B - 100_000_000_000, B - 2_000_000_000):
            for seen in (1_000_000_000, 1_500_000_000):
                self.t.feed(record('scan', trigger='scan finished',
                                   bss=[{**cam, 'last_seen_boot_ns': zero + seen, 'beacon_tsf_us': seen // 1000}]))
        confirmed = [e for e in self.t.events if 'confirmed by two cache reads' in e[3]]
        self.assertEqual([e[0] for e in confirmed], [B - 100_000_000_000, B - 2_000_000_000])
        self.assertIn(f'the previous TSF zero was {tl.clock(R - 100_000_000_000)}', confirmed[1][3])

    def test_a_scan_end_carries_its_duration_and_who_asked_and_the_cache_its_highest_channel(self):
        self.t.feed(record('iw', line=f'[{stamp(R)}]: {IFACE} (phy #0): scan started'))
        self.t.feed(record('iw', boot=B + 10_480_000_000,
                           line=f'[{stamp(R + 10_480_000_000)}]: {IFACE} (phy #0): scan aborted: 2412 5180 5805, ""'))
        self.t.feed(record('scan', boot=B + 10_490_000_000, trigger='scan aborted', bss=[
            {'bssid': 'a', 'freq': 2412, 'seen_ms_ago': 5}, {'bssid': 'b', 'freq': 5240, 'seen_ms_ago': 9000},
            {'bssid': 'c', 'freq': 5805, 'seen_ms_ago': 60000}]))
        self.t.feed(record('journal', boot=B + 11_000_000_000, SYSLOG_IDENTIFIER='sudo',
                           MESSAGE='base3 : COMMAND=/usr/local/libexec/selfie-camera-control scan'))
        self.t.feed(record('iw', boot=B + 11_300_000_000, line=f'[{stamp(R + 11_300_000_000)}]: {IFACE} (phy #0): scan started'))
        self.t.feed(record('iw', boot=B + 18_300_000_000,
                           line=f'[{stamp(R + 18_300_000_000)}]: {IFACE} (phy #0): scan finished: 5180 5745, ""'))
        self.assertEqual(self.texts(key_only=True), [
            'background scan aborted after 10.5 s: 3 channels, ""',
            'cache after scan aborted: 2 channels heard in the last 15 s, highest 5240 MHz, nothing at 5745 MHz or above',
            "camera AP not in the adapter's cache (read after scan aborted)",
            'helper scan finished after 7.0 s: 2 channels, ""'])

    def test_the_helper_is_credited_with_the_scan_that_starts_after_its_sudo_line(self):
        def iw(at, text):
            self.t.feed(record('iw', boot=B + at, line=f'[{stamp(R + at)}]: {IFACE} (phy #0): {text}'))
        iw(0, 'scan started')
        self.t.feed(record('journal', boot=B + 1_000_000_000, SYSLOG_IDENTIFIER='sudo',
                           MESSAGE='base3 : COMMAND=/usr/local/libexec/selfie-camera-control scan'))
        iw(22_200_000_000, 'scan finished: 2412 5180, ""')
        iw(22_500_000_000, 'scan started')
        iw(29_500_000_000, 'scan finished: 5180 5745, ""')
        ends = [e[3] for e in self.t.events if 'finished' in e[3]]
        self.assertEqual(ends, ['background scan finished after 22.2 s: 2 channels, ""',
                                'helper scan finished after 7.0 s: 2 channels, ""'])

    def test_a_helper_command_whose_journal_line_arrives_late_still_claims_its_own_scan_only(self):
        def iw(at, text):
            self.t.feed(record('iw', boot=B + at, line=f'[{stamp(R + at)}]: {IFACE} (phy #0): {text}'))
        iw(300_000_000, 'scan started')
        self.t.feed(record('journal', boot=B + 400_000_000, SYSLOG_IDENTIFIER='sudo',
                           __MONOTONIC_TIMESTAMP=str((M + 10_000_000) // 1000),
                           MESSAGE='base3 : COMMAND=/usr/local/libexec/selfie-camera-control scan'))
        iw(7_300_000_000, 'scan finished: 5180 5745, ""')
        iw(8_000_000_000, 'scan started')
        iw(29_000_000_000, 'scan finished: 2412 5180 5745, ""')
        ends = [e[3] for e in self.t.events if 'finished' in e[3]]
        self.assertEqual(ends, ['helper scan finished after 7.0 s: 2 channels, ""',
                                'background scan finished after 21.0 s: 3 channels, ""'])

    def test_background_scans_show_only_when_their_outcome_changes(self):
        for i, end in enumerate(('aborted', 'aborted', 'finished', 'finished')):
            t0 = B + i * 100_000_000_000
            self.t.feed(record('iw', boot=t0, line=f'[{stamp(R + t0 - B)}]: {IFACE} (phy #0): scan started'))
            self.t.feed(record('iw', boot=t0 + 10**10, line=f'[{stamp(R + t0 - B + 10**10)}]: {IFACE} (phy #0): scan {end}: 5180, ""'))
        self.assertEqual(self.texts(key_only=True), ['background scan aborted after 10.0 s: 1 channels, ""',
                                                     'background scan finished after 10.0 s: 1 channels, ""'])

    def test_recorded_station_moments_are_placed_and_worded(self):
        s = {'bssid': 'f2', 'assoc_at_boot_ns': B - 100_000_000, 'beacon_rx': 533, 'beacon_loss': 1}
        for events, extra in ((['joined'], {}), (['associated'], {}), (['heartbeat'], {}),
                              (['beacon stall'], {'stall_s': 0.8}), (['beacon loss'], {}), (['left'], {})):
            self.t.feed(record('station', events=events, stations=[] if events == ['left'] else [s], **extra))
        self.assertEqual(self.texts(key_only=True), [
            'station entry for f2 (authenticating)', 'associated with f2 (kernel association time)',
            'no beacon counted for 0.8 s (count stuck at 533)', 'kernel beacon-loss count now 1', 'no longer associated'])
        self.assertEqual(self.t.events[1][0], B - 100_000_000)

    def test_a_beacon_gap_in_an_old_recording_is_reported_once_with_its_end(self):
        counts = [100, 102, 104, 104, 104, 104, 104, 106]
        for i, n in enumerate(counts):
            self.t.feed(record('station', boot=B + i * 200_000_000, stations=[{'bssid': 'f2', 'beacon_rx': n}]))
        texts = self.texts(key_only=True)
        self.assertEqual(sum('no beacon counted since' in x for x in texts), 1)
        self.assertEqual(sum(x == 'beacons counted again' for x in texts), 1)


class TimelineFilters(unittest.TestCase):
    def setUp(self):
        self.t = tl.Timeline()
        self.t.feed(record('recorder', event='camera', iface=IFACE, ssid='X5 TEST01.OSC', bssid='02:00:00:00:00:01'))
        self.t.feed(record('recorder', event='nm_device', path=DEV, camera_aps=[]))
        self.t.events.clear()

    def keys(self):
        return [e[3] for e in self.t.events if e[4]]

    def test_the_sequence_view_keeps_milestones_and_drops_detail(self):
        for msg, unit in (('INFO:     camera op=1 connect requested by 198.51.100.30', 'selfie.service'),
                          ('INFO:     198.51.100.30:65469 - "POST /connect HTTP/1.1" 202 Accepted', 'selfie.service'),
                          ('INFO:     camera op=1 stage=ready elapsed=21.472s Camera connected and preview ready.', 'selfie.service'),
                          ('XArmHandler::Move complete. Reached target angles.', 'selfie.service'),
                          ('<info>  device (wlx): state change: config -> need-auth (reason none)', 'NetworkManager.service'),
                          ("<info>  device (wlx): Activation: starting connection 'X5'", 'NetworkManager.service'),
                          ('<info>  dhcp4 (wlx): state changed new lease, address=192.0.2.4, acd pending', 'NetworkManager.service'),
                          ('<info>  dhcp4 (wlx): state changed new lease, address=192.0.2.4', 'NetworkManager.service'),
                          ('wlx: CTRL-EVENT-BEACON-LOSS', 'wpa_supplicant.service')):
            self.t.feed(record('journal', MESSAGE=msg, _SYSTEMD_UNIT=unit))
        self.assertEqual(self.keys(), ['camera op=1 connect requested by 198.51.100.30',
                                       'camera op=1 stage=ready elapsed=21.472s Camera connected and preview ready.',
                                       "<info>  device (wlx): Activation: starting connection 'X5'",
                                       '<info>  dhcp4 (wlx): state changed new lease, address=192.0.2.4'])
        rendered = tl.render(self.t.events)
        self.assertEqual(rendered[0], '')
        self.assertEqual(len(rendered), 5)
        self.assertEqual(len(tl.render(self.t.events, show_all=True)), 9)

    def nm(self, line):
        self.t.feed(record('nm', line=line))

    def test_nm_shows_the_camera_ap_arriving_and_leaving(self):
        self.nm("/org/freedesktop: org.freedesktop.DBus.ObjectManager.InterfacesAdded (objectpath "
                "'/org/freedesktop/NetworkManager/AccessPoint/500', {'org.freedesktop.NetworkManager.AccessPoint': "
                f"{{'Ssid': <[byte {CAM_SSID_BYTES}]>, 'Frequency': <uint32 5240>}}}})")
        self.nm(f"{DEV}: org.freedesktop.NetworkManager.Device.Wireless.AccessPointAdded (objectpath "
                "'/org/freedesktop/NetworkManager/AccessPoint/500',)")
        self.nm(f"{DEV}: org.freedesktop.NetworkManager.Device.Wireless.AccessPointAdded (objectpath "
                "'/org/freedesktop/NetworkManager/AccessPoint/501',)")
        self.nm(f"{DEV}: org.freedesktop.NetworkManager.Device.StateChanged (uint32 100, uint32 70, uint32 0)")
        self.nm(f"{DEV}: org.freedesktop.NetworkManager.Device.Wireless.AccessPointRemoved (objectpath "
                "'/org/freedesktop/NetworkManager/AccessPoint/500',)")
        self.assertEqual(self.keys(), ['NetworkManager lists the camera AP on 5240 MHz',
                                       'NetworkManager dropped the camera AP from its list'])
        self.assertIn('device activated (from ip-config, reason 0)', [e[3] for e in self.t.events])

    def test_the_wake_beacon_shows_when_bluetooth_advertising_starts_and_stops(self):
        for n in ('0x01', '0x01', '0x00'):
            self.t.feed(record('bluez', line="/org/bluez/hci0: org.freedesktop.DBus.Properties.PropertiesChanged "
                                             f"('org.bluez.LEAdvertisingManager1', {{'ActiveInstances': <byte {n}>}}, @as [])"))
        self.assertEqual(self.keys(), ['Bluetooth advertising on (1 active)', 'Bluetooth advertising off (0 active)'])

    def test_sockets_from_either_recorder_show_opening_idle_reuse_and_closing(self):
        line = 'ESTAB 0 0 192.0.2.4:53732 192.0.2.1:80 users:(("uvicorn",pid=1700961,fd=6))\n\t cubic rtt:3.1/1 lastsnd:{} lastrcv:9'
        for boot, value in ((B, line.format(313978)), (B + 500_000_000, line.format(12)), (B + 10**9, '')):
            self.t.feed(record('sockets', boot=boot, value=value))
        new = {'local': '192.0.2.4:40000', 'peer': '192.0.2.1:80', 'state': 'SYN-SENT', 'owner': 'python pid 9',
               'retrans': '0/0'}
        for boot, sockets in ((B + 2 * 10**9, [new]), (B + 3 * 10**9, [{**new, 'retrans': '1/1'}]),
                              (B + 4 * 10**9, [{**new, 'retrans': '1/2'}]), (B + 5 * 10**9, [])):
            self.t.feed(record('sockets', boot=boot, sockets=sockets, reused=[]))
        self.assertEqual(self.keys(), [
            '192.0.2.4:53732 -> 192.0.2.1:80 ESTAB (uvicorn pid 1700961)',
            '192.0.2.4:53732 -> 192.0.2.1:80 ESTAB: sent again after 314.0 s idle',
            '192.0.2.4:53732 -> 192.0.2.1:80 gone',
            '192.0.2.4:40000 -> 192.0.2.1:80 SYN-SENT (python pid 9)',
            '192.0.2.4:40000 -> 192.0.2.1:80 SYN-SENT: retransmits 1/1',
            '192.0.2.4:40000 -> 192.0.2.1:80 gone'])

    def test_closed_sockets_waiting_out_time_wait_are_not_milestones(self):
        probe = {'local': '192.0.2.4:46940', 'peer': '192.0.2.1:80', 'state': 'TIME-WAIT', 'owner': ''}
        live = {'local': '192.0.2.4:55916', 'peer': '192.0.2.1:80', 'state': 'ESTAB', 'owner': 'uvicorn pid 1'}
        for boot, sockets in ((B, [probe]), (B + 10**9, [probe, live]), (B + 2 * 10**9, [probe, {**live, 'state': 'TIME-WAIT'}]),
                              (B + 3 * 10**9, [])):
            self.t.feed(record('sockets', boot=boot, sockets=sockets, reused=[]))
        self.assertEqual(self.keys(), ['192.0.2.4:55916 -> 192.0.2.1:80 ESTAB (uvicorn pid 1)',
                                       '192.0.2.4:55916 -> 192.0.2.1:80 gone'])


if __name__ == '__main__':
    unittest.main()
