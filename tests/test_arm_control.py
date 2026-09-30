"""Arm workflow checks with a fake handler; these never connect to hardware."""

import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import arm_control as control


class FakeArm:
    def __init__(self, pose=None):
        self.pose = list(pose if pose is not None else control.ARM_HOME_POSE)
        self.arm = self
        self.moves = []
        self.modes = []
        self.fail_move = None
        self.fail_mode = None
        self.read_code = 0
        self.disconnected = False
        self.moves_at_disconnect = None
        self.initial = [0.0, 63.0, 0.0, 0.0, 27.0, 0.0]
        self.initial_code = 0
        self.x = 300.0
        self.position_code = 0
        self.freeze = False
        self.gripper = "robotiq"
        self.open_result = True
        self.opened_at = None       # how many moves had run when the gripper opened

    def gripper_open(self, blocking=None):
        self.opened_at = len(self.moves)
        return self.open_result

    def api_get_servo_angle(self, **kwargs):
        return self.read_code, self.pose

    def api_set_servo_angle(self, *, angle, **kwargs):
        self.moves.append(angle)
        if len(self.moves) == self.fail_move:
            return -10
        if not self.freeze:
            self.pose = list(angle)
        return 0

    def get_initial_point(self):
        return self.initial_code, list(self.initial)

    def api_get_position(self, is_radian=False):
        return self.position_code, [self.x, 0.0, 0.0, 180.0, 0.0, 0.0]

    def disconnect(self):
        self.disconnected = True
        self.moves_at_disconnect = len(self.moves)

    def clean_error(self):
        return 0

    def clean_warn(self):
        return 0

    def motion_enable(self, enabled):
        return 0

    def api_set_mode(self, mode):
        self.modes.append(mode)
        return -1 if mode == self.fail_mode else 0

    def api_set_state(self, state):
        return 0


def pin_camera_line(case):
    p = patch.object(control, "_ARM_CFG", SimpleNamespace(home_caution_x_mm=50))
    p.start()
    case.addCleanup(p.stop)


class ArmWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.arm = FakeArm()
        control._arm_handler = self.arm
        control._arm_deployed = False
        control._last_waypoint_index = None
        self.sleep = patch.object(control.time, "sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        pin_camera_line(self)

    def test_deploy_home_and_release(self):
        control.move_arm_to_selfie()
        self.assertEqual(self.arm.moves, control.ARM_TRAJECTORY[1:])
        self.assertTrue(control._arm_deployed)
        control.move_arm_home()
        self.assertEqual(self.arm.moves, control.ARM_TRAJECTORY[1:] + control.ARM_TRAJECTORY[-2::-1])
        self.assertFalse(control._arm_deployed)
        control.arm_release()
        self.assertEqual(self.arm.modes, [0, 0, 2])
        self.assertTrue(self.arm.disconnected)
        self.assertIsNone(control._arm_handler)

    def test_the_gripper_opens_before_the_first_waypoint_out(self):
        control.move_arm_to_selfie()
        self.assertEqual(self.arm.opened_at, 0)
        self.assertEqual(self.arm.moves, control.ARM_TRAJECTORY[1:])

    def test_a_gripper_that_does_not_open_keeps_the_arm_home(self):
        self.arm.open_result = False
        with self.assertRaisesRegex(RuntimeError, "gripper did not open"):
            control.move_arm_to_selfie()
        self.assertEqual(self.arm.moves, [])

    def test_an_arm_base_control_that_cannot_say_logs_it_and_goes_out(self):
        self.arm.open_result = None
        with self.assertLogs("uvicorn.error.arm", "WARNING") as logs:
            control.move_arm_to_selfie()
        self.assertIn("does not report whether the gripper opened", logs.output[0])
        self.assertEqual(self.arm.moves, control.ARM_TRAJECTORY[1:])

    def test_an_arm_with_no_gripper_goes_out_without_one(self):
        self.arm.gripper = None
        control.move_arm_to_selfie()
        self.assertIsNone(self.arm.opened_at)
        self.assertEqual(self.arm.moves, control.ARM_TRAJECTORY[1:])

    def test_the_station_builds_its_arm_with_the_robots_gripper(self):
        import ast
        from pathlib import Path
        tree = ast.parse(Path(control.__file__).read_text())
        built = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "SelfieArmHandler"]
        self.assertEqual([ast.unparse(k.value) for n in built for k in n.keywords if k.arg == "gripper"], ["GRIPPER"])

    def test_disconnect_before_deployment_does_not_move(self):
        control.move_arm_home()
        self.assertEqual(self.arm.moves, [])

    def test_an_unknown_start_pose_behind_the_camera_line_needs_a_manual_reset(self):
        self.arm.pose, self.arm.x = [5] * 6, 12.0
        with self.assertRaisesRegex(RuntimeError, "manual reset"):
            control.move_arm_to_selfie()
        self.assertEqual(self.arm.moves, [])

    def test_failed_waypoint_can_return_from_last_reached(self):
        self.arm.fail_move = 2
        with self.assertRaisesRegex(RuntimeError, "waypoint"):
            control.move_arm_to_selfie()
        self.assertEqual(self.arm.pose, control.ARM_TRAJECTORY[1])
        control.move_arm_home()
        self.assertEqual(self.arm.pose, control.ARM_HOME_POSE)
        self.assertFalse(control._arm_deployed)

    def test_mid_move_pose_refuses_automatic_reverse(self):
        self.arm.fail_move = 2
        with self.assertRaises(RuntimeError):
            control.move_arm_to_selfie()
        self.arm.pose = [(a + b) / 2 for a, b in zip(control.ARM_TRAJECTORY[1], control.ARM_TRAJECTORY[2])]
        self.arm.x = 12.0
        with self.assertRaisesRegex(RuntimeError, "manual reset"):
            control.move_arm_home()
        self.assertEqual(len(self.arm.moves), 2)

    def test_an_arm_at_the_initial_point_goes_home_then_deploys(self):
        self.arm.pose = list(self.arm.initial)
        control._arm_deployed = True
        with self.assertLogs("uvicorn.error.arm", "WARNING"):
            control.move_arm_to_selfie()
        self.assertEqual(self.arm.moves, [control.ARM_HOME_POSE] + control.ARM_TRAJECTORY[1:])
        self.assertTrue(control._arm_deployed)

    def test_return_home_accepts_the_initial_point(self):
        control._arm_deployed, control._last_waypoint_index = True, len(control.ARM_TRAJECTORY) - 1
        self.arm.pose = list(self.arm.initial)
        with self.assertLogs("uvicorn.error.arm", "WARNING"):
            self.assertTrue(control.move_arm_home())
        self.assertEqual(self.arm.moves, [control.ARM_HOME_POSE])
        self.assertFalse(control._arm_deployed)

    def test_an_arm_off_the_path_clear_of_the_line_goes_home_through_the_initial_point(self):
        control._arm_deployed, control._last_waypoint_index = True, len(control.ARM_TRAJECTORY) - 1
        self.arm.pose = [10.0, 40.0, -20.0, 0.0, 30.0, 0.0]
        with self.assertLogs("uvicorn.error.arm", "WARNING"):
            self.assertTrue(control.move_arm_home())
        self.assertEqual(self.arm.moves, [self.arm.initial, control.ARM_HOME_POSE])
        self.assertFalse(control._arm_deployed)

    def test_an_arm_off_the_path_behind_the_line_needs_a_manual_reset_and_is_left_untouched(self):
        control._arm_deployed, control._last_waypoint_index = True, len(control.ARM_TRAJECTORY) - 1
        self.arm.pose, self.arm.x = [-150.0, 60.0, -30.0, 0.0, 0.0, 0.0], 12.0
        for action in (control.move_arm_home, control.move_arm_to_selfie):
            with self.assertRaisesRegex(RuntimeError, "Arm needs a manual reset: the arm is at x = 12 mm, behind "
                                                      "the 50 mm line near the VLA cameras, and below 50 mm"):
                action()
        self.assertEqual((self.arm.moves, self.arm.modes), ([], []))
        self.assertTrue(control._arm_deployed)

    def test_an_unreadable_position_off_the_path_needs_a_manual_reset(self):
        control._arm_deployed = True
        self.arm.pose, self.arm.position_code = [10.0, 40.0, -20.0, 0.0, 30.0, 0.0], -1
        with self.assertRaisesRegex(RuntimeError, "manual reset: the arm's position could not be read"):
            control.move_arm_home()
        self.assertEqual(self.arm.moves, [])

    def test_an_unreadable_initial_point_moves_nothing(self):
        control._arm_deployed = True
        self.arm.pose, self.arm.initial_code = [10.0, 40.0, -20.0, 0.0, 30.0, 0.0], -1
        with self.assertRaisesRegex(RuntimeError, "initial point cannot be read"):
            control.move_arm_home()
        self.assertEqual(self.arm.moves, [])

    def test_a_stale_flag_with_the_arm_at_home_still_deploys(self):
        control._arm_deployed = True
        control.move_arm_to_selfie()
        self.assertEqual(self.arm.moves, control.ARM_TRAJECTORY[1:])

    def test_a_stop_on_the_path_still_needs_a_return_home_first(self):
        control._arm_deployed = True
        self.arm.pose = list(control.ARM_TRAJECTORY[2])
        with self.assertRaisesRegex(RuntimeError, "return it home first"):
            control.move_arm_to_selfie()
        self.assertEqual(self.arm.moves, [])

    def test_a_recovery_move_that_does_not_arrive_is_reported(self):
        control._arm_deployed = True
        self.arm.pose, self.arm.freeze = list(self.arm.initial), True
        with self.assertLogs("uvicorn.error.arm", "WARNING"), \
                self.assertRaisesRegex(RuntimeError, "did not reach the home pose"):
            control.move_arm_home()
        self.assertTrue(control._arm_deployed)

    def test_failed_pose_read_prevents_motion(self):
        self.arm.read_code = -1
        with self.assertRaisesRegex(RuntimeError, "Cannot read arm pose"):
            control.move_arm_to_selfie()
        self.assertEqual(self.arm.moves, [])

    def test_failed_mode_prevents_motion(self):
        self.arm.fail_mode = 0
        with self.assertRaisesRegex(RuntimeError, "position mode"):
            control.move_arm_to_selfie()
        self.assertEqual(self.arm.moves, [])

    def test_release_refuses_unparked_arm(self):
        self.arm.pose = list(control.ARM_SELFIE_POSE)
        with self.assertRaisesRegex(RuntimeError, "at home"):
            control.arm_release()
        self.assertEqual(self.arm.modes, [])

    def test_short_read_does_not_match_pose(self):
        self.arm.pose = [0]
        self.assertFalse(control.arm_at_pose(control.ARM_HOME_POSE))

    def test_capture_requires_measured_selfie_pose(self):
        with self.assertRaises(control.ArmNotPosed):
            with control.selfie_pose_guard():
                pass

        control.move_arm_to_selfie()
        with control.selfie_pose_guard():
            pass

        self.arm.pose[0] += control.ARM_POSE_TOL_DEG + 1
        with self.assertRaises(control.ArmNotPosed):
            with control.selfie_pose_guard():
                pass

    def test_capture_leaves_the_pose_to_a_session_holding_the_arm(self):
        control._arm_handler = None
        held = type("Lease", (), {"who": lambda self: (4242, "sbot", "classical", "")})()
        free = type("Lease", (), {"who": lambda self: (None, None, None, None)})()
        with patch.object(control, "default_lease", return_value=held):
            with control.selfie_pose_guard():
                pass
        with patch.object(control, "default_lease", return_value=free):
            with self.assertRaises(control.ArmNotPosed):
                with control.selfie_pose_guard():
                    pass

    def test_a_robot_with_no_selfie_path_refuses_before_connecting(self):
        control._arm_handler = None
        with patch.object(control, "ARM_TRAJECTORY", []), \
                patch.object(control, "get_arm", side_effect=AssertionError("connected")):
            with self.assertRaisesRegex(RuntimeError, "no arm.selfie path"):
                control.move_arm_to_selfie()


