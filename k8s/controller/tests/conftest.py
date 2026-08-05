"""Put the controller package on sys.path so tests import ``reconciler``,
``projections``, ``capabilities`` as flat modules — mirrors how they load
inside the controller container.
"""
from __future__ import annotations

import sys
from pathlib import Path

_CONTROLLER_DIR = Path(__file__).resolve().parent.parent
if str(_CONTROLLER_DIR) not in sys.path:
    sys.path.insert(0, str(_CONTROLLER_DIR))
