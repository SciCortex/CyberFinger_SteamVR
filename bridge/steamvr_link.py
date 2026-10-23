"""Fusion Studio ↔ the CyberFinger SteamVR driver.

  SteamVRContextSource  the headset's own hand tracking as the driver taps it (CFOP, UDP 27016), in SteamVR raw
                        space: the Studio's camera input while it drives SteamVR. Live Preview (OpenXR) and the OpenVR
                        skeleton reader read the CyberFinger hands themselves, so once the Studio drives them they would
                        read the Studio's own output back.
  SteamVROutput         the Studio's hand as a fused hand state (CFHS, UDP 27015): /pose/raw, velocities and the 31
                        SteamVR bones. The driver shows it (FUSED) while packets keep coming, and goes back to the
                        headset hand (PASSTHROUGH) 150 ms after they stop.

The bones follow the driver's conventions: synthesize() is a port of src/SkeletonSynth.cpp (itself Valve's
handskeletonsimulation sample), checked against the compiled C++. retarget() starts from that skeleton at the hand's
curls and turns each finger bone so its child joint lies where the Studio's hand has it, so every finger segment points
exactly the Studio's way (bone lengths stay Valve's). /pose/raw is placed so the skeleton's wrist and palm frame land on
the Studio's wrist and hand frame (STEAMVR_PLAN.md §8.4).
"""

import math
import socket
import threading
import time

import numpy as np

import cf_protocol

# ── skeleton layout (src/BoneData.h) ──
NUM_BONES = 31
PARENT = (-1, 0, 1, 2, 3, 4, 1, 6, 7, 8, 9, 1, 11, 12, 13, 14, 1, 16, 17, 18, 19, 1, 21, 22, 23, 24, 0, 0, 0, 0, 0)
AUX_SOURCE = (4, 9, 14, 19, 24)                      # the last knuckle bone of each finger
FIRST = (2, 6, 11, 16, 21)                           # first bone of each finger chain (thumb 4 bones, others 5)
CHAINS = tuple(tuple(range(f, f + (4 if i == 0 else 5))) for i, f in enumerate(FIRST))


# ── quaternions (w, x, y, z) as plain tuples: faster than numpy for single 4-vectors ──
def qmul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (aw * bw - ax * bx - ay * by - az * bz, aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx, aw * bz + ax * by - ay * bx + az * bw)


def qconj(q):
    return (q[0], -q[1], -q[2], -q[3])


def qnorm(q):
    n = math.sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3])
    return (1.0, 0.0, 0.0, 0.0) if n < 1e-12 else (q[0] / n, q[1] / n, q[2] / n, q[3] / n)


def qrot(q, v):
    """v' = v + 2w(u×v) + 2u×(u×v), as MathUtil.h Rotate."""
    w, ux, uy, uz = q
    vx, vy, vz = v
    tx, ty, tz = 2 * (uy * vz - uz * vy), 2 * (uz * vx - ux * vz), 2 * (ux * vy - uy * vx)
    return (vx + w * tx + uy * tz - uz * ty, vy + w * ty + uz * tx - ux * tz, vz + w * tz + ux * ty - uy * tx)


def q_to_R(q):
    w, x, y, z = qnorm(q)
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                     [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                     [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])