class FakeLease:
    def __init__(self, owner=(4242, "sbot@l5vel-base03", "sbot", "pickup")):
        self.owner = owner
        self.callbacks = []

    def who(self):
        return self.owner

    def on_takeover(self, fn):
        self.callbacks.append(fn)


class HandoverTests(unittest.TestCase):
    """Another launcher asked for the arm: the app brings it home along the path, then lets go."""

    def setUp(self):
        self.arm = FakeArm()
        control._arm_handler = self.arm
        control._arm_deployed = False
        control._last_waypoint_index = None
        control._takeover_watched = False
        self.sleep = patch.object(control.time, "sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        self.addCleanup(setattr, control, "_arm_handler", None)
        pin_camera_line(self)

    def test_a_takeover_returns_the_arm_along_the_path_before_releasing_it(self):
        control.move_arm_to_selfie()
        control.hand_over()
        self.assertEqual(self.arm.moves, control.ARM_TRAJECTORY[1:] + control.ARM_TRAJECTORY[-2::-1])
        self.assertEqual(self.arm.moves_at_disconnect, len(self.arm.moves))
        self.assertIsNone(control._arm_handler)
        self.assertFalse(control._arm_deployed)

    def test_an_arm_off_the_path_is_released_where_it_stands_and_logged(self):
        control.move_arm_to_selfie()
        self.arm.pose = [(a + b) / 2 for a, b in zip(control.ARM_TRAJECTORY[2], control.ARM_TRAJECTORY[3])]
        self.arm.x = 12.0
        with self.assertLogs("uvicorn.error.arm", "ERROR") as logs:
            control.hand_over()
        self.assertEqual(self.arm.moves, control.ARM_TRAJECTORY[1:])
        self.assertTrue(self.arm.disconnected)
        self.assertIsNone(control._arm_handler)
        self.assertIn("releasing it where it stands", logs.output[0])

    def test_a_handover_with_no_arm_held_does_nothing(self):
        control._arm_handler = None
        control.hand_over()
        self.assertFalse(self.arm.disconnected)

    def test_release_where_it_stands_lets_go_of_an_arm_that_is_out_without_moving_it(self):
        control.move_arm_to_selfie()
        moves = len(self.arm.moves)
        self.assertTrue(control.release_where_it_stands())
        self.assertEqual(len(self.arm.moves), moves, "the release moves nothing")
        self.assertEqual(self.arm.moves_at_disconnect, moves)
        self.assertIsNone(control._arm_handler)
        self.assertFalse(control.deployed(), "the idle shutoff must not try to bring home an arm it let go")

    def test_release_where_it_stands_with_no_arm_held_says_so(self):
        control._arm_handler = None
        self.assertFalse(control.release_where_it_stands())
        self.assertFalse(self.arm.disconnected)

    def test_a_takeover_after_the_release_finds_nothing_to_bring_home(self):
        control.move_arm_to_selfie()
        control.release_where_it_stands()
        moves = len(self.arm.moves)
        control.hand_over()
        self.assertEqual(len(self.arm.moves), moves)


    def test_the_takeover_callback_only_starts_the_handover_thread(self):
        ran = threading.Event()
        names = []

        def record():
            names.append(threading.current_thread().name)
            ran.set()
        with patch.object(control, "hand_over", record):
            control._on_takeover()
            self.assertTrue(ran.wait(5.0))
        self.assertEqual(names, ["selfie-handover"])

    def test_a_refused_takeover_raises_instead_of_returning_no_arm(self):
        control._arm_handler = None
        with patch.object(control, "default_lease", return_value=FakeLease()), \
                patch.object(control, "confirm_or_takeover", return_value="abort"), \
                patch.object(control, "SelfieArmHandler", side_effect=AssertionError("constructed")):
            with self.assertRaises(control.ArmLeaseConflict) as refused:
                control.get_arm(takeover=True)
        self.assertEqual(refused.exception.owner["pid"], 4242)
        self.assertIsNone(control._arm_handler)

    def test_taking_the_arm_names_the_selfie_owner_and_watches_for_a_takeover_once(self):
        control._arm_handler = None
        lease, built = FakeLease(), []

        def construct(**kwargs):
            built.append(kwargs)
            return FakeArm()
        with patch.object(control, "default_lease", return_value=lease), \
                patch.object(control, "confirm_or_takeover", return_value=control.TAKEOVER), \
                patch.object(control, "SelfieArmHandler", side_effect=construct):
            control.get_arm(takeover=True)
            control._arm_handler = None
            control.get_arm(takeover=True)
        self.assertEqual([b["lease_mode"] for b in built], ["selfie", "selfie"])
        self.assertEqual(lease.callbacks, [control._on_takeover])


if __name__ == "__main__":
    unittest.main()
