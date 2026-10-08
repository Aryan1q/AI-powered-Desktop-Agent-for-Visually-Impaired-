"""Network primitives: adapters, connectivity, DNS, download, upload-free fetch.

There is deliberately no "post this somewhere" helper. Anything that leaves
the machine goes through `fetch()`, which records the destination, and the
policy layer treats an unknown host as a high-risk action the user must
approve.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..errors import ActionError
from ..logging_setup import get_logger, record
from ..utils import retry as retryutil

log = get_logger(__name__)

USER_AGENT = "ufo-automation/2.0"
DEFAULT_TIMEOUT = 30
MAX_DOWNLOAD = 200 * 1024 * 1024


@dataclass
class Host:
    name: str
    ips: list[str]
    port: int = 443

    def to_dict(self) -> dict:
        return {"name": self.name, "ips": self.ips, "port": self.port}


def is_online(host: str = "1.1.1.1", port: int = 53, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve(host: str) -> Host:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ActionError(f"Cannot resolve {host!r}: {e}") from e
    ips = sorted({i[4][0] for i in infos})
    return Host(host, ips)


def public_ip() -> str:
    try:
        return fetch_text("https://api.ipify.org", timeout=10).strip()
    except Exception as e:
        raise ActionError(f"Could not determine the public IP: {e}") from e


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.0)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


# --------------------------------------------------------------------------
# adapters / firewall
# --------------------------------------------------------------------------
def adapters() -> str:
    from . import shell

    return shell.ps(
        "Get-NetAdapter | Select-Object Name,Status,LinkSpeed,MacAddress,"
        "InterfaceDescription | Format-Table -AutoSize | Out-String -Width 220",
        timeout=40,
    ).output or "(no adapters reported)"


def ip_config() -> str:
    from . import shell

    return shell.ps("ipconfig /all", timeout=30).output


def wifi_networks() -> str:
    from . import shell

    return shell.ps(
        "netsh wlan show networks mode=bssid", timeout=45
    ).output or "(no wireless networks found or Wi-Fi is off)"


def connect_wifi(ssid: str, password: str = "", profile: str = "") -> str:
    """Join a wireless network. The password never appears in the log record."""
    from . import shell

    if profile:
        result = shell.ps(
            f'netsh wlan add profile filename="{profile}" interface="Wi-Fi" '
            f"user=current", timeout=60)
        name = Path(profile).name
    else:
        result = shell.ps(
            "netsh wlan set profileparameter name="
            f'"{ssid}" keymaterial="{password}"', timeout=60)
        name = ssid
    record("wifi_connect", ssid=ssid, used_profile=bool(profile))
    if not result.ok:
        raise ActionError(f"Could not configure Wi-Fi profile: {result.output[:300]}")
    joined = shell.ps(f'netsh wlan connect name="{ssid}" ssid="{ssid}"', timeout=45)
    if not joined.ok:
        raise ActionError(f"Could not connect to {ssid!r}: {joined.output[:300]}")
    return f"connected to Wi-Fi network {name!r}"


def forget_wifi(ssid: str) -> str:
    from . import shell

    result = shell.ps(
        f'netsh wlan delete profile name="{ssid}" interface="Wi-Fi"', timeout=45)
    record("wifi_forget", ssid=ssid)
    if not result.ok:
        raise ActionError(result.output[:300])
    return f"forgot Wi-Fi network {ssid!r}"


def ping(host: str = "1.1.1.1", count: int = 4) -> str:
    from . import shell

    return shell.ps(f"ping -n {int(count)} {host}", timeout=15 + count * 2).output


def trace_route(host: str, max_hops: int = 15) -> str:
    from . import shell

    return shell.ps(f"tracert -h {int(max_hops)} {host}", timeout=90).output


def ports() -> str:
    from . import shell

    return shell.ps(
        "Get-NetTCPConnection -State Listen | Select-Object LocalAddress,LocalPort,"
        "OwningProcess | Sort-Object LocalPort | Format-Table -AutoSize | Out-String -Width 160",
        timeout=45,
    ).output or "(no listening ports reported)"


def firewall_status() -> str:
    from . import shell

    return shell.ps(
        "Get-NetFirewallProfile | Select-Object Name,Enabled,DefaultInboundAction"
        " | Format-Table -AutoSize | Out-String", timeout=45,
    ).output


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _request(url: str, method: str = "GET", headers: dict | None = None,
             data: bytes | None = None, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, bytes, dict]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ActionError(f"Only http/https URLs are supported, got {parsed.scheme!r}.")
    req = urllib.request.Request(url, data=data, method=method.upper())
    req.add_header("User-Agent", USER_AGENT)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:10_000], dict(e.headers or {})
    except urllib.error.URLError as e:
        raise ActionError(f"Request to {url} failed: {e.reason}") from e
    except socket.timeout as e:
        raise ActionError(f"Request to {url} timed out after {timeout}s") from e


def fetch(url: str, method: str = "GET", headers: dict | None = None,
          data: str | bytes | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Make one HTTP request. The destination is recorded for the audit trail."""
    payload = data.encode("utf-8") if isinstance(data, str) else data
    record("http", url=url[:300], method=method.upper(), bytes=len(payload or b""))
    status, body, resp_headers = _request(url, method, headers, payload, timeout)
    return {
        "status": status,
        "ok": 200 <= status < 400,
        "headers": {k: v for k, v in resp_headers.items()
                    if k.lower() in ("content-type", "content-length", "location",
                                     "last-modified", "etag")},
        "size": len(body),
        "text": _decode(body)[:200_000],
    }


