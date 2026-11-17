# robot_dump/ — files pulled from the robot over MQTT (2026-11-17/18)

Everything here was read via `dynamic_params/config_content/get`
(`tools/narwal_access.py` / `tools/fsprobe.py`). `probe_results.json` is
the manifest (path, size, sha256 per pull). Filenames mirror robot paths
(`/` → `_`).

## What we learned pulling these

- **The read primitive never degraded** — the earlier "reads return
  empty" state was client-side bugs in `narwal_access.py` (malformed auth
  frame, wrong paho callback arity, no response-frame stripping). Fixed;
  reads are reliable again.
- **Read size ceiling**: ≥1.2 MB works (`librobokit_shark.so`,
  1.2 MB ≈ 76 s) — bigger files are slow, not capped. Node binaries need
  300–600 s timeouts.
- **Response-topic contention**: two concurrent app clients answering on
  the same `/response` topic cross-match replies. Never run two
  dynamic_params clients against one robot; matching is now
  correlation-strict in `narwal_access.py`.

## Layout of the robot (from these files)

- ROS Kinetic (Python 2.7) under `/opt/ros/kinetic`, roscore at
  127.0.0.1:11311, launched by `/oem/DISK/bin/pita/start.sh`
  (nodes: map_server, hal, hal_monitor, power_health, mapping, planning,
  application, perception, **wireless_communicator**, toolkit).
- Build tree (from `__FILE__` strings): `/root/mr813/src/robokit_framework/…`
- Custom libs in `/oem/DISK/bin/pita/lib/`: `librobokit_{framework,
  framework_common, shark, platform_auth, platform_device_info,
  platform_ssl_utils, robata_client_lib, robata_proto_lib}.so`,
  `libcommon.so`, `libpaho-mqtt3as.so` (all pulled).
- Logger: `RotateFileLogger` in framework_common writes
  `<dir>/<YYYYMMDDHHMM>.<n>.<name>` (from `%s/%04d%02d%02d%02d%02d.%d.%s`).
- Node log dirs: `/userdata/logs/{application,planning,perception,
  wireless_communicator,mqtt}/`.
- Partitions (boot log): rootfs, rootfs_data, oem, original, robotdata,
  userdata; U-Boot env on `/dev/by-name/env{,-redund}` (0x20000 each).
- IPv6 was disabled robot-side (`/etc/sysctl.conf` +
  `net.ipv6.conf.{all,default}.disable_ipv6=1`, original kept in
  `etc_sysctl.conf.orig`); verified off after reboot 2026-11-17 23:22.

## Highlights

| File | Why it matters |
|---|---|
| `oem_DISK_bin_pita_start.sh` | master launcher — full node map |
| `oem_DISK_bin_pita_env.sh` | ROS env, `LD_LIBRARY_PATH`, project name |
| `robotdata_static_robot_info_domain.ini` | every cloud endpoint the robot uses |
| `oem_DISK_bin_pita_robokit_platform_conf_auth_config.conf` | JWT login flow config |
| `usr_bin_swupdate` | OTA updater (stock SWUpdate, aes-key-file support) |
| `oem_DISK_bin_pita_toolkit_bin_toolkit_recorder` | first full node binary pulled |
| `oem_DISK_bin_pita_lib_librobokit_*` | the robokit platform layer (auth, device-info, robata client) |
| `tmp_.lastlog.boot` | full boot log from the 23:22 reboot |
| `oem_inventory_paths.txt` | the 150-file dynamic_params registry (deduped) |
