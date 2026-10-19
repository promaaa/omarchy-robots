{
  description = "Omarchy Robots: a NixOS robot OS declared by one file, robot.nix";

  inputs = {
    # The release the pinned jetpack-nixos is developed against (its flake.nix).
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    jetpack = {
      url = "github:anduril/jetpack-nixos";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    disko = {
      url = "github:nix-community/disko";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      jetpack,
      disko,
    }:
    let
      inherit (nixpkgs) lib;
      version = "0.0.3";
      boards = import ./modules/boards.nix;
      jetsonBoards = lib.filterAttrs (_: board: board ? som) boards;
      pkgs = nixpkgs.legacyPackages.x86_64-linux.extend self.overlays.default;

      # Everything a robot system is made of; `target` names it in build.json.
      robotModules = target: [
        ./modules
        ./robot.nix
        jetpack.nixosModules.default
        disko.nixosModules.disko
        {
          nixpkgs.overlays = [ self.overlays.default ];
          omarchy.robot.build = {
            inherit target version;
            revision = self.rev or self.dirtyRev or "dirty";
            robotNix = builtins.hashFile "sha256" ./robot.nix;
            inputs = {
              nixpkgs = nixpkgs.rev;
              jetpack-nixos = jetpack.rev;
              disko = disko.rev;
            };
          };
        }
      ];
      robot = target: extraModules: lib.nixosSystem { modules = robotModules target ++ extraModules; };
      onBoard = board: { omarchy.robot.board = lib.mkForce board; };
      # NVIDIA's flashing tools are x86_64 binaries: like jetpack-nixos, build
      # the flash scripts from an x86_64 build platform.
      crossFromX86 = {
        nixpkgs.buildPlatform = "x86_64-linux";
      };
      flash =
        target: extraModules:
        (robot target (extraModules ++ [ crossFromX86 ])).config.system.build.flashScript;
      installerModules = target: [
        (import ./modules/installer.nix target)
        jetpack.nixosModules.default
        { nixpkgs.overlays = [ self.overlays.default ]; }
      ];
      installer =
        target: (lib.nixosSystem { modules = installerModules target; }).config.system.build.isoImage;
      perBoard =
        prefix: f: lib.mapAttrs' (board: _: lib.nameValuePair "${prefix}-${board}" (f board)) jetsonBoards;
      robotBoard = self.nixosConfigurations.robot.config.omarchy.robot.board;
    in
    {
      # .#robot is robot.nix as written; .#<board> is robot.nix on that board.
      nixosConfigurations = {
        robot = robot "robot" [ ];
      }
      // lib.mapAttrs (board: _: robot board [ (onBoard board) ]) boards;

      overlays.default = final: _: {
        omarchy-robot = final.callPackage ./pkgs/omarchy-robot { };
        omarchy-runtime = final.callPackage ./pkgs/omarchy-runtime {
          revision = self.rev or self.dirtyRev or null;
        };
        omarchy-branding = final.callPackage ./pkgs/omarchy-branding { };
      };

      packages =
        lib.recursiveUpdate
          {
            x86_64-linux = {
              inherit (pkgs) omarchy-robot omarchy-runtime;
              # The local S2 backend of `omarchy-runtime ttft-measure`: nixpkgs'
              # llama.cpp, as packaged (baseline x86-64: GGML_NATIVE off, no
              # AVX2), and the same built for AVX2 hosts (FMA, F16C, BMI2 too).
              inherit (pkgs) llama-cpp;
              llama-cpp-avx2 = pkgs.llama-cpp.overrideAttrs (old: {
                cmakeFlags =
                  old.cmakeFlags
                  ++ map (flag: lib.cmakeBool flag true) [
                    "GGML_AVX"
                    "GGML_AVX2"
                    "GGML_FMA"
                    "GGML_F16C"
                    "GGML_BMI2"
                  ];
              });
              vm = self.nixosConfigurations.vm.config.system.build.vm;
              default = self.packages.x86_64-linux.vm;
              installer-vm = installer self.nixosConfigurations.vm;
              installer-pc = installer self.nixosConfigurations.pc;
              # The S2 placement experiment, every scenario, profile and S2 time
              # to first token: two VMs, an emulated network. Wants KVM.
              s2-placement = pkgs.testers.runNixOSTest (
                import ./tests/s2-placement.nix {
                  inherit pkgs robotModules onBoard;
                  full = true;
                }
              );
            }
            # The same for one scenario at a time, with the measured S2 TTFT
            # distributions (ttft-measure, data/latency: the local model on the
            # host, llama.cpp as packaged and built for AVX2) besides 0.1, 1 and
            # 5 s, every cell run 6 times: the repeated matrix, one CI job each.
            // lib.genAttrs' [ "s2_placement" "cup_moved" "slot_blocked" "fallback_expiry" ] (
              scenario:
              lib.nameValuePair "s2-placement-repeated-${scenario}" (
                pkgs.testers.runNixOSTest (
                  import ./tests/s2-placement.nix {
                    inherit pkgs robotModules onBoard;
                    full = true;
                    scenarios = [ scenario ];
                    measured = [
                      "qwen2.5-1.5b-q4km-host"
                      "qwen2.5-1.5b-q4km-host-avx2"
                    ];
                    repetitions = 6;
                  }
                )
              )
            )
            // perBoard "flash" (board: flash board [ (onBoard board) ]);
            aarch64-linux = perBoard "installer" (board: installer self.nixosConfigurations.${board}) // {
              # Pure Python and numpy: the same package for a Jetson.
              inherit ((nixpkgs.legacyPackages.aarch64-linux.extend self.overlays.default)) omarchy-runtime;
            };
          }
          # .#flash and .#installer: the bring-up artifacts for robot.nix as written.
          (
            if !(jetsonBoards ? ${robotBoard}) then
              { x86_64-linux.installer = installer self.nixosConfigurations.robot; }
            else
              {
                x86_64-linux.flash = flash "robot" [ ];
                aarch64-linux.installer = installer self.nixosConfigurations.robot;
              }
          );

      checks.x86_64-linux = import ./tests {
        inherit
          self
          pkgs
          lib
          robot
          robotModules
          installerModules
          onBoard
          jetsonBoards
          version
          ;
      };

      formatter.x86_64-linux = pkgs.nixfmt-tree;

      devShells.x86_64-linux.default = pkgs.mkShell {
        packages = with pkgs; [
          deadnix
          nixfmt
          (python3.withPackages (ps: [
            ps.mypy
            ps.numpy
          ]))
          ruff
          statix
        ];
      };
    };
}
