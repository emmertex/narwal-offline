#!/usr/bin/env python3
"""pull_wc.py — instrumented multi-attempt pull of one big robot file."""
import json
import sys
import time

sys.path.insert(0, "/home/ku7/git/narwal-offline/tools")
import narwal_access as na

PATH = "/oem/DISK/bin/pita/wireless_communicator/bin/wireless_communicator_node"
OUT = "/home/ku7/git/narwal-offline/robot_dump/wireless_communicator_node"

tok = json.load(open("/home/ku7/git/narwal-offline/tools/narwal_tokens.json"))
link = na.RobotLink("EHf6cRNRGT", "0123456789abcdef0123456789abcdef",
                    tok["uuid"], tok["token"], na.BROKERS["us"])
link.connect()
try:
    for attempt in range(3):
        link.wake()
        time.sleep(2)
        # sanity ping: small read must work
        ping = link.read_file("/etc/openwrt_release", timeout=20)
        print(f"[{attempt}] ping openwrt_release: {0 if ping is None else len(ping)}B", flush=True)
        t0 = time.time()
        d = link.read_file(PATH, timeout=150)
        print(f"[{attempt}] wc_node: {'None' if d is None else f'{len(d)}B'} in {time.time()-t0:.0f}s", flush=True)
        if d:
            open(OUT, "wb").write(d)
            print("saved", flush=True)
            break
finally:
    link.cli.loop_stop()
