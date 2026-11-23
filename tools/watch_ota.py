#!/usr/bin/env python3
"""Fetch the firmware package Narwal's cloud is currently offering to YOUR robot.

Run by the robot OWNER. Plain HTTPS to Narwal's own servers — no MITM, no
network access to the robot, nothing is changed on the robot.

How it works:
  1. Log in by email/password (or reuse a refresh token) on the app REST API.
  2. Read the robot's product key and home region from getDeviceInfoList.
  3. Ask the robot OTA check endpoint for that product key across regions.
     The endpoint only answers while Narwal is staging a rollout for that
     product; between rollouts it returns nothing to download.
  4. On a hit, download the .npkg, record its sha256, and save the URL —
     the download URL stays valid after the staging window closes.

Examples:
  python3 watch_ota.py --email you@example.com              # poll every 5 min
  python3 watch_ota.py --email you@example.com --once       # single check
  python3 watch_ota.py --refresh-token eyJ...               # no password
  python3 watch_ota.py --product-key fjhpiem4ba             # key already known

Python 3.10+, stdlib only. Nothing is sent anywhere except Narwal's own servers.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

APP_HOSTS = {
    "us": "us-app.narwaltech.com",
    "eu": "eu-app.narwaltech.com",
    "il": "il-app.narwaltech.com",
    "cn": "cn-app.narwaltech.com",
    "aus": "aus-app.narwaltech.com",
}
OPEN_HOST = "{region}-open.narwaltech.com"
LOGIN_PATH = "/user-authentication-server/v2/login/loginByEmail"
REFRESH_PATH = "/user-authentication-server/v1/token/refresh"
DEVICES_PATH = "/user-device-platform-server/device-info/getDeviceInfoList"
OTA_PATH = "/open-platform-manage-ota/v1/iot-platform/ota/check"
FAKE_DEVICE_ID = "0" * 32
FAKE_OLD_VERSION = "v01.00.00.00"
MANIFEST_NAME = "firmware_manifest.json"

EXIT_HIT = 0
EXIT_NO_HIT = 1
EXIT_NO_DEVICE = 2
EXIT_INTERRUPTED = 130


class ApiError(RuntimeError):
    pass


class NetworkError(RuntimeError):
    pass


def request(url: str, body: dict | None = None, token: str | None = None,
            timeout: int = 20) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Auth-Token"] = token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise ApiError(f"HTTP {e.code} from {url.split('/', 3)[2]}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise NetworkError(f"{type(e).__name__}: {e}") from e


def api(region: str, path: str, body=None, token=None) -> dict:
    r = request(f"https://{APP_HOSTS[region]}{path}", body=body, token=token)
    if r.get("code") != 0:
        raise ApiError(f"{path} failed: code={r.get('code')} "
                       f"err_code={r.get('err_code')} msg={r.get('msg')}")
    return r


def login(email: str, password: str, region: str) -> dict:
    return api(region, LOGIN_PATH, {"email": email, "password": password})["result"]


def refresh(refresh_token: str, region: str) -> dict:
    return api(region, REFRESH_PATH, {"refreshToken": refresh_token})["result"]


def get_devices(token: str, region: str) -> list[dict]:
    r = api(region, DEVICES_PATH, token=token)
    return r["result"].get("deviceInfoList") or []


def ota_check(region: str, product_id: str, token: str | None) -> dict | None:
    """The staged-package result for this region, or None when nothing is staged.

    us/eu answer without auth; cn/aus/il want a token.
    """
    body = {"device_id": FAKE_DEVICE_ID, "firmware_version": FAKE_OLD_VERSION,
            "product_id": product_id}
    url = f"https://{OPEN_HOST.format(region=region)}{OTA_PATH}"
    try:
        r = request(url, body=body, token=token)
    except ApiError as e:
        # 4xx here means "not signed in for this region" or "nothing for
        # this product"; either way it is not a hit.
        print(f"  {region}: {e}, skipping")
        return None
    if r.get("code") != 0:
        return None
    res = r.get("result") or {}
    url = res.get("robotDownloadUrl")
    if not url or not str(url).startswith("https://"):
        return None
    return res


def safe_name(url: str) -> str:
    name = url.split("?", 1)[0].rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name or "firmware.npkg"


def download(url: str, out_dir: Path) -> tuple[Path, str, int]:
    """Download to <name>.part, then rename. Skips a file that is already complete."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / safe_name(url)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    n = 0
    try:
        with urllib.request.urlopen(url, timeout=120) as resp, open(part, "wb") as f:
            expected = resp.headers.get("Content-Length")
            while chunk := resp.read(1 << 20):
                f.write(chunk)
                h.update(chunk)
                n += len(chunk)
                print(f"\r  {n / 1e6:.1f} MB", end="", flush=True)
        print()
        if expected and n != int(expected):
            raise NetworkError(f"truncated download ({n} of {expected} bytes)")
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    digest = h.hexdigest()
    if dest.exists():
        if file_sha256(dest) == digest:
            part.unlink()
            print(f"  already have {dest.name} (same sha256), not saving a copy")
            return dest, digest, n
        dest = dest.with_name(f"{dest.stem}-{digest[:12]}{dest.suffix}")
    part.rename(dest)
    print(f"  saved {dest}")
    return dest, digest, n


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def record_manifest(out_dir: Path, entry: dict) -> None:
    path = out_dir / MANIFEST_NAME
    try:
        entries = json.loads(path.read_text())
        if not isinstance(entries, list):
            entries = []
    except (OSError, ValueError):
        entries = []
    entries = [e for e in entries if e.get("sha256") != entry["sha256"]]
    entries.append(entry)
    path.write_text(json.dumps(entries, indent=2) + "\n")


