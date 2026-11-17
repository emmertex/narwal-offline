#!/usr/bin/env python3
"""fsprobe.py — read many robot files in ONE MQTT session; save hits to robot_dump/.

The dynamic_params read service answers every request in one publish, so a
single broker connection can walk dozens of paths quickly. Non-existent
paths and directories come back as 0-byte replies.

Usage:
  python3 fsprobe.py paths.txt            # one absolute path per line, '#' comments
  python3 fsprobe.py -p /etc/inittab -p /etc/rc.local
  echo /etc/config/network | python3 fsprobe.py -

Hits land in ../robot_dump/<path with / -> _>, plus a probe_results.json
manifest (path, size, sha256) next to the dumps.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import narwal_access as na  # noqa: E402

DUMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "robot_dump")
MANIFEST = os.path.join(DUMP, "probe_results.json")


def load_paths(args) -> list[str]:
    if args.paths_file:
        src = open(args.paths_file) if args.paths_file != "-" else sys.stdin
        return [ln.strip() for ln in src
                if ln.strip() and not ln.strip().startswith("#")]
    return list(args.path or [])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths_file", nargs="?", help="file with one path per line ('-' = stdin)")
    ap.add_argument("-p", "--path", action="append", help="single path (repeatable)")
    ap.add_argument("--sleep", type=float, default=0.25, help="pause between reads")
    args = ap.parse_args()

    paths = load_paths(args)
    if not paths:
        sys.exit("no paths given")

    os.makedirs(DUMP, exist_ok=True)
    tok = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "narwal_tokens.json")))
    link = na.RobotLink("EHf6cRNRGT", "0123456789abcdef0123456789abcdef",
                        tok["uuid"], tok["token"], na.BROKERS["us"])
    link.connect()
    results = []
    try:
        link.wake()
        for p in paths:
            t0 = time.time()
            d = link.read_file(p, timeout=30)
            dt = time.time() - t0
            if d is None:
                results.append({"path": p, "status": "timeout"})
                print(f"  ?  {p} (timeout)")
            elif len(d) == 0:
                results.append({"path": p, "status": "empty"})
                print(f"  .  {p}")
            else:
                safe = p.strip("/").replace("/", "_")
                dest = os.path.join(DUMP, safe)
                with open(dest, "wb") as f:
                    f.write(d)
                sha = hashlib.sha256(d).hexdigest()
                results.append({"path": p, "status": "hit", "size": len(d),
                                "sha256": sha, "file": safe})
                print(f" HIT {p} ({len(d)}B, {dt:.1f}s) -> {safe}")
            time.sleep(args.sleep)
    finally:
        link.cli.loop_stop()

    old = []
    if os.path.exists(MANIFEST):
        try:
            old = json.load(open(MANIFEST))
        except Exception:
            old = []
    seen = {r["path"] for r in old}
    old.extend(r for r in results if r["path"] not in seen)
    json.dump(old, open(MANIFEST, "w"), indent=1)
    hits = sum(1 for r in results if r["status"] == "hit")
    print(f"\n{hits}/{len(results)} hits; manifest -> {MANIFEST}")


if __name__ == "__main__":
    main()
