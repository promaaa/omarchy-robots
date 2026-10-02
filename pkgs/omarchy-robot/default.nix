{ python3Minimal, runCommand }:
runCommand "omarchy-robot"
  {
    meta = {
      description = "Identity, bring-up validation, update and rollback for Omarchy Robots";
      mainProgram = "omarchy-robot";
    };
  }
  ''
    install -Dm755 ${./omarchy_robot.py} $out/bin/omarchy-robot
    substituteInPlace $out/bin/omarchy-robot \
      --replace-fail "#!/usr/bin/env python3" "#!${python3Minimal.interpreter}"
  ''
