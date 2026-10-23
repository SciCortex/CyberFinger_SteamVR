# SPDX-License-Identifier: GPL-3.0-only
"""Key Postures — recognise a few IMPORTANT hand postures (open, fist, point, pinch, victory) from the forearm EMG + wrist
tilt, so they can be reconstructed exactly when the camera cannot see the hand. Numpy only.

Why a separate recogniser: the continuous EMG→curl regressor is taught by the camera, and the camera cannot label a
fist / point / victory (the curled fingers hide behind the hand) nor anything out of view. For a handful of FIXED
postures the label is simply what you hold ("record fist"), so the data can be recorded exactly where it will be used —
at the hip, at the side, behind the back — which also handles the limb-position effect (EMG changes with arm posture).

Features (43): the same Hudgins TD features (8 ch × MAV, RMS, WL, ZC, SSC) + wrist tilt R(q_wrist)ᵀ·[0,−1,0] that the
Pose Hand regressor uses, so one live feature vector feeds both. Model: class-balanced softmax regression trained on
real + physically-grounded synthetic EMG (electrode roll ±1, per-channel gain, contraction intensity, noise, tilt jitter)
— the recipe already validated in Posture Hand. A time-persistence gate turns probabilities into a stable decision.
Each posture has a TEMPLATE = per-finger curls for the anatomical hand (`_procedural_hand`); where the camera saw the
posture clearly during recording, the template is the camera's median curls instead."""
import glob
import math
import os
import time

import numpy as np

POSTURES = ("open", "fist", "point", "pinch", "victory", "rest", "other")
# REST (the supervisor): the relaxed hand between fist and open — fingers naturally half curled, muscles quiet. Not in
# the recordings so far (the Transitions sessions drill the five; on the position recordings the camera calls the
# relaxed hand "open" 1151 s and rest-like 18 s), so it is recorded like the others (Key Postures › rest) and the
# recogniser learns it when ⚙ Train finds rest clips; until then nothing changes.
POSITIONS = ("front", "hip", "side", "back", "moving")
CLIP_SECONDS = 12.0
EMG_WIN = 64
AUG_WEIGHT = 0.4
TEMPLATES = {"open": (0.0, 0.0, 0.0, 0.0, 0.0), "fist": (0.90, 0.95, 0.95, 0.95, 0.90), "point": (0.85, 0.0, 0.95, 0.95, 0.95),
             "pinch": (0.60, 0.62, 0.12, 0.08, 0.05), "victory": (0.85, 0.0, 0.0, 0.95, 0.95),
             "rest": (0.30, 0.35, 0.40, 0.45, 0.50)}            # until the camera's own rest shape replaces it
COLORS = {"open": "#00e676", "fist": "#ff9100", "point": "#448aff", "pinch": "#e6007e", "victory": "#b388ff", "rest": "#26c6da",
          "other": "#888888"}
SW_TOP, SW_MARGIN, HOLD_T, OFF_P, OFF_T = 0.75, 0.25, 0.15, 0.50, 0.20
FAST_P, FAST_T = 0.97, 0.04              # a very confident leader is committed after FAST_T instead of HOLD_T (latency)
# Turning the forearm or bending the wrist changes the EMG by itself (muscles slide under the electrodes): on the 42
# held-posture clips the recogniser is right 90 % of the time while the wrist IMU turns < 30°/s and 79 % above 180°/s,
# and a turn doubles the rate of wrong switches. While an IMU turns faster than ROT_DPS the fast path is off and a new
# posture needs a clear lead for HOLD_T, growing to ROT_HOLD at twice ROT_DPS; the fast path stays off for ROT_SETTLE
# after the turn. In the Studio sessions the arm moves at 25–175°/s (median), so the first version (60°/s, 0.40 s flat,
# 0.25 s settle) was on most of the time: real switches with an active arm 141 ms median / 660 ms 90th. This one: wrong
# switches 9.0 → 6.5 per minute (6.2 for the strict one), real switches 39/143 ms calm, 48/237 ms active (39/128 without).
ROT_DPS, ROT_HOLD, ROT_SETTLE = 90.0, 0.25, 0.10
# Expected next posture (the transition prior, optional): opening a fist passes through shapes that read as point or
# victory, and the gate showed them (40 % of fist → open changes on realistic simulated changes). A posture that lies
# between the current one and the expected next one (finger by finger) must hold PATH_HOLD without the fast path. And
# opening means relaxing: while the expected next posture is open, a drop of the EMG power below RELAX_DROP × its level
# over the last RELAX_S counts the in-between evidence toward open and lets open commit at RELAX_P. Fist → open: flashes
# 40 → 0 %, 254 → 212 ms; open → point / victory unchanged; wrong switches while holding 9.0 → 9.4 per minute; a
# genuine fist → point (2–3 % of fist changes) takes longer and usually shows open first.
# With REST in the recogniser, rest is never an in-between flash (it is a posture you hold, and its shape is the in-
# between one), the in-between evidence of a relaxing hand goes to open or rest, whichever leads, and either commits at
# RELAX_P. Changes rebuilt from the clips (recognisers that never saw the arm position, prior on): fist → rest showed
# open first 30 of 30 times (the old rule pooled rest into open) → 7 of 30, rest shown after 268 → 226 ms; fist → open
# shows rest on the way 2 of 60; the other changes and holding unchanged.
PATH_HOLD, PATH_MARGIN = 0.25, 0.15
# Transition flags (transition_events: the EMG burst that starts a posture change, ~60 ms before the fingers move,
# 75 % of changes flagged at ~2 false flags per minute held). Replayed over the six Transitions recordings of
# 2026-10-22 (190 changes the camera saw; the recogniser rebuilt tick by tick with camera teaching; flags from detectors
# that never saw the recording): within FLAG_WIN after a flag a clear leader commits after FLAG_HOLD instead of HOLD_T,
# and with no flag in the last FLAG_WIN a switch needs NOFLAG_HOLD and no fast path. The new posture then shows 197 ms
# after the movement onset instead of 252, with 18.9 wrong switches per minute instead of 21.4 (agreement with the
# camera 83.8 vs 83.9 %); 3 more of the 190 changes take over 1.5 s (no flag). The fast part alone: 163 ms but 28.2
# wrong switches per minute.
FLAG_WIN, FLAG_HOLD, NOFLAG_HOLD = 0.6, 0.05, 0.25
# Soft hand + flag HMM (a Fusion Studio switch): a sticky belief over the postures replaces the gate. Leaving the current
# posture is rare (SOFT_RATE_HOLD per s) except within FLAG_WIN after a transition flag (SOFT_RATE_FLAG per s); the
# recogniser's probabilities ** SOFT_KAPPA are the evidence; the named posture changes when another one's belief passes
# SOFT_ENTER, and out of view the hand is drawn as the belief-weighted mix of the postures' shapes (smoothed SOFT_TAU).
# Replayed over the six Transitions recordings against the camera's own hand: the drawn hand lags the camera by 15 ms
# (snapping after the gate: 175), finger error 0.069 (0.094; holding 0.042 vs 0.048, around changes 0.117 vs 0.174).
# As a gate alone: agreement with the camera 88.4 % (83.9), wrong switches 17.4 per minute (21.4), the new posture
# named 187 ms after the movement onset (252). Without flags the same HMM: 88.3 %, 20.4, 190 ms.
SOFT_RATE_HOLD, SOFT_RATE_FLAG, SOFT_KAPPA, SOFT_ENTER, SOFT_TAU = 0.1, 8.0, 0.35, 0.9, 0.01
# Anticipation (the whiteboard's T1 / T2 / T3 out of S1): for FLAG_WIN after a flag, while the forearm is still, each
# update also pulls the belief toward the usual next postures — TRANSITION_PRIOR's row of the posture named when the
# flag fired, ** SOFT_ANTICIP. The burst comes ~60 ms before the fingers move, so the hand starts moving with them.
# With SOFT_TAU 10 ms (was 30), recogniser taught up front / as the session goes: lag behind the camera's hand 80 → 30
# / 30 → −30 ms, finger error around changes 0.098 → 0.084 / 0.124 → 0.120, flicker outside posture changes 16.0 →
# 18.6 / 19.6 → 24.1 per minute (the old gate: 14.0 / 24.3), while the forearm turns 9.6 → 9.0 / 15.4 → 19.1 (gate
# 10.6 / 19.6). A second, quick belief for the hand during flag windows added flicker and no speed.
SOFT_ANTICIP = 0.3
# Anticipating from YOUR transitions (FlagHMM.prior = the Transitions tracker's matrix: the general prior as 10
# pseudo-transitions + the changes the camera saw in your recordings + those of this session): each recording scored
# with counts from the others, the usual next posture is the one you then make 51 % of the time vs 44 % for the general
# prior (190 changes); lag and finger error unchanged, flicker slightly lower (16.9 vs 18.6 per minute).
# Turning the forearm slides the muscles under the electrodes and the recogniser follows (the user, live: "it flickers
# when I turn my hand"). The gate held the posture while the arm turned; the belief does it too: while the WRIST IMU
# turns faster than TURN_DPS the evidence counts only ** TURN_KAPPA and the belief cannot switch (the knuckle IMU is not
# used: it turns when the hand closes). Clear posture switches per minute outside the camera's posture changes, while
# the forearm turns, recogniser taught as the session goes / up front: a quicker belief tried for the drawn hand 106.9 /
# 56.2, the sticky belief 82.4 / 45.1, damped while turning 15.4 / 9.6 (the gate as before: 19.6 / 10.6); overall
# 19.6 / 16.0 vs the gate's 24.3 / 14.0, with the drawn hand 30 / 80 ms behind the camera's (the gate: 175 / 205).
TURN_DPS, TURN_KAPPA, TURN_RATE = 60.0, 0.05, 0.0
RELAX_DROP, RELAX_S, RELAX_P = 0.6, 0.5, 0.8


