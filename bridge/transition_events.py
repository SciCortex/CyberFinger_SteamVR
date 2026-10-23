"""EMG transition events: a posture change starts with a burst in the EMG, before the fingers move.

The burst is a CHANGE of the muscle pattern, measured causally: each channel's log envelope (mean |emg| over the last
ENV_S) minus its own mean over BASE_S (300 .. 30 ms ago), RMS over the channels. A flag is raised when that score
crosses the threshold. Your first recording (2026-10-22, 89 s, 36 transitions the camera saw): every transition has a
burst, peaking at the movement onset (median peak 1.1, holds' 99th percentile 0.87, 25 ms envelope); with the 50 ms
envelope and threshold 0.9, 20 of 36 are flagged at ≤ 2 false flags per minute held, ~100 ms before the fingers move as
the camera sees it, ~200 ms before the camera names the new posture and ~300 ms before the EMG gate switches (the 25 ms
envelope at 1.2 caught 12). The quiet ones are pinch ↔ open and victory → point (peaks 0.7–0.9). Measured offline:
  emg2pose (16-ch wrist EMG at 2 kHz + motion capture, 249 transitions): the score peaks at the movement onset; at
  threshold 1.0 a flag catches 94 % of the transitions, median 55 ms BEFORE the fingers start to move and ~275 ms before
  the new posture can be read from the hand shape. The first 20 ms of the burst already tell where the hand is going
  better than the transition prior alone (next posture right 66 % vs 54 %); longer windows add nothing.
  your MindRove holds (42 key-posture clips of Sep 24, 8 min, through this module, 50 ms envelope): with the arm
  still, flags with no posture change come 12.8 / 5.6 / 2.2 per minute at threshold 0.8 / 0.9 / 1.0; with the arm
  moving, most come while the wrist turns > 60 °/s, which the CyberFinger wrist IMU vetoes.
A flag tells WHEN something changes; the transition prior and the burst's channel pattern tell WHERE to. Whether it
was a real posture change is settled afterwards by what the EMG gate and the camera showed (TransitionTracker).

A single threshold on the score tops out at ~2 transitions in 3: pinch ↔ open barely changes the muscle pattern. The
LEARNED detector (LearnedDetector, train_detector) reads the per-channel changes of a 50 ms and a 25 ms envelope and how
they moved over the last 40 ms, and is trained from the Transitions recordings, the camera labelling every transition.
On the first three recordings (122 transitions), each tested by a model trained on the other two with the threshold
chosen there: 76 % caught at 2.5 false flags/min held vs 63 % at 1.3 for the score threshold, both ~60 ms before the
fingers move; adding the knuckle IMU made flags later and did not help."""

import collections
import json
import math
import os

import numpy as np

ENV_S = 0.050            # envelope: mean |emg| over the newest 50 ms (25 ms: 13 of 36 flagged at the same false rate)
SHORT_ENV_S = 0.025      # a second, faster envelope: one of the learned detector's inputs
LAG_STEPS = 4            # the learned detector also sees how the change moved over the last 40 ms
BASE_S = (0.300, 0.030)  # baseline: the mean log envelope 300 .. 30 ms ago
STEP_S = 0.010           # one score every 10 ms
THRESHOLD = 0.9          # flag when the score crosses this (your recording: ≤ 2 false flags/min held; Sep 24 holds 5.6)
REFRACTORY_S = 0.4       # one flag per burst
VETO_DPS = 60.0          # the forearm turning faster than this changes the EMG by itself: no flag. The WRIST IMU only:
                         # the knuckle IMU turns 60–105 °/s when the hand closes (4 of 5 flags it vetoed were real)
RESOLVE_S = 1.0          # a posture change this long after a flag counts as the flag's (the EMG gate needs ~0.2–0.7 s)
CAM_STABLE_S = 0.15      # a camera label counts once it has held this long
HIST_S = 12.0            # history kept for the dashboard
LOCK_S = (-0.3, 0.6)     # score kept around each flag (the "peak" picture)
FEAT_S = 0.020           # burst pattern = the mean change over the first 20 ms after the flag
PRIOR_CLASSES = ("fist", "open", "pinch", "point", "victory", "rest")   # = key_postures.PRIOR_CLASSES
PRIOR_WEIGHT = 10.0      # the general transition prior counts as this many transitions; yours are added to it


