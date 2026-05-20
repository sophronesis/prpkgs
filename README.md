# prpkgs

aur for nixpkgs. an index of every package sitting in an open nixpkgs PR,
exposed as a flake so you can drop pending packages into your config the same
way you'd reference anything from `pkgs.`.

each entry is pinned by commit SHA and content hash, so evaluation is fully
pure - no `--impure` flag. pinning the prpkgs flake input to a specific
commit gives you a reproducible snapshot of every open PR at that moment.

## what it does

1. scrapes open nixpkgs PRs labelled `8.has: package (new)` via the github
   search api and stores them in a local sqlite db
2. for each PR records the head commit sha
3. prefetches each PR's nixpkgs tarball and stores the SRI narHash, keyed by
   commit sha so daily re-runs only touch PRs that moved
4. exports a `pending.nix` keyed by leaf package name -> `{ pr, rev, narHash,
   attr, ... }`
5. the flake reads `pending.nix` and exposes
   `prpkgs.packages.${system}.<name>` for each entry, fetched purely

## cli

```bash
uv pip install -e ".[dev]"
# or
nix develop

# pull every open new-package PR + each PR's head commit SHA
GITHUB_TOKEN=ghp_... prpkgs sync

# hash each PR's nixpkgs snapshot (slow first time, near-instant after)
prpkgs prefetch

# regenerate the file the flake imports
prpkgs export -o pending.nix

# look stuff up
prpkgs check tetro-tui      # exact match, exit 0/1 for scripts
prpkgs check -q tetro-tui   # silent
prpkgs search neovim
prpkgs list --pinned        # only entries that have a narHash
prpkgs show 521856          # PR details + build commands
prpkgs install tetro-tui    # print snippet to paste into a flake
prpkgs stats
```

## use as a flake (the main feature)

once `pending.nix` exists in this repo, point your config at it:

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    prpkgs = {
      url = "github:sophronesis/prpkgs";   # or path:/home/you/prpkgs
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, prpkgs }: {
    nixosConfigurations.host = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        ({ pkgs, ... }: {
          environment.systemPackages = [
            pkgs.firefox
            prpkgs.packages.${pkgs.stdenv.hostPlatform.system}.tetro-tui
          ];
        })
      ];
    };
  };
}
```

then `sudo nixos-rebuild switch`. no `--impure` needed.

want a specific historical version? pin to a commit:

```nix
prpkgs.url = "github:sophronesis/prpkgs/abcdef1234";
```

since each commit on `main` is a content-addressed snapshot of every open PR,
that gives you a totally reproducible build of any pending package at the
state it was in on that day.

### overlay

```nix
nixpkgs.overlays = [ prpkgs.overlays.default ];
```

after that `pkgs.tetro-tui` works regardless of whether nixpkgs has it.

### one-off build, no config edit

```bash
nix build 'github:NixOS/nixpkgs/<rev>#tetro-tui'    # use `prpkgs show N` to get the rev
```

### lib helper for nested attr paths

attr paths with dots (e.g. `python3Packages.foo`) aren't exposed as
top-level flake outputs. use the helper:

```nix
prpkgs.lib.fetchPRPackage {
  system = pkgs.stdenv.hostPlatform.system;
  rev = "abc123...";
  narHash = "sha256-...";
  attr = "python3Packages.foo";
}
```

`prpkgs.lib.pending` is the parsed attrset if you want to look the values up
programmatically.

## how it stays fresh

`.github/workflows/sync.yml` runs daily: `sync`, `prefetch`, `export`, commit
the new `pending.nix` if anything changed. the sqlite db is cached between
runs so prefetch only touches PRs whose head sha moved.

run it manually any time via the github actions `Run workflow` button, or
locally with the three commands above.

## data location

- index db: `~/.local/share/prpkgs/prpkgs.db`
- flake-readable export: `<repo>/pending.nix`

## caveats

- **prefetch is bandwidth-heavy**: each PR's nixpkgs tarball is ~150MB
  unpacked. first prefetch over ~500 PRs downloads ~75GB. subsequent runs
  only touch changed PRs - usually a handful per day
- **nested attrs** like `python3Packages.foo` need `lib.fetchPRPackage`
  rather than the top-level `packages.<system>.<name>` output
- **collisions**: when multiple PRs propose the same leaf name, export picks
  `has narHash > merge-ready > non-draft > most-recently-updated`. use the
  lib helper to pick a specific PR
- **closed/merged PRs**: pruned on next sync. if you pinned an old commit of
  prpkgs, that snapshot still builds as long as the commit is reachable via
  `archive/<sha>.tar.gz` on github (usually forever, but PRs from deleted
  forks can disappear)

## license

MIT
