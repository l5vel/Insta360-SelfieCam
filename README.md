# Insta360 SelfieCam

A kiosk selfie booth: an Insta360 X5 on the end of an xArm, driven from a web page. A
visitor opens the page, the arm moves to the selfie pose, the camera streams a de-warped
live preview, and one press captures a photo, brands it and emails it to them.

The camera is an access point at a fixed `192.168.42.1`, so the host joins the camera's
Wi-Fi to talk to it — on a **dedicated USB dongle**, so the normal network stays up. The
arm is shared with the rest of the robot stack through ArmBaseControl's lease.

---

## Pieces

| file | what it is |
|---|---|
| `main.py` | The FastAPI app — every endpoint below, the RTMP pull, frame processing and email. |
| `arm_control.py` | Arm poses and the ArmBaseControl lease integration. |
| `insta360_daemon.py` | Standalone SRT re-streamer. Not used by the web app; its SSID and profile name are hardcoded. |
| `index.html`, `static/` | The kiosk front end and its assets. |
| `insta360.service`, `install_service.sh` | systemd unit and its installer. |
| `tests/` | Lease release, API contract, capture retry. |

## HTTP API

| method | path | does |
|---|---|---|
| `GET` | `/` | Serves `index.html`, re-read per request. |
| `GET` | `/stream`, `/stream/equirec` | MJPEG of the cropped selfie view / full equirectangular frame. |
| `GET` | `/status` | Camera state for the front end to poll. |
| `POST` | `/connect` | Joins the camera's Wi-Fi and starts the RTMP pull. |
| `POST` | `/position-arm` | Moves the arm to the selfie pose. |
| `POST` | `/capture` | Fires `camera.takePicture` over OSC, downloads the full-res JPEG, brands it, writes it to `static/`. Returns `status`, `file_name`, `url`. |
| `POST` | `/email?email=…&filename=…` | Emails a captured photo. Both params required. |
| `POST` | `/disconnect` | Drops the camera link. `?home_arm=true` also sends the arm home. |

## Running it

```bash
./venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000   # directly
./install_service.sh                                     # or as a service
journalctl -u insta360 -f
```

There is no `requirements.txt` — the venv is the only record of the dependencies and is not
in git, so the environment cannot be rebuilt from the repo. Worth fixing with
`./venv/bin/pip freeze > requirements.txt`.

## Configuration

| env | meaning |
|---|---|
| `GMAIL_USERNAME` | Sending account. |
| `GMAIL_APP_PASSWORD` | Google **app password**. No default; `/email` fails without it. |
| `CAMERA_SSID`, `CAMERA_SSID_PATTERN` | Preferred SSID, and the fallback regex (`\.OSC$`). |
| `WIFI_INTERFACE`, `WIFI_PROFILE_NAME` | Dongle and NetworkManager profile. |
| `CAPTURE_POLL_ATTEMPTS`, `CAPTURE_DOWNLOAD_RETRIES` | Capture patience knobs. |

**Do not put the app password in `insta360.service`** — that file is committed. Use
`sudo systemctl edit insta360` and set it in the drop-in, which is not in git.

## The Wi-Fi hop

`find_camera_ssid()` picks the camera out of an `nmcli` scan by the `.OSC` suffix rather
than an exact name, because the X5 advertises as `<model> <serial>.OSC`, sometimes with an
`Insta360` prefix and sometimes serial-only. `sync_profile_ssid()` then repoints the saved
profile, so a rename — or a different X5 — still works without creating a second profile.

### Pinning the camera to its own radio

Two radios: `wlp5s0` (internal, normal network) and `wlx9cefd5f89420` (MT7612U dongle,
camera only). NetworkManager does not respect that split by default — it will autoconnect
any saved profile on either radio, and it did: a duplicate `level5_` profile grabbed the
dongle and became the host's default route and DNS.

```bash
# camera profile -> the dongle only, and never the default route
sudo nmcli connection modify Insta360 \
  connection.interface-name wlx9cefd5f89420 \
  802-11-wireless.mac-address 9C:EF:D5:F8:94:20 \
  connection.autoconnect yes connection.autoconnect-priority 100 \
  ipv4.never-default yes ipv6.never-default yes

# every other wifi profile -> the internal radio only
sudo nmcli connection modify <name> connection.interface-name wlp5s0
```

`ipv4.never-default` is the one that matters: without it the camera link installs a default
route, and if its metric wins the host tries to reach the internet through a 360 camera.

### Two traps

**Profiles are owned by netplan.** `/etc/NetworkManager/system-connections/` is empty here
— that is normal, not a missing file. Profiles live in `/etc/netplan/90-NM-<uuid>.yaml` and
are generated into `/run/NetworkManager/system-connections/`. `nmcli connection modify`
writes back through netplan and does persist.