def turn_rate(hist, t, q, span=0.2):
    """deg/s of an orientation stream over the last `span` s; hist is a deque of (t, unit quaternion w,x,y,z)."""
    if q is None:
        return None
    q = np.asarray(q, float)
    if q.shape != (4,) or not np.isfinite(q).all() or np.abs(q).sum() == 0:
        return None
    q = q / np.linalg.norm(q)
    hist.append((t, q))
    while len(hist) > 2 and hist[1][0] <= t - span:
        hist.popleft()
    t0, q0 = hist[0]
    if t - t0 < 1e-3:
        return 0.0
    d = min(1.0, abs(float(q0 @ q)))
    return math.degrees(2.0 * math.acos(d)) / (t - t0)


class ChangeScore:
    """Causal EMG change score from a stream of new samples (n_ch, n), every STEP_S."""

    def __init__(self, fs, n_ch):
        self.fs = float(fs)
        self.n_ch = int(n_ch)
        self.env_n = max(2, int(round(ENV_S * fs)))
        self.short_n = max(2, int(round(SHORT_ENV_S * fs)))
        self.buf_n = max(self.env_n, self.short_n)
        self.step = max(1, int(round(STEP_S * fs)))
        self.abs = np.zeros((self.n_ch, self.buf_n))
        self.filled = 0
        self.since = 0
        self.ref = None; self.ref_s = None
        self.b0 = int(round(BASE_S[0] / STEP_S)); self.b1 = int(round(BASE_S[1] / STEP_S))
        self.hist = collections.deque(maxlen=self.b0 + 1)   # log envelopes of the last b0 steps + this one
        self.hist_s = collections.deque(maxlen=self.b0 + 1)

    def push(self, x, t_end):
        """New samples → list of (t, score, D, Ds) for every step completed; D, Ds = per-channel change of the 50 ms
        and the 25 ms envelope."""
        x = np.asarray(x, float)
        if x.ndim != 2 or x.shape[0] != self.n_ch or x.shape[1] == 0:
            return []
        out = []
        n = x.shape[1]
        i = 0
        while i < n:
            k = min(n - i, self.step - self.since)
            a = np.abs(x[:, i:i + k])
            if k >= self.buf_n:
                self.abs = a[:, -self.buf_n:].copy()
            else:
                self.abs = np.concatenate([self.abs[:, k:], a], 1)
            self.filled = min(self.buf_n, self.filled + k)
            i += k
            self.since += k
            if self.since < self.step:
                continue
            self.since = 0
            t = t_end - (n - i) / self.fs
            if self.filled < self.buf_n:
                continue
            env = self.abs[:, -self.env_n:].mean(1); env_s = self.abs[:, -self.short_n:].mean(1)
            self.ref = env.copy() if self.ref is None else self.ref + 0.002 * (env - self.ref)   # ~5 s level
            self.ref_s = env_s.copy() if self.ref_s is None else self.ref_s + 0.002 * (env_s - self.ref_s)
            L = np.log(env + 0.05 * np.maximum(self.ref, 1e-9))
            Ls = np.log(env_s + 0.05 * np.maximum(self.ref_s, 1e-9))
            self.hist.append(L); self.hist_s.append(Ls)
            if len(self.hist) < self.b1 + 3:
                continue
            D = L - np.mean(list(self.hist)[:-self.b1], 0)      # steps b0 .. b1 back (counted, not timed: exact)
            Ds = Ls - np.mean(list(self.hist_s)[:-self.b1], 0)
            out.append((t, float(np.sqrt(np.mean(D ** 2))), D, Ds))
        return out


def features(D, Ds, D_lag):
    """The learned detector's input at one step: the per-channel change of both envelopes, how the 50 ms one moved over
    the last LAG_STEPS steps, and the burst score with the largest rise and fall."""
    D = np.asarray(D, float)
    return np.concatenate([D, np.asarray(Ds, float), D - np.asarray(D_lag, float), [np.sqrt(np.mean(D ** 2)), D.max(), D.min()]])


