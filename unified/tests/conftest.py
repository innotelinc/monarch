"""Test configuration.

The settings object is built at import time, so the environment has to be in
place before any test module imports `app.settings`. Pytest imports conftest
first, which is what makes that possible.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SANDBOX = Path(tempfile.mkdtemp(prefix="monarch-core-tests-"))

os.environ.update({
    "CORE_DATA_DIR": str(_SANDBOX / "data"),
    "CORE_STATIC_DIR": str(ROOT / "static"),
    "CORE_LIBRARY_ROOT": str(_SANDBOX / "library"),
    "CORE_COMPLETED_ROOT": str(_SANDBOX / "completed"),
    "CORE_AUTH_DISABLED": "1",
    "TRANSMISSION_URL": "http://127.0.0.1:9",  # nothing listens; clients must cope
    "CORE_SEARCH_INTERVAL": "3600",
    "CORE_IMPORT_INTERVAL": "3600",
})
