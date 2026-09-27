/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 * SPDX-FileCopyrightText: Valve Corporation
 *
 * SPDX-License-Identifier: GPL-3.0-only AND BSD-3-Clause
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// SkeletonSynth.h — procedural SteamVR hand skeletons and skeleton utilities
//
// Synthesize() is a port of Valve's OpenVR SDK sample "handskeletonsimulation"
// (samples/drivers/drivers/handskeletonsimulation/src/hand_simulation.cpp),
// which builds bones with the conventions SteamVR and applications expect.
// The skeleton root is /pose/raw and the wrist bone carries the raw → wrist
// offset of an Index controller.
// ═══════════════════════════════════════════════════════════════════════════

#include <openvr_driver.h>
#include "BoneData.h"
#include "MathUtil.h"

namespace cf {

// curls: thumb..pinky, 0 open .. 1 closed. splays: -1 .. 1.
void SynthesizeSkeleton(bool rightHand, const float curls[5], const float splays[5],
                        vr::VRBoneTransform_t out[eBone_Count]);

// The synthesized skeleton's wrist bone (raw → wrist), identical for every pose.
Xform SynthWristBone(bool rightHand);

// Model-space (root-relative) transforms by forward kinematics.
void ModelSpace(const vr::VRBoneTransform_t bones[eBone_Count], Xform model[eBone_Count]);

// Overwrites the aux bones from the finger chains.
void FillAuxBones(vr::VRBoneTransform_t bones[eBone_Count]);

// Where a skeleton puts the wrist relative to its /pose/raw (root · wrist bone).
Xform SourceWrist(const vr::VRBoneTransform_t* bones);

// Re-roots another driver's skeleton onto our /pose/raw, whose wrist bone is ourWrist. With
// raw_ours = raw_src · srcWrist · ourWrist⁻¹ every bone keeps its world transform: the finger chains
// hang off the wrist and carry over unchanged, and the root-parented aux bones move with the root.
void ReRootBones(const vr::VRBoneTransform_t* src, const Xform& srcWrist, const Xform& ourWrist,
                 vr::VRBoneTransform_t out[eBone_Count]);

// +1 if the skeleton follows the right-hand convention, -1 for left, 0 if unclear.
// SteamVR mirrors the hands: finger bones extend along +X of their parent on the
// left hand and along -X on the right.
int SkeletonHandedness(const vr::VRBoneTransform_t* bones, uint32_t count);

// Per-finger curl estimate (0 open .. 1 closed) from joint bend angles.
void CurlsFromBones(const vr::VRBoneTransform_t bones[eBone_Count], float curls[5]);

// Hand-tracking gestures from a skeleton, for when the headset's own gesture
// values are unavailable. pinch[i]: thumb tip to index/middle/ring/pinky tip,
// 0 (>= 6 cm apart) .. 1 (<= 2 cm). grasp: mean curl of the four fingers.
struct HandGestures {
    float pinch[4] = {};
    float grasp = 0.f;
    bool  indexPoint = false;
};
HandGestures GesturesFromBones(const vr::VRBoneTransform_t bones[eBone_Count]);
float PinchFromDistance(double metres);

} // namespace cf