def fetch_text(url: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    return fetch(url, timeout=timeout)["text"]


def fetch_json(url: str, timeout: float = DEFAULT_TIMEOUT) -> Any:
    result = fetch(url, timeout=timeout)
    if not result["ok"]:
        raise ActionError(f"HTTP {result['status']} from {url}")
    try:
        return json.loads(result["text"])
    except json.JSONDecodeError as e:
        raise ActionError(f"{url} did not return JSON: {e}") from e


def _decode(body: bytes) -> str:
    return body.decode("utf-8", errors="replace")


def download(url: str, dest: str = "", timeout: float = 120.0,
             retries: int = 3) -> str:
    """Download a file, resuming nothing but retrying transient failures."""
    target = Path(os.path.expandvars(str(dest) or Path(urllib.parse.urlparse(url).path).name))
    if not target.name:
        raise ActionError(f"Cannot determine a filename for {url!r}; pass dest=.")
    record("download", url=url[:300], dest=str(target))
    delays = retryutil.backoff_delays(2, 2, 15, retries)

    def _do() -> str:
        status, body, headers = _request(url, timeout=timeout)
        if status >= 400:
            raise ActionError(f"HTTP {status} downloading {url}")
        if len(body) > MAX_DOWNLOAD:
            raise ActionError(
                f"{url} is {len(body)} bytes, over the {MAX_DOWNLOAD} byte limit. "
                "Use a ranged request or download it manually."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        return (f"downloaded {len(body)} bytes to {target}"
                + (f" ({headers.get('Content-Type', '')})" if headers.get("Content-Type") else ""))

    return retryutil.call(_do, exceptions=(ActionError,), delays=delays)


def head(url: str, timeout: float = 20.0) -> dict:
    return fetch(url, method="HEAD", timeout=timeout)


def open_browser(url: str = "") -> str:
    from . import shell

    return shell.open_url(url) if url else shell.launch("msedge.exe", "https://www.bing.com") and "opened the browser"


def search_web(query: str) -> str:
    from . import shell

    return shell.open_url("https://duckduckgo.com/?q=" + urllib.parse.quote(query))


def is_private(host: str) -> bool:
    """True when a hostname resolves only to private/loopback addresses."""
    try:
        addresses = [ipaddress.ip_address(ip) for ip in resolve(host).ips]
    except ActionError:
        return False
    return all(a.is_private or a.is_loopback for a in addresses)
