#!/usr/bin/env python3
"""Merges camera_recorder.py files into one timeline of the selfie camera's connect and disconnect sequences.

By default it shows each sequence's milestones: the app's requests and stages, scans with their
durations, when the camera was heard and its TSF zero, the join, the DHCP lease, link loss and its
cause, camera sockets, and errors. --all adds everything recorded. Each event goes on the boot
clock from its source's own stamp where it has one, and local time is printed beside seconds from
the first event shown.

    python3 scripts/camera_timeline.py FILE... [--since HH:MM[:SS]] [--until HH:MM[:SS]] [--all]
"""
from __future__ import annotations

import argparse
import json
import re
import time

from camera_recorder import IDLE_REUSE_MS, ssid_bytes

NM_STATES = {0: "unknown", 10: "unmanaged", 20: "unavailable", 30: "disconnected", 40: "prepare",
             50: "config", 60: "need-auth", 70: "ip-config", 80: "ip-check", 90: "secondaries",
             100: "activated", 110: "deactivating", 120: "failed"}
JOURNAL_LABELS = {"selfie": "app", "selfie-camera-adapter": "helper", "NetworkManager": "nm-log",
                  "wpa_supplicant": "wpa", "bluetooth": "bt-log", "kernel": "kernel", "sudo": "sudo"}
IW_KEY = ("disconnected", "connected to", "deauth", "disassoc", "beacon loss")
APP_KEY = ("requested by", " stage=", "camera link", "Wi-Fi heard", "on the air")
NM_KEY = ("Activation: starting connection", "Activation: successful", "Activation: failed", "link timed out",
          "-> failed (reason", "state changed new lease, address", "state change: activated -> deactivating")
WPA_KEY = ("Trying to authenticate", "SSID-TEMP-DISABLED", "ASSOC-REJECT", "AUTH-REJECT")
TROUBLE = re.compile(r"(?i)error|fail|timed? ?out")
BEACON_GAP_S = 0.6
SIGNAL_STEP_DB = 6
HEARTBEAT_S = 30.0
EPOCH_MATCH_MS = 20.0
HEARD_WITHIN_MS = 15000
HELPER_WAIT_S = 35
UPPER_BAND_MHZ = 5745
WIDTH = 170
_IW = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.(\d{6})\]: (.*)$")
_IP = re.compile(r"^\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)\.(\d{6})\] (.*)$")
_OBJ = re.compile(r"objectpath '([^']+)'")
_FRAME = re.compile(r" \[frame:[^\]]*\]?")
_STATE = re.compile(r"Device\.StateChanged \(uint32 (\d+), uint32 (\d+), uint32 (\d+)\)")
_FREQS = re.compile(r"(scan (?:finished|aborted)(?: after [\d.]+ s)?:)((?: \d+)+)")
_FREQ = re.compile(r"'Frequency': <uint32 (\d+)>")
_ADVERTS = re.compile(r"'ActiveInstances': <byte (0x[0-9a-f]+)>")
_OWNER = re.compile(r'users:\(\("([^"]+)",pid=(\d+)')
_TCPINFO = re.compile(r"\b(lastsnd|lastrcv|lastack|retrans|unacked|rtt):(\S+)")
_PREFIXES = ("org.freedesktop.NetworkManager.", "org.freedesktop.DBus.Properties.",
             "org.freedesktop.DBus.ObjectManager.", "org.freedesktop.DBus.", "org.bluez.")


def wall_ns(stamp: str, micros: str, fmt: str) -> int:
    return int(time.mktime(time.strptime(stamp, fmt))) * 10**9 + int(micros) * 1000


def at_wall(rec, ns: int) -> int:
    """Boot-clock time of a realtime stamp, through the record's own clock triplet."""
    return rec["boot"] - (rec["rt"] - ns)


def at_mono(rec, ns: int) -> int:
    return rec["boot"] - (rec["mono"] - ns)


