"""Tests for bridge/transition_events.py: the EMG change score, transition flags and what followed them."""

import math
import os
import sys
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bridge"))
import transition_events as te  # noqa: E402

FS = 500


def emg(rng, seconds, gains=None, n_ch=8):
    """Gaussian 'EMG' at FS with a per-channel gain profile (a function of time or a constant)."""
    n = int(seconds * FS)
    x = rng.normal(size=(n_ch, n)) * 20.0
    if gains is not None:
        t = np.arange(n) / FS
        x *= np.array([gains(tt) for tt in t]).T
    return x


def feed(tr, x, t0, chunk=8, posture=lambda t: None, cam=lambda t: None, wq=lambda t: None):
    """Stream x into the tracker in GUI-sized chunks; t0 = time of the first sample."""
    n = x.shape[1]
    for i in range(0, n, chunk):
        t = t0 + (min(n, i + chunk) - 1) / FS
        tr.update(x[:, i:i + chunk], t, posture(t), cam(t), wq(t), None)


def burst(t, at, ch, k=8.0, dur=0.15):
    g = np.ones(8)
    if at <= t < at + dur:
        g[list(ch)] = k
    return g


class ChangeScoreTest(unittest.TestCase):
    def test_quiet_then_step(self):
        rng = np.random.default_rng(1)
        cs = te.ChangeScore(FS, 8)
        x = emg(rng, 4.0, lambda t: np.ones(8) * (4.0 if t >= 3.0 else 1.0))
        out = cs.push(x, 4.0 - 1 / FS)
        t = np.array([o[0] for o in out]); s = np.array([o[1] for o in out])
        self.assertLess(np.percentile(s[(t > 1) & (t < 3)], 99), te.THRESHOLD)
        first = t[(t >= 3.0) & (s > te.THRESHOLD)][0]
        self.assertLess(first - 3.0, 0.03)                          # within one envelope of the step

    def test_chunking_does_not_matter(self):
        rng = np.random.default_rng(2)
        x = emg(rng, 2.0)
        a = te.ChangeScore(FS, 8).push(x, 2.0 - 1 / FS)
        cs = te.ChangeScore(FS, 8); b = []
        for i in range(0, x.shape[1], 7):
            b += cs.push(x[:, i:i + 7], (min(x.shape[1], i + 7) - 1) / FS)
        self.assertEqual(len(a), len(b))
        np.testing.assert_allclose([o[1] for o in a], [o[1] for o in b], atol=1e-9)
        np.testing.assert_allclose([o[0] for o in a], [o[0] for o in b], atol=1e-9)


class TrackerTest(unittest.TestCase):
    def test_flag_before_the_change_and_a_miss(self):
        rng = np.random.default_rng(3)
        tr = te.TransitionTracker(FS, 8)
        x = emg(rng, 12.0, lambda t: burst(t, 5.0, (0, 1, 2, 3)))
        posture = lambda t: "fist" if t < 5.25 else ("open" if t < 10.0 else "pinch")   # 10 s: a change with no burst
        feed(tr, x, 0.0, posture=posture)
        ev = [e for e in tr.events if not e["vetoed"]]
        self.assertEqual(len(ev), 1)
        e = ev[0]
        self.assertAlmostEqual(e["t"], 5.0, delta=0.03)
        self.assertEqual((e["from"], e["gate_to"], e["outcome"]), ("fist", "open", "change"))
        self.assertAlmostEqual(e["gate_dt"], 0.25, delta=0.03)       # the flag came 250 ms before the EMG gate
        self.assertEqual([(m["from"], m["to"]) for m in tr.misses], [("open", "pinch")])
        st = tr.stats()
        self.assertEqual(st["gate_caught"], (1, 2))
        self.assertAlmostEqual(st["gate_lead_ms"], 250, delta=30)

    def test_turning_wrist_vetoes(self):
        rng = np.random.default_rng(4)
        tr = te.TransitionTracker(FS, 8)
        x = emg(rng, 8.0, lambda t: burst(t, 5.0, (0, 1, 2, 3)))
        spin = lambda t: (math.cos(t * 1.5 / 2), 0.0, 0.0, math.sin(t * 1.5 / 2)) if 4.5 < t < 5.5 else (1.0, 0, 0, 0)
        feed(tr, x, 0.0, posture=lambda t: "fist" if t < 5.25 else "open", wq=spin)   # ~86 °/s
        self.assertTrue(tr.events and all(e["vetoed"] for e in tr.events))
        self.assertEqual(len(tr.misses), 1)

    def test_camera_change_counts_once_stable(self):
        rng = np.random.default_rng(5)
        tr = te.TransitionTracker(FS, 8)
        x = emg(rng, 8.0, lambda t: burst(t, 5.0, (4, 5, 6, 7)))
        cam = lambda t: "open" if t < 5.3 else ("pinch" if 5.3 <= t < 5.35 else "point")   # a 50 ms flicker first
        feed(tr, x, 0.0, posture=lambda t: "open", cam=cam)
        e = [e for e in tr.events if not e["vetoed"]][0]
        self.assertEqual(e["cam_to"], "point")
        self.assertAlmostEqual(e["cam_dt"], 0.35, delta=0.03)


FIVE = ("fist", "open", "pinch", "point", "victory")          # the 5 × 5 priors below (PRIOR_CLASSES has rest too)


