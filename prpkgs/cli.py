"""Command-line interface for prpkgs."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import __version__
from .crawler import NEW_PACKAGE_LABEL, NixpkgsPRCrawler
from .db import Database
from .export import export as render_pending_nix
from .models import PendingPackage
from .prefetch import PrefetchError, check_tools_available, prefetch_many

console = Console()
err_console = Console(stderr=True)


def _truncate(text: str | None, limit: int) -> str:
    if not text:
        return "-"
    text = text.replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _format_pkg_row(pkg: PendingPackage) -> tuple[str, ...]:
    flags = []
    if pkg.draft:
        flags.append("draft")
    if pkg.merge_ready:
        flags.append("ready")
    if pkg.nar_hash:
        flags.append("pinned")
    return (
        str(pkg.pr_number),
        pkg.attr_path or pkg.name,
        pkg.version or "-",
        pkg.author,
        (pkg.pr_updated_at or pkg.pr_created_at or "")[:10],
        ",".join(flags),
    )


def _packages_table(pkgs: list[PendingPackage], title: str) -> Table:
    table = Table(title=title)
    table.add_column("PR", style="dim", no_wrap=True)
    table.add_column("Package", style="cyan")
    table.add_column("Version", style="green")
    table.add_column("Author", style="blue")
    table.add_column("Updated")
    table.add_column("Status", style="yellow")
    for pkg in pkgs:
        table.add_row(*_format_pkg_row(pkg))
    return table


@click.group()
@click.version_option(__version__)
def main() -> None:
    """prpkgs - aur-like index of packages sitting in open nixpkgs PRs."""


@main.command()
@click.option("--label", default=NEW_PACKAGE_LABEL, help="GitHub label to filter PRs.")
@click.option("--max", "max_results", default=2000, type=int, help="Max search results.")
@click.option(
    "--prune/--no-prune",
    default=True,
    help="After sync, drop rows for PRs that didn't show up (closed/merged).",
)
@click.option(
    "--refresh-revs",
    is_flag=True,
    help="Re-fetch head SHA for every PR, even when updated_at hasn't changed.",
)
def sync(label: str, max_results: int, prune: bool, refresh_revs: bool) -> None:
    """Pull every open new-package PR into the local index.

    Also fetches each PR's head commit SHA, skipping PRs whose `updated_at`
    matches the previous sync (so a daily run typically hits the per-PR
    endpoint only for changed PRs).
    """
    try:
        crawler = NixpkgsPRCrawler()
    except ValueError as e:
        err_console.print(f"[red]error:[/red] {e}")
        sys.exit(1)

    sync_started = datetime.now().isoformat(timespec="seconds")
    with Database() as db, crawler:

        def head_resolver(pr_number: int, pr_updated_at: str) -> str | None:
            if refresh_revs:
                return None
            meta = db.get_pr_meta(pr_number)
            if not meta:
                return None
            if meta["head_rev"] and meta["pr_updated_at"] == pr_updated_at:
                return meta["head_rev"]
            return None

        def on_package(pkg: PendingPackage) -> None:
            db.upsert(pkg)

        console.print(f"syncing PRs labelled [cyan]{label}[/cyan] ...")
        stats = crawler.crawl(
            label=label,
            max_results=max_results,
            head_resolver=head_resolver,
            on_package=on_package,
        )

        pruned = 0
        if prune and stats.errors == 0 and stats.packages_seen > 0:
            pruned = db.prune_not_seen_since(sync_started)

        note = f"label={label} pruned={pruned}"
        db.record_sync(
            started_at=stats.started_at,
            prs_seen=stats.prs_seen,
            packages_seen=stats.packages_seen,
            errors=stats.errors,
            note=note,
        )

    console.print()
    console.print(f"[green]sync done[/green]  pages={stats.pages}")
    console.print(f"  PRs seen:        {stats.prs_seen}")
    console.print(f"  packages seen:   {stats.packages_seen}")
    console.print(f"  head fetches:    {stats.head_lookups}")
    console.print(f"  head from cache: {stats.head_lookups_skipped}")
    if prune:
        console.print(f"  pruned stale:    {pruned}")
    if stats.errors:
        console.print(f"  [red]errors:          {stats.errors}[/red]")


@main.command()
@click.option(
    "--max",
    "max_revs",
    default=0,
    type=int,
    help="Limit number of revs prefetched this run (0 = no limit).",
)
def prefetch(max_revs: int) -> None:
    """Compute SRI narHashes for every PR head still missing one.

    Hashes are cached by commit SHA, so this is incremental: PRs that haven't
    moved since the last prefetch are skipped.
    """
    try:
        check_tools_available()
    except PrefetchError as e:
        err_console.print(f"[red]error:[/red] {e}")
        sys.exit(1)

    with Database() as db:
        revs = db.revs_needing_hash()
        if not revs:
            console.print("[green]nothing to prefetch[/green] - every PR has a narHash")
            return
        if max_revs and len(revs) > max_revs:
            console.print(f"limiting to {max_revs} of {len(revs)} pending revs")
            revs = revs[:max_revs]

        console.print(f"prefetching [cyan]{len(revs)}[/cyan] tarballs ...")
        with Progress(
            SpinnerColumn(),
            TextColumn("{task.completed}/{task.total}  {task.description}"),
            TimeElapsedColumn(),
            console=console,
        ) as bar:
            task = bar.add_task("starting", total=len(revs))

            def on_progress(i: int, total: int, rev: str, status: str) -> None:
                bar.update(task, completed=i, description=f"{rev[:7]} {status}")

            stats = prefetch_many(
                revs,
                cache_get=db.cache_get,
                cache_put=db.cache_put,
                apply_to_rows=lambda rev, h: db.set_nar_hash_for_rev(rev, h),
                progress=on_progress,
            )

    console.print()
    console.print("[green]prefetch done[/green]")
    console.print(f"  cache hits:  {stats.hits}")
    console.print(f"  fetched:     {stats.fetched}")
    if stats.errors:
        console.print(f"  [red]errors:      {stats.errors}[/red]")
        for rev in stats.failed_revs[:5]:
            console.print(f"    - {rev}")
        if len(stats.failed_revs) > 5:
            console.print(f"    (+{len(stats.failed_revs) - 5} more)")
        sys.exit(1)


@main.command()
@click.argument("name")
@click.option("--quiet", "-q", is_flag=True, help="Suppress output, only set exit code.")
def check(name: str, quiet: bool) -> None:
    """Is PACKAGE waiting in an open PR? Exits 0 if yes, 1 if no."""
    with Database() as db:
        hits = db.check(name)

    if not hits:
        if not quiet:
            console.print(f"[yellow]not in prpkgs[/yellow]: {name}")
            console.print("  (run `prpkgs search` for fuzzy matches)")
        sys.exit(1)

    if quiet:
        sys.exit(0)

    for pkg in hits:
        flags = []
        if pkg.merge_ready:
            flags.append("[green]merge-ready[/green]")
        if pkg.draft:
            flags.append("[yellow]draft[/yellow]")
        if pkg.nar_hash:
            flags.append("[cyan]pinned[/cyan]")
        flag_str = " " + " ".join(flags) if flags else ""
        version = f" {pkg.version}" if pkg.version else ""
        console.print(
            f"[cyan]{pkg.attr_path or pkg.name}[/cyan]{version}  "
            f"PR [dim]#{pkg.pr_number}[/dim] by [blue]@{pkg.author}[/blue]{flag_str}"
        )
        console.print(f"  {pkg.pr_url}")
        if pkg.nar_hash and pkg.head_rev:
            console.print(
                f"  build: [dim]nix build "
                f"'github:NixOS/nixpkgs/{pkg.head_rev}#{pkg.attr_path or pkg.name}'[/dim]"
            )
        else:
            console.print(f"  build: [dim]nix build --impure '{pkg.build_ref}'[/dim]")
    sys.exit(0)


@main.command()
@click.argument("query")
@click.option("--limit", "-l", default=20, type=int, help="Max results.")
def search(query: str, limit: int) -> None:
    """Fuzzy / full-text search across the index."""
    with Database() as db:
        hits = db.search(query, limit=limit)
    if not hits:
        console.print(f"no matches for '{query}'")
        sys.exit(1)
    console.print(_packages_table(hits, f"search: {query}"))


@main.command(name="list")
@click.option("--limit", "-l", default=30, type=int, help="Max rows.")
@click.option("--ready", is_flag=True, help="Only show merge-ready PRs.")
@click.option("--no-drafts", is_flag=True, help="Hide draft PRs.")
@click.option("--pinned", is_flag=True, help="Only show packages with a stored narHash.")
def list_pkgs(limit: int, ready: bool, no_drafts: bool, pinned: bool) -> None:
    """List pending packages (most recently updated first)."""
    with Database() as db:
        hits = db.list_recent(
            limit=limit,
            merge_ready_only=ready,
            include_drafts=not no_drafts,
            only_with_hash=pinned,
        )
    if not hits:
        console.print("index is empty - run `prpkgs sync`")
        sys.exit(1)
    title = "merge-ready packages" if ready else "pending packages"
    console.print(_packages_table(hits, title))


@main.command()
@click.argument("pr_number", type=int)
def show(pr_number: int) -> None:
    """Show every package + build instructions for a single PR."""
    with Database() as db:
        hits = db.get_by_pr(pr_number)
    if not hits:
        console.print(f"[red]PR #{pr_number} not in the index[/red]")
        sys.exit(1)

    first = hits[0]
    header = f"[bold]#{first.pr_number}[/bold] - {first.pr_title}"
    sub = f"by [blue]@{first.author}[/blue] - {first.pr_url}"
    flags = []
    if first.merge_ready:
        flags.append("[green]merge-ready[/green]")
    if first.draft:
        flags.append("[yellow]draft[/yellow]")
    if first.nar_hash:
        flags.append("[cyan]pinned[/cyan]")
    if flags:
        sub += "  " + " ".join(flags)
    console.print(Panel.fit(f"{header}\n{sub}"))

    if first.head_rev:
        console.print(f"  head rev: [dim]{first.head_rev}[/dim]")
    if first.nar_hash:
        console.print(f"  narHash:  [dim]{first.nar_hash}[/dim]")

    table = Table(title="packages introduced")
    table.add_column("Attr path", style="cyan")
    table.add_column("Version", style="green")
    table.add_column("nix build", style="dim")
    pure = first.head_rev and first.nar_hash
    for pkg in hits:
        attr = pkg.attr_path or pkg.name
        if pure:
            build = f"nix build 'github:NixOS/nixpkgs/{pkg.head_rev}#{attr}'"
        else:
            build = f"nix build --impure '{pkg.build_ref}'"
        table.add_row(attr, pkg.version or "-", build)
    console.print(table)

    if first.labels:
        console.print()
        console.print("[bold]labels:[/bold] " + ", ".join(first.labels))

    if first.pr_body:
        console.print()
        console.print(Panel(_truncate(first.pr_body, 1200), title="PR body"))


@main.command()
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write pending.nix to PATH (defaults to stdout).",
)
@click.option(
    "--require-hash/--allow-unhashed",
    default=True,
    help="Skip rows without a narHash (the default keeps the flake pure).",
)
def export(output: Path | None, require_hash: bool) -> None:
    """Emit a Nix attrset that the prpkgs flake consumes.

    Run this after `prpkgs sync` + `prpkgs prefetch` to refresh the file the
    flake imports. The output is the source of truth for which PR-snapshot
    each pending package resolves to, so committing it produces a
    reproducible index commit per day.
    """
    with Database() as db:
        rendered, stats = render_pending_nix(db, require_hash=require_hash)
    if output is None:
        click.echo(rendered, nl=False)
        return
    output.write_text(rendered)
    console.print(
        f"[green]wrote[/green] {output}  "
        f"({stats['kept']} packages, skipped {stats['skipped_no_hash']} missing hash)"
    )
    if stats["skipped_no_hash"] and require_hash:
        console.print("[yellow]hint:[/yellow] run `prpkgs prefetch` to fill missing hashes")


@main.command()
@click.argument("name")
@click.option(
    "--input-url",
    default="github:sophronesis/prpkgs",
    help="Flake input URL to suggest in the install snippet.",
)
def install(name: str, input_url: str) -> None:
    """Print the snippet to drop a pending package into your flake."""
    with Database() as db:
        hits = db.check(name)
    if not hits:
        console.print(f"[red]not in prpkgs[/red]: {name}")
        console.print("  run `prpkgs sync` and try again, or `prpkgs search` for fuzzy hits")
        sys.exit(1)

    hits.sort(
        key=lambda p: (
            0 if p.nar_hash else 1,
            0 if p.merge_ready else 1,
            1 if p.draft else 0,
            -p.pr_number,
        )
    )
    pkg = hits[0]
    attr = pkg.attr_path or pkg.name
    leaf = attr.rsplit(".", 1)[-1]
    pure = bool(pkg.head_rev and pkg.nar_hash)

    header_flags = []
    if pkg.merge_ready:
        header_flags.append("[green]merge-ready[/green]")
    if pkg.nar_hash:
        header_flags.append("[cyan]pinned[/cyan]")
    flag_str = "  " + " ".join(header_flags) if header_flags else ""
    console.print(
        Panel.fit(
            f"[cyan]{attr}[/cyan]"
            + (f"  {pkg.version}" if pkg.version else "")
            + f"  PR [dim]#{pkg.pr_number}[/dim] by [blue]@{pkg.author}[/blue]"
            + flag_str
        )
    )
    console.print()
    console.print("[bold]1) Add the flake input[/bold]")
    console.print("    inputs.prpkgs = {")
    console.print(f'      url = "{input_url}";')
    console.print('      inputs.nixpkgs.follows = "nixpkgs";')
    console.print("    };")
    console.print()
    if "." in attr:
        console.print(
            "[yellow]note:[/yellow] nested attr path - use the lib helper rather "
            "than a top-level flake output."
        )
        console.print()
        console.print("[bold]2) Use the lib helper[/bold]")
        if pure:
            console.print(
                "    environment.systemPackages = [\n"
                "      (inputs.prpkgs.lib.fetchPRPackage {\n"
                "        system = pkgs.stdenv.hostPlatform.system;\n"
                f'        rev = "{pkg.head_rev}";\n'
                f'        narHash = "{pkg.nar_hash}";\n'
                f'        attr = "{attr}";\n'
                "      })\n"
                "    ];"
            )
        else:
            console.print(
                "    environment.systemPackages = [\n"
                "      (inputs.prpkgs.lib.fetchPRPackageImpure {\n"
                "        system = pkgs.stdenv.hostPlatform.system;\n"
                f"        pr = {pkg.pr_number};\n"
                f'        attr = "{attr}";\n'
                "      })\n"
                "    ];"
            )
    else:
        console.print("[bold]2) Reference the package[/bold]")
        console.print(
            "    environment.systemPackages = [\n"
            f"      inputs.prpkgs.packages.${{pkgs.stdenv.hostPlatform.system}}.{leaf}\n"
            "    ];"
        )

    console.print()
    if pure:
        console.print("[bold]3) Build normally (pure)[/bold]")
        console.print("    nixos-rebuild switch")
        console.print(f"    nix build '{input_url}#{leaf}'")
        console.print()
        console.print("[bold]direct build, no flake config edit:[/bold]")
        console.print(f"    nix build 'github:NixOS/nixpkgs/{pkg.head_rev}#{attr}'")
    else:
        console.print("[bold]3) Build with --impure[/bold]")
        console.print(
            "    No narHash on file yet - run `prpkgs prefetch` to make it pure.\n"
            "    Until then, add --impure to nix calls:"
        )
        console.print(f"    nix build --impure '{pkg.build_ref}'")


@main.command()
def stats() -> None:
    """Print index counts and last-sync info."""
    with Database() as db:
        s = db.stats()
    console.print(f"packages in index: [cyan]{s['packages']}[/cyan]")
    console.print(f"distinct PRs:      [cyan]{s['prs']}[/cyan]")
    console.print(f"merge-ready:       [green]{s['merge_ready']}[/green]")
    console.print(f"drafts:            [yellow]{s['drafts']}[/yellow]")
    console.print(f"with narHash:      [cyan]{s['with_hash']}[/cyan] / {s['packages']}")
    console.print(f"hash cache size:   {s['cache_size']} revs")
    last = s["last_sync"]
    if last:
        console.print(
            f"last sync:         {last['finished_at']} "
            f"(prs={last['prs_seen']}, pkgs={last['packages_seen']})"
        )
    else:
        console.print("last sync:         [yellow]never - run `prpkgs sync`[/yellow]")


if __name__ == "__main__":
    main()
