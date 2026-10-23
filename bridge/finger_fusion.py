"""Camera ↔ EMG finger fusion: hand = w · camera + (1 − w) · EMG, per finger, with w = f(m, d).

  m  how reliable the camera is for that finger (0..1): its visibility from the ray cast, lowered when the gate says
     another hand or an object is in the way (the Studio's per-finger camera confidence)
  d  how different the two estimates are: the finger's curl in the camera hand vs in the EMG hand (0..1)

w is an S-curve in m. When camera and EMG agree (small d) it is nearly a straight line (w ≈ m): mixing two
similar hands is safe and smooths them. The more they disagree, the steeper it gets, up to a step: two different hands
are not averaged into one that is neither — the more reliable source is taken, the camera above M0. Rescaled to span 0..1 at
every steepness. The EMG model learns from the camera only where the camera clearly dominates (w > LEARN_W).
"""

import math

import numpy as np

import key_postures

M0 = 0.7          # where the choice flips when camera and EMG disagree (agreeing: w ≈ m). Tested in the Studio on the 40
                  # held-posture clips (EMG from models that never saw that arm position): right posture 95.8 % (old
                  # ramp) → 96.6 % at 0.5, 96.6 % at 0.6, 96.8 % at 0.7; at 0.5 a partly hidden finger that disagrees
                  # went fully to the camera, which is right only ~70 % of the time there (93.2 → 92.1 %); 0.7 keeps
                  # 93.2 %. Hands that are no posture at all: 3.0 → 2.0 %.
K_MIN = 1.0       # steepness when they agree (≈ linear)
K_MAX = 30.0      # steepness when they disagree completely (≈ a step)
D_REF = 0.5       # curl difference counted as "completely different"
TAU = 0.06        # s: smoothing of w, so a finger near M0 doesn't flip between two different hands frame to frame
LEARN_W = 0.8     # the camera teaches the EMG only above this weight


def _sig(x):
    return 1.0 / (1.0 + np.exp(-x))


def weight(m, d, m0=None, k_min=None, k_max=None, d_ref=None):
    """Camera weight per finger (arrays or scalars). Unset parameters take the module's current settings."""
    m0 = M0 if m0 is None else m0
    k_min = K_MIN if k_min is None else k_min
    k_max = K_MAX if k_max is None else k_max
    d_ref = D_REF if d_ref is None else d_ref
    m = np.clip(np.asarray(m, float), 0.0, 1.0)
    k = k_min + (k_max - k_min) * np.clip(np.asarray(d, float) / d_ref, 0.0, 1.0)
    lo, hi = _sig(-k * m0), _sig(k * (1.0 - m0))
    return np.clip((_sig(k * (m - m0)) - lo) / (hi - lo), 0.0, 1.0)


def curl_distance(cam_local, emg_local):
    """Per-finger |curl(camera) − curl(EMG)|, with the same finger measure the posture labeller uses."""
    return np.abs(key_postures.finger_flex(np.asarray(cam_local, float)) - key_postures.finger_flex(np.asarray(emg_local, float)))


class FingerFusion:
    """Smoothed per-finger weights for a stream of frames."""

    def __init__(self, tau=TAU):
        self.tau = tau
        self.w = None

    def reset(self):
        self.w = None

    def step(self, m, d, dt):
        w = weight(m, d)
        if self.w is None or self.w.shape != w.shape:
            self.w = w
        else:
            self.w = self.w + (1.0 - math.exp(-max(float(dt), 1e-3) / self.tau)) * (w - self.w)
        return self.w.copy()


# ── keeping a mixed hand anatomical ──
# Mixing hands by averaging joint positions (camera with EMG, a held shape with a template, the smoothing between
# sources) shortens or stretches bones and can bend a joint backwards: on two replayed sessions 7.7 % of the
# camera-plus-filled-fingers hands, 12 % of the session-shape hands and 87 % of the EMG-anchored hands broke the user's
# anatomy, against 0.4 % for the camera alone. anatomize() rebuilds each finger from its root outwards with the user's
# bone lengths, keeping every joint's bend but within these limits (degrees; flexion is negative in the hand frame,
# x forward, y side, z palm normal; sideways about each finger's own hinge). The camera's own range on clear frames:
# MCP −51 … +7 (pinky +23), PIP −77 … 0, DIP −45 … +8; it rarely sees a full fist, so flexion is left anatomical.
CHAINS = ((2, 3, 4, 5), (6, 7, 8, 9, 10), (11, 12, 13, 14, 15), (16, 17, 18, 19, 20), (21, 22, 23, 24, 25))
FINGER_LIMITS = ((-100.0, 30.0, 35.0), (-120.0, 10.0, 15.0), (-100.0, 20.0, 15.0))   # MCP, PIP, DIP: flex lo, hi, sideways


