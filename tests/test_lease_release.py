"""The arm lease must come back without anyone being SIGKILLed.

`arm_release()` was a misnomer: it cleared faults and switched to mode 1, but never called
`XArmHandler.disconnect()` — the only path that reaches `ResourceLease.release()`. So this
process held /dev/shm/sbot.lease from the first /position-arm until it died, showing up to
the dashboard as `uvicorn@l5vel-base03 (mode=idle)`.

Every teleop takeover then ran the full escalation: request_takeover, a 15s cooperative wait
this process could not answer (no on_takeover handler, no polling), then SIGTERM/SIGKILL —
and systemd's Restart=always brought it back 5s later with a fresh pid. Two fixes: answer the
cooperative request, and actually let go when done.
"""
from __future__ import annotations

import ast
import pathlib
import sys
import time
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _FakeArmApi:
    def __init__(self):
        self.calls = []

    def clean_error(self):
        self.calls.append("clean_error")

    def clean_warn(self):
        self.calls.append("clean_warn")

    def motion_enable(self, _on):
        self.calls.append("motion_enable")


class _FakeHandler:
    def __init__(self, **kw):
        self.arm = _FakeArmApi()
        self.disconnects = 0
        self.modes, self.states, self.moves = [], [], []

    def api_set_mode(self, m):
        self.modes.append(m)

    def api_set_state(self, s):
        self.states.append(s)

    def api_get_servo_angle(self, is_radian=False):
        # The SELFIE pose, not home: a fake sitting at home makes move_arm_home a no-op and
        # silently hides any test asserting that nothing moved.
        return 0, [-179, 72.4, -27.8, 40.7, 24.4, -50.9, 0]

    def api_set_servo_angle(self, **kw):
        self.moves.append(kw.get("angle"))
        return 0

    def disconnect(self):
        self.disconnects += 1


@pytest.fixture
def ac(monkeypatch):
    import arm_control

    monkeypatch.setattr(arm_control, "XArmHandler", _FakeHandler)
    monkeypatch.setattr(arm_control, "_arm_handler", None)
    monkeypatch.setattr(arm_control, "_arm_deployed", False)
    monkeypatch.setattr(arm_control.time, "sleep", lambda _s: None)
    return arm_control


def test_releasing_disconnects_the_handler(ac):
    handler = ac.get_arm()
    ac.arm_release()
    assert handler.disconnects == 1, (
        "without disconnect() the lease is never released and uvicorn squats on the arm")


def test_releasing_drops_the_cached_handler(ac):
    ac.get_arm()
    ac.arm_release()
    assert ac._arm_handler is None


def test_the_next_call_reconnects(ac):
    first = ac.get_arm()
    ac.arm_release()
    assert ac.get_arm() is not first


def test_releasing_still_hands_back_mode_1(ac):
    """The original job of the function, which the new teardown must not displace."""
    handler = ac.get_arm()
    ac.arm_release()
    assert handler.modes[-1] == 1 and handler.states[-1] == 0


def test_release_without_a_connection_is_still_a_no_op(ac):
    ac.arm_release()
    assert ac._arm_handler is None


def test_a_failing_disconnect_still_clears_the_cache(ac):
    """A wedged SDK must not leave a dead handler cached forever."""
    handler = ac.get_arm()
    handler.disconnect = lambda: (_ for _ in ()).throw(RuntimeError("sdk gone"))
    ac.arm_release()
    assert ac._arm_handler is None


# ── the cooperative takeover hook ───────────────────────────────────────────

def test_the_takeover_hook_releases_the_arm(ac):
    handler = ac.get_arm()
    ac._release_for_takeover()
    for _ in range(200):
        if handler.disconnects:
            break
        time.sleep(0.01)
    assert handler.disconnects == 1


def test_the_takeover_hook_does_not_home_the_arm(ac):
    """The taker adopts the arm where it stands — that is what ABC's adopt path is for.
    Homing here would also blow the 15s cooperative window and get us killed anyway."""
    handler = ac.get_arm()
    ac._arm_deployed = True
    ac._release_for_takeover()
    for _ in range(200):
        if handler.disconnects:
            break
        time.sleep(0.01)
    assert handler.moves == [], "the takeover hook commanded motion"


def test_the_hook_is_registered_with_the_lease(monkeypatch):
    import arm_control

    registered = []
    fake = types.SimpleNamespace(on_takeover=registered.append)
    mod = types.ModuleType("arm_base_control.resource_lease")
    mod.default_lease = lambda: fake
    monkeypatch.setitem(sys.modules, "arm_base_control.resource_lease", mod)
    arm_control._register_takeover_hook()
    assert registered == [arm_control._release_for_takeover]


def test_an_older_lease_api_does_not_break_startup(monkeypatch):
    """Registration must never be able to stop the service booting."""
    import arm_control

    mod = types.ModuleType("arm_base_control.resource_lease")
    monkeypatch.setitem(sys.modules, "arm_base_control.resource_lease", mod)
    arm_control._register_takeover_hook()


# ── the call sites ──────────────────────────────────────────────────────────

def _fn(path: pathlib.Path, name: str) -> ast.FunctionDef:
    return next(f for f in ast.walk(ast.parse(path.read_text()))
                if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name == name)


def test_shutdown_releases_the_arm():
    """systemctl stop must free the lease; nothing else in this process does."""
    body = ast.unparse(_fn(ROOT / "main.py", "lifespan"))
    assert "arm_release" in body


def test_the_disconnect_endpoint_still_releases_after_homing():
    src = (ROOT / "main.py").read_text()
    home = src.index("arm_control.move_arm_home")
    rel = src.index("arm_control.arm_release", home)
    assert rel > home, "release must follow homing, or the arm is dropped mid-trajectory"


def test_release_is_the_only_disconnect_path():
    """One teardown, so a second one cannot drift out of sync with the lease."""
    calls = [n for n in ast.walk(ast.parse((ROOT / "arm_control.py").read_text()))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "disconnect"]
    assert len(calls) == 1


def test_the_hook_is_registered_at_import():
    """Registering inside get_arm() would mean an arm nobody has moved yet is unpreemptable
    — and it cannot fire before we own the lease anyway."""
    tree = ast.parse((ROOT / "arm_control.py").read_text())
    top = [n for n in tree.body
           if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
           and isinstance(n.value.func, ast.Name)
           and n.value.func.id == "_register_takeover_hook"]
    assert len(top) == 1
