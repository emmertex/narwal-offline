# Sentinel/endpoint experiment — 2026-11-18 00:39–00:52

Window: dnsmasq hijack (all narwaltech.com + sentinel names → 10.2.100.2),
UDR DHCP DNS → VM, robot rebooted into it. Reverted at ~00:49; robot
self-recovered on the real cloud by 00:58 (verified via MQTT read).

## Result: NEGATIVE for bootstrap trust; endpoint question stays moot

1. **The bootstrap client (`iot-auth-global.narwaltech.com`) uses its own
   trust store.** With our CA in the system bundle (verified present
   earlier that night), every TLS handshake to the emulator's leaf was
   rejected with `unknown_ca` — strictly, every ~5 s, for ~10 minutes.
   No strict→lenient flip (unlike us-open's known fallback).
2. **All other robot channels are gated behind bootstrap at boot.** DNS
   census while hijacked (query counts): us.device 588, us-open 160,
   us-01.mqtt 153, us-open-data 82, iot-auth-global 58, us-sweeper 37 —
   but the ONLY TCP connections were bootstrap retries (tcpdump: SYNs to
   :443 only, SNI = iot-auth-global). us-01.mqtt was resolved but never
   connected → **the robot has a default/compiled-in MQTT endpoint**;
   a bootstrap-provided endpoint override can't be tested until bootstrap
   itself is trusted — circular with the itls blocker.
3. **New endpoint discovered**: `narwal-robot-logs-06.oss-us-west-1.
   aliyuncs.com` (144 queries) — the robot's log-upload OSS bucket, and
   it tried to reach it even while cloud-offline. Next-session lead:
   drive the app's "upload log" flow (`/sweeper-log-server/v2/file/
   getUploadToken` + robot gather/upload) as an account holder and read
   the uploaded logs — they may contain the communicator's TLS errors
   (cert paths) without needing the binary.
4. **IPv6-off verified live in the wild**: the robot queried AAAA, got
   NODATA from the hijacked dnsmasq, and cleanly used the A record —
   the old "v6 leak to real cloud" failure mode is gone.
5. Sentinels were never queried by the robot (bootstrap never
   succeeded). One LAN box (g1-cachyos, 10.2.102.66) did query
   bootstrap-sentinel after hitting the emulator — the emulator
   bootstrap/login responses ARE honored by non-robot clients, so the
   sentinel plumbing works.

Evidence: `robot_dnsmasq.log`, `emulator_sni.log` (this dir). VM state
after: dnsmasq disarmed (overrides commented), offline mosquitto now
also listens plaintext :1884, emulator has the sentinel bootstrap.
