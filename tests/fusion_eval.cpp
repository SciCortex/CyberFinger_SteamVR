/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
// ═══════════════════════════════════════════════════════════════════════════
// fusion_eval.cpp — replay a capture through ImuFusion (joint IMU + optical orientation)
//
//   fusion_eval <capture.csv> [name=value ...]      names: tauCorrection defaultLag imu (joint|body)
//                                                          prior (default | <other capture.csv>)
//
// Captures (tools/analyze_tap_capture.py --capture N, with the bridge running) hold the headset hand's poses
// and skeletons and the glove IMUs on the driver's clock. Per hand this replays them in arrival order: IMU
// samples into AddImu (stamped at their BLE arrival), optical poses into Observe (seen = the skeleton changed
// within 80 ms, as the driver decides). It reports the calibration, the agreement with the optical orientation
// while seen (the fused orientation `lag` earlier, as the optical one trails), the rotation jitter at rest,
// and simulated occlusions: the optical stream marked unseen for D seconds every 10 s, the fused orientation
// compared with the (hidden) optical one, against holding the last optical orientation.
// prior: a cold start from a saved calibration, the built-in default or the one solved on another capture
// (as the driver starts from the previous session's).
// ═══════════════════════════════════════════════════════════════════════════

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>
#include "ImuFusion.h"

using namespace cf;

struct Event { double t; int kind; bool changed; std::vector<double> v; };
static int g_slot = 8;          // quaternion offset in the IMU record: 8 = joint, 0 = body 1
static bool g_havePrior = false;
static ImuFusion::Calibration g_prior[2];

static double AngleDeg(const Quat& a, const Quat& b) {
    const double d = std::fabs(a.w * b.w + a.x * b.x + a.y * b.y + a.z * b.z);
    return 2.0 * std::acos(std::min(1.0, d)) * 180.0 / kPi;
}

struct Sample { double t; Quat opt, fused, ahead; bool seen, haveFused, blank; };   // ahead: fused + 40 ms of w

// Replay one hand. blank(t): the optical stream is hidden (unseen) at time t.
template <class Blank>
static std::vector<Sample> Replay(const std::vector<Event>& ev, const ImuFusion::Params& prm, Blank blank,
                                  ImuFusion::Status& st, double& calibratedAt, int hand,
                                  ImuFusion::Calibration* cal = nullptr) {
    ImuFusion f;
    f.SetParams(prm);
    f.Reset();
    if (g_havePrior) f.SetPrior(g_prior[hand]);
    std::vector<Sample> out;
    double lastChange = -1e9;
    calibratedAt = -1;
    for (const Event& e : ev) {
        if (e.kind == 6) {
            f.AddImu(e.t - e.v[21], Normalize(Quat{ e.v[g_slot], e.v[g_slot + 1], e.v[g_slot + 2], e.v[g_slot + 3] }));
        } else if (e.kind == 1) {
            if (e.changed) lastChange = e.t;
        } else if (e.kind == 0) {
            const Quat q = Normalize(Quat{ e.v[4], e.v[5], e.v[6], e.v[7] });
            const bool hidden = blank(e.t);
            const bool seen = !hidden && e.v[17] > 0.5 && e.t - lastChange < 0.08;
            f.Observe(e.t, q, seen);
            Sample s{ e.t, q, {}, {}, seen, false, hidden };
            Vec3 w;
            s.haveFused = f.Orientation(e.t, s.fused, w);
            if (s.haveFused) {                              // as SteamVR would extrapolate it 40 ms ahead
                const Vec3 r = w * 0.04;
                const double a = Length(r), k = a > 1e-12 ? std::sin(a / 2) / a : 0.5;
                s.ahead = Normalize(Quat{ std::cos(a / 2), r.x * k, r.y * k, r.z * k } * s.fused);
            }
            if (s.haveFused && calibratedAt < 0) calibratedAt = e.t;
            out.push_back(s);
        }
    }
    st = f.GetStatus();
    if (cal && !f.GetCalibration(*cal)) *cal = {};
    return out;
}

static bool Load(const char* path, std::vector<Event> hands[2]) {
    std::ifstream in(path);
    if (!in) return false;
    std::string line;
    std::getline(in, line);
    while (std::getline(in, line)) {
        std::stringstream ss(line);
        std::string cell;
        std::vector<double> c;
        while (std::getline(ss, cell, ',')) c.push_back(std::atof(cell.c_str()));
        if (c.size() < 26) continue;
        const int kind = int(c[2]);
        if (kind != 0 && kind != 1 && kind != 6) continue;
        if (kind == 6 && !(int(c[3]) & (g_slot == 8 ? 0x4 : 0x1))) continue;   // that IMU isn't in this report
        hands[int(c[1]) & 1].push_back({ c[0], kind, c[3] != 0, std::vector<double>(c.begin() + 4, c.end()) });
    }
    return true;
}

