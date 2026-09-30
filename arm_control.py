"""xArm motion for the selfie station.

The handler is constructed on first use. Importing this module never connects to
the robot, so the camera service can start while the arm is unavailable.
"""

import logging
import math
import os
import threading
import time
from contextlib import contextmanager

from arm_base_control import load_config
from arm_base_control.arm import XArmHandler, selfie_path
from arm_base_control.resource_lease import (
    default_lease,
    confirm_or_takeover,
    TAKEOVER,
)

class SelfieArmHandler(XArmHandler):
    """Keep the station's explicit trajectory in charge of recovery motion."""

    def start_error_monitor(self):
        # The upstream monitor can reset or home the arm on a fault. A station
        # operator must decide when to retry after a failed waypoint instead.
        pass


ARM_IP = os.environ.get("SELFIE_ARM_IP", "172.16.0.13")
# The robot config's arm.selfie block, which SBot's selfie_pose verb follows too.
_ARM_CFG = load_config().arm
_SELFIE = getattr(_ARM_CFG, "selfie", None)
ARM_TRAJECTORY = [[float(a) for a in wp] for wp in _SELFIE.path] if _SELFIE else []
ARM_HOME_POSE = ARM_TRAJECTORY[0] if ARM_TRAJECTORY else None
ARM_SELFIE_POSE = ARM_TRAJECTORY[-1] if ARM_TRAJECTORY else None
ARM_POSE_TOL_DEG = float(getattr(_SELFIE, "tolerance_deg", 0))
ARM_SPEED = float(getattr(_SELFIE, "speed", 0))
ARM_MVACC = float(getattr(_SELFIE, "mvacc", 0))
ARM_MOVE_TIMEOUT_SEC = 30
ARM_SETTLE_SEC = 0.5
LOG = logging.getLogger("uvicorn.error.arm")

_arm_handler = None
_arm_lock = threading.RLock()
_arm_deployed = False
_last_waypoint_index = None
_takeover_watched = False


class ArmLeaseConflict(RuntimeError):
    """Another ArmBaseControl process currently owns the arm lease."""

    def __init__(self, name, pid, mode, task):
        super().__init__(f"Arm is currently controlled by {name} (pid {pid}).")
        self.owner = {"name": name, "pid": pid, "mode": mode, "task": task}


class ArmNotPosed(RuntimeError):
    """The arm must be at the selfie pose before a capture."""


def get_arm(takeover=False):
    """Connect once, only when arm access is requested."""
    global _arm_handler
    with _arm_lock:
        if _arm_handler is None:
            lease = default_lease()
            pid, name, mode, task = lease.who()

            if pid is None:
                decision = TAKEOVER
            else:
                print(f"Robot is held by {name} (pid {pid}, mode={mode}, task={task})")
                if not takeover:
                    raise ArmLeaseConflict(name, pid, mode, task)
                decision = confirm_or_takeover(mode="my-project", assume_yes=True, lease=lease)

            if decision != TAKEOVER:
                raise ArmLeaseConflict(name, pid, mode, task)
            # Safe to connect / construct the robot handler now.
            _arm_handler = SelfieArmHandler(
                robot_ip=ARM_IP, gripper=None, dynamic_recovery_enabled=False, lease_mode="selfie"
            )
            _watch_takeover(lease)
        return _arm_handler


def _watch_takeover(lease):
    """Hand the arm over when another launcher asks for it; registered once, the lease re-arms it per acquire."""
    global _takeover_watched
    if not _takeover_watched:
        lease.on_takeover(_on_takeover)
        _takeover_watched = True


def _on_takeover():
    """Fired on the lease's heartbeat thread, so it only starts the handover."""
    threading.Thread(target=hand_over, name="selfie-handover", daemon=True).start()


