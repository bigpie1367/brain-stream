import hashlib
import secrets
import time

import requests

from src.utils.logger import get_logger

log = get_logger(__name__)

_API_VERSION = "1.16.1"
_CLIENT = "music-auto"


def _auth_params(username: str, password: str) -> dict:
    salt = secrets.token_hex(6)
    token = hashlib.md5((password + salt).encode()).hexdigest()
    return {
        "u": username,
        "t": token,
        "s": salt,
        "v": _API_VERSION,
        "c": _CLIENT,
        "f": "json",
    }


def trigger_scan(url: str, username: str, password: str) -> bool:
    params = _auth_params(username, password)
    endpoint = f"{url.rstrip('/')}/rest/startScan"
    log.info("triggering navidrome scan")
    try:
        resp = requests.get(endpoint, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("subsonic-response", {}).get("status")
        if status == "ok":
            log.info("scan triggered successfully")
            return True
        log.error("scan trigger failed", response=data)
        return False
    except requests.RequestException as exc:
        log.error("navidrome request failed", error=str(exc))
        return False


def wait_for_scan(url: str, username: str, password: str, timeout: int = 300) -> bool:
    endpoint = f"{url.rstrip('/')}/rest/getScanStatus"
    deadline = time.time() + timeout
    log.info("waiting for navidrome scan to complete")
    while time.time() < deadline:
        try:
            params = _auth_params(username, password)
            resp = requests.get(endpoint, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            scan_status = data.get("subsonic-response", {}).get("scanStatus", {})
            if not scan_status.get("scanning", False):
                log.info("navidrome scan complete", count=scan_status.get("count"))
                return True
        except requests.RequestException as exc:
            log.warning("poll failed", error=str(exc))
        time.sleep(5)
    log.error("scan did not complete within timeout", timeout=timeout)
    return False
