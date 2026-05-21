"""Crawl open nixpkgs PRs and yield PendingPackage rows.

Strategy: hit the GitHub search API filtered by repo + label + state:open.
Pages return up to 100 items each and include `body`/`labels`/`draft`, so
most metadata comes for free.

The search response does NOT include the PR head commit SHA, so to record a
pinnable rev we follow up with one /repos/.../pulls/N request per PR. That
call is short-circuited when the caller already knows the rev is fresh.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable

import httpx

from .models import PendingPackage
from .parser import parse_pr_title

GITHUB_API = "https://api.github.com"
NEW_PACKAGE_LABEL = "8.has: package (new)"
MERGE_READY_LABEL = "2.status: merge-bot eligible"


@dataclass
class CrawlStats:
    prs_seen: int = 0
    packages_seen: int = 0
    pages: int = 0
    head_lookups: int = 0
    head_lookups_skipped: int = 0
    errors: int = 0
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


# HeadResolver returns the head SHA for a PR, given the PR number and the
# `updated_at` field the search API returned. Returning None means "skip head
# SHA resolution for this PR" (typically: we already have a fresh value).
HeadResolver = Callable[[int, str], str | None]


class NixpkgsPRCrawler:
    """Fetches open new-package PRs from the GitHub search API."""

    def __init__(
        self,
        token: str | None = None,
        client: httpx.Client | None = None,
        sleep_between_pages: float = 1.5,
        sleep_between_pr_lookups: float = 0.05,
    ):
        self.token = token or os.environ.get("GITHUB_TOKEN")
        if not self.token:
            raise ValueError(
                "GITHUB_TOKEN is required - export a GitHub personal access token "
                "(no scopes needed for public repo search)."
            )
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "prpkgs-crawler",
        }
        self.client = client or httpx.Client(headers=headers, timeout=30.0)
        self._owns_client = client is None
        self.sleep_between_pages = sleep_between_pages
        self.sleep_between_pr_lookups = sleep_between_pr_lookups
        self.stats = CrawlStats()

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "NixpkgsPRCrawler":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # public API

    def crawl(
        self,
        label: str = NEW_PACKAGE_LABEL,
        max_results: int = 2000,
        head_resolver: HeadResolver | None = None,
        on_package: Callable[[PendingPackage], None] | None = None,
    ) -> CrawlStats:
        for pkg in self.iter_packages(
            label=label, max_results=max_results, head_resolver=head_resolver
        ):
            if on_package:
                on_package(pkg)
        return self.stats

    def iter_packages(
        self,
        label: str = NEW_PACKAGE_LABEL,
        max_results: int = 2000,
        head_resolver: HeadResolver | None = None,
    ) -> Iterable[PendingPackage]:
        query = f'repo:NixOS/nixpkgs is:pr is:open label:"{label}"'
        for pr in self._iter_search(query, max_results=max_results):
            self.stats.prs_seen += 1
            head_rev = self._resolve_head(pr, head_resolver)
            for pkg in self._pr_to_packages(pr, head_rev=head_rev):
                self.stats.packages_seen += 1
                yield pkg

    def fetch_head_sha(self, pr_number: int) -> str | None:
        """Hit /repos/NixOS/nixpkgs/pulls/N and return head.sha. None on failure."""
        resp = self._get_with_retry(f"{GITHUB_API}/repos/NixOS/nixpkgs/pulls/{pr_number}")
        if resp is None:
            self.stats.errors += 1
            return None
        data = resp.json()
        sha = (data.get("head") or {}).get("sha")
        if not sha:
            self.stats.errors += 1
            return None
        return sha

    # internals

    def _resolve_head(self, pr: dict, head_resolver: HeadResolver | None) -> str | None:
        pr_number = pr["number"]
        pr_updated_at = (pr.get("updated_at") or "")[:19]
        if head_resolver is not None:
            cached = head_resolver(pr_number, pr_updated_at)
            if cached:
                self.stats.head_lookups_skipped += 1
                return cached
        sha = self.fetch_head_sha(pr_number)
        if sha is not None:
            self.stats.head_lookups += 1
            time.sleep(self.sleep_between_pr_lookups)
        return sha

    def _iter_search(self, query: str, max_results: int) -> Iterable[dict]:
        page = 1
        per_page = 100
        fetched = 0
        while fetched < max_results:
            resp = self._get_with_retry(
                f"{GITHUB_API}/search/issues",
                params={
                    "q": query,
                    "per_page": per_page,
                    "page": page,
                    "sort": "updated",
                    "order": "desc",
                    "advanced_search": "true",
                },
            )
            if resp is None:
                self.stats.errors += 1
                return

            self.stats.pages += 1
            data = resp.json()
            items = data.get("items", [])
            if not items:
                return
            for item in items:
                yield item
                fetched += 1
                if fetched >= max_results:
                    return
            if len(items) < per_page:
                return
            page += 1
            if page > 10:  # github search caps at 1000 results
                return
            time.sleep(self.sleep_between_pages)

    def _get_with_retry(self, url: str, params: dict | None = None, max_tries: int = 6):
        for attempt in range(max_tries):
            try:
                resp = self.client.get(url, params=params)
            except (
                httpx.ReadTimeout,
                httpx.ConnectTimeout,
                httpx.ReadError,
                httpx.ConnectError,
            ) as e:
                # transient network glitch - back off and retry
                wait = min(5 * (attempt + 1), 60)
                print(f"network error on {url}: {e!r}; retry in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if resp.status_code == 200:
                return resp
            if resp.status_code == 404:
                return None
            if resp.status_code in (403, 429):
                reset = resp.headers.get("X-RateLimit-Reset")
                if reset:
                    wait = max(int(reset) - int(time.time()), 5)
                else:
                    wait = min(60 * (attempt + 1), 300)
                time.sleep(min(wait, 300))
                continue
            if 500 <= resp.status_code < 600:
                time.sleep(2**attempt)
                continue
            return None
        return None

    def _pr_to_packages(self, pr: dict, head_rev: str | None = None) -> list[PendingPackage]:
        title = pr.get("title", "")
        label_names = [lbl.get("name", "") for lbl in pr.get("labels", [])]
        merge_ready = MERGE_READY_LABEL in label_names
        draft = bool(pr.get("draft", False))
        author = (pr.get("user") or {}).get("login") or "unknown"
        body = pr.get("body") or ""
        if body and len(body) > 4000:
            body = body[:4000] + "..."

        parsed = parse_pr_title(title)
        packages: list[PendingPackage] = []
        for entry in parsed:
            packages.append(
                PendingPackage(
                    pr_number=pr["number"],
                    name=entry.name,
                    attr_path=entry.attr_path,
                    version=entry.version,
                    author=author,
                    pr_url=pr.get("html_url", ""),
                    pr_title=title,
                    pr_body=body,
                    state=pr.get("state", "open"),
                    labels=label_names,
                    draft=draft,
                    merge_ready=merge_ready,
                    head_rev=head_rev,
                    pr_created_at=(pr.get("created_at") or "")[:19],
                    pr_updated_at=(pr.get("updated_at") or "")[:19],
                )
            )
        return packages