class LearnedDetector:
    """P(a posture change is starting) every 10 ms — logistic regression on features(), trained from Transitions
    recordings by train_detector(); flags when it crosses `threshold` (chosen for ≤ 2 false flags per minute held)."""

    def __init__(self, mu, sd, w, b, threshold, info=None):
        self.mu = np.asarray(mu, float); self.sd = np.asarray(sd, float); self.w = np.asarray(w, float)
        self.b = float(b); self.threshold = float(threshold); self.info = info or {}

    def p(self, f):
        return float(1.0 / (1.0 + math.exp(-float(((np.asarray(f, float) - self.mu) / self.sd) @ self.w + self.b))))

    def p_many(self, F):
        return 1.0 / (1.0 + np.exp(-(((np.asarray(F, float) - self.mu) / self.sd) @ self.w + self.b)))

    def save(self, path):
        np.savez(path, mu=self.mu, sd=self.sd, w=self.w, b=self.b, threshold=self.threshold, info=json.dumps(self.info), version=1)

    @staticmethod
    def load(path):
        d = np.load(path, allow_pickle=False)
        return LearnedDetector(d["mu"], d["sd"], d["w"], float(d["b"]), float(d["threshold"]), json.loads(str(d["info"])))


def default_model_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "transition_model.npz")


def default_log_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "transition_logs")


class DirectionModel:
    """Where a flagged burst leads: transition prior × nearest centroid of the burst patterns seen before for the same
    (from → to) — the whiteboard's T1, T2, T3 out of S1. Learns online from resolved flags."""

    def __init__(self, prior=None, classes=PRIOR_CLASSES):
        self.classes = tuple(classes)
        self.prior = None if prior is None else np.asarray(prior, float)
        self.samples = collections.defaultdict(list)    # from → [(pattern, to)]
        self.counts = collections.defaultdict(lambda: np.zeros(len(self.classes)))   # from → your transitions this session
        self.base_counts = np.zeros((len(self.classes), len(self.classes)))         # yours from the recordings

    def set_base_counts(self, counts, classes):
        """Your transitions counted in the Transitions recordings (rows from, columns to, in `classes` order)."""
        M = np.zeros((len(self.classes), len(self.classes)))
        C = np.asarray(counts, float); cl = list(classes)
        for i, a in enumerate(self.classes):
            for j, b in enumerate(self.classes):
                if a in cl and b in cl:
                    M[i, j] = C[cl.index(a), cl.index(b)]
        self.base_counts = M

    def prior_row(self, a, known=None):
        """P(next | a): the general prior (PRIOR_WEIGHT pseudo-transitions) updated with your transitions — those
        counted in the recordings and those made this session. known: the postures the recogniser has (the general
        prior is spread over those only, so a recogniser without rest anticipates exactly as before rest existed)."""
        k = len(self.classes)
        if a not in self.classes:
            return np.full(k, 1.0 / k)
        base = np.full(k, 1.0 / k) if self.prior is None else self.prior[self.classes.index(a)].copy()
        m = np.array([known is None or c in known for c in self.classes], float); m[self.classes.index(a)] = 0.0
        base = (base * m / ((base * m).sum() or 1.0) + 1e-3) * m
        row = PRIOR_WEIGHT * base / base.sum() + self.base_counts[self.classes.index(a)] + self.counts[a]
        row[self.classes.index(a)] = 0.0
        return row / row.sum()

    def matrix(self, known=None):
        """P(next | from) for every posture, rows and columns in self.classes order."""
        return np.array([self.prior_row(a, known) for a in self.classes])

    def predict(self, a, f):
        """→ (prior-only probabilities, prior × burst probabilities) over self.classes."""
        p0 = self.prior_row(a)
        S = self.samples.get(a) or []
        if f is None or len(S) < 3:
            return p0, p0.copy()
        X = np.array([s[0] for s in S]); y = [s[1] for s in S]
        mu, sd = X.mean(0), X.std(0) + 1e-6
        z = (np.asarray(f, float) - mu) / sd
        lp = np.log(p0 + 1e-9)
        for j, c in enumerate(self.classes):
            m = [i for i, b in enumerate(y) if b == c]
            if m:
                lp[j] += -2.0 * float(np.mean((z - ((X[m] - mu) / sd).mean(0)) ** 2))
            elif c != a:
                lp[j] += -2.0                       # a destination never seen with a burst from here
        if a in self.classes:
            lp[self.classes.index(a)] = -np.inf             # a flag out of a posture leads somewhere else
        p = np.exp(lp - lp.max())
        return p0, p / p.sum()

    def learn(self, a, f, b):
        if a in self.classes and b in self.classes and a != b:
            self.counts[a][self.classes.index(b)] += 1.0
            if f is not None:
                self.samples[a].append((np.asarray(f, float), b))
                if len(self.samples[a]) > 400:
                    self.samples[a].pop(0)


