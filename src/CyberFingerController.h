/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// CyberFingerController.h — one CyberFinger hand as a SteamVR controller
//
// Holds the /user/hand/left|right role (high hand-selection priority) and
// exposes the glove's physical inputs plus a 31-bone skeleton. Per frame it
// picks one pose/skeleton source:
//
//   FUSED        CFHS from the Fusion Studio (camera + IMU + EMG)
//   PASSTHROUGH  the headset's own hand tracking (OpticalTap), re-rooted.
//                Each pose and skeleton update of the headset hand is
//                republished as it arrives (OnTapPose / OnTapSkeleton), with
//                its own timing; the frame loop only takes over when those
//                updates stop or during a cross-fade.
//   NO_POSE      nothing tracks the hand: pose invalid, inputs stay live
// ═══════════════════════════════════════════════════════════════════════════

#include <openvr_driver.h>
#include <atomic>
#include <mutex>
#include <string>
#include "BoneData.h"
#include "MathUtil.h"
#include "OpticalTap.h"
#include "PoseFilter.h"
#include "Protocol.h"
#include "SkeletonSynth.h"
#include "StudioLink.h"
#include "TapHold.h"

namespace cf {

class CyberFingerController final : public vr::ITrackedDeviceServerDriver {
public:
    struct Config {
        std::string serial;
        vr::EVRSkeletalTrackingLevel trackingLevel = vr::VRSkeletalTracking_Full;
        int32_t handPriority = 1000;
        double  grabTapTime = 0.2;          // s: a shorter grip press latches /input/grab (0 = never)
        uint8_t maskA = kBtnStartSelect;    // glove buttons driving /input/a/click
        uint8_t maskB = kBtnMenu;           // … /input/b/click (context menu, like Touch B/Y)
        uint8_t maskSystem = 0;             // … /input/system/click (dashboard)
        bool    forwardTapSystem = true;    // headset hand's system button → ours
        double  fusedTimeout = 0.15;        // s
        double  disconnectAfter = 0.0;      // s without any data → report disconnected; 0 = never
        Xform   noSkeletonOffset;           // tap raw pose → our raw pose, when no tap skeleton was ever seen
    };

    CyberFingerController(int hand, const Config& cfg);

    // ── ITrackedDeviceServerDriver ─────────────────────────────────────
    vr::EVRInitError Activate(uint32_t unObjectId) override;
    void Deactivate() override;
    void EnterStandby() override {}
    void* GetComponent(const char*) override { return nullptr; }
    void DebugRequest(const char*, char* buf, uint32_t size) override { if (size) buf[0] = 0; }
    vr::DriverPose_t GetPose() override { return m_pose; }

    // active = false reports the device disconnected (the setting "active"), releasing the hand role.
    void Update(const GloveState& glove, const HandStateSample& fused, const TapHandSnapshot& tap, double now,
                bool active = true);

    // The headset hand's own updates, as they arrive (ServerProvider's republish thread).
    void OnTapPose(const vr::DriverPose_t& pose);
    void OnTapSkeleton(vr::EVRSkeletalMotionRange range, const vr::VRBoneTransform_t* bones, uint32_t count);

    // Poses and skeletons republished per second since the last call.
    void TakeEventRates(double seconds, double& poseRate, double& skeletonRate);

    // Filter for the headset hand's pose in PASSTHROUGH (settings, changeable live).
    void SetPoseFilter(const PoseFilter::Params& params);
    // Whether the current source's poses need it (Steam Link's do; Virtual Desktop's are clean). Frame loop.
    void SetSourceFiltered(bool filtered, const std::string& sourceType);

    const std::string& Serial() const { return m_cfg.serial; }
    uint32_t ObjectId() const { return m_objectId; }
    uint8_t Mode() const { return m_mode; }
    bool Following() const { return m_follow.load(std::memory_order_relaxed); }
    uint32_t AppliedSeq() const { return m_appliedSeq; }
    vr::VRInputComponentHandle_t HapticHandle() const { return m_haptic; }

private:
    bool Right() const { return m_hand == 1; }
    Xform LinkToOurs();                   // tap source's /pose/raw → ours
    void PassthroughPose(const TapHandSnapshot& tap, const GloveState& glove, double now,
                         Xform& raw, Vec3& lin, Vec3& ang, vr::VRBoneTransform_t* bones, float* curls);
    void GloveCurls(const GloveState& glove, bool fresh, float curls[5]) const;
    void SubmitInputs(const GloveState& glove, bool gloveFresh, bool live, const TapHandSnapshot& tap,
                      const float curls[5], const HandGestures& gestures, double now);
    void SubmitSkeleton(const vr::VRBoneTransform_t* bones);

    int       m_hand;
    Config    m_cfg;
    uint32_t  m_objectId = vr::k_unTrackedDeviceIndexInvalid;
    vr::PropertyContainerHandle_t m_container = vr::k_ulInvalidPropertyContainer;
    vr::DriverPose_t m_pose{};

    Xform     m_synthWrist;               // our raw → wrist bone (Index convention)
    std::mutex m_wristLock;               // m_linkWrist, m_haveLinkWrist: also used on the republish thread
    Xform     m_linkWrist;                // tap source's raw → wrist, last seen
    bool      m_haveLinkWrist = false;

    // Event-driven PASSTHROUGH (see OnTapPose / OnTapSkeleton)
    std::atomic<bool>     m_follow{ false };
    std::atomic<double>   m_lastTapPose{ -1e9 };
    std::atomic<double>   m_lastTapSkeleton{ -1e9 };
    std::atomic<uint32_t> m_eventPoses{ 0 };
    std::atomic<uint32_t> m_eventSkeletons{ 0 };

    TapHold   m_grab;

    std::mutex         m_filterLock;      // republish thread vs settings updates
    PoseFilter         m_filter;
    PoseFilter::Params m_filterParams;
    std::atomic<bool>  m_sourceFiltered{ false };   // SetSourceFiltered; off until the source is known
    bool               m_sourceFilterKnown = false; // frame loop only
    vr::VRBoneTransform_t m_prevSourceBones[eBone_Count]{};   // republish thread only
    std::atomic<double>   m_lastSkeletonChange{ -1e9 };

    uint8_t   m_mode = kModeNone;
    uint32_t  m_appliedSeq = 0;
    double    m_lastData = -1e9;
    double    m_lastTapBones = -1e9;      // frame loop: last time the snapshot had a live skeleton

    Xform     m_lastRaw;
    vr::VRBoneTransform_t m_lastBones[eBone_Count]{};
    double    m_blendStart = -1e9;
    Xform     m_blendFromRaw;
    vr::VRBoneTransform_t m_blendFromBones[eBone_Count]{};

    double    m_lastBatteryUpdate = -1e9;
    uint8_t   m_lastBattery = 255;

    enum Bool { kSystem, kA, kB, kC, kD, kE, kTriggerClick, kGripClick, kGrab, kStickClick, kIndexPoint, kNumBool };
    enum Scalar { kTrigger, kGrip, kStickX, kStickY, kFingerIndex, kFingerMiddle, kFingerRing, kFingerPinky,
                  kIndexPinch, kMiddlePinch, kRingPinch, kPinkyPinch, kGrasp, kNumScalar };
    vr::VRInputComponentHandle_t m_bool[kNumBool] = {};
    vr::VRInputComponentHandle_t m_scalar[kNumScalar] = {};
    vr::VRInputComponentHandle_t m_skeleton = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_haptic = vr::k_ulInvalidInputComponentHandle;
};

} // namespace cf