def qR(q):
    w, x, y, z = np.asarray(q, float) / (np.linalg.norm(q) + 1e-9)
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                     [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                     [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])


def tilt(wq):
    """The wrist-tilt feature exactly as the Pose Hand regressor uses it (kept identical on purpose)."""
    return qR(wq).T @ np.array([0.0, -1.0, 0.0])


def tdfeat(E, zc_thr=8.0):
    """Hudgins TD features on raw windows (B,8,W) → (B,40); identical to hand_avatar.td_feature_vector."""
    x = np.asarray(E, float); dx = np.diff(x, axis=2)
    mav = np.abs(x).mean(2); rms = np.sqrt((x * x).mean(2)); wl = np.abs(dx).sum(2)
    zc = ((np.sign(x[:, :, :-1]) != np.sign(x[:, :, 1:])) & (np.abs(dx) >= zc_thr)).sum(2).astype(float)
    ssc = (((x[:, :, 1:-1] - x[:, :, :-2]) * (x[:, :, 1:-1] - x[:, :, 2:])) >= zc_thr).sum(2).astype(float)
    return np.stack([mav, rms, wl, zc, ssc], 2).reshape(len(x), -1)


# ── clips ────────────────────────────────────────────────────────────────────────────────────────────────────────
def save_clip(folder, posture, position, buf):
    """buf: dict of lists t, X(40), E(8,64), wq(4), kq(4), acc(3), C, local(26,3). → path."""
    os.makedirs(folder, exist_ok=True)
    fn = os.path.join(folder, f"kp_{posture}_{position}_{time.strftime('%Y%m%d_%H%M%S')}.npz")
    f32 = np.float32
    np.savez_compressed(fn, t=np.asarray(buf["t"], float), X=np.asarray(buf["X"], f32), E=np.asarray(buf["E"], f32),
                        wq=np.asarray(buf["wq"], f32), kq=np.asarray(buf["kq"], f32), acc=np.asarray(buf["acc"], f32),
                        C=np.asarray(buf["C"], f32), local=np.asarray(buf["local"], f32),
                        posture=np.asarray(posture), position=np.asarray(position), version=np.int64(1))
    return fn


def load_clips(folder):
    out = []
    for f in sorted(glob.glob(os.path.join(folder, "kp_*.npz")), key=os.path.getmtime):
        try:
            d = np.load(f, allow_pickle=False)
            out.append({"path": f, "posture": str(d["posture"]), "position": str(d["position"]), "t": np.asarray(d["t"], float),
                        "X": np.asarray(d["X"], float), "E": np.asarray(d["E"], float), "wq": np.asarray(d["wq"], float),
                        "C": np.asarray(d["C"], float), "local": np.asarray(d["local"], float)})
        except Exception:
            continue
    return out


def counts(clips):
    """→ {posture: {position: frames}}, {posture: n_clips}."""
    c = {p: {q: 0 for q in POSITIONS} for p in POSTURES}; n = {p: 0 for p in POSTURES}
    for cl in clips:
        if cl["posture"] in c:
            c[cl["posture"]][cl["position"] if cl["position"] in POSITIONS else "moving"] += len(cl["t"]); n[cl["posture"]] += 1
    return c, n


# ── features / training ──────────────────────────────────────────────────────────────────────────────────────────
def finger_flex(P, flex_max=(130.0, 270.0, 270.0, 270.0, 270.0)):
    chains = ((1, 2, 3, 4, 5), (1, 6, 7, 8, 9, 10), (1, 11, 12, 13, 14, 15), (1, 16, 17, 18, 19, 20), (1, 21, 22, 23, 24, 25))
    out = []
    for ci, ch in enumerate(chains):
        a = 0.0
        for i in range(1, len(ch) - 1):
            v1 = P[ch[i]] - P[ch[i - 1]]; v2 = P[ch[i + 1]] - P[ch[i]]
            c = float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-9))
            a += math.degrees(math.acos(max(-1.0, min(1.0, c))))
        out.append(max(0.0, min(1.0, (a - 15.0) / (flex_max[ci] - 15.0))))
    return np.array(out, float)


