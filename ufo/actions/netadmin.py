"""Network and connectivity: adapters, Wi-Fi, DNS, ping, firewall, speed.

Backs "fix my wifi", "what is my IP" and "why is the internet slow". The
diagnostic actions here read state and report it; the ones that change it are
marked in the catalog so the guard can ask first.
"""
from __future__ import annotations

import re

from ..errors import ActionError
from ..logging_setup import get_logger

from . import network, shell

log = get_logger(__name__)


def profile(target: str = "", args: str = "") -> str:
    """The whole network picture in one answer."""
    lines = [f"local {network.local_ip()}", f"public {network.public_ip()}",
             f"online: {'yes' if network.is_online() else 'no'}"]
    adapters = network.adapters()
    if adapters:
        lines.append(f"adapters: {adapters}")
    wifi = network.wifi_networks()
    if wifi:
        lines.append(f"wifi nearby: {wifi}")
    return "\n".join(lines)


def wifi_list(target: str = "", args: str = "") -> str:
    return str(network.wifi_networks()) or "no wifi networks found"


def wifi_connect(target: str = "", args: str = "") -> str:
    """target is the SSID; args may carry 'password' or 'profile|SSID'."""
    if not target:
        return "give me the network name"
    password, profile = "", ""
    for piece in (args or "").split("|"):
        piece = piece.strip()
        if piece.lower().startswith("profile"):
            _, _, profile = piece.partition(" ")
        elif piece:
            password = piece
    return str(network.connect_wifi(target, password, profile))


def wifi_forget(target: str = "", args: str = "") -> str:
    if not target:
        return "give me the network name"
    return str(network.forget_wifi(target))


def dns(target: str = "", args: str = "") -> str:
    """Show DNS servers, or set them. target='set' with args='1.1.1.1 8.8.8.8'."""
    if target.strip().lower() in ("set", "change", "use"):
        servers = args.split()
        if not servers:
            return "give me the DNS servers to use, e.g. 1.1.1.1 8.8.8.8"
        return _set_dns(servers)
    return str(network.ip_config())


def _set_dns(servers: list[str]) -> str:
    if not network.is_admin():
        return ("changing DNS needs an administrator shell. "
                "Run: Start-Process powershell -Verb RunAs")
    interface = _up_interface()
    if not interface:
        return "no active network interface found"
    for server in servers:
        shell.run_cmd(f'netsh interface ip set dns name="{interface}" '
                      f'static {server}')
    shell.run_cmd(f'netsh interface ip set dns name="{interface}" '
                  f'static {servers[-1]}')
    shell.run_cmd("ipconfig /flushdns")
    return f"DNS set to {', '.join(servers)} on {interface}"


def _up_interface() -> str:
    result = str(shell.run_powershell(
        "(Get-NetAdapter | Where-Object {$_.Status -eq 'Up'} | "
        "Select-Object -First 1).Name"))
    return result.strip()


def hotspot(target: str = "", args: str = "") -> str:
    """Mobile hotspot on/off. Windows exposes no API, so this reports honestly."""
    return ("Windows exposes no scriptable API for the mobile hotspot. "
            "Turn it on in Settings > Network & Internet > Mobile hotspot.")


def ping(target: str = "", args: str = "") -> str:
    host = target or "1.1.1.1"
    return str(network.ping(host, count=int(args) if args.isdigit() else 4))


def trace(target: str = "", args: str = "") -> str:
    return str(network.trace_route(target or "1.1.1.1"))


def open_ports(target: str = "", args: str = "") -> str:
    return str(network.ports())


def firewall_status(target: str = "", args: str = "") -> str:
    return str(network.firewall_status())


