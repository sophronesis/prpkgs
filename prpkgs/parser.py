"""Parse package names + versions out of nixpkgs PR titles.

Nixpkgs convention is `name: init at version` for new packages. A single PR
can introduce multiple packages, with a few syntactic variants:

    "foo: init at 1.0.0"
    "python3Packages.foo: init at 1.0.0"
    "foo, bar: init at 1.0.0"
    "{foo, bar}: init at 1.0.0"
    "foo: init at 1.0.0, bar: init at 2.0.0"

This parser tries to extract every (name, version) pair from a title.
When it can't find any structured pair it falls back to a single entry
using the first colon-prefix or the trimmed title itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# `name: init at version`. version captures up to the next comma or end.
INIT_RE = re.compile(
    r"""
    (?P<names>[A-Za-z0-9_.\-+/{}\s,]+?)   # one or more comma-/brace-listed names
    \s*:\s*init\s+at\s+
    (?P<version>[^,;]+?)
    (?=\s*(?:,\s*[A-Za-z0-9_.\-+/{][^:]*?:\s*init\s+at|$|;))
    """,
    re.IGNORECASE | re.VERBOSE,
)

# fallback: anything that looks like `name: ...` (no version captured)
FALLBACK_PREFIX_RE = re.compile(r"^\s*([A-Za-z0-9_.\-+/{}, ]+?)\s*:\s*")

VALID_ATTR = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")


@dataclass(frozen=True)
class ParsedPackage:
    name: str  # last path segment (e.g. "foo" from "python3Packages.foo")
    attr_path: str  # the full attr path as written (e.g. "python3Packages.foo")
    version: str | None


def _expand_names(raw: str) -> list[str]:
    """Turn 'foo, bar' or '{foo, bar}' into ['foo', 'bar']."""
    raw = raw.strip()
    if raw.startswith("{") and raw.endswith("}"):
        raw = raw[1:-1]
    parts = [p.strip() for p in re.split(r"[,\s]+", raw) if p.strip()]
    # drop bare tokens that are obviously not attribute paths
    return [p for p in parts if VALID_ATTR.match(p)]


def parse_pr_title(title: str) -> list[ParsedPackage]:
    out: list[ParsedPackage] = []
    seen: set[tuple[str, str]] = set()

    for m in INIT_RE.finditer(title):
        version = m.group("version").strip().rstrip(",;")
        for attr in _expand_names(m.group("names")):
            name = attr.rsplit(".", 1)[-1]
            key = (attr, version)
            if key in seen:
                continue
            seen.add(key)
            out.append(ParsedPackage(name=name, attr_path=attr, version=version))

    if out:
        return out

    # nothing structured - try the bare prefix
    m = FALLBACK_PREFIX_RE.match(title)
    if m:
        for attr in _expand_names(m.group(1)):
            name = attr.rsplit(".", 1)[-1]
            out.append(ParsedPackage(name=name, attr_path=attr, version=None))
        if out:
            return out

    # last resort: whole title trimmed
    cleaned = title.strip().lstrip("[(").rstrip(")]")
    return [ParsedPackage(name=cleaned, attr_path="", version=None)]
