"""Shared pytest fixtures + sys.path bootstrap."""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure the spike/executor/ dir is on sys.path so tests can import
# runtime modules the same way the executor CLI does.
_EXECUTOR_DIR = Path(__file__).resolve().parent.parent
if str(_EXECUTOR_DIR) not in sys.path:
    sys.path.insert(0, str(_EXECUTOR_DIR))