def _features(cl, rng, augment=True):
    """One clip → (real F (n,43), synthetic Fa (m,43)) with the Posture Hand augmentation on the RAW windows."""
    X = cl["X"]; T = np.array([tilt(q) for q in cl["wq"]], float)
    ok = np.isfinite(X).all(1) & np.isfinite(T).all(1)
    X, T = X[ok], T[ok]; F = np.hstack([X, T]); Fa = []
    E = cl["E"][ok][:, :, -EMG_WIN:] if (augment and cl["E"].ndim == 3 and cl["E"].shape[2] >= EMG_WIN) else None
    if E is not None and len(E):
        blocks = [(tdfeat(np.roll(E, k, axis=1)), T) for k in (-1, 1)]
        gg = np.exp(rng.uniform(np.log(0.8), np.log(1.25), (len(E), 8)))[:, :, None]
        blocks.append((tdfeat(E * gg), T))
        blocks.append((tdfeat(E * rng.uniform(0.7, 1.45, (len(E), 1, 1))), T))
        blocks.append((tdfeat(E + rng.standard_normal(E.shape) * E.std(2, keepdims=True) * 0.12), T))
        Tj = T.copy()
        for i in range(len(T)):
            ax = rng.standard_normal(3); ax /= np.linalg.norm(ax) + 1e-9; th = math.radians(rng.normal(0, 8))
            K = np.array([[0, -ax[2], ax[1]], [ax[2], 0, -ax[0]], [-ax[1], ax[0], 0]])
            Tj[i] = (np.eye(3) + math.sin(th) * K + (1 - math.cos(th)) * K @ K) @ T[i]
        blocks.append((X.copy(), Tj))
        Fa = [np.hstack([a, b]) for a, b in blocks]
    return F, (np.vstack(Fa) if Fa else np.zeros((0, 43)))


def _fit(F, y, sw, K, iters=600, lr=0.5, l2=1e-3):
    mu = F.mean(0); sd = F.std(0) + 1e-9
    Z = np.hstack([(F - mu) / sd, np.ones((len(F), 1))]); Yh = np.eye(K)[y]
    cnt = np.maximum(np.array([(y == k).sum() for k in range(K)], float), 1.0)
    w = (len(y) / K) / cnt[y] * sw; w = (w / w.mean())[:, None]
    W = np.zeros((Z.shape[1], K))
    for _ in range(iters):
        S = Z @ W; S -= S.max(1, keepdims=True); P = np.exp(S); P /= P.sum(1, keepdims=True)
        W -= lr * (Z.T @ (w * (P - Yh)) / len(Z) + l2 * W)
    return {"W": W, "mu": mu, "sd": sd}


def _assemble(clips, classes, rng, augment=True):
    F, y, sw = [], [], []
    for cl in clips:
        if cl["posture"] not in classes:
            continue
        k = classes.index(cl["posture"]); Fr, Fa = _features(cl, rng, augment)
        F.append(Fr); y += [k] * len(Fr); sw += [1.0] * len(Fr)
        if len(Fa):
            F.append(Fa); y += [k] * len(Fa); sw += [AUG_WEIGHT] * len(Fa)
    return (np.vstack(F) if F else np.zeros((0, 43))), np.asarray(y, int), np.asarray(sw, float)


def train(clips, seed=0):
    """→ (model, report). Held-out check: for every posture with ≥ 2 clips its NEWEST clip is held out; one model is
    trained without all of them and scored per posture (accuracy + confusion); then the final model uses everything."""
    rng = np.random.RandomState(seed)
    classes = sorted({cl["posture"] for cl in clips if cl["posture"] in POSTURES})
    if len(classes) < 2:
        return None, {"error": "record at least two different postures first"}
    K = len(classes)
    held = []
    for c in classes:
        cls_ = [cl for cl in clips if cl["posture"] == c]
        if len(cls_) >= 2:
            held.append(max(cls_, key=lambda cl: cl["t"][0]))
    report = {"classes": classes, "held_out": None}
    if held:
        rest = [cl for cl in clips if cl not in held]
        if len({cl["posture"] for cl in rest}) >= 2:
            F, y, sw = _assemble(rest, classes, rng); m = _fit(F, y, sw, K)
            conf = np.zeros((K, K), int); per = {}; per_pos = {}
            for cl in held:
                Fh, _ = _features(cl, rng, augment=False); k = classes.index(cl["posture"])
                pr = predict(m, Fh, classes=None).argmax(1)
                for p_ in pr:
                    conf[k, p_] += 1
                per[cl["posture"]] = float((pr == k).mean()) if len(pr) else float("nan")
                per_pos[cl["posture"]] = cl["position"]
            report["held_out"] = {"acc_per_posture": per, "position_per_posture": per_pos, "confusion": conf.tolist(),
                                  "acc": float(np.trace(conf) / max(conf.sum(), 1)), "n_frames": int(conf.sum())}
    F, y, sw = _assemble(clips, classes, rng)
    model = _fit(F, y, sw, K); model["classes"] = classes
    tem = np.zeros((K, 5)); src = []; tj = np.zeros((K, 26, 3)); has_j = np.zeros(K, bool)
    for k, c in enumerate(classes):
        tem[k] = np.asarray(TEMPLATES.get(c, (0.3,) * 5), float); s = "anatomical prior"
        if c != "fist":                                              # the camera cannot see a fist's fingers
            curls = []; shapes = []
            for cl in clips:
                if cl["posture"] == c and cl["local"].ndim == 3:
                    for i in np.where(cl["C"] >= 0.9)[0]:
                        L = cl["local"][i]
                        if np.isfinite(L).all() and np.abs(L).sum() > 0:
                            curls.append(finger_flex(L)); shapes.append(L - L[1])
            if len(curls) >= 30:                                     # the camera's own median SHAPE is the template (a pinch's
                tem[k] = np.median(np.asarray(curls), 0)             # thumb really meets the index — curls alone can't say that)
                tj[k] = np.median(np.asarray(shapes), 0); has_j[k] = True; s = f"camera shape ({len(curls)} frames)"
        src.append(s)
    model["templates"] = tem; model["template_src"] = src; model["template_joints"] = tj; model["template_has_joints"] = has_j
    report["n_real"] = int((sw == 1.0).sum()); report["n_syn"] = int((sw < 1.0).sum())
    report["train_acc"] = float((predict(model, F).argmax(1) == y).mean())
    return model, report


def predict(model, F, classes=None):
    Z = np.hstack([(np.atleast_2d(np.asarray(F, float)) - model["mu"]) / model["sd"], np.ones((len(np.atleast_2d(F)), 1))])
    S = Z @ model["W"]; S -= S.max(1, keepdims=True); P = np.exp(S)
    return P / P.sum(1, keepdims=True)


def save(path, model, report=None):
    out = {"W": model["W"].astype(np.float32), "mu": model["mu"].astype(np.float32), "sd": model["sd"].astype(np.float32),
           "classes": np.asarray(model["classes"]), "templates": model["templates"].astype(np.float32),
           "template_src": np.asarray(model["template_src"]), "trained": np.asarray(time.strftime("%Y-%m-%d %H:%M")),
           "template_joints": np.asarray(model.get("template_joints", np.zeros((len(model["classes"]), 26, 3))), np.float32),
           "template_has_joints": np.asarray(model.get("template_has_joints", np.zeros(len(model["classes"]), bool)), bool)}
    if report:
        ho = report.get("held_out") or {}
        out["report_acc"] = np.float32(ho.get("acc", np.nan)); out["report_n"] = np.int64(ho.get("n_frames", 0))
        out["report_per"] = np.asarray([ho.get("acc_per_posture", {}).get(c, np.nan) for c in model["classes"]], np.float32)
        out["report_pos"] = np.asarray([ho.get("position_per_posture", {}).get(c, "") for c in model["classes"]])
        out["report_conf"] = np.asarray(ho.get("confusion", np.zeros((len(model["classes"]),) * 2)), np.int64)
        out["n_real"] = np.int64(report.get("n_real", 0)); out["n_syn"] = np.int64(report.get("n_syn", 0))
    np.savez(path, **out)


