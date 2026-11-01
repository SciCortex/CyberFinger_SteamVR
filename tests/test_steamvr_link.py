"""Tests for bridge/steamvr_link.py (no Tk, no SteamVR)."""

import math
import os
import socket
import sys
import time
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bridge"))
import cf_protocol as cfp  # noqa: E402
import steamvr_link as sl  # noqa: E402

# Bones (px, py, pz, qw, qx, qy, qz) printed by src/SkeletonSynth.cpp SynthesizeSkeleton, compiled.
GOLDEN = {
    (True, (0.85, 0, 0.95, 0.95, 0.95), (0,) * 5): {
        2: (0.0113279754, 0.0317978263, 0.0368859768, 0.234733045, -0.575865149, 0.697393417, 0.356261343),
        3: (-0.0500000007, 0.0, 0.0, 0.785316944, 0.0, 0.0, 0.619093955),
        7: (-0.0729999989, 0.0, 0.0, 0.999657333, 0.0, 0.0261769481, 0.0),
        12: (-0.0909999982, 0.0, 0.0, 0.734322488, 0.0, 0.0, 0.678800762),
        26: (-0.037758626, 0.0258071311, 0.0776016936, -0.488199413, 0.55366832, -0.489970356, -0.463725358),
    },
    (False, (1, 1, 1, 1, 1), (0.5, -0.3, 0.2, 1, -1)): {
        6: (-0.00259228447, 0.00673994003, 0.0291179195, 0.531131744, 0.417964488, -0.461490363, 0.574657619),
        17: (0.0729999989, 0.0, 0.0, 0.700396299, 0.0, 0.109709017, 0.705272257),
        23: (0.0299999993, 0.0, 0.0, 0.737277329, 0.0, 0.0, 0.675590217),
        29: (0.0173819065, -0.018187616, 0.142023385, -0.039799694, 0.00437888689, 0.328263462, -0.943736851),
    },
}


def rand_R(rng):
    q = rng.normal(size=4)
    return sl.q_to_R(tuple(q / np.linalg.norm(q)))


def angle(a, b):
    return math.degrees(math.acos(max(-1.0, min(1.0, float(a @ b) / (np.linalg.norm(a) * np.linalg.norm(b))))))


class SynthTest(unittest.TestCase):
    def test_matches_the_driver(self):
        for (right, curls, splays), bones in GOLDEN.items():
            out = sl.synthesize(right, curls, splays)
            for b, g in bones.items():
                q, p = out[b]
                np.testing.assert_allclose(p, g[:3], atol=1e-6)
                same = np.allclose(q, g[3:], atol=1e-6) or np.allclose(q, [-x for x in g[3:]], atol=1e-6)
                self.assertTrue(same, f"bone {b}: {q} vs {g[3:]}")

    def test_curls_match_the_driver(self):
        """CurlsFromBones of the compiled driver on the open left hand."""
        got = sl.curls_from_bones(sl.synthesize(False, (0,) * 5))
        np.testing.assert_allclose(got, (0.0, 0.0519999824, 0.0399999842, 0.0439999849, 0.0479999892), atol=1e-6)


class RetargetTest(unittest.TestCase):
    def test_skeleton_lands_on_the_hand(self):
        """pose(): the SteamVR skeleton's wrist, palm frame and every finger segment follow the given hand."""
        rng = np.random.default_rng(3)
        out = sl.SteamVROutput(port=9)
        for right in (True, False):
            for _ in range(20):
                src = sl.synthesize(right, rng.uniform(0, 1, 5), rng.uniform(-1, 1, 5))
                J = sl.joints(sl.model_space(src))
                R = rand_R(rng)
                W = J @ R.T + rng.normal(size=3)                 # a hand somewhere in the room
                F = sl.hand_frame(W, right)
                local = (W - W[1]) @ F
                bones, p_raw, R_raw = out.pose(right, W[1], F, local)
                G = sl.joints(sl.model_space(bones)) @ R_raw.T + p_raw
                self.assertLess(np.linalg.norm(G[1] - W[1]), 1e-9)
                Fg = sl.hand_frame(G, right)
                self.assertLess(np.abs(Fg - F).max(), 1e-9)
                for ch in sl.CHAINS:
                    for b in ch[:-1]:
                        self.assertLess(angle(G[b + 1] - G[b], W[b + 1] - W[b]), 1e-4)
        out.close()


class FakeLink:
    def __init__(self):
        self.ctx = None

    def context(self, max_age=0.5):
        return self.ctx


def tap_context(seq, bones, raw_pos=(0.1, 1.2, -0.3), raw_rot=(1, 0, 0, 0)):
    tap = {"pose_valid": True, "skel_valid": True, "tracking_result": 200, "bone_count": 31, "system_click": False,
           "source_found": True, "skel_age_us": 5000, "raw_pos": raw_pos, "raw_rot": raw_rot, "lin_vel": (0, 0, 0),
           "ang_vel": (0, 0, 0), "bones": bones}
    none = dict(tap, pose_valid=False, skel_valid=False, source_found=False, bones=[(0,) * 7] * 31)
    return {"seq": seq, "hmd_valid": True, "hmd_pos": (0, 1.6, 0), "hmd_rot": (1, 0, 0, 0), "mode": [2, 2],
            "tap": [none, tap]}