// Fused orientation at time t (interpolated from the replayed samples).
static bool FusedAt(const std::vector<Sample>& s, double t, Quat& q) {
    auto it = std::lower_bound(s.begin(), s.end(), t, [](const Sample& a, double v) { return a.t < v; });
    if (it == s.begin() || it == s.end()) return false;
    const Sample& b = *it;
    const Sample& a = *(it - 1);
    if (!a.haveFused || !b.haveFused) return false;
    Quat bq = b.fused;
    if (a.fused.w * bq.w + a.fused.x * bq.x + a.fused.y * bq.y + a.fused.z * bq.z < 0)
        bq = { -bq.w, -bq.x, -bq.y, -bq.z };
    q = Normalize(Slerp(a.fused, bq, (t - a.t) / std::max(1e-9, b.t - a.t)));
    return true;
}

// Per sample: angle (deg) from the centred 100 ms mean orientation.
static std::vector<double> Jitter(const std::vector<double>& t, const std::vector<Quat>& q) {
    std::vector<double> out(t.size(), 0);
    size_t j0 = 0, j1 = 0;
    for (size_t i = 0; i < t.size(); ++i) {
        while (j1 < t.size() && t[j1] <= t[i] + 0.05) ++j1;
        while (t[j0] < t[i] - 0.05) ++j0;
        Quat m{ 0, 0, 0, 0 };
        for (size_t j = j0; j < j1; ++j) {
            const double s = (q[j].w * q[i].w + q[j].x * q[i].x + q[j].y * q[i].y + q[j].z * q[i].z) < 0 ? -1 : 1;
            m = { m.w + s * q[j].w, m.x + s * q[j].x, m.y + s * q[j].y, m.z + s * q[j].z };
        }
        out[i] = AngleDeg(q[i], Normalize(m));
    }
    return out;
}

static double Rms(const std::vector<double>& v) {
    double s = 0;
    for (double x : v) s += x * x;
    return v.empty() ? 0 : std::sqrt(s / v.size());
}
static double Pct(std::vector<double> v, double p) {
    if (v.empty()) return 0;
    const size_t k = std::min(v.size() - 1, size_t(p * v.size()));
    std::nth_element(v.begin(), v.begin() + k, v.end());
    return v[k];
}

