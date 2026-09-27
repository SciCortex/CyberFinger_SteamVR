/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 * SPDX-FileCopyrightText: Valve Corporation
 *
 * SPDX-License-Identifier: GPL-3.0-only AND BSD-3-Clause
 *
 * SynthesizeSkeleton() and its helpers are ported from the OpenVR SDK sample
 * "handskeletonsimulation" (hand_simulation.cpp), Copyright (c) Valve
 * Corporation, distributed under the BSD 3-Clause license:
 *
 *   Redistribution and use in source and binary forms, with or without
 *   modification, are permitted provided that the following conditions are met:
 *   1. Redistributions of source code must retain the above copyright notice,
 *      this list of conditions and the following disclaimer.
 *   2. Redistributions in binary form must reproduce the above copyright
 *      notice, this list of conditions and the following disclaimer in the
 *      documentation and/or other materials provided with the distribution.
 *   3. Neither the name of the copyright holder nor the names of its
 *      contributors may be used to endorse or promote products derived from
 *      this software without specific prior written permission.
 *
 *   THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
 *   AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
 *   IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
 *   ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
 *   LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
 *   CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
 *   SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
 *   INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
 *   CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
 *   ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
 *   POSSIBILITY OF SUCH DAMAGE.
 */
// ═══════════════════════════════════════════════════════════════════════════
// SkeletonSynth.cpp — procedural hand skeleton (Valve sample port) + utilities
// ═══════════════════════════════════════════════════════════════════════════

#include "SkeletonSynth.h"
#include <algorithm>
#include <utility>