def load(path):
    d = np.load(path, allow_pickle=False)
    m = {"W": np.asarray(d["W"], float), "mu": np.asarray(d["mu"], float), "sd": np.asarray(d["sd"], float),
         "classes": [str(c) for c in d["classes"]], "templates": np.asarray(d["templates"], float),
         "template_src": [str(c) for c in d["template_src"]], "trained": str(d["trained"]) if "trained" in d.files else ""}
    for k in ("report_acc", "report_n", "report_per", "report_pos", "report_conf", "n_real", "n_syn"):
        if k in d.files:
            m[k] = d[k]
    m["template_joints"] = np.asarray(d["template_joints"], float) if "template_joints" in d.files else np.zeros((len(m["classes"]), 26, 3))
    m["template_has_joints"] = np.asarray(d["template_has_joints"], bool) if "template_has_joints" in d.files else np.zeros(len(m["classes"]), bool)
    return m


# ── posture transition prior: an HMM forward filter in front of the gate (optional) ──
# Which posture follows which: 314 transitions in Meta's emg2pose mini set (one user, 14 gesture stages, both hands;
# CC-BY-NC-SA 4.0, research use only) and 23 the camera saw in this lab's sessions, +1 each so nothing is forbidden.
# The scripted counting stage is left out: its fist → 1 finger → 2 fingers drill made 'point' look like the usual
# next posture after a fist (all 20 of its fist → point transitions); everywhere else a fist opened 23 times of 24.
# Open is the hub: after a fist the hand opens 88 % of the time, after a pinch 98 %. Rows: from, columns: to.
# REST (added 2026-10-23, once cam_label knew it from the user's rest clips): emg2pose relabelled the same way (counting
# drill left out) + the user's position recordings, 1161 changes, +1 each. Mostly the free-hand stages: a hand at ease
# drifts between open and rest (emg2pose 761 of 1050 changes; the user's relaxed arm-moving sessions 87 of 111). A
# posture's share going to rest is that count; the rest of its row keeps the five-posture proportions above. After rest
# the hand opens 86 %, pinches 10 %, closes 3 %; after a fist it opens 79 %, rests 10 %; after open it rests 72 %.
PRIOR_CLASSES = ("fist", "open", "pinch", "point", "victory", "rest")
TRANSITION_PRIOR = ((0.0, 0.791, 0.055, 0.027, 0.027, 0.1),
                    (0.044, 0.0, 0.225, 0.005, 0.005, 0.721),
                    (0.005, 0.632, 0.0, 0.005, 0.005, 0.353),
                    (0.114, 0.229, 0.114, 0.0, 0.114, 0.429),
                    (0.167, 0.333, 0.167, 0.167, 0.0, 0.166),
                    (0.027, 0.86, 0.104, 0.007, 0.002, 0.0))
HMM_RATE, HMM_KAPPA = 0.6, 0.25        # posture changes per second · weight of one 60-Hz frame's evidence


class PostureHMM:
    """belief ← (belief · A) ⊙ p^κ, renormalised. A keeps the posture (1 − ε) or moves on along the transition prior
    (ε = 1 − e^(−rate·dt)); κ < 1 because consecutive EMG windows overlap, so one frame is weak evidence (scaled with the
    frame rate, so a second of evidence weighs the same at any rate). Classes outside the prior ('other') are reached
    from every posture with OTHER_SHARE and leave to every class alike."""

    OTHER_SHARE = 0.15

    def __init__(self, classes, prior=TRANSITION_PRIOR, prior_classes=PRIOR_CLASSES, rate=HMM_RATE, kappa=HMM_KAPPA):
        self.classes = list(classes)
        K = len(self.classes)
        T = np.full((K, K), 1.0 / max(K - 1, 1))
        np.fill_diagonal(T, 0.0)
        idx = [self.classes.index(c) if c in self.classes else None for c in prior_classes]
        known = [i for i in idx if i is not None]
        others = [k for k in range(K) if k not in known]
        for a, ia in enumerate(idx):
            if ia is None:
                continue
            row = np.zeros(K)
            for b, ib in enumerate(idx):
                if ib is not None:
                    row[ib] = prior[a][b]
            if row.sum() > 0:
                row = row / row.sum() * (1.0 - (self.OTHER_SHARE if others else 0.0))
                for k in others:
                    row[k] = self.OTHER_SHARE / len(others)
                T[ia] = row
        self.T = T
        self.rate, self.kappa = float(rate), float(kappa)
        self.reset()

    def reset(self):
        self.b = np.full(len(self.classes), 1.0 / len(self.classes))

    def update(self, probs, dt):
        dt = max(float(dt), 1e-3)
        eps = 1.0 - math.exp(-self.rate * dt)
        A = (1.0 - eps) * np.eye(len(self.classes)) + eps * self.T
        b = (self.b @ A) * np.power(np.clip(np.asarray(probs, float), 1e-6, 1.0), self.kappa * dt * 60.0)
        s = float(b.sum())
        self.b = b / s if s > 0 else np.full(len(b), 1.0 / len(b))
        return self.b