class TransitionTracker:
    """Flags from the EMG change score + what followed each one (the posture the EMG gate and the camera showed next,
    and how much earlier the flag came), misses (posture changes with no flag before them), the burst patterns per
    transition and running statistics — everything the Transitions dashboard draws."""

    def __init__(self, fs, n_ch, prior=None, classes=PRIOR_CLASSES):
        self.cs = ChangeScore(fs, n_ch)
        self.dirm = DirectionModel(prior, classes)
        self.threshold = THRESHOLD
        self.veto_on = True
        self.det = None                               # a LearnedDetector, used instead of the score threshold when on
        self.use_det = False
        self.reset_stats()

    def set_detector(self, det, on=True):
        self.det = det
        self.use_det = bool(on and det is not None)
        self._above = False
        info = (det.info if det is not None else {}) or {}
        if "counts" in info:                          # your transitions from the recordings the detector was trained on
            self.dirm.set_base_counts(info["counts"], info.get("count_classes", PRIOR_CLASSES))

    def active(self):
        """(what flags: "learned" or "score", its threshold, the top of its scale for drawing)."""
        if self.use_det and self.det is not None:
            return "learned", self.det.threshold, 1.0
        return "score", self.threshold, max(3.0, self.threshold * 1.6)

    def reset_stats(self):
        self.hist = collections.deque()             # (t, flag signal, D, posture, cam, rot, burst score)
        self._lag = collections.deque(maxlen=LAG_STEPS + 1)
        self.events = []
        self.misses = []
        self._last_flag = -1e9
        self._last_go = -1e9                        # the last flag that was not vetoed
        self._above = False
        self._rot_w = collections.deque(); self._rot_k = collections.deque()
        self._posture = None; self._posture_t = None
        self._cam = None; self._cam_cand = (None, None); self._cam_t = None; self._cam_seen = False
        self.t0 = None
        self.hold_s = 0.0
        self._last_t = None

    # ── input ──
    def update(self, x_new, t, posture=None, cam=None, wq=None, kq=None):
        """x_new: the EMG samples that arrived since the last call (n_ch, n); t: wall time of the newest one; posture:
        the EMG gate's posture now (or None); cam: the camera's posture label now (or None when it can't tell)."""
        if self.t0 is None:
            self.t0 = t
        rot = turn_rate(self._rot_w, t, wq) or 0.0
        turn_rate(self._rot_k, t, kq)                       # kept for the record, not for the veto
        self._track_postures(t, posture, cam)
        for (ts, sc, D, Ds) in self.cs.push(x_new, t):
            self._lag.append(D)
            if self.use_det and self.det is not None:
                sig = self.det.p(features(D, Ds, self._lag[0] if len(self._lag) == self._lag.maxlen else np.zeros_like(D)))
                thr = self.det.threshold
            else:
                sig, thr = sc, self.threshold
            self.hist.append((ts, sig, D, self._posture, self._cam, rot, sc))
            crossing = sig > thr and not self._above
            self._above = sig > thr
            if crossing and ts - self._last_flag >= REFRACTORY_S:
                self._last_flag = ts
                vetoed = self.veto_on and rot > VETO_DPS
                if not vetoed:
                    self._last_go = ts
                a = self._cam if (self._cam_seen and self._cam is not None) else self._posture   # the camera knows better
                self.events.append({"t": ts, "from": a, "score": sig, "peak": sig, "D": D.copy(), "f": None,
                                    "vetoed": vetoed, "rot": rot, "p_prior": None, "p_burst": None, "pred": None,
                                    "gate_to": None, "gate_dt": None, "cam_to": None, "cam_dt": None, "to": None,
                                    "outcome": "vetoed" if vetoed else "pending", "lock": None})
        while self.hist and self.hist[0][0] < t - HIST_S:
            self.hist.popleft()
        if self._last_t is not None and self._posture is not None:
            self._count_hold(t)
        self._last_t = t
        self._finish(t)

    def _count_hold(self, t):
        """Time spent holding a posture (no flag or change in the last second): the base for false flags per minute."""
        quiet = (t - self._last_flag > RESOLVE_S) and (self._posture_t is None or t - self._posture_t > RESOLVE_S)
        if quiet:
            self.hold_s += min(0.1, t - self._last_t)

    def _track_postures(self, t, posture, cam):
        self._cam_seen = cam is not None
        if posture is not None and posture != self._posture:
            if self._posture is not None:
                self._on_change("gate", self._posture, posture, t)
            self._posture = posture; self._posture_t = t
        if cam != self._cam_cand[0]:
            self._cam_cand = (cam, t)
        elif cam is not None and cam != self._cam and t - self._cam_cand[1] >= CAM_STABLE_S:
            if self._cam is not None:
                self._on_change("cam", self._cam, cam, self._cam_cand[1])
            self._cam = cam; self._cam_t = self._cam_cand[1]

    def _on_change(self, who, a, b, t):
        """A posture change seen by the EMG gate or the camera: give it to the most recent flag before it, or a miss."""
        cand = [e for e in self.events if not e["vetoed"] and 0.0 <= t - e["t"] <= RESOLVE_S and e[who + "_to"] is None]
        if cand:
            e = cand[-1]
            e[who + "_to"] = b; e[who + "_dt"] = t - e["t"]
        else:
            self.misses.append({"t": t, "who": who, "from": a, "to": b})

    def _finish(self, t):
        recent = [e for e in self.events[-20:] if t - e["t"] <= RESOLVE_S + 0.2]
        if not recent:
            return
        hs = [h for h in self.hist if h[0] >= recent[0]["t"] + LOCK_S[0] - 0.05]
        for e in recent:
            if e["f"] is None and t - e["t"] >= FEAT_S:
                seg = [h[2] for h in hs if e["t"] <= h[0] <= e["t"] + FEAT_S]
                if seg:
                    e["f"] = np.mean(seg, 0)
                    if e["from"] is not None:
                        e["p_prior"], e["p_burst"] = self.dirm.predict(e["from"], e["f"])
                        e["pred"] = self.dirm.classes[int(np.argmax(e["p_burst"]))]
            if t - e["t"] <= 0.12:
                e["peak"] = max(e["peak"], max([h[1] for h in hs if e["t"] <= h[0] <= e["t"] + 0.12], default=e["peak"]))
            if e["lock"] is None and t - e["t"] >= LOCK_S[1]:
                tt = np.array([h[0] for h in hs]); sc = np.array([h[1] for h in hs])
                if len(tt) > 2 and tt[0] <= e["t"] + LOCK_S[0] + 0.03:
                    e["lock"] = np.interp(e["t"] + np.arange(LOCK_S[0], LOCK_S[1] + 1e-9, 0.02), tt, sc)
            if e["outcome"] == "pending" and t - e["t"] >= RESOLVE_S:
                # where the hand SETTLED, not the gate's first switch (it often passes an in-between posture first:
                # on a replayed session the first switch was the destination only 24 times in 35)
                cam = self._cam if (self._cam_seen and self._cam is not None) else e["cam_to"]
                gate = self._posture if self._posture is not None else e["gate_to"]
                to = [x for x in (cam, gate, e["cam_to"], e["gate_to"]) if x is not None and x != e["from"]]
                e["to"] = to[0] if to else None                  # never "back where it started": that is no change
                e["outcome"] = "change" if e["to"] is not None else "no change"
                if e["to"] is not None and e["from"] is not None:
                    self.dirm.learn(e["from"], e["f"], e["to"])
        if len(self.events) > 500:
            self.events = self.events[-500:]
        if len(self.misses) > 500:
            self.misses = self.misses[-500:]

    # ── read-outs ──
    def last_flag_age(self, t):
        return t - self._last_flag

    def flag_age(self, t):
        """Seconds since the last flag that was not vetoed (math.inf before the first): the posture gate's input."""
        return t - self._last_go if self._last_go > -1e8 else math.inf

    def stats(self):
        ev = [e for e in self.events if e["outcome"] in ("change", "no change")]
        ch = [e for e in ev if e["outcome"] == "change"]
        nf = [e for e in ev if e["outcome"] == "no change"]
        out = {"flags": len(self.events), "change": len(ch), "no_change": len(nf),
               "vetoed": sum(e["outcome"] == "vetoed" for e in self.events),
               "false_per_min": (60.0 * len(nf) / self.hold_s) if self.hold_s > 5 else None}
        for who in ("gate", "cam"):
            hit = [e for e in self.events if e[who + "_to"] is not None]
            miss = [m for m in self.misses if m["who"] == who]
            n = len(hit) + len(miss)
            out[who + "_caught"] = (len(hit), n)
            out[who + "_lead_ms"] = (1000.0 * float(np.median([e[who + "_dt"] for e in hit]))) if hit else None
        right0 = right1 = n = 0
        for e in ch:
            to = e["to"]
            if e["p_prior"] is None or to not in self.dirm.classes:
                continue
            n += 1
            right0 += self.dirm.classes[int(np.argmax(e["p_prior"]))] == to
            right1 += e["pred"] == to
        out["next_right"] = (right0, right1, n)
        return out

    def locked(self):
        """Mean score around flags that led to a change vs flags that did not (the 'specific peak')."""
        res = {}
        for k in ("change", "no change"):
            L = [e["lock"] for e in self.events if e["outcome"] == k and e["lock"] is not None]
            res[k] = (np.mean(L, 0), len(L)) if L else (None, 0)
        return np.arange(LOCK_S[0], LOCK_S[1] + 1e-9, 0.02), res

    def patterns(self):
        """2-D view of the burst patterns of resolved changes (principal components), with each one's transition."""
        E = [e for e in self.events if e["outcome"] == "change" and e["f"] is not None and e["from"] is not None]
        if len(E) < 3:
            return None, []
        X = np.array([e["f"] for e in E]); Xc = X - X.mean(0)
        _, _, Vt = np.linalg.svd(Xc, full_matrices=False)
        P = Xc @ Vt[:2].T
        return P, [(e["from"], e["to"]) for e in E]

    def export(self):
        """Events as arrays for np.savez (offline training of the direction classifier)."""
        E = [e for e in self.events if e["f"] is not None]
        return {"t": np.array([e["t"] for e in E]), "from": np.array([e["from"] or "" for e in E]),
                "to": np.array([e["to"] or "" for e in E]),
                "to_gate": np.array([e["gate_to"] or "" for e in E]), "to_cam": np.array([e["cam_to"] or "" for e in E]),
                "dt_gate": np.array([e["gate_dt"] if e["gate_dt"] is not None else np.nan for e in E]),
                "dt_cam": np.array([e["cam_dt"] if e["cam_dt"] is not None else np.nan for e in E]),
                "outcome": np.array([e["outcome"] for e in E]), "score": np.array([e["peak"] for e in E]),
                "f": np.array([e["f"] for e in E]) if E else np.zeros((0, self.cs.n_ch)),
                "D": np.array([e["D"] for e in E]) if E else np.zeros((0, self.cs.n_ch)),
                "vetoed": np.array([e["vetoed"] for e in E], bool)}


