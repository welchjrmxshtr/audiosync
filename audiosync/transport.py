"""Address selection across wired, wireless and Bluetooth PAN links.

The app must not assume one NIC or one address.  Enumeration touches the
system; every decision is a pure function so the ranking rules are testable
without hardware.

A Bluetooth PAN (``bnep*``) is a point-to-point link that skips the router.  It
is offered as an *explicit* transport, never picked silently: PAN is narrower
than both Ethernet and Wi-Fi (a PCM stream alone is 1.536 Mbit/s), so opting in
is a user decision and the codec ceiling follows from it.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path

WIRED = "wired"
WIFI = "wifi"
BTPAN = "btpan"
OTHER = "other"

#: User-facing transport policy.  ``auto`` never selects a PAN on its own.
POLICIES = ("auto", "lan", "btpan")

#: Lower wins when several paths reach the same peer.
_KIND_RANK = {WIRED: 0, WIFI: 1, OTHER: 2, BTPAN: 3}

_BTPAN_PREFIXES = ("bnep", "bt-pan", "btpan")
_WIRED_PREFIXES = ("en", "eth", "em")
_VIRTUAL_PREFIXES = (
    "docker",
    "veth",
    "virbr",
    "br-",
    "tun",
    "wg",
    "tailscale",
    "zt",
    "dummy",
    "lo",
)
_SYS_NET = Path("/sys/class/net")


@dataclass(frozen=True)
class Endpoint:
    """One address a peer (or this device) can be reached at."""

    address: str
    interface: str = ""
    kind: str = OTHER

    @property
    def usable(self) -> bool:
        try:
            ip = ipaddress.IPv4Address(self.address)
        except ipaddress.AddressValueError:
            return False
        return not (ip.is_loopback or ip.is_link_local or ip.is_unspecified)


def _base_interface(name: object) -> str:
    text = str(name or "").strip()
    return text.split("@", 1)[0].strip()


def is_virtual_interface(name: object) -> bool:
    """True for adapters that cannot carry usable LAN audio traffic."""
    text = _base_interface(name).lower()
    return bool(text) and text.startswith(_VIRTUAL_PREFIXES)


def classify_interface(name: object, wireless: bool | None = None) -> str:
    """Classify an interface name without trusting a single naming scheme.

    ``wireless`` may be supplied by the caller (or a test); otherwise the
    kernel's ``/sys/class/net`` entries are consulted.  Unknown names are
    ``OTHER`` rather than being guessed as Ethernet.
    """
    text = _base_interface(name).lower()
    if not text:
        return OTHER
    if text.startswith(_BTPAN_PREFIXES):
        return BTPAN
    if text.startswith(_VIRTUAL_PREFIXES):
        return OTHER
    if wireless is None:
        base = _SYS_NET / text
        wireless = (base / "wireless").exists() or (base / "phy80211").exists()
    if wireless:
        return WIFI
    if text.startswith(_WIRED_PREFIXES):
        return WIRED
    return OTHER


def default_address() -> str:
    """Best-effort primary address, without sending packets.

    The UDP connect is to TEST-NET-1, which is never routed; it only asks the
    kernel which local address the default route would use.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))
        return str(sock.getsockname()[0])
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


def local_endpoints() -> list[Endpoint]:
    """Every usable IPv4 endpoint on this device, best path first."""
    found: dict[str, Endpoint] = {}
    try:
        import ifaddr  # a zeroconf dependency, so present whenever mDNS works

        for adapter in ifaddr.get_adapters():
            interface = _base_interface(getattr(adapter, "nice_name", ""))
            if is_virtual_interface(interface):
                continue
            kind = classify_interface(interface)
            for ip in adapter.ips:
                if not isinstance(ip.ip, str):
                    continue
                endpoint = Endpoint(ip.ip, interface, kind)
                if endpoint.usable:
                    found.setdefault(endpoint.address, endpoint)
    except Exception:
        pass
    if not found:
        address = default_address()
        endpoint = Endpoint(address, "", OTHER)
        if endpoint.usable:
            found[address] = endpoint
    return sorted(found.values(), key=lambda item: (_KIND_RANK.get(item.kind, 9), item.interface, item.address))


def format_endpoint_props(endpoints: list[Endpoint]) -> str:
    """Encode endpoints for an mDNS TXT record (``kind|address,...``)."""
    parts = [f"{endpoint.kind}|{endpoint.address}" for endpoint in endpoints if endpoint.usable]
    return ",".join(parts)[:255]


def parse_endpoint_props(text: object) -> list[Endpoint]:
    """Decode ``format_endpoint_props`` tolerantly (a bare address is a kind)."""
    endpoints: list[Endpoint] = []
    for piece in str(text or "").split(","):
        piece = piece.strip()
        if not piece:
            continue
        kind, separator, address = piece.partition("|")
        if not separator:
            kind, address = OTHER, kind
        endpoint = Endpoint(address.strip(), "", kind if kind in _KIND_RANK else OTHER)
        if endpoint.usable:
            endpoints.append(endpoint)
    return endpoints


def _same_link(left: str, right: str) -> bool:
    """Same /24 (the LAN case the ARP path depends on); best-effort only."""
    return left.rsplit(".", 1)[0] == right.rsplit(".", 1)[0]


def choose_endpoint(
    endpoints: list[Endpoint] | tuple[Endpoint, ...] | None,
    policy: str = "auto",
    local_addresses: list[str] | None = None,
) -> Endpoint | None:
    """Pick one address for a peer, or ``None`` when the policy cannot be met.

    ``btpan`` fails closed instead of silently falling back to Wi-Fi, so a
    user who asked for the direct link hears about a down link rather than
    getting a quiet detour over the router.
    """
    usable = [endpoint for endpoint in (endpoints or []) if endpoint.usable]
    if not usable:
        return None
    if policy == BTPAN:
        direct = [endpoint for endpoint in usable if endpoint.kind == BTPAN]
        return min(direct, key=lambda item: item.address) if direct else None
    if policy == "lan":
        usable = [endpoint for endpoint in usable if endpoint.kind != BTPAN]
        if not usable:
            return None
    here = [address for address in (local_addresses or []) if address]

    def sort_key(endpoint: Endpoint) -> tuple[int, int, str]:
        near = 0 if any(_same_link(endpoint.address, address) for address in here) else 1
        return (near, _KIND_RANK.get(endpoint.kind, 9), endpoint.address)

    return min(usable, key=sort_key)


def validate_transport(value: object) -> str:
    """Return a supported policy or raise ``ValueError`` (callers wrap it)."""
    text = str(value or "").strip().lower()
    if text not in POLICIES:
        raise ValueError(f"transport must be one of {', '.join(POLICIES)}; got {value!r}")
    return text


def pan_status() -> dict:
    """What the Bluetooth PAN looks like right now, without privileges.

    Used only to explain a failed selection; nothing here changes the system.
    """
    endpoints = [endpoint for endpoint in local_endpoints() if endpoint.kind == BTPAN]
    return {
        "available": bool(endpoints),
        "interfaces": sorted({endpoint.interface for endpoint in endpoints if endpoint.interface}),
        "addresses": [endpoint.address for endpoint in endpoints],
    }


def link_label(endpoint: Endpoint | None) -> str:
    """Short human label for the status line."""
    if endpoint is None:
        return "unknown path"
    name = {"wired": "wired", "wifi": "Wi-Fi", "btpan": "Bluetooth"}.get(endpoint.kind, "network")
    return f"{name} {endpoint.address}".strip()