class PostureGate:
    """Probabilities → a STABLE decision: a posture becomes active after it has led by a clear margin for HOLD_T
    seconds, and stays active until its probability has been below OFF_P for OFF_T seconds. 'other' never snaps."""

    def __init__(self, classes):
        self.classes = list(classes); self.cur = None; self.cand = None; self.cand_t = 0.0; self.low_t = 0.0
        self.turn_since = 1e9; self._rms = []; self._t = 0.0
        self._tpl = {c: np.asarray(TEMPLATES[c], float) for c in self.classes if c in TEMPLATES}

    def _between(self, a, x, b):
        """Posture x lies between a and b finger by finger (its curls within their range, with a margin)."""
        A, X, B = self._tpl.get(a), self._tpl.get(x), self._tpl.get(b)
        if A is None or X is None or B is None:
            return False
        return bool(np.all((X >= np.minimum(A, B) - PATH_MARGIN) & (X <= np.maximum(A, B) + PATH_MARGIN)))

    def update(self, probs, dt, rot_dps=None, prior=None, rms=None, flag_age=None, flag_fast=True, flag_steady=True):
        """rot_dps: how fast the arm/hand IMUs turn (deg/s, smoothed); None = no rotation-aware holding.
        prior: transition rows over PRIOR_CLASSES (TRANSITION_PRIOR) to use the expected next posture; rms: the EMG
        window's power, for the relaxation cue. flag_age: seconds since the last transition flag (math.inf: none yet);
        None = flags not used. flag_fast: commit quickly right after a flag; flag_steady: hold steadier without one."""
        p = np.asarray(probs, float).copy()
        self._t += dt
        if rms is not None:
            self._rms = [x for x in self._rms if self._t - x[0] <= RELAX_S] + [(self._t, float(rms))]
        path_mid = []; expected = None; relax = False
        if prior is not None and self.cur is not None and self.classes[self.cur] in PRIOR_CLASSES:
            a = self.classes[self.cur]; row = prior[PRIOR_CLASSES.index(a)]
            known = [j for j, c in enumerate(PRIOR_CLASSES) if c in self.classes]     # the recogniser's postures only
            exp_name = PRIOR_CLASSES[max(known, key=lambda j: row[j])]
            if exp_name in self.classes:
                expected = self.classes.index(exp_name)
                # rest is a posture you hold (its shape IS the in-between one), never a flash on the way
                path_mid = [k for k, c in enumerate(self.classes) if k not in (self.cur, expected) and c != "rest" and self._between(a, c, exp_name)]
                relax = bool(exp_name == "open" and rms is not None and len(self._rms) >= 10
                             and float(rms) < RELAX_DROP * max(x[1] for x in self._rms))
                if relax and path_mid:                   # opening: the shapes on the way count toward open (or rest)
                    to = expected
                    if "rest" in self.classes and p[self.classes.index("rest")] > p[expected]:
                        to = self.classes.index("rest")
                    p[to] += p[path_mid].sum(); p[path_mid] = 0.0
        self.dbg = (self.classes[expected] if expected is not None else None, [self.classes[k] for k in path_mid], relax)
        top = int(p.argmax()); second = float(np.sort(p)[-2]) if len(p) > 1 else 0.0
        strong = p[top] > SW_TOP and (p[top] - second) > SW_MARGIN
        turning = rot_dps is not None and rot_dps > ROT_DPS
        self.turn_since = 0.0 if turning else self.turn_since + dt
        hold_t = (HOLD_T + (ROT_HOLD - HOLD_T) * min(1.0, (rot_dps - ROT_DPS) / ROT_DPS)) if turning else HOLD_T
        fast_ok = self.turn_since >= ROT_SETTLE
        fast_p = FAST_P
        if top in path_mid:                              # probably just passing through on the way to the expected one
            hold_t = max(hold_t, PATH_HOLD); fast_ok = False
        elif relax and (top == expected or self.classes[top] == "rest"):     # a relaxing hand opens or rests
            fast_p = RELAX_P
        if flag_age is not None:
            if flag_age <= FLAG_WIN:
                if flag_fast:
                    hold_t = min(hold_t, FLAG_HOLD)
            elif flag_steady:
                hold_t = max(hold_t, NOFLAG_HOLD); fast_ok = False
        if self.cur is not None and top == self.cur:
            self.cand = None; self.cand_t = 0.0
            self.low_t = self.low_t + dt if p[self.cur] < OFF_P else 0.0
            if self.low_t >= OFF_T:
                self.cur = None; self.low_t = 0.0
        elif strong:
            if self.cand == top:
                self.cand_t += dt
            else:
                self.cand, self.cand_t = top, dt
            if self.cand_t >= hold_t or (fast_ok and p[top] >= fast_p and self.cand_t >= FAST_T):
                self.cur = top; self.cand = None; self.cand_t = 0.0; self.low_t = 0.0
        else:
            self.cand = None; self.cand_t = 0.0
            if self.cur is not None:
                self.low_t = self.low_t + dt if p[self.cur] < OFF_P else 0.0
                if self.low_t >= OFF_T:
                    self.cur = None; self.low_t = 0.0
        name = self.classes[self.cur] if self.cur is not None else None
        return (name if name != "other" else None), float(p[top]), self.classes[top]

    def reset(self):
        self.cur = None; self.cand = None; self.cand_t = 0.0; self.low_t = 0.0; self.turn_since = 1e9; self._rms = []


class FlagHMM:
    """The soft hand's belief: a sticky forward filter over the recogniser's classes whose switching rate jumps right
    after a transition flag (the supervisor's latch: hold the posture, a flag releases it)."""

    def __init__(self, classes, rate_hold=SOFT_RATE_HOLD, rate_flag=SOFT_RATE_FLAG, kappa=SOFT_KAPPA, enter=SOFT_ENTER,
                 anticip=SOFT_ANTICIP):
        self.classes = list(classes)
        self.rate_hold, self.rate_flag, self.kappa, self.enter, self.anticip = rate_hold, rate_flag, kappa, enter, anticip
        self.reset()

    def reset(self):
        self.b = np.full(len(self.classes), 1.0 / len(self.classes)); self.shown = None
        self._age = None; self._start = None                 # the last flag's age, the posture named when it fired
        if not hasattr(self, "prior"):
            self.prior = TRANSITION_PRIOR                    # rows: from, columns: to, over PRIOR_CLASSES

    def update(self, probs, dt, flag_age=None, turn_dps=None):
        """probs: the recogniser's probabilities; flag_age: seconds since the last transition flag (None / inf: none);
        turn_dps: how fast the forearm (wrist IMU) turns. → (the named posture or None, the belief over self.classes)."""
        K = len(self.classes)
        recent = flag_age is not None and flag_age <= FLAG_WIN
        turning = turn_dps is not None and turn_dps > TURN_DPS
        if recent and (self._age is None or flag_age < self._age - 1e-6):        # a new flag: where the hand starts
            self._start = self.classes[int(np.argmax(self.b))]
        self._age = flag_age
        rate = TURN_RATE if turning else (self.rate_flag if recent else self.rate_hold)
        kappa = TURN_KAPPA if turning else self.kappa
        sw = 1.0 - math.exp(-rate * max(float(dt), 1e-3))
        b = (1.0 - sw) * self.b + sw * (1.0 - self.b) / (K - 1)
        b = b * np.clip(np.asarray(probs, float), 1e-4, 1.0) ** kappa
        if self.anticip > 0 and recent and not turning and self._start in PRIOR_CLASSES:
            pr = self.prior[PRIOR_CLASSES.index(self._start)]
            tot = sum(pr[PRIOR_CLASSES.index(c)] for c in self.classes if c in PRIOR_CLASSES) or 1.0   # over the recogniser's
            row = np.full(K, 1e-3)
            for j, c in enumerate(self.classes):
                if c in PRIOR_CLASSES:
                    row[j] = pr[PRIOR_CLASSES.index(c)] / tot + 1e-3
                elif c != "other":
                    row[j] = 1.0 / len(PRIOR_CLASSES)        # a posture the prior does not know: neutral
            row[self.classes.index(self._start)] = 1e-3
            b = b * (row / row.max()) ** self.anticip
        self.b = b / b.sum()
        top = int(np.argmax(self.b)); name = self.classes[top]
        if name != "other" and (self.shown is None or (name != self.shown and self.b[top] >= self.enter)):
            self.shown = name
        return self.shown, self.b.copy()


def default_model_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "key_postures_model.npz")


def default_model2_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "key_postures_model2.npz")