# ── training the learned detector from Transitions recordings (the camera labels every transition) ──
def replay(d):
    """A recording through ChangeScore exactly as live → (step times, features (n, 3·ch + 3), burst score)."""
    cs = ChangeScore(float(d["fs"]), d["emg"].shape[0])
    lag = collections.deque(maxlen=LAG_STEPS + 1)
    T, F, S = [], [], []
    i0 = 0
    for k, n in enumerate(d["emg_n"]):
        for (ts, sc, D, Ds) in cs.push(d["emg"][:, i0:i0 + n], float(d["t"][k])):
            lag.append(D)
            T.append(ts); S.append(sc)
            F.append(features(D, Ds, lag[0] if len(lag) == lag.maxlen else np.zeros_like(D)))
        i0 += int(n)
    return np.array(T), np.array(F), np.array(S)


def camera_transitions(d):
    """The camera's stable postures (≥ 0.2 s; one label split by < 0.3 s of nothing is one) and, for each change, the
    movement onset: where the camera hand's finger-curl speed rises (20 % of its peak) before the new label; the label
    time − 0.1 s when the camera hand is missing. → (transitions [{a, b, onset, cam}], segments [[label, t0, t1]])."""
    import key_postures as kp
    t = np.asarray(d["t"], float); cam = [str(x) or None for x in d["cam"]]
    segs = []; s0 = 0
    for i in range(1, len(cam) + 1):
        if i == len(cam) or cam[i] != cam[s0]:
            if cam[s0] is not None and t[i - 1] - t[s0] >= 0.2:
                segs.append([cam[s0], t[s0], t[i - 1]])
            s0 = i
    merged = []
    for g in segs:
        if merged and merged[-1][0] == g[0] and g[1] - merged[-1][2] < 0.3:
            merged[-1][2] = g[2]
        else:
            merged.append(g)
    CL = np.asarray(d["cam_local"], float)
    curl = np.array([kp.finger_flex(L - L[1]) if np.isfinite(L).all() else np.full(5, np.nan) for L in CL])
    spd = np.r_[0.0, np.nansum(np.abs(np.diff(curl, axis=0)), 1) / np.maximum(np.diff(t), 1e-3)]
    spd = np.convolve(np.nan_to_num(spd), np.ones(3) / 3, "same")
    TR = []
    for a, b in zip(merged[:-1], merged[1:]):
        if a[0] == b[0] or b[1] - a[2] > 1.0:
            continue
        w = np.where((t >= a[2] - 0.4) & (t <= b[1] + 0.1))[0]
        hold = (t >= a[1]) & (t <= a[2])
        onset = b[1] - 0.1
        if len(w) and np.isfinite(curl[w]).all() and hold.any():
            s_0 = np.median(spd[hold]); ip = w[int(np.argmax(spd[w]))]; th = s_0 + 0.2 * (spd[ip] - s_0); j = ip
            while j > w[0] and spd[j] > th:
                j -= 1
            onset = t[j]
        TR.append({"a": a[0], "b": b[0], "onset": float(onset), "cam": float(b[1])})
    return TR, merged


