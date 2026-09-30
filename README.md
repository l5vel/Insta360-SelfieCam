# Selfie station

**Read [Need to know](#need-to-know) before you use this code.** It lists when the arm moves, including
with no press, what a phone press does to a robot session that holds the arm, and what the app exposes
on the network.

The selfie station poses the robot's arm with an Insta360 X5 camera, shows a live preview on a phone
page and takes the picture. This FastAPI app serves that page on port 8000. It drives the arm through
ArmBaseControl and reaches the camera over the camera's own Wi-Fi, through a USB Wi-Fi adapter kept for
the camera alone.

## Need to know

This section lists what can move the arm or change the robot's state when you may not expect it. Each
item says when it happens and what to do, and the linked sections hold the full explanations.

### Arm motion

- **Take Picture moves the arm at once.** The page posts `/position-arm?takeover=true`, and the arm sets
  out before the camera check and the 3 s countdown, so a camera that then fails leaves the arm out. On
  base03 the `arm.selfie` path turns J1 through 180 degrees at 30 degrees per second and ends behind the
  robot near the VLA cameras. Keep that whole swing clear while the page is open to visitors. See
  [Taking a picture](#taking-a-picture).
- **The app brings the arm home on its own.** Two minutes after the last POST from any client, with an
  arm this app sent out and no camera work running, the app disconnects the camera and retraces the path
  home. It then switches the arm to joint teaching mode, so the arm moves freely by hand until another
  program takes it. This happens with no press, so keep the swing clear for two minutes after the last
  use. See [Deploying and bringing the arm home](#deploying-and-bringing-the-arm-home).
- **Any open page can end a session and send the arm home.** The Disconnect button and each page's own
  2-minute timer post `/disconnect?home_arm=true`. That request cancels camera work another client
  started, takes the camera's Wi-Fi down for every client, and brings home an arm this app sent out. A
  second phone or a stale tab whose timer runs out after someone else connected ends that person's
  session this way, so keep one page open per session. See
  [Deploying and bringing the arm home](#deploying-and-bringing-the-arm-home).
- **An arm off the path goes home in straight joint moves.** When the arm matches no waypoint, the next
  Take Picture, Disconnect, idle shutoff or handover moves it to the controller's initial point and then
  to the path's home. The only guard checks the starting pose and stops with "Arm needs a manual reset"
  when the TCP x there is below `arm.home_caution_x_mm` or cannot be read. Bring an arm left off the path
  home from the dashboard or by hand, because a robot config without that key gets no check at all. See
  ["Arm needs a manual reset"](#arm-needs-a-manual-reset).
- **The gripper opens before the arm goes out.** With `arm.gripper.type` set in the robot config, the
  app opens the gripper at the path's home before the first waypoint, so anything it holds drops there.
  An object a robot session was still holding when a phone took the arm drops there too. A gripper that
  reports it did not open keeps the arm home, and the reason goes to the journal while the page shows its
  plain arm message.

### Stops

- **The page cannot stop the arm.** The app has no stop endpoint, and the page disables Disconnect for
  all of Take Picture, including a camera wake that can run for 200 s. Stop the arm with the robot's
  e-stop, whose latch refuses the app's moves while it is set (ArmBaseControl README, Need to know). In
  the code the idle shutoff retries every 2 minutes and moves the arm on its first try after the latch
  clears, so post `/release-arm` before you release an e-stop with the arm out.
- **The next request clears a controller fault and moves the arm.** Before every move the app clears the
  controller's error and warning codes, so a stop from a collision or kinematic fault lasts only until
  the next Take Picture, Disconnect or idle shutoff. After a fault, check the arm and the people near it
  at once, because the idle shutoff can move the arm about 2 minutes later.
- **Stopping or restarting the app leaves the arm out.** A stop or restart of `selfie.service` closes the
  camera and leaves the arm where it stands, which can be the selfie pose behind the robot. The restarted
  app has no record that it sent the arm out, so its idle shutoff leaves the arm there. Bring the arm home
  before a restart, with Disconnect on the page or from the dashboard.

### When a robot session holds the arm

- **A phone press ends the robot session that holds the arm.** Take Picture asks for the arm with
  takeover, so a running SBot session aborts its task, even mid-pick, lets go of the arm where it stands,
  and closes (SBot README, Need to know). The dashboard sees its session end, and its Connect relaunches
  the session, which is a short relaunch. Sending the phone's pose and return to a running robot session,
  so the lease stays put, is planned and not built. See [Sharing the arm](#sharing-the-arm).
- **A press can force-stop an owner that does not let go.** ArmBaseControl force-stops an owner whose
  lease heartbeat has gone stale. It does the same to an owner that still holds the arm after 15 s, when
  that owner is one of the robot's own launchers or runs outside a managed systemd service. Force-stopping
  sends SIGTERM and then SIGKILL, so nothing safes the arm first (ArmBaseControl README, Need to know). See
  [Sharing the arm](#sharing-the-arm).
- **Another program's launch sends the arm home.** While this app has the arm out, any launcher that asks
  for the lease, such as the dashboard's Take over, makes the app retrace the path home and let go. When
  the dashboard's "Arm at startup" is "Leave it where it stands", the dashboard first posts
  `/release-arm`, and nothing moves (SBotDashboard README, Need to know). The arm then stays where it is,
  possibly at the selfie pose, and the app's idle shutoff no longer brings it home. See
  [Sharing the arm](#sharing-the-arm).

### Hardware and system changes

- **The camera adapter helper changes the system as root.** A Connect, or a Take Picture that finds the
  camera silent, runs the helper's `scan` through sudo, and its `reset` when the adapter fails its check.
  A reset unloads and reloads the `mt76x2u` driver, which drops every adapter that driver serves, and the
  boot unit `selfie-camera-adapter.service` resets a broken adapter at startup. The installer and each
  boot add an unreachable route for the camera's whole /24, so keep other networks the robot uses off
  that subnet. See [The adapter helper](#the-adapter-helper).
- **Disconnect takes down whichever profile `station.toml` names.** Every Disconnect and idle shutoff
  runs `nmcli connection down` on the `[camera] nm_profile_uuid` profile without naming an interface, and
  nothing checks that the profile belongs to the camera's adapter. A UUID copied from the robot's own
  Wi-Fi profile would drop the robot's network on the next Disconnect, so confirm the UUID names the
  camera's profile. See [The camera's network profile](#the-cameras-network-profile).

### Tests and scripts

- **Arm tests stay off the real arm only by convention.** Each test in `tests/test_arm_control.py`
  installs a fake handler before it calls the app, and nothing else stops a test from building a real
  `SelfieArmHandler`. A real handler would take the arm lease when it is free and connect to the arm at
  172.16.0.13, so keep new arm tests on the fake. The camera tests take a fake helper, scan lock and
  station values from `tests/sandbox.py`, and each test mocks its own `nmcli`, `bluetoothctl` and socket
  calls. See [Tests](#tests).

### Network, data and secrets

- **Anyone who reaches port 8000 can drive the station.** `selfie.service` serves the app on 0.0.0.0:8000
  with no authentication and CORS open to every origin. Any device on base03's networks, or a web page
  open on one, can post `/position-arm?takeover=true`, `/home-arm`, `/disconnect?home_arm=true` or
  `/release-arm`, and can view `/stream`, `/stream/equirec` and the last photo at `/static/latest.jpg`.
  Run the station only on a network limited to people who may move the arm and see its pictures.
- **Photos and video can leave the robot.** `/email` signs in to the Gmail account in `station.toml` with
  the app password from `GMAIL_APP_PASSWORD` and sends `static/latest.jpg` to any address a client names.
  Every capture overwrites that file, so a second picture taken before the first visitor presses Send
  goes to the first visitor. `POST /connect?client_ip=<IPv4>` pushes the camera's video over SRT to port
  7003 at that address, and later reconnects keep sending there.

## Setup

### Insta360 access

Apply for access to Insta360's SDK at https://www.insta360.com/sdk/apply. The app itself controls the
camera through the camera's OSC HTTP API, and it takes the live preview through the client in the
community `insta360` package.

### Installing

The app drives the arm through its own ArmBaseControl checkout in `third_party/ArmBaseControl`, which
git ignores. `pyproject.toml` installs it from there as an editable dependency with its `sparkflex` CAN
stack. Check out the commit SBot pins, so the station and SBot's supervisor run the same arm code, and
then install everything recorded in `uv.lock`:

```sh
git clone git@github.com:l5vel/ArmBaseControl.git third_party/ArmBaseControl
git -C third_party/ArmBaseControl checkout <commit>   # git -C <SBot> ls-tree HEAD third_party/ArmBaseControl
uv sync --locked
```

ArmBaseControl picks its config file by hostname, and the arm's selfie path is `arm.selfie` in that file.
Keep the `anker-solix-api` metadata block in `pyproject.toml`, because ArmBaseControl resolves here only
with it.

The project pins Python 3.11, and the shell's `python` may be another version, so run project commands
through `uv run` or `.venv/bin/python`. To start the app by hand, run
`uv run uvicorn main:app --host 0.0.0.0 --port 8000` in this directory. SRT output starts only when a
client supplies a destination, and it needs the system's `ffmpeg` with `libx264` and SRT support. The
page itself uses MJPEG.

The app writes each photo to `static/latest.jpg`, so git ignores `static/`. Copy in the two images the
station shows by hand: `static/logo.png`, an RGBA PNG the app puts on each photo's border when it is
there, and `static/utulsaIRA.jpg` in the page's header.

### This station's values

`station.toml` holds what belongs to one station, and git ignores it. It names the camera adapter's
interface, the camera profile's UUID, the camera's serial and address, and the Gmail account that sends
photos. Copy `station.example.toml` to `station.toml` and fill it in. The serial is the six characters
after "X5 " in the camera's Bluetooth name, and the app builds both the wake beacon and that name from it.

While a camera value is missing, the app still starts and the arm still works. Connect and Resume stop
with the station user's usual line and name the missing value in the detail. Take Picture stops the same
way once the arm has already gone out to the selfie pose. Disconnect still brings the arm home and says
it left the Wi-Fi alone. Email needs `[email] sender` and `GMAIL_APP_PASSWORD`, and without either
`/email` answers 503 and names what is missing.

### The camera's network profile

NetworkManager holds one profile for the camera, bound to the camera adapter and pinned to the camera's
BSSID. The camera's SSID is its model and serial followed by `.OSC`. Leave the profile's channel unset,
since the camera picks a channel each time its Wi-Fi starts. Autoconnect stays off, so NetworkManager
joins the camera only when the app asks. The UUID and the adapter below are the ones in `station.toml`.

```sh
nmcli connection modify uuid <camera-profile-uuid> \
  connection.interface-name <camera-adapter> connection.autoconnect no \
  802-11-wireless.band a 802-11-wireless.channel "" \
  802-11-wireless.powersave 2 ipv4.never-default yes ipv4.ignore-auto-dns yes \
  ipv4.ignore-auto-routes yes ipv6.method disabled
```

### The adapter helper

`scripts/selfie-camera-control` owns the camera's USB Wi-Fi adapter, a MediaTek MT7612U on base03's USB
port 1-6, and nothing else. Install it, its sudo rule and its boot check with
`sudo bash scripts/install-camera-helper.sh`. Until it is installed, Connect stops and names that command
in the detail. The sudo rule lets base3 run `reset`, `scan` and `sweep` and nothing more. The installer
also runs `isolate` once and sets the camera profile to take no default route, DNS server or DHCP route
from the camera.

The helper in this repository names no adapter and refuses every command. The installer reads the
adapter's interface and the camera's address from `station.toml`, and it refuses an interface that the
MT7612U did not create, so `wlp5s0` cannot be picked by mistake. It then installs a copy with both
values written in, so root never reads a user-owned file at run time. Run the installer again after
changing either value, because Connect stops and names the reinstall when the installed copy names a
different adapter from `station.toml`.

| command | runs as | what it does |
|---|---|---|
| `check` | anyone | ready when the adapter is on the USB bus, authorized, bound to `mt76x2u`, has created its interface, and NetworkManager manages it; otherwise prints the first layer that failed |
| `reset` | root | re-authorizes the adapter, reloads `mt76x2u`, port-resets the adapter if the reload brings no interface back, and hands it to NetworkManager |
| `scan` | root | clears any wpa_supplicant scan restriction, then kernel-scans channels 36 to 48 and 149 to 165 and prints each network heard in that scan; waits up to 30 s for another scan on the adapter to finish |
| `sweep` | root | the same over every channel the adapter supports |
| `isolate` | root | adds an unreachable route for the camera's subnet at metric 4000, so camera traffic has no way out but the camera's adapter |
| `boot` | root | at startup, runs `isolate`, then waits up to 30 s for the adapter and resets it up to twice |
| `configure STATION_TOML` | anyone | prints this helper with the adapter and the camera's /24 subnet from `station.toml` written in; the installer installs that copy |

`selfie-camera-adapter.service` runs `boot` once at startup, and `journalctl -u selfie-camera-adapter`
shows its result. Nothing waits for it, so SBot and teleop start on their own schedule. The app runs
`check` only when a Connect, or a Take Picture that finds the camera silent, has to join the camera's
Wi-Fi, and SBot and teleop never run it.

### Running as a service

`selfie.service` in `/etc/systemd/system` runs this directory's `.venv` on port 8000 as base3. After a
change to the code or the lock, run `uv sync --locked` here and then `sudo systemctl restart selfie`.
Email needs `GMAIL_APP_PASSWORD` in the service's environment, for example through an `EnvironmentFile=`
line that names a file kept at mode 600. Run one Uvicorn worker, because a process lease lets only one
process own the camera.

### Tests

Run the hardware-free tests with:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

No test runs the real adapter helper, takes the real scan lock or reads `station.toml`. Every test
module that loads `camera_connection` imports `tests/sandbox.py`, which points the helper and the lock at
a temporary directory and gives the app made-up station values, and a test fails any module that skips
it. These tests cover logic only and say nothing about hardware timing.

## How it works

### Taking a picture

Connect to Camera posts `/connect`, and Disconnect posts `/disconnect`. Both return HTTP 202 with an
`operation_id`, and the page polls `/status?operation_id=...` until `done` is true. The status carries
the stages reached, any error, and monotonic stage timings. The preview counts as ready at its first
decoded image. A capture is a synchronous request, queued behind any connect or disconnect in progress.

Connect reuses an active camera link. Otherwise it checks the adapter through the helper and resets it
once if it came up broken. It then scans the camera's channels with the helper and counts only networks
heard during that scan. When the camera is absent, the app wakes it over Bluetooth. It sends the wake
beacon for up to 60 s and then waits 20 s more, scanning every 3 s. The Bluetooth controller cannot
watch for the camera while it sends the beacon, so the app checks Bluetooth once the beacon ends. If the
camera advertises then, or its state cannot be read, the app sweeps every channel once before it gives
up. It then activates the profile and waits up to 25 s for the link. The whole connect has a 200 s
budget, and a preview error keeps the Wi-Fi up so the next Connect can retry the preview.

Take Picture first sends the arm to the selfie pose with `/position-arm?takeover=true`. It then asks for
`/connect?ensure=true`, which returns at once when the camera's API answers and leaves the preview
alone. When the camera stays silent, it runs the whole connect to the preview's current destination, so
a dashboard watching over SRT keeps its stream. Each stage shows in the page's status line and over the
live feed. The countdown starts once the feed is back, and a failed reconnect shows its plain line in
both places. NetworkManager joins the camera only when the app asks, so a camera whose Wi-Fi restarted
between pictures needs this fresh connect. A capture from any other client gets "Camera is unreachable.
Connect before capturing." when the camera does not answer.

A capture first checks that the camera's API answers, waiting up to 2 s so one lost packet cannot fail
it. It stops the preview stream on port 6666 while it runs. When the camera refuses the picture, the
error carries the camera's own reply, such as its OSC error code, so the log says why. Each capture
closes its HTTP connections to the camera when it ends, because a pooled connection can outlive the
link. One stayed established for 26 minutes through a link loss and a Disconnect, and a later capture
would have reused it and been reset.

Every step of Connect and Take Picture moves on when a live check passes, and its number is only how
long it waits before giving up with a message. The helper's scan must hear the camera, and the wake
beacon stops the moment a scan every 3 s does. NetworkManager must report the link activated, the
camera's API must answer a probe made every 0.5 s, and the preview must decode its first frame. A
capture ends when the camera reports the picture done, polled every 0.4 s, and the file has downloaded.
The arm moves block until the controller reports each one, and the pose guard reads the joint angles
before the shutter. A few waits are fixed by design. These are the 3 s countdown, pauses of 0.2 to
0.5 s between arm controller commands, the 0.5 s settle once the arm reaches its pose, and the 2-minute
idle shutoff. The longest waits come from the helper waiting out NetworkManager's own scans, as
"Scans on the camera adapter" describes.

These times were measured on base03 with the X5, and each rests on one or two runs.

| step | measured | runs |
|---|---|---|
| Connect with the camera awake and its Wi-Fi on, to preview ready | 21.5 s | 1 |
| Connect with the camera asleep, to preview ready | 38.8 s and 61.7 s | 2 |
| Take Picture reconnecting after the camera's Wi-Fi restarted | 26.5 s | 1 |
| Capture, from the shutter to the downloaded file | 6.1 s and 6.2 s | 2 |
| Pre-capture check finding a silent camera | 1 ms with the isolation route, 2 s without it | 1 each |
| Idle shutoff firing after the last use, checked every 5 s | 122 s | 1 |
| Idle shutoff bringing the arm home along the path | 12.1 s over three waypoint moves | 1 |

In the awake connect, the adapter joined 1.1 s after NetworkManager began activating the profile. The
DHCP lease came 0.4 s after the join, and the preview 1.5 s after the lease.

### Sharing the arm

The phone page takes the arm directly. Take Picture posts `/position-arm?takeover=true`, which asks
whoever holds the arm lease to release it and waits up to 15 s. An SBot session answers by aborting its
running task and letting a selfie hold go where the arm stands. It tells its dashboard that another
program took the arm, then closes within its 8 s teardown. That report is lost when the session's status
wire closes first. An owner whose lease heartbeat has gone stale is force-stopped at once. One that still
holds the arm after 15 s is force-stopped too when it is one of the robot's own launchers or runs outside
a managed systemd service. Force-stopping sends SIGTERM and then SIGKILL, so nothing safes the arm first.
An owner inside a managed service keeps the arm, the page shows its plain arm message, and the journal
names the owner.

The dashboard asks for the arm the same way. When it connects while this app holds the arm, its takeover
dialog names the selfie station and offers Take over and Cancel. Take over has this app bring the arm
home along the `arm.selfie` path, then disconnect and release the lease. From the selfie pose the retrace
took 12.1 s in its one timed run, when the idle shutoff made it. That leaves about 3 s of the 15 s the
dashboard's supervisor waits. An arm the retrace cannot place on the path is released where it stands,
and the journal logs why.

A dashboard set to leave the arm where it stands asks first with `POST /release-arm`. The app then
lets go of the arm without moving it and answers whether it held one. Its idle shutoff no longer
counts the arm as out, so nothing brings it home later, and the takeover that follows finds nothing
to retrace.

### Saving a frame

`POST /save-frame` writes the 360 frame `/stream/equirec` last sent, byte for byte, to
`logs/frames/frame-<date>-<time>.jpg` and answers with the path. It fires no shutter, pauses nothing
and applies no crop, border or logo, so it records exactly what a client such as the dashboard's
Operator view was shown. It refuses with 409 when no frame went out in the last 2 s.

### Deploying and bringing the arm home

Importing `arm_control` leaves the robot alone until a request needs the arm. Deployment starts from the
path's home and follows the joint waypoints in the robot config's `arm.selfie` block, which the SBot
supervisor's `selfie_pose` verb follows too. While another process holds the arm lease, such as an SBot
session that posed the arm for the dashboard, `/capture` leaves the pose to that owner. Return-home runs
only after this process has deployed the arm, and it retraces the path from a verified waypoint. An arm
found off the path goes home through the controller's own initial point, which the app reads from xArm
Studio's API. An arm already at that point goes straight to the path's home. Deployment does the same
before it sets out, so an arm that is home deploys even after a failed deployment. The arm's address
comes from `SELFIE_ARM_IP`, with base03's `172.16.0.13` as the default.

The app moves an arm off the path only while its TCP x is at least `arm.home_caution_x_mm` (50 mm on
base03). Below that line the arm sits near the VLA cameras, and the app stops with "Arm needs a manual
reset", giving the measured x. It stops the same way when it cannot read the position or the initial
point. A stop on the path mid-deployment still asks for a return home first, since the path is the safe
route back. Release enters xArm joint teaching mode only after the arm is home, then closes the
connection and releases the ownership lease.

Once a dashboard session has the arm, every way home it offers follows the selfie path's own waypoints.
Return arm on the Insta360 page and ARM HOME both retrace the path while the robot holds the arm out.
Settings' Go home from selfie starts from whichever waypoint the robot measures the arm at, works with or
without a hold, and refuses from anywhere else. ARM HOME does the same for an arm on the path with no
hold. The retrace starts only from a measured waypoint, because a straight move onto the wrong one can
hit the VLA cameras.

An arm off the path with its TCP x below `arm.home_caution_x_mm` sits behind the robot near those
cameras. base03 sets the line at 50 mm, and only the selfie pose and the basket dropoff, which is not
configured yet, take the arm behind it. From there ARM HOME asks the operator before a straight move, and
only Proceed with caution sends it. The Deck pad's RB+Y chord skips that question in classical and
teleop mode. In classical mode it refuses on the selfie path and during a selfie hold, and an arm left
out should still come home through the dashboard's buttons.

A new SBot session starts cold, and on base03 the cold start brings an arm on the selfie path home along
it. An arm off the path behind the line stays where it is, and the dashboard says why. The dashboard's
ways home and the cold start share ArmBaseControl's `arm/selfie_path.py`, while this app's own return
keeps its copy in `arm_control.py`. These checks have run only in tests and in SBot's verb probe so far.

The station also brings the arm home by itself. After a picture, or when a session stops partway, it
disconnects the camera and homes the arm once no one has used it for 2 minutes. The page's timer stops
when a phone locks or the tab closes, so the app keeps a clock of its own. Every POST to the app counts
as use, and the page posts `/activity` at most every 15 s while someone acts on it. Status polls leave
the clock alone. The app acts when its clock reaches 2 minutes with the arm out and no camera work
running. It disconnects the camera, retraces the selfie path home along its poses as Disconnect does,
and releases the arm. It logs a warning, and `/status` then reads "Camera disconnected by the 2-minute
idle shutoff."

When the phone wakes, or the page's own timer fires late, the page asks the app first. A station the app
already shut off returns to the Connect view with the app's message, and the page sends no second
disconnect. A Disconnect that finds the camera link already down finishes quietly, without
NetworkManager's "not an active connection" warning. An explicit Disconnect takes the camera's Wi-Fi link
down, while shutting the app down stops the preview, releases the process lease and leaves the link up.

### Logs

The app logs to the journal, and `journalctl -u selfie` reads it. Every connect, disconnect and capture
logs the client that asked for it and each stage it passes. A disconnect that cuts another client's
connect short logs a warning naming the sender, and the cancelled operation's message in `/status` names
it too.

While a connection is up, the app checks the link every 3 s. It logs one warning when the adapter leaves
the USB bus, the Wi-Fi goes down, the camera stops answering or the preview stalls, and one line when the
link recovers. `/status` carries the current reason in its `link` field, and the reason an operation
failed in its `detail` field. Successful polls of `/status` and the preview streams stay out of the
access log, because clients poll them twice a second.

A camera whose Wi-Fi goes off leaves a clear trail. In one recorded loss, the recorder flagged the
silence 0.65 s after the camera's last beacon, and the kernel reported beacon loss at 1.3 s. The adapter
disconnected itself for inactivity at 2.5 s, and the app logged the link loss at 2.0 s. NetworkManager
gave up about 17 s after the last beacon with `ssid-not-found`. The arm SDK can log `[Errno 32] Broken pipe` while
the arm is released, and the release still completes. That line appeared once in 14 logged releases.

### Scans on the camera adapter

Every scan request on the adapter, from the app, the helper or a test script, holds
`/run/lock/selfie-camera-scan.lock`, so none of them overlap. NetworkManager's own background scans
cannot take the lock, and the helper's scan waits for them to finish. NetworkManager scans the adapter
every 120 to 121 s while idle, and back to back for about three minutes after any disconnect, a user's or
a lost link's. Each of its scans asks for 39 channels across both bands, and once scans can finish, each
runs 21 to 22 s. A helper scan asked for in that window waits up to 30 s for the scan in progress. In
three connects the helper waited 7, 16 and 21 s before its first scan. In one of them the camera sat in
the adapter's scan cache for about 13 s before the app heard it.

When the helper's 30 s wait runs out, the connect ends with "The camera Wi-Fi was busy. Please try
again." for the station user, and the helper's reason goes in the detail. The app accepts these waits
and leaves the kernel's scan handling alone. A busy adapter ends the connect with that prompt, since the
two connects that fell back to NetworkManager's 39-channel scans took 77 s and 173 s, and the second
failed. A helper that cannot run for any other reason still falls back to them.

The app scans through the helper because NetworkManager's rescans proved unreliable on this adapter. A
rescan asked for soon after another scan came back without a new scan, and one asked for during
NetworkManager's own scan was answered by that older scan. A kernel scan waits for any scan in progress
and then runs its own.

### Camera traffic stays on the camera's adapter

While the camera link is up, its own route at NetworkManager's metric of 600 outranks the unreachable one
from `isolate`. While it is down, anything sent to the camera fails at once, where it would otherwise
leave through base03's own Wi-Fi, wlp5s0, and wait out its timeout. Once the helper is installed,
`ip route show type unreachable` lists the route, and `ip route get <camera-ip>` answers "No route to
host" while the camera is disconnected.

The wake beacon and the awake check are the one piece still shared with base03's own Wi-Fi. They use the
onboard Bluetooth on the MediaTek MT7922 chip that also carries wlp5s0, and a separate USB Bluetooth
adapter would move them off it.

### Waking the camera

The app sends the wake beacon through BlueZ's advertising interface with `bluetoothctl`, which needs no
root, and bluetoothd confirms the beacon on the air. `btmgmt` 5.72 hangs after every command, even
`--version`, so nothing here uses it. A sleeping camera stays silent on Bluetooth, but it listens for the
beacon.

Thirteen of the fourteen station connects that found the camera's Wi-Fi off woke it with its Wi-Fi. The
one that failed sent its beacon 2.0 min after the camera was last seen, and afterwards the camera was
no longer advertising. The table lists each wake by how long the camera had gone unseen before the
beacon, taken from the app's journal. The second column is when the app's scans first heard the
camera's Wi-Fi, counted from the start of the beacon.

| camera last seen awake | Wi-Fi heard |
|---|---|
| about 3.5 h before | 52 s |
| about 2 h before | 21 s |
| about 100 min before | 23 s |
| about 48 min before | 21 s |
| 16.0 min before | 51 s |
| 14.7 min before | 73 s |
| 10.8 min before | 42 s |
| 8.1 min before | 20 s |
| 7.6 min before | 61 s |
| 4.8 min before | 30 s |
| 3.1 min before | 28 s |
| 2.8 min before | 51 s |
| 2.3 min before | 30 s |
| 2.0 min before | not heard |

The camera's Wi-Fi starts sooner than the app hears it. In three wakes the recorder captured, it came
up 11.3 s and 12.5 s after the beacon started when the camera had slept for minutes or about 48 min,
and 41.2 s after it had slept about 3.5 h. The rest of each gap is scan timing. The first helper scan
that starts after the Wi-Fi does hears the camera, and in the two shorter wakes those scans also waited
behind NetworkManager's. The wake gives the camera 80 s, the 60 s beacon plus 20 s of scans, so a longer
sleep eats into that margin.

A beacon sent while the camera is awake with its Wi-Fi timed out does nothing, as one connect showed.
The failed wake in the table fits the same case, and "Known failures" covers it.

### The camera's own timers and settings

The camera turns its Wi-Fi off 2 minutes after its preview connection closes. That was measured twice to
the second, and two more gaps of the same length match it. A capture closes that connection, and the
Wi-Fi goes off even while the robot's adapter stays joined. One exception has no known cause yet: after
one capture the camera stopped beaconing about 13 s after the preview closed. Left alone, the camera falls asleep two to
four minutes after waking.

The camera's menu shows firmware 1.11.6 with MCU 1.2.5 on hardware 620. Auto Power Off is set to 3
minutes, Bluetooth Wakeup is on, and touch to wake the screen is off. The 3 minutes fit the camera
falling asleep two to four minutes after waking. The menu has no Wi-Fi mode setting such as Auto or
Always On, so the camera keeps its fixed Wi-Fi idle timeout. Over the camera's protocol, its Bluetooth
wake switch (`BT_WAKEUP_SW`) reads On and `STANDBY_DURATION` reads 0. That 0 matches no menu setting, so
the field is some other setting or its 0 means something the protocol leaves unstated.

The app assumes the camera's Wi-Fi is always on 5 GHz. The profile's band is `a` and the helper's scan
covers only 5 GHz channels, so a camera switched to 2.4 GHz would be neither found nor joined. The
camera's region is set to America, so it picks a channel from 36 to 48 or 149 to 165 each time its Wi-Fi
starts. The logs show 36, 48 and 149, and the one sweep covers any other channel.

### The camera's battery

While the preview runs, the app reads the camera's battery every 30 s from its OSC state, `POST
/osc/state`, which answers `state.batteryLevel` from 0 to 1. The X5 answered 0.82 in 0.01 s on its
first read, and its state carries no charging flag. `/status` carries the reading as `battery` with
`percent` and `age_s` while it is under 60 s old, and as `null` otherwise. Nothing is read while the
preview is stopped, because whether a request resets the camera's 2-minute Wi-Fi timer is untested. The
reading is forgotten once the link ends, so a disconnected camera never shows a number.

### Recording the camera link

Each log keeps its own clock and some steps leave no trace, so `scripts/camera_recorder.py` records what
base03 can observe of the camera's Wi-Fi. A failed connect or capture can then be timed afterwards from
evidence. The recorder only reads and listens, runs as base3 without root, and sends nothing to wlp5s0
or to the camera. `scripts/camera_timeline.py` turns one or more recordings into the connect, capture and
disconnect sequences, and `--all` adds everything recorded.

```
python3 scripts/camera_recorder.py                  # until Ctrl-C
python3 scripts/camera_recorder.py --seconds 600
python3 scripts/camera_timeline.py logs/camera-recorder/<date>/recorder-<time>.jsonl
python3 scripts/camera_timeline.py FILE --since HH:MM:SS --until HH:MM:SS --all
```

Each run writes one file under `logs/camera-recorder/<date>/`, with one JSON record per line. Every
record carries the realtime, monotonic and boottime clocks, read together as it is written. The recorder
polls fast and writes only the moments a sequence needs. Its sources are these:

- `iw event -T -f`, for scans with their channel lists, authentication, association, and disconnects
  with their reason codes.
- nl80211, read in process over a netlink socket. The camera adapter's station counters are polled five
  times a second. They are written when the adapter joins or leaves, when no beacon arrives for 0.6 s and
  when beacons resume, when a loss or failure counter rises, and every 10 s while joined. The scan cache
  is read after every scan, join, disconnect, beacon loss and beacon stall.
- `gdbus monitor` on NetworkManager, keeping the camera adapter, the camera's access point and the
  connection, and on BlueZ.
- `ip -ts monitor`, for the camera adapter's link, addresses and neighbours.
- `journalctl -f`, for the app, the adapter helper, NetworkManager, wpa_supplicant, bluetoothd, the
  kernel and sudo.
- sock_diag, the kernel's socket-diagnostics netlink, for TCP sockets to the camera. It is read twice a
  second while the adapter is joined and every 5 s otherwise. A socket is written when it opens, closes,
  changes state, retransmits, or sends after 10 s idle. Closed connections waiting out TIME-WAIT are
  left out, since the app's link check opens one every 3 s.

It counts and drops wlp5s0's signal reports, which `iw` and wpa_supplicant each print every 3 s, along
with neighbour and link changes on other interfaces and NetworkManager's reports about other devices. A
password, PSK or ANKER_* value in any log line, sudo command lines included, is replaced with
`[redacted]` before the line is written. The recorder reads only the camera adapter's scan cache, and
each read makes the kernel drop entries not heard for 30 s, as that adapter's next scan would anyway.

The timeline calls a scan the helper's when it is the first to start after a helper command, judged by
the command's own journal stamp. Any other scan on the adapter is a background scan, shown only when its
outcome changes.

The timeline places each event by its own source's stamp. Those are the receipt times of `iw` and `ip`,
journald's monotonic time, the kernel's association time, and the kernel's receive time for the camera's
last frame. It also estimates when the camera's Wi-Fi started from the camera's TSF, the counter each
beacon and probe response carries. The kernel keeps the TSF of the latest beacon and of the latest probe
response, but one last-seen time for both, so either pairing can be wrong. In the recordings so far, the
beacon pairing held within 1 ms while the adapter was joined, and the probe-response pairing held during
scans. Each time, the other pairing sat 1.3 s off. The timeline therefore prints both candidates until two cache reads
of different frames agree within 20 ms, and then reports that TSF zero as confirmed. In four recorded
sessions it fell just before the camera was first heard, which is consistent with its Wi-Fi start but
not proof. `iw ... scan dump` prints only the probe response's TSF, which was 48 s older than the latest
beacon in one reading.

A 24-minute run spanning two station sessions used 0.45% of one core and wrote 986 KB. A 150 s run with
the camera off used 0.26% of one core, over a third of it the start-up reads, and wrote 11 KiB. Each read of the kernel's TCP table walks its whole hash table and costs 2.5 to 3.8 ms here, which
is why sockets are polled slowly while nothing is joined. wlp5s0 stayed associated through every
recording, and its connected time rose by the full time between readings. One test fails if the recorder
ever issues a command or netlink request that is not a read, and all of them run with
`python3 -m unittest tests/test_camera_recorder.py`.

## Known failures

### "The camera isn't ready. Please try again in a minute, or ask for help."

The station user sees this line when a connect fails. The reason and the hand action it needs go in the
`detail` field of `/status` and in the log, and the dashboard's Insta360 panel shows that detail to the
operator. A camera that does not answer the wake beacon needs its power button. A wake that brings the
camera up without its Wi-Fi says so, and the fix is the camera's Wi-Fi switch. An adapter that a reset
cannot bring back needs to be unplugged and plugged in again. Until the helper is installed, the detail
names `sudo bash scripts/install-camera-helper.sh`. The whole wake path runs up to about two minutes,
and the page says so while it waits.

### "The camera Wi-Fi was busy. Please try again."

A NetworkManager scan held the camera adapter for the helper's whole 30 s wait. It happens most in the
three minutes after any disconnect, while NetworkManager scans back to back. Press Connect or Take
Picture again, and each press starts a fresh wait. If it keeps happening, look for an open GNOME Settings
Wi-Fi panel, the next entry.

### Scan requests collide about twice a minute

GNOME Settings' Wi-Fi panel, left open on base03's desktop, has both adapters scan every 15 to 30 s,
wlp5s0 on level5_ included. With the panel open for an hour, the camera adapter's scan requests collided
about twice a minute. With it closed, wlp5s0 went 100 s without a scan and no request collided. Close the
panel when you are done with it.

### NetworkManager's scans stop at channel 48 after a boot or an adapter reset

After a boot, a driver reload or an adapter reset, NetworkManager's scans on the camera adapter end as
aborted before they reach the upper 5 GHz channels. Ten such scans each ended after 10.4 to 11.7 s. The
adapter's scan cache then held networks only up to 5240 MHz (channel 48), while level5_ transmits on
5805 MHz and the kernel allows those channels.

The cause is in wpa_supplicant 2.10, as Ubuntu's noble-updates tree builds it. `src/drivers/driver_nl80211_scan.c`
gives each scan it requests a 10 s timeout and aborts the scan when that fires. The timeout rises to
30 s once the kernel reports a finished scan on that interface, and `driver_nl80211_event.c` sets that
flag only on new scan results. This adapter needs 21 to 22 s for all 39 channels, so every such scan is
cut off and the timeout stays at 10 s. The helper's 9-channel kernel scans finish in about 7 s, and
the first of them ends the loop. After the helper's first two scans following a boot, every later
background scan ran all 39 channels and finished. With the helper installed nothing more is needed, and
the app's fallback to NetworkManager rescans would share the limit.

### The adapter hears nothing above channel 64

The camera adapter can go deaf above channel 64, which hides a camera on channels 149 to 165. The scan
timeout above fits it. One such episode survived a driver reload and ended as soon as a two-channel `iw`
scan finished, and what started it is unknown. The helper's scans end it the same way, and
`sudo /usr/local/libexec/selfie-camera-control scan` runs one by hand.

### The camera shows "app disconnected" after a picture

A capture stops the preview stream on port 6666 while it runs, and in the recorded run the stream stayed
closed afterwards. That is likely the message the camera shows. It is expected, and Take Picture
reconnects when the next picture needs it.

### The camera's Wi-Fi is gone two minutes after a picture

The camera turns its Wi-Fi off 2 minutes after its preview connection closes, and a capture closes it. A
later Take Picture finds the camera silent and reconnects before its countdown. That took 26.5 s with the
camera awake and 38.8 to 61.7 s once it had fallen asleep. Keeping the Wi-Fi up between sessions has not
been tried.

### A wake beacon brings nothing back

A beacon sent while the camera is awake with its Wi-Fi timed out does nothing, as one sequence showed.
The camera falls asleep about two minutes later, and a beacon then wakes it with its Wi-Fi. A second
failure fits this case: a beacon 2.0 min after the Wi-Fi went off brought nothing back, and the next
Connect, 4.8 min after, woke the camera in 30 s. Switch the Wi-Fi on at the camera, or wait and press
Connect again. The dependable fix would switch the Wi-Fi on
remotely with the camera's open-Wi-Fi command (`PHONE_COMMAND_OPEN_CAMERA_WIFI`, code 33). The Insta360
app sends that over the camera's Bluetooth control link, which this app has yet to implement.

### "The camera woke over Bluetooth, but its Wi-Fi stayed off."

The beacon woke the camera, and no scan heard its Wi-Fi before the connect gave up. In both connects on
record the adapter most likely missed a camera that was on. One ran while the adapter was deaf above
channel 64, and in the other NetworkManager's full scans heard the camera on channel 36 only after the
connect had ended. A busy adapter ends the connect with the retry prompt, which closes that second path. Press Connect again. If the detail repeats, check the adapter with
`/usr/local/libexec/selfie-camera-control check`, then switch the Wi-Fi on at the camera.

### The phone shows "Loading live feed..." after Connect

A connect reaches "preview ready", but the phone keeps showing "Loading live feed..." until Take Picture
changes the screen. The cause is not confirmed yet. The page reveals the feed only when the preview
image fires its `load` event, and whether the phone's browser fires it for a live MJPEG stream is
untested. Frames reaching the phone have not been counted either. Take Picture still poses the arm and
takes the photo.

### A capture fails while the camera is in video mode

The camera refuses a picture in video mode, and the capture error carries its reply. The app leaves the
camera's mode as it finds it, so switch the camera to photo mode at the camera and press Take Picture
again.

### "Arm needs a manual reset"

The arm is off the selfie path with its TCP x below `arm.home_caution_x_mm`, or the app could not read
its position or the controller's initial point. The message gives the measured x. Bring the arm home
from the dashboard, where ARM HOME asks before a straight move and only Proceed with caution sends it, or
move it by hand.

### Take over fails because the app is stuck

A stuck app refuses the dashboard's Take over. `sudo systemctl restart selfie` frees the arm, and the next
connect needs no dialog.

### The dashboard reports a failed connect after Take over

An arm still moving out when the request arrives finishes that move before the retrace starts, so the
handover can overrun the supervisor's 15 s wait. The dashboard then reports the failed connect with the
robot's `[lease]` line. The app still finishes its retrace and releases the arm, so connecting again
finds it free.

## Known limitations

This arrangement suits a supervised demonstration, and it would be risky in a real deployment. Anyone who
can open the phone page can end an operator's session with one press, including in the middle of a pick.
A dashboard-launched session that has not released after 15 s is sent SIGTERM and then SIGKILL, so
nothing safes the arm first. The phone page and a dashboard can also take the arm from each other in
turn, and every exchange costs a session restart, plus a retrace when the arm is out. A deployed station
should be restructured so an operator approves each request. Another way is for the page to ask the SBot
session to run its `selfie_pose` verb while the session keeps the lease. The handover has run only in
tests against fakes, and its first live run should watch the retrace.

Several cases still need a run on hardware. They are repeated connects to an awake camera, a repeated
Connect while already ready, Disconnect during association, unplugging and replugging the camera adapter,
and an unavailable SRT receiver. The longest sleep measured before a wake is about 3.5 h,
and its Wi-Fi took 41 s of the 80 s the wake allows. A night's sleep is still unmeasured; the first
Connect after one will show whether it fits, and the journal records it.
