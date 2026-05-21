"""Compute SRI narHashes for nixpkgs PR tarballs.

We shell out to `nix-prefetch-url --unpack` which is universally available on
any nix installation. It returns a base32 sha256 of the unpacked tree, which
matches the value `builtins.fetchTarball { url=...; sha256=...; }` accepts.

The base32 hash is then converted to SRI form (`sha256-...`) via `nix hash`
since newer flake-aware nix prefers SRI and the format is what we emit into
pending.nix.

Hashes are cached in the prpkgs DB keyed by commit SHA, so a re-run only
prefetches tarballs whose PR has moved since last time.

Each unpacked nixpkgs is ~700MB in the nix store. We delete the store path
immediately after computing the hash so a CI runner with ~14GB free doesn't
fall over after ~20 PRs. Toggle with PRPKGS_KEEP_STORE_PATHS=1 if you want
to keep them around for repeat builds locally.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable

TARBALL_URL = "https://github.com/NixOS/nixpkgs/archive/{rev}.tar.gz"

KEEP_STORE_PATHS = os.environ.get("PRPKGS_KEEP_STORE_PATHS", "").lower() in (
    "1",
    "true",
    "yes",
)


class PrefetchError(RuntimeError):
    pass


@dataclass
class PrefetchStats:
    hits: int = 0  # rev already cached
    fetched: int = 0  # rev prefetched this run
    errors: int = 0
    failed_revs: list[str] = field(default_factory=list)


def check_tools_available() -> None:
    """Raise if `nix` / `nix-prefetch-url` / `nix-store` are missing."""
    for tool in ("nix", "nix-prefetch-url", "nix-store"):
        if shutil.which(tool) is None:
            raise PrefetchError(
                f"`{tool}` not on PATH - install nix or enter `nix develop`"
            )


def _prefetch_with_path(rev: str) -> tuple[str, str]:
    """Fetch the tarball and return (base32_sha256, store_path)."""
    url = TARBALL_URL.format(rev=rev)
    proc = subprocess.run(
        [
            "nix-prefetch-url",
            "--unpack",
            "--print-path",
            "--type",
            "sha256",
            url,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        msg = (proc.stderr.strip() or proc.stdout.strip()).splitlines()
        snippet = " | ".join(msg[-3:]) if msg else f"exit {proc.returncode}"
        raise PrefetchError(f"nix-prefetch-url failed for {rev}: {snippet}")
    lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    if len(lines) < 2:
        raise PrefetchError(
            f"nix-prefetch-url returned unexpected output for {rev}: {proc.stdout!r}"
        )
    return lines[0], lines[1]


def _to_sri(base32: str) -> str:
    # newer nix exposes `nix hash convert`; older nix uses `nix hash to-sri`.
    last_err = ""
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
        last_err = proc.stderr.strip()
    raise PrefetchError(
        f"could not convert hash to SRI (tried `nix hash convert` and "
        f"`nix hash to-sri`): {last_err}"
    )


def _delete_store_path(path: str) -> None:
    """Best-effort delete; ignore failures (e.g. live GC root, already gone).

    We skip `--ignore-liveness` because it requires the nix daemon's uid in
    multi-user installs. Plain `nix-store --delete` automatically prunes the
    stale auto-roots that `nix-prefetch-url --unpack` leaves behind once its
    tempdir is gone, which it is by the time we get here.
    """
    subprocess.run(
        ["nix-store", "--delete", path],
        capture_output=True,
        text=True,
    )


def prefetch_rev(rev: str) -> str:
    """Fetch a single tarball, return its SRI sha256, free disk."""
    base32, store_path = _prefetch_with_path(rev)
    try:
        sri = _to_sri(base32)
    finally:
        if not KEEP_STORE_PATHS:
            _delete_store_path(store_path)
    return sri


def prefetch_many(
    revs: list[str],
    cache_get: Callable[[str], str | None],
    cache_put: Callable[[str, str], None],
    apply_to_rows: Callable[[str, str], None] | None = None,
    progress: Callable[[int, int, str, str], None] | None = None,
    on_error: Callable[[str, str], None] | None = None,
) -> PrefetchStats:
    """Prefetch every rev, hitting the cache first.

    Args:
        revs: distinct head SHAs to hash.
        cache_get: rev -> stored narHash or None.
        cache_put: (rev, narHash) -> None.
        apply_to_rows: optional (rev, narHash) -> None hook called once per rev
            so the caller can write the hash onto the pending_packages rows.
        progress: optional (index, total, rev, status) -> None callback.
            status is "hit", "fetched", or a short error label.
        on_error: optional (rev, full_error_message) -> None callback.
            Use this to log full error text without trampling a progress bar.
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
            err_text = str(e)
            if on_error:
                on_error(rev, err_text)
            else:
                print(f"prefetch error {rev}: {err_text}", file=sys.stderr)
            if progress:
                progress(i, total, rev, "error")
            continue
        cache_put(rev, nar_hash)
        if apply_to_rows:
            apply_to_rows(rev, nar_hash)
        stats.fetched += 1
        if progress:
            progress(i, total, rev, "fetched")
    return stats
