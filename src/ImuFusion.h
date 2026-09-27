/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// ImuFusion.h — the glove's joint IMU fused with the headset's optical hand
// orientation, driver-side (until the Fusion Studio's full fusion, which
// replaces all of this through the FUSED mode)
//
// The joint IMU sits on the back of the hand. Its orientation relates to the
// optical hand orientation (driver space, any hand-fixed frame) as
//
//     q_hand(t) = Y(α) · C · q_imu(t) · M              (bridge/mount_calib.py)
//
// C relabels the IMU's z-up world as SteamVR's y-up, α is the IMU's heading
// (arbitrary, drifting slowly: 6-axis, no magnetometer), M the sensor's mounting
// on the hand. While the headset sees the hand, α and M are solved from a window
// of (IMU, optical) pairs — a grid over α, the mean mounting for each — with the
// IMU sample taken `lag` earlier: the optical stream trails the IMU (Steam Link
// 5–45 ms), and the lag is measured continuously by correlating the two rotation
// rates. The output is
//
//     q_out = q_corr · Y(α) · C · q_imu(newest) · M
//
// q_corr follows the optical orientation slowly (tauCorrection), so the absolute
// orientation is the headset's while the fast motion, and its timing, are the
// IMU's: ~3× less jitter at rest and no optical lag. While the hand is unseen
// q_corr is frozen and the IMU carries the orientation alone.
//
// Cold start: M belongs to the glove and how it sits on the hand, and the lag to
// the streamer, so both carry over between sessions (the driver saves them); α
// restarts with every IMU power-up. Given a prior M and lag, α alone is fitted
// from the first few pairs — no spread of orientations needed — and the fusion
// runs within a second; the full solve then refines M as usual.
// ═══════════════════════════════════════════════════════════════════════════

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <deque>
#include <mutex>
#include <vector>
#include "MathUtil.h"

namespace cf {

class ImuFusion {
public:
    struct Params {
        double tauCorrection = 0.5;    // s: how fast the output settles on the optical orientation
        double defaultLag = 0.03;      // s: optical behind the IMU, until measured
        double maxLag = 0.12;          // s
        double imuTimeout = 0.1;       // s without IMU data: no fused output
        double pairInterval = 0.1;     // s between calibration pairs
        double pairWindow = 60.0;      // s of pairs kept
        double solveEvery = 3.0;       // s
        size_t minPairs = 40;
        double minSpreadDeg = 12.0;    // the optical orientations must differ this much for a solve
        double maxResidualDeg = 25.0;  // a worse fit is rejected
        double maxCorrectionRate = 1.0; // rad/s: faster optical turns aren't compared (the streams disagree in
                                        // motion: Steam Link's optical rotation is a prediction; the IMU leads)
        size_t priorPairs = 5;         // pairs to fit the heading with a prior mounting
        double maxPriorResidualDeg = 25.0; // a prior mounting that fits worse is ignored (another glove, sensor turned).
                                           // Sessions of the same glove scatter by 8-23° (how it sits, the postures
                                           // seen), and even that start beats waiting for a full solve.
    };

    struct Status {
        bool   calibrated = false;
        bool   fromPrior = false;      // running on the prior mounting (no full solve yet)
        double lag = 0;                // s, optical behind the IMU
        double lagCorrelation = 0;     // of the last lag measurement
        double alphaDeg = 0;
        double residualDeg = 0;        // rms spread of the mounting over the pairs at the last solve (or prior fit)
        size_t pairs = 0;
        size_t solves = 0;             // full solves
        double priorResidualDeg = -1;  // the prior mounting's fit at the last try (-1: not tried)
    };

    // What carries over to the next session: the mounting (hand-fixed frame of the optical source) and the lag.
    struct Calibration {
        Quat   mount;
        double lag = 0;                // s; 0: unknown
        double residualDeg = 0;
    };

    // The reference gloves' calibration, the start when nothing is saved yet: the mean of three Steam Link
    // sessions (2026-09-26/27; each within 8-12° of it), the lag their middle (they measured 17-88 ms).
    static Calibration DefaultCalibration(int hand) {
        return hand ? Calibration{ { 0.4773, 0.4682, 0.5676, 0.4804 }, 0.05, 0 }
                    : Calibration{ { 0.4982, 0.4682, -0.5101, -0.5219 }, 0.05, 0 };
    }

