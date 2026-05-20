{
  description = "prpkgs - aur-like index of packages waiting in open nixpkgs PRs";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f system);

      pendingPath = ./pending.nix;
      pending =
        if builtins.pathExists pendingPath
        then import pendingPath
        else { };

      # Pure path: tarball pinned by rev + narHash, no --impure needed.
      mkPure = system: { rev, narHash, attr, ... }:
        let
          src = builtins.fetchTarball {
            url = "https://github.com/NixOS/nixpkgs/archive/${rev}.tar.gz";
            sha256 = narHash;
          };
          prPkgs = import src { inherit system; config.allowUnfree = true; };
          path = nixpkgs.lib.splitString "." attr;
          deep = nixpkgs.lib.attrByPath path null prPkgs;
        in
        if deep == null
        then throw "prpkgs: ${attr} not found in nixpkgs@${rev}"
        else deep;

      # Impure fallback for entries the indexer couldn't pin (still useful
      # while a daily CI run hasn't happened yet, or for manual lookups).
      mkImpure = system: { pr, attr, ... }:
        let
          src = builtins.fetchTarball {
            url = "https://github.com/NixOS/nixpkgs/archive/pull/${toString pr}/head.tar.gz";
          };
          prPkgs = import src { inherit system; config.allowUnfree = true; };
          path = nixpkgs.lib.splitString "." attr;
          deep = nixpkgs.lib.attrByPath path null prPkgs;
        in
        if deep == null
        then throw "prpkgs: ${attr} not found in nixpkgs#PR${toString pr}"
        else deep;

      mkPackage = system: entry:
        if (entry.impure or false)
        then mkImpure system entry
        else mkPure system entry;

      mkAllForSystem = system:
        builtins.mapAttrs (_: entry: mkPackage system entry) pending;
    in
    {
      # Drop `prpkgs.packages.${system}.tetro-tui` into your config.
      # Evaluates without --impure when pending.nix has rev+narHash for the entry.
      packages = forAllSystems mkAllForSystem;

      # Fold the pending packages into a regular pkgs set.
      overlays.default = final: prev:
        builtins.mapAttrs
          (_: entry: mkPackage final.stdenv.hostPlatform.system entry)
          pending;

      lib = {
        # Pure: caller supplies both rev and narHash (look them up in pending.nix
        # or run `nix store prefetch-tarball` yourself).
        fetchPRPackage = { system, rev, narHash, attr }:
          mkPure system { inherit rev narHash attr; };

        # Impure escape hatch: build straight from the live PR head.
        fetchPRPackageImpure = { system, pr, attr }:
          mkImpure system { inherit pr attr; };

        pending = pending;
      };

      devShells = forAllSystems (system:
        let pkgs = nixpkgs.legacyPackages.${system}; in
        {
          default = pkgs.mkShell {
            packages = with pkgs; [
              (python312.withPackages (ps: with ps; [ pip click httpx rich pytest ]))
              uv
              ruff
              nix
            ];
            shellHook = ''
              export PYTHONPATH="$PWD:$PYTHONPATH"
              echo "prpkgs dev shell"
              echo "Run: python -m prpkgs --help"
            '';
          };
        });
    };
}