class DirectionTest(unittest.TestCase):
    def test_burst_pattern_beats_the_prior(self):
        """fist → open bursts on channels 0-3, fist → point on 4-7: after a few of each the pattern decides, while the
        prior alone always says open."""
        rng = np.random.default_rng(6)
        prior = ((0.0, 0.9, 0.04, 0.03, 0.03),) + ((0.25,) * 5,) * 4
        tr = te.TransitionTracker(FS, 8, prior=prior, classes=FIVE)
        t0 = 0.0; seq = []
        for i in range(16):
            to = "open" if i % 2 else "point"
            ch = (0, 1, 2, 3) if to == "open" else (4, 5, 6, 7)
            x = emg(rng, 3.0, lambda t: burst(t, 1.5, ch))
            feed(tr, x, t0, posture=lambda t, t0=t0, to=to: "fist" if t < t0 + 1.75 else to)
            x = emg(rng, 2.0)                                    # back to a fist, no burst (a miss, fine)
            feed(tr, x, t0 + 3.0, posture=lambda t: "fist")
            seq.append(to); t0 += 5.0
        ch = [e for e in tr.events if e["outcome"] == "change" and e["from"] == "fist"]
        late = ch[8:]
        self.assertTrue(len(late) >= 6)
        self.assertTrue(all(e["pred"] == e["to"] == e["gate_to"] for e in late))
        st = tr.stats()
        self.assertGreater(st["next_right"][1], st["next_right"][0])


class PriorTest(unittest.TestCase):
    def test_prior_learns_your_transitions(self):
        dm = te.DirectionModel(((0.0, 0.9, 0.04, 0.03, 0.03),) + ((0.25,) * 5,) * 4, classes=FIVE)
        self.assertEqual(dm.classes[int(np.argmax(dm.prior_row("fist")))], "open")
        for _ in range(20):
            dm.learn("fist", None, "point")                      # this user goes fist → point
        p = dm.prior_row("fist")
        self.assertEqual(dm.classes[int(np.argmax(p))], "point")
        self.assertAlmostEqual(float(p.sum()), 1.0, places=9)
        self.assertEqual(float(p[dm.classes.index("fist")]), 0.0)


def synthetic_recording(rng, path, n_changes=14):
    """A Transitions recording as the GUI saves it: ticks of 8 samples, the camera's label changing every 2 s, a burst
    100 ms before each change, no camera hand (onset = label − 0.1 s)."""
    names = ("open", "pinch", "open", "fist")
    secs = 2.0 * (n_changes + 1); n = int(secs * FS)
    changes = [2.0 * (i + 1) for i in range(n_changes)]
    def gains(t):
        g = np.ones(8)
        for c in changes:
            if c - 0.1 <= t < c + 0.05:
                g[[0, 2, 4, 6] if int(c) % 4 == 0 else [1, 3, 5, 7]] = 8.0
        return g
    x = emg(rng, secs, gains)
    ticks = np.arange(8, n + 1, 8)
    t = (ticks - 1) / FS
    cam = np.array([names[int(tt // 2.0) % 4] for tt in t])
    np.savez(path, emg=x.astype(np.float32), emg_n=np.full(len(ticks), 8, np.int32), fs=float(FS), t=t,
             posture=cam, cam=cam, cam_local=np.full((len(t), 26, 3), np.nan, np.float32),
             wq=np.tile([1.0, 0, 0, 0], (len(t), 1)), kq=np.tile([1.0, 0, 0, 0], (len(t), 1)), threshold=0.9, version=1)
    return changes


class LearnedDetectorTest(unittest.TestCase):
    def test_train_and_use(self):
        import tempfile
        rng = np.random.default_rng(7)
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for i in range(3):
                paths.append(os.path.join(d, f"tx_{i}.npz")); synthetic_recording(rng, paths[-1])
            det = te.train_detector(paths)
            u = det.info["unseen"]["learned"]
            self.assertGreaterEqual(u["caught"] / u["of"], 0.8)          # on recordings the model never saw
            self.assertLess(u["false_per_min"], 3.0)
            self.assertTrue(0.0 < det.threshold < 1.0)
            C = np.array(det.info["counts"]); cl = det.info["count_classes"]
            self.assertEqual(C[cl.index("open"), cl.index("pinch")] + C[cl.index("open"), cl.index("fist")], C.sum() / 2)   # open ↔ pinch, open ↔ fist
            path = os.path.join(d, "model.npz"); det.save(path)
            det2 = te.TransitionTracker(FS, 8)
            det2.set_detector(te.LearnedDetector.load(path))
            self.assertEqual(det2.active()[0], "learned")
            self.assertEqual(det2.dirm.classes[int(np.argmax(det2.dirm.prior_row("pinch")))], "open")    # its counts were loaded
            self.assertGreater(det2.dirm.base_counts.sum(), 0)
            changes = synthetic_recording(rng, os.path.join(d, "live.npz"))
            r = dict(np.load(os.path.join(d, "live.npz")))
            i0 = 0
            for k, n in enumerate(r["emg_n"]):
                det2.update(r["emg"][:, i0:i0 + n], float(r["t"][k]), str(r["posture"][k]), str(r["cam"][k])); i0 += n
            hit = [c for c in changes if any(-0.3 <= e["t"] - c <= 0.1 for e in det2.events)]
            self.assertGreaterEqual(len(hit), 0.8 * len(changes))

    def test_too_few_transitions(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "tx.npz"); synthetic_recording(np.random.default_rng(8), p, n_changes=4)
            with self.assertRaises(ValueError):
                te.train_detector([p])


if __name__ == "__main__":
    unittest.main()
