"""Command line entry point: ``python3 -m audiosync``.

The GUI is the default.  The headless verbs exist because the plan's build
order checks the risky parts (units, discovery, roles) before any window
is involved.
"""

from __future__ import annotations

import argparse
import sys
import time

from . import __version__, config, transport
from .backend import Backend, BackendError, reachable, server_ready, unit_active
from .discovery import Discovery, resolve_host


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="audiosync",
        description="Sample-locked audio between machines on your LAN.",
    )
    parser.add_argument("--gui", action="store_true", help="open the window (default)")
    parser.add_argument("--list", action="store_true", help="list discovered peers")
    parser.add_argument("--host", action="store_true", help="become the audio host")
    parser.add_argument("--join", metavar="HOST", help="join a host by address")
    parser.add_argument("--stop", action="store_true", help="stop server and client here")
    parser.add_argument("--status", action="store_true", help="print unit and client status")
    parser.add_argument("--buffer", type=int, metavar="MS", help="buffer to use when hosting")
    parser.add_argument("--playback", choices=config.PLAYBACK_MODES, help="synced|direct|off")
    parser.add_argument(
        "--transport",
        choices=transport.POLICIES,
        help="auto|lan|btpan; btpan fails closed when no PAN link is up",
    )
    parser.add_argument("--resolve", metavar="NAME", help=argparse.SUPPRESS)
    parser.add_argument("--smoke", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"audiosync {__version__}")
    return parser.parse_args(argv)


def cmd_list(seconds: float = 3.0) -> int:
    discovery = Discovery()
    if not discovery.available:
        print("python3-zeroconf is not installed; discovery unavailable")
        return 1
    discovery.start()
    time.sleep(seconds)
    peers = discovery.peers()
    if not peers:
        print("no peers found (are they running the app?)")
    for peer in peers:
        paths = " ".join(f"{endpoint.kind}:{endpoint.address}" for endpoint in peer.endpoints) or peer.address
        print(f"{peer.name:<20} {paths:<40} role={peer.role:<7} buffer={peer.buf_ms}ms preset={peer.preset}")
    discovery.stop()
    return 0


def _load_cli_config() -> config.Config | None:
    """Load config for host/join without silently repairing hostile values."""
    try:
        return config.Config.load(strict=True)
    except config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def cmd_host(args: argparse.Namespace) -> int:
    cfg = _load_cli_config()
    if cfg is None:
        return 1
    if args.playback:
        cfg.playback = args.playback
    if args.transport:
        cfg.transport = args.transport
    backend = Backend(cfg)
    buffer_ms = args.buffer or cfg.effective_buffer_ms()
    try:
        backend.host(buffer_ms=buffer_ms, codec=cfg.codec, playback=cfg.playback)
    except BackendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        cfg.save()
    except OSError as exc:
        print(f"error: could not save config: {exc}", file=sys.stderr)
        return 1
    print(f"hosting on 0.0.0.0:{config.SNAPCAST_STREAM_PORT} buffer={buffer_ms}ms playback={cfg.playback}")
    return 0


def cmd_join(host: str) -> int:
    cfg = _load_cli_config()
    if cfg is None:
        return 1
    address = resolve_host(host) or host
    backend = Backend(cfg)
    try:
        # Resolve the local hardware output before snapclient starts.  Without
        # this, a fresh config has no explicit --soundcard and the safety check
        # correctly refuses rather than falling back to the virtual sink.
        backend.refresh_outputs(hosting=False, restart=False)
        backend.join(address, host_name="" if address == host else host)
    except BackendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    ok = server_ready(address, timeout=3.0)
    print(f"joined {address} ({'reachable' if ok else 'no reply on :1780 yet'})")
    if address != host:
        print(f"resolved {host} -> {address}")
    return 0


def cmd_stop() -> int:
    cfg = config.Config.load()
    Backend(cfg).stop()
    print("stopped")
    return 0


def cmd_status() -> int:
    cfg = config.Config.load()
    backend = Backend(cfg)
    peer = cfg.preferred_peer or cfg.preferred_host or "this device"
    print(f"transport={cfg.transport} peer={peer}")
    for unit in ("snapserver.service", "snapcast-capture.service", "snapclient.service"):
        print(f"{unit:<28} {'active' if unit_active(unit) else 'inactive'}")
    for host in ("127.0.0.1", cfg.preferred_host or ""):
        if not host:
            continue
        if not reachable(host, config.SNAPCAST_HTTP_PORT, timeout=0.5):
            continue
        print(f"--- server {host}")
        try:
            for row in backend.clients(host):
                print(f"  {row['name']:<20} connected={row['connected']} volume={row['volume']}")
        except BackendError as exc:
            print(f"  {exc}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    if args.resolve:
        address = resolve_host(args.resolve)
        if not address:
            print(f"could not resolve {args.resolve}", file=sys.stderr)
            return 1
        print(address)
        return 0
    if args.smoke:
        from .app import run_gui

        return run_gui(smoke=True)
    if args.list:
        return cmd_list()
    if args.host:
        return cmd_host(args)
    if args.join:
        return cmd_join(args.join)
    if args.stop:
        return cmd_stop()
    if args.status:
        return cmd_status()
    from .app import run_gui

    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
