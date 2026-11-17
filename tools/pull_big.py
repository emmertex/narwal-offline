#!/usr/bin/env python3
"""pull_big.py — read one robot file with a long timeout, save to robot_dump/.

Usage: python3 pull_big.py /robot/path [--timeout 180] [--out name]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import narwal_access as na

DUMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "robot_dump")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--out")
    a = ap.parse_args()

    tok = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "narwal_tokens.json")))
    link = na.RobotLink("EHf6cRNRGT", "0123456789abcdef0123456789abcdef",
                        tok["uuid"], tok["token"], na.BROKERS["us"])
    link.connect()
    try:
        link.wake()
        d = link.read_file(a.path, timeout=a.timeout)
        print("result:", "None(timeout)" if d is None else f"{len(d)}B")
        if d:
            dest = os.path.join(DUMP, a.out or a.path.strip("/").replace("/", "_"))
            open(dest, "wb").write(d)
            print("saved ->", dest)
    finally:
        link.cli.loop_stop()


if __name__ == "__main__":
    main()