    void SetParams(const Params& p) { std::lock_guard<std::mutex> g(m_lock); m_p = p; }

    void Reset() {
        std::lock_guard<std::mutex> g(m_lock);
        m_imu.clear();
        m_opt.clear();
        m_pairs.clear();
        m_st = Status{};
        m_st.lag = m_p.defaultLag;
        m_haveCorr = m_haveLag = m_havePrior = m_solved = false;
        m_nextPair = m_nextSolve = m_nextLag = m_lastObserve = m_nextPriorFit = 0;
    }

    // Start from an earlier session's calibration: until the first full solve, the heading alone is fitted.
    // The lag (if > 0) is the starting point for its measurement.
    void SetPrior(const Calibration& c) {
        std::lock_guard<std::mutex> g(m_lock);
        m_prior = c;
        m_prior.mount = Normalize(c.mount);
        m_havePrior = true;
        if (!m_haveLag && c.lag > 0) {
            m_st.lag = std::min(c.lag, m_p.maxLag);
            m_haveLag = true;
        }
    }

    // The latest full solve's mounting and the lag, to save for the next session. False before a full solve.
    bool GetCalibration(Calibration& c) const {
        std::lock_guard<std::mutex> g(m_lock);
        if (!m_solved) return false;
        c = { m_mount, m_haveLag ? m_st.lag : 0.0, m_st.residualDeg };
        return true;
    }

    // A joint IMU orientation (its own z-up world), stamped with the driver's clock. Any thread.
    void AddImu(double t, Quat q) {
        std::lock_guard<std::mutex> g(m_lock);
        q = Normalize(q);
        if (!m_imu.empty()) {
            if (t <= m_imu.back().t) return;
            if (Dot4(q, m_imu.back().q) < 0) q = Neg(q);
        }
        m_imu.push_back({ t, q, true });
        while (!m_imu.empty() && m_imu.front().t < t - 3.0) m_imu.pop_front();
    }

    // An optical observation of the hand's orientation (driver space, any hand-fixed frame). seen: the headset
    // tracks the hand now (not extrapolating a lost one). Any thread.
    void Observe(double t, Quat q, bool seen) {
        std::lock_guard<std::mutex> g(m_lock);
        q = Normalize(q);
        if (!m_opt.empty()) {
            if (t <= m_opt.back().t) return;
            if (Dot4(q, m_opt.back().q) < 0) q = Neg(q);
        }
        m_opt.push_back({ t, q, seen });
        while (!m_opt.empty() && m_opt.front().t < t - 5.0) m_opt.pop_front();
        const double dt = std::min(0.1, std::max(0.0, t - m_lastObserve));
        m_lastObserve = t;
        if (!seen) return;

        if (t >= m_nextLag) { m_nextLag = t + 1.0; MeasureLag(t); }

        // Only a slowly turning hand is compared: in motion the two streams disagree by several degrees
        // (timing, and the optical stream's prediction), and at rest they agree to ~1°. So the output rides the
        // IMU through fast motion and settles on the optical orientation whenever the hand slows down.
        double rate;
        if (!RateAt(m_opt, t - 0.025, rate) || rate > m_p.maxCorrectionRate) return;

        Quat qi;
        if (t >= m_nextPair && ImuAt(t - m_st.lag, qi)) {
            m_nextPair = t + m_p.pairInterval;
            m_pairs.push_back({ t, qi, q });
            while (!m_pairs.empty() && m_pairs.front().t < t - m_p.pairWindow) m_pairs.pop_front();
            m_st.pairs = m_pairs.size();
        }
        if (!m_st.calibrated && m_havePrior && m_pairs.size() >= m_p.priorPairs && t >= m_nextPriorFit) {
            m_nextPriorFit = t + 0.2;
            FitHeading();
        }
        if (t >= m_nextSolve && m_pairs.size() >= m_p.minPairs) { m_nextSolve = t + m_p.solveEvery; Solve(); }

        // Settle slowly on the optical orientation, comparing like with like: the IMU `lag` earlier.
        if (m_st.calibrated && ImuAt(t - m_st.lag, qi)) {
            Quat target = Normalize(q * Conj(Model(qi)));
            if (!m_haveCorr) {
                m_corr = target;
                m_haveCorr = true;
            } else {
                if (Dot4(target, m_corr) < 0) target = Neg(target);
                m_corr = Normalize(Slerp(m_corr, target, 1.0 - std::exp(-dt / std::max(1e-3, m_p.tauCorrection))));
            }
        }
    }