class ContextSourceTest(unittest.TestCase):
    def test_joints_and_lost_hand(self):
        b = sl.synthesize(True, (0.3,) * 5)
        bones = [(p[0], p[1], p[2], q[0], q[1], q[2], q[3]) for q, p in b]
        link = FakeLink()
        src = sl.SteamVRContextSource(link)
        link.ctx = tap_context(1, bones)
        src.update()
        self.assertIsNone(src.world_joints[0])
        J = np.asarray(src.world_joints[1])
        np.testing.assert_allclose(J[1], np.asarray(b[1][1]) + (0.1, 1.2, -0.3), atol=1e-9)
        self.assertEqual(src.hmd["quat"], [0, 0, 0, 1])      # x, y, z, w for the gate
        # the same skeleton for longer than LOST_S: the headset lost the hand
        time.sleep(sl.SteamVRContextSource.LOST_S + 0.02)
        link.ctx = tap_context(2, bones)
        src.update()
        self.assertIsNone(src.world_joints[1])
        link.ctx = None
        src.update()
        self.assertIsNone(src.hmd)


class PoseFilterTest(unittest.TestCase):
    def test_matches_the_driver(self):
        """src/PoseFilter.h with its defaults, compiled, on the same samples (incl. a one-frame runaway at 25)."""
        golden = {10: ((0.171193501607, 1.23833129498, -0.4), (0.97977498443, 0.0603331507583, 0.180999452275, 0.0603331507583),
                       (0.399040974624, -0.0512330148455, 0)),
                  25: ((0.137546303636, 1.19984755983, -0.4), (0.961352043202, 0.0830127312419, 0.249038193726, 0.0830127312419),
                       (-0.328674312067, -0.0971374014041, 0)),
                  39: ((-0.143842365431, 1.15629132962, -0.4), (0.999749800957, -0.00674426009728, -0.0202327802918, -0.00674426009728),
                       (-0.476440835546, -0.0613261701491, 0))}
        f = sl.PoseFilter()
        t = 0.0
        for i in range(40):
            t += 1.0 / 60
            p = [0.2 * math.sin(t * 6.0) + 0.002 * math.sin(i * 2.3), 1.2 + 0.05 * math.cos(t * 4.0), -0.4]
            a = 0.6 * math.sin(t * 5.0) + 0.01 * math.cos(i * 1.7)
            q = sl.qnorm((math.cos(a / 2), 0.3 * math.sin(a / 2), 0.9 * math.sin(a / 2), 0.3 * math.sin(a / 2)))
            if i == 25:
                p[0] += 0.3
            pf, qf, v, _ = f.filter(p, q, t)
            if i in golden:
                gp, gq, gv = golden[i]
                np.testing.assert_allclose(pf, gp, atol=1e-9)
                np.testing.assert_allclose(qf, gq, atol=1e-9)
                np.testing.assert_allclose(v, gv, atol=1e-9)
        self.assertEqual(f.rejected, 1)


class OutputTest(unittest.TestCase):
    def test_stream(self):
        """A Studio frame becomes a steady stream of CFHS that puts the hand where it was drawn; it stops when the
        Studio stops."""
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(1.0)
        out = sl.SteamVROutput(port=rx.getsockname()[1])
        src = sl.synthesize(True, (0.1, 0.2, 0.3, 0.4, 0.5))
        W = sl.joints(sl.model_space(src)) + (0.0, 1.0, -0.4)
        F = sl.hand_frame(W, True)
        self.assertTrue(out.publish(1, True, W[1], F, (W - W[1]) @ F, camera_sees=True, optical_seq=7))
        got = []
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 0.08:
            got.append(cfp.unpack_hand_state(rx.recvfrom(4096)[0]))
        self.assertGreaterEqual(len(got), 5)                     # ~120 per second
        hs = got[-1]
        self.assertEqual(hs["header"]["hand"], 1)
        self.assertTrue(hs["header"]["flags"] & cfp.HS_POSE_VALID and hs["header"]["flags"] & cfp.HS_HAS_BONES)
        self.assertTrue(hs["header"]["flags"] & cfp.HS_CAMERA_SEES)
        self.assertEqual(hs["optical_seq"], 7)
        self.assertEqual(len(hs["bones"]), 31)
        G = sl.joints(sl.model_space([((x[3], x[4], x[5], x[6]), (x[0], x[1], x[2])) for x in hs["bones"]]))
        G = G @ sl.q_to_R(hs["raw_rot"]).T + np.asarray(hs["raw_pos"])
        self.assertLess(np.linalg.norm(G[1] - W[1]), 1e-5)    # float32 on the wire; one frame: the filter starts there
        time.sleep(sl.SteamVROutput.STALE_S + 0.05)            # the Studio stopped: so does the stream
        rx.settimeout(0.05)
        while True:
            try:
                rx.recvfrom(4096)
            except socket.timeout:
                break
        with self.assertRaises(socket.timeout):
            rx.recvfrom(4096)
        out.close()
        rx.close()


if __name__ == "__main__":
    unittest.main()