**Never touch this adapter from GNOME Settings.** `gnome-control-center` deleted the
`Insta360` profile twice in one session. Each delete discards the pinning, and GNOME stores
the password in the *user keyring* rather than the profile — which a headless service can
never read, so auth then fails forever. Keep the PSK in the profile:

```bash
sudo nmcli connection modify Insta360 \
  802-11-wireless-security.psk '<camera password>' \
  802-11-wireless-security.psk-flags 0
```

## Sharing the arm with teleop

The arm is held under ArmBaseControl's lease. Two things follow, both deliberate:

- **The lease is released when the camera session ends.** `arm_release()` calls
  `disconnect()`, the only path reaching `ResourceLease.release()`. Without it the process
  held the lease from the first `/position-arm` until it died, and every teleop takeover
  ended in SIGKILL plus a systemd restart.
- **A takeover hook is registered at import**, so the cooperative wait succeeds. It does
  **not** home the arm — the requester adopts it where it stands, and homing would blow the
  15 s window and get the process killed anyway.

Registration is guarded, so an older ArmBaseControl without `on_takeover` cannot stop the
service booting.

## Troubleshooting

### `/connect` → 504 "SSID not found"

The scan genuinely did not see the camera. Check whether the AP is on air before touching
the host:

```bash
sudo nmcli device wifi rescan ifname wlx9cefd5f89420
sleep 12          # scans take ~11s on this adapter
nmcli -t -f SSID,SIGNAL,CHAN dev wifi list ifname wlx9cefd5f89420 --rescan no | grep -i OSC
```

Other APs listed but no camera means the fault is the camera: flat battery, Wi-Fi off,
asleep, or mid-reboot.

`nmcli device wifi rescan` **returns immediately** — it requests a scan, it does not wait
for one, and a full scan here takes ~11 s. Listing a second later just re-reads the previous
scan's cache. `/connect` uses 4 passes with a 12 s settle for exactly this reason; do not
shorten that sleep.

### `ip link` says `state DOWN` — the adapter is fine

`UP` in the flag list is the administrative state and it is set. `state DOWN`, `NO-CARRIER`
and `DORMANT` only mean *not currently associated* — the normal idle condition, including
while scanning. `ip link set … up` on an already-up interface returns 0 and changes nothing.
To actually test the radio, make it do work:

```bash
nmcli -t -f SSID dev wifi list ifname wlx9cefd5f89420 --rescan yes | wc -l   # 40+ = healthy
rfkill list                                                                 # both phys unblocked?
```

### `systemctl restart insta360` hangs

Uvicorn's graceful shutdown waits for in-flight requests rather than cancelling them, and
`/connect` can legitimately run ~48 s while the page polls `/status` twice a second. The
unit passes `--timeout-graceful-shutdown 10` to cap it.

### `/capture` → 502 "OSC network error during …"

Raised only from `httpx.RequestError` — a network failure, never a camera rejection. The
message names the phase (`takePicture`, `status poll`, `image download`).

Captures are ~13 MB: under 2 s on a healthy link, but while the camera reboots latency goes
from ~4 ms to over 1 s and the transfer overruns. The download retries 3× at a 90 s timeout;
nothing recovers a camera that vanishes mid-transfer.

```bash
curl -s http://192.168.42.1/osc/info | python3 -m json.tool          # uptime, firmware
curl -s -X POST http://192.168.42.1/osc/state | python3 -m json.tool # battery, card
ping -c5 -I wlx9cefd5f89420 192.168.42.1                             # ~4ms, not ~1000ms
```

**A low `uptime` on a camera that has been connected a while means it rebooted.** An X5 that
restarts repeatedly under sustained RTMP streaming is usually overheating; it presents as the
AP vanishing and returning, and no host-side change fixes it.

### Photo taken but no preview

```
"POST /capture HTTP/1.1" 200 OK
"GET /undefined HTTP/1.1" 404 Not Found
```

The capture succeeded and the file is in `static/`; the front end read a key the response
does not have, so `resultImage.src` became the string `undefined`. `/capture` returns
`status`, `file_name`, `url` — the page must read `data.url`, and `/email` wants `filename`,
not `image`. `tests/test_api_contract.py` fails if the two sides drift apart again.
`index.html` is re-read per request, so front-end fixes need only a browser refresh.

### Reading the logs

```bash
journalctl -u insta360 --since "30 min ago" | grep -v "GET /status"
journalctl -u NetworkManager --since "30 min ago" | grep -iE "insta360|connection-delete|auto-activating"
```

Filtering `/status` matters — the kiosk polls twice a second and drowns everything else.
`connection-delete` names the PID that removed a profile, which is how GNOME Settings was
caught.

## Tests

`tests/conftest.py` stubs the robot SDK, so no camera or arm is needed:

```bash
uv run --quiet --with pytest pytest -q
```

The capture-retry tests import `main` and skip unless the app's dependencies are present;
the contract and lease tests run anywhere.