def clock(wall: int) -> str:
    return time.strftime("%H:%M:%S", time.localtime(wall // 10**9)) + f".{wall // 10**6 % 1000:03d}"


def tsf_zeros(bss: dict) -> list:
    """[(frame, boot ns)]: the TSF zero from pairing last-seen with the latest beacon, and with the latest probe response."""
    heard = bss.get("last_seen_boot_ns")
    if heard is None:
        return []
    zeros = []
    if bss.get("beacon_tsf_us") is not None:
        zeros.append(("beacon", heard - bss["beacon_tsf_us"] * 1000))
    if bss.get("tsf_us") is not None and bss.get("tsf_us") != bss.get("beacon_tsf_us"):
        zeros.append(("probe response" if bss.get("presp") else "beacon", heard - bss["tsf_us"] * 1000))
    return zeros


def short(text: str) -> str:
    for prefix in _PREFIXES:
        text = text.replace(prefix, "")
    return text


def parse_ss(text: str) -> list:
    """Camera sockets from `ss -tinpH` text, the form recordings made before 15:40 on 2026-09-29 hold."""
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():
            parts = line.split()
            if len(parts) >= 5:
                owner = _OWNER.search(line)
                out.append({"local": parts[3], "peer": parts[4], "state": parts[0],
                            "owner": f"{owner[1]} pid {owner[2]}" if owner else ""})
        elif out:
            info = dict(_TCPINFO.findall(line))
            for name in ("lastsnd", "lastrcv", "lastack"):
                if info.get(name, "").isdigit():
                    out[-1][f"{name}_ms"] = int(info[name])
            if "retrans" in info:
                out[-1]["retrans"] = info["retrans"]
    return out


class Timeline:
    def __init__(self, show_all=False, bt_name=None):
        self.all = show_all
        self.events = []
        self.camera = {}
        self.nm_path = None
        self.cam_aps = set()
        self.new_aps = {}
        self.bt_name = bt_name
        self.bt_paths = set()
        self.adverts = None
        self.sockets = {}
        self.scan_began = None
        self.helper_cmds = []
        self.bg_outcome = None
        self.upper_missing = None
        self.cam_in_cache = None
        self.heard_shown = None
        self.prev_zeros = []
        self.prev_heard = None
        self.epoch = None
        self.station = None
        self.reset_station()

    def reset_station(self):
        self.beacon = None
        self.gap_shown = False
        self.loss = self.failed = self.signal = self.beat = None

    def add(self, rec, boot, src, text, key=False):
        if not self.all and len(text) > WIDTH:
            text = text[:WIDTH - 3] + "..."
        self.events.append((boot, boot + (rec["rt"] - rec["boot"]), src, text, key))

    def feed(self, rec):
        handler = getattr(self, "on_" + rec.get("src", ""), None)
        if handler:
            handler(rec)

    # ── recorder context ──────────────────────────────────────────────────
    def on_recorder(self, rec):
        event = rec.get("event")
        if event == "camera":
            self.camera = rec
            self.bt_name = self.bt_name or (rec.get("ssid") or "").removesuffix(".OSC") or None
            self.add(rec, rec["boot"], "recorder", f"camera adapter {rec.get('iface')} ({rec.get('operstate')}), "
                                                   f"profile SSID {rec.get('ssid')!r} BSSID {rec.get('bssid')}", True)
        elif event == "nm_device":
            self.nm_path = rec.get("path")
            self.cam_aps = set(rec.get("camera_aps") or [])
            self.add(rec, rec["boot"], "recorder", f"NetworkManager device {self.nm_path}")
        elif event in ("usage", "stop"):
            self.add(rec, rec["boot"], "recorder",
                     f"{event}: cpu {rec.get('cpu_s')} s, reaped children {rec.get('reaped_children_cpu_s')} s, "
                     f"streams {rec.get('stream_cpu_s')}, dropped {rec.get('dropped')}", event == "stop")
        else:
            detail = " ".join(f"{k}={v}" for k, v in rec.items() if k not in ("rt", "mono", "boot", "src", "event"))
            self.add(rec, rec["boot"], "recorder", f"{event} {detail}".strip(), True)

    # ── kernel and NetworkManager events ──────────────────────────────────
    def on_iw(self, rec):
        m = _IW.match(rec.get("line", ""))
        if not m:
            return
        boot, text = at_wall(rec, wall_ns(m[1], m[2], "%Y-%m-%d %H:%M:%S")), m[3]
        iface = self.camera.get("iface")
        if not (iface and text.startswith(iface)):
            self.add(rec, boot, "iw-other", text, any(k in text for k in IW_KEY))
            return
        text = text.split("): ", 1)[-1]
        if text.startswith("scan started"):
            self.scan_began = boot
            self.add(rec, boot, "iw", text)
            return
        key = any(k in text for k in IW_KEY)
        if text.startswith(("scan finished", "scan aborted")):
            if self.scan_began is not None:
                text = text.replace(":", f" after {(boot - self.scan_began) / 1e9:.1f} s:", 1)
            if self.helper_started_it():
                text, key = "helper " + text, True
            else:
                outcome = "aborted" if text.startswith("scan aborted") else "finished"
                key = outcome != self.bg_outcome
                self.bg_outcome = outcome
                text = "background " + text
            self.scan_began = None
        if not self.all:
            text = _FRAME.sub("", _FREQS.sub(lambda f: f"{f[1]} {len(f[2].split())} channels", text))
        self.add(rec, boot, "iw", text, key)

    def on_ip(self, rec):
        m = _IP.match(rec.get("line", ""))
        boot = at_wall(rec, wall_ns(m[1], m[2], "%Y-%m-%dT%H:%M:%S")) if m else rec["boot"]
        text = (m[3] if m else rec.get("line", "")).strip()
        self.add(rec, boot, "ip", text, " inet " in f" {text} ")

    def on_nm(self, rec):
        path, _, rest = rec.get("line", "").partition(": ")
        if not rest or not path.startswith("/"):
            return
        objects = _OBJ.findall(rest)
        if "InterfacesAdded" in rest and objects and ssid_bytes(rest) == self.camera.get("ssid"):
            freq = _FREQ.search(rest)
            self.new_aps[objects[0]] = f"{freq[1]} MHz" if freq else "an unknown frequency"
            return
        key = False
        if path == self.nm_path and ("AccessPointAdded" in rest or "AccessPointRemoved" in rest):
            ap = objects[0] if objects else None
            if "AccessPointAdded" in rest and ap in self.new_aps:
                self.cam_aps.add(ap)
                text, key = f"NetworkManager lists the camera AP on {self.new_aps.pop(ap)}", True
            elif "AccessPointRemoved" in rest and ap in self.cam_aps:
                self.cam_aps.discard(ap)
                text, key = "NetworkManager dropped the camera AP from its list", True
            else:
                text = "device " + short(rest)
        elif path == self.nm_path:
            m = _STATE.search(rest)
            text = (f"device {NM_STATES.get(int(m[1]), m[1])} (from {NM_STATES.get(int(m[2]), m[2])}, reason {m[3]})"
                    if m else "device " + short(rest))
        elif path in self.cam_aps:
            text = "camera AP " + short(rest)
        else:
            text = f"{path.replace('/org/freedesktop/NetworkManager', '') or '/'}: {short(rest)}"
        self.add(rec, rec["boot"], "nm", text, key)

    def on_bluez(self, rec):
        path, _, rest = rec.get("line", "").partition(": ")
        if not rest or not path.startswith("/"):
            return
        objects = _OBJ.findall(rest)
        if self.bt_name and self.bt_name in rest:
            self.bt_paths.update(objects or [path])
        adverts = _ADVERTS.search(rest)
        if adverts:
            count = int(adverts[1], 16)
            text, key = f"Bluetooth advertising {'on' if count else 'off'} ({count} active)", (count > 0) != self.adverts
            self.adverts = count > 0
        elif path in self.bt_paths or any(o in self.bt_paths for o in objects):
            text, key = "camera: " + short(rest), True
        else:
            text, key = f"{path.rsplit('/', 1)[-1]}: {short(rest)}", False
        self.add(rec, rec["boot"], "bluez", text, key)

    def on_journal(self, rec):
        mono = rec.get("__MONOTONIC_TIMESTAMP")
        boot = at_mono(rec, int(mono) * 1000) if mono else rec["boot"]
        ident = rec.get("SYSLOG_IDENTIFIER")
        name = (ident if ident in ("sudo", "kernel") else (rec.get("_SYSTEMD_UNIT") or ident or "journal")).removesuffix(".service")
        label = JOURNAL_LABELS.get(name, name[:9])
        msg = str(rec.get("MESSAGE"))
        if label == "sudo" and "selfie-camera-control" in msg and (" scan" in msg or " sweep" in msg):
            self.helper_cmds.append(boot)
        camera_related = any(k in msg for k in (self.camera.get("iface") or "wlx", "mt76", "cfg80211",
                                                f"usb {self.camera.get('usb_port') or '1-6'}:"))
        if label == "kernel" and not camera_related and not self.all:
            return
        if label == "app":
            key = msg.startswith(("ERROR", "WARNING")) or any(k in msg for k in APP_KEY)
        elif label == "nm-log":
            key = any(k in msg for k in NM_KEY) and "acd pending" not in msg
        elif label == "wpa":
            key = any(k in msg for k in WPA_KEY)
        elif label in ("kernel", "bt-log", "helper"):
            key = label == "helper" or bool(TROUBLE.search(msg))
        else:
            key = False
        self.add(rec, boot, label, msg.replace("INFO:     ", ""), key)

    # ── association, beacons, the scan cache, TCP ─────────────────────────
    def on_station(self, rec):
        now = rec["boot"]
        if "error" in rec:
            self.add(rec, now, "station", rec["error"], True)
            self.station = None
            self.reset_station()
            return
        stations = rec.get("stations") or []
        s = stations[0] if stations else {}
        if "events" not in rec:
            self.old_station(rec, now, stations[0] if stations else None)
            return
        for event in rec["events"]:
            if event == "joined":
                self.add(rec, now, "station", f"station entry for {s.get('bssid')} (authenticating)", True)
            elif event == "associated":
                self.add(rec, s.get("assoc_at_boot_ns") or now, "station",
                         f"associated with {s.get('bssid')} (kernel association time)", True)
            elif event == "left":
                self.add(rec, now, "station", "no longer associated", True)
            elif event == "beacon stall":
                self.add(rec, now, "station", f"no beacon counted for {rec.get('stall_s', BEACON_GAP_S):.1f} s "
                                              f"(count stuck at {s.get('beacon_rx')})", True)
            elif event == "beacons resumed":
                self.add(rec, now, "station", "beacons counted again", True)
            elif event == "beacon loss":
                self.add(rec, now, "station", f"kernel beacon-loss count now {s.get('beacon_loss')}", True)
            else:
                self.add(rec, now, "station", f"{event}: signal {s.get('signal_avg_dbm')} dBm, beacons {s.get('beacon_rx')}, "
                                              f"tx retries {s.get('tx_retries')}, tx failed {s.get('tx_failed')}")

    def old_station(self, rec, now, s):
        """Recordings made before 15:40 on 2026-09-29 hold every 5 Hz sample; find the same moments in them."""
        if bool(s) != bool(self.station):
            if s:
                self.add(rec, now, "station", f"station entry for {s.get('bssid')} (authenticating)", True)
            else:
                self.add(rec, now, "station", "no longer associated", True)
            self.reset_station()
        if s and s.get("assoc_at_boot_ns") and not (self.station or {}).get("assoc_at_boot_ns"):
            self.add(rec, s["assoc_at_boot_ns"], "station", f"associated with {s.get('bssid')} (kernel association time)", True)
        self.station = s
        if not s:
            return
        rx, sig = s.get("beacon_rx"), s.get("signal_avg_dbm", s.get("signal_dbm"))
        if rx is not None:
            if self.beacon is None or rx != self.beacon[0]:
                if self.gap_shown:
                    self.add(rec, now, "station", "beacons counted again", True)
                self.beacon, self.gap_shown = (rx, now), False
            elif not self.gap_shown and now - self.beacon[1] >= BEACON_GAP_S * 1e9:
                self.add(rec, now, "station", f"no beacon counted since {clock(self.beacon[1] + rec['rt'] - rec['boot'])} "
                                              f"(count stuck at {rx})", True)
                self.gap_shown = True
        for key, attr in (("beacon_loss", "loss"), ("tx_failed", "failed")):
            value, old = s.get(key), getattr(self, attr)
            if value is not None and old is not None and value > old:
                self.add(rec, now, "station", f"{key.replace('_', ' ')} {old} -> {value}", key == "beacon_loss")
            setattr(self, attr, value)
        if sig is not None and (self.signal is None or abs(sig - self.signal) >= SIGNAL_STEP_DB):
            self.add(rec, now, "station", f"signal {sig} dBm")
            self.signal = sig
        if self.beat is None or now - self.beat[0] >= HEARTBEAT_S * 1e9:
            if self.beat is not None:
                dt = (now - self.beat[0]) / 1e9
                self.add(rec, now, "station", f"last {dt:.0f} s: {((rx or 0) - self.beat[1]) / dt:.1f} beacons/s, "
                                              f"signal {sig} dBm, tx retries +{(s.get('tx_retries') or 0) - self.beat[2]}, "
                                              f"tx failed +{(s.get('tx_failed') or 0) - self.beat[3]}")
            self.beat = (now, rx or 0, s.get("tx_retries") or 0, s.get("tx_failed") or 0)

    def helper_started_it(self):
        """Whether the scan now ending is the first to start after a helper command, by the command's own stamp."""
        start = self.scan_began
        if start is None:
            return False
        self.helper_cmds = [t for t in self.helper_cmds if start - t <= HELPER_WAIT_S * 1e9]
        mine = next((t for t in self.helper_cmds if t <= start), None)
        if mine is None:
            return False
        self.helper_cmds.remove(mine)
        return True

    def is_camera(self, bss):
        bssid, ssid = self.camera.get("bssid"), self.camera.get("ssid")
        return bss.get("status") == 1 or (bssid and bss.get("bssid") == bssid) or (ssid and bss.get("ssid") == ssid)

    def on_scan(self, rec):
        trigger = (rec.get("trigger") or "").split(":")[0]
        off = rec["rt"] - rec["boot"]
        bss = rec.get("bss") or []
        if trigger in ("scan finished", "scan aborted"):
            freqs = {b["freq"] for b in bss if (b.get("seen_ms_ago") or 0) <= HEARD_WITHIN_MS and b.get("freq")}
            missing = not freqs or max(freqs) < UPPER_BAND_MHZ
            self.add(rec, rec["boot"], "scan", f"cache after {trigger}: {len(freqs)} channels heard in the last "
                                               f"{HEARD_WITHIN_MS // 1000} s" + (f", highest {max(freqs)} MHz" if freqs else "")
                                               + (f", nothing at {UPPER_BAND_MHZ} MHz or above" if missing else ""),
                     missing != self.upper_missing)
            self.upper_missing = missing
        cams = [b for b in bss if self.is_camera(b)]
        if not cams:
            if trigger != "joined":
                self.add(rec, rec["boot"], "scan", f"camera AP not in the adapter's cache (read after {trigger})",
                         self.cam_in_cache is not False)
                self.cam_in_cache = False
            return
        first_sighting, self.cam_in_cache = self.cam_in_cache is not True, True
        b = cams[0]
        heard, zeros = b.get("last_seen_boot_ns"), tsf_zeros(b)
        if heard is None or not zeros:
            return
        tol = EPOCH_MATCH_MS * 1e6
        new_frame = self.prev_heard is not None and abs(heard - self.prev_heard) > tol
        agreed = [z for _, z in zeros if new_frame and any(abs(z - p) <= tol for p in self.prev_zeros)]
        confirmed = agreed[0] if len(agreed) == 1 or (agreed and max(agreed) - min(agreed) <= tol) else None
        if confirmed is not None and (self.epoch is None or abs(confirmed - self.epoch) > tol):
            before = f"; the previous TSF zero was {clock(self.epoch + off)}" if self.epoch is not None else ""
            self.add(rec, confirmed, "scan", f"camera TSF zero, likely its Wi-Fi start, confirmed by two cache "
                                             f"reads{before}", True)
            self.epoch = confirmed
        new_session = self.epoch is None or all(abs(z - self.epoch) > tol for _, z in zeros)
        if (trigger != "joined" and heard != self.heard_shown) or self.all:
            open_zero = "" if not new_session else "; TSF zero at " + " or ".join(
                f"{clock(z + off)} ({frame})" for frame, z in zeros) + ", unconfirmed"
            key = trigger not in ("scan finished", "scan aborted", "joined") or first_sighting or new_session
            self.add(rec, heard, "scan", f"camera AP last heard on {b.get('freq')} MHz at "
                                         f"{(b.get('signal_mbm') or 0) / 100:.0f} dBm{open_zero} (read after {trigger})", key)
            self.heard_shown = heard
        self.prev_zeros, self.prev_heard = [z for _, z in zeros], heard

    def on_sockets(self, rec):
        now = rec["boot"]
        if "error" in rec:
            self.add(rec, now, "tcp", f"socket read failed: {rec['error']}", True)
            return
        entries = rec["sockets"] if "sockets" in rec else parse_ss(rec.get("value") or "")
        socks = {(s["local"], s["peer"]): s for s in entries if s["state"] != "TIME-WAIT"}
        for key, s in socks.items():
            old = self.sockets.get(key)
            label = f"{key[0]} -> {key[1]}"
            owner = f" ({s['owner']})" if s.get("owner") else ""
            if old is None or old["state"] != s["state"]:
                self.add(rec, now, "tcp", f"{label} {s['state']}{owner}", True)
                continue
            was, sent = old.get("lastsnd_ms"), s.get("lastsnd_ms")
            if was is not None and sent is not None and was >= IDLE_REUSE_MS and sent < was:
                self.add(rec, now, "tcp", f"{label} {s['state']}: sent again after {was / 1000:.1f} s idle", True)
            elif s.get("retrans") != old.get("retrans"):
                first = str(old.get("retrans", "0/0")).endswith("/0")
                self.add(rec, now, "tcp", f"{label} {s['state']}: retransmits {s.get('retrans')}", first)
            elif self.all:
                self.add(rec, now, "tcp", f"{label} {s['state']} lastsnd {sent} ms lastrcv {s.get('lastrcv_ms')} ms")
        for key in self.sockets.keys() - socks.keys():
            self.add(rec, now, "tcp", f"{key[0]} -> {key[1]} gone", True)
        self.sockets = socks


def load(paths):
    recs = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    continue
    return sorted(recs, key=lambda r: r.get("boot", 0))


def window_ns(value, first_rt):
    """Local HH:MM[:SS[.fff]] on the recording's first day, as realtime ns."""
    day = time.strftime("%Y-%m-%d", time.localtime(first_rt // 10**9))
    whole, _, frac = value.partition(".")
    fmt = "%Y-%m-%d %H:%M:%S" if whole.count(":") == 2 else "%Y-%m-%d %H:%M"
    return int(time.mktime(time.strptime(f"{day} {whole}", fmt))) * 10**9 + int((frac + "000")[:3]) * 10**6


def render(events, since=None, until=None, show_all=False):
    """Lines in boot-clock order, each source's own order kept for equal stamps; key events only unless show_all."""
    events = sorted((e for e in events if (show_all or e[4]) and (since is None or e[1] >= since)
                     and (until is None or e[1] <= until)), key=lambda e: e[0])
    if not events:
        return []
    t0, out = events[0][0], []
    for boot, wall, src, text, _ in events:
        if not show_all and src == "app" and "requested by" in text:
            out.append("")
        out.append(f"{clock(wall)} {(boot - t0) / 1e9:+10.3f}  {src:<9} {text}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--all", action="store_true", help="every event recorded, untruncated")
    ap.add_argument("--bt-name", help="the camera's Bluetooth name; default the SSID without .OSC")
    args = ap.parse_args(argv)
    recs = load(args.files)
    if not recs:
        print("no records")
        return 1
    tl = Timeline(args.all, args.bt_name)
    for rec in recs:
        tl.feed(rec)
    first = recs[0]["rt"]
    since = window_ns(args.since, first) if args.since else None
    until = window_ns(args.until, first) if args.until else None
    for line in render(tl.events, since, until, args.all):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
