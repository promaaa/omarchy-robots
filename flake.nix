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
      version = "0.1.0";
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
    in
    {
      # .#robot is robot.nix as written; .#<board> is robot.nix on that board.
      nixosConfigurations = {
        robot = robot "robot" [ ];
      }
      // lib.mapAttrs (board: _: robot board [ (onBoard board) ]) boards;

      overlays.default = final: _: {
        omarchy-robot = final.callPackage ./pkgs/omarchy-robot { };
      };

      packages.x86_64-linux = {
        inherit (pkgs) omarchy-robot;
        vm = self.nixosConfigurations.vm.config.system.build.vm;
        default = self.packages.x86_64-linux.vm;
      };

      checks.x86_64-linux = import ./tests {
        inherit
          self
          pkgs
          lib
          robot
          robotModules
          onBoard
          jetsonBoards
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
