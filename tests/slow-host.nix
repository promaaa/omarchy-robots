# Test VMs only. Without KVM, a loaded host can take longer than the test
# instrumentation's 300 s to show a VM its disk; wait up to 15 minutes.
{ lib, ... }:
{
  boot.initrd.systemd.settings.Manager.DefaultDeviceTimeoutSec = lib.mkForce 900;
  systemd.settings.Manager.DefaultDeviceTimeoutSec = lib.mkForce 900;
}
