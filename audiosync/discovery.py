"""Peer discovery over mDNS, with a manual-IP fallback.

Two records are watched: our own ``_audiosync._tcp`` (so the app knows who
is running it and in which role) and snapserver's ``_snapcast._tcp`` (so we
can see who is actually serving audio right now, even if their app is shut).
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field

from . import config, transport

try:  # exercised by the GUI and CLI both
    from zeroconf import ServiceBrowser, ServiceInfo, ServiceStateChange, Zeroconf
except Exception:  # pragma: no cover - exercised only when the dep is absent
    ServiceBrowser = None  # type: ignore[assignment]
    ServiceInfo = None  # type: ignore[assignment]
    ServiceStateChange = None  # type: ignore[assignment]
    Zeroconf = None  # type: ignore[assignment]

STALE_AFTER_S = 90.0

def lan_addresses() -> list[str]:
    """IPv4 addresses that can actually carry multicast on this LAN.

    Every usable NIC is included: a device with both Ethernet and Wi-Fi must
    advertise both, otherwise peers only ever learn the path that happened to
    be enumerated first.
    """
    return [endpoint.address for endpoint in transport.local_endpoints()]


def local_ip() -> str:
    """Best-effort LAN address, without sending any packets."""
    return transport.default_address()


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
    endpoints: list[transport.Endpoint] = field(default_factory=list)
    last_seen: float = field(default_factory=time.time)

    def all_addresses(self) -> list[str]:
        """Every address this peer advertised, primary first."""
        addresses = [endpoint.address for endpoint in self.endpoints]
        if self.address and self.address not in addresses:
            addresses.insert(0, self.address)
        return addresses

    def best_endpoint(
        self,
        policy: str = "auto",
        local_addresses: list[str] | None = None,
    ) -> transport.Endpoint | None:
        endpoints = self.endpoints or [transport.Endpoint(self.address)]
        return transport.choose_endpoint(endpoints, policy, local_addresses)

    @property
    def is_host(self) -> bool:
        return self.role == "host"

    @property
    def is_self(self) -> bool:
        return self.ident and self.ident == config.machine_id()


class Discovery:
    """Announce this device and collect everyone else doing the same."""

    def __init__(self, name: str | None = None) -> None:
        self._zc = None
        self._info = None
        self._browser = None
        self._peers: dict[str, Peer] = {}
        self._manual: dict[str, Peer] = {}
        self.available = Zeroconf is not None
        self._props: dict[str, str] = {}
        self._name = (name or "").strip() or config.device_name()
        self._endpoint_sig: tuple[tuple[str, str], ...] | None = None

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
            self._endpoint_sig = self._signature()
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
    def _signature(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((endpoint.address, endpoint.kind) for endpoint in transport.local_endpoints()))

    def refresh_addresses(self) -> bool:
        """Re-announce when a NIC appears, disappears, or gets a new lease.

        Without this a device that moves from Wi-Fi to Ethernet keeps
        advertising an address nobody can reach until the app restarts.
        """
        signature = self._signature()
        if not signature or signature == self._endpoint_sig:
            return False
        if self._endpoint_sig is None:
            self._endpoint_sig = signature
            return False
        self._endpoint_sig = signature
        if self._zc is None:
            return False
        self._unregister()
        self._register()
        return True

    def _register(self) -> None:
        assert self._zc is not None and ServiceInfo is not None
        try:
            endpoints = transport.local_endpoints()
            addresses = [socket.inet_aton(endpoint.address) for endpoint in endpoints if endpoint.usable]
            if not addresses:
                addresses = [socket.inet_aton(transport.default_address())]
            properties = dict(self._props)
            properties["ep"] = transport.format_endpoint_props(endpoints)
            info = ServiceInfo(
                config.DISCOVERY_TYPE,
                f"{self._name}.{config.DISCOVERY_TYPE}",
                addresses=addresses,
                port=config.SNAPCAST_HTTP_PORT,
                properties=properties,
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
        addresses = [socket.inet_ntoa(raw) for raw in (info.addresses or []) if len(raw) == 4]
        endpoints = transport.parse_endpoint_props(props.get("ep", ""))
        known = {endpoint.address for endpoint in endpoints}
        for address in addresses:
            if address not in known:
                endpoints.append(transport.Endpoint(address))
        if not endpoints:
            return
        short = name.split(".")[0]
        peer = Peer(
            name=short,
            address=endpoints[0].address,
            port=info.port or config.SNAPCAST_HTTP_PORT,
            role=props.get("role", "idle"),
            serves=props.get("serves", ""),
            buf_ms=int(props.get("buf", "0") or 0),
            preset=props.get("preset", "auto"),
            ident=props.get("id", ""),
            endpoints=endpoints,
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
        fresh = [peer for peer in self._peers.values() if now - peer.last_seen < STALE_AFTER_S]
        combined: dict[str, Peer] = {}
        seen_addresses: set[str] = set()
        for peer in fresh:
            key = peer.ident or peer.address
            existing = combined.get(key)
            if existing is None:
                combined[key] = peer
            else:
                # The same device answers on every NIC.  Keep one peer and
                # merge the paths so the policy can choose between them.
                kept = peer if len(peer.endpoints) > len(existing.endpoints) else existing
                present = {endpoint.address for endpoint in kept.endpoints}
                for endpoint in (*existing.endpoints, *peer.endpoints):
                    if endpoint.address not in present:
                        kept.endpoints.append(endpoint)
                        present.add(endpoint.address)
                if kept.address and kept.address not in present:
                    kept.endpoints.insert(0, transport.Endpoint(kept.address))
                combined[key] = kept
            seen_addresses.update(peer.all_addresses())
        for peer in self._manual.values():
            if peer.address in seen_addresses:
                continue
            combined.setdefault(peer.address, peer)
            seen_addresses.add(peer.address)
        return sorted(combined.values(), key=lambda item: item.name.lower())

    def hosts(self) -> list[Peer]:
        return [p for p in self.peers() if p.is_host]

    def find(self, name_or_address: str) -> Peer | None:
        wanted = (name_or_address or "").strip()
        if not wanted:
            return None
        for peer in self.peers():
            if wanted in (peer.name, peer.address, peer.ident):
                return peer
            if wanted in peer.all_addresses():
                return peer
        return None


def resolve_host(name: str, timeout: float = 1.2) -> str:
    """Resolve a peer label to an address, across a network change.

    Order: literal address, system resolver (``/etc/hosts`` or nss-mdns),
    then an mDNS lookup of the app's own service.  Returns ``""`` when the
    peer cannot be found right now, so callers keep their last known address
    instead of guessing.
    """
    text = (name or "").strip().rstrip(".")
    if not text:
        return ""
    try:
        socket.inet_aton(text)
        return text
    except OSError:
        pass
    for candidate in (text, f"{text}.local"):
        try:
            infos = socket.getaddrinfo(candidate, None, socket.AF_INET)
        except OSError:
            continue
        for info in infos:
            address = str(info[4][0])
            if address and not address.startswith(("127.", "0.0.0.0")):
                return address
    if Zeroconf is None:
        return ""
    try:
        zc = Zeroconf(interfaces=lan_addresses() or None)
    except Exception:
        try:
            zc = Zeroconf()
        except Exception:
            return ""
    try:
        info = zc.get_service_info(
            config.DISCOVERY_TYPE,
            f"{text}.{config.DISCOVERY_TYPE}",
            timeout=int(max(0.2, timeout) * 1000),
        )
        for raw in (info.addresses if info else []) or []:
            address = socket.inet_ntoa(raw)
            if address and not address.startswith(("127.", "169.254.")):
                return address
        return ""
    except Exception:
        return ""
    finally:
        try:
            zc.close()
        except Exception:
            pass
