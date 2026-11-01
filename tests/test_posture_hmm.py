"""Tests for the posture transition prior (bridge/key_postures.py PostureHMM), no Tk."""

import os
import sys
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bridge"))
import key_postures as kp  # noqa: E402

CLASSES = ["fist", "open", "other", "pinch", "point", "victory"]     # the live model's classes


def evidence(cls, p=0.9):
    v = np.full(len(CLASSES), (1 - p) / (len(CLASSES) - 1))
    v[CLASSES.index(cls)] = p
    return v


def frames_to_switch(h, frm, to, max_frames=120):
    for _ in range(60):
        h.update(evidence(frm), 1 / 60.)
    for n in range(1, max_frames + 1):
        if CLASSES[int(np.argmax(h.update(evidence(to), 1 / 60.)))] == to:
            return n
    return None


class PostureHMMTest(unittest.TestCase):
    def test_rows(self):
        h = kp.PostureHMM(CLASSES)
        np.testing.assert_allclose(h.T.sum(1), 1.0)
        self.assertTrue(np.all(np.diag(h.T) == 0))
        self.assertAlmostEqual(h.T[CLASSES.index("fist"), CLASSES.index("other")], kp.PostureHMM.OTHER_SHARE)
        self.assertGreater(h.T[CLASSES.index("pinch"), CLASSES.index("open")], 0.8)

    def test_likely_switches_beat_unlikely_ones(self):
        """From a fist, opening is likely and pinching is not: the same evidence switches sooner to open."""
        n_open = frames_to_switch(kp.PostureHMM(CLASSES), "fist", "open")
        n_pinch = frames_to_switch(kp.PostureHMM(CLASSES), "fist", "pinch")
        self.assertIsNotNone(n_pinch)                        # unlikely, never forbidden
        self.assertLess(n_open, n_pinch)
        self.assertLessEqual(n_pinch, 12)                   # clear evidence still wins within 0.2 s

    def test_a_blip_does_not_flip(self):
        h = kp.PostureHMM(CLASSES)
        for _ in range(60):
            h.update(evidence("fist"), 1 / 60.)
        for _ in range(2):                                   # two frames of a wrong pinch reading
            b = h.update(evidence("pinch", 0.95), 1 / 60.)
        self.assertEqual(CLASSES[int(np.argmax(b))], "fist")

    def test_frame_rate_independent(self):
        """A second of evidence weighs the same at 30 and 60 Hz."""
        a = kp.PostureHMM(CLASSES)
        b = kp.PostureHMM(CLASSES)
        for _ in range(30):
            a.update(evidence("open", 0.6), 1 / 30.)
        for _ in range(60):
            b.update(evidence("open", 0.6), 1 / 60.)
        self.assertLess(abs(a.b[CLASSES.index("open")] - b.b[CLASSES.index("open")]), 0.05)


if __name__ == "__main__":
    unittest.main()


GATE_CL = ["fist", "open", "other", "pinch", "point", "victory"]


def ev(cls, p=0.98):
    v = np.full(len(GATE_CL), (1 - p) / (len(GATE_CL) - 1))
    v[GATE_CL.index(cls)] = p
    return v


def settle(g, cls, rms=40.0, n=40, prior=kp.TRANSITION_PRIOR):
    for _ in range(n):
        g.update(ev(cls), 1 / 60., prior=prior, rms=rms)


def frames_until(g, cls, evidence, rms, prior=kp.TRANSITION_PRIOR, n=60):
    for k in range(1, n + 1):
        if g.update(evidence, 1 / 60., prior=prior, rms=rms)[0] == cls:
            return k
    return None


class ExpectedNextTest(unittest.TestCase):
    def test_prior_says_a_fist_opens(self):
        row = kp.TRANSITION_PRIOR[kp.PRIOR_CLASSES.index("fist")]
        self.assertEqual(kp.PRIOR_CLASSES[int(np.argmax(row))], "open")

    def test_in_between_flash_is_held_back(self):
        """Leaving a fist, a brief 'point' reading (on the way to open) is not shown with the prior; it is without."""
        for prior, shown in ((kp.TRANSITION_PRIOR, False), (None, True)):
            g = kp.PostureGate(GATE_CL)
            settle(g, "fist", prior=prior)
            got = [g.update(ev("point"), 1 / 60., prior=prior, rms=40.0)[0] for _ in range(9)]   # 150 ms of point
            self.assertEqual("point" in got, shown)

    def test_relaxing_fist_opens_at_once(self):
        """A fist whose EMG power drops, with moderate open evidence, opens sooner with the prior."""
        e = ev("open", 0.85)
        g1 = kp.PostureGate(GATE_CL); settle(g1, "fist")
        g0 = kp.PostureGate(GATE_CL); settle(g0, "fist", prior=None)
        n1 = frames_until(g1, "open", e, rms=15.0)
        n0 = frames_until(g0, "open", e, rms=15.0, prior=None)
        self.assertIsNotNone(n1)
        self.assertLess(n1, n0)

    def test_pointing_from_open_is_not_slowed(self):
        g1 = kp.PostureGate(GATE_CL); settle(g1, "open")
        g0 = kp.PostureGate(GATE_CL); settle(g0, "open", prior=None)
        self.assertEqual(frames_until(g1, "point", ev("point"), rms=40.0), frames_until(g0, "point", ev("point"), rms=40.0, prior=None))


