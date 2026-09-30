"""Imported by every test module that loads camera_connection: no test runs the real camera helper, takes the real scan lock or reads station.toml."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import camera_connection as cc

# Made-up station values; 192.0.2.0/24 is the RFC 5737 documentation range.
STATION = {'INTERFACE': 'wlxtest0', 'PROFILE': '00000000-0000-4000-8000-000000000001', 'CAMERA_IP': '192.0.2.1',
           'CAMERA_BT_NAME': 'X5 TEST01', 'WAKE_BEACON': cc.wake_beacon('TEST01'), 'STATION_PROBLEM': ''}

_state = {}


def setUpModule():
    _state['dir'] = tempfile.TemporaryDirectory()
    root = Path(_state['dir'].name)
    _state['patches'] = [patch.object(cc, 'HELPER', str(root / 'no-such-helper')),
                         patch.object(cc, 'SCAN_LOCK', root / 'scan.lock'),
                         *(patch.object(cc, name, value) for name, value in STATION.items())]
    for p in _state['patches']:
        p.start()


def tearDownModule():
    for p in _state.pop('patches'):
        p.stop()
    _state.pop('dir').cleanup()