def hand_over():
    """Give the arm to a launcher that asked for it: home it along the selfie path when it is out, then let go."""
    global _arm_handler, _arm_deployed, _last_waypoint_index
    with _arm_lock:
        if _arm_handler is None:
            return
        LOG.warning("another program asked for the arm; returning it along the selfie path and releasing it")
        try:
            move_arm_home()
        except Exception as exc:
            LOG.error("could not return the arm home (%s); releasing it where it stands", exc)
        try:
            _arm_handler.disconnect()  # releases the ArmBaseControl ownership lease
        finally:
            _arm_handler = None
            _arm_deployed = False
            _last_waypoint_index = None


def arm_current_angles(takeover=False):
    """Return six measured joint angles, or None when the read is invalid."""
    code, angles = get_arm(takeover=takeover).api_get_servo_angle(is_radian=False, is_real=True)
    if code != 0 or angles is None or len(angles) < 6:
        return None
    measured = list(angles[:6])
    if not all(math.isfinite(a) for a in measured):
        return None
    return measured


def _matches_pose(angles, target, tol=ARM_POSE_TOL_DEG):
    if len(target) != 6:
        raise ValueError("An arm pose must contain six joint angles")
    return angles is not None and all(
        abs(actual - desired) <= tol for actual, desired in zip(angles, target)
    )


def arm_at_pose(target, tol=ARM_POSE_TOL_DEG):
    """Check the measured pose without issuing a motion command."""
    return _matches_pose(arm_current_angles(), target, tol)


def _check_code(action, code):
    if code != 0:
        raise RuntimeError(f"Arm {action} failed (code {code})")


def _initial_point(takeover=False):
    """The controller's own initial point [J1..J6 deg], or None when it cannot be read."""
    code, angles = get_arm(takeover=takeover).get_initial_point()
    if code != 0 or angles is None or len(angles) < 6:
        return None
    point = [float(a) for a in angles[:6]]
    return point if all(math.isfinite(a) for a in point) else None


def _move_verified(name, target, takeover=False):
    _check_code(
        f"move to {name}",
        get_arm(takeover=takeover).api_set_servo_angle(
            angle=list(target), speed=ARM_SPEED, mvacc=ARM_MVACC,
            wait=True, timeout=ARM_MOVE_TIMEOUT_SEC,
        ),
    )
    if not _matches_pose(arm_current_angles(takeover=takeover), target):
        raise RuntimeError(f"Arm did not reach {name}")


def _recover_home(angles, takeover=False):
    """Bring an arm that is off the path home through the controller's initial point; refuse behind the camera line."""
    initial = _initial_point(takeover=takeover)
    if initial is None:
        raise RuntimeError("Arm is off the selfie path and its controller's initial point cannot be read; "
                           "manual recovery required")
    at_initial = _matches_pose(angles, initial)
    if not at_initial:
        behind = selfie_path.behind_line(get_arm(takeover=takeover), _ARM_CFG)
        if behind:
            line = float(getattr(_ARM_CFG, "home_caution_x_mm", 0))
            raise RuntimeError(f"Arm needs a manual reset: {behind}, and below {line:g} mm the station does not "
                               f"move it on its own")
    arm_ready(takeover=takeover)
    if not at_initial:
        LOG.warning("arm is off the selfie path, clear of the camera line; moving it to the controller's "
                    "initial point %s", [round(a, 1) for a in initial])
        _move_verified("the initial point", initial, takeover=takeover)
    LOG.warning("arm is at the controller's initial point; moving it to the selfie path's home %s", ARM_HOME_POSE)
    _move_verified("the home pose", ARM_HOME_POSE, takeover=takeover)


def arm_ready(takeover=False):
    """Enter position mode for blocking joint moves."""
    arm = get_arm(takeover=takeover)
    _check_code("clear error", arm.arm.clean_error())
    _check_code("clear warning", arm.arm.clean_warn())
    time.sleep(0.5)
    _check_code("enable motion", arm.motion_enable(True))
    _check_code("set position mode", arm.api_set_mode(0))
    _check_code("set ready state", arm.api_set_state(0))
    time.sleep(0.2)


