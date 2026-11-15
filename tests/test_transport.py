"""Transport policy, peer identity and the Bluetooth PAN cap.

Pure helpers only: no adapters, no audio, no root.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiosync import codec, config, discovery, preload, transport


class ClassifyTests(unittest.TestCase):
    def test_known_interface_names(self) -> None:
        self.assertEqual(transport.classify_interface("bnep0"), transport.BTPAN)
        self.assertEqual(transport.classify_interface("wlp3s0", wireless=True), transport.WIFI)
        self.assertEqual(transport.classify_interface("enp4s0", wireless=False), transport.WIRED)
        self.assertEqual(transport.classify_interface("eth0", wireless=False), transport.WIRED)
        self.assertEqual(transport.classify_interface(""), transport.OTHER)

    def test_virtual_interfaces_are_rejected(self) -> None:
        for name in ("docker0", "veth1", "tailscale0", "wg0", "tun0", "lo", "virbr0"):
            self.assertTrue(transport.is_virtual_interface(name), name)
        self.assertFalse(transport.is_virtual_interface("enp4s0"))
        self.assertFalse(transport.is_virtual_interface("bnep0"))


def _endpoints() -> list[transport.Endpoint]:
    return [
        transport.Endpoint("192.0.2.12", "wlan0", transport.WIFI),
        transport.Endpoint("198.51.100.7", "enp4s0", transport.WIRED),
        transport.Endpoint("10.44.0.2", "bnep0", transport.BTPAN),
    ]


class PolicyTests(unittest.TestCase):
    def test_auto_prefers_wired_then_wifi_and_never_pan(self) -> None:
        chosen = transport.choose_endpoint(_endpoints(), "auto")
        assert chosen is not None
        self.assertEqual(chosen.address, "198.51.100.7")
        self.assertEqual(chosen.kind, transport.WIRED)
        wifi = [endpoint for endpoint in _endpoints() if endpoint.kind != transport.WIRED]
        fallback = transport.choose_endpoint(wifi, "auto")
        assert fallback is not None
        self.assertEqual(fallback.address, "192.0.2.12")

    def test_btpan_is_opt_in_and_fails_closed(self) -> None:
        chosen = transport.choose_endpoint(_endpoints(), "btpan")
        assert chosen is not None
        self.assertEqual(chosen.address, "10.44.0.2")
        wifi_only = [endpoint for endpoint in _endpoints() if endpoint.kind == transport.WIFI]
        self.assertIsNone(transport.choose_endpoint(wifi_only, "btpan"))
        self.assertIsNone(transport.choose_endpoint([], "btpan"))

    def test_lan_policy_excludes_pan(self) -> None:
        chosen = transport.choose_endpoint(_endpoints(), "lan")
        assert chosen is not None
        self.assertNotEqual(chosen.kind, transport.BTPAN)
        pan_only = [endpoint for endpoint in _endpoints() if endpoint.kind == transport.BTPAN]
        self.assertIsNone(transport.choose_endpoint(pan_only, "lan"))

    def test_same_subnet_wins_within_auto(self) -> None:
        endpoints = [
            transport.Endpoint("192.0.2.50", "wlan0", transport.WIFI),
            transport.Endpoint("198.51.100.9", "enp4s0", transport.WIRED),
        ]
        chosen = transport.choose_endpoint(endpoints, "auto", ["192.0.2.7"])
        assert chosen is not None
        self.assertEqual(chosen.address, "192.0.2.50")

    def test_unusable_addresses_are_ignored(self) -> None:
        endpoints = [
            transport.Endpoint("127.0.0.1"),
            transport.Endpoint("169.254.4.4"),
            transport.Endpoint("not-an-ip"),
        ]
        self.assertIsNone(transport.choose_endpoint(endpoints, "auto"))

    def test_validate_transport(self) -> None:
        self.assertEqual(transport.validate_transport("BTPAN"), "btpan")
        self.assertEqual(transport.validate_transport("auto"), "auto")
        for bad in (None, "", "carrier-pigeon"):
            with self.assertRaises(ValueError):
                transport.validate_transport(bad)


class PropsTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        text = transport.format_endpoint_props(_endpoints())
        self.assertIn("wired|198.51.100.7", text)
        parsed = transport.parse_endpoint_props(text)
        self.assertEqual(
            {(endpoint.kind, endpoint.address) for endpoint in parsed},
            {
                (transport.WIFI, "192.0.2.12"),
                (transport.WIRED, "198.51.100.7"),
                (transport.BTPAN, "10.44.0.2"),
            },
        )

    def test_tolerates_bare_addresses_and_junk(self) -> None:
        parsed = transport.parse_endpoint_props("192.0.2.5, wifi|192.0.2.6, ,bad, ")
        self.assertEqual(
            [(endpoint.kind, endpoint.address) for endpoint in parsed],
            [(transport.OTHER, "192.0.2.5"), (transport.WIFI, "192.0.2.6")],
        )

    def test_pan_status_is_report_only(self) -> None:
        status = transport.pan_status()
        self.assertIsInstance(status, dict)
        self.assertIn("available", status)
        self.assertIsInstance(status["interfaces"], list)


class CodecCapTests(unittest.TestCase):
    def test_btpan_cap_is_applied_and_reported(self) -> None:
        chosen, reason = codec.choose_codec(
            codec.LinkProbe(ok=True, rtt_ms=3.0, loss=0.0), limit="flac"
        )
        self.assertEqual(chosen, "flac")
        self.assertIn("capped", reason)

    def test_cap_never_upgrades_a_worse_link(self) -> None:
        chosen, _ = codec.choose_codec(
            codec.LinkProbe(ok=True, rtt_ms=90.0, loss=0.0), limit="flac"
        )
        self.assertEqual(chosen, "opus")


class ConfigTransportTests(unittest.TestCase):
    def _load(self, payload: dict) -> config.Config:
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, "config.json").write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.dict(os.environ, {"AUDIOSYNC_CONFIG_DIR": temp}):
                return config.Config.load(strict=True)

    def test_unknown_transport_is_rejected_in_strict_mode(self) -> None:
        with self.assertRaises(config.ConfigError):
            self._load({"transport": "carrier-pigeon"})

    def test_transport_and_peer_identity_round_trip(self) -> None:
        cfg = self._load({"transport": "btpan", "preferred_peer": "living-room"})
        self.assertEqual(cfg.transport, "btpan")
        self.assertEqual(cfg.preferred_peer, "living-room")

    def test_display_name_falls_back_to_the_hostname(self) -> None:
        cfg = config.Config()
        self.assertEqual(cfg.display_name(), config.device_name())
        cfg.device_name = "study"
        self.assertEqual(cfg.display_name(), "study")


class PeerIdentityTests(unittest.TestCase):
    def _discovery(self) -> discovery.Discovery:
        return discovery.Discovery("this-device")

    def test_one_device_on_two_interfaces_is_one_peer(self) -> None:
        instance = self._discovery()
        instance._peers["wifi"] = discovery.Peer(
            name="study",
            address="192.0.2.12",
            ident="abc123",
            endpoints=[transport.Endpoint("192.0.2.12", "wlan0", transport.WIFI)],
        )
        instance._peers["wired"] = discovery.Peer(
            name="study",
            address="198.51.100.7",
            ident="abc123",
            endpoints=[transport.Endpoint("198.51.100.7", "enp4s0", transport.WIRED)],
        )
        peers = instance.peers()
        self.assertEqual(len(peers), 1)
        chosen = peers[0].best_endpoint("auto")
        assert chosen is not None
        self.assertEqual(chosen.kind, transport.WIRED)

    def test_manual_duplicate_of_a_discovered_address_is_skipped(self) -> None:
        instance = self._discovery()
        instance._peers["mdns"] = discovery.Peer(
            name="study", address="192.0.2.12", ident="abc123"
        )
        instance.add_manual("192.0.2.12")
        self.assertEqual(len(instance.peers()), 1)

    def test_find_matches_a_secondary_address(self) -> None:
        instance = self._discovery()
        instance._peers["mdns"] = discovery.Peer(
            name="study",
            address="192.0.2.12",
            ident="abc123",
            endpoints=[
                transport.Endpoint("192.0.2.12", "wlan0", transport.WIFI),
                transport.Endpoint("198.51.100.7", "enp4s0", transport.WIRED),
            ],
        )
        self.assertIsNotNone(instance.find("198.51.100.7"))
        self.assertIsNotNone(instance.find("abc123"))

    def test_literal_address_resolves_without_network(self) -> None:
        self.assertEqual(discovery.resolve_host("192.0.2.9"), "192.0.2.9")
        self.assertEqual(discovery.resolve_host(""), "")


class PeerTargetTests(unittest.TestCase):
    def test_auto_targets_the_wired_address(self) -> None:
        peer = discovery.Peer(name="study", address="192.0.2.12", endpoints=_endpoints())
        targets = preload.peer_targets([peer], "auto")
        self.assertEqual([target.host for target in targets], ["198.51.100.7"])

    def test_btpan_targets_the_direct_link(self) -> None:
        peer = discovery.Peer(name="study", address="192.0.2.12", endpoints=_endpoints())
        targets = preload.peer_targets([peer], "btpan")
        self.assertEqual([target.host for target in targets], ["10.44.0.2"])

    def test_btpan_without_a_pan_keeps_the_known_address(self) -> None:
        peer = discovery.Peer(
            name="study",
            address="192.0.2.12",
            endpoints=[transport.Endpoint("192.0.2.12", "wlan0", transport.WIFI)],
        )
        targets = preload.peer_targets([peer], "btpan")
        self.assertEqual([target.host for target in targets], ["192.0.2.12"])


if __name__ == "__main__":
    unittest.main()
