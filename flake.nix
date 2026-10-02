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
      version = "0.0.1";
      boards = import ./modules/boards.nix;
      jetsonBoards = removeAttrs boards [ "vm" ];
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
        omarchy-branding = final.callPackage ./pkgs/omarchy-branding { };
      };

      packages =
        lib.recursiveUpdate
          {
            x86_64-linux = {
              inherit (pkgs) omarchy-robot;
              vm = self.nixosConfigurations.vm.config.system.build.vm;
              default = self.packages.x86_64-linux.vm;
              installer-vm = installer self.nixosConfigurations.vm;
            }
            // perBoard "flash" (board: flash board [ (onBoard board) ]);
            aarch64-linux = perBoard "installer" (board: installer self.nixosConfigurations.${board});
          }
          # .#flash and .#installer: the bring-up artifacts for robot.nix as written.
          (
            if robotBoard == "vm" then
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
          mypy
          nixfmt
          python3
          ruff
          statix
        ];
      };
    };
}