# ═══════════════════════════════ v2: covariance features + MLP + camera self-teaching ═══════════════════════════════
# Measured on the recordings (Sep 24 = session 1, Sep 25 = session 2): the 43-d linear recogniser separates the five
# postures WITHIN a session (85 % at unseen arm positions) but collapses across sessions (open 2 %, point 30 %): the
# same posture is held with a different contraction on another day and its channel pattern changes. Adding the
# channel-COVARIANCE of the raw windows lifts within-session to 94 %; a 64-unit MLP on those features, adapted with
# frames the CAMERA labels during the session, scores 86 % on held-out session-2 clips including their hidden frames
# (open 82 / pinch 94 / point 82) against 45 % for the shipped recogniser.
FEAT2_DIM = 79                       # 40 TD + 8 log channel std + 28 channel correlations + 3 tilt
FEAT_WIN = 48                        # v2 feature window (samples @ 500 Hz = 96 ms; 64 = 128 ms scored the same, 32 ms slower)
MLP_H, MLP_EPOCHS, MLP_LR = 64, 60, 2e-3
PRIOR_ROWS = 12000                   # class-balanced subset of the training rows kept in the model for live replay
TEACH_DWELL, TEACH_CONF, TEACH_WEIGHT, BUF_PER_CLASS = 0.4, 0.9, 3.0, 1500
TEACH_STEPS = 3                      # Adam mini-batch steps per taught frame (live)
# The tilt input of v2 is R(q)ᵀ[0,−1,0] — a HORIZONTAL axis of the CyberFinger IMU's z-up world, so it follows where the user
# faces and the IMU's heading drift, and each posture was recorded at its own arm angles: the MLP learned arm angle as a
# shortcut (same EMG, another frame's tilt → a different answer on 18 % of frames). Without it: 88.7 → 91.4 % at unseen
# arm positions, wrong switches 12.1 → 9.0 per minute, and with camera teaching on the next day 67–72 → 76–80 % on the
# frames the camera did not see (open 13 → 44–50 %). True-gravity tilt was no better (15 wrong switches per minute). New
# models are trained without it (`tilt` = "none", the 3 tilt columns stay zero); older files keep theirs.
TILT_INPUT = "none"


def model_tilt(model, wq):
    """The tilt columns a v2 model expects: zeros for a tilt-free model, else the wrist tilt."""
    if (model or {}).get("tilt", "wrist") == "none":
        return np.zeros(3)
    return tilt(wq)


def feat_raw(E, T):
    """Raw windows (n,8,W) + tilt (n,3) → (n,79): Hudgins TD, log channel std, channel correlations, tilt."""
    E = np.asarray(E, float); E = E - E.mean(2, keepdims=True); n = len(E)
    C = np.einsum("nct,ndt->ncd", E, E) / E.shape[2]; d = np.sqrt(np.einsum("ncc->nc", C)) + 1e-6
    R = C / (d[:, :, None] * d[:, None, :]); iu = np.triu_indices(8, 1)
    return np.hstack([tdfeat(E), np.log(d), R[:, iu[0], iu[1]], np.asarray(T, float).reshape(n, 3)])


def cam_label(L):
    """Posture name from the camera's 26 local joints (geometry only), None when it is none of the five. On the
    recordings it agrees with the recorded label on 98–100 % of clearly seen frames and refuses 'other'."""
    L = np.asarray(L, float)
    if not np.isfinite(L).all() or np.abs(L).sum() == 0:
        return None
    th, ix, mi, ri, pi = finger_flex(L); d_ti = float(np.linalg.norm(L[5] - L[10]))
    if ix < 0.35 and mi < 0.35 and ri > 0.6 and pi > 0.6:
        return "victory"
    if ix < 0.35 and mi > 0.6 and ri > 0.6 and pi > 0.6:
        return "point"
    if d_ti < 0.035 and mi < 0.5 and ri < 0.5 and pi < 0.5:
        return "pinch"
    if ix > 0.6 and mi > 0.6 and ri > 0.6 and pi > 0.6:
        return "fist"
    # rest: the four fingers loosely and evenly half curled, the thumb off the index. Set on the user's rest clips of
    # 2026-10-23 (curls 0.25–0.38 on average, gap 4–9 cm): 99 % of their seen frames, 0 % of the other postures' clips
    f4 = (ix, mi, ri, pi)
    if sum(f4) / 4 >= 0.22 and max(f4) <= 0.6 and min(f4) >= 0.17 and th <= 0.6 and d_ti >= 0.035:
        return "rest"
    if ix < 0.3 and mi < 0.3 and ri < 0.3 and pi < 0.3 and d_ti > 0.05:
        return "open"
    return None


def _features_raw(cl, rng, augment=True):
    """One clip → (real F (n,79), synthetic Fa (m,79), ok mask) with the same physical augmentation on the raw windows."""
    X = cl["X"]; E_all = cl["E"]
    ok = np.isfinite(X).all(1) & (np.isfinite(E_all).all(axis=(1, 2)) if E_all.ndim == 3 else False)
    T = (np.zeros((len(X), 3)) if TILT_INPUT == "none" else np.array([tilt(q) for q in cl["wq"]], float)); ok &= np.isfinite(T).all(1)
    if not ok.any():
        return np.zeros((0, FEAT2_DIM)), np.zeros((0, FEAT2_DIM)), ok
    E = E_all[ok][:, :, -FEAT_WIN:].astype(float); T = T[ok]; F = feat_raw(E, T); Fa = []
    if augment:
        for k in (-1, 1):
            Fa.append(feat_raw(np.roll(E, k, axis=1), T))
        Fa.append(feat_raw(E * np.exp(rng.uniform(np.log(0.8), np.log(1.25), (len(E), 8)))[:, :, None], T))
        Fa.append(feat_raw(E * rng.uniform(0.6, 1.7, (len(E), 1, 1)), T))
        Fa.append(feat_raw(E + rng.standard_normal(E.shape) * E.std(2, keepdims=True) * 0.12, T))
        Tj = T.copy()
        for i in range(len(T)):
            ax = rng.standard_normal(3); ax /= np.linalg.norm(ax) + 1e-9; th = math.radians(rng.normal(0, 8))
            K = np.array([[0, -ax[2], ax[1]], [ax[2], 0, -ax[0]], [-ax[1], ax[0], 0]])
            Tj[i] = (np.eye(3) + math.sin(th) * K + (1 - math.cos(th)) * K @ K) @ T[i]
        Fa.append(np.hstack([F[:, :-3], Tj]))
    return F, (np.vstack(Fa) if Fa else np.zeros((0, FEAT2_DIM))), ok


def _assemble_raw(clips, classes, rng, augment=True, labeller=None):
    F, y, sw = [], [], []
    for cl in clips:
        if labeller is None and cl["posture"] not in classes:
            continue
        Fr, Fa, ok = _features_raw(cl, rng, augment)
        if not len(Fr):
            continue
        if labeller is None:
            labs = [cl["posture"]] * len(Fr)
        else:
            idx = np.where(ok)[0]
            labs = [(labeller(cl["local"][i]) if float(cl["C"][i]) >= TEACH_CONF else None) for i in idx]
        keep = np.array([j for j, l in enumerate(labs) if l is not None and l in classes], int)
        if not len(keep):
            continue
        ks = [classes.index(labs[j]) for j in keep]
        F.append(Fr[keep]); y += ks; sw += [1.0] * len(keep)
        nb = len(Fa) // len(Fr)
        for b in range(nb):
            F.append(Fa[b * len(Fr):(b + 1) * len(Fr)][keep]); y += ks; sw += [AUG_WEIGHT] * len(keep)
    return (np.vstack(F) if F else np.zeros((0, FEAT2_DIM))), np.asarray(y, int), np.asarray(sw, float)