namespace cf {

namespace {

struct SwingTwist { double swing[2] = { 0, 0 }; double twist = 0; };
struct Hinge { double rotation = 0; };
struct SimThumb { SwingTwist metacarpal, proximal; Hinge distal; };
struct SimFinger { SwingTwist metacarpal, proximal; Hinge intermediate, distal; };
struct SimHand { bool right = false; SimThumb thumb; SimFinger fingers[4]; };

// Rough joint lengths (m): thumb, index, middle, ring, pinky.
constexpr double kJointLengths[5][5] = {
    { 0.05, 0.05, 0.035, 0.025, 0.0 },
    { 0.03, 0.073, 0.045, 0.025, 0.02 },
    { 0.01, 0.091, 0.049, 0.03, 0.02 },
    { 0.02, 0.073, 0.045, 0.03, 0.03 },
    { 0.03, 0.067, 0.03, 0.025, 0.02 },
};

Quat FromSwingTwist(const double swing[2], double twist) {
    Quat r;
    const double sq = swing[0] * swing[0] + swing[1] * swing[1];
    const double ct = std::cos(twist * 0.5), st = std::sin(twist * 0.5);
    if (sq > 0.0) {
        const double th = std::sqrt(sq);
        const double cs = std::cos(th * 0.5);
        const double k = std::sin(th * 0.5) / th;
        r.w = cs * ct;
        r.x = cs * st;
        r.y = swing[1] * ct * k - swing[0] * st * k;
        r.z = swing[0] * ct * k + swing[1] * st * k;
    } else {
        const double k = 0.5;
        r.w = ct;
        r.x = st;
        r.y = swing[1] * ct * k - swing[0] * st * k;
        r.z = swing[0] * ct * k + swing[1] * st * k;
    }
    return r;
}

// Rotation about the bone's local Z axis (the sample's FromEulerAngles(r, 0, 0)).
Quat FromHinge(double r) { return { std::cos(r * 0.5), 0, 0, std::sin(r * 0.5) }; }

// The sample builds the left hand and mirrors positions along X for the right.
vr::VRBoneTransform_t MakeBone(bool right, const Quat& q, const Vec3& pos) {
    Vec3 p = pos;
    if (right) p.x = -p.x;
    return XformToBone({ q, p });
}
vr::VRBoneTransform_t MakeBone(bool right, const Quat& q, double length) {
    return MakeBone(right, q, Vec3{ length, 0, 0 });
}

// FBX-style metacarpals are rotated 90° from the wrist, and the right hand's
// "up" axis is flipped (see the SDK sample's ComputeBoneTransformMetacarpal).
vr::VRBoneTransform_t MakeMetacarpal(bool right, const Quat& q, double length) {
    const Quat magic{ 0.5, 0.5, -0.5, 0.5 };
    Quat bone = magic * q;
    const Vec3 pos = Rotate(bone, Vec3{ length, 0, 0 });
    if (right) {
        std::swap(bone.w, bone.x);
        std::swap(bone.y, bone.z);
        bone.x = -bone.x;
        bone.z = -bone.z;
    }
    return MakeBone(right, bone, pos);
}

void InitHand(SimHand& h) {
    for (auto& f : h.fingers) {
        f.metacarpal.swing[1] = 0;
        f.metacarpal.twist = 0;
        f.proximal.swing[1] = DegToRad(10);
        f.intermediate.rotation = DegToRad(5);
        f.distal.rotation = DegToRad(5);
    }
    h.thumb.metacarpal.swing[0] = DegToRad(10);
    h.thumb.metacarpal.swing[1] = DegToRad(40);
    h.thumb.metacarpal.twist = DegToRad(70);
    h.thumb.proximal = SwingTwist{};
    h.thumb.distal.rotation = 0;

    // Metacarpal spread, then proximal spread (these are the joints that splay).
    const double metaSplay[4] = { 13, 0, -15, -27 };
    const double proxSplay[4] = { 3, 0, -1, -2 };
    for (int i = 0; i < 4; ++i) {
        h.fingers[i].metacarpal.swing[1] = DegToRad(metaSplay[i]);
        h.fingers[i].proximal.swing[1] = DegToRad(proxSplay[i]);
    }
}

void ApplyFinger(double curl, double splay, SimFinger& f) {
    f.metacarpal.swing[0] += DegToRad(curl * 5.0);   // the metacarpal only curls a little
    f.proximal.swing[0] += DegToRad(curl * 90.0);
    f.proximal.swing[1] += DegToRad(splay * 15.0);
    f.intermediate.rotation += DegToRad(curl * 80.0);
    f.distal.rotation += DegToRad(curl * 80.0);
}

} // namespace

Xform SynthWristBone(bool right) {
    // Taken from the Index controller pose: places the wrist relative to /pose/raw.
    Xform w{ { -0.055147, -0.078608, -0.920279, 0.379296 }, { -0.034038, 0.036503, 0.164722 } };
    if (right) {
        w.p.x = -w.p.x;
        w.q.y = -w.q.y;
        w.q.z = -w.q.z;
    }
    return w;
}

void SynthesizeSkeleton(bool right, const float curls[5], const float splays[5],
                        vr::VRBoneTransform_t out[eBone_Count]) {
    auto clamp01 = [](float v) { return std::min(1.f, std::max(0.f, v)); };
    auto clamp11 = [](float v) { return std::min(1.f, std::max(-1.f, v)); };

    SimHand h;
    h.right = right;
    InitHand(h);

    const double tc = clamp01(curls[0]), ts = clamp11(splays[0]);
    h.thumb.metacarpal.swing[0] += DegToRad(tc * 5.0);
    h.thumb.metacarpal.swing[1] += DegToRad(ts * 5.0);
    h.thumb.metacarpal.twist = 0;
    h.thumb.proximal.swing[0] += DegToRad(tc * 90.0);
    h.thumb.proximal.swing[1] += DegToRad(ts * 20.0);
    h.thumb.proximal.twist = 0;
    h.thumb.distal.rotation += DegToRad(tc * 90.0);
    for (int i = 0; i < 4; ++i)
        ApplyFinger(clamp01(curls[i + 1]), clamp11(splays[i + 1]), h.fingers[i]);

    out[eBone_Root] = XformToBone({});
    out[eBone_Wrist] = XformToBone(SynthWristBone(right));

    // The sample passes the metacarpal twist to the thumb's proximal joint too.
    out[eBone_Thumb0] = MakeMetacarpal(right, FromSwingTwist(h.thumb.metacarpal.swing, h.thumb.metacarpal.twist), kJointLengths[0][0]);
    out[eBone_Thumb1] = MakeBone(right, FromSwingTwist(h.thumb.proximal.swing, h.thumb.metacarpal.twist), kJointLengths[0][1]);
    out[eBone_Thumb2] = MakeBone(right, FromHinge(h.thumb.distal.rotation), kJointLengths[0][2]);
    out[eBone_Thumb3] = MakeBone(right, Quat{}, kJointLengths[0][3]);

    for (int i = 0; i < 4; ++i) {
        const SimFinger& f = h.fingers[i];
        const double* len = kJointLengths[i + 1];
        const int b = eBone_IndexFinger0 + i * 5;
        out[b + 0] = MakeMetacarpal(right, FromSwingTwist(f.metacarpal.swing, f.metacarpal.twist), len[0]);
        out[b + 1] = MakeBone(right, FromSwingTwist(f.proximal.swing, f.proximal.twist), len[1]);
        out[b + 2] = MakeBone(right, FromHinge(f.intermediate.rotation), len[2]);
        out[b + 3] = MakeBone(right, FromHinge(f.distal.rotation), len[3]);
        out[b + 4] = MakeBone(right, Quat{}, len[4]);
    }

    FillAuxBones(out);
}

void ModelSpace(const vr::VRBoneTransform_t bones[eBone_Count], Xform model[eBone_Count]) {
    for (int i = 0; i < eBone_Count; ++i) {
        const Xform local = BoneToXform(bones[i]);
        model[i] = (kBoneParent[i] < 0) ? local : model[kBoneParent[i]] * local;
    }
}

void FillAuxBones(vr::VRBoneTransform_t bones[eBone_Count]) {
    Xform model[eBone_Count];
    ModelSpace(bones, model);
    for (int f = 0; f < 5; ++f)
        bones[eBone_Aux_Thumb + f] = XformToBone(model[kAuxSource[f]]);
}

Xform SourceWrist(const vr::VRBoneTransform_t* bones) {
    return BoneToXform(bones[eBone_Root]) * BoneToXform(bones[eBone_Wrist]);
}

void ReRootBones(const vr::VRBoneTransform_t* src, const Xform& srcWrist, const Xform& ourWrist,
                 vr::VRBoneTransform_t out[eBone_Count]) {
    std::copy(src, src + eBone_Count, out);
    out[eBone_Root] = XformToBone({});
    out[eBone_Wrist] = XformToBone(ourWrist);
    const Xform fix = ourWrist * Inverse(srcWrist) * BoneToXform(src[eBone_Root]);
    for (int b = eBone_Aux_Thumb; b <= eBone_Aux_PinkyFinger; ++b)
        out[b] = XformToBone(fix * BoneToXform(src[b]));
}

int SkeletonHandedness(const vr::VRBoneTransform_t* bones, uint32_t count) {
    if (count < uint32_t(eBone_PinkyFinger4 + 1)) return 0;
    double sum = 0;
    for (int f = 1; f < 5; ++f)                       // proximal .. tip of the four fingers
        for (int k = 1; k <= 4; ++k)
            sum += bones[kFingerFirstBone[f] + k].position.v[0];
    if (sum > 0.02) return -1;                        // bones extend along +X: left hand
    if (sum < -0.02) return +1;
    return 0;
}

void CurlsFromBones(const vr::VRBoneTransform_t bones[eBone_Count], float curls[5]) {
    Xform model[eBone_Count];
    ModelSpace(bones, model);
    for (int f = 0; f < 5; ++f) {
        const int first = kFingerFirstBone[f];
        const int n = (f == 0) ? 4 : 5;               // joints in the chain
        double bend = 0;
        for (int k = 1; k + 1 < n; ++k) {
            const Vec3 a = model[first + k].p - model[first + k - 1].p;
            const Vec3 b = model[first + k + 1].p - model[first + k].p;
            const double la = Length(a), lb = Length(b);
            if (la < 1e-6 || lb < 1e-6) continue;
            bend += std::acos(std::max(-1.0, std::min(1.0, Dot(a, b) / (la * lb))));
        }
        // A fully curled Valve hand bends ~250° (fingers) / ~180° (thumb) over the chain.
        const double full = (f == 0) ? DegToRad(180) : DegToRad(250);
        curls[f] = float(std::max(0.0, std::min(1.0, bend / full)));
    }
}

float PinchFromDistance(double d) {
    constexpr double kClosed = 0.02, kOpen = 0.06;   // m, between tip bone origins
    return float(std::max(0.0, std::min(1.0, (kOpen - d) / (kOpen - kClosed))));
}

HandGestures GesturesFromBones(const vr::VRBoneTransform_t bones[eBone_Count]) {
    Xform model[eBone_Count];
    ModelSpace(bones, model);
    HandGestures g;
    const Vec3 thumbTip = model[eBone_Thumb3].p;
    for (int f = 0; f < 4; ++f)
        g.pinch[f] = PinchFromDistance(Length(model[kFingerFirstBone[f + 1] + 4].p - thumbTip));
    float curls[5];
    CurlsFromBones(bones, curls);
    g.grasp = (curls[1] + curls[2] + curls[3] + curls[4]) / 4.f;
    g.indexPoint = curls[1] < 0.35f && std::min({ curls[2], curls[3], curls[4] }) > 0.6f;
    return g;
}

} // namespace cf
