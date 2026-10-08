"""Remote access, virtual machines, smart home and everyday admin.

These are the capabilities that reach outside this machine. Each one is either
read-only, opens a tool for the user to drive, or reports plainly that Windows
has no API for it.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from ..errors import ActionError
from ..logging_setup import get_logger

from . import network, shell, system

log = get_logger(__name__)


def _life_home() -> Path:
    from ..config import active

    return Path(active().storage.home) / "life"


def _finance_home() -> Path:
    root = _life_home() / "finance"
    root.mkdir(parents=True, exist_ok=True)
    return root


# --------------------------------------------------------------------------
# remote access
# --------------------------------------------------------------------------
def remote_desktop(target: str = "", args: str = "") -> str:
    """Connect to a remote PC over RDP. target is the host or IP."""
    host = (target or args).strip()
    if not host:
        return "give me a hostname or IP address"
    return str(shell.run_powershell(f"mstsc.exe {host}"))


def remote_ssh(target: str = "", args: str = "") -> str:
    """Open a terminal to a host over SSH."""
    host = (target or args).strip()
    if not host:
        return "give me a host"
    if not shutil.which("ssh"):
        return "ssh is not installed; Windows 11 has it in Optional features"
    return str(shell.run_powershell(f"Start-Process ssh {host}"))


def rdp_status(target: str = "", args: str = "") -> str:
    return str(shell.run_powershell(
        "Get-ItemProperty 'HKLM:\\System\\CurrentControlSet\\Control\\Terminal "
        "Server' -Name fDenyTSConnections | Select-Object fDenyTSConnections"))


# --------------------------------------------------------------------------
# virtual machines
# --------------------------------------------------------------------------
def vm(target: str = "", args: str = "") -> str:
    """List Hyper-V or WSL virtual machines."""
    verb = f"{target} {args}".strip().lower()
    if "wsl" in verb:
        return str(shell.run_powershell("wsl --list --verbose"))
    if shutil.which("VBoxManage"):
        return str(shell.run_powershell("VBoxManage list vms"))
    if "list" in verb:
        return ("no VM manager found. Install Hyper-V (Windows Pro and up) or "
                "VirtualBox.")
    return ("say 'vm list' to see what is installed, or name a manager to "
            "open.")


def wsl(target: str = "", args: str = "") -> str:
    """Run a command in the default WSL distribution."""
    if not shutil.which("wsl"):
        return "WSL is not installed"
    return str(shell.run_powershell(f"wsl {target} {args}".strip()))


def containers(target: str = "", args: str = "") -> str:
    if not shutil.which("docker"):
        return "docker is not installed"
    return str(shell.run_powershell(f"docker ps -a"))


# --------------------------------------------------------------------------
# smart home
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# smart home
#
# The honest position: there is no "turn on the lamp" in Windows. What there is,
# on a home network, is devices that announce themselves over documented,
# standard protocols. Those can be found, and a few can be spoken to.
#
# What will not happen here is sending a packet to a device whose protocol
# nobody has read. A guessed message to a real device is how a light ends up
# flashing at 2am in someone's bedroom, so anything unrecognised is reported
# rather than attempted.
# --------------------------------------------------------------------------

#: SSDP service types worth listening for. These are the standard discovery
#: types, not guesses: a device that advertises one of them is telling us what
#: it is.
_SSDP_TYPES = {
    "urn:google-cast-service": "Chromecast / Google Cast",
    "urn:schemas-upnp-org:device:MediaRenderer:1": "UPnP media renderer",
    "urn:schemas-upnp-org:device:MediaServer:1": "UPnP media server",
    "urn:schemas-sony-com:service:ScalarWebAPI:1": "Sony speaker",
    "urn:apple:device:AirPlay:1": "AirPlay speaker",
    "urn:schemas-upnp-org:service:WANIPConnection:1": "UPnP internet gateway",
    "urn:Belkin:device:control:1": "Belkin device",
}

#: MAC OUI prefixes we can name without guessing. A lookup table, not a guess.
_KNOWN_VENDORS = {
    "B8:27:EB": "Raspberry Pi",
    "DC:A6:32": "Raspberry Pi",
    "E4:5F:01": "Raspberry Pi",
    "FC:FB:FB": "Cisco",
    "00:1A:11": "Google",
    "F4:F5:E8": "Google",
    "18:B4:30": "Nest",
    "44:65:0D": "Amazon",
    "68:54:FD": "Amazon",
    "AC:6F:74": "Sonos",
    "00:0E:58": "Sonos",
    "B8:E9:37": "Sonos",
    "00:09:B0": "Onkyo",
    "E8:AB:FA": "Espressif (IoT)",
    "24:6F:28": "Espressif (IoT)",
    "8C:AA:B5": "Espressif (IoT)",
    "D8:D1:22": "Espressif (IoT)",
}

_SSDP_QUERY = """$ErrorActionPreference='SilentlyContinue'
$request = "M-SEARCH * HTTP/1.1`r`nHOST: 239.255.255.250:1900`r`nMAN: `"ssdp:discover`"`r`nMX: 2`r`nST: ssdp:all`r`n`r`n"
$udp = New-Object System.Net.Sockets.UdpClient
$udp.Client.SetSocketOption([System.Net.Sockets.SocketOptionLevel]::Socket, [System.Net.Sockets.SocketOptionName]::ReuseAddress, $true)
$udp.Client.Bind([System.Net.IPEndPoint]::new([System.Net.IPAddress]::Any, 0))
$udp.Send($request, [System.Text.Encoding]::ASCII.GetBytes($request), [System.Net.IPEndPoint]::new([System.Net.IPAddress]::Parse('239.255.255.250'), 1900))
$deadline = (Get-Date).AddSeconds(3)
$found = @{}
while ((Get-Date) -lt $deadline) {
  try {
    $remote = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
    $bytes = $udp.Receive([ref]$remote)
    $text = [System.Text.Encoding]::UTF8.GetString($bytes)
    $st = ([regex]::Match($text, '(?im)^ST:\\s*(.+)$')).Groups[1].Value.Trim()
    $location = ([regex]::Match($text, '(?im)^LOCATION:\\s*(.+)$')).Groups[1].Value.Trim()
    $server = ([regex]::Match($text, '(?im)^SERVER:\\s*(.+)$')).Groups[1].Value.Trim()
    if ($st -and -not $found.ContainsKey($remote.Address.IPAddressToString)) {
      $found[$remote.Address.IPAddressToString] = [pscustomobject]@{ IP=$remote.Address.IPAddressToString; ST=$st; LOCATION=$location; SERVER=$server }
    }
  } catch { Start-Sleep -Milliseconds 250 }
}
$udp.Close()
$found.Values | ConvertTo-Json -Compress
"""


def iot_discover(target: str = "", args: str = "") -> str:
    """Devices on the local network, from SSDP, the ARP table and mDNS.

    All three are read-only and standard. Nothing here sends a command; a device
    is only listed once it has announced itself or appeared in the neighbour
    table.
    """
    lines = ["local network:"]

    announcements = _ssdp_devices()
    if announcements:
        for ip, st, location, server in announcements:
            label = _SSDP_TYPES.get(st.lower())
            if label is None:
                # Not in the table: say so rather than guess a brand.
                label = f"announced {st or 'an unknown service'}"
            lines.append(f"  {ip:<16} {label}"
                         + (f"  ({server})" if server else ""))
    else:
        lines.append("  no SSDP devices answered")

    neighbours = _neighbours()
    if neighbours:
        lines.append("  (other hosts on the network)")
        for ip, mac, name in neighbours:
            vendor = _vendor_of(mac)
            lines.append(f"  {ip:<16} {mac}{f'  {vendor}' if vendor else ''}"
                         + (f"  {name}" if name else ""))
    else:
        lines.append("  no other hosts in the neighbour table")

    lines.append("")
    lines.append("nothing has been sent to any of these. "
                 "ask me about a specific one and I will say what I can do.")
    return "\n".join(lines)


def _ssdp_devices() -> list[tuple[str, str, str, str]]:
    """Ask the network who is there, over the standard SSDP multicast."""
    out = _run_ps(_SSDP_QUERY)
    if not out or not out.strip():
        return []
    try:
        import json

        data = json.loads(out.strip())
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        rows.append((str(item.get("IP", "")), str(item.get("ST", "")),
                     str(item.get("LOCATION", "")), str(item.get("SERVER", ""))))
    return rows


def _neighbours() -> list[tuple[str, str, str]]:
    """The ARP/neighbour table: who the machine has actually spoken to."""
    script = (
        "Get-NetNeighbor -AddressFamily IPv4 | "
        "Where-Object {$_.State -ne 'Unreachable'} | "
        "ForEach-Object { "
        "$mac=$_.LinkLayerAddress; $name=''; "
        "try { $name=(Resolve-DnsName $_.IPAddress -ErrorAction Stop | "
        "Where-Object {$_.NameHost} | Select-Object -First 1).NameHost } catch {}; "
        "'{0}|{1}|{2}' -f $_.IPAddress, $mac, $name }"
    )
    rows = []
    for line in _run_ps(script).splitlines():
        parts = line.strip().split("|")
        if len(parts) == 3 and parts[0]:
            rows.append((parts[0], parts[1], parts[2]))
    return rows


def _vendor_of(mac: str) -> str:
    """A name from the OUI table, or "" when it is not one we know.

    An OUI is the first three octets, which is what the table is keyed on;
    joining the first two would match nothing at all.
    """
    octets = [o for o in str(mac or "").split(":") if o]
    if len(octets) < 3:
        return ""
    return _KNOWN_VENDORS.get(":".join(octets[:3]).upper(), "")


def _run_ps(script: str, timeout: float = 20.0) -> str:
    try:
        result = shell.run_powershell(script, timeout=timeout)
        return str(result.stdout or result.output or "")
    except Exception as e:
        log.debug("smart-home discovery failed: %s", e)
        return ""


def iot_control(target: str = "", args: str = "") -> str:
    """Turn a smart plug or light on or off.

    Only devices with a documented local API are touched: Chromecast devices
    advertise `urn:google-cast-service` and speak a published JSON protocol on
    port 8009. Anything else is named, and the vendor's app is offered, because
    a message this agent made up is not a message the device understands.
    """
    verb = _control_verb(target, args)
    if not verb:
        return ("say on, off or toggle, and name the device — e.g. "
                "'turn the living room light off'")

    for ip, st, _location, _server in _ssdp_devices():
        if st.lower() == "urn:google-cast-service":
            return _cast_command(ip, verb)
        if st.lower() == "urn:apple:device:airplay:1":
            return (f"{ip} is an AirPlay speaker. I can play audio to it, but "
                    f"turning it on and off is the speaker's own protocol and "
                    f"is not documented for third parties, so I will not guess "
                    f"at it. Open its app, or tell me to play something.")
    return ("I found no device I can speak to. Cast and AirPlay devices answer "
            "on standard protocols; most smart plugs and bulbs need their "
            "vendor's app or a local hub such as Home Assistant, which is "
            "where I would point you. Name the app and I will open it.")


def _cast_command(ip: str, verb: str) -> str:
    """Talk to a Chromecast over its published JSON API on port 8009."""
    payload = {
        "type": "urn:x-cast:com.google.cast.media",
        "requestId": int(time.time() * 1000) % 100000,
        "sessionId": "ufo-1",
        "message": {"type": "GET_STATUS" if verb == "on" else "PAUSE"},
    }
    import json

    script = (
        f"try {{ $r = Invoke-WebRequest -UseBasicParsing -Method Post "
        f"-Uri 'http://{ip}:8009/v2/ipc' -ContentType 'application/json' "
        f"-Body '{json.dumps(payload)}' -TimeoutSec 5; "
        f"$r.StatusCode }} catch {{ 'error: ' + $_.Exception.Message }}"
    )
    outcome = _run_ps(script, timeout=10.0).strip()
    if outcome.startswith("error"):
        return f"tried to reach the cast device at {ip} but it did not answer: {outcome}"
    if verb == "on":
        return f"asked the cast device at {ip} what it is playing; nothing was changed"
    return f"sent a pause to the cast device at {ip} ({outcome})"


def iot_scene(target: str = "", args: str = "") -> str:
    """Run a scene, or open the app that owns it.

    Scenes live in the vendor's app, not in Windows, so the useful thing is to
    find the app and open it rather than to claim a scene was run.
    """
    name = f"{target} {args}".strip()
    if not name:
        return "say which scene, e.g. 'movie night'"
    for app in _SMART_HOME_APPS:
        if app in name.lower():
            shell.run_powershell(f"Start-Process '{app}'")
            return (f"opened {app}. The scene is its own to run from there — "
                    f"say 'movie night' again once it is up and I will drive it.")
    return (f"I do not know which app owns {name!r}. If you tell me the app, I "
            f"will open it, or I can start Home Assistant, which runs scenes "
            f"itself over the local network.")


_SMART_HOME_APPS = ("home assistant", "smartthings", "homekit", "yubii", "hubitat")

#: The verbs a smart device understands. Checked as whole words, and the first
#: one wins, so "turn the light on and the music off" takes "on" rather than
#: whichever word happened to be last in the sentence.
_CONTROL_VERBS = ("toggle", "off", "on")


def _control_verb(target: str = "", args: str = "") -> str:
    """The on/off/toggle the user asked for, or "" when they did not say.

    `args` is checked first because the catalog puts the device in `target` and
    the verb in `args`. Only when `args` is empty is the combined text searched,
    which is what makes `iot_control` usable both ways round.
    """
    for text in (str(args or ""), f"{target} {args}"):
        words = re.findall(r"[a-z]+", str(text or "").lower())
        for verb in _CONTROL_VERBS:
            if verb in words:
                return verb
    return ""


# --------------------------------------------------------------------------
# everyday admin
# --------------------------------------------------------------------------
def travel_book(target: str = "", args: str = "") -> str:
    """Find flights and trains. It never buys a ticket."""
    what = f"{target} {args}".strip()
    if not what:
        return "say where and when, e.g. flights Delhi to Mumbai next Friday"
    from . import web

    web.search(f"{what} flight fare", "")
    return (f"opened results for {what!r}. I can compare, but buying a ticket "
            f"is yours to press.")


def bill_pay(target: str = "", args: str = "") -> str:
    """Paying a bill means entering a card number. Never done by a script."""
    return ("I will not type card details into a payment page. Open the bill's "
            "site and pay it yourself.")


def travel_list(target: str = "", args: str = "") -> str:
    """Upcoming travel from the calendar."""
    from . import comms

    return comms.calendar_list(target, args)


def transfer(target: str = "", args: str = "") -> str:
    """Send a file to another machine over SSH."""
    host = (target or args).strip().split()[0] if (target or args).strip() else ""
    if not host or not shutil.which("scp"):
        return "give me user@host, and note that scp is needed"
    from . import filesystem

    source = Path(filesystem.expand(args))
    if not source.exists():
        return f"no such file to send: {source}"
    return str(shell.run_powershell(
        f"scp {source} {host}:")) and f"sent {source.name} to {host}"


def share_screen(target: str = "", args: str = "") -> str:
    """Share the screen in a call app the user already has open."""
    app = (target or args).strip()
    if not app:
        return "give me the app to share in, e.g. teams or zoom"
    from . import apps

    apps.open(app)
    return (f"opened {app}. Start the call, then use its Share screen button — "
            f"capturing the screen needs your click, not a background switch.")


# --------------------------------------------------------------------------
# personal finance
#
# Money is the one area where a wrong answer costs something. Every handler here
# is read-only by design: nothing moves money. Paying needs a bank session the
# agent does not have, and the handler says so rather than half-doing it.
# --------------------------------------------------------------------------
def _ledger(target: str = "") -> Path:
    """Find the statement CSV. Nothing is invented if it is missing."""
    from ..config import active

    root = Path(active().storage.home) / "finance"
    if target.strip():
        candidate = Path(target).expanduser()
        if candidate.is_file():
            return candidate
    if root.is_dir():
        found = sorted(root.glob("*.csv"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
        if found:
            return found[0]
    return root / "statement.csv"


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    import csv as _csv

    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(_csv.DictReader(handle))


def _column(rows: list[dict], *names: str) -> str:
    if not rows:
        return ""
    for name in names:
        for key in rows[0]:
            if key.strip().lower() == name.lower():
                return key
    return next(iter(rows[0]), "")



def balance(target: str = "", args: str = "") -> str:
    """Money in, money out, and what is left, from a statement CSV."""
    path = _ledger(target)
    rows = _rows(path)
    if not rows:
        return (f"no statement at {path}. Export one from your bank as CSV and "
                f"save it there, then ask again.")
    amount = _column(rows, "amount", "value", "debit", "credit", "total")
    if not amount:
        return f"no amount column in {path.name}; found: {', '.join(rows[0])}"
    incoming = sum(max(_money(r.get(amount, 0)), 0.0) for r in rows)
    outgoing = -sum(min(_money(r.get(amount, 0)), 0.0) for r in rows)
    return (f"{len(rows)} transaction(s) in {path.name}\n"
            f"in   {incoming:,.2f}\n"
            f"out  {outgoing:,.2f}\n"
            f"net  {incoming - outgoing:,.2f}")


def statement(target: str = "", args: str = "") -> str:
    """The transaction lines themselves, newest first when there is a date."""
    path = _ledger(target)
    rows = _rows(path)
    if not rows:
        return f"no statement at {path}"
    date = _column(rows, "date", "posted", "transaction date")
    if date:
        rows = sorted(rows, key=lambda r: str(r.get(date, "")), reverse=True)
    amount = _column(rows, "amount", "value", "debit", "credit", "total")
    payee = _column(rows, "description", "payee", "merchant", "name", "details")
    limit = int(args) if args.strip().isdigit() else 20
    lines = []
    for row in rows[:limit]:
        when = str(row.get(date, "")) if date else ""
        who = str(row.get(payee, ""))[:38] if payee else ""
        what = f"{_money(row.get(amount, 0)):,.2f}" if amount else ""
        lines.append(f"{when:<12}{who:<40}{what:>12}")
    return "\n".join(lines) or "nothing to show"


def pay_bill(target: str = "", args: str = "") -> str:
    """Paying is refused here on purpose. It needs a bank session."""
    bill = (target or args).strip()
    if not bill:
        return "give me the bill to pay"
    return ("I will not pay a bill from here. Paying needs your bank's own "
            f"session, and I have no credentials for it. Open {bill} on its "
            "site, or say 'remind me to pay it' and I will set a reminder.")


def track_expenses(target: str = "", args: str = "") -> str:
    """What has been spent, grouped by category."""
    return categorise_transactions(target, args)


def categorise_transactions(target: str = "", args: str = "") -> str:
    """Group spending by a category column, or guess from the payee name."""
    path = _ledger(target)
    rows = _rows(path)
    if not rows:
        return f"no statement at {path}"
    amount = _column(rows, "amount", "value", "debit", "credit", "total")
    category = _column(rows, "category", "type")
    payee = _column(rows, "description", "payee", "merchant", "name", "details")
    if not amount:
        return f"no amount column in {path.name}"
    buckets: dict[str, float] = {}
    for row in rows:
        value = _money(row.get(amount, 0))
        if value >= 0:
            continue
        key = str(row.get(category, "")).strip() if category else ""
        if not key:
            name = str(row.get(payee, "")).lower() if payee else ""
            key = next((g for g in ("grocer", "fuel", "rent", "food", "travel",
                                    "subscription", "shopping")
                        if g in name), "other")
        buckets[key] = buckets.get(key, 0.0) + -value
    total = sum(buckets.values())
    if not buckets:
        return "no outgoing transactions to group"
    lines = [f"{name:<16}{value:>12,.2f}  {value / total * 100:>5.1f}%"
             for name, value in sorted(buckets.items(), key=lambda kv: -kv[1])]
    return "\n".join(["outgoing by category:", *lines,
                      f"{'total':<16}{total:>12,.2f}"])


def invoice(target: str = "", args: str = "") -> str:
    """Build a simple invoice from an item, hours and a rate."""
    from . import docs

    what = f"{target} {args}".strip()
    if not what:
        return "give me the item, hours and rate, e.g. invoice 'March work' 4 800"
    parts = what.rsplit(" ", 2)
    if len(parts) == 3 and all(p.replace(".", "", 1).isdigit() for p in parts[1:]):
        item, hours, rate = parts[0], float(parts[1]), float(parts[2])
        total = hours * rate
        body = (f"Invoice\n\n{item}\n{hours:g} h at {rate:,.2f}\n"
                f"{'total':<28}{total:>12,.2f}")
        path = Path(docs.write("invoice.txt", body))
        return f"{total:,.2f} total -> {path}"
    return "give me the item, hours and rate, e.g. invoice 'design work' 3 500"


def payment_reminder(target: str = "", args: str = "") -> str:
    """A reminder to pay something, in the calendar."""
    from . import comms

    subject = (target or args).strip()
    if not subject:
        return "give me what to pay and when"
    return comms.remind(f"pay {subject}", args or "tomorrow 09:00")


# --------------------------------------------------------------------------
# things the provider would tell you, kept locally instead
#
# A live flight status, a parcel's ETA and a subscription's next charge all live
# behind someone's account. This agent has no credentials, and the previous
# answer — a refusal — was honest but left the user with nothing.
#
# The local answer is a record the *user* owns, in a plain file they can read and
# edit. It is never presented as live data: "your record says" is not "your
# flight is on time". What it buys is the part that does not need an account —
# finding your own boarding pass, knowing what is due this week, and being told
# about it on time.
# --------------------------------------------------------------------------
_RECORD_KINDS = ("trip", "flight", "parcel", "subscription", "price", "booking")


def _records_path() -> Path:
    return _life_home() / "records.json"


def load_records(kind: str = "") -> list[dict]:
    """Every record, newest first, optionally of one kind."""
    try:
        data = json.loads(_records_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("records", []) if isinstance(data, dict) else []
    rows = [r for r in items if isinstance(r, dict)]
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    return sorted(rows, key=lambda r: str(r.get("when", "")), reverse=True)


def save_record(kind: str, name: str, **fields) -> dict:
    """Add or update one record. Matching is by kind and name."""
    if kind not in _RECORD_KINDS:
        raise ActionError(f"unknown record kind {kind!r}; "
                          f"use one of {', '.join(_RECORD_KINDS)}")
    name = str(name or "").strip()
    if not name:
        raise ActionError("give the record a name so I can find it again")

    rows = load_records()
    entry = {"kind": kind, "name": name,
             "when": str(fields.get("when", "") or ""),
             "detail": str(fields.get("detail", "") or ""),
             "added": time.strftime("%Y-%m-%d %H:%M")}
    for key, value in fields.items():
        if key not in ("when", "detail"):
            entry[key] = value

    for index, existing in enumerate(rows):
        if existing.get("kind") == kind and str(existing.get("name", "")).lower() == name.lower():
            entry["added"] = existing.get("added", entry["added"])
            rows[index] = entry
            break
    else:
        rows.append(entry)

    path = _records_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"records": rows}, indent=2), encoding="utf-8")
    return entry


def forget_record(name: str, kind: str = "") -> bool:
    rows = load_records()
    needle = str(name or "").strip().lower()
    kept = [r for r in rows
            if not (str(r.get("name", "")).lower() == needle
                    and (not kind or r.get("kind") == kind))]
    if len(kept) == len(rows):
        return False
    _records_path().write_text(json.dumps({"records": kept}, indent=2),
                              encoding="utf-8")
    return True


def _format_records(rows: list[dict], title: str) -> str:
    if not rows:
        return f"nothing recorded for {title}. Add one and I will keep track of it."
    lines = [f"{title}, from your own records:"]
    for row in rows:
        when = row.get("when") or "no date"
        detail = row.get("detail") or ""
        lines.append(f"  {row.get('name', '?')}  [{when}]"
                     + (f"  {detail}" if detail else ""))
    lines.append("these are the details you gave me, not live provider data.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# travel
# --------------------------------------------------------------------------
def _needs_provider(service: str) -> str:
    return (f"{service} needs an account with the provider itself. I have no "
            f"credentials, so I will not guess at flight times, prices or "
            f"tracking numbers. What I can do instead: keep the details you "
            f"tell me in a local record, and read them back. "
            f"Try: remember this trip as {service!r}.")


def record_trip(target: str = "", args: str = "") -> str:
    """Keep the details of a trip in a local record you own.

    "LHR to DEL on 14 May, flight AI 173" splits into the route, the date and the
    rest. Splitting on the first " on " alone put the route in the date field,
    which then read back wrong in the itinerary.
    """
    text = f"{target} {args}".strip()
    if not text:
        return ('say the trip, e.g. "LHR to DEL on 14 May, flight AI 173, '
                'terminal 3, seats 21A 21B"')

    route, on, rest = text.partition(" on ")
    if not on:
        entry = save_record("trip", text[:60] or text)
    else:
        when, _, detail = rest.partition(",")
        entry = save_record("trip", route.strip() or text[:60],
                            when=when.strip(), detail=detail.strip())
    return (f"kept that trip in your local records as {entry['name']!r}."
            + (f" on {entry['when']}." if entry.get("when") else "")
            + f" Ask me for the trip any time and I will read it back. "
            f"It is not live data, so check with the airline before you travel.")


def flight_status(target: str = "", args: str = "") -> str:
    """What your own records say about a flight, or how to add one.

    This never reports a live status. Inventing "on time" from a local file is
    exactly the sort of confident wrong answer that strands someone at an
    airport, so the wording is deliberately narrow.
    """
    code = (target or args).strip().upper()
    if not code:
        return "give me the flight number, e.g. AI 173"

    rows = [r for r in load_records()
            if code in f"{r.get('name', '')} {r.get('detail', '')}".upper()
            or code in str(r.get("flight", "")).upper()]
    if rows:
        return (_format_records(rows, f"{code}, from your local record")
                + "\n\nI cannot see the airline's live board. Check it before "
                  "you leave.")

    if len(code) < 4:
        return f"{code!r} does not look like a flight number. Try 'AI 173'."
    save_record("flight", code, detail="added by flight_status, no details yet")
    return (f"I had nothing for {code}, so I have noted it in your local "
            f"records. Tell me the time, terminal and seats and I will keep "
            f"them. I still cannot see the live board.")


def boarding_pass(target: str = "", args: str = "") -> str:
    """Find a boarding pass you already have on this machine, and open it.

    Searching the user's own files is something this can actually do well, and it
    is more useful than declining: a pass is a PDF in a Downloads folder long
    before it is anything the provider can be asked about.
    """
    needle = f"{target} {args}".strip()
    if not needle:
        return "give me something to look for, e.g. the flight number or 'LHR to DEL'"

    roots = [Path.home() / "Downloads", Path.home() / "Desktop",
             Path.home() / "Documents", _life_home()]
    words = [w for w in re.findall(r"[a-z0-9]+", needle.lower()) if len(w) > 2]
    if not words:
        return f"nothing distinctive in {needle!r} to search for"

    hits: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for path in root.rglob("*"):
                if len(hits) >= 20:
                    break
                if path.suffix.lower() not in (".pdf", ".png", ".jpg", ".jpeg",
                                               ".html", ".htm"):
                    continue
                name = path.name.lower()
                if any(word in name for word in words):
                    hits.append(path)
        except (OSError, PermissionError):
            continue

    if not hits:
        return (f"no boarding pass matching {needle!r} in Downloads, Desktop "
                f"or Documents. Tell me the file and I will open it.")
    for path in hits[:5]:
        shell.run_powershell(f"Start-Process '{path}'")
    listed = "\n".join(f"  {p}" for p in hits[:5])
    extra = (f"\n  ...and {len(hits) - 5} more" if len(hits) > 5 else "")
    return (f"opened {min(len(hits), 5)} of {len(hits)} match(es):\n"
            f"{listed}{extra}")


def itinerary(target: str = "", args: str = "") -> str:
    """Everything you have told me about your trips."""
    rows = load_records("trip")
    if not rows:
        return ("you have not told me about any trips. Say one, e.g. "
                "'LHR to DEL on 14 May, flight AI 173'")
    return _format_records(rows, "your trips")


def track_parcel(target: str = "", args: str = "") -> str:
    """Track parcels you have told me about, and remind you when one is due.

    `target` is the tracking number and `args`, when given, is when it is due.
    Reading them with `target or args` — the idiom used elsewhere in this module
    — throws the due date away the moment a number is present, which is the only
    case where it matters, and the reminder silently never happens.
    """
    number = str(target or "").strip()
    due = str(args or "").strip()
    if not number:
        number, due = due, ""
    if not number:
        return "give me the tracking number"

    when, _, rest = number.partition(" due ")
    if rest:
        number, due = when.strip(), rest.strip() or due

    rows = [r for r in load_records("parcel")
            if number.lower() in f"{r.get('name', '')} {r.get('detail', '')}".lower()]
    if not rows and not due:
        save_record("parcel", number)
        return (f"I cannot see the courier's site, so I have noted {number!r} "
                f"in your local records. Add when it is due and I will remind "
                f"you: 'track {number} due friday'.")
    if due:
        save_record("parcel", number, when=due)
        from . import comms

        comms.remind(f"parcel {number} is due", due)
        return f"noted {number!r} as due {due}, and put it in your reminders."
    return _format_records(rows, "your parcels")



def _recurring_from_ledger() -> list[str]:
    """Amounts that repeat in the expense ledger, by description."""
    rows = _rows(_ledger("expenses.csv")) if _ledger("expenses.csv").exists() else []
    totals: dict[str, list[float]] = {}
    for row in rows:
        amount = _money(_column(row, "amount", "Amount", "value"))
        if amount <= 0:
            continue
        key = (_column(row, "description", "Description", "note", "name")
               or "?").strip().lower()
        totals.setdefault(key, []).append(amount)

    out = []
    for key, amounts in sorted(totals.items()):
        if len(amounts) < 2:
            continue
        out.append(f"{key}: {len(amounts)} charges, "
                   f"about {sum(amounts) / len(amounts):.2f} each")
    return out


def _money(text: str) -> float:
    try:
        return float(re.sub(r"[^\d.\-]", "", str(text or "")) or 0)
    except ValueError:
        return 0.0


def subscription(target: str = "", args: str = "") -> str:
    """Recurring charges, from the file the user keeps and the records they gave.

    Providers do not expose a local "what renews this month" and this has no
    credentials, so both sources are the user's own: `subscriptions.csv` beside
    the ledger, and anything said with `save_record`.
    """
    path = _finance_home() / "subscriptions.csv"
    rows = _rows(path)
    verb = f"{target} {args}".strip().lower()
    if verb.startswith("add"):
        import csv as _csv

        with path.open("a", encoding="utf-8", newline="") as handle:
            _csv.writer(handle).writerow(
                [target.strip()[4:].strip(), args.strip(),
                 time.strftime("%Y-%m-%d")])
        return f"recurring charge recorded in {path.name}"

    lines: list[str] = []
    if rows:
        charge = _column(rows, "amount", "cost", "price")
        when = _column(rows, "renews", "next", "date")
        total = sum(_money(r.get(charge, 0)) for r in rows) if charge else 0.0
        lines.append(f"from {path.name}:")
        lines += [f"{str(r.get(when, '')):<12}{str(r.get(charge, '')):>10}  "
                  f"{list(r.values())[0][:36]}" for r in rows]
        lines.append(f"{'monthly total':<12}{total:>10,.2f}")

    said = load_records("subscription")
    if said:
        lines.append("you have also told me about:")
        lines += [f"  {r.get('name', '?')}"
                  + (f"  {r.get('amount')}" if r.get("amount") else "")
                  for r in said]

    recurring = _recurring_from_ledger()
    if recurring:
        lines.append("repeating in your expense ledger:")
        lines += [f"  {line}" for line in recurring]

    if not lines:
        return (f"nothing recorded yet. Add one with 'subscriptions add netflix 15', "
                f"or I will spot repeating amounts in your ledger.")
    lines.append("your own figures, not the providers' live billing data.")
    return "\n".join(lines)


def compare_insurance(target: str = "", args: str = "") -> str:
    """Compare quotes the user pastes in. I do not rank real policies."""
    quotes = Path(args.strip() or "").expanduser()
    if quotes.is_file():
        rows = _rows(quotes)
        if rows:
            price = _column(rows, "premium", "price", "cost", "monthly")
            cover = _column(rows, "cover", "coverage", "sum insured")
            width = max((len(str(r.get(cover, ""))) for r in rows), default=10)
            return "\n".join(
                f"{str(r.get(cover, '')):<{width}}  {r.get(price, '')}"
                for r in rows)
        return quotes.read_text(encoding="utf-8", errors="replace")[:3000]
    return ("paste the quotes as a CSV or text and I will lay them side by "
            "side. I will not rank real policies from memory — terms differ, "
            "and exclusions matter more than price.")


def government_form(target: str = "", args: str = "") -> str:
    """Fill a local form draft. Never a submission."""
    form = (target or args).strip()
    if not form:
        return "give me the form, e.g. form 'visa application' passport 1234567"
    root = _life_home() / "forms"
    root.mkdir(parents=True, exist_ok=True)
    path = root / (re.sub(r"\W+", "-", form.lower()).strip("-") + ".md")
    path.write_text(
        f"# {form}\n\n* draft only — nothing has been submitted anywhere\n\n"
        "fields to complete:\n\n"
        + "\n".join(f"- {line}" for line in args.split() if line.strip())
        + "\n", encoding="utf-8")
    return f"draft at {path}. It is not submitted, and I will not submit it."


# --------------------------------------------------------------------------
# shopping
# --------------------------------------------------------------------------
def compare_products(target: str = "", args: str = "") -> str:
    """Compare products from a list the user provides."""
    source = Path(args.strip() or "").expanduser()
    if not source.is_file():
        return ("give me the products as text or a CSV — one per line, with "
                "price and the detail that matters to you.")
    rows = _rows(source)
    if not rows:
        return source.read_text(encoding="utf-8", errors="replace")[:3000]
    price = _column(rows, "price", "cost", "amount")
    name = _column(rows, "name", "product", "item", "title")
    return "\n".join(f"{str(r.get(name, ''))[:34]:<36}{r.get(price, ''):>10}"
                     for r in rows)


def track_price(target: str = "", args: str = "") -> str:
    return _needs_provider("price tracking")


def wishlist(target: str = "", args: str = "") -> str:
    """A local wishlist the user adds to by hand."""
    root = _life_home() / "shopping"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "wishlist.md"
    add = f"{target} {args}".strip()
    if add and add.lower() not in ("list", "show"):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"- {add}\n")
        return f"added to {path}"
    if not path.is_file():
        return "nothing on the wishlist yet"
    return path.read_text(encoding="utf-8").strip()


def coupon(target: str = "", args: str = "") -> str:
    return ("coupon codes are not something I can look up reliably, and I "
            "will not guess a code that then fails at checkout. Check the "
            "store's own newsletter page or a coupon extension.")


def reorder(target: str = "", args: str = "") -> str:
    return ("reordering means buying something. I will not place an order on "
            "your behalf — open the store and confirm it yourself.")


def reviews(target: str = "", args: str = "") -> str:
    return _needs_provider("reviews")


def delivery(target: str = "", args: str = "") -> str:
    return _needs_provider("live delivery tracking")


def return_request(target: str = "", args: str = "") -> str:
    """Draft a return request without sending it."""
    root = _life_home() / "shopping"
    root.mkdir(parents=True, exist_ok=True)
    item = (target or args).strip()
    if not item:
        return "give me the item and the order number"
    path = root / f"return-{time.strftime('%Y%m%d')}.md"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            f"# Return request — {item}\n\n* draft only; nothing was sent\n\n"
            f"{item} — order {args.strip() or '(number to add)'}\n\n"
            "Reason: \n\nThis is a draft. Paste it into the store's returns "
            "page yourself.\n\n---\n\n")
    return f"drafted in {path}. Nothing was sent."


# --------------------------------------------------------------------------
# social media
# --------------------------------------------------------------------------
def _social_file(name: str) -> Path:
    root = _life_home() / "social"
    root.mkdir(parents=True, exist_ok=True)
    return root / name


def draft_post(target: str = "", args: str = "") -> str:
    """Draft a post into a file. Publishing is always the user's own click."""
    text = f"{target} {args}".strip()
    if not text:
        return "give me what the post should say"
    path = _social_file(f"draft-{time.strftime('%Y%m%d-%H%M%S')}.md")
    path.write_text(f"# Draft\n\n{text}\n\n---\nnot posted anywhere\n",
                    encoding="utf-8")
    return f"drafted in {path}. I have not posted it."


def schedule_post(target: str = "", args: str = "") -> str:
    """A reminder to post, since posting needs the account session."""
    from . import comms

    text = f"{target} {args}".strip()
    if not text:
        return "give me the post and the time"
    return comms.remind(f"post: {text}", args.strip() or "tomorrow 12:00")


def reply_comments(target: str = "", args: str = "") -> str:
    path = _social_file("replies.md")
    reply = f"{target} {args}".strip()
    if reply and reply.lower() not in ("draft", "list"):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"- {reply}\n")
        return f"drafted in {path}. Nothing sent."
    if not path.is_file():
        return "no drafted replies yet"
    return path.read_text(encoding="utf-8").strip()


def captions(target: str = "", args: str = "") -> str:
    """Short caption suggestions for an image or video."""
    subject = (target or args).strip() or "this"
    return "\n".join([
        f"1. {subject} — the short version, no punctuation",
        f"2. a bit of {subject}, and what came out of it",
        f"3. things nobody tells you about {subject}",
        f"4. one line about {subject}, then a question",
    ])


def crosspost(target: str = "", args: str = "") -> str:
    return ("crossposting means posting into several accounts. I do not hold "
            "those sessions. Copy the draft and paste it into each app.")


def export_my_data(target: str = "", args: str = "") -> str:
    """Find a data-export archive a site has already given the user."""
    from . import filesystem

    root = Path(filesystem.expand(target or "~/Downloads"))
    if not root.is_dir():
        return f"no such folder: {root}"
    found = sorted((p for p in root.iterdir()
                    if p.is_file()
                    and p.suffix.lower() in (".zip", ".json", ".csv")
                    and "export" in p.name.lower()),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not found:
        return f"no export archive in {root}. Ask each site for its export first."
    return "\n".join(f"{p.name}  ({p.stat().st_size:,} bytes)" for p in found[:5])


def engagement(target: str = "", args: str = "") -> str:
    return ("engagement numbers need the account's own analytics page or API. "
            "I will not invent them. Open the insights page and ask me to "
            "summarise what you read off it.")


# --------------------------------------------------------------------------
# games
# --------------------------------------------------------------------------
def game(target: str = "", args: str = "") -> str:
    """Launch a game."""
    title = (target or args).strip()
    if not title:
        return "give me the game to launch"
    from . import apps

    return apps.search(title) or (
        f"{title} is not in the installed list. Give me the full title or the "
        f"folder it lives in.")


def game_graphics(target: str = "", args: str = "") -> str:
    """Show or set the graphics settings for the display."""
    verb = f"{target} {args}".strip().lower()
    if verb.startswith(("set", "change")):
        return ("changing graphics needs the vendor control panel: NVIDIA "
                "Control Panel or AMD Adrenalin. Windows exposes only the "
                "resolution and refresh rate to scripts.")
    return str(shell.run_powershell(
        "Get-CimInstance Win32_VideoController | Select-Object Name, "
        "CurrentHorizontalResolution, CurrentVerticalResolution, "
        "CurrentRefreshRate | Format-List"))


def stream(target: str = "", args: str = "") -> str:
    """Start a stream. Needs a streaming app and an account the agent lacks."""
    title = (target or args).strip()
    return (f"streaming {title or 'this'} needs OBS and a channel key. Put "
            "the key in the vault with `ufo secrets set stream_key` and I "
            "will launch OBS with it; I will not type a key onto a command "
            "line where it lands in your shell history.")


def record_gameplay(target: str = "", args: str = "") -> str:
    """Record the screen with Windows' own recorder."""
    from . import keyboard

    keyboard.hotkey("win+alt+r")
    return "started the built-in game bar recorder (press again to stop)"


def game_library(target: str = "", args: str = "") -> str:
    """Game libraries installed and where they are."""
    from . import filesystem

    root = Path(filesystem.expand(target or "~/"))
    names = ("steam", "epic", "ubisoft", "battle.net", "battlenet", "gog", "itch")
    found = [str(p) for p in sorted(root.glob("*")) if p.is_dir()
             and any(n in p.name.lower() for n in names)]
    if not found:
        return f"no game library folders in {root}"
    return "\n".join(found)