def describe_hit(hit: dict) -> None:
    print(f"  version : {hit.get('robotVersion')}")
    print(f"  size    : {hit.get('robotApkSize')} KiB")
    print(f"  notes   : {hit.get('upgradeDescribe')}")
    print(f"  URL (stays valid after the rollout window closes):\n  {hit.get('robotDownloadUrl')}")


def resolve_account(args, parser: argparse.ArgumentParser) -> tuple[list[str], str | None, list[str]]:
    """Returns (product ids, auth token, regions to poll)."""
    regions = ["us", "eu", args.region, "aus", "il"]
    if args.product_key:
        regions = list(dict.fromkeys(regions))
        print(f"product key {args.product_key}; regions: {', '.join(regions)}")
        return [args.product_key], args.token, regions

    try:
        if args.refresh_token:
            result = refresh(args.refresh_token, args.region)
        elif args.email:
            password = args.password or getpass.getpass("Narwal password: ")
            result = login(args.email, password, args.region)
        else:
            parser.error("need --email, --refresh-token or --product-key")
        token = result["token"]
        if args.token is None:
            args.token = token
        devices = get_devices(token, args.region)
    except ApiError as e:
        sys.exit(f"login failed on {args.region} ({e}). "
                 "Check your email/password and --region.")
    except NetworkError as e:
        sys.exit(f"could not reach Narwal ({e}). Check your internet connection.")

    rt = result.get("refresh_token") or result.get("refreshToken")
    if rt:
        print(f"logged in ({args.region}). Refresh token for next time "
              f"(keep it private, it is your password equivalent):\n  {rt}")

    if not devices:
        print("no robots are bound to this account. Are they in the app, and "
              "is --region the region you signed up in?")
        sys.exit(EXIT_NO_DEVICE)
    for i, d in enumerate(devices, 1):
        print(f"  device {i}: key={d.get('productId')}  "
              f"model={d.get('productModel', '?')}  fw={d.get('firmwareVersion')}  "
              f"site={d.get('deviceSiteCode')}")
    if args.device is not None and not 1 <= args.device <= len(devices):
        sys.exit(f"--device {args.device} is out of range; this account has {len(devices)}")
    chosen = devices if args.device is None else [devices[args.device - 1]]
    product_ids = list(dict.fromkeys(d["productId"] for d in chosen if d.get("productId")))

    home = (chosen[0].get("deviceSiteCode") or args.region).lower()
    if home in APP_HOSTS:
        regions = [home] + [r for r in regions if r != home]
    regions = list(dict.fromkeys(regions))
    return product_ids, args.token, regions


