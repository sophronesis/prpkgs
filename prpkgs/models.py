"""Data models for prpkgs."""

from dataclasses import dataclass, field


@dataclass
class PendingPackage:
    """A package waiting in an open nixpkgs PR."""

    pr_number: int
    name: str
    author: str
    pr_url: str
    pr_title: str
    pr_created_at: str
    attr_path: str = ""
    version: str | None = None
    pr_body: str | None = None
    state: str = "open"
    labels: list[str] = field(default_factory=list)
    draft: bool = False
    merge_ready: bool = False
    pr_updated_at: str | None = None
    head_rev: str | None = None
    nar_hash: str | None = None
    id: int | None = None

    @property
    def display_name(self) -> str:
        return self.attr_path or self.name

    @property
    def build_ref(self) -> str:
        """flake ref that builds this package straight from the PR."""
        return f"github:NixOS/nixpkgs/pull/{self.pr_number}/head#{self.display_name}"

    @property
    def nix_shell_url(self) -> str:
        """tarball URL for `-I nixpkgs=...`."""
        return f"https://github.com/NixOS/nixpkgs/archive/pull/{self.pr_number}/head.tar.gz"
