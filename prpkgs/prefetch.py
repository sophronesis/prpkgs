"""Compute SRI narHashes for nixpkgs PR tarballs.

We shell out to `nix-prefetch-url --unpack` which is universally available on
any nix installation. It returns a base32 sha256 of the unpacked tree, which
matches the value `builtins.fetchTarball { url=...; sha256=...; }` accepts.

The base32 hash is then converted to SRI form (`sha256-...`) via `nix hash`
since newer flake-aware nix prefers SRI and the format is what we emit into
pending.nix.

Hashes are cached in the prpkgs DB keyed by commit SHA, so a re-run only
prefetches tarballs whose PR has moved since last time.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable

TARBALL_URL = "https://github.com/NixOS/nixpkgs/archive/{rev}.tar.gz"


class PrefetchError(RuntimeError):
    pass


@dataclass
class PrefetchStats:
    hits: int = 0  # rev already cached
    fetched: int = 0  # rev prefetched this run
    errors: int = 0
    failed_revs: list[str] = field(default_factory=list)


def check_tools_available() -> None:
    """Raise if `nix` / `nix-prefetch-url` are missing."""
    for tool in ("nix", "nix-prefetch-url"):
        if shutil.which(tool) is None:
            raise PrefetchError(f"`{tool}` not on PATH - install nix or enter `nix develop`")


def _prefetch_base32(rev: str) -> str:
    url = TARBALL_URL.format(rev=rev)
    proc = subprocess.run(
        ["nix-prefetch-url", "--unpack", "--type", "sha256", url],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise PrefetchError(
            f"nix-prefetch-url failed for {rev}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()


def _to_sri(base32: str) -> str:
    # newer nix exposes `nix hash convert`; older nix uses `nix hash to-sri`.
    for argv in (
        [
            "nix",
            "--extra-experimental-features",
            "nix-command",
            "hash",
            "convert",
            "--hash-algo",
            "sha256",
            "--to",
            "sri",
            base32,
        ],
        [
            "nix",
            "--extra-experimental-features",
            "nix-command",
            "hash",
            "to-sri",
            "--type",
            "sha256",
            base32,
        ],
    ):
        proc = subprocess.run(argv, capture_output=True, text=True)
        if proc.returncode == 0:
            return proc.stdout.strip()
    raise PrefetchError(
        "could not convert hash to SRI (tried `nix hash convert` and `nix hash to-sri`)"
    )


def prefetch_rev(rev: str) -> str:
    """Fetch a single tarball and return its SRI sha256."""
    return _to_sri(_prefetch_base32(rev))


def prefetch_many(
    revs: list[str],
    cache_get: Callable[[str], str | None],
    cache_put: Callable[[str, str], None],
    apply_to_rows: Callable[[str, str], None] | None = None,
    progress: Callable[[int, int, str, str], None] | None = None,
) -> PrefetchStats:
    """Prefetch every rev, hitting the cache first.

    Args:
        revs: distinct head SHAs to hash.
        cache_get: rev -> stored narHash or None.
        cache_put: (rev, narHash) -> None.
        apply_to_rows: optional (rev, narHash) -> None hook called once per rev
            so the caller can write the hash onto the pending_packages rows.
        progress: optional (index, total, rev, status) -> None callback.
            status is "hit", "fetched", or "error".
    """
    check_tools_available()
    stats = PrefetchStats()
    total = len(revs)
    for i, rev in enumerate(revs, 1):
        cached = cache_get(rev)
        if cached:
            stats.hits += 1
            if apply_to_rows:
                apply_to_rows(rev, cached)
            if progress:
                progress(i, total, rev, "hit")
            continue
        try:
            nar_hash = prefetch_rev(rev)
        except PrefetchError as e:
            stats.errors += 1
            stats.failed_revs.append(rev)
            if progress:
                progress(i, total, rev, f"error: {e}")
            continue
        cache_put(rev, nar_hash)
        if apply_to_rows:
            apply_to_rows(rev, nar_hash)
        stats.fetched += 1
        if progress:
            progress(i, total, rev, "fetched")
    return stats
