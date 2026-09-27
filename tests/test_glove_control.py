# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for bridge/glove_control.py: the haptic command (firmware src/vr_gatt.h VrGattHapticCommand) and the
per-hand coalescing writer.
python -m unittest discover -s tests"""

import asyncio
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bridge"))
import glove_control as gc  # noqa: E402


class PackTest(unittest.TestCase):
    def test_layout(self):
        # cmd, amplitude, duration_ms LE, frequency_hz LE: 6 bytes, as the firmware's packed struct
        self.assertEqual(gc.pack_haptic(0.25, 160.0, 1.0), bytes([0x10, 255, 250, 0, 160, 0]))
        self.assertEqual(len(gc.pack_haptic(0, 0, 0.5)), 6)
        self.assertEqual(gc.pack_haptic_stop(), bytes([0x10, 0, 0, 0, 0, 0]))

    def test_ranges(self):
        self.assertEqual(gc.pack_haptic(0.001, 0, 0.001)[1], 1)       # faint but not 0 (0 stops the motor)
        self.assertEqual(gc.pack_haptic(0.1, 0, 0.0)[1], 0)
        self.assertEqual(gc.pack_haptic(0.1, 0, 3.0)[1], 255)
        self.assertEqual(gc.HAPTIC.unpack(gc.pack_haptic(100.0, 1e6, 0.5))[2:], (65535, 65535))
        self.assertEqual(gc.HAPTIC.unpack(gc.pack_haptic(-1.0, -5, 0.5))[2:], (0, 0))


class SenderTest(unittest.TestCase):
    def test_coalesces_while_a_write_is_in_flight(self):
        written = []
        release = None

        async def fake_write(char, payload):
            written.append((char, gc.HAPTIC.unpack(payload)))
            await release.wait()
            return True

        async def run():
            nonlocal release
            release = asyncio.Event()
            s = gc.HapticSender()
            s.attach(1, "right-char")
            self.assertTrue(s.available(1))
            self.assertFalse(s.available(0))
            self.assertFalse(s.request(0, 0.1, 0, 1.0))                  # the left glove isn't attached
            s.request(1, 0.010, 0, 0.5)                                  # goes out at once
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            s.request(1, 0.300, 0, 0.2)                                  # these two, while the first is in flight,
            s.request(1, 0.050, 90, 0.9)                                 # merge: latest amplitude, longest duration
            await asyncio.sleep(0.01)
            release.set()
            for _ in range(10):
                await asyncio.sleep(0)
            return written

        orig = gc.write_without_response
        gc.write_without_response = fake_write
        try:
            w = asyncio.run(run())
        finally:
            gc.write_without_response = orig
        self.assertEqual([c for c, _ in w], ["right-char", "right-char"])
        self.assertEqual(w[0][1], (0x10, 128, 10, 0))
        self.assertEqual(w[1][1], (0x10, 230, 300, 90))


if __name__ == "__main__":
    unittest.main()
