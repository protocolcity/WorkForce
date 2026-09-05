"""Tiny HTTP probe for engine-API reachability."""
from __future__ import annotations

import urllib.error
import urllib.request


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1.5) as r:
            return 200 <= int(r.status) < 500
    except urllib.error.HTTPError as e:
        return 100 <= int(getattr(e, "code", 0) or 0) < 600
    except Exception:
        return False