def _mlp_forward(M, F):
    z = (np.atleast_2d(np.asarray(F, float)) - M["mu"]) / M["sd"]; h = np.maximum(z @ M["W1"] + M["b1"], 0.0)
    s = h @ M["W2"] + M["b2"]; s -= s.max(1, keepdims=True); p = np.exp(s)
    return p / p.sum(1, keepdims=True)


def _adam_step(M, opt, F, y, w, rng, lr, K, drop=0.2, l2=1e-4):
    """One mini-batch step of the MLP in place (Adam). opt: dict of moments + t."""
    z = (F - M["mu"]) / M["sd"]; h = np.maximum(z @ M["W1"] + M["b1"], 0.0)
    mask = (rng.rand(*h.shape) > drop) / (1.0 - drop) if drop > 0 else 1.0; hd = h * mask
    s = hd @ M["W2"] + M["b2"]; s -= s.max(1, keepdims=True); p = np.exp(s); p /= p.sum(1, keepdims=True)
    g = (p - np.eye(K)[y]) * w[:, None] / len(y)
    grads = {"W2": hd.T @ g, "b2": g.sum(0)}; gh = (g @ M["W2"].T) * mask * (h > 0)
    grads["W1"] = z.T @ gh + l2 * M["W1"]; grads["b1"] = gh.sum(0)
    opt["t"] += 1; t = opt["t"]
    for k, gr in grads.items():
        opt["m"][k] = 0.9 * opt["m"][k] + 0.1 * gr; opt["v"][k] = 0.999 * opt["v"][k] + 0.001 * gr * gr
        M[k] -= lr * (opt["m"][k] / (1 - 0.9 ** t)) / (np.sqrt(opt["v"][k] / (1 - 0.999 ** t)) + 1e-8)


def _new_opt(M):
    return {"t": 0, "m": {k: np.zeros_like(M[k]) for k in ("W1", "b1", "W2", "b2")},
            "v": {k: np.zeros_like(M[k]) for k in ("W1", "b1", "W2", "b2")}}


def _fit_mlp(F, y, sw, K, H=MLP_H, epochs=MLP_EPOCHS, seed=0):
    rng = np.random.RandomState(seed); mu = F.mean(0); sd = F.std(0) + 1e-9; n, d = F.shape
    cnt = np.maximum(np.bincount(y, minlength=K), 1); w = (n / K) / cnt[y] * sw; w = w / w.mean()
    M = {"mu": mu, "sd": sd, "W1": rng.standard_normal((d, H)) * np.sqrt(2.0 / d), "b1": np.zeros(H),
         "W2": np.zeros((H, K)), "b2": np.zeros(K)}
    opt = _new_opt(M); bs = 512
    for _ in range(epochs):
        perm = rng.permutation(n)
        for i in range(0, n, bs):
            b = perm[i:i + bs]; _adam_step(M, opt, F[b], y[b], w[b], rng, MLP_LR, K)
    return M


