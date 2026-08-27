"""Stub the robot SDK so these tests run without the service venv.

arm_control imports path_setup and arm_base_control.arm at module scope; both live only in
the SelfieCam venv, which has no pytest. The tests replace XArmHandler anyway.
"""
import sys
import types

for name in ("path_setup",):
    sys.modules.setdefault(name, types.ModuleType(name))

if "arm_base_control" not in sys.modules:
    pkg = types.ModuleType("arm_base_control")
    pkg.__path__ = []
    arm_mod = types.ModuleType("arm_base_control.arm")
    arm_mod.XArmHandler = type("XArmHandler", (), {"__init__": lambda self, **kw: None})
    pkg.arm = arm_mod
    sys.modules["arm_base_control"] = pkg
    sys.modules["arm_base_control.arm"] = arm_mod