def _fit(X, y, l2=1e-2, it=400, lr=0.5):
    m = y >= 0
    X = X[m]; y = y[m].astype(float)
    mu, sd = X.mean(0), X.std(0) + 1e-6
    Z = (X - mu) / sd
    w = np.zeros(Z.shape[1]); b = 0.0
    sw = np.where(y == 1, (y == 0).sum() / max(1, (y == 1).sum()), 1.0); sw /= sw.mean()
    for _ in range(it):
        p = 1.0 / (1.0 + np.exp(-(Z @ w + b))); g = (p - y) * sw
        w -= lr * (Z.T @ g / len(y) + l2 * w); b -= lr * g.mean()
    return mu, sd, w, b


def _flags(st, p, th):
    above = p > th; ev = []; last = -1e9
    for k in np.where(above[1:] & ~above[:-1])[0] + 1:
        if st[k] - last >= REFRACTORY_S:
            ev.append(k); last = st[k]
    return np.array(ev, int)


def _evaluate(rec, p, th):
    """→ (transitions caught, false flags in holds, minutes held, leads vs the movement onset)."""
    ev = _flags(rec["st"], p, th); tf = rec["st"][ev] if len(ev) else np.zeros(0); leads = []
    for x in rec["TR"]:
        dd = tf[(tf >= x["onset"] - 0.3) & (tf <= x["cam"] + 0.2)]
        if len(dd):
            leads.append(float(dd[0] - x["onset"]))
    return len(leads), int(np.isin(ev, np.where(rec["hm"])[0]).sum()), rec["hm"].sum() * STEP_S / 60.0, leads


