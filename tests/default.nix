# Everything `nix flake check` runs on x86_64-linux.
{
  self,
  pkgs,
  lib,
  robot,
  robotModules,
  installerModules,
  onBoard,
  jetsonBoards,
  version,
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
  # jetpack-nixos's, CUDA stays off nixpkgs-wide, and the system and its flash
  # script evaluate. They are the flake's own outputs, evaluated once.
  evaluates =
    board: spec:
    let
      system = self.nixosConfigurations.${board};
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
    pkgs.writeText "eval-${board}" (
      lib.concatMapStrings (drv: builtins.unsafeDiscardStringContext drv.drvPath + "\n") [
        config.system.build.toplevel
        self.packages.x86_64-linux."flash-${board}"
      ]
    );

  # With the GPU stack and a power mode, once per JetPack major (the stack is
  # the same across its boards): the unfree predicate covers CUDA and
  # TensorRT, and power.mode reaches nvpmodel.
  evaluatesWithGpu =
    board:
    let
      inherit
        (self.nixosConfigurations.${board}.extendModules {
          modules = [
            {
              omarchy.robot.gpu.enable = lib.mkForce true;
              omarchy.robot.power.mode = lib.mkForce 0;
            }
          ];
        })
        config
        ;
    in
    assert lib.assertMsg (
      config.services.nvpmodel.profileNumber == 0
    ) "${board}: power.mode does not reach nvpmodel";
    pkgs.writeText "eval-gpu-${board}" (
      builtins.unsafeDiscardStringContext config.system.build.toplevel.drvPath
    );

  # With the runtime enabled: installed, named in build.json, and the system
  # still evaluates (for a Jetson, the aarch64 build of the same package).
  evaluatesWithRuntime =
    board:
    let
      system = self.nixosConfigurations.${board}.extendModules {
        modules = [ { omarchy.robot.runtime.enable = lib.mkForce true; } ];
      };
      inherit (system) config;
      runtime = system.pkgs.omarchy-runtime;
      build = builtins.fromJSON config.environment.etc."omarchy-robot/build.json".text;
    in
    assert lib.assertMsg (lib.elem runtime config.environment.systemPackages)
      "${board}: omarchy-runtime is not installed";
    assert lib.assertMsg (
      build.runtime.version == runtime.version
    ) "${board}: build.json does not name the runtime";
    pkgs.writeText "eval-runtime-${board}" (
      builtins.unsafeDiscardStringContext config.system.build.toplevel.drvPath
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

  # The runtime: lint, strict types, and every test, the whole scenario matrix
  # included (simulated, no VM). Its version is the release's.
  omarchy-runtime =
    assert lib.assertMsg (
      pkgs.omarchy-runtime.version == version
    ) "pkgs/omarchy-runtime/pyproject.toml has ${pkgs.omarchy-runtime.version}, flake.nix ${version}";
    check "omarchy-runtime"
      [
        pkgs.ruff
        (pkgs.python3.withPackages (ps: [
          ps.mypy
          ps.numpy
        ]))
        pkgs.omarchy-runtime
      ]
      ''
        cd pkgs/omarchy-runtime
        export PYTHONDONTWRITEBYTECODE=1
        ruff check --no-cache .
        ruff format --check --no-cache .
        mypy --cache-dir=/dev/null omarchy_runtime tests
        python3 -m unittest discover -s tests -t . -v
        omarchy-runtime --version
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
  refuses-pc-without-keys = refuses "pc-without-keys" "needs at least one key" {
    board = "pc";
    admin.sshKeys = [ ];
  };
  refuses-gpu-on-pc = refuses "gpu-on-pc" "gpu.enable is for Jetson boards" {
    board = "pc";
    gpu.enable = true;
  };
  refuses-power-mode-on-pc = refuses "power-mode-on-pc" "power.mode selects a Jetson nvpmodel mode" {
    board = "pc";
    power.mode = 0;
  };

  # The pc board: an x86_64 system with NixOS's generic hardware support and
  # microcode, no Jetson layer, its installer image, and the NVMe default disk.
  eval-pc =
    let
      inherit (self.nixosConfigurations.pc) config;
      expect = cond: what: lib.assertMsg cond "pc: ${what}";
    in
    assert expect (config.nixpkgs.hostPlatform.system == "x86_64-linux") "not x86_64";
    assert expect (!config.hardware.nvidia-jetpack.enable) "jetpack-nixos is enabled";
    assert expect (
      config.hardware.enableAllHardware && config.hardware.enableRedistributableFirmware
    ) "generic hardware support or firmware is off";
    assert expect (
      config.hardware.cpu.intel.updateMicrocode && config.hardware.cpu.amd.updateMicrocode
    ) "CPU microcode is off";
    assert expect (lib.elem "nvme" config.boot.initrd.availableKernelModules) "no nvme in the initrd";
    assert expect (
      config.omarchy.robot.storage.device == "/dev/nvme0n1"
    ) "storage.device is not /dev/nvme0n1";
    pkgs.writeText "eval-pc" (
      lib.concatMapStrings (drv: builtins.unsafeDiscardStringContext drv.drvPath + "\n") [
        config.system.build.toplevel
        self.packages.x86_64-linux.installer-pc
      ]
    );
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
        version
        ;
    }
  );
  vm-installer = pkgs.testers.runNixOSTest (
    import ./installer.nix {
      inherit
        pkgs
        lib
        robot
        installerModules
        ;
      board = "vm";
    }
  );
  vm-installer-pc = pkgs.testers.runNixOSTest (
    import ./installer.nix {
      inherit
        pkgs
        lib
        robot
        installerModules
        ;
      board = "pc";
    }
  );
  vm-update-rollback = pkgs.testers.runNixOSTest (
    import ./update-rollback.nix { inherit robotModules onBoard; }
  );
  vm-runtime = pkgs.testers.runNixOSTest (
    import ./runtime.nix { inherit pkgs robotModules onBoard; }
  );
  # The S2 placement experiment, reduced: 3 network profiles x 2 S2 times to
  # first token. The full matrix is the flake output s2-placement.
  vm-s2-placement = pkgs.testers.runNixOSTest (
    import ./s2-placement.nix { inherit pkgs robotModules onBoard; }
  );
  eval-runtime-orin-agx-devkit = evaluatesWithRuntime "orin-agx-devkit";
  eval-runtime-pc = evaluatesWithRuntime "pc";
  eval-gpu-orin-agx-devkit = evaluatesWithGpu "orin-agx-devkit";
  eval-gpu-thor-agx-devkit = evaluatesWithGpu "thor-agx-devkit";
}
// lib.mapAttrs' (
  board: spec: lib.nameValuePair "eval-${board}" (evaluates board spec)
) jetsonBoards
