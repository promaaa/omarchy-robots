# Everything `nix flake check` runs on x86_64-linux.
{
  self,
  pkgs,
  lib,
  robot,
  robotModules,
  onBoard,
  jetsonBoards,
}:
let
  check =
    name: inputs: script:
    pkgs.runCommand "check-${name}" { nativeBuildInputs = inputs; } ''
      cd ${self}
      ${script}
      touch $out
    '';

  # Per board: jetpack-nixos receives the mapped settings, the kernel is
  # jetpack-nixos's, CUDA stays off nixpkgs-wide, and the system evaluates,
  # also with the GPU stack and a power mode (where the unfree predicate is
  # exercised).
  evaluates =
    board: spec:
    let
      system = robot board [ (onBoard board) ];
      gpu = robot board [
        (onBoard board)
        {
          omarchy.robot.gpu.enable = lib.mkForce true;
          omarchy.robot.power.mode = lib.mkForce 0;
        }
      ];
      inherit (system) config;
      jp = config.hardware.nvidia-jetpack;
      expect = cond: what: lib.assertMsg cond "${board}: ${what}";
    in
    assert expect jp.enable "jetpack-nixos is not enabled";
    assert expect (
      jp.som == spec.som && jp.carrierBoard == spec.carrierBoard && jp.super == spec.super
    ) "som/carrierBoard/super are ${jp.som}/${jp.carrierBoard}/${lib.boolToString jp.super}";
    assert expect (
      config.boot.kernelPackages.kernel.drvPath
      == system.pkgs.nvidia-jetpack.kernelPackages.kernel.drvPath
    ) "the kernel is not jetpack-nixos's";
    assert expect (
      !jp.configureCuda && !(system.pkgs.config.cudaSupport or false)
    ) "CUDA is enabled across nixpkgs";
    assert expect (
      gpu.config.services.nvpmodel.profileNumber == 0
    ) "power.mode does not reach nvpmodel";
    pkgs.writeText "eval-${board}" (
      lib.concatMapStrings (drv: builtins.unsafeDiscardStringContext drv.drvPath + "\n") [
        config.system.build.toplevel
        gpu.config.system.build.toplevel
      ]
    );

  # Invalid robot.nix combinations fail the assertion that explains why.
  refuses =
    name: message: settings:
    let
      inherit ((robot "refuses" [ { omarchy.robot = lib.mapAttrsRecursive (_: lib.mkForce) settings; } ]))
        config
        ;
      failed = map (a: a.message) (lib.filter (a: !a.assertion) config.assertions);
    in
    assert lib.assertMsg (lib.any (lib.hasInfix message) failed)
      "${name}: no failed assertion says \"${message}\" (failed: ${toString failed})";
    pkgs.writeText "refuses-${name}" (lib.concatLines failed);
in
{
  format = check "format" [ pkgs.nixfmt ] "nixfmt --check $(find . -name '*.nix')";
  statix = check "statix" [ pkgs.statix ] "statix check .";
  deadnix = check "deadnix" [ pkgs.deadnix ] "deadnix --fail .";

  omarchy-robot =
    check "omarchy-robot"
      [
        pkgs.ruff
        pkgs.mypy
        pkgs.python3Minimal
      ]
      ''
        cd pkgs/omarchy-robot
        ruff check --no-cache .
        mypy --strict --cache-dir=/dev/null omarchy_robot.py test_omarchy_robot.py
        python3 -m unittest -v test_omarchy_robot
      '';

  # Defaults of the installed system that the QEMU test VMs override.
  defaults =
    let
      inherit (self.nixosConfigurations.robot) config;
    in
    assert lib.assertMsg config.services.timesyncd.enable "time synchronisation is off";
    assert lib.assertMsg (
      config.fileSystems."/".device == "/dev/disk/by-partlabel/disk-main-root"
    ) "root is not the disko partition on storage.device";
    pkgs.writeText "defaults" "";

  # The board type itself rejects unknown boards.
  refuses-unknown-board =
    assert lib.assertMsg (
      !(builtins.tryEval (robot "refuses" [ (onBoard "jetson-nano") ]).config.omarchy.robot.board).success
    ) "an unknown board evaluated";
    pkgs.writeText "refuses-unknown-board" "";
  refuses-jetson-without-keys = refuses "jetson-without-keys" "needs at least one key" {
    board = "orin-agx-devkit";
    admin.sshKeys = [ ];
  };
  refuses-gpu-on-vm = refuses "gpu-on-vm" "gpu.enable is for Jetson boards" {
    board = "vm";
    gpu.enable = true;
  };
  refuses-power-mode-on-vm = refuses "power-mode-on-vm" "power.mode selects a Jetson nvpmodel mode" {
    board = "vm";
    power.mode = 0;
  };
  refuses-gateway-without-address = refuses "gateway-without-address" "go with a static .address" {
    network.gateway = "192.168.1.1";
  };

  vm-base = pkgs.testers.runNixOSTest (
    import ./base.nix {
      inherit
        self
        pkgs
        robotModules
        onBoard
        ;
    }
  );
  vm-update-rollback = pkgs.testers.runNixOSTest (
    import ./update-rollback.nix { inherit robotModules onBoard; }
  );
}
// lib.mapAttrs' (
  board: spec: lib.nameValuePair "eval-${board}" (evaluates board spec)
) jetsonBoards