    // The fused orientation for display, from the newest IMU sample, and its angular velocity (driver space).
    // False until calibrated, or when the IMU is silent.
    bool Orientation(double now, Quat& q, Vec3& w) {
        std::lock_guard<std::mutex> g(m_lock);
        if (!m_st.calibrated || !m_haveCorr || m_imu.size() < 3 || now - m_imu.back().t > m_p.imuTimeout)
            return false;
        const Stamped& b = m_imu.back();
        q = Normalize(m_corr * Model(b.q));
        // Angular velocity over at least 20 ms of IMU samples, in driver space. (Reports are stamped with their
        // BLE arrival, and a connection event often delivers two within a millisecond: a shorter span is noise.)
        size_t ia = m_imu.size() - 2;
        while (ia > 0 && b.t - m_imu[ia].t < 0.02) --ia;
        const Stamped& a = m_imu[ia];
        const Quat d = Normalize(Model(b.q) * Conj(Model(a.q)));
        w = Rotate(m_corr, RotationVector(d) * (1.0 / std::max(0.005, b.t - a.t)));
        // Carried on to `now` (the IMU reports every ~9 ms): smooth between reports instead of stepping.
        q = Normalize(FromRotationVector(w * std::min(0.03, std::max(0.0, now - b.t))) * q);
        return true;
    }

    Status GetStatus() const { std::lock_guard<std::mutex> g(m_lock); return m_st; }

private:
    struct Stamped { double t; Quat q; bool seen; };
    struct Pair { double t; Quat imu, opt; };

    static double Dot4(const Quat& a, const Quat& b) { return a.w * b.w + a.x * b.x + a.y * b.y + a.z * b.z; }
    static Quat Neg(const Quat& q) { return { -q.w, -q.x, -q.y, -q.z }; }
    static double AngleDeg(const Quat& a, const Quat& b) {
        return 2.0 * std::acos(std::min(1.0, std::fabs(Dot4(a, b)))) * 180.0 / kPi;
    }
    static Vec3 RotationVector(Quat q) {
        if (q.w < 0) q = Neg(q);
        const double s = std::sqrt(q.x * q.x + q.y * q.y + q.z * q.z);
        if (s < 1e-9) return { 2 * q.x, 2 * q.y, 2 * q.z };
        return Vec3{ q.x, q.y, q.z } * (2.0 * std::atan2(s, q.w) / s);
    }
    static Quat FromRotationVector(const Vec3& r) {
        const double a = Length(r);
        if (a < 1e-12) return {};
        const double s = std::sin(a / 2) / a;
        return { std::cos(a / 2), r.x * s, r.y * s, r.z * s };
    }
    static Quat YawQ(double a) { return { std::cos(a / 2), 0, std::sin(a / 2), 0 }; }
    // IMU z-up world → SteamVR y-up: (x, y, z) → (x, z, -y), a -90° turn about x.
    static Quat ImuToVr() { return { std::sqrt(0.5), -std::sqrt(0.5), 0, 0 }; }

    Quat Model(const Quat& imu) const { return YawQ(m_alpha) * ImuToVr() * imu * m_mount; }

    // IMU orientation at time t (interpolated; the newest sample for up to 50 ms after it).
    bool ImuAt(double t, Quat& q) const { return At(m_imu, t, q); }
    static bool At(const std::deque<Stamped>& s, double t, Quat& q) {
        if (s.size() < 2 || t < s.front().t || t > s.back().t + 0.05) return false;
        if (t >= s.back().t) { q = s.back().q; return true; }
        size_t lo = 0, hi = s.size() - 1;                  // s[lo].t <= t < s[hi].t
        while (hi - lo > 1) {
            const size_t mid = (lo + hi) / 2;
            (s[mid].t <= t ? lo : hi) = mid;
        }
        const double f = (t - s[lo].t) / std::max(1e-9, s[hi].t - s[lo].t);
        q = Normalize(Slerp(s[lo].q, s[hi].q, f));
        return true;
    }

