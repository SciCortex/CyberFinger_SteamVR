"""Tests for bridge/finger_fusion.py: the camera ↔ EMG weight w = f(m, d)."""

import os
import sys
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bridge"))
import finger_fusion as ff  # noqa: E402


class WeightTest(unittest.TestCase):
    def test_ends_and_centre(self):
        for d in (0.0, 0.2, 0.5, 1.0):
            self.assertAlmostEqual(float(ff.weight(0.0, d)), 0.0, places=9)
            self.assertAlmostEqual(float(ff.weight(1.0, d)), 1.0, places=9)
        self.assertAlmostEqual(float(ff.weight(ff.M0, ff.D_REF)), 0.5, places=3)   # disagreeing: the flip is at M0

    def test_agreeing_sources_blend_smoothly(self):
        m = np.linspace(0, 1, 11)
        np.testing.assert_allclose(ff.weight(m, 0.0, m0=0.5), m, atol=0.03)   # nearly w = m
        self.assertTrue(np.all(np.abs(np.diff(ff.weight(m, 0.0), 2)) < 0.01))     # and gently curved at any centre

    def test_disagreeing_sources_pick_one(self):
        self.assertLess(float(ff.weight(ff.M0 - 0.15, ff.D_REF)), 0.02)   # nearly a step at M0 (within ±0.15)
        self.assertGreater(float(ff.weight(ff.M0 + 0.15, ff.D_REF)), 0.98)

    def test_steeper_as_they_disagree(self):
        """The more they disagree, the closer the curve gets to a step at M0."""
        m = np.concatenate([np.linspace(0, ff.M0 - 0.05, 20), np.linspace(ff.M0 + 0.05, 1, 20)])
        step = (m > ff.M0).astype(float)
        gap = [float(np.abs(ff.weight(m, d) - step).mean()) for d in (0.0, 0.1, 0.25, 0.5)]
        self.assertTrue(all(a > b for a, b in zip(gap, gap[1:])))

    def test_monotonic_in_m(self):
        for d in (0.0, 0.3, 0.6):
            w = ff.weight(np.linspace(0, 1, 101), d)
            self.assertTrue(np.all(np.diff(w) >= -1e-12))

    def test_smoothing(self):
        f = ff.FingerFusion()
        a = f.step(np.full(5, 0.9), np.full(5, 0.6), 1 / 60.)               # camera clearly wins
        b = f.step(np.full(5, 0.1), np.full(5, 0.6), 1 / 60.)               # one frame later it flips
        self.assertTrue(np.all(b > 0.5) and np.all(b < a))                 # glides, doesn't jump in one frame


if __name__ == "__main__":
    unittest.main()


import steamvr_link as sl  # noqa: E402


def valve_hand(right, curls):
    """A natural hand (Valve's simulated skeleton) in the Studio's hand frame, wrist at the origin."""
    J = sl.joints(sl.model_space(sl.synthesize(right, curls)))
    R = sl.hand_frame(J, right)
    return (J - J[1]) @ R


class AnatomizeTest(unittest.TestCase):
    def test_natural_hands_pass_unchanged(self):
        for right in (True, False):
            for c in ((0, 0, 0, 0, 0), (1, 1, 1, 1, 1), (0.85, 0, 0.95, 0.95, 0.95), (0.6, 0.62, 0.12, 0.08, 0.05)):
                L = valve_hand(right, c)
                out = ff.anatomize(L, ff.bone_lengths(L))
                self.assertLess(float(np.abs(out - L).max()), 1e-6, f"right={right} curls={c}")
                self.assertEqual(ff.anomalies(L, ff.bone_lengths(L)), (0, 0))

    def test_stretched_bones_are_restored(self):
        L = valve_hand(True, (0.2, 0.3, 0.3, 0.3, 0.3))
        ref = ff.bone_lengths(L)
        bad = L.copy()
        bad[8:11] += (bad[8] - bad[7]) * 0.4                 # index: the proximal phalanx 40 % too long
        self.assertGreater(ff.anomalies(bad, ref)[0], 0)
        np.testing.assert_allclose(ff.bone_lengths(ff.anatomize(bad, ref)), ref, rtol=1e-9)

    def test_a_joint_bent_backwards_is_limited(self):
        L = valve_hand(True, (0, 0, 0, 0, 0))
        ref = ff.bone_lengths(L)
        bad = L.copy()
        a, b = bad[8], bad[9] - bad[8]                       # index PIP: bend the rest of the finger 50° the wrong way
        R = sl.q_to_R((np.cos(np.radians(25)), 0.0, np.sin(np.radians(25)), 0.0))
        for j in (9, 10):
            bad[j] = a + (bad[j] - a) @ R.T
        out = ff.anatomize(bad, ref)
        self.assertEqual(ff.anomalies(out, ref), (0, 0))