def check_round(product_ids: list[str], regions: list[str], token: str | None,
                seen: set[str]) -> list[tuple[str, str, dict]]:
    """Ask every region about every product; return (region, product_id, hit) for new URLs."""
    hits = []
    offline = False
    for product_id in product_ids:
        for region in regions:
            try:
                hit = ota_check(region, product_id, token)
            except NetworkError as e:
                print(f"  {region}: {e}")
                offline = True
                continue
            if hit and hit["robotDownloadUrl"] not in seen:
                seen.add(hit["robotDownloadUrl"])
                print(f"[{now()}] HIT on {region} for {product_id}:")
                describe_hit(hit)
                hits.append((region, product_id, hit))
    return hits


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Fetch the firmware package Narwal's cloud is offering to your robot.",
        epilog="Exit codes: 0 = firmware downloaded, 1 = nothing staged, "
               "2 = no robot on the account, 130 = interrupted.")
    p.add_argument("--region", default="us", choices=sorted(APP_HOSTS),
                   help="your account's region (default: us)")
    p.add_argument("--email", help="Narwal account email")
    p.add_argument("--password", help="account password (omit to be prompted securely)")
    p.add_argument("--refresh-token", help="refresh token from a previous run")
    p.add_argument("--product-key", help="skip login; you already know the product key")
    p.add_argument("--token", help="Auth-Token to use for cn/aus/il OTA checks")
    p.add_argument("--device", type=int, metavar="N",
                   help="only the Nth robot on the account (1-based; default: all)")
    p.add_argument("--once", action="store_true", help="check once and exit, no polling")
    p.add_argument("--interval", type=int, default=300,
                   help="seconds between polling rounds (default: 300)")
    p.add_argument("--keep-going", action="store_true",
                   help="keep polling after a hit (to catch later rollouts)")
    p.add_argument("--out", default="firmware", help="download directory (default: ./firmware)")
    args = p.parse_args()
    if args.interval < 1:
        p.error("--interval must be at least 1 second")

    product_ids, token, regions = resolve_account(args, p)
    out_dir = Path(args.out)
    print(f"checking OTA staging for {', '.join(product_ids)} on "
          f"{', '.join(regions)} ({'once' if args.once else f'every {args.interval}s'})")

    seen: set[str] = set()
    try:
        while True:
            hits = check_round(product_ids, regions, token, seen)
            for region, product_id, hit in hits:
                try:
                    path, digest, size = download(hit["robotDownloadUrl"], out_dir)
                except (NetworkError, urllib.error.HTTPError, OSError) as e:
                    print(f"  download FAILED ({e}). The URL above can be retried by hand.")
                    continue
                print(f"  sha256 {digest}")
                record_manifest(out_dir, {
                    "file": path.name, "sha256": digest, "size_bytes": size,
                    "product_id": product_id, "region": region,
                    "robot_version": hit.get("robotVersion"),
                    "url": hit["robotDownloadUrl"],
                    "first_seen_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "notes": hit.get("upgradeDescribe"),
                })
            if hits:
                print(f"recorded in {out_dir / MANIFEST_NAME}")
                if not args.keep_going:
                    return EXIT_HIT
            elif args.once:
                print("nothing is staged right now. Narwal only publishes a package "
                      "while a rollout is in progress — run without --once to wait.")
                return EXIT_NO_HIT
            else:
                print(f"[{now()}] nothing staged")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
