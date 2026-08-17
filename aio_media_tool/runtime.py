from __future__ import annotations

import sys


def is_frozen() -> bool:
    """Return True when the app is running from a packaged executable."""
    return bool(getattr(sys, "frozen", False))