class TransitionFlagTest(unittest.TestCase):
    """The posture gate with transition flags (flag_age): quick right after a flag, steadier without one."""

    @staticmethod
    def frames(flag_age, p=0.85, frm="open", to="point"):
        g = kp.PostureGate(GATE_CL); settle(g, frm)
        for k in range(1, 61):
            if g.update(ev(to, p), 1 / 60., prior=kp.TRANSITION_PRIOR, rms=40.0, flag_age=flag_age)[0] == to:
                return k
        return None

    def test_no_flags_changes_nothing(self):
        rng = np.random.default_rng(0)
        g0 = kp.PostureGate(GATE_CL); g1 = kp.PostureGate(GATE_CL)
        for _ in range(600):
            p = rng.dirichlet(np.full(len(GATE_CL), 0.3))
            self.assertEqual(g0.update(p, 1 / 60., prior=kp.TRANSITION_PRIOR, rms=30.0),
                             g1.update(p, 1 / 60., prior=kp.TRANSITION_PRIOR, rms=30.0, flag_age=None))

    def test_fast_right_after_a_flag(self):
        n0, n1 = self.frames(None), self.frames(0.1)                      # a clear lead of 0.85
        self.assertLess(n1, n0)
        self.assertLessEqual(n1 / 60., kp.FLAG_HOLD + 1 / 60. + 1e-9)

    def test_steadier_without_a_flag(self):
        n0, n1 = self.frames(None, p=0.98), self.frames(float("inf"), p=0.98)   # confident: the fast path, unless no flag
        self.assertGreater(n1, n0)
        self.assertGreaterEqual(n1 / 60., kp.NOFLAG_HOLD - 1e-9)
        self.assertEqual(self.frames(float("inf"), p=0.98), self.frames(kp.FLAG_WIN + 0.01, p=0.98))


