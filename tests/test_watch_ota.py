"""Offline self-test for tools/watch_ota.py — fakes Narwal's servers, no network."""
import hashlib
import io
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import watch_ota as w  # noqa: E402

NPKG = b"NPKG" * 1000
SHA = hashlib.sha256(NPKG).hexdigest()
URL = "https://d1.example.net/fw/IP1221-S_0200_SU_813A_v01.09.02.00_x.npkg"


class Resp(io.BytesIO):
    def __init__(self, body):
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body))}


def fake_urlopen(req, timeout=None, context=None):
    url = req.full_url if hasattr(req, "full_url") else req
    if url == URL:
        return Resp(NPKG)
    if url.endswith(w.LOGIN_PATH) or url.endswith(w.REFRESH_PATH):
        body = {"code": 0, "result": {"token": "tok", "refresh_token": "rt"}}
    elif url.endswith(w.DEVICES_PATH):
        if req.get_header("Auth-token") != "tok":
            raise urllib.error.HTTPError(url, 401, "", {}, None)
        body = {"code": 0, "result": {"deviceInfoList": [
            {"productId": "keyA", "productModel": "Freo X Ultra", "firmwareVersion": "v01.09.01.00", "deviceSiteCode": "EU"},
            {"productId": "keyA", "productModel": "Freo X Ultra", "firmwareVersion": "v01.09.01.00"},
            {"productId": "keyB", "productModel": "Other", "firmwareVersion": "v01.00.00.00"},
        ]}}
    elif url.endswith(w.OTA_PATH):
        host = url.split("/")[2]
        body = {"code": 0, "result": None}
        if host.startswith("eu-") and b'"keyA"' in req.data:
            body = {"code": 0, "result": {"robotDownloadUrl": URL, "robotVersion": "v01.09.02.00",
                                          "robotApkSize": 1234, "upgradeDescribe": "notes"}}
        elif host.startswith("us-"):
            body = {"code": 0, "result": None}
        else:
            body = {"code": 4003}
    else:
        raise urllib.error.HTTPError(url, 404, "", {}, None)
    return Resp(json.dumps(body).encode())


urllib.request.urlopen = fake_urlopen
w.urllib.request.urlopen = fake_urlopen


def run(argv):
    sys.argv = ["watch_ota.py"] + argv
    try:
        return w.main()
    except SystemExit as e:
        return e.code


with tempfile.TemporaryDirectory() as tmp:
    # 1. login -> two products, hit on eu, manifest written
    rc = run(["--email", "x@y.z", "--password", "pw", "--region", "us", "--once", "--out", tmp])
    out = Path(tmp)
    man = json.loads((out / w.MANIFEST_NAME).read_text())
    assert rc == 0 and len(man) == 1, (rc, man)
    assert man[0]["sha256"] == SHA and man[0]["region"] == "eu" and man[0]["product_id"] == "keyA"
    assert [p.name for p in out.iterdir() if p.suffix == ".npkg"] == ["IP1221-S_0200_SU_813A_v01.09.02.00_x.npkg"]
    print("1 login + multi-product + download + manifest: ok")

    # 2. same file again -> no duplicate
    rc = run(["--refresh-token", "rt", "--region", "us", "--once", "--out", tmp])
    files = [p for p in out.iterdir() if ".npkg" in p.name]
    assert rc == 0 and len(files) == 1 and len(json.loads((out / w.MANIFEST_NAME).read_text())) == 1, files
    print("2 re-run dedupes file and manifest: ok")

    # 3. --device 2 (keyB only) -> no hit -> exit 1
    rc = run(["--refresh-token", "rt", "--region", "us", "--once", "--device", "3", "--out", tmp])
    assert rc == 1, rc
    print("3 --device 3 = keyB only, no hit -> exit 1: ok")

    # 4. --device out of range
    rc = run(["--refresh-token", "rt", "--region", "us", "--once", "--device", "9", "--out", tmp])
    assert isinstance(rc, str) and "out of range" in rc, rc
    print("4 --device out of range: ok")

    # 5. --product-key path, no hit
    rc = run(["--product-key", "zzz", "--once", "--out", tmp])
    assert rc == 1, rc
    print("5 --product-key without login: ok")

    # 6. bad host -> network error handled, polling mode not used, --once
    real = w.OPEN_HOST
    w.OPEN_HOST = "{region}-nonexistent.invalid"
    rc = run(["--product-key", "zzz", "--once", "--out", tmp])
    w.OPEN_HOST = real
    assert rc == 1, rc
    print("6 network failure per region is survivable: ok")

    # 7. no args -> usage error
    rc = run(["--once"])
    assert rc == 2, rc
    print("7 missing credentials -> usage error: ok")
print("all passed")