def speed_test(target: str = "", args: str = "") -> str:
    """Measure download throughput against a fixed public file."""
    import time
    import urllib.request

    url = "https://speed.cloudflare.com/__down?bytes=10000000"
    try:
        started = time.time()
        with urllib.request.urlopen(url, timeout=30) as response:
            data = response.read(10_000_000)
        seconds = max(0.001, time.time() - started)
    except Exception as e:
        return f"speed test failed: {type(e).__name__}: {e}"
    mbps = (len(data) * 8 / 1_000_000) / seconds
    return f"about {mbps:.1f} Mbps down ({len(data) / 1e6:.0f} MB in {seconds:.1f}s)"


def adapters(target: str = "", args: str = "") -> str:
    return str(network.adapters()) or "no adapters found"


def ip_config(target: str = "", args: str = "") -> str:
    return str(network.ip_config())


def flush_dns(target: str = "", args: str = "") -> str:
    shell.run_cmd("ipconfig /flushdns")
    return "DNS cache flushed"


def devices(target: str = "", args: str = "") -> str:
    return str(shell.run_powershell(
        "Get-PnpDevice -Class Net | Select-Object FriendlyName, Status | "
        "Format-Table -AutoSize"))


def block_device(target: str = "", args: str = "") -> str:
    """Disable or re-enable a network device in Device Manager."""
    name = (target or args).strip()
    if not name:
        return "give me the device name"
    if "unblock" in f"{target} {args}".lower() or "enable" in f"{target} {args}".lower():
        verb = "Enable"
    else:
        verb = "Disable"
    result = str(shell.run_powershell(
        f"Get-NetAdapter -Name '*{name}*' | {verb}-NetAdapter -Confirm:$false"))
    return (f"{verb}d {name}" if "error" not in result.lower()
            else f"could not {verb.lower()} {name}: {result[:200]}")


def port_check(target: str = "", args: str = "") -> str:
    """Can we reach a host on a port, and how long does it take."""
    host = (target or args).strip()
    if not host:
        return "give me a host, e.g. example.com 443"
    return str(network.ping(host, count=2))


def vpn(target: str = "", args: str = "") -> str:
    """List VPN profiles. Connecting needs a credential the agent should not hold."""
    return str(shell.run_powershell(
        "Get-VpnConnection | Select-Object Name, ServerAddress, TunnelType | "
        "Format-Table -AutoSize"))


def static_ip(target: str = "", args: str = "") -> str:
    """Set a static address on the active adapter: target='10.0.0.5/24'."""
    address = (target or args).strip()
    if not address:
        return str(network.ip_config())
    interface = _up_interface()
    if not interface:
        return "no active network interface found"
    ip, _, prefix = address.partition("/")
    gateway = args.split()[-1] if args.split() else ""
    result = str(shell.run_powershell(
        f"New-NetIPAddress -InterfaceAlias '{interface}' -IPAddress '{ip}' "
        f"-PrefixLength {prefix or 24}"))
    if "error" in result.lower():
        return f"could not set the address: {result[:200]}"
    if gateway:
        shell.run_powershell(
            f"New-NetRoute -InterfaceAlias '{interface}' -DestinationPrefix "
            f"'0.0.0.0/0' -NextHop '{gateway}'")
    return f"static address {ip} set on {interface}"


def fix_no_internet(target: str = "", args: str = "") -> str:
    """The standard "it says no internet" ladder, cheapest step first."""
    steps = []
    steps.append("releasing the DHCP lease: " + str(shell.run_cmd("ipconfig /release")))
    steps.append("renewing it: " + str(shell.run_cmd("ipconfig /renew")))
    steps.append("flushing DNS: " + str(shell.run_cmd("ipconfig /flushdns")))
    steps.append("resetting the stack: " + str(shell.run_powershell("netsh int ip reset")))
    steps.append(f"still online? {network.is_online()}")
    return "\n".join(steps)


def reset_stack(target: str = "", args: str = "") -> str:
    shell.run_powershell("netsh winsock reset")
    shell.run_powershell("netsh int ip reset")
    return "winsock and IP stack reset — reboot to finish"