class FlagHMMTest(unittest.TestCase):
    """The soft hand's belief: sticky, and released quickly by a transition flag."""

    @staticmethod
    def frames(flag_age, frm="open", to="fist", p=0.9):
        h = kp.FlagHMM(GATE_CL)
        for _ in range(120):
            h.update(ev(frm, p), 1 / 60., flag_age=float("inf"))
        for k in range(1, 241):
            if h.update(ev(to, p), 1 / 60., flag_age=flag_age)[0] == to:
                return k
        return None

    def test_belief_is_a_distribution(self):
        h = kp.FlagHMM(GATE_CL); shown, b = h.update(ev("pinch"), 1 / 60.)
        self.assertAlmostEqual(float(b.sum()), 1.0, places=9)
        self.assertTrue(np.all(b >= 0))

    def test_a_flicker_does_not_switch(self):
        h = kp.FlagHMM(GATE_CL)
        for _ in range(120):
            h.update(ev("open"), 1 / 60.)
        got = [h.update(ev("point"), 1 / 60.)[0] for _ in range(3)]          # 50 ms of another posture
        self.assertEqual(set(got), {"open"})

    def test_a_flag_releases_it(self):
        n_flag, n_none = self.frames(0.05), self.frames(float("inf"))
        self.assertIsNotNone(n_flag); self.assertIsNotNone(n_none)
        self.assertLess(n_flag, n_none)

    def test_other_is_never_named(self):
        h = kp.FlagHMM(GATE_CL)
        got = [h.update(ev("other"), 1 / 60.)[0] for _ in range(60)]
        self.assertEqual(set(got), {None})

    def test_turning_the_forearm_holds_the_posture(self):
        """While the wrist IMU turns fast the recogniser's evidence is damped: a quarter second of another posture does
        not switch it, and a lasting one takes several times longer than with the arm still."""
        def frames(turn, n_max=600):
            h = kp.FlagHMM(GATE_CL)
            for _ in range(120):
                h.update(ev("open", 0.9), 1 / 60., turn_dps=0.0)
            for k in range(1, n_max + 1):
                if h.update(ev("pinch", 0.9), 1 / 60., turn_dps=turn)[0] == "pinch":
                    return k
            return None
        h = kp.FlagHMM(GATE_CL)
        for _ in range(120):
            h.update(ev("open", 0.9), 1 / 60., turn_dps=0.0)
        burst = [h.update(ev("pinch", 0.9), 1 / 60., turn_dps=kp.TURN_DPS + 40)[0] for _ in range(15)]
        self.assertEqual(set(burst), {"open"})
        still, turning = frames(0.0), frames(kp.TURN_DPS + 40)
        self.assertIsNotNone(still); self.assertIsNotNone(turning)
        self.assertGreater(turning, 3 * still)

    def test_a_flag_anticipates_the_usual_next_posture(self):
        """Right after a flag out of a fist, uncertain evidence leans toward open (the usual next posture) at once."""
        def lean(anticip):
            h = kp.FlagHMM(GATE_CL, anticip=anticip)
            for _ in range(120):
                h.update(ev("fist", 0.9), 1 / 60., flag_age=float("inf"))
            flat = np.full(len(GATE_CL), 1.0 / len(GATE_CL))
            for k in range(6):                                                   # 100 ms after the flag, no clear evidence
                _, b = h.update(flat, 1 / 60., flag_age=k / 60.)
            return b[GATE_CL.index("open")]
        self.assertGreater(lean(kp.SOFT_ANTICIP), 2 * lean(0.0))

    def test_anticipation_follows_your_own_transitions(self):
        """With a prior where a fist usually becomes a point (yours), a flag out of a fist leans toward point, not open."""
        own = np.array(kp.TRANSITION_PRIOR, float); i = kp.PRIOR_CLASSES.index("fist")
        own[i] = 0.0; own[i, kp.PRIOR_CLASSES.index("point")] = 0.8; own[i, kp.PRIOR_CLASSES.index("open")] = 0.2
        h = kp.FlagHMM(GATE_CL); h.prior = own
        for _ in range(120):
            h.update(ev("fist", 0.9), 1 / 60., flag_age=float("inf"))
        flat = np.full(len(GATE_CL), 1.0 / len(GATE_CL))
        for k in range(6):
            _, b = h.update(flat, 1 / 60., flag_age=k / 60.)
        self.assertGreater(b[GATE_CL.index("point")], b[GATE_CL.index("open")])

    def test_rest_is_recordable_and_not_suppressed(self):
        """REST can be recorded (posture, colour, fallback shape) and a flag's anticipation does not push it away."""
        self.assertIn("rest", kp.POSTURES); self.assertIn("rest", kp.COLORS); self.assertIn("rest", kp.TEMPLATES)
        cl = ["fist", "open", "other", "pinch", "point", "rest", "victory"]
        h = kp.FlagHMM(cl)
        v = np.full(len(cl), 0.02); v[cl.index("fist")] = 0.88
        for _ in range(60):
            h.update(v, 1 / 60., flag_age=float("inf"))
        r = np.full(len(cl), 0.02); r[cl.index("rest")] = 0.88
        got = [h.update(r, 1 / 60., flag_age=k / 60.)[0] for k in range(30)]
        self.assertIn("rest", got)

    def test_relaxing_fist_rests(self):
        """With rest in the recogniser, a fist whose EMG goes quiet and reads rest (point flickering in between) shows
        rest — the prior's relaxation rule used to pool rest into open."""
        cl = ["fist", "open", "other", "pinch", "point", "rest", "victory"]
        g = kp.PostureGate(cl)
        v = np.full(len(cl), 0.01); v[cl.index("fist")] = 0.94
        for _ in range(40):
            g.update(v, 1 / 60., prior=kp.TRANSITION_PRIOR, rms=40.0)
        r = np.full(len(cl), 0.02); r[cl.index("rest")] = 0.6; r[cl.index("point")] = 0.2; r[cl.index("open")] = 0.08
        shown = [g.update(r, 1 / 60., prior=kp.TRANSITION_PRIOR, rms=8.0)[0] for _ in range(40)]
        self.assertEqual(shown[-1], "rest")
        self.assertNotIn("open", shown)

    def test_prior_postures_match_the_tracker(self):
        import transition_events as te
        self.assertEqual(tuple(te.PRIOR_CLASSES), tuple(kp.PRIOR_CLASSES))
        T = np.asarray(kp.TRANSITION_PRIOR, float)
        self.assertEqual(T.shape, (len(kp.PRIOR_CLASSES),) * 2)
        np.testing.assert_allclose(T.sum(1), 1.0, atol=2e-3)