def train2(clips, seed=0, log=None):
    """v2 recogniser → (model, report): same held-out protocol as train() (newest clip per posture held out), then the
    final MLP on everything + a class-balanced subset of its training rows for live replay (`prior_F`, `prior_y`)."""
    rng = np.random.RandomState(seed)
    clips = [cl for cl in clips if cl["posture"] in POSTURES and cl.get("E") is not None and cl["E"].ndim == 3]
    classes = sorted({cl["posture"] for cl in clips})
    if len(classes) < 2:
        return None, {"error": "record at least two different postures first"}
    K = len(classes); held = []
    for c in classes:
        cls_ = [cl for cl in clips if cl["posture"] == c]
        if len(cls_) >= 2:
            held.append(max(cls_, key=lambda cl: cl["t"][0]))
    report = {"classes": classes, "held_out": None, "kind": "mlp"}
    if held:
        rest = [cl for cl in clips if cl not in held]
        if len({cl["posture"] for cl in rest}) >= 2:
            if log: log("held-out check…")
            F, y, sw = _assemble_raw(rest, classes, rng); m = _fit_mlp(F, y, sw, K, seed=seed)
            conf = np.zeros((K, K), int); per = {}; per_pos = {}
            for cl in held:
                Fh, _, _ = _features_raw(cl, rng, augment=False); k = classes.index(cl["posture"])
                pr = _mlp_forward(m, Fh).argmax(1) if len(Fh) else np.zeros(0, int)
                for p_ in pr:
                    conf[k, p_] += 1
                per[cl["posture"]] = float((pr == k).mean()) if len(pr) else float("nan"); per_pos[cl["posture"]] = cl["position"]
            report["held_out"] = {"acc_per_posture": per, "position_per_posture": per_pos, "confusion": conf.tolist(),
                                  "acc": float(np.trace(conf) / max(conf.sum(), 1)), "n_frames": int(conf.sum())}
    if log: log("final model…")
    F, y, sw = _assemble_raw(clips, classes, rng)
    model = _fit_mlp(F, y, sw, K, seed=seed); model["classes"] = classes; model["kind"] = "mlp"; model["win"] = int(FEAT_WIN)
    model["tilt"] = TILT_INPUT
    # class-balanced replay subset
    per_c = max(PRIOR_ROWS // K, 1); sel = []
    for k in range(K):
        idx = np.where(y == k)[0]
        sel.append(idx if len(idx) <= per_c else rng.choice(idx, per_c, replace=False))
    sel = np.concatenate(sel); model["prior_F"] = F[sel].astype(np.float32); model["prior_y"] = y[sel].astype(np.int64)
    # templates: identical rule to train()
    tem = np.zeros((K, 5)); src = []; tj = np.zeros((K, 26, 3)); has_j = np.zeros(K, bool)
    for k, c in enumerate(classes):
        tem[k] = np.asarray(TEMPLATES.get(c, (0.3,) * 5), float); s_ = "anatomical prior"
        if c != "fist":
            curls = []; shapes = []
            for cl in clips:
                if cl["posture"] == c and cl["local"].ndim == 3:
                    for i in np.where(cl["C"] >= 0.9)[0]:
                        L = cl["local"][i]
                        if np.isfinite(L).all() and np.abs(L).sum() > 0:
                            curls.append(finger_flex(L)); shapes.append(L - L[1])
            if len(curls) >= 30:
                tem[k] = np.median(np.asarray(curls), 0); tj[k] = np.median(np.asarray(shapes), 0); has_j[k] = True
                s_ = f"camera shape ({len(curls)} frames)"
        src.append(s_)
    model["templates"] = tem; model["template_src"] = src; model["template_joints"] = tj; model["template_has_joints"] = has_j
    report["n_real"] = int((sw == 1.0).sum()); report["n_syn"] = int((sw < 1.0).sum())
    report["train_acc"] = float((_mlp_forward(model, F).argmax(1) == y).mean())
    return model, report


def predict2(model, F):
    """Probabilities for v1 (linear, 43-d) or v2 (MLP, 79-d) models."""
    return _mlp_forward(model, F) if model.get("kind") == "mlp" else predict(model, F)


def save2(path, model, report=None):
    out = {"kind": np.asarray("mlp"), "win": np.int64(model.get("win", FEAT_WIN)), "mu": model["mu"].astype(np.float32), "sd": model["sd"].astype(np.float32),
           "W1": model["W1"].astype(np.float32), "b1": model["b1"].astype(np.float32),
           "W2": model["W2"].astype(np.float32), "b2": model["b2"].astype(np.float32),
           "prior_F": model["prior_F"].astype(np.float32), "prior_y": model["prior_y"].astype(np.int64),
           "classes": np.asarray(model["classes"]), "templates": model["templates"].astype(np.float32),
           "template_src": np.asarray(model["template_src"]), "trained": np.asarray(time.strftime("%Y-%m-%d %H:%M")),
           "template_joints": np.asarray(model["template_joints"], np.float32), "template_has_joints": np.asarray(model["template_has_joints"], bool),
           "tilt": np.asarray(model.get("tilt", "wrist"))}
    if report:
        ho = report.get("held_out") or {}
        out["report_acc"] = np.float32(ho.get("acc", np.nan)); out["report_n"] = np.int64(ho.get("n_frames", 0))
        out["report_per"] = np.asarray([ho.get("acc_per_posture", {}).get(c, np.nan) for c in model["classes"]], np.float32)
        out["report_pos"] = np.asarray([ho.get("position_per_posture", {}).get(c, "") for c in model["classes"]])
        out["report_conf"] = np.asarray(ho.get("confusion", np.zeros((len(model["classes"]),) * 2)), np.int64)
        out["n_real"] = np.int64(report.get("n_real", 0)); out["n_syn"] = np.int64(report.get("n_syn", 0))
    np.savez_compressed(path, **out)


def load2(path):
    d = np.load(path, allow_pickle=False)
    m = {k: np.asarray(d[k], float) for k in ("mu", "sd", "W1", "b1", "W2", "b2")}
    m.update({"kind": "mlp", "win": int(d["win"]) if "win" in d.files else EMG_WIN, "prior_F": np.asarray(d["prior_F"], float), "prior_y": np.asarray(d["prior_y"], int),
              "classes": [str(c) for c in d["classes"]], "templates": np.asarray(d["templates"], float),
              "template_src": [str(c) for c in d["template_src"]], "trained": str(d["trained"]) if "trained" in d.files else "",
              "template_joints": np.asarray(d["template_joints"], float), "template_has_joints": np.asarray(d["template_has_joints"], bool),
              "tilt": str(d["tilt"]) if "tilt" in d.files else "wrist"})
    for k in ("report_acc", "report_n", "report_per", "report_pos", "report_conf", "n_real", "n_syn"):
        if k in d.files:
            m[k] = d[k]
    return m


class OnlineTeacher:
    """Camera self-teaching of a v2 model during the session. observe() collects frames whose posture the camera
    labelled (clear view, same label for TEACH_DWELL s); step() takes one Adam mini-batch step on a copy of the model
    with half prior rows (the model's replay subset) and half session rows (weight TEACH_WEIGHT). The live model is
    `self.M`; the prior stays untouched (reset() returns to it)."""

    def __init__(self, model, seed=0):
        self.prior = model; self.rng = np.random.RandomState(seed); self.reset()

    def reset(self):
        P = self.prior; self.M = dict(P)
        for k in ("mu", "sd", "W1", "b1", "W2", "b2"):                          # only the weights are copied (trained)
            self.M[k] = np.array(P[k], float, copy=True)
        self.opt = _new_opt(self.M); self.K = len(P["classes"])
        self.buf = [[] for _ in range(self.K)]; self.n_taught = np.zeros(self.K, int); self.steps = 0
        self._last = None; self._last_t = 0.0; self._dwell_ok = False

    def observe(self, F, label, t):
        """F (79,) live features; label from cam_label() or None; t = time. → True when the frame was kept."""
        if label is None or label not in self.M["classes"]:
            self._last = None; self._dwell_ok = False; return False
        if label != self._last:
            self._last, self._last_t, self._dwell_ok = label, t, False; return False
        if not self._dwell_ok and t - self._last_t < TEACH_DWELL:
            return False
        self._dwell_ok = True; k = self.M["classes"].index(label); b = self.buf[k]
        b.append(np.asarray(F, float)); self.n_taught[k] += 1
        if len(b) > BUF_PER_CLASS:
            del b[0]
        return True

    @property
    def n_session(self):
        return int(sum(len(b) for b in self.buf))

    def step(self, n_steps=1, bs=32):
        if self.n_session < 30:
            return False
        P = self.prior; have = [k for k in range(self.K) if self.buf[k]]
        for _ in range(n_steps):
            ip = self.rng.randint(0, len(P["prior_F"]), bs); Fp = P["prior_F"][ip]; yp = P["prior_y"][ip]
            ks = self.rng.choice(have, bs); Fs = np.array([self.buf[k][self.rng.randint(len(self.buf[k]))] for k in ks])
            F = np.vstack([Fp, Fs]); y = np.concatenate([yp, ks]); w = np.concatenate([np.ones(bs), np.full(bs, TEACH_WEIGHT)])
            _adam_step(self.M, self.opt, F, y, w / w.mean(), self.rng, MLP_LR, self.K, drop=0.0)
        self.steps += n_steps
        return True

    def status(self):
        parts = [f"{c} {int(n)}" for c, n in zip(self.M["classes"], self.n_taught) if n]
        return ("taught today: " + " · ".join(parts) if parts else "nothing taught yet") + (f" · {self.steps} steps" if self.steps else "")


def default_data_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "key_postures")


# ── self-test on synthetic data ──────────────────────────────────────────────────────────────────────────────────
def _selftest():
    rng = np.random.RandomState(0); clips = []
    for c in ("open", "fist", "point", "pinch", "victory"):
        base = rng.uniform(20, 60, 8) * (1 + 0.6 * rng.rand(8))
        for rep in range(3):
            n = 200; E = rng.standard_normal((n, 8, 64)) * base[None, :, None] * (1 + 0.1 * rep)
            X = tdfeat(E); wq = np.tile([0.7, 0.1, 0.7, 0.05], (n, 1)) + 0.02 * rng.standard_normal((n, 4))
            clips.append({"path": f"{c}{rep}", "posture": c, "position": POSITIONS[rep % 3], "t": np.arange(n) / 30.0 + rep * 1000,
                          "X": X, "E": E, "wq": wq, "C": np.zeros(n), "local": np.zeros((n, 26, 3))})
    m, rep = train(clips)
    assert rep["held_out"]["acc"] > 0.95, rep
    g = PostureGate(m["classes"]); F, _ = _features(clips[3], rng, False)
    seq = [g.update(predict(m, F[i:i + 1])[0], 1 / 30.0)[0] for i in range(60)]
    assert seq[0] is None and seq[-1] == "fist", seq[:8]
    seq2 = [g.update(np.full(len(m["classes"]), 1.0 / len(m["classes"])), 1 / 30.0)[0] for _ in range(20)]
    assert seq2[-1] is None and seq2[0] == "fist", seq2
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_kp_selftest.npz"); save(p, m, rep); m2 = load(p); os.remove(p)
    assert m2["classes"] == m["classes"] and np.allclose(m2["W"], m["W"], atol=1e-6)
    return rep["held_out"]["acc"], rep["train_acc"]


if __name__ == "__main__":
    a, b = _selftest()
    print(f"key_postures self-test OK — synthetic held-out accuracy {a * 100:.0f} %, train {b * 100:.0f} %; gate + save/load pass")
