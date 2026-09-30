#!/usr/bin/env python3
"""Passive recorder for the selfie camera's Wi-Fi link; every source it uses is listen-only.

It never scans, joins or configures anything, and it sends nothing to wlp5s0. It polls fast and
writes only the moments a connect or disconnect sequence needs: joins and leaves, beacon stalls,
scans ending, NetworkManager's camera states, and camera sockets opening, closing or waking from
idle. Each output line is one JSON record stamped with the realtime, monotonic and boottime
clocks; camera_timeline.py merges them. Stop it with Ctrl-C or SIGTERM.

    python3 scripts/camera_recorder.py [--out DIR] [--iface IFACE] [--seconds N]

The adapter and the camera's address come from station.toml.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import re
import resource
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import station  # noqa: E402

CAM_IFACE, CAM_IFACE_UNSET = station.get("camera", "wifi_interface")
CAM_IP, CAM_IP_UNSET = station.get("camera", "ip")
NM = "org.freedesktop.NetworkManager"
NM_ROOT = "/org/freedesktop/NetworkManager"
STATION_HZ = 5.0
STALL_S = 0.6
HEARTBEAT_S = 10.0
SOCKET_HZ = 2.0
SOCKET_IDLE_HZ = 0.2
IDLE_REUSE_MS = 10000
USAGE_EVERY_S = 60.0
STREAM_RESTART_S = 5.0
JOURNAL_UNITS = ("selfie.service", "selfie-camera-adapter.service", "NetworkManager.service",
                 "wpa_supplicant.service", "bluetooth.service")
JOURNAL_KEEP = ("__REALTIME_TIMESTAMP", "__MONOTONIC_TIMESTAMP", "_SOURCE_REALTIME_TIMESTAMP",
                "_SOURCE_MONOTONIC_TIMESTAMP", "TIMESTAMP_BOOTTIME", "TIMESTAMP_MONOTONIC",
                "SYSLOG_IDENTIFIER", "_SYSTEMD_UNIT", "_PID", "PRIORITY")
SCAN_TRIGGERS = ("scan finished", "scan aborted", "connected to", "disconnected", "deauth",
                 "disassoc", "beacon loss")
NM_NOISE = {"State", "StateReason", "Bitrate", "InterfaceFlags", "Metered", "Mode", "Ip4Connectivity",
            "Ip6Connectivity", "IpInterface", "AccessPoints", "Strength", "RxBytes", "TxBytes"}
_SECRET = re.compile(r"(?i)((?:psk|password|passwd|passphrase|secret|wep-key\d*|anker_\w+|\w*_pass)"
                     r"[\"']?\s*[=:\s]\s*[\"']?)[^\s\"',;]+")
_OBJ = re.compile(r"objectpath '([^']+)'")
_KEYS = re.compile(r"'(\w+)': <")
_SSID = re.compile(r"'Ssid': <\[byte ([^\]]*)\]>")
_AP_SSID = re.compile(r"'(/org/freedesktop/NetworkManager/AccessPoint/\d+)': \{'org\.freedesktop\.NetworkManager\."
                      r"AccessPoint': \{[^}]*?'Ssid': <\[byte ([^\]]*)\]>")
_AP_PATH = re.compile(r"/org/freedesktop/NetworkManager/AccessPoint/\d+")

# Generic netlink, nl80211 and sock_diag numbers, from /usr/include/linux (genetlink.h, nl80211.h,
# netlink.h, sock_diag.h, inet_diag.h, tcp.h).
NETLINK_SOCK_DIAG, NETLINK_GENERIC = 4, 16
NLM_F_REQUEST, NLM_F_DUMP = 0x1, 0x300
NLMSG_ERROR, NLMSG_DONE = 2, 3
GENL_ID_CTRL, CTRL_CMD_GETFAMILY, CTRL_ATTR_FAMILY_NAME, CTRL_ATTR_FAMILY_ID = 16, 3, 2, 1
CMD_GET_STATION, CMD_GET_SCAN = 17, 32
ATTR_IFINDEX, ATTR_MAC, ATTR_STA_INFO, ATTR_BSS = 3, 6, 21, 47
STA_INFO = {1: ("inactive_ms", "I"), 7: ("signal_dbm", "b"), 9: ("rx_packets", "I"),
            10: ("tx_packets", "I"), 11: ("tx_retries", "I"), 12: ("tx_failed", "I"),
            13: ("signal_avg_dbm", "b"), 16: ("connected_s", "I"), 18: ("beacon_loss", "I"),
            28: ("rx_drop_misc", "Q"), 29: ("beacon_rx", "Q"), 30: ("beacon_signal_avg_dbm", "b"),
            42: ("assoc_at_boot_ns", "Q")}
STA_RATES = {8: "tx_mbps", 14: "rx_mbps"}
RATE_BITRATE, RATE_BITRATE32 = 1, 5
BSS_INFO = {2: ("freq", "I"), 3: ("tsf_us", "Q"), 4: ("beacon_int_tu", "H"), 7: ("signal_mbm", "i"),
            9: ("status", "I"), 10: ("seen_ms_ago", "I"), 13: ("beacon_tsf_us", "Q"),
            15: ("last_seen_boot_ns", "Q")}
BSS_BSSID, BSS_IES, BSS_BEACON_IES, BSS_PRESP_DATA = 1, 6, 11, 14
SOCK_DIAG_BY_FAMILY, INET_DIAG_INFO, INET_DIAG_MSG_LEN = 20, 2, 72
TCP_STATES = {1: "ESTAB", 2: "SYN-SENT", 3: "SYN-RECV", 4: "FIN-WAIT-1", 5: "FIN-WAIT-2", 6: "TIME-WAIT",
              7: "CLOSE", 8: "CLOSE-WAIT", 9: "LAST-ACK", 10: "LISTEN", 11: "CLOSING"}


def redact(text: str) -> str:
    return _SECRET.sub(r"\1[redacted]", text)


def ssid_bytes(text: str):
    """The SSID in a D-Bus 'Ssid': <[byte ...]> value, or None."""
    m = _SSID.search(text)
    if not m:
        return None
    try:
        return bytes(int(b, 16) for b in m[1].split(", ") if b).decode("utf-8", "replace")
    except ValueError:
        return None


def nla(kind: int, payload: bytes) -> bytes:
    return struct.pack("=HH", 4 + len(payload), kind) + payload + b"\0" * (-len(payload) % 4)


def parse_attrs(buf: bytes) -> dict:
    """{type: payload} of a netlink attribute stream, with the nested and byte-order flags masked off."""
    out, i = {}, 0
    while i + 4 <= len(buf):
        size, kind = struct.unpack_from("=HH", buf, i)
        if size < 4:
            break
        out[kind & 0x3FFF] = buf[i + 4:i + size]
        i += (size + 3) & ~3
    return out


def unpack(fields: dict, attrs: dict) -> dict:
    out = {}
    for kind, (name, fmt) in fields.items():
        raw = attrs.get(kind)
        if raw is not None and len(raw) >= struct.calcsize("=" + fmt):
            out[name] = struct.unpack_from("=" + fmt, raw)[0]
    return out


def mac(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw[:6])


def ssid_of(ies: bytes):
    i = 0
    while i + 2 <= len(ies):
        kind, size = ies[i], ies[i + 1]
        if kind == 0:
            return ies[i + 2:i + 2 + size].decode("utf-8", "replace")
        i += 2 + size
    return None


def rate_mbps(raw: bytes):
    a = parse_attrs(raw)
    if RATE_BITRATE32 in a:
        return struct.unpack_from("=I", a[RATE_BITRATE32])[0] / 10
    if RATE_BITRATE in a:
        return struct.unpack_from("=H", a[RATE_BITRATE])[0] / 10
    return None


def station_entry(attrs: dict) -> dict:
    info = parse_attrs(attrs.get(ATTR_STA_INFO, b""))
    out = {"bssid": mac(attrs[ATTR_MAC])} if ATTR_MAC in attrs else {}
    out.update(unpack(STA_INFO, info))
    for kind, name in STA_RATES.items():
        if kind in info:
            out[name] = rate_mbps(info[kind])
    return out


def bss_entry(attrs: dict) -> dict:
    bss = parse_attrs(attrs.get(ATTR_BSS, b""))
    out = {"bssid": mac(bss[BSS_BSSID])} if BSS_BSSID in bss else {}
    out["ssid"] = ssid_of(bss.get(BSS_IES) or bss.get(BSS_BEACON_IES) or b"")
    out.update(unpack(BSS_INFO, bss))
    out["presp"] = BSS_PRESP_DATA in bss
    return out


def tcp_entry(msg: bytes) -> dict:
    """One inet_diag_msg with its tcp_info: addresses, state, and the idle and retransmit counters."""
    sport, dport = struct.unpack_from("!HH", msg, 4)
    out = {"local": f"{socket.inet_ntoa(msg[8:12])}:{sport}", "peer": f"{socket.inet_ntoa(msg[24:28])}:{dport}",
           "state": TCP_STATES.get(msg[1], str(msg[1])), "inode": struct.unpack_from("=I", msg, 68)[0]}
    info = parse_attrs(msg[INET_DIAG_MSG_LEN:]).get(INET_DIAG_INFO)
    if info and len(info) >= 104:
        sent, _, recv, ack = struct.unpack_from("=IIII", info, 44)
        out.update(lastsnd_ms=sent, lastrcv_ms=recv, lastack_ms=ack, unacked=struct.unpack_from("=I", info, 24)[0],
                   rtt_ms=struct.unpack_from("=I", info, 68)[0] / 1000,
                   retrans=f"{info[2]}/{struct.unpack_from('=I', info, 100)[0]}")
    return out


class Netlink:
    """One netlink socket that sends a request and collects its reply messages."""

    def __init__(self, protocol):
        self.sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, protocol)
        self.sock.settimeout(2.0)
        self.sock.bind((0, 0))
        self.seq = 0

    def _ask(self, kind, flags, payload, header=4):
        """Payloads of the reply, each past its `header` bytes of fixed header."""
        self.seq += 1
        self.sock.send(struct.pack("=IHHII", 16 + len(payload), kind, NLM_F_REQUEST | flags, self.seq, 0) + payload)
        out = []
        while True:
            data = self.sock.recv(1 << 16)
            i = 0
            while i + 16 <= len(data):
                size, got, _, seq, _ = struct.unpack_from("=IHHII", data, i)
                if size < 16:
                    return out
                if seq == self.seq:
                    if got in (NLMSG_ERROR, NLMSG_DONE):
                        err = struct.unpack_from("=i", data, i + 16)[0] if size >= 20 else 0
                        if err < 0:
                            raise OSError(-err, os.strerror(-err))
                        return out
                    out.append(data[i + 16 + header:i + size])
                    if not flags & NLM_F_DUMP:
                        return out
                i += (size + 3) & ~3

    def close(self):
        self.sock.close()


class Nl80211(Netlink):
    """Station and scan-cache dumps over generic netlink; reads only, and no process per poll."""

    def __init__(self):
        super().__init__(NETLINK_GENERIC)
        reply = self.genl(GENL_ID_CTRL, 0, CTRL_CMD_GETFAMILY, nla(CTRL_ATTR_FAMILY_NAME, b"nl80211\0"))
        self.family = struct.unpack_from("=H", reply[0][CTRL_ATTR_FAMILY_ID])[0]

    def genl(self, family, flags, cmd, payload):
        return [parse_attrs(p) for p in self._ask(family, flags, struct.pack("=BBH", cmd, 0, 0) + payload)]

    def stations(self, ifindex: int) -> list:
        return [station_entry(a) for a in self.genl(self.family, NLM_F_DUMP, CMD_GET_STATION,
                                                     nla(ATTR_IFINDEX, struct.pack("=I", ifindex)))]

    def scan_cache(self, ifindex: int) -> list:
        return [bss_entry(a) for a in self.genl(self.family, NLM_F_DUMP, CMD_GET_SCAN,
                                                 nla(ATTR_IFINDEX, struct.pack("=I", ifindex)))]


class SockDiag(Netlink):
    """TCP sockets to one IPv4 address, from one sock_diag dump."""

    def __init__(self):
        super().__init__(NETLINK_SOCK_DIAG)

    def tcp_to(self, ip: str) -> list:
        req = struct.pack("=BBBBI", socket.AF_INET, socket.IPPROTO_TCP, 1 << (INET_DIAG_INFO - 1), 0, 0xFFFFFFFF)
        want = socket.inet_aton(ip)
        return [tcp_entry(m) for m in self._ask(SOCK_DIAG_BY_FAMILY, NLM_F_DUMP, req + bytes(48), header=0)
                if len(m) >= INET_DIAG_MSG_LEN and m[24:28] == want]


def socket_owner(inode: int) -> str:
    """'name pid N' of the process holding a socket, from /proc/*/fd; '' when none is readable."""
    target = f"socket:[{inode}]"
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                if os.readlink(f"/proc/{pid}/fd/{fd}") == target:
                    with open(f"/proc/{pid}/comm") as f:
                        return f"{f.read().strip()} pid {pid}"
        except OSError:
            continue
    return ""


def camera_aps(objects: str, device: str, ssid) -> tuple:
    """(paths, count): the device's access points that carry the camera's SSID, from GetManagedObjects text."""
    m = re.search(re.escape(device) + r"': \{.*?'AccessPoints': <(?:@ao )?\[([^\]]*)\]>", objects, re.S)
    mine = _AP_PATH.findall(m[1]) if m else []
    ssids = {path: bytes(int(b, 16) for b in raw.split(", ") if b).decode("utf-8", "replace")
             for path, raw in _AP_SSID.findall(objects)}
    return [path for path in mine if ssid and ssids.get(path) == ssid], len(mine)


class StationWatch:
    """Turns 5 Hz station samples into the moments worth writing."""

    def __init__(self):
        self.bssid = self.assoc = self.rx = self.loss = self.failed = None
        self.rx_at = self.beat_at = 0.0
        self.stalled = False

    def events(self, stations: list, now: float) -> list:
        s = stations[0] if stations else None
        out = []
        if (s or {}).get("bssid") != self.bssid:
            out.append("joined" if s else "left")
            self.__init__()
            self.bssid = (s or {}).get("bssid")
            self.beat_at = now
        if not s:
            return out
        assoc = s.get("assoc_at_boot_ns") or None
        if assoc and assoc != self.assoc:
            out.append("associated")
            self.assoc, self.rx_at = assoc, now
        rx = s.get("beacon_rx")
        if rx is not None:
            if rx != self.rx:
                if self.stalled:
                    out.append("beacons resumed")
                self.rx, self.rx_at, self.stalled = rx, now, False
            elif self.assoc and not self.stalled and now - self.rx_at >= STALL_S:
                out.append("beacon stall")
                self.stalled = True
        for key, attr in (("beacon_loss", "loss"), ("tx_failed", "failed")):
            value, old = s.get(key), getattr(self, attr)
            if value is not None and old is not None and value > old:
                out.append(key.replace("_", " "))
            setattr(self, attr, value)
        if now - self.beat_at >= HEARTBEAT_S:
            out.append("heartbeat")
            self.beat_at = now
        return out


def run(cmd, timeout=5.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"<{type(e).__name__}>"


def proc_cpu_s(pid: int):
    try:
        with open(f"/proc/{pid}/stat") as f:
            rest = f.read().rsplit(")", 1)[1].split()
        return round((int(rest[11]) + int(rest[12])) / os.sysconf("SC_CLK_TCK"), 2)
    except (OSError, IndexError, ValueError):
        return None


class Sink:
    """One JSONL file; every record gets the three clocks, and writes after close are dropped."""

    def __init__(self, path):
        self._f = open(path, "a", buffering=1, encoding="utf-8")
        self._lock = threading.Lock()
        self.path = path
        self.counts = {}

    def write(self, src, /, **fields):
        rec = {"rt": time.time_ns(), "mono": time.monotonic_ns(),
               "boot": time.clock_gettime_ns(time.CLOCK_BOOTTIME), "src": src, **fields}
        line = json.dumps(rec, separators=(",", ":"), default=str)
        with self._lock:
            if self._f.closed:
                return
            self._f.write(line + "\n")
            self.counts[src] = self.counts.get(src, 0) + 1

    def size(self) -> int:
        with self._lock:
            return 0 if self._f.closed else self._f.tell()

    def close(self):
        with self._lock:
            self._f.close()


class Recorder:
    def __init__(self, sink, iface):
        self.sink, self.iface = sink, iface
        self.stop = threading.Event()
        self.dumps = queue.SimpleQueue()
        self.procs = {}
        self.threads = []
        self.joined = False
        self.dropped = {}
        self.journal_cursor = None
        self.ssid = None
        self.nm_path = None
        self.cam_aps = set()
        self.socks = {}
        self.sock_view = None
        self.owners = {}

    def drop(self, src):
        self.dropped[src] = self.dropped.get(src, 0) + 1

    # ── listen-only streams, restarted if one exits ────────────────────────
    def stream(self, name, argv, handle):
        def loop():
            while not self.stop.is_set():
                cmd = argv() if callable(argv) else argv
                try:
                    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, text=True, errors="replace",
                                         bufsize=1, start_new_session=True)
                except OSError as e:
                    self.sink.write("recorder", event="source_failed", source=name, error=str(e))
                else:
                    self.procs[name] = p
                    for line in p.stdout:
                        handle(line.rstrip("\n"))
                    rc = p.wait()
                    if self.stop.is_set():
                        break
                    self.sink.write("recorder", event="source_ended", source=name, rc=rc)
                self.stop.wait(STREAM_RESTART_S)

        self.spawn(loop, name)

    def spawn(self, target, name):
        t = threading.Thread(target=target, name=f"rec-{name}", daemon=True)
        t.start()
        self.threads.append(t)

    def on_iw(self, line):
        if "CQM event: RSSI" in line and self.iface not in line:
            self.drop("iw")
            return
        self.sink.write("iw", line=line)
        if self.iface in line and any(t in line for t in SCAN_TRIGGERS):
            self.dumps.put(next(t for t in SCAN_TRIGGERS if t in line))

    def on_ip(self, line):
        if self.iface in line or CAM_IP.rsplit(".", 1)[0] + "." in line:
            self.sink.write("ip", line=line)
        else:
            self.drop("ip")

    def on_nm(self, line):
        path, _, rest = line.partition(": ")
        if "DeviceAdded" in rest or "DeviceRemoved" in rest:
            self.sink.write("nm", line=line)
            self.nm_device()
        elif self.nm_keep(path, rest):
            self.sink.write("nm", line=line)
        else:
            self.drop("nm")

    def nm_keep(self, path, rest) -> bool:
        """Whether a NetworkManager signal concerns the camera adapter, its access point or its connection."""
        objects = _OBJ.findall(rest)
        if "InterfacesAdded" in rest or "InterfacesRemoved" in rest:
            if objects and (objects[0] in self.cam_aps or (self.ssid and ssid_bytes(rest) == self.ssid)):
                self.cam_aps.add(objects[0])
                return True
            return False
        keys = set(_KEYS.findall(rest)) if "PropertiesChanged" in rest else set()
        if path == self.nm_path:
            if "AccessPointAdded" in rest or "AccessPointRemoved" in rest:
                return bool(objects) and objects[0] in self.cam_aps
            return not ("Device.Statistics" in rest or (keys and keys <= NM_NOISE))
        if path in self.cam_aps:
            return not (keys and keys <= NM_NOISE)
        if path == NM_ROOT or "/ActiveConnection/" in path:
            return "StateChanged" in rest
        return "/DHCP4Config/" in path

    def on_bluez(self, line):
        self.sink.write("bluez", line=line)

    def on_journal(self, line):
        try:
            rec = json.loads(line)
        except ValueError:
            self.sink.write("journal", MESSAGE=redact(line))
            return
        self.journal_cursor = rec.get("__CURSOR", self.journal_cursor)
        msg = rec.get("MESSAGE")
        if isinstance(msg, list):
            msg = bytes(msg).decode("utf-8", "replace")
        if isinstance(msg, str) and "CTRL-EVENT-SIGNAL-CHANGE" in msg and not msg.startswith(self.iface):
            self.drop("journal")
            return
        keep = {k: rec[k] for k in JOURNAL_KEEP if k in rec}
        self.sink.write("journal", MESSAGE=redact(msg) if isinstance(msg, str) else msg, **keep)

    def journal_argv(self):
        where = [f"--after-cursor={self.journal_cursor}"] if self.journal_cursor else ["-n", "0"]
        match = []
        for unit in JOURNAL_UNITS:
            match += [f"_SYSTEMD_UNIT={unit}", "+"]
        return ["journalctl", "-f", "-o", "json", *where, *match, "_TRANSPORT=kernel", "+",
                "SYSLOG_IDENTIFIER=sudo"]

    # ── nl80211: station counters polled at 5 Hz, written on events; the scan cache read on events
    def netlink_loop(self):
        nl, watch, error = None, StationWatch(), None
        while not self.stop.is_set():
            try:
                why = self.dumps.get(timeout=1.0 / STATION_HZ)
            except queue.Empty:
                why = None
            if self.stop.is_set():
                break
            now = time.monotonic()
            try:
                ifindex = socket.if_nametoindex(self.iface)
                nl = nl or Nl80211()
                stations = nl.stations(ifindex)
                if why is not None:
                    self.sink.write("scan", trigger=why, bss=nl.scan_cache(ifindex))
            except OSError as e:
                problem = "adapter absent" if e.errno == 19 else f"nl80211: {e}"
                if problem != error:
                    self.sink.write("station", error=problem)
                    error = problem
                if nl is not None:
                    nl.close()
                nl, watch, self.joined = None, StationWatch(), False
                continue
            error = None
            self.joined = bool(stations)
            events = watch.events(stations, now)
            if events:
                extra = {"stall_s": round(now - watch.rx_at, 3)} if "beacon stall" in events else {}
                self.sink.write("station", events=events, ifindex=ifindex, stations=stations, **extra)
                if "beacon stall" in events:
                    self.dumps.put("beacon stall")
        if nl is not None:
            nl.close()

    # ── TCP to the camera: 2 Hz while joined, every 5 s otherwise, written on change ──
    def sockets_loop(self):
        diag, error = None, None
        while not self.stop.wait(1.0 / (SOCKET_HZ if self.joined else SOCKET_IDLE_HZ)):
            try:
                diag = diag or SockDiag()
                self.note_sockets(diag.tcp_to(CAM_IP))
                error = None
            except OSError as e:
                if str(e) != error:
                    self.sink.write("sockets", error=str(e))
                    error = str(e)
                if diag is not None:
                    diag.close()
                diag = None
        if diag is not None:
            diag.close()

    def note_sockets(self, entries: list):
        """Write the camera sockets when one opens, closes, changes state, retransmits or sends after an idle."""
        socks = {(s["local"], s["peer"]): s for s in entries if s["state"] != "TIME-WAIT"}
        for key, s in socks.items():
            if key not in self.owners:
                self.owners[key] = socket_owner(s["inode"])
            s["owner"] = self.owners[key]
        self.owners = {k: v for k, v in self.owners.items() if k in socks}
        view = {k: (s["state"], s.get("retrans")) for k, s in socks.items()}
        reused = [f"{k[0]} -> {k[1]}" for k, s in socks.items()
                  if k in self.socks and (self.socks[k].get("lastsnd_ms") or 0) >= IDLE_REUSE_MS
                  and (s.get("lastsnd_ms") or 0) < self.socks[k]["lastsnd_ms"]]
        if view != self.sock_view or reused:
            self.sink.write("sockets", sockets=list(socks.values()), reused=reused)
        self.socks, self.sock_view = socks, view

    # ── context the timeline needs ─────────────────────────────────────────
    def describe(self):
        """The camera adapter and the NetworkManager profile bound to it."""
        net = f"/sys/class/net/{self.iface}"
        info = {"iface": self.iface}
        for key, path in (("mac", "address"), ("phy", "phy80211/name"), ("operstate", "operstate")):
            try:
                with open(f"{net}/{path}") as f:
                    info[key] = f.read().strip()
            except OSError:
                info[key] = None
        dev = f"{net}/device"
        info["usb_port"] = os.path.basename(os.path.dirname(os.path.realpath(dev))) if os.path.exists(dev) else None
        for row in run(["nmcli", "-t", "-f", "UUID,TYPE", "connection", "show"]).splitlines():
            uuid, _, kind = row.partition(":")
            if kind != "802-11-wireless":
                continue
            got = run(["nmcli", "-e", "no", "-g", "connection.interface-name,802-11-wireless.ssid,"
                       "802-11-wireless.bssid", "connection", "show", "uuid", uuid]).split("\n")
            if got[0].strip() == self.iface:
                info.update(profile=uuid, ssid=(got + [""])[1].strip(), bssid=(got + ["", ""])[2].strip().lower())
                self.ssid = info["ssid"]
                break
        self.sink.write("recorder", event="camera", **info)
        self.nm_device()

    def nm_device(self):
        """The camera adapter's NetworkManager object and its access points, read without asking NM to do anything."""
        found = _OBJ.findall(run(["gdbus", "call", "--system", "--dest", NM, "--object-path", NM_ROOT,
                                  "--method", f"{NM}.GetDeviceByIpIface", self.iface]))
        aps, count = [], 0
        if found:
            aps, count = camera_aps(run(["gdbus", "call", "--system", "--dest", NM, "--object-path", "/org/freedesktop",
                                         "--method", "org.freedesktop.DBus.ObjectManager.GetManagedObjects"]),
                                    found[0], self.ssid)
        self.nm_path = found[0] if found else None
        self.cam_aps = set(aps)
        self.sink.write("recorder", event="nm_device", path=self.nm_path, camera_aps=aps, ap_count=count)

    def write_usage(self, event="usage"):
        me = resource.getrusage(resource.RUSAGE_SELF)
        kids = resource.getrusage(resource.RUSAGE_CHILDREN)
        streams = {name: proc_cpu_s(p.pid) for name, p in self.procs.items() if p.poll() is None}
        self.sink.write("recorder", event=event, cpu_s=round(me.ru_utime + me.ru_stime, 3),
                        reaped_children_cpu_s=round(kids.ru_utime + kids.ru_stime, 3),
                        stream_cpu_s=streams, max_rss_kb=me.ru_maxrss, lines=dict(self.sink.counts),
                        dropped=dict(self.dropped), bytes=self.sink.size())

    def usage_loop(self):
        while not self.stop.wait(USAGE_EVERY_S):
            self.write_usage()

    def start(self):
        self.sink.write("recorder", event="start", pid=os.getpid(), kernel=os.uname().release)
        self.describe()
        self.stream("iw", ["iw", "event", "-T", "-f"], self.on_iw)
        self.stream("ip", ["ip", "-ts", "monitor", "link", "neigh", "address"], self.on_ip)
        self.stream("nm", ["gdbus", "monitor", "--system", "--dest", NM], self.on_nm)
        self.stream("bluez", ["gdbus", "monitor", "--system", "--dest", "org.bluez"], self.on_bluez)
        self.stream("journal", self.journal_argv, self.on_journal)
        self.spawn(self.sockets_loop, "sockets")
        self.spawn(self.netlink_loop, "netlink")
        self.spawn(self.usage_loop, "usage")

    def close(self):
        self.write_usage()
        self.stop.set()
        for p in self.procs.values():
            if p.poll() is None:
                p.terminate()
        for p in self.procs.values():
            try:
                p.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                p.kill()
        end = time.monotonic() + 8.0
        for t in self.threads:
            t.join(max(0.0, end - time.monotonic()))
        self.write_usage(event="stop")
        self.sink.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(APP / "logs/camera-recorder" / time.strftime('%Y-%m-%d')))
    ap.add_argument("--iface", default=CAM_IFACE)
    ap.add_argument("--seconds", type=float, default=0.0, help="stop after this long; 0 runs until stopped")
    args = ap.parse_args(argv)
    for value, why in ((args.iface, CAM_IFACE_UNSET), (CAM_IP, CAM_IP_UNSET)):
        if not value:
            ap.error(why)
    os.makedirs(args.out, exist_ok=True)
    sink = Sink(os.path.join(args.out, f"recorder-{time.strftime('%H%M%S')}.jsonl"))
    rec = Recorder(sink, args.iface)
    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    rec.start()
    print(f"recording to {sink.path}", flush=True)
    done.wait(args.seconds or None)
    rec.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