    // Rotation rate (rad/s) of a stream around time t, over 40 ms: frame-independent, so the two streams compare.
    static bool RateAt(const std::deque<Stamped>& s, double t, double& r) {
        Quat a, b;
        if (!At(s, t - 0.02, a) || !At(s, t + 0.02, b)) return false;
        r = 2.0 * std::acos(std::min(1.0, std::fabs(Dot4(a, b)))) / 0.04;
        return true;
    }

    // The optical stream's lag behind the IMU: the shift that best correlates their rotation rates over the last
    // 4 s (only when the hand was seen throughout, and moved).
    void MeasureLag(double t) {
        const double t0 = t - 4.0, t1 = t - 0.1;
        size_t seen = 0, total = 0;
        for (const Stamped& o : m_opt)
            if (o.t >= t0) { ++total; if (o.seen) ++seen; }
        if (total == 0 || seen < total * 9 / 10) return;
        const double step = 0.005;
        const int n = int((t1 - t0) / step);
        std::vector<double> opt(n);
        for (int i = 0; i < n; ++i)
            if (!RateAt(m_opt, t0 + i * step, opt[i])) return;
        double mean = 0, var = 0;
        for (double v : opt) mean += v;
        mean /= n;
        for (double v : opt) var += (v - mean) * (v - mean);
        if (var / n < 0.3 * 0.3) return;                    // too little motion to time anything
        const int maxShift = int(m_p.maxLag / step);
        // IMU rates once, on the grid extended back by the largest shift: imu[j] is at t0 + (j - maxShift) * step.
        std::vector<double> imuRate(n + maxShift, 0.0);
        std::vector<char> imuOk(n + maxShift, 0);
        for (int j = 0; j < n + maxShift; ++j)
            imuOk[j] = RateAt(m_imu, t0 + (j - maxShift) * step, imuRate[j]);
        std::vector<double> corr(maxShift + 1, -1.0);
        for (int k = 0; k <= maxShift; ++k) {
            double sa = 0, sb = 0, sab = 0, saa = 0, sbb = 0;
            int m = 0;
            for (int i = 0; i < n; ++i) {
                const int j = i - k + maxShift;                 // the IMU at t0 + (i - k) * step
                if (!imuOk[j]) continue;
                const double a = opt[i], b = imuRate[j];
                sa += a; sb += b; sab += a * b; saa += a * a; sbb += b * b;
                ++m;
            }
            if (m < n / 2) continue;
            const double cov = sab / m - (sa / m) * (sb / m);
            const double va = saa / m - (sa / m) * (sa / m), vb = sbb / m - (sb / m) * (sb / m);
            if (va > 0 && vb > 0) corr[k] = cov / std::sqrt(va * vb);
        }
        const int best = int(std::max_element(corr.begin(), corr.end()) - corr.begin());
        if (corr[best] < 0.7) return;
        double shift = best;
        if (best > 0 && best < maxShift) {                  // parabolic refinement between grid points
            const double c0 = corr[best - 1], c1 = corr[best], c2 = corr[best + 1];
            const double den = c0 - 2 * c1 + c2;
            if (den < 0) shift += 0.5 * (c0 - c2) / den;
        }
        const double lag = shift * step;
        m_st.lag = m_haveLag ? m_st.lag + 0.3 * (lag - m_st.lag) : lag;
        m_haveLag = true;
        m_st.lagCorrelation = corr[best];
    }

