# SPDX-FileCopyrightText: 2026 Philip Eklöf
#
# SPDX-License-Identifier: MIT

{
  description = "YubiKey PIV key generator compatible with age-plugin-yubikey";

  # flake.lock pins nixpkgs (python, uv, ruff, reuse, pcsclite, swig) and the
  # uv2nix tooling; uv.lock pins every Python dependency by version and hash.
  # uv2nix turns uv.lock into Nix derivations, so nothing is resolved or
  # downloaded outside of these two lock files.
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      ...
    }:
    let
      inherit (nixpkgs) lib;

      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = f: lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };

      overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };

      # pyscard has no Linux wheels. Build its sdist the same way nixpkgs does:
      # with swig from nixpkgs (the PyPI swig binary does not run on NixOS) and
      # libpcsclite referenced by store path.
      pyprojectOverrides =
        pkgs: final: prev:
        lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
          pyscard = prev.pyscard.overrideAttrs (old: {
            nativeBuildInputs =
              old.nativeBuildInputs
              ++ final.resolveBuildSystem { setuptools = [ ]; }
              ++ [
                pkgs.swig
                pkgs.pkg-config
              ];
            buildInputs = (old.buildInputs or [ ]) ++ [ pkgs.pcsclite ];
            postPatch = (old.postPatch or "") + ''
              substituteInPlace pyproject.toml \
                --replace-fail 'requires = ["setuptools","swig"]' 'requires = ["setuptools"]'
              substituteInPlace setup.py --replace-fail "pkg-config" "$PKG_CONFIG"
              substituteInPlace src/smartcard/scard/winscarddll.c \
                --replace-fail "libpcsclite.so.1" "${lib.getLib pkgs.pcsclite}/lib/libpcsclite.so"
            '';
          });
        };

      pythonSets = forAllSystems (
        pkgs:
        (pkgs.callPackage pyproject-nix.build.packages { python = pkgs.python313; }).overrideScope (
          lib.composeManyExtensions [
            pyproject-build-systems.overlays.wheel
            overlay
            (pyprojectOverrides pkgs)
          ]
        )
      );
    in
    {
      devShells = forAllSystems (
        pkgs:
        let
          pythonSet = pythonSets.${pkgs.stdenv.hostPlatform.system}.overrideScope (
            workspace.mkEditablePyprojectOverlay { root = "$REPO_ROOT"; }
          );
          virtualenv = pythonSet.mkVirtualEnv "yk-piv-age-keygen-dev-env" workspace.deps.all;
        in
        {
          default = pkgs.mkShell {
            packages = [
              virtualenv
              pkgs.uv
              pkgs.ruff
              pkgs.reuse
            ];

            env = {
              # Keep uv from syncing its own venv or downloading interpreters;
              # use it only to edit and refresh uv.lock.
              UV_NO_SYNC = "1";
              UV_PYTHON = pythonSet.python.interpreter;
              UV_PYTHON_DOWNLOADS = "never";
            };

            shellHook = ''
              unset PYTHONPATH
              export REPO_ROOT=$(git rev-parse --show-toplevel)
            '';
          };
        }
      );
    };
}