def _follow_indices(indices, takeover=False):
    global _last_waypoint_index
    arm = get_arm(takeover=takeover)
    for index in indices:
        _check_code(
            f"move to waypoint {index + 1}/{len(ARM_TRAJECTORY)}",
            arm.api_set_servo_angle(
                angle=list(ARM_TRAJECTORY[index]), speed=ARM_SPEED,
                mvacc=ARM_MVACC, wait=True, timeout=ARM_MOVE_TIMEOUT_SEC,
            ),
        )
        _last_waypoint_index = index


def move_arm_to_selfie(takeover=False):
    """Deploy from the home pose through every intermediate waypoint."""
    global _arm_deployed, _last_waypoint_index
    with _arm_lock:
        if not ARM_TRAJECTORY:
            raise RuntimeError("This robot's config has no arm.selfie path")
        angles = arm_current_angles(takeover=takeover)
        if angles is None:
            raise RuntimeError("Cannot read arm pose before deployment")
        if _matches_pose(angles, ARM_SELFIE_POSE):
            _arm_deployed = True
            _last_waypoint_index = len(ARM_TRAJECTORY) - 1
            return
        if not _matches_pose(angles, ARM_HOME_POSE):
            if _arm_deployed and selfie_path.waypoint(angles, ARM_TRAJECTORY, ARM_POSE_TOL_DEG) is not None:
                raise RuntimeError("Arm deployment is incomplete; return it home first")
            _recover_home(angles, takeover=takeover)
        _last_waypoint_index = 0
        _arm_deployed = True  # permit recovery if a later waypoint fails
        arm_ready(takeover=takeover)
        _follow_indices(range(1, len(ARM_TRAJECTORY)), takeover=takeover)
        time.sleep(ARM_SETTLE_SEC)


@contextmanager
def selfie_pose_guard():
    """Keep the arm at its measured selfie pose throughout the shutter; another process holding the arm keeps its own."""
    with _arm_lock:
        if _arm_handler is None and default_lease().who()[0] is not None:
            yield
            return
        if (_arm_handler is None or not _arm_deployed
                or not _matches_pose(arm_current_angles(), ARM_SELFIE_POSE)):
            raise ArmNotPosed("Pose the arm before taking a picture.")
        yield


def move_arm_home():
    """Retrace the trajectory from a verified waypoint reached this session."""
    global _arm_deployed, _last_waypoint_index
    with _arm_lock:
        if not _arm_deployed:
            return False
        angles = arm_current_angles()
        if angles is None:
            raise RuntimeError("Cannot read arm pose for safe return home")
        if _matches_pose(angles, ARM_HOME_POSE):
            _arm_deployed = False
            _last_waypoint_index = 0
            return True
        # A failed move may have reached its target despite a nonzero status.
        highest = min((_last_waypoint_index or 0) + 1, len(ARM_TRAJECTORY) - 1)
        start = next(
            (i for i in range(highest, 0, -1)
             if _matches_pose(angles, ARM_TRAJECTORY[i])),
            None,
        )
        if start is None:
            _recover_home(angles)
            _arm_deployed = False
            _last_waypoint_index = 0
            return True
        arm_ready()
        _follow_indices(range(start - 1, -1, -1))
        if not arm_at_pose(ARM_HOME_POSE):
            raise RuntimeError("Arm did not reach the home pose")
        _arm_deployed = False
        return True


def deployed():
    """Whether this app has the arm out along its selfie path."""
    return _arm_deployed


def arm_release():
    """Switch a parked arm to teaching mode and release its connection."""
    global _arm_handler, _arm_deployed, _last_waypoint_index
    with _arm_lock:
        if _arm_handler is None:
            return
        if not arm_at_pose(ARM_HOME_POSE):
            raise RuntimeError("Arm must be at home before manual release")
        arm = _arm_handler
        _check_code("clear error", arm.arm.clean_error())
        _check_code("clear warning", arm.arm.clean_warn())
        time.sleep(0.3)
        _check_code("set joint teaching mode", arm.api_set_mode(2))
        _check_code("set ready state", arm.api_set_state(0))
        try:
            arm.disconnect()  # releases the ArmBaseControl ownership lease
        finally:
            _arm_handler = None
        _arm_deployed = False
        _last_waypoint_index = None
