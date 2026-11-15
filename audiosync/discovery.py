"""Peer discovery over mDNS, with a manual-IP fallback.

Two records are watched: our own ``_audiosync._tcp`` (so the app knows who
is running it and in which role) and snapserver's ``_snapcast._tcp`` (so we
can see who is actually serving audio right now, even if their app is shut).
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field

from . import config

try:  # exercised by the GUI and CLI both
    from zeroconf import ServiceBrowser, ServiceInfo, ServiceStateChange, Zeroconf
except Exception:  # pragma: no cover - exercised only when the dep is absent
    ServiceBrowser = None  # type: ignore[assignment]
    ServiceInfo = None  # type: ignore[assignment]
    ServiceStateChange = None  # type: ignore[assignment]
    Zeroconf = None  # type: ignore[assignment]

STALE_AFTER_S = 90.0

# Adapters that cannot carry mDNS multicast usefully.  ZeroTier/Tailscale-style
# point-to-point links make ``Zeroconf()`` join 224.0.0.251 everywhere, and on
# Fedora 44 that made peer discovery intermittent: each device saw only some of
# the others.  Restricting the socket to real LAN addresses fixes it.
_SKIP_ADAPTER_PREFIXES = ("tailscale", "tun", "wg", "docker", "veth", "virbr", "lo")


def lan_addresses() -> list[str]:
    """IPv4 addresses that can actually carry multicast on this LAN."""
    addrs: list[str] = []
    try:
        import ifaddr  # a zeroconf dependency, so always present when mDNS works

        for adapter in ifaddr.get_adapters():
            if (adapter.nice_name or "").startswith(_SKIP_ADAPTER_PREFIXES):
                continue
            for ip in adapter.ips:
                if isinstance(ip.ip, str) and not ip.ip.startswith(("127.", "169.254.")):
                    addrs.append(ip.ip)
    except Exception:
        pass
    return addrs


def local_ip() -> str:
    """Best-effort LAN address, without sending any packets."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


@dataclass
class Peer:
    name: str
    address: str
    port: int = config.SNAPCAST_HTTP_PORT
    role: str = "idle"
    serves: str = ""
    buf_ms: int = 0
    preset: str = "auto"
    ident: str = ""
    source: str = "mdns"  # or "manual"
    last_seen: float = field(default_factory=time.time)

    @property
    def is_host(self) -> bool:
        return self.role == "host"

    @property
    def is_self(self) -> bool:
        return self.ident and self.ident == config.machine_id()


class Discovery:
    """Announce this device and collect everyone else doing the same."""

    def __init__(self) -> None:
        self._zc = None
        self._info = None
        self._browser = None
        self._peers: dict[str, Peer] = {}
        self._manual: dict[str, Peer] = {}
        self.available = Zeroconf is not None
        self._props: dict[str, str] = {}
        self._name = config.device_name()

    # -- lifecycle ----------------------------------------------------
    def start(self, role: str = "idle", serves: str = "", buf_ms: int = 250, preset: str = "auto") -> None:
        self._props = {
            "v": "1",
            "role": role,
            "serves": serves,
            "buf": str(buf_ms),
            "preset": preset,
            "id": config.machine_id(),
        }
        if not self.available or self._zc is not None:
            return
        try:
            interfaces = lan_addresses()
            self._zc = Zeroconf(interfaces=interfaces) if interfaces else Zeroconf()
            self._register()
            self._browser = ServiceBrowser(self._zc, config.DISCOVERY_TYPE, handlers=[self._on_change])
        except Exception:
            self._zc = None
            self.available = False

    def update(self, role: str | None = None, serves: str | None = None, buf_ms: int | None = None, preset: str | None = None) -> None:
        changed = False
        for key, value in (("role", role), ("serves", serves), ("preset", preset)):
            if value is not None and self._props.get(key) != str(value):
                self._props[key] = str(value)
                changed = True
        if buf_ms is not None and self._props.get("buf") != str(buf_ms):
            self._props["buf"] = str(buf_ms)
            changed = True
        if changed and self._zc is not None:
            self._unregister()
            self._register()

    def stop(self) -> None:
        self._unregister()
        if self._browser is not None:
            try:
                self._browser.cancel()
            except Exception:
                pass
            self._browser = None
        if self._zc is not None:
            try:
                self._zc.close()
            except Exception:
                pass
            self._zc = None

    # -- zeroconf plumbing -------------------------------------------
    def _register(self) -> None:
        assert self._zc is not None and ServiceInfo is not None
        try:
            address = (lan_addresses() or [local_ip()])[0]
            info = ServiceInfo(
                config.DISCOVERY_TYPE,
                f"{self._name}.{config.DISCOVERY_TYPE}",
                addresses=[socket.inet_aton(address)],
                port=config.SNAPCAST_HTTP_PORT,
                properties=self._props,
                server=f"{self._name}.local.",
            )
            self._zc.register_service(info)
            self._info = info
        except Exception:
            self._info = None

    def _unregister(self) -> None:
        if self._info is not None and self._zc is not None:
            try:
                self._zc.unregister_service(self._info)
            except Exception:
                pass
            self._info = None

    def _on_change(self, zeroconf, service_type, name, state_change) -> None:  # noqa: ANN001
        if state_change is not ServiceStateChange.Removed:
            self._absorb(zeroconf, service_type, name)
        else:
            self._peers.pop(name, None)

    def _absorb(self, zeroconf, service_type: str, name: str) -> None:  # noqa: ANN001
        try:
            info = zeroconf.get_service_info(service_type, name, timeout=1500)
        except Exception:
            return
        if info is None:
            return
        props = {
            (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
            for k, v in (info.properties or {}).items()
            if v is not None
        }
        address = socket.inet_ntoa(info.addresses[0]) if info.addresses else ""
        if not address:
            return
        short = name.split(".")[0]
        peer = Peer(
            name=short,
            address=address,
            port=info.port or config.SNAPCAST_HTTP_PORT,
            role=props.get("role", "idle"),
            serves=props.get("serves", ""),
            buf_ms=int(props.get("buf", "0") or 0),
            preset=props.get("preset", "auto"),
            ident=props.get("id", ""),
        )
        if peer.is_self:
            return
        self._peers[name] = peer

    # -- queries ------------------------------------------------------
    def add_manual(self, host: str, port: int = config.SNAPCAST_HTTP_PORT) -> Peer:
        """For paths mDNS does not cross (different subnet, Tailscale)."""
        peer = Peer(name=host, address=host, port=port, source="manual", last_seen=time.time())
        self._manual[host] = peer
        return peer

    def remove_manual(self, host: str) -> None:
        self._manual.pop(host, None)

    def peers(self) -> list[Peer]:
        now = time.time()
        fresh = [p for p in self._peers.values() if now - p.last_seen < STALE_AFTER_S]
        combined = {p.address: p for p in fresh}
        for peer in self._manual.values():
            combined.setdefault(peer.address, peer)
        return sorted(combined.values(), key=lambda p: p.name.lower())

    def hosts(self) -> list[Peer]:
        return [p for p in self.peers() if p.is_host]

    def find(self, name_or_address: str) -> Peer | None:
        for peer in self.peers():
            if name_or_address in (peer.name, peer.address):
                return peer
        return None