int main(int argc, char** argv) {
    if (argc < 2) {
        std::printf("usage: fusion_eval <capture.csv> [tauCorrection=0.5 defaultLag=0.03 imu=joint "
                    "prior=default|<capture.csv>]\n");
        return 2;
    }
    ImuFusion::Params prm;
    std::string prior;
    for (int i = 2; i < argc; ++i) {
        const char* eq = std::strchr(argv[i], '=');
        if (!eq) continue;
        const std::string name(argv[i], size_t(eq - argv[i]));
        const double v = std::atof(eq + 1);
        if (name == "tauCorrection") prm.tauCorrection = v;
        else if (name == "defaultLag") prm.defaultLag = v;
        else if (name == "maxPriorResidualDeg") prm.maxPriorResidualDeg = v;
        else if (name == "priorPairs") prm.priorPairs = size_t(v);
        else if (name == "imu") g_slot = (std::string(eq + 1) == "body") ? 0 : 8;
        else if (name == "prior") prior = eq + 1;
        else { std::printf("unknown parameter %s\n", argv[i]); return 2; }
    }
    std::vector<Event> hands[2];
    if (!Load(argv[1], hands)) { std::printf("cannot read %s\n", argv[1]); return 1; }
    std::printf("tauCorrection %.2f s\n", prm.tauCorrection);
    if (prior == "default") {
        for (int h = 0; h < 2; ++h) g_prior[h] = ImuFusion::DefaultCalibration(h);
        g_havePrior = true;
    } else if (!prior.empty()) {                            // solved on another capture, as a previous session
        std::vector<Event> other[2];
        if (!Load(prior.c_str(), other)) { std::printf("cannot read %s\n", prior.c_str()); return 1; }
        for (int h = 0; h < 2; ++h) {
            ImuFusion::Status st;
            double at;
            if (!other[h].empty()) Replay(other[h], prm, [](double) { return false; }, st, at, h, &g_prior[h]);
        }
        g_havePrior = true;
    }
    if (g_havePrior)
        for (int h = 0; h < 2; ++h)
            std::printf("prior %s: mount (%.4f, %.4f, %.4f, %.4f), lag %.0f ms\n", h ? "right" : "left",
                        g_prior[h].mount.w, g_prior[h].mount.x, g_prior[h].mount.y, g_prior[h].mount.z,
                        g_prior[h].lag * 1e3);
    for (int h = 0; h < 2; ++h) {
        const auto& ev = hands[h];
        size_t nImu = 0;
        for (const Event& e : ev) nImu += e.kind == 6;
        if (nImu < 100) { std::printf("%s hand: no joint IMU data\n", h ? "right" : "left"); continue; }
        ImuFusion::Status st;
        ImuFusion::Calibration cal;
        double calAt;
        const auto s = Replay(ev, prm, [](double) { return false; }, st, calAt, h, &cal);
        const double t0 = s.front().t;
        std::printf("%s hand: %zu optical, %zu IMU samples, %.0f s\n", h ? "right" : "left", s.size(), nImu,
                    s.back().t - t0);
        if (!st.calibrated) { std::printf("  not calibrated (pairs %zu)\n", st.pairs); continue; }
        std::printf("  calibrated after %.1f s; lag %.1f ms (correlation %.2f), heading %.1f deg, mounting spread "
                    "%.1f deg rms over %zu pairs, %zu solves%s\n", calAt - t0, st.lag * 1e3, st.lagCorrelation,
                    st.alphaDeg, st.residualDeg, st.pairs, st.solves, st.fromPrior ? " (still on the prior)" : "");
        if (g_havePrior) std::printf("  prior fit %.1f deg at its last try\n", st.priorResidualDeg);
        if (st.solves) {
            std::printf("  mount (%.4f, %.4f, %.4f, %.4f)", cal.mount.w, cal.mount.x, cal.mount.y, cal.mount.z);
            if (g_havePrior) std::printf(", %.1f deg from the prior", AngleDeg(cal.mount, g_prior[h].mount));
            std::printf("\n");
        }

        // Agreement while seen, after calibration: fused `lag` earlier vs optical.
        std::vector<double> err, tf, to;
        std::vector<Quat> qf, qo, qa;
        for (const Sample& x : s) {
            if (!x.haveFused || !x.seen) continue;
            Quat q;
            if (FusedAt(s, x.t - st.lag, q)) err.push_back(AngleDeg(q, x.opt));
            tf.push_back(x.t); qf.push_back(x.fused); qa.push_back(x.ahead);
            to.push_back(x.t); qo.push_back(x.opt);
        }
        std::printf("  seen: fused vs optical (lag-aligned) %.2f deg rms, p95 %.2f, max %.1f\n", Rms(err),
                    Pct(err, 0.95), Pct(err, 1.0));
        {   // the start: the first 5 s after calibration, overall and at rest (optical turning < 0.3 rad/s)
            std::vector<double> all, still;
            for (size_t i = 0; i < s.size(); ++i) {
                const Sample& x = s[i];
                if (!x.haveFused || !x.seen || x.t > calAt + 5.0) continue;
                Quat q;
                if (!FusedAt(s, x.t - st.lag, q)) continue;
                all.push_back(AngleDeg(q, x.opt));
                size_t a = i, b = i;
                while (a > 0 && s[a].t > x.t - 0.05) --a;
                while (b + 1 < s.size() && s[b].t < x.t + 0.05) ++b;
                if (AngleDeg(s[a].opt, s[b].opt) * kPi / 180.0 / std::max(1e-3, s[b].t - s[a].t) < 0.3)
                    still.push_back(all.back());
            }
            std::printf("        first 5 s after calibration: %.1f deg rms (%.1f at rest)\n", Rms(all), Rms(still));
        }
        {   // the same split by how fast the optical hand turns: static model error vs timing
            std::vector<double> still, slow, fast;
            for (size_t i = 0; i < s.size(); ++i) {
                const Sample& x = s[i];
                if (!x.haveFused || !x.seen) continue;
                size_t a = i, b = i;
                while (a > 0 && s[a].t > x.t - 0.05) --a;
                while (b + 1 < s.size() && s[b].t < x.t + 0.05) ++b;
                const double rate = AngleDeg(s[a].opt, s[b].opt) * kPi / 180.0 / std::max(1e-3, s[b].t - s[a].t);
                Quat q;
                if (!FusedAt(s, x.t - st.lag, q)) continue;
                (rate < 0.3 ? still : rate < 1.5 ? slow : fast).push_back(AngleDeg(q, x.opt));
            }
            std::printf("        by turn rate: <0.3 rad/s %.1f deg rms (%zu), 0.3-1.5 %.1f (%zu), >1.5 %.1f (%zu)\n",
                        Rms(still), still.size(), Rms(slow), slow.size(), Rms(fast), fast.size());
            // Would another alignment do better? Disagreement while turning (> 0.3 rad/s) per assumed lag.
            std::printf("        moving, per assumed lag:");
            for (int ms = 0; ms <= 100; ms += 10) {
                std::vector<double> e;
                for (size_t i = 0; i < s.size(); ++i) {
                    const Sample& x = s[i];
                    if (!x.haveFused || !x.seen) continue;
                    size_t a = i, b = i;
                    while (a > 0 && s[a].t > x.t - 0.05) --a;
                    while (b + 1 < s.size() && s[b].t < x.t + 0.05) ++b;
                    if (AngleDeg(s[a].opt, s[b].opt) * kPi / 180.0 / std::max(1e-3, s[b].t - s[a].t) < 0.3) continue;
                    Quat q;
                    if (FusedAt(s, x.t - ms * 1e-3, q)) e.push_back(AngleDeg(q, x.opt));
                }
                std::printf(" %d:%.1f", ms, Rms(e));
            }
            std::printf("\n");
        }
        // Jitter at rest: samples whose 100 ms neighbourhood barely turns (optical rate < 0.3 rad/s).
        // The raw IMU at the same instants (interpolated between its reports), for comparison.
        std::vector<double> ti;
        std::vector<Quat> qiRaw, qi;
        for (const Event& e : ev)
            if (e.kind == 6) {
                Quat q = Normalize(Quat{ e.v[g_slot], e.v[g_slot + 1], e.v[g_slot + 2], e.v[g_slot + 3] });
                if (!qiRaw.empty() && q.w * qiRaw.back().w + q.x * qiRaw.back().x + q.y * qiRaw.back().y +
                                      q.z * qiRaw.back().z < 0)
                    q = { -q.w, -q.x, -q.y, -q.z };
                if (!ti.empty() && e.t - e.v[21] <= ti.back()) continue;
                ti.push_back(e.t - e.v[21]);
                qiRaw.push_back(q);
            }
        for (double t : tf) {
            auto it = std::lower_bound(ti.begin(), ti.end(), t);
            const size_t k = std::min<size_t>(std::max<size_t>(1, it - ti.begin()), ti.size() - 1);
            const double u = std::min(1.0, std::max(0.0, (t - ti[k - 1]) / std::max(1e-9, ti[k] - ti[k - 1])));
            qi.push_back(Normalize(Slerp(qiRaw[k - 1], qiRaw[k], u)));
        }
        const auto jf = Jitter(tf, qf), jo = Jitter(to, qo), ji = Jitter(tf, qi), ja = Jitter(tf, qa);
        std::vector<double> sf, so, si, sa;
        for (size_t i = 1; i + 1 < to.size(); ++i) {
            size_t a = i, b = i;
            while (a > 0 && to[a] > to[i] - 0.05) --a;
            while (b + 1 < to.size() && to[b] < to[i] + 0.05) ++b;
            const double rate = AngleDeg(qo[a], qo[b]) * kPi / 180.0 / std::max(1e-3, to[b] - to[a]);
            if (rate < 0.3) { sf.push_back(jf[i]); so.push_back(jo[i]); si.push_back(ji[i]); sa.push_back(ja[i]); }
        }
        std::printf("  at rest (%zu samples): rotation jitter optical %.3f deg -> fused %.3f deg (%.1f -> %.1f mm "
                    "at a 2 m laser target); raw IMU there %.3f deg; fused 40 ms ahead %.3f deg\n", so.size(),
                    Rms(so), Rms(sf), Rms(so) * kPi / 180 * 2000, Rms(sf) * kPi / 180 * 2000, Rms(si), Rms(sa));

        // Simulated occlusions.
        for (double d : { 0.5, 1.0, 2.0, 5.0 }) {
            auto blank = [&](double t) {
                const double u = t - t0 - 6.0;                      // after 6 s (calibration), every D + 2 s
                return u > 0 && std::fmod(u, d + 2.0) < d;
            };
            ImuFusion::Status st2;
            double cal2;
            const auto s2 = Replay(ev, prm, blank, st2, cal2, h);
            std::vector<double> ef, eh;
            Quat held;
            bool haveHeld = false;
            for (const Sample& x : s2) {
                if (!x.blank) { held = x.opt; haveHeld = true; continue; }
                Quat q;
                if (!haveHeld || !FusedAt(s2, x.t - st2.lag, q)) continue;
                ef.push_back(AngleDeg(q, x.opt));
                eh.push_back(AngleDeg(held, x.opt));
            }
            std::printf("  occluded %.1f s: fused %.1f deg rms (p95 %.1f) vs holding the last optical %.1f deg rms "
                        "(p95 %.1f), %zu samples\n", d, Rms(ef), Pct(ef, 0.95), Rms(eh), Pct(eh, 0.95), ef.size());
        }
    }
    return 0;
}