    // α and M from the pairs: for each heading on a grid, the mean mounting; the heading whose mountings agree
    // best wins, refined on a finer grid. The output stays continuous across a new solution.
    void Solve() {
        // The optical orientations must span enough to separate heading from mounting.
        Quat mean = { 0, 0, 0, 0 };
        for (const Pair& p : m_pairs) {
            const double s = Dot4(p.opt, m_pairs.front().opt) < 0 ? -1 : 1;
            mean = { mean.w + s * p.opt.w, mean.x + s * p.opt.x, mean.y + s * p.opt.y, mean.z + s * p.opt.z };
        }
        mean = Normalize(mean);
        double spread = 0;
        for (const Pair& p : m_pairs) spread = std::max(spread, AngleDeg(p.opt, mean));
        if (spread < m_p.minSpreadDeg) return;

        Quat mount;
        auto fit = [&](double alpha) {
            const Quat pre = YawQ(alpha) * ImuToVr();
            Quat sum = { 0, 0, 0, 0 }, first;
            bool haveFirst = false;
            for (const Pair& p : m_pairs) {
                Quat m = Normalize(Conj(pre * p.imu) * p.opt);
                if (!haveFirst) { first = m; haveFirst = true; }
                if (Dot4(m, first) < 0) m = Neg(m);
                sum = { sum.w + m.w, sum.x + m.x, sum.y + m.y, sum.z + m.z };
            }
            mount = Normalize(sum);
            double cost = 0;
            for (const Pair& p : m_pairs) {
                const double d = Dot4(Normalize(Conj(pre * p.imu) * p.opt), mount);
                cost += 1.0 - d * d;
            }
            return cost;
        };
        double bestC;
        const double bestA = BestHeading(fit, bestC);
        fit(bestA);
        const double residual = Residual(bestC);
        if (residual > m_p.maxResidualDeg) return;
        Apply(bestA, mount);
        m_st.fromPrior = false;
        m_st.residualDeg = residual;
        m_solved = true;
        ++m_st.solves;
    }

    // With the prior mounting, the heading alone: any pairs will do, a still hand included.
    void FitHeading() {
        auto fit = [&](double alpha) {
            const Quat pre = YawQ(alpha) * ImuToVr();
            double cost = 0;
            for (const Pair& p : m_pairs) {
                const double d = Dot4(Normalize(pre * p.imu * m_prior.mount), p.opt);
                cost += 1.0 - d * d;
            }
            return cost;
        };
        double bestC;
        const double bestA = BestHeading(fit, bestC);
        const double residual = Residual(bestC);
        m_st.priorResidualDeg = residual;
        if (residual > m_p.maxPriorResidualDeg) return;
        Apply(bestA, m_prior.mount);
        m_st.fromPrior = true;
        m_st.residualDeg = residual;
    }

    // The heading minimising cost(α): a 5° grid, then 0.5° around the best.
    template <class Cost> static double BestHeading(Cost cost, double& bestC) {
        double bestA = 0;
        bestC = 1e30;
        for (int i = 0; i < 72; ++i) {
            const double a = i * 5.0 * kPi / 180.0;
            const double c = cost(a);
            if (c < bestC) { bestC = c; bestA = a; }
        }
        const double coarse = bestA;
        for (int i = -10; i <= 10; ++i) {
            const double a = coarse + i * 0.5 * kPi / 180.0;
            const double c = cost(a);
            if (c < bestC) { bestC = c; bestA = a; }
        }
        return bestA;
    }

    double Residual(double cost) const {
        return 2.0 * std::asin(std::sqrt(std::max(0.0, cost / m_pairs.size()))) * 180.0 / kPi;
    }

    void Apply(double alpha, const Quat& mount) {
        if (m_st.calibrated && m_haveCorr && !m_imu.empty()) {   // keep the output where it is
            const Quat before = m_corr * Model(m_imu.back().q);
            m_alpha = alpha;
            m_mount = mount;
            m_corr = Normalize(before * Conj(Model(m_imu.back().q)));
        } else {
            m_alpha = alpha;
            m_mount = mount;
        }
        m_st.calibrated = true;
        m_st.alphaDeg = alpha * 180.0 / kPi;
    }

    mutable std::mutex m_lock;
    Params m_p;
    Status m_st{ false, false, 0.03 };
    std::deque<Stamped> m_imu, m_opt;
    std::deque<Pair> m_pairs;
    double m_alpha = 0;
    Quat   m_mount;
    Quat   m_corr;
    Calibration m_prior;
    bool   m_haveCorr = false;
    bool   m_haveLag = false;
    bool   m_havePrior = false;
    bool   m_solved = false;           // a full solve happened: m_mount is this session's
    double m_nextPair = 0, m_nextSolve = 0, m_nextLag = 0, m_lastObserve = 0, m_nextPriorFit = 0;
};

} // namespace cf
