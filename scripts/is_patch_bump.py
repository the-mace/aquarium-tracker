#!/usr/bin/env python3
"""Return exit 0 if a Dependabot PR title is a patch bump, else 2.

Used by .github/workflows/dependabot-automerge.yml so only patches auto-merge.
Minors/majors stay open for a look. Unparseable titles do not merge.
"""
from __future__ import annotations

import re
import sys

_FROM_TO = re.compile(
    r"from\s+v?(\d+(?:\.\d+)*)\s+to\s+v?(\d+(?:\.\d+)*)",
    re.IGNORECASE,
)


def _pad(version: str) -> tuple[int, int, int]:
    parts = tuple(int(p) for p in version.split("."))
    parts = parts + (0,) * (3 - len(parts))
    return parts[0], parts[1], parts[2]


def is_patch_bump(title: str) -> bool | None:
    """True if patch, False if minor/major, None if versions cannot be parsed."""
    match = re.search(_FROM_TO, title or "")
    if not match:
        return None
    old, new = _pad(match.group(1)), _pad(match.group(2))
    return old[0] == new[0] and old[1] == new[1]


if __name__ == "__main__":
    result = is_patch_bump(sys.argv[1] if len(sys.argv) > 1 else "")
    if result is True:
        sys.exit(0)
    sys.exit(2)
