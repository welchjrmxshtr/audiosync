"""Pure tests for output selection; no PipeWire required."""

from __future__ import annotations

import unittest

from audiosync import outputs

DESKTOP_SINKS = """
Sink #63
\tState: SUSPENDED
\tName: alsa_output.pci-0000_01_00.1.hdmi-stereo
\tDescription: HDA NVidia Digital Stereo (HDMI)
\tProperties:
\t\talsa.card = "0"
\t\talsa.device = "3"
\t\talsa.card_name = "HDA NVidia"

Sink #64
\tState: RUNNING
\tName: alsa_output.pci-0000_0b_00.6.analog-stereo
\tDescription: Ryzen HD Audio Controller Analog Stereo
\tProperties:
\t\talsa.card = "1"
\t\talsa.device = "0"
\t\talsa.card_name = "HD-Audio Generic"
"""

DESKTOP_CARDS = """
 0 [NVidia         ]: HDA-Intel - HDA NVidia
                      HDA NVidia at 0xf4080000 irq 72
 1 [Generic        ]: HDA-Intel - HD-Audio Generic
                      HD-Audio Generic at 0xf4500000 irq 73
"""

LAPTOP_SINKS = """
Sink #72
\tState: SUSPENDED
\tName: alsa_output.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__HDMI3__sink
\tDescription: HDMI 3
\tProperties:
\t\talsa.card = "0"
\t\talsa.device = "5"
\t\talsa.card_name = "sof-hda-dsp"

Sink #74
\tState: IDLE
\tName: alsa_output.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__HDMI1__sink
\tDescription: HDMI 1
\tProperties:
\t\talsa.card = "0"
\t\talsa.device = "3"
\t\talsa.card_name = "sof-hda-dsp"

Sink #75
\tState: RUNNING
\tName: alsa_output.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__Speaker__sink
\tDescription: Speaker
\tProperties:
\t\talsa.card = "0"
\t\talsa.device = "0"
\t\talsa.card_name = "sof-hda-dsp"

Sink #33
\tState: IDLE
\tName: audiosync_sink
\tDescription: Audiosync
"""

LAPTOP_CARDS = " 0 [sofhdadsp      ]: sof-hda-dsp - sof-hda-dsp\n"

HDMI_ONLY = """
Sink #63
\tName: alsa_output.pci-0000_01_00.1.hdmi-stereo
\tDescription: HDA NVidia Digital Stereo (HDMI)
\tProperties:
\t\talsa.card = "0"
\t\talsa.device = "3"
\t\talsa.card_name = "HDA NVidia"

Sink #64
\tName: auto_null
\tDescription: Dummy Output
"""


class OutputParsingTests(unittest.TestCase):
    def test_parse_properties(self) -> None:
        sinks = outputs.parse_pactl_sinks(DESKTOP_SINKS)
        self.assertEqual(len(sinks), 2)
        analog = [s for s in sinks if "analog-stereo" in s.name][0]
        self.assertEqual(analog.card_index, 1)
        self.assertEqual(analog.device, 0)
        self.assertEqual(analog.card_name, "HD-Audio Generic")

    def test_card_index_maps_to_alias_id(self) -> None:
        cards = outputs.parse_asound_cards(DESKTOP_CARDS)
        self.assertEqual(cards[0], "NVidia")
        self.assertEqual(cards[1], "Generic")


class OutputChoiceTests(unittest.TestCase):
    def test_desktop_prefers_ryzen_analog(self) -> None:
        choice = outputs.choose_output(
            outputs.parse_pactl_sinks(DESKTOP_SINKS),
            outputs.parse_asound_cards(DESKTOP_CARDS),
        )
        self.assertIn("analog-stereo", choice.sink)
        self.assertEqual(choice.card, "Generic")
        self.assertEqual(outputs.output_device(choice), "hw:Generic,0")
        self.assertEqual(choice.warning, "")

    def test_laptop_skips_hdmi_for_builtin_speaker(self) -> None:
        choice = outputs.choose_output(
            outputs.parse_pactl_sinks(LAPTOP_SINKS),
            outputs.parse_asound_cards(LAPTOP_CARDS),
        )
        self.assertIn("Speaker__sink", choice.sink)
        self.assertEqual(choice.card, "sofhdadsp")
        self.assertEqual(outputs.output_device(choice), "hw:sofhdadsp,0")

    def test_hdmi_only_is_chosen_with_warning(self) -> None:
        choice = outputs.choose_output(outputs.parse_pactl_sinks(HDMI_ONLY))
        self.assertIn("hdmi-stereo", choice.sink)
        self.assertIn("HDMI", choice.reason)
        self.assertIn("no speakers", choice.warning)

    def test_virtual_and_dummy_sinks_are_ignored(self) -> None:
        only_virtual = """
Sink #1
\tName: audiosync_sink
\tDescription: Audiosync
Sink #2
\tName: auto_null
\tDescription: Dummy Output
Sink #3
\tName: alsa_output.custom-virtual
\tDescription: Custom loopback
Sink #4
\tName: alsa_output.desk-output
\tDescription: Innocent-looking custom sink
\tProperties:
\t\tnode.virtual = "true"
"""
        sinks = outputs.parse_pactl_sinks(only_virtual)
        self.assertTrue(next(sink for sink in sinks if sink.name == "alsa_output.desk-output").virtual)
        choice = outputs.choose_output(sinks)
        self.assertEqual(choice.sink, "")
        self.assertIn("no hardware output", choice.reason)


if __name__ == "__main__":
    unittest.main()
