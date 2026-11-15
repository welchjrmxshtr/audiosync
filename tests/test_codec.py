"""The codec ladder, driven by synthetic probes.

These do not throttle a real link (that needs root and a second machine);
they pin the decision rules the probe feeds.
"""

from __future__ import annotations

import unittest

from audiosync.codec import CODEC_BITRATE, LinkProbe, buffer_floor_for, choose_codec


class CodecLadderTests(unittest.TestCase):
    def test_clean_link_stays_pcm(self) -> None:
        codec, reason = choose_codec(LinkProbe(ok=True, rtt_ms=3.0, loss=0.0))
        self.assertEqual(codec, "pcm")
        self.assertIn("clean", reason)

    def test_any_loss_drops_to_flac(self) -> None:
        codec, reason = choose_codec(LinkProbe(ok=True, rtt_ms=25.0, loss=0.0))
        self.assertEqual(codec, "flac")
        self.assertIn("jittery", reason)

    def test_single_lost_sample_drops_to_flac(self) -> None:
        codec, _ = choose_codec(LinkProbe(ok=True, rtt_ms=2.0, loss=0.1))
        self.assertEqual(codec, "flac")

    def test_congested_link_drops_to_opus(self) -> None:
        codec, reason = choose_codec(LinkProbe(ok=True, rtt_ms=90.0, loss=0.0))
        self.assertEqual(codec, "opus")
        self.assertIn("congested", reason)

    def test_heavy_loss_drops_to_opus(self) -> None:
        codec, _ = choose_codec(LinkProbe(ok=True, rtt_ms=5.0, loss=0.67))
        self.assertEqual(codec, "opus")

    def test_unreachable_keeps_preference(self) -> None:
        codec, reason = choose_codec(LinkProbe(ok=False, loss=1.0))
        self.assertEqual(codec, "pcm")
        self.assertEqual(reason, "no probe")

    def test_no_probe_keeps_preference(self) -> None:
        self.assertEqual(choose_codec(None)[0], "pcm")

    def test_bitrates_are_ordered(self) -> None:
        self.assertGreater(CODEC_BITRATE["pcm"], CODEC_BITRATE["flac"])
        self.assertGreater(CODEC_BITRATE["flac"], CODEC_BITRATE["opus"])

    def test_buffer_floor_rises_with_compression(self) -> None:
        self.assertLessEqual(buffer_floor_for("pcm"), buffer_floor_for("flac"))
        self.assertLess(buffer_floor_for("flac"), buffer_floor_for("opus"))


if __name__ == "__main__":
    unittest.main()
