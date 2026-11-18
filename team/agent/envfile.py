"""Minimal .env loader (KEY=VALUE lines). Existing environment variables win.
Returns only the *names* it set, so callers can log them without exposing values."""
from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: str | os.PathLike | None) -> list[str]:
    if not path:
        return []
    p = Path(path)
    if not p.is_file():
        return []
    loaded = []
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k and k not in os.environ:
            os.environ[k] = v
            loaded.append(k)
    return loaded