def bone_lengths(L):
    """Segment lengths along the five chains, in CHAINS order (4 + 4 × 4 = 19 ... thumb 3, fingers 4 each)."""
    L = np.asarray(L, float)
    return np.array([np.linalg.norm(L[c[k + 1]] - L[c[k]]) for c in CHAINS for k in range(len(c) - 1)])


def _hinge(meta):
    """A finger's own side axis: perpendicular to the palm normal (z) and its metacarpal — the hand's side axis turned
    with the finger's splay, so a curled splayed finger doesn't read as bent sideways."""
    h = np.cross(np.array([0.0, 0.0, 1.0]), meta)
    n = float(np.linalg.norm(h))
    return h / n if n > 1e-6 else np.array([0.0, 1.0, 0.0])


def _bend(s1, s2, y=None):
    """(flexion, sideways) of direction s2 relative to s1, both unit, about side axis y (default: the hand's)."""
    y = np.array([0.0, 1.0, 0.0]) if y is None else y
    yp = y - (y @ s1) * s1
    n = np.linalg.norm(yp)
    if n < 1e-6:
        return None
    yp /= n
    return (math.atan2(float(np.cross(s1, s2) @ yp), float(s1 @ s2)), math.asin(max(-1.0, min(1.0, float(s2 @ yp)))))


def _unbend(s1, flex, side, y=None):
    y = np.array([0.0, 1.0, 0.0]) if y is None else y
    yp = y - (y @ s1) * s1
    n = np.linalg.norm(yp)
    if n < 1e-6:
        return s1
    yp /= n
    return math.cos(side) * (math.cos(flex) * s1 + math.sin(flex) * np.cross(yp, s1)) + math.sin(side) * yp


def anatomize(L, ref_lengths=None, limits=FINGER_LIMITS):
    """A hand (26 joints, hand frame, wrist at the origin) → the same hand with the user's bone lengths and every
    finger joint within its limits. Roots (thumb CMC, metacarpal bases) stay where they are; the thumb's lengths are
    restored but its bends left (it flexes about another axis)."""
    L = np.asarray(L, float)
    out = L.copy()
    ref = bone_lengths(L) if ref_lengths is None else np.asarray(ref_lengths, float)
    r = 0
    for fi, c in enumerate(CHAINS):
        prev_orig = prev_new = None
        hinge = _hinge(L[c[1]] - L[c[0]])
        for k in range(len(c) - 1):
            d = L[c[k + 1]] - L[c[k]]
            nd = float(np.linalg.norm(d))
            d = d / nd if nd > 1e-9 else (prev_new if prev_new is not None else np.array([1.0, 0.0, 0.0]))
            new_dir = d
            if prev_orig is not None and fi > 0:
                b = _bend(prev_orig, d, hinge)
                if b is not None:
                    lo, hi, side = limits[k - 1]
                    fl = min(max(b[0], math.radians(lo)), math.radians(hi))
                    sd = min(max(b[1], -math.radians(side)), math.radians(side))
                    new_dir = _unbend(prev_new, fl, sd, hinge)
            elif prev_orig is not None:                     # thumb: keep its bend, relative to the rebuilt parent
                rot = _rot_between(prev_orig, prev_new)
                new_dir = rot @ d
            out[c[k + 1]] = out[c[k]] + ref[r] * new_dir
            prev_orig, prev_new = d, new_dir / (np.linalg.norm(new_dir) or 1.0)
            r += 1
    return out


def _rot_between(a, b):
    """Rotation matrix taking unit vector a onto unit vector b (shortest arc)."""
    v = np.cross(a, b)
    c = float(a @ b)
    if c < -0.999999:
        p = np.array([1.0, 0.0, 0.0]) if abs(a[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
        v = np.cross(a, p)
        v /= np.linalg.norm(v)
        return 2.0 * np.outer(v, v) - np.eye(3)
    K = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + K + K @ K / (1.0 + c)


def anomalies(L, ref_lengths, limits=FINGER_LIMITS, tol=0.15):
    """(bones off by more than tol, joints beyond their limits) for a hand — the check the tests and tools use."""
    L = np.asarray(L, float)
    off = int(np.sum(np.abs(bone_lengths(L) / np.asarray(ref_lengths, float) - 1.0) > tol))
    bad = 0
    for fi, c in enumerate(CHAINS[1:]):
        hinge = _hinge(L[c[1]] - L[c[0]])
        for k in range(1, len(c) - 1):
            s1 = L[c[k]] - L[c[k - 1]]
            s2 = L[c[k + 1]] - L[c[k]]
            b = _bend(s1 / np.linalg.norm(s1), s2 / np.linalg.norm(s2), hinge)
            if b is None:
                continue
            lo, hi, side = limits[k - 1]
            bad += (math.degrees(b[0]) < lo - 1e-6) or (math.degrees(b[0]) > hi + 1e-6) or (abs(math.degrees(b[1])) > side + 1e-6)
    return off, bad