def _pick(recs, ps, target):
    """The lowest threshold with at most `target` false flags per minute held over these recordings."""
    allp = np.concatenate(ps); mins = sum(r["hm"].sum() * STEP_S / 60.0 for r in recs)
    best = float(np.max(allp))
    for th in np.linspace(np.percentile(allp, 99.9), np.percentile(allp, 50), 300):
        if sum(_evaluate(r, p, th)[1] for r, p in zip(recs, ps)) / max(mins, 1e-9) > target:
            break
        best = float(th)
    return best


def train_detector(paths, target_false_per_min=2.0, progress=None):
    """Fit the learned detector on Transitions recordings. 'Starting' = movement onset −100 .. +150 ms, 'holding' =
    inside a stable posture 0.5 s from its ends. With ≥ 2 recordings each one is also tested by a model trained on
    the others (threshold chosen there), next to the score threshold: the report says what to expect live."""
    recs = []
    for i, path in enumerate(paths):
        if progress:
            progress(f"replaying {os.path.basename(path)} ({i + 1}/{len(paths)})")
        d = dict(np.load(path, allow_pickle=False))
        st, F, sc = replay(d)
        TR, segs = camera_transitions(d)
        y = np.full(len(st), -1); hm = np.zeros(len(st), bool)
        for g in segs:
            hm |= (st > g[1] + 0.5) & (st < g[2] - 0.5)
        y[hm] = 0
        for x in TR:
            y[(st >= x["onset"] - 0.1) & (st <= x["onset"] + 0.15)] = 1
        recs.append({"name": os.path.basename(path), "st": st, "F": F, "sc": sc, "y": y, "TR": TR, "hm": hm})
    n_tr = sum(len(r["TR"]) for r in recs)
    if n_tr < 10:
        raise ValueError(f"only {n_tr} transitions the camera saw in {len(recs)} recording(s): record more first")
    counts = np.zeros((len(PRIOR_CLASSES), len(PRIOR_CLASSES)))
    for r in recs:
        for x in r["TR"]:
            if x["a"] in PRIOR_CLASSES and x["b"] in PRIOR_CLASSES and x["a"] != x["b"]:
                counts[PRIOR_CLASSES.index(x["a"]), PRIOR_CLASSES.index(x["b"])] += 1
    report = {"recordings": [r["name"] for r in recs], "transitions": n_tr, "target_false_per_min": target_false_per_min,
              "counts": counts.tolist(), "count_classes": list(PRIOR_CLASSES)}
    if len(recs) >= 2:
        if progress:
            progress("testing each recording with a model trained on the others")
        tot = {"learned": [0, 0, 0.0, []], "score": [0, 0, 0.0, []]}
        for i, rt in enumerate(recs):
            tr = [r for j, r in enumerate(recs) if j != i]
            mu, sd, w, b = _fit(np.vstack([r["F"] for r in tr]), np.concatenate([r["y"] for r in tr]))
            pm = lambda F: 1.0 / (1.0 + np.exp(-(((F - mu) / sd) @ w + b)))
            for name, p_tr, p_te in (("learned", [pm(r["F"]) for r in tr], pm(rt["F"])), ("score", [r["sc"] for r in tr], rt["sc"])):
                c, fa, mins, leads = _evaluate(rt, p_te, _pick(tr, p_tr, target_false_per_min))
                T = tot[name]; T[0] += c; T[1] += fa; T[2] += mins; T[3] += leads
        report["unseen"] = {k: {"caught": v[0], "of": n_tr, "false_per_min": v[1] / max(v[2], 1e-9),
                                "lead_ms": 1000.0 * float(np.median(v[3])) if v[3] else None} for k, v in tot.items()}
    if progress:
        progress("fitting on all recordings")
    mu, sd, w, b = _fit(np.vstack([r["F"] for r in recs]), np.concatenate([r["y"] for r in recs]))
    det = LearnedDetector(mu, sd, w, b, 0.5, report)
    det.threshold = _pick(recs, [det.p_many(r["F"]) for r in recs], target_false_per_min)
    return det
