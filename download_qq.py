#!/usr/bin/env python3
"""Download a file from QQ flash transfer (qfile.qq.com)."""
import json
import hmac
import hashlib
import time
import random
import sys
import urllib.request
import urllib.error

# File metadata extracted from the share page
PHYSICAL_ID = "EhQftn5wfLNQsJonn3hSSDYz-JT6yhi7sAQgtXQot_3K1ua4lQMyBHByb2RQgOpJWhBJu8X8RxRucXc33KrBgB0FegMMlP-CAQJneg"
FILESET_ID = "2acb1be3-260f-44dd-9873-060ee2934102"
SRV_FILEID = "efbde128-a347-c849-5bb6-b7eb0d54fd02"
FILE_NAME = "optimized_files.zip"
EXPECTED_SIZE = 71739
EXPECTED_MD5 = "fce6bd20f45a468f81c6d1232b8c6b29"
EXPECTED_SHA1 = "1fb67e707cb350b09a279f7852483633f894faca"

HMAC_KEY = "9EB18BB9ED457684"
BASE_URL = "https://qfile.qq.com/http2rpc/gotrpc/noauth/"
BATCH_DOWNLOAD_SERVICE = "trpc.qqntv2.richmedia.InnerProxy/BatchDownload"
# Command code for BatchDownload: "0x9248_4"
COMMAND = "0x9248"
SERVICE_TYPE = "4"

# scene_type for normal browser = SCENE_TYPE_OTHER_EXTERNAL = 103
SCENE_TYPE_OTHER_EXTERNAL = 103

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
DEVICE_ID = "a1b2c3d4e5f6g7h8"


def sign(body_str, nonce, timestamp):
    """HMAC-SHA1 signature: HmacSHA1(bodyJson + nonce + timestamp, key) -> hex."""
    msg = body_str + str(nonce) + str(timestamp)
    return hmac.new(HMAC_KEY.encode(), msg.encode(), hashlib.sha1).hexdigest()


def call_api(service_path, body, command, service_type, extra_headers=None):
    """Call a QQ tRPC API endpoint."""
    url = BASE_URL + service_path
    # Body sent = original body + scene_type (added by wrapper)
    full_body = dict(body)
    full_body["scene_type"] = SCENE_TYPE_OTHER_EXTERNAL
    body_str = json.dumps(full_body, separators=(",", ":"))

    nonce = random.randint(1, 10000)
    timestamp = str(int(time.time()))
    signature = sign(body_str, nonce, timestamp)

    headers = {
        "Content-Type": "application/json",
        "x-oidb": json.dumps({"uint32_command": command, "uint32_service_type": service_type}, separators=(", ", ": ")),
        "cookie": "uin=9000002;p_uin=9000002;",
        "x-device-id": DEVICE_ID,
        "User-Agent": UA,
        "x-qq-ar-nonce": str(nonce),
        "x-qq-ar-timestamp": timestamp,
        "x-qq-ar-signature": signature,
        "Origin": "https://qfile.qq.com",
        "Referer": "https://qfile.qq.com/q/xc3sZPQgPQ",
    }
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(url, data=body_str.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read().decode("utf-8")
            return resp.status, data
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, body


def batch_download():
    """Call BatchDownload to get the download URL."""
    body = {
        "req_head": {"agent": 8},
        "download_info": [
            {
                "batch_id": PHYSICAL_ID,
                "scene": {"business_type": 4, "app_type": 22, "scene_type": 5},
                "index_node": {"file_uuid": PHYSICAL_ID},
                "url_type": 2,
                "download_scene": 0,
            }
        ],
    }
    # Empty captcha headers (the JS sends empty strings initially)
    extra = {"x-fsq-captcha-ticket": "", "x-fsq-captcha-randstr": ""}
    print("[*] Calling BatchDownload API...")
    status, resp = call_api(BATCH_DOWNLOAD_SERVICE, body, COMMAND, SERVICE_TYPE, extra)
    print(f"[*] HTTP status: {status}")
    print(f"[*] Response: {resp[:2000]}")
    return resp


def main():
    resp = batch_download()
    try:
        j = json.loads(resp)
    except Exception:
        print("[!] Response is not JSON")
        return 1

    # Check for captcha requirement
    err = j.get("error") or {}
    if str(err.get("code", "")) == "170019016":
        print("[!] Captcha required (error 170019016). Cannot proceed without solving captcha.")
        return 2

    data = j.get("data") or {}
    download_rsp = data.get("download_rsp") or data.get("downloadRsp") or []
    if not download_rsp:
        print("[!] No download_rsp in response")
        return 3

    urls = [d.get("url") for d in download_rsp if d.get("url")]
    if not urls:
        print("[!] No download URL returned")
        return 4

    dl_url = urls[0]
    print(f"[*] Download URL: {dl_url[:120]}...")

    # Download the file
    print(f"[*] Downloading {FILE_NAME}...")
    req = urllib.request.Request(
        dl_url,
        headers={
            "User-Agent": UA,
            "Referer": "https://qfile.qq.com/q/xc3sZPQgPQ",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        content = r.read()

    with open(FILE_NAME, "wb") as f:
        f.write(content)

    # Verify
    md5 = hashlib.md5(content).hexdigest()
    sha1 = hashlib.sha1(content).hexdigest()
    print(f"[*] Saved {FILE_NAME}: {len(content)} bytes")
    print(f"[*] MD5:  {md5} (expected {EXPECTED_MD5})")
    print(f"[*] SHA1: {sha1} (expected {EXPECTED_SHA1})")
    if md5 == EXPECTED_MD5:
        print("[+] MD5 verified - file integrity OK")
    else:
        print("[!] MD5 mismatch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