def R_to_q(R):
    R = np.asarray(R, float)
    t = R[0, 0] + R[1, 1] + R[2, 2]
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        q = (0.25 * s, (R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s)
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2
        q = ((R[2, 1] - R[1, 2]) / s, 0.25 * s, (R[0, 1] + R[1, 0]) / s, (R[0, 2] + R[2, 0]) / s)
    elif R[1, 1] > R[2, 2]:
        s = math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2
        q = ((R[0, 2] - R[2, 0]) / s, (R[0, 1] + R[1, 0]) / s, 0.25 * s, (R[1, 2] + R[2, 1]) / s)
    else:
        s = math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2
        q = ((R[1, 0] - R[0, 1]) / s, (R[0, 2] + R[2, 0]) / s, (R[1, 2] + R[2, 1]) / s, 0.25 * s)
    q = qnorm(q)
    return q if q[0] >= 0 else (-q[0], -q[1], -q[2], -q[3])


def q_between(u, v):
    """The shortest rotation taking direction u to direction v (both non-zero)."""
    ux, uy, uz = u
    vx, vy, vz = v
    nu = math.sqrt(ux * ux + uy * uy + uz * uz)
    nv = math.sqrt(vx * vx + vy * vy + vz * vz)
    ux, uy, uz, vx, vy, vz = ux / nu, uy / nu, uz / nu, vx / nv, vy / nv, vz / nv
    d = ux * vx + uy * vy + uz * vz
    if d < -0.999999:                                    # opposite: any perpendicular axis
        ax = (0.0, -uz, uy) if abs(ux) < 0.9 else (-uz, 0.0, ux)
        n = math.sqrt(ax[0] ** 2 + ax[1] ** 2 + ax[2] ** 2)
        return (0.0, ax[0] / n, ax[1] / n, ax[2] / n)
    return qnorm((1.0 + d, uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx))


def xmul(a, b):
    """Xform (q, p) composition, as MathUtil.h: a · b."""
    qa, pa = a
    qb, pb = b
    r = qrot(qa, pb)
    return (qmul(qa, qb), (pa[0] + r[0], pa[1] + r[1], pa[2] + r[2]))


# ── Valve's simulated hand (port of src/SkeletonSynth.cpp SynthesizeSkeleton) ──
_LEN = ((0.05, 0.05, 0.035, 0.025, 0.0), (0.03, 0.073, 0.045, 0.025, 0.02), (0.01, 0.091, 0.049, 0.03, 0.02),
        (0.02, 0.073, 0.045, 0.03, 0.03), (0.03, 0.067, 0.03, 0.025, 0.02))
_D = math.pi / 180.0


def _swing_twist(s0, s1, twist):
    sq = s0 * s0 + s1 * s1
    ct, st = math.cos(twist * 0.5), math.sin(twist * 0.5)
    if sq > 0.0:
        th = math.sqrt(sq)
        cs = math.cos(th * 0.5)
        k = math.sin(th * 0.5) / th
        return (cs * ct, cs * st, s1 * ct * k - s0 * st * k, s0 * ct * k + s1 * st * k)
    k = 0.5
    return (ct, st, s1 * ct * k - s0 * st * k, s0 * ct * k + s1 * st * k)


def _hinge(r):
    return (math.cos(r * 0.5), 0.0, 0.0, math.sin(r * 0.5))


def _bone(right, q, pos):
    return (q, ((-pos[0] if right else pos[0]), pos[1], pos[2]))


def _metacarpal(right, q, length):
    bone = qmul((0.5, 0.5, -0.5, 0.5), q)
    pos = qrot(bone, (length, 0.0, 0.0))
    if right:
        w, x, y, z = bone
        bone = (x, -w, z, -y)
    return _bone(right, bone, pos)


def synth_wrist(right):
    """The synthesized skeleton's wrist bone (raw → wrist), from the Index controller."""
    q, p = (-0.055147, -0.078608, -0.920279, 0.379296), (-0.034038, 0.036503, 0.164722)
    if right:
        q, p = (q[0], q[1], -q[2], -q[3]), (-p[0], p[1], p[2])
    return (q, p)


def synthesize(right, curls, splays=(0.0,) * 5):
    """31 bones as Xforms (q, p), parent space: SynthesizeSkeleton(right, curls, splays)."""
    c = [min(1.0, max(0.0, float(v))) for v in curls]
    s = [min(1.0, max(-1.0, float(v))) for v in splays]
    out = [None] * NUM_BONES
    out[0] = ((1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    out[1] = synth_wrist(right)
    # thumb (metacarpal twist 0, passed to the proximal joint too, as in the sample)
    tm0, tm1 = 10 * _D + c[0] * 5 * _D, 40 * _D + s[0] * 5 * _D
    tp0, tp1 = c[0] * 90 * _D, s[0] * 20 * _D
    out[2] = _metacarpal(right, _swing_twist(tm0, tm1, 0.0), _LEN[0][0])
    out[3] = _bone(right, _swing_twist(tp0, tp1, 0.0), (_LEN[0][1], 0.0, 0.0))
    out[4] = _bone(right, _hinge(c[0] * 90 * _D), (_LEN[0][2], 0.0, 0.0))
    out[5] = _bone(right, (1.0, 0.0, 0.0, 0.0), (_LEN[0][3], 0.0, 0.0))
    meta_splay, prox_splay = (13, 0, -15, -27), (3, 0, -1, -2)
    for i in range(4):
        cu, sp, ln, b = c[i + 1], s[i + 1], _LEN[i + 1], FIRST[i + 1]
        out[b] = _metacarpal(right, _swing_twist(cu * 5 * _D, meta_splay[i] * _D, 0.0), ln[0])
        out[b + 1] = _bone(right, _swing_twist(cu * 90 * _D, prox_splay[i] * _D + sp * 15 * _D, 0.0), (ln[1], 0.0, 0.0))
        out[b + 2] = _bone(right, _hinge(5 * _D + cu * 80 * _D), (ln[2], 0.0, 0.0))
        out[b + 3] = _bone(right, _hinge(5 * _D + cu * 80 * _D), (ln[3], 0.0, 0.0))
        out[b + 4] = _bone(right, (1.0, 0.0, 0.0, 0.0), (ln[4], 0.0, 0.0))
    fill_aux(out)
    return out


def model_space(bones):
    """Root-relative transforms by forward kinematics (ModelSpace)."""
    m = [None] * NUM_BONES
    for i in range(NUM_BONES):
        m[i] = bones[i] if PARENT[i] < 0 else xmul(m[PARENT[i]], bones[i])
    return m


def fill_aux(bones):
    for f in range(5):                                   # the aux bones hang off the root: not needed for the chains
        if bones[26 + f] is None:
            bones[26 + f] = ((1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    m = model_space(bones)
    for f in range(5):
        bones[26 + f] = m[AUX_SOURCE[f]]


def joints(model):
    """(26, 3) OpenXR-order joints in the skeleton's model space: joints 1–25 are bone origins 1–25, the palm (0) the
    midpoint of the middle metacarpal and proximal origins."""
    J = np.empty((26, 3))
    for b in range(1, 26):
        J[b] = model[b][1]
    J[0] = 0.5 * (J[11] + J[12])
    return J


def _chain_curls(P):
    """Per-finger curl as CurlsFromBones: the summed bend along each chain over 180° (thumb) / 250° (fingers)."""
    out = []
    for f, ch in enumerate(CHAINS):
        bend = 0.0
        for k in range(1, len(ch) - 1):
            a = P[ch[k]] - P[ch[k - 1]]
            b = P[ch[k + 1]] - P[ch[k]]
            la, lb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
            if la < 1e-6 or lb < 1e-6:
                continue
            bend += math.acos(max(-1.0, min(1.0, float(a @ b) / (la * lb))))
        out.append(max(0.0, min(1.0, bend / ((180.0 if f == 0 else 250.0) * _D))))
    return out


def curls_from_bones(bones):
    return _chain_curls(joints(model_space(bones)))


def hand_frame(P, right):
    """The Studio's hand frame (fusion/extrinsic.py hand_frame_wrist): forward = wrist → metacarpal midpoint,
    across the index/pinky metacarpals, normal = across × forward."""
    w, ik, pk = P[1], P[6], P[21]
    fwd = 0.5 * (ik + pk) - w
    fwd = fwd / (np.linalg.norm(fwd) or 1.0)
    across = (ik - pk) if right else (pk - ik)
    n = np.cross(across, fwd)
    n = n / (np.linalg.norm(n) or 1.0)
    return np.column_stack([fwd, np.cross(n, fwd), n])


def retarget(right, local):
    """The Studio's hand (26 joints in its hand frame, wrist-relative) → (31 bones, R_ref, wrist_ref): R_ref is the
    skeleton's hand frame and wrist_ref its wrist in model space, which place /pose/raw (see SteamVROutput)."""
    L = np.asarray(local, float)
    L = L - L[1]
    bones = synthesize(right, _chain_curls(L))          # start from Valve's hand at the same curls: small turns left
    m = model_space(bones)
    J = joints(m)
    R_ref = hand_frame(J, right)
    T = J[1] + L @ R_ref.T                              # target joints in model space
    for ch in CHAINS:
        for k, b in enumerate(ch):
            m[b] = xmul(m[PARENT[b]], bones[b])
            if k == len(ch) - 1:
                break
            c = b + 1
            cur = qrot(m[b][0], bones[c][1])
            tgt = T[c] - T[b]
            if float(tgt @ tgt) < 1e-10 or (cur[0] ** 2 + cur[1] ** 2 + cur[2] ** 2) < 1e-10:
                continue
            q_new = qmul(q_between(cur, (float(tgt[0]), float(tgt[1]), float(tgt[2]))), m[b][0])
            bones[b] = (qnorm(qmul(qconj(m[PARENT[b]][0]), q_new)), bones[b][1])
            m[b] = xmul(m[PARENT[b]], bones[b])
    fill_aux(bones)
    return bones, R_ref, J[1].copy()


def world_joints_from_tap(raw_pos, raw_rot, bones):
    """A tapped headset hand (raw pose + bones as (px, py, pz, qw, qx, qy, qz)) → (26, 3) joints in raw space."""
    xb = [((b[3], b[4], b[5], b[6]), (b[0], b[1], b[2])) for b in bones[:NUM_BONES]]
    J = joints(model_space(xb))
    R = q_to_R(raw_rot)
    return J @ R.T + np.asarray(raw_pos, float)


# ── the headset hand from the driver ──
class SteamVRContextSource:
    """Duck-typed skeleton source (as OpenXRHandSkeletonSource) built from the driver's context stream (CFOP):
    hmd, world_joints, active, wrist_valid, hands, pose_info, jflags, jradius, status. Call update() each poll."""

    LOST_S = 0.08        # an unchanged skeleton this long: the headset lost the hand (the driver's kOcclusion)

    def __init__(self, link):
        self.link = link
        self.hands = [None, None]
        self.pose_info = [None, None]
        self.world_joints = [None, None]
        self.jflags = [None, None]
        self.jradius = [None, None]
        self.active = [0, 0]
        self.wrist_valid = [0, 0]
        self.hmd = None
        self.status = "waiting for the CyberFinger SteamVR driver"
        self.ctx = None
        self.seq = None
        self._prev = [None, None]
        self._changed = [0.0, 0.0]

    def update(self):
        ctx = self.link.context(max_age=0.25) if self.link is not None else None
        if ctx is None:
            self.ctx = None
            self.world_joints = [None, None]
            self.active = [0, 0]
            self.wrist_valid = [0, 0]
            self.hmd = None
            self.status = "no data from the CyberFinger driver — SteamVR running, VR Mode on?"
            return
        self.ctx = ctx
        if ctx["seq"] == self.seq:
            return
        self.seq = ctx["seq"]
        now = time.perf_counter()
        q = ctx["hmd_rot"]
        self.hmd = {"pos": list(ctx["hmd_pos"]), "quat": [q[1], q[2], q[3], q[0]], "valid": 1 if ctx["hmd_valid"] else 0}
        wj, act = [None, None], [0, 0]
        for h in (0, 1):
            tap = ctx["tap"][h]
            key = tap["bones"][1:26]
            if key != self._prev[h]:
                self._prev[h] = key
                self._changed[h] = now
            ok = (tap["source_found"] and tap["pose_valid"] and tap["skel_valid"] and tap["bone_count"] >= 26
                  and now - self._changed[h] <= self.LOST_S)
            if ok:
                try:
                    J = world_joints_from_tap(tap["raw_pos"], tap["raw_rot"], tap["bones"])
                    if np.isfinite(J).all():
                        wj[h] = tuple((float(a), float(b), float(c)) for a, b, c in J)
                        act[h] = 1
                except Exception:
                    pass
        self.world_joints = wj
        self.active = act
        self.wrist_valid = list(act)
        self.status = "SteamVR headset hand (via the CyberFinger driver)"


# ── the driver's PASSTHROUGH pose filter, for the Studio's hand ──
def qslerp(a, b, t):
    """MathUtil.h Slerp."""
    d = a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]
    if d < 0:
        b = (-b[0], -b[1], -b[2], -b[3])
        d = -d
    if d > 0.9995:
        return qnorm(tuple(a[k] + t * (b[k] - a[k]) for k in range(4)))
    th = math.acos(d)
    s = math.sin(th)
    wa, wb = math.sin((1 - t) * th) / s, math.sin(t * th) / s
    return tuple(wa * a[k] + wb * b[k] for k in range(4))


def _rotvec(q):
    """Axis · angle of a unit quaternion (PoseFilter.h RotationVector)."""
    r = q if q[0] >= 0 else (-q[0], -q[1], -q[2], -q[3])
    s = math.sqrt(r[1] * r[1] + r[2] * r[2] + r[3] * r[3])
    if s < 1e-9:
        return np.array([2 * r[1], 2 * r[2], 2 * r[3]])
    return np.array(r[1:]) * (2.0 * math.atan2(s, r[0]) / s)


class PoseFilter:
    """Port of the driver's src/PoseFilter.h, with its default settings (resources/settings/default.vrsettings):
    a One Euro filter on position and rotation, velocities from the filtered pose (low-passed, capped, scaled by
    `prediction` / `rot_prediction`) and the glitch gate. The driver runs it on Steam Link's hand in PASSTHROUGH; the
    Studio runs it on its own hand before FUSED, so both reach SteamVR alike (Steam Link's poses arrive already
    predicted and noisy, and SteamVR extrapolates again)."""

    def __init__(self, min_cutoff=1.0, beta=15.0, rot_beta=1.0, speed_cutoff=1.0, velocity_cutoff=5.0,
                 prediction=0.5, rot_prediction=0.0, max_speed=4.0, max_angular_speed=15.0, gate=0.05,
                 gate_angle=0.5, resync=0.08):
        self.min_cutoff, self.beta, self.rot_min_cutoff, self.rot_beta = min_cutoff, beta, min_cutoff, rot_beta
        self.speed_cutoff, self.velocity_cutoff = speed_cutoff, velocity_cutoff
        self.prediction, self.rot_prediction = prediction, rot_prediction
        self.max_speed, self.max_angular_speed = max_speed, max_angular_speed
        self.gate, self.gate_angle, self.resync = gate, gate_angle, resync
        self.rejected = 0
        self.reset()

    def reset(self):
        self._init = False

    @staticmethod
    def _alpha(cutoff, dt):
        tau = 1.0 / (2.0 * math.pi * max(cutoff, 1e-3))
        return 1.0 / (1.0 + tau / dt)

    @staticmethod
    def _cap(a, mx):
        n = float(np.linalg.norm(a))
        return a * (mx / n) if (n > mx and n > 0) else a

    def _restart(self, p, q, t):
        self._p, self._q = np.array(p, float), tuple(q)
        self._dv, self._dw, self._v, self._w = np.zeros(3), np.zeros(3), np.zeros(3), np.zeros(3)
        self._t = self._last = t
        self._init = True

    def filter(self, p, q, t):
        """One sample (position, unit quaternion w, x, y, z) at time t (s) → (p, q, v, w): the filtered pose and the
        velocities to publish."""
        p = np.asarray(p, float)
        if not self._init or t - self._t > 0.25 or t < self._t:
            self._restart(p, q, t)
            return self._p.copy(), self._q, np.zeros(3), np.zeros(3)
        dt = max(t - self._t, 0.002)
        qs = tuple(q)
        if sum(a * b for a, b in zip(qs, self._q)) < 0:
            qs = (-qs[0], -qs[1], -qs[2], -qs[3])
        since = t - self._last
        ang = 2.0 * math.acos(min(1.0, abs(sum(a * b for a, b in zip(self._q, qs)))))
        far = (float(np.linalg.norm(p - (self._p + self._v * dt))) > self.gate + self.max_speed * since
               or ang > self.gate_angle + self.max_angular_speed * since)
        if far:
            self.rejected += 1
            if since < self.resync:
                return (self._p.copy(), self._q, self._cap(self._v, self.max_speed) * self.prediction,
                        self._cap(self._w, self.max_angular_speed) * self.rot_prediction)
            self._restart(p, q, t)
            return self._p.copy(), self._q, np.zeros(3), np.zeros(3)
        self._last = t
        self._t = t
        a_s = self._alpha(self.speed_cutoff, dt)
        dp = p - self._p
        self._dv = self._dv + (dp / dt - self._dv) * a_s
        self._dw = self._dw + (_rotvec(qnorm(qmul(qs, qconj(self._q)))) / dt - self._dw) * a_s
        p_new = self._p + dp * self._alpha(self.min_cutoff + self.beta * float(np.linalg.norm(self._dv)), dt)
        q_new = qslerp(self._q, qs, self._alpha(self.rot_min_cutoff + self.rot_beta * float(np.linalg.norm(self._dw)), dt))
        a_v = self._alpha(self.velocity_cutoff, dt)
        self._v = self._v + ((p_new - self._p) / dt - self._v) * a_v
        self._w = self._w + (_rotvec(qnorm(qmul(q_new, qconj(self._q)))) / dt - self._w) * a_v
        self._p = p_new
        self._q = qnorm(q_new)
        return (self._p.copy(), self._q, self._cap(self._v, self.max_speed) * self.prediction,
                self._cap(self._w, self.max_angular_speed) * self.rot_prediction)


# ── the Studio's hand to the driver ──
class SteamVROutput:
    """Sends the Studio's hand to the driver as CFHS. publish() hands over one Studio frame (from the Tk thread); a
    sender thread streams at RATE Hz, interpolating between the two latest frames DELAY behind (so fingers and rotation
    don't step at the Studio's frame rate on a 90/120 Hz display), and stops when the Studio stops (the driver then
    shows the headset hand again)."""

    RATE = 120.0
    STALE_S = 0.12               # no Studio frame this long: stop sending
    NB = NUM_BONES

    # The filter for the Studio's hand: the driver's, a little lighter, and linear prediction ×1 (rotation still not
    # extrapolated). On a recorded hand with Steam Link-like noise, through a simulated 90 Hz SteamVR display: palm
    # jitter 10.2° → 1.45° (the old output extrapolated noisy angular velocity), wrist jitter 6.2 → 3.4 mm, error vs the
    # real hand median 5.6 → 4.0 mm (p95 11.1 → 9.5), overshoot when stopping 4.4 → 1.6 mm, delay 15 → 30 ms. The
    # driver's own settings (1 Hz, beta 15, ×0.5) give 1.06° but lag 55 ms.
    FILTER = dict(min_cutoff=1.5, beta=25.0, rot_beta=2.0, prediction=1.0, rot_prediction=0.0)

    def __init__(self, port=cf_protocol.DRIVER_PORT, filter_pose=True):
        self.addr = ("127.0.0.1", port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.filter = PoseFilter(**self.FILTER) if filter_pose else None
        self.seq = 0
        self.sent = 0
        self.rate = 0.0               # packets per second (sender)
        self.studio_rate = 0.0        # Studio frames per second
        self.last_error = ""
        self._frames = []             # [(t, frame dict)], newest last, at most 3
        self._lock = threading.Lock()
        self._dt_ema = None
        self._run = True
        self._thread = threading.Thread(target=self._loop, name="cf-steamvr-out", daemon=True)
        self._thread.start()

    def close(self):
        self._run = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        try:
            self.sock.close()
        except Exception:
            pass

    @staticmethod
    def pose(right, wrist, hand_R, hand_local):
        """→ (bones, p_raw, R_raw): the skeleton and the /pose/raw that put its wrist at `wrist` with the drawn palm
        frame (raw space). Unfiltered."""
        bones, H, w_ref, R_ref = SteamVROutput._shape(right, hand_R, hand_local)
        R_raw = H @ R_ref.T
        return bones, np.asarray(wrist, float) - R_raw @ w_ref, R_raw

    @staticmethod
    def _shape(right, hand_R, hand_local):
        L = np.asarray(hand_local, float)
        L = L - L[1]
        F = hand_frame(L, right)          # the drawn palm's own frame (identity for a camera hand; templates, the EMG
        bones, R_ref, w_ref = retarget(right, L @ F)    # hand and blends can sit a few degrees off): the palm lands too
        return bones, np.asarray(hand_R, float) @ F, w_ref, R_ref

    def publish(self, hand, right, wrist, hand_R, hand_local, camera_sees=False, calibrated=False, finger_conf=None,
                optical_seq=0, age_s=0.0, pos_src=0, rot_src=0, pose_src=0, key_posture=0):
        """One Studio frame (any thread). → True if it was taken."""
        try:
            now = time.perf_counter()
            bones, H, w_ref, R_ref = self._shape(right, hand_R, hand_local)
            pw = np.asarray(wrist, float)
            qh = R_to_q(H)
            v = w = np.zeros(3)
            if self.filter is not None:                  # the hand frame at the wrist, as the driver filters Steam Link's
                pw, qh, v, w = self.filter.filter(pw, qh, now)
            R_raw = q_to_R(qh) @ R_ref.T
            p_raw = pw - R_raw @ w_ref
            # /pose/raw velocities that move the WRIST by v and turn the hand by w (SteamVR extrapolates the root)
            lin = v + np.cross(w, p_raw - pw)
            Q = np.array([q for q, _ in bones], float)
            P = np.array([b for _, b in bones], float)
            fc = [1.0] * 5 if finger_conf is None else [float(min(1.0, max(0.0, x))) for x in finger_conf]
            fr = {"hand": int(hand), "p": p_raw, "q": R_to_q(R_raw), "lin": lin, "ang": np.asarray(w, float), "Q": Q, "P": P,
                  "curl": np.array(curls_from_bones(bones)), "cam": bool(camera_sees), "cal": bool(calibrated), "fc": fc,
                  "oseq": int(optical_seq or 0), "src": (int(pos_src), int(rot_src), int(pose_src), int(key_posture))}
            with self._lock:
                if self._frames:
                    d = now - self._frames[-1][0]
                    if 0.0 < d < 0.25:
                        self._dt_ema = d if self._dt_ema is None else 0.9 * self._dt_ema + 0.1 * d
                        self.studio_rate = 1.0 / self._dt_ema
                self._frames = (self._frames + [(now, fr)])[-3:]
            return True
        except Exception as e:
            self.last_error = repr(e)
            return False

    def delay(self):
        """How far behind now the stream is drawn: a bit more than one Studio frame, so two frames bracket it."""
        return min(0.06, max(0.012, 1.25 * (self._dt_ema or 1 / 60.0)))

    def _sample(self, now):
        """The interpolated frame at now − delay, or None when the Studio stopped."""
        with self._lock:
            frames = list(self._frames)
        if not frames or now - frames[-1][0] > self.STALE_S:
            return None, 0.0
        d = self.delay()
        tr = now - d
        if len(frames) == 1 or tr >= frames[-1][0]:
            return frames[-1][1], now - frames[-1][0]
        for (ta, a), (tb, b) in zip(frames[:-1], frames[1:]):
            if ta <= tr <= tb:
                break
        else:
            return frames[0][1], now - frames[0][0]
        x = (tr - ta) / max(tb - ta, 1e-6)
        Qa, Qb = a["Q"], b["Q"]
        sgn = np.where((Qa * Qb).sum(1, keepdims=True) < 0, -1.0, 1.0)
        Q = (1 - x) * Qa + x * sgn * Qb
        Q /= np.linalg.norm(Q, axis=1, keepdims=True)
        out = dict(b)
        out.update({"p": (1 - x) * a["p"] + x * b["p"], "q": qslerp(a["q"], b["q"], x), "Q": Q,
                    "P": (1 - x) * a["P"] + x * b["P"], "lin": (1 - x) * a["lin"] + x * b["lin"],
                    "ang": (1 - x) * a["ang"] + x * b["ang"], "curl": (1 - x) * a["curl"] + x * b["curl"]})
        return out, d

    def _loop(self):
        period = 1.0 / self.RATE
        nxt = time.perf_counter()
        rate_t, rate_n = nxt, 0
        while self._run:
            nxt += period
            now = time.perf_counter()
            if nxt < now - period:                      # fell behind (a stall): don't burst
                nxt = now + period
            try:
                fr, age = self._sample(now)
                if fr is not None:
                    self.seq += 1
                    pkt = cf_protocol.pack_hand_state(
                        fr["hand"], self.seq, tuple(float(x) for x in fr["p"]), tuple(float(x) for x in fr["q"]),
                        lin_vel=tuple(float(x) for x in fr["lin"]), ang_vel=tuple(float(x) for x in fr["ang"]),
                        curl=tuple(float(x) for x in fr["curl"]), splay=(0.0,) * 5,
                        bones=[(P[0], P[1], P[2], Q[0], Q[1], Q[2], Q[3]) for P, Q in zip(fr["P"].tolist(), fr["Q"].tolist())],
                        pose_valid=True, camera_sees=fr["cam"], calibrated=fr["cal"], pose_conf=float(sum(fr["fc"]) / 5.0),
                        finger_conf=fr["fc"], optical_seq=fr["oseq"], pos_src=fr["src"][0], rot_src=fr["src"][1],
                        pose_src=fr["src"][2], key_posture=fr["src"][3], age_us=int(min(age, 0.1) * 1e6))
                    self.sock.sendto(pkt, self.addr)
                    self.sent += 1
                    rate_n += 1
                    self.last_error = ""
            except Exception as e:
                self.last_error = repr(e)
            if now - rate_t >= 1.0:
                self.rate = rate_n / (now - rate_t)
                rate_t, rate_n = now, 0
            time.sleep(max(0.0, nxt - time.perf_counter()))

    def reset_motion(self):
        if self.filter is not None:
            self.filter.reset()
