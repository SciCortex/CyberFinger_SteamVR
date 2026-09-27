# CyberFinger Fusion → SteamVR: implementation plan

*Draft for review, 2026-09-25; status and todo updated 2026-09-26. Covers the SteamVR driver (`src/`, `resources/`)
and the Fusion Studio (`bridge/`).*

**→ [Todo list](#todo)**

## Status (2026-09-26, branch `steamvr-driver-v2`)

**Phase 1a is implemented and works in the headset** with Steam Link and Virtual Desktop, in Resonite and the
SteamVR dashboard (committed: `5f1a46c`, `6194a05`, pushed). The driver side of Phase 1b is also in: the `CFHS`
receiver with the FUSED mode, and the `CFOP` context stream. The Studio side of 1b (§7.1, §7.3, §7.4) is next,
with a simple driver-side IMU fusion before it (Todo).

What the survey of the installed SteamVR, VRChat and Resonite files settled:

| Item | Finding |
|---|---|
| S1 roles | Steam Link's hand devices (`VRLINKQ_Hand_Left/Right`, type `svl_hand_interaction_augmented`) declare `hand_priority: -1`, so CyberFinger (1000) takes the roles |
| Load order | Higher `loadPriority` loads first (Steam Link: 110). CyberFinger uses 2000, so its hooks are in place before Steam Link creates its hand components. Virtual Desktop loads before us regardless |
| Hook layout | `IVRDriverInput_003` and `_004` share vtable slots 0–6; one set of MinHook detours covers both |
| ≡ pinch | Arrives as `/input/system/click` on Steam Link's left hand, bound by role in the compositor binding. The driver forwards it to CyberFinger's left `/input/system` |
| Q1 buttons | MENU (the context / rotary-dial button) → B/Y, Start/Select (black wrist button) → A/X, C/D/E → `/input/c,d,e` for per-app binding. Grab is tap to hold (`/input/grab`) in the VRChat and Resonite defaults |
| Binding UI image | SteamVR has one `binding_image_point` per input, in right-image pixels, mirrored for the left hand. So both hands show the right render, the left with `"transform": "scale(-1,1)"` (as Valve does for the Vive wand); `tools/preview_bindingui.py` draws the points |
| Pink wrist button | The PMU power button (IO expander pin 4). The firmware ignores short presses (`TODO emit another button press event`) and the VR report's 8 button bits are all used: exposing it needs a firmware bit and a `CFG2` field (a reserved byte is free) |
| Quest OS button | Handled by Horizon OS; under Steam Link it never reaches SteamVR, and a driver cannot press it |
| Gestures | The standard hand-tracking set (pinches, grasp, index point) is exposed but unbound, except thumb-pinky pinch = MENU. Values come from Steam Link's own gestures (hooked) or from the skeleton |
| Haptics | Driver → bridge `CFHP` (36 B, UDP 27016) → glove `VR_CMD_HAPTIC` on `0xCF02` (6 B, write without response; `bridge/glove_control.py`, firmware 1.3.3 `src/haptics.cpp`: DRV2605L real-time drive of the ERM motor). VR mode only |
| VRChat binding | Derived from VRChat's own Touch binding (`tools/generate_bindings.py`), with touch and gesture inputs removed |
| Resonite | Picks its controller mode from the render model of the devices it registers as hands; only its Touch mode has a dash. The default binding emulates an Oculus Touch controller, with pose and skeletons bound (in Touch mode Resonite undoes its Touch offset for the hand): black button = dash, B = context menu. The streamers' own Touch controllers are marked never tracked while CyberFinger holds the hands, or Resonite registers them instead or shows them as trackers |
| Streamers | Steam Link streams predicted, noisy hand poses (broadband 8–40 Hz); Virtual Desktop's are clean. Both present Oculus-identity Touch controllers besides the hand devices; Steam Link creates its native controller devices (`VRLINKQ3_Controller_*`) only when they're first picked up |
| `CFG2` size | 34 bytes, not ~36 as estimated in §5 |

Still open: Q4 (left-arm EMG), Q5 (render model: a small wrist marker for now), Q6. Q2 answered for Steam Link and
Virtual Desktop.

## Todo

Collected 2026-09-26, after the first headset tests, from `notes_steamvr_driver.txt` and the follow-up findings.
Items marked *check* are hypotheses to verify.

### Tracking quality

- [x] **Noisy hand tracking.** Two causes. (1) The driver resampled Steam Link's hand once per frame: now each
  pose and skeleton is republished the moment it's submitted (hook on `TrackedDevicePoseUpdated`; `mode=2
  (event)`, 360/360 Hz). (2) Steam Link's pose stream itself is a predictor output with broadband 8–40 Hz noise
  and noisy velocities (Virtual Desktop's is clean). Pose filter `src/PoseFilter.h` (One Euro + glitch gate),
  tuned offline with `filter_eval` on captures; the biggest win is not extrapolating rotation
  (`pose_rotation_prediction` 0). Applies to Steam Link only (`pose_filter_types`); Virtual Desktop passes through.
  Confirmed live: Steam Link "way better", Virtual Desktop low-noise.
- [ ] **Driver-side IMU fusion** until the Studio's full fusion (sEMG and more) arrives: body IMU (body 1/2) to
  steady the position, joint IMU for the orientation, and dead reckoning from the IMUs while the hands are out of
  view. The glove IMUs are ~3× quieter than the optical rotation and lead it by ~40 ms (capture 2026-09-26).
  **Orientation done** (2026-09-27, `src/ImuFusion.h`, setting `imu_fusion`): the bridges stream `CFIM` always;
  the driver solves the joint IMU's heading and mounting (Studio's `mount_calib` model) and the optical lag from
  slow-turning, seen samples, then drives the orientation from the IMU with a slow optical correction. Offline
  (`fusion_eval`): agrees with the headset to 0.6–1.9° at rest; through simulated occlusions 5–20° off vs 13–76°
  for holding the last pose. No switch message needed: the Studio's fusion (FUSED mode) bypasses it.
  Live (Steam Link, 2026-09-27): CyberFinger's rotation follows the IMU (+2 ms) while the headset's trails it by
  ~90 ms; laser jitter left 11.3 → 5.0 mm still, 64.5 → 21.8 mm fast.
  **Cold start** (2026-09-27): the mounting and lag are saved per hand and source
  (`%LOCALAPPDATA%\CyberFinger\imu_calibration.txt`, averaged over sessions; built-in reference default) and
  the fusion starts from them, fitting only the heading: running 0.5–0.8 s after the hand is seen (was 4–5 s
  offline, 20–60 s live with still hands). Between sessions the solved mounting scatters 14–21°, mostly tilt,
  not the heading ambiguity: the glove's fit, or the optical hand frame's posture bias. *Check live.*
  **Lag estimate** (2026-09-27): live it swung 30–97 ms (single windows landing on 0 or the largest shift,
  averaged in). Now the windows' correlation curves are averaged (15 s memory) and, once calibrated, the angular
  velocity vectors are compared instead of the rates (which read 10–25 ms late). Offline its correction wobble
  matches the best fixed lag in hindsight on all captures. Steam Link's lag differs by session (10–100 ms) and
  drifts within one (90 → 25 ms over 10 s). *Check live.*
  **Trust and gate** (2026-09-27, `src/TrackingTrust.h`): a 2-minute capture with exits showed Steam Link rarely
  loses the hands (losses 0.2 s median, 1 s max; it tracks them down at the sides) but tracks them badly in places:
  more than 20° off the IMU 68 % of the time with the hand 10–20° behind the other, 17–43 % beside/behind the
  head, lost overhead. Trust (view direction × other hand in front × distance) slows the corrections and keeps poor views
  out of the calibration; a 25° gate refuses flips and glitches, with an escape after 3 s of refusing every trusted
  view (glove slipped). On that capture the corrections turn the output 50–64 % less, worst cases improve (p90
  15.9 → 11.0°); older captures unchanged. *Check live.*
  - [ ] Trust maps for other headsets (Quest 2, Quest Pro, Quest 3S, Steam Frame, …): a 2-minute capture each,
    `tools/handover_check.py`; then a per-headset table chosen by the headset's model.
  - [ ] The hand's own orientation to the cameras: oblique views 2–3× worse in the first capture, not consistent
    across axes yet. More data.
  - [ ] **Position through gaps and bad views: the arm chain** (`tools/arm_chain_check.py`). SlimeVR elbow tracker
    (upper-arm Slime) + forearm (glove body IMU) + hand (joint IMU): over 5–8 s gaps 12–16 cm median off against
    39–42 cm for holding the position (without the Slime, elbow held to the headset: 23–30 cm). The accelerometer
    only helps for ≤ 0.5 s. Next: fit the chain online, hand over position by the same trust and gate.
- [ ] **Skeleton simpler than OpenVR's.** Probably the same cause as the rigid hands seen later: in Touch mode
  without skeletons bound, Resonite draws canned Touch hands. *Check* now that the default binds them.
- [ ] Dashboard laser: Steam Link draws its own stabilised pointer on the headset. Option: hand the roles back to
  Steam Link while the dashboard is open. Low priority now that the filter works.

### Devices and rendering

- [ ] **Red dot at the wrist** in the SteamVR dashboard: the placeholder render model (pink sphere,
  `tools/make_rendermodels.py`). Replace it with a glove model or make it invisible (Q5).
- [ ] **Hand skeleton in the SteamVR dashboard**, as Steam Link's own hands show.
- [x] **Vive tracker shown in Resonite.** The streamers' Touch controllers lose the hand roles to CyberFinger and
  Resonite maps role-less controllers as trackers. Fixed: marked `Prop_NeverTracked` while CyberFinger holds their
  hand (`hide_other_hand_controllers`); devices added later (Steam Link's controllers) are hidden within a frame.
  *Check* on Steam Link that the first pickup no longer leaves a tracker.
- [x] **Handoff to the Touch controllers** (`yield_to_controllers`): when a hand's skeleton stops while a controller
  for it is tracked, CyberFinger releases the hand (~0.5 s); it takes it back as soon as hand tracking is live.
  Round trips tested on Virtual Desktop and Steam Link. *Check* the faster rule on Steam Link.

### Buttons and bindings

- [x] Pink buttons (firmware 1.3.3, the report's extension byte): left → SteamVR dashboard (driver), right →
  Windows microphone mute (bridge, VR mode). Hand-tracking gestures off by default: no palm-pinch forward
  (`forward_tap_system_button` false), no thumb-pinky pinch in the default bindings. *Check live.*
- [x] Black button long press (`black_hold_ms`, 800) → `/input/a_hold`, held while the button stays down; Resonite's
  default binds it to FluxAction1 (left) / FluxAction2 (right). *To check:* SteamVR still loads Resonite's binding
  when the FluxActions set is missing (mod not installed).
- [ ] **MoreFluxActions** (`~/src/MoreFluxActionsMod`, BepInEx): the renderer half adds FluxAction1–42 to Resonite's
  SteamVR manifest and reads them; the engine half fires `FluxActionN.Pressed` / `.Released` / `FluxActionN`
  (bool) dynamic impulses under the local user. Both halves build; *test in game*. Next: the AI tool
  (pyresonitelink) that writes ProtoFlux for the actions and deploys it under the avatar.

- [x] Tap to hold (CyberFingerMod style): `/input/grab` is the grip with a driver-side latch. A press shorter
  than `grab_tap_ms` (200) holds until the next press, a longer one grabs while held; the VRChat and Resonite
  defaults bind grab to it. SteamVR's *Toggle Button* can't do this (it flips on every press).
- [x] Binding UI image showing the wrist module: the right render for both hands, mirrored for the left, with
  the measured points (`tools/preview_bindingui.py`).
- [x] **Black wrist button (Start/Select) → Resonite dash**, via Touch emulation in the default Resonite binding.
  Works on Virtual Desktop; *check* on Steam Link.
- [x] Defaults picked up: the Resonite and dashboard defaults are regenerated from `tools/generate_bindings.py`;
  the dashboard gains right click on the plain grip (Index uses the trackpad). User binding selections for Resonite
  and the dashboard, and two broken `vd_hand_controller` selections (empty workshop items, which made Resonite
  open the binding UI on every start under Virtual Desktop), were removed from `steamvr.vrsettings` (backups kept).
- [ ] Binding UI: check that the leader lines are visible over the new image (they weren't over the photo).
- [ ] Unused left render `resources/icons/steamvr_cybrfngr_left_transp.png`: archive or delete (it ships with
  the driver).
- [ ] **Pink wrist button (power button, short press) → OS.** The Quest OS button can't be triggered from SteamVR.
  - [ ] Firmware: emit a bit on short press (`main.cpp`, `// TODO emit another button press event`). The VR
    report's 8 button bits are full, so this needs another byte.
  - [ ] Bridges: read it and send it in a `CFG2` reserved byte.
  - [ ] Driver: left → `/input/system` (SteamVR dashboard); right → a bindable button.
  - [ ] Then default the Quest palm pinch → dashboard (`forward_tap_system_button`) to off? The note says
    "remove the os pink by default": confirm what it means.
- [ ] **Custom buttons and gestures → ProtoFlux in Resonite** (e.g. right pink button → launch an AI voice
  assistant), without overriding the default binding behaviour. Options: spare Touch-set inputs under emulation
  read from ProtoFlux, a mod that adds actions, or the bridge → ResoniteLink. A question to the Resonite community
  is drafted in the notes.

### Resonite grabbing (in Resonite)

- [ ] Proximity grab: close the bottom three fingers (middle, ring, pinky).
- [ ] Precision grab: any finger to the thumb.

### Platforms

- [x] **Virtual Desktop.** SteamVR's built-in Oculus driver provides the headset and Touch controllers through
  VD's injector; VD adds its own hand devices `HANDL`/`HANDR` (type `vd_hand_controller`), found by controller type
  (`tap_controller_types`). VD hooks the same driver-host calls, so our hooks only queue and a CyberFinger thread
  republishes (the first try, republishing inside the hook chain, hung vrserver). Works: clean hands at 90 Hz,
  binding, handoff.

### Studio and glove

- [x] **Second glove IMU missing** in both bridges: firmware v1.3 sends a variable-length report (absent IMU slots
  omitted), which the bridges took for the old one-quaternion report. `bridge/glove_report.py` decodes every
  revision; the Fusion Studio's wrist slot falls back to body 1 when body 2 isn't fitted.
- [x] Firmware: the body IMU (the QMI, onboard; the ICM body is extra hardware) lagged the joint IMU by 31–38 ms.
  Its LPF (widest mode, 13.37% of ODR) sat at ~15 Hz: now 448 Hz ODR (~60 Hz), every sample read from the FIFO
  into VQF, Wire buffer 1024 (the FIFO read overran the 128-byte default: boot loop). Firmware 1.3.2-beta, both
  gloves: 3–5 ms behind the joint IMU (capture 2026-09-27); rotation jitter at rest 0.03° → 0.04–0.06°.
- [ ] Phase 1b, Studio side: §7.1 split compute from drawing, §7.3 `SteamVRContextSource`, §7.4 `SteamVROutput`.
- [x] Haptics to the glove over GATT (2026-09-27): both bridges forward the driver's requests in VR mode
  (`glove_control.py`, coalesced per hand); firmware 1.3.3 drives the motor from the main loop (the DRV2605L
  shares I2C with the IMUs). *Check live:* the feel of the ERM constants (35 ms minimum pulse, drive 40–127).

### Release and repos

- [x] Commit `steamvr-driver-v2` (`5f1a46c`, `6194a05`, pushed).
- [ ] `CyberFinger_SteamVR` is a second clone of the same repository: retire it (its uncommitted copies are all in
  the branch), or replace it with a `git worktree` of `main`. No more copying files between folders.
- [ ] Pull request `steamvr-driver-v2` → `main` (brings the Fusion Studio too, merged in PR #1).
- [ ] Retire `origin/steamvr` (July): superseded (its variable-length IMU parser is `glove_report.py` now, its
  `vr_controller.py` and driver changes target the v1 protocol and `MergedController`). Tag it before deleting.
- [ ] Try the installer here: upgrade 1.4.1 → 2.0.0 in place, dev registration replaced.
- [ ] **Instruction manual:** revise and expand once the current work settles (pink buttons, haptics, the black
  button's long press and FluxActions, the IMU fusion, the arm chain). Source `docs/manual/manual.html` (labelled
  glove images, callouts in the images' pixel coordinates); `python docs/manual/build_manual.py [--single-file]`
  rebuilds the standalone page. First version published as a private claude.ai page (2026-09-27).
- [ ] **Web version of the manual:** host it (e.g. GitHub Pages from `docs/`), link it from the README, the
  installer and the bridge.

## 1. Goal

Each hand appears to SteamVR applications as one controller in the `/user/hand/left` or `/user/hand/right` role:

| Channel | Source |
|---|---|
| 6-DoF pose | Fusion Studio wrist pose: camera while the hand is in view, glove IMUs + position model when it is not |
| 31-bone hand skeleton | Fusion Studio hand: camera fingers where seen, EMG regressor or recognised key posture elsewhere |
| Inputs | The glove's controls, per hand: push-stick (x, y, click), analog trigger, grip, 2–3 buttons. No touchpad |

Applications must not see a second pair of hands from Steam Link.

| Application | Device identity it sees | Why |
|---|---|---|
| VRChat | CyberFinger (native) | VRChat does not support controller emulation and detects it; it animates fingers from any device's skeleton |
| Resonite | CyberFinger (native) | Grab and precision pinch will be implemented in Resonite from the skeleton |
| All other apps | Valve Index controller, emulated through SteamVR bindings | Many titles only enable skeletal input for `knuckles` |

**Input policy (decided):** buttons for everything. The hand skeleton drives finger animation only. The one
exception is grab and precision grab in Resonite, which are built in Resonite from the skeleton. The driver turns
no hand shapes into inputs. (VRChat's own pinch controls come on at the default tracking level `Full`; D2.)

## 2. Where things stand

### Already working

- The driver registers two controllers, `CYBERFINGER_L`/`_R`, listens on UDP 27015, copies the pose of Steam Link's
  hand devices ([MergedController.cpp:429](src/MergedController.cpp#L429), serial match `Hand_Left`/`Hand_Right`)
  and synthesizes a skeleton from button state ([SkeletonComposer.cpp](src/SkeletonComposer.cpp)).
- The Studio's glove connection already sends a `CFGP` button/stick packet to the driver on every BLE report
  (`VRMode`, [fusion_studio.py:873](bridge/fusion_studio.py#L873)).
- The fusion (`FusionStudioApp._st_tick`, [fusion_studio.py:3012](bridge/fusion_studio.py#L3012)) already computes
  everything the driver needs: wrist position `S["wrist"]`, hand rotation `S["hand_R"]`, 26 joints in the hand frame
  `S["hand_local"]`, per-finger `S["curls"]`, the key posture, and provenance (`src_pos`, `src_R`, `src_pose`).

### Gaps

| # | Gap | Evidence |
|---|---|---|
| G1 | The fusion is tied to the UI and to one hand. It returns early unless the Fusion tab is visible, runs only for `_gest_hand` (right), and its result only goes to `pt.draw` | [fusion_studio.py:3014](bridge/fusion_studio.py#L3014), [:2255](bridge/fusion_studio.py#L2255), [:3314](bridge/fusion_studio.py#L3314) |
| G2 | Nothing carries the fused pose or skeleton to the driver; buttons are the only Studio → driver traffic | [fusion_studio.py:873](bridge/fusion_studio.py#L873) |
| G3 | The optical source can't run beside a game, and would loop back. The Studio opens its own OpenXR session, a scene application, and SteamVR runs one at a time (the Studio already juggles this between its own two sources, [fusion_studio.py:2647](bridge/fusion_studio.py#L2647)). SteamVR Input serves skeletons by hand role, and SteamVR's OpenXR hand tracking is expected to follow the same devices. Once CyberFinger holds the roles, the Studio would read back its own output | [openxr_skeleton_provider.py](bridge/openxr_skeleton_provider.py), `OpenVRSkeletonSource` |
| G4 | Button layout mismatch. Firmware and Studio use the 8-bit layout (TRIG, GRIP, C, D, E, MENU, JCLK, STSEL); the driver decodes the old 5-bit one (bit2 = B, bit3 = joy click, bit4 = A). MENU, JCLK and STSEL are ignored and C, D and E are misread. The stick is also sent uncentred | [fusion_studio.py:186-193](bridge/fusion_studio.py#L186-L193) vs [MergedController.cpp:228-232](src/MergedController.cpp#L228-L232) |
| G5 | Apps can't use the skeleton. Only `WithController` is updated, but VRChat reads `WithoutController`. The rest pose is a placeholder, the aux bones are parented to the wrist instead of the root, and the rest pose is passed as the grip limit | [MergedController.cpp:480](src/MergedController.cpp#L480), [:135-143](src/MergedController.cpp#L135-L143); [BoneData.h:87-91](src/BoneData.h#L87-L91), [:106](src/BoneData.h#L106), [:159](src/BoneData.h#L159) |
| G6 | Device identity. The render model `indexcontroller_left` doesn't exist. The profile declares trigger and grip as buttons while the driver creates scalar values. `/pose/raw`, finger and haptic entries are missing. There are no default bindings, no remapping and no hand-selection priority (hence the `TrackingOverrides` workaround in the README) | [MergedController.cpp:93](src/MergedController.cpp#L93); [cyberfinger_profile.json:36,43](resources/input/cyberfinger_profile.json#L36); [README.md:83](README.md#L83) |
| G7 | With no source, the pose falls back to a hand floating 30 cm in front of the user, reported as valid | [MergedController.cpp:405-420](src/MergedController.cpp#L405-L420) |
| G8 | Hygiene. CMake copies `settingsschema.vrsettings`, which isn't in the repo. The UDP socket binds all interfaces, so any host on the LAN can inject input | [CMakeLists.txt:114](CMakeLists.txt#L114); [HandTrackingReceiver.cpp:101](src/HandTrackingReceiver.cpp#L101) |

## 3. Decisions

**D1 — Native `cyberfinger` device; Index emulation only through bindings.** Keep controller type `cyberfinger`
with our own input profile and render model. Emulation is a per-application binding option
(`simulated_controller_type`) that SteamVR applies only to that application; the device stays itself in the
dashboard, in VRChat and in Resonite. Valve's driver docs recommend this over declaring `knuckles` (SDK docs,
"Notes on hand tracking compatibility" and "Application Compatibility").
- VRChat and Resonite get explicit default bindings with no emulation. VRChat's driver guide says emulation is
  unsupported and detected, and that the older "VRChat requires `knuckles`" advice (still in Valve's docs) no longer
  holds. VRChat retargets `WithoutController` bone rotations from any device.
- Every other application gets Index emulation by default, through SteamVR Automatic Rebinding (a `remapping` file,
  SteamVR ≥ 1.26) plus an emulating legacy binding (§9).

**D2 — Skeletal tracking level `Full` (revised 2026-09-26).** Resonite and generic apps come first, VRChat later.
The hands are camera-tracked, so `Full` is the honest level, and it is what Steam Link reports for them. Resonite
ignores the level (its renderer builds the hand from the skeleton the same way for every controller type). VRChat
uses the skeleton as an input device (pinch gestures for menu, locomotion, jump, mic) only at `Full`; setting
`skeletal_tracking_level` to `partial` keeps that off. The level is fixed when the skeleton component is created,
so it is a startup setting. (Originally `Partial`, chosen for VRChat.)

**D3 — The driver is the only SteamVR integration point.** While a game runs, the Studio opens no XR session. The
driver taps the headset's optical hand data inside vrserver: Steam Link's hand-device poses through
`GetRawTrackedDevicePoses`, and their skeletons through a hook on `IVRDriverInput::UpdateSkeletonComponent` (§6.6).
It streams that data and the HMD pose to the Studio (the *context stream*), and the Studio sends the fused hand back
(the *hand-state stream*). Everything stays in SteamVR raw tracking space, so no frame alignment is needed and the
G3 loop cannot form. The OpenXR provider remains for development without SteamVR output, and for Linux.

**D4 — Take the hand roles by priority.** Set `Prop_ControllerHandSelectionPriority_Int32` above that of Steam
Link's hand devices. They stay alive as the tap source, but apps stop binding them: per the SDK docs, only one active
controller holds each hand role. Drop `TrackingOverrides`.

**D5 — All pose and skeleton math in the Studio; a thin driver.** The Studio sends a finished `/pose/raw` and 31
bones. The driver validates, predicts and times out, drives the inputs, and keeps a small C++ fallback skeleton for
when the Studio isn't running: a port of Valve's `handskeletonsimulation` sample, BSD-3. The fusion work happens in
Python, a driver change costs a SteamVR restart, and a driver crash takes vrserver down with it.

**D6 — The skeleton in two stages.** Stage A (Phase 1): curls + splay → bones through the procedural hand of Valve's
SDK sample, which has SteamVR's bone conventions by construction. Stage B (Phase 2): full retarget of the 26 fused
joints onto the SteamVR reference skeleton. Stage B is what makes fingertips meet in a pinch, which Resonite's
precision grab needs.

**D7 — `/pose/raw` follows the Index controller's frame relative to the hand.** The skeleton root sits at
`/pose/raw` and the wrist bone carries the raw → wrist offset. We use the Index offset from the SDK sample, so
Index-emulated applications that apply Index hand offsets place the hand correctly, and our render model can copy the
Index's pose locators (aim, grip, tip).

**D8 — Buttons stay on their own low-latency stream, versioned.** The Studio sends one per BLE report, independent
of the fusion tick. A new magic `CFG2` carries the 8-bit layout; the driver keeps decoding the legacy `CFGP` from the
old CLI bridge.

## 4. Architecture

```
Quest headset ──Steam Link──► SteamVR (vrserver.exe)
                               │
   ┌───────────────────────────┴─────────────────────────────┐
   │ Steam Link driver: hand devices "…Hand_Left/Right"       │  keep running, lose the hand roles
   └───────────────┬─────────────────────────────────────────┘
                   │ raw poses (GetRawTrackedDevicePoses) + skeletons (hooked UpdateSkeletonComponent)
   ┌───────────────▼─────────────────────────────────────────┐
   │ driver_cyberfinger                                       │
   │   OpticalTap ─────── CFOP context (UDP :27016) ──────────────────► Fusion Studio
   │   CyberFinger L/R ◄─ CFHS fused hand + CFG2 glove (UDP :27015) ◄── (glove BLE, EMG,
   │   (hand roles: pose, skeleton, stick, buttons)           │          SteamVRContextSource,
   └───────────────┬─────────────────────────────────────────┘          fusion → SteamVROutput)
                   ▼
   /user/hand/left|right ──► VRChat (native) · Resonite (native) · other apps (Index-emulated)
```

The driver runs one output mode per hand:

| Mode | When | Pose | Skeleton |
|---|---|---|---|
| FUSED | a `CFHS` with `POSE_VALID` younger than `fused_timeout_ms` (150) | `CFHS` pose, predicted | `CFHS` bones |
| PASSTHROUGH | no fresh `CFHS`, tap pose valid | Steam Link hand pose, re-expressed in our raw convention (§8.6) | tap bones, re-rooted; fallback synth if the hook is off |
| NO_POSE | neither | `poseIsValid = false`, `deviceIsConnected = true` (inputs keep working) | fallback synth from buttons |
| RELEASED | nothing at all for `disconnect_after_ms` (2 s), if S1 shows the roles then fall back to Steam Link | `deviceIsConnected = false` | — |

Switches between FUSED and PASSTHROUGH blend over 100 ms. In Phase 1 only the right (EMG) hand is fused; the left
hand runs PASSTHROUGH until per-hand fusion lands in Phase 2.

## 5. Wire protocol v2

Little-endian, packed, loopback only. All new packets share one header:

```c
#pragma pack(push, 1)
struct CfHeader {             // 24 bytes
    uint32_t magic;           // 'CFG2' 0x32474643 | 'CFHS' 0x53484643 | 'CFOP' 0x504F4643
    uint8_t  version;         // 1
    uint8_t  hand;            // 0 left, 1 right, 0xFF n/a
    uint16_t flags;
    uint32_t seq;             // per sender, per hand
    uint32_t age_us;          // age of the newest sensor sample behind this packet, at send time
    uint64_t t_send_us;       // sender's monotonic clock; diagnostics only, never compared across processes
};

struct CfBone {               // same layout as vr::VRBoneTransform_t
    float px, py, pz, pw;
    float qw, qx, qy, qz;
};

// CFG2 — glove input, Studio → driver, UDP 27015, on every BLE report
struct CfGlove {
    CfHeader h;
    uint8_t  buttons;         // TRIG 0x01 GRIP 0x02 C 0x04 D 0x08 E 0x10 MENU 0x20 JCLK 0x40 STSEL 0x80
    uint8_t  trigger;         // 0..255
    int16_t  joy_x, joy_y;    // centred + radial deadzone applied in the Studio, ±32767, +y up
    uint8_t  battery_pct;
    uint8_t  reserved[3];
};

// CFHS — fused hand state, Studio → driver, UDP 27015, every fusion tick (target 90 Hz)
struct CfHandState {
    CfHeader h;               // flags: POSE_VALID 0x1, HAS_BONES 0x2, CAMERA_SEES 0x4, CALIBRATED 0x8
    float    raw_pos[3];      // /pose/raw in SteamVR raw space, metres
    float    raw_rot[4];      // w, x, y, z
    float    lin_vel[3];      // m/s, raw space
    float    ang_vel[3];      // rad/s, axis-angle, raw space
    float    curl[5];         // thumb..pinky, 0 open .. 1 closed (also feeds /input/finger/*)
    float    splay[5];        // -1 .. 1
    float    pose_conf;       // 0..1
    float    finger_conf[5];  // 0..1, camera visibility per finger
    uint32_t optical_seq;     // seq of the CFOP frame this state used, 0 if none
    uint8_t  pos_src, rot_src, pose_src, key_posture;   // enums mirroring S["src_pos"], S["src_R"], S["src_pose"], kp
    CfBone   bones[31];       // parent space; written to both motion ranges
};

// CFOP — context, driver → Studio, UDP 27016, ≤ 120 Hz from RunFrame
struct CfTapHand {
    uint8_t  pose_valid, skel_valid, tracking_result, bone_count;  // bone_count as submitted by the source
    uint32_t skel_age_us;
    float    raw_pos[3], raw_rot[4], lin_vel[3], ang_vel[3];       // Steam Link hand device, raw space
    CfBone   bones[31];       // WithoutController, parent space, as submitted
};
struct CfContext {            // hand = 0xFF
    CfHeader  h;
    uint8_t   hmd_valid, mode_left, mode_right, tap_hook_ok;
    float     hmd_pos[3], hmd_rot[4], hmd_lin_vel[3], hmd_ang_vel[3];
    uint32_t  applied_hs_seq[2];   // last CFHS applied per hand, so the Studio can show link health
    CfTapHand tap[2];
};
#pragma pack(pop)
```

The packets are about 36 bytes (`CFG2`), 1.15 kB (`CFHS`) and 2.2 kB (`CFOP`).

- **Latency.** The driver keeps `{seq → send time}` for recent `CFOP` frames. On each `CFHS` it reads `optical_seq`
  and gets the exact end-to-end latency in its own clock. Prediction uses `age_us`:
  `poseTimeOffset = −(age_us + time since arrival)`.
- **Legacy.** `CFGP` (5-bit layout) and `HTSK` keep parsing. `CFHS` supersedes `HTSK`.
- **One source of truth.** The layouts live in `bridge/cf_protocol.py` and `src/Protocol.h`, kept in sync by a
  golden-vector test (§11).

## 6. Driver work

### 6.1 Device (`MergedController` → `CyberFingerController`)

These properties follow the list in VRChat's guide for a native device:

| Property | Value |
|---|---|
| `Prop_ControllerType_String` | `cyberfinger` |
| `Prop_InputProfilePath_String` | `{cyberfinger}/input/cyberfinger_profile.json` |
| `Prop_ManufacturerName_String`, `Prop_TrackingSystemName_String`, `Prop_ModelNumber_String` | `SciCortex`, `cyberfinger`, `CyberFinger` (unchanged) |
| `Prop_SerialNumber_String` | `CYBERFINGER_L`, `CYBERFINGER_R` |
| `Prop_ControllerRoleHint_Int32` | 1 left, 2 right |
| `Prop_ControllerHandSelectionPriority_Int32` | above Steam Link's hand devices (value from S1) |
| `Prop_RenderModelName_String` | `{cyberfinger}cyberfinger_left`, `…_right` (§6.2) |
| `Prop_DeviceProvidesBatteryStatus_Bool`, `Prop_DeviceBatteryPercentage_Float` | from the `CFG2` battery byte |

Component names match the Index's, so the remapping needs few rules:

| Component | Source |
|---|---|
| `/input/trigger/value`, `/input/trigger/click` | analog trigger; TRIG bit |
| `/input/grip/value`, `/input/grip/click` | GRIP bit (value is 0/1 until the firmware reports an analog grip) |
| `/input/thumbstick/x`, `/y`, `/click` | push-stick; JCLK |
| `/input/a/click`, `/input/b/click`, `/input/system/click` | the 2–3 extra buttons through `button_map` (§14 Q1); system opens the SteamVR dashboard |
| `/input/finger/index` … `/pinky` | fused curls |
| `/input/skeleton/left` or `/right` | base pose `/pose/raw`, `VRSkeletalTracking_Full` (setting; D2), grip limit `nullptr` |
| `/output/haptic` | created; forwarded to the Studio if the glove ever gets a motor |

There are no trackpad, touch or force components, because the glove has no such sensors; the remapping converts
Index bindings (§9.2). Update both motion ranges every frame, and once immediately after creating the skeleton
component, since SteamVR otherwise treats skeletal input as inactive.

### 6.2 Resources

- `resources/input/cyberfinger_profile.json`: rewrite per §9.1.
- `resources/input/cyberfinger_remapping.json`: Automatic Rebinding with Index emulation (§9.2).
- `resources/input/legacy_bindings_cyberfinger.json`: carries the Index emulation options for legacy-input titles.
- `resources/input/bindings/`: `steam.app.438100_cyberfinger.json` (VRChat), `steam.app.2519830_cyberfinger.json`
  (Resonite), `vrcompositor_cyberfinger.json` (dashboard laser and click).
- `resources/rendermodels/cyberfinger_left|right/`: a minimal mesh (or none) plus locators (`base`, `handgrip`,
  `tip`, `openxr_grip`, `openxr_aim`) copied from the Index controller's render-model JSON in the SteamVR install.
- `resources/settings/settingsschema.vrsettings`: add it (fixes G8) with the settings of §6.7.

### 6.3 Receiver (`HandTrackingReceiver` → `StudioLink`)

- Bind 127.0.0.1, with a setting to widen it.
- Parse `CFG2` and `CFHS`, plus legacy `CFGP` and `HTSK`. Keep the latest two `CFHS` per hand, with arrival times.
- Apply `button_map` here: bits → components, per hand.

### 6.4 Pose output

- Run the mode machine of §4.
- Update from `RunFrame`, or from a 250 Hz thread as the SDK sample does; at least once per frame. SteamVR
  extrapolates from the velocities for up to 100 ms, then invalidates the pose.
- Set `poseTimeOffset = −age` and the `CFHS` velocities. Leave `qWorldFromDriver` and `qDriverFromHead` at identity:
  all data is already in raw space.
- Call `GetRawTrackedDevicePoses` once per frame; today each controller calls it twice.

### 6.5 Skeleton output

- FUSED: write the `CFHS` bones to both ranges. Interpolation (render about one packet behind and slerp) is behind a
  setting; the default holds the latest.
- PASSTHROUGH: re-root the tap skeleton (§8.6).
- Fallback: the C++ port of `hand_simulation.cpp`, driven by the existing button-to-curl mapping, with aux bones
  filled. `SkeletonComposer`'s "buttons override tracked fingers" logic runs only here, never in FUSED.

### 6.6 OpticalTap

- **Pose tap, no hook.** Find Steam Link's hand devices by serial pattern (settings; default `Hand_Left`,
  `Hand_Right`, as `FindSourceDevice` matches today) and read their raw poses every frame.
- **Skeleton tap, hook, opt-in `optical_tap_hook`.** Uses MinHook (BSD-2), as a submodule under `third_party/`.
  - On `Init`, fetch `IVRDriverInput_004` (and `_003` if present) through the driver context. Hook
    `CreateSkeletonComponent` and `UpdateSkeletonComponent` through their vtable slots.
  - Also hook `IVRDriverContext::GetGenericInterface`, to catch interface versions requested later.
    OpenVR-SpaceCalibrator uses the same pattern for `TrackedDevicePoseUpdated`.
  - The create detour calls through. If the container belongs to a tapped device (serial via `VRProperties`), it
    records handle → hand.
  - The update detour calls through unchanged. For a tapped handle, it copies the `WithoutController` bones, the bone
    count and the time into a per-hand double buffer. It never blocks or allocates.
  - If Steam Link created its components before our hooks existed (load order), identify the hand from the chirality
    of the wrist-bone offset, and log it.
  - On any failure, log, disable the skeleton tap and keep the pose tap. Gate the hook on a list of tested SteamVR
    versions (vrserver file version), with an override.
- Send `CFOP` every frame (≤ 120 Hz).

### 6.7 Settings (`default.vrsettings` + schema)

| Key | Default | Notes |
|---|---|---|
| `handtracking_udp_port` | 27015 | Studio → driver |
| `context_udp_port` | 27016 | driver → Studio |
| `bind_loopback_only` | `true` | |
| `skeletal_tracking_level` | `"full"` | read at activation (D2) |
| `hand_selection_priority` | from S1 | |
| `optical_tap`, `optical_tap_hook` | `true`, `true` | hook only on tested SteamVR versions |
| `tap_serial_left`, `tap_serial_right` | `Hand_Left`, `Hand_Right` | presets for other streamers later |
| `fused_timeout_ms`, `disconnect_after_ms` | 150, 2000 | |
| `skeleton_interpolation` | `false` | |
| `button_map_left`, `button_map_right` | §14 Q1 | |

Remove `grip_angle_*` and `pose_offset_*` once PASSTHROUGH re-rooting lands; they compensate for the missing skeleton
alignment.

### 6.8 Build

- Add MinHook as a submodule, with `add_subdirectory`.
- Add the missing schema file.
- Add a `cf_protocol_test` executable for the golden vectors.
- Keep openvr pinned at v2.12.14 (`IVRDriverInput_004`).

## 7. Studio work

### 7.1 Split compute from drawing (Phase 1b, small)

- Split `_st_tick` into `_st_compute(src) → S` and `_st_draw(S)`. Compute runs whenever SteamVR output is on; drawing
  happens only while the tab is visible. This removes G1's visibility gate.
- After compute, call `self.steamvr_out.publish(h, S)`.
- Keep the 60 Hz Tk tick for now, and measure it (S5).

### 7.2 Per-hand engine (Phase 2)

- Move the fusion state out of the app into `bridge/fusion_engine.py`: fusers, mount calibration, hybrid tracker,
  pose regressor, key postures, filters and last-known values. `HandFusion(right, has_emg)` is instantiated for both
  hands. The non-EMG hand runs camera + glove IMUs + position model.
- Run a worker thread triggered by each `CFOP` frame (event-driven, ≤ 120 Hz) instead of Tk `after()`. The UI reads
  the latest `S`.
- Headless, the source switches come from config.

### 7.3 `SteamVRContextSource` (`bridge/steamvr_context.py`)

It implements the duck-typed source contract that `_gate_v2`, `_st_tick` and the panels already consume from
`OpenXRHandSkeletonSource`:

| Attribute | Built from `CFOP` |
|---|---|
| `hmd` `{"pos", "quat", "valid"}` | HMD raw pose; `quat` in the x, y, z, w order the gate expects |
| `world_joints[h]` (26 × 3) | Forward kinematics (FK) of the tap bones, placed in raw space by the tap pose. OpenXR joints 1–25 are bones 1–25; the palm (0) is the midpoint of the middle metacarpal and proximal origins |
| `active[h]`, `wrist_valid[h]` | tap pose valid and skeleton younger than 100 ms |
| `jflags`, `jradius` | synthesized "valid" / `None` (the gate falls back to its default radius) |
| `hands`, `pose_info`, `status` | as in `OpenVRSkeletonSource` |

`_poll_queues` selects it automatically while SteamVR output is on.

### 7.4 `SteamVROutput` (`bridge/steamvr_out.py`)

`publish(h, S)`:
1. Skip unless `S` has `wrist`, `hand_R` and `hand_local`.
2. Compute curls and splay with one function for camera, EMG, key-posture and blended hands (§8.3).
3. Build the bones: Stage A synth in Phase 1, Stage B retarget in Phase 2.
4. Place `/pose/raw` by palm-frame alignment (§8.4).
5. Compute velocities by One-Euro-filtered finite differences of the raw pose (reuse `_OneEuroVec`).
6. Take flags and confidences from `S` (`src_pos`, gate type, finger visibility `fc`).
7. Pack the `CFHS` and send it.

### 7.5 Glove stream

`VRMode` sends `CFG2`: the full 8-bit buttons and the centred, deadzoned stick from `HandState._joy_deadzoned`.

### 7.6 Loop guard

In SteamVR-output mode the only allowed optical source is `SteamVRContextSource`. The Studio refuses to start the
OpenXR preview or `OpenVRSkeletonSource` while output is on, because both would read CyberFinger's own skeleton once
it holds the hand roles, and it says so in the console.

### 7.7 UI

Add a "SteamVR out" toggle to the top bar, and a status chip showing:
- context-stream rate,
- tap state (pose / skeleton / hook off),
- the driver's mode per hand,
- end-to-end latency p50/p95.

## 8. Frames and skeleton math

### 8.1 Frames

| Name | Definition |
|---|---|
| raw | SteamVR raw tracking space (`GetRawTrackedDevicePoses`), +y up, metres. The Studio's world frame in SteamVR mode |
| `R_opt` | the Studio hand frame, [`hand_frame_wrist`](fusion/extrinsic.py): columns are forward (wrist → metacarpal midpoint), side and palm normal. `S["hand_R"]` is this frame in world coordinates; `S["hand_local"]` holds the joints in it |
| bone frames | SteamVR skeleton, parent-relative. The bind pose points along −Z with palms facing −X (right hand) or +X (left). Metacarpals are rotated 90° relative to the wrist (the FBX convention), and "up" is flipped between hands |
| `/pose/raw` | the skeleton root; bone 1 is the raw → wrist offset |

### 8.2 Reference data (S4 tool)

`bridge/tools/dump_steamvr_skeleton.py` is an OpenVR overlay client, reusing the `OpenVRSkeletonSource` plumbing.
Per hand, it calls `GetBoneHierarchy`, `GetBoneName` and `GetSkeletalReferenceTransforms` for BindPose, OpenHand,
Fist and GripLimit, in both Parent and Model space. It writes `bridge/assets/steamvr_skeleton_reference.json`, and
needs a skeletal device in each hand role (Steam Link hands, or our driver). The dump is used to:
- confirm the aux-bone rule: the OpenVR wiki says aux bones "have the same position and rotation as the last knuckle
  bone in each finger, but are direct children of the root bone", and the dump shows which bone that is;
- seed Stage B;
- check Stage A against Valve's open and fist poses.

### 8.3 Stage A: curls and splay → bones

- Port `hand_simulation.cpp` to Python (`bridge/steamvr_skeleton.py`) and to C++ (the driver fallback); golden
  vectors keep the two equal. Add the aux bones, which the sample leaves unset (26–30).
- **Curl.** Map the Studio's per-finger flexion (the sum of interior joint angles, `_finger_flex`) onto the sample's
  joint-angle budget at curl 1: metacarpal 5°, proximal 90°, intermediate 80°, distal 80°; thumb 5°/90°/90°.
- **Splay.** From `hand_local`: the signed angle of each proximal phalanx against its metacarpal in the palm plane,
  minus the sample's rest splay, divided by the sample's range (15°, thumb 20°). EMG-only hands get splay 0;
  key-posture templates carry their own.
- **Limitation.** Fingertip contact in a pinch isn't guaranteed; that is Stage B's job.

### 8.4 Palm-frame alignment → `/pose/raw`

FK of this frame's bones gives joint positions `J` in the raw frame, with the wrist at `J[1]`. Compute
`R_opt_m = hand_frame_wrist(J, right)`. With the fused wrist `p_w` and `hand_R`:

```
R_raw = hand_R · R_opt_mᵀ
p_raw = p_w − R_raw · J[1]
```

The fused wrist then coincides with the skeleton's wrist, and the palm orientation matches. A unit test checks that
alignment followed by FK reproduces `p_w` and `hand_R`.

### 8.5 Stage B: full retarget (Phase 2)

For each finger chain (bones 2–5 for the thumb, 6–10 … 21–25), walk outwards from the metacarpal in the reference
model frame:
1. Express `hand_local` in the reference palm frame, using Kabsch on the wrist and the four metacarpal bases (joints
   1, 6, 11, 16, 21 ↔ bone origins 1, 6, 11, 16, 21).
2. Swing: rotate the reference bone so its along-bone axis (towards its child in the reference pose) points at the
   fused child joint.
3. Twist: align the reference hinge axis with the fused hinge. When the finger is bent, the hinge is the normal of
   consecutive phalanges; when it is straight, carry the parent's hinge.
4. The local rotation is `parent_modelᵀ · bone_model`. Positions keep Valve's bone lengths; VRChat ignores positions,
   and there is an optional hand-size scale for Resonite.

Tests:
- round-trip OpenHand and Fist within 1°;
- round-trip recorded Steam Link skeletons (bones → joints → bones) within a 5° median;
- on a pinch fixture, the fingertip gap survives within 5 mm.

### 8.6 Tap → joints, and re-rooting

- **Joints.** FK of the tap bones gives the bone origins; OpenXR joints 1–25 map one-to-one onto bones 1–25 and the
  palm is synthesized. `_get_bones` notes that Steam Link submits a bone count different from the one it reports
  ([fusion_studio.py:1768](bridge/fusion_studio.py#L1768)). If it submits 26, map by the hierarchy the hook observes
  instead (S2).
- **PASSTHROUGH re-rooting** (driver, C++). Let `B1_link` be Steam Link's wrist bone and `B1_ours` the Index offset.
  Then:
  - `raw_ours = raw_link · B1_link · B1_ours⁻¹`
  - bones 2–25 are wrist-relative and copy unchanged
  - bone 1 becomes `B1_ours`
  - aux bones become `B1_ours · B1_link⁻¹ · aux_link`

  The rendered hand stays continuous across FUSED ↔ PASSTHROUGH, and the raw pose keeps one convention.

## 9. Application compatibility

### 9.1 Input profile (sketch)

```json
{
  "jsonid": "input_profile",
  "controller_type": "cyberfinger",
  "device_class": "TrackedDeviceClass_Controller",
  "resource_root": "cyberfinger",
  "driver_name": "cyberfinger",
  "input_bindingui_mode": "controller_handed",
  "input_bindingui_left":  { "transform": "scale(-1,1)", "image": "{cyberfinger}/icons/steamvr_cybrfngr_right_transp.png" },
  "input_bindingui_right": { "image": "{cyberfinger}/icons/steamvr_cybrfngr_right_transp.png" },
  "remapping": "cyberfinger_remapping.json",
  "legacy_binding": "legacy_bindings_cyberfinger.json",
  "input_source": {
    "/input/trigger":    { "type": "trigger",  "click": true, "value": true, "order": 1 },
    "/input/grip":       { "type": "trigger",  "click": true, "value": true, "order": 2 },
    "/input/a":          { "type": "button",   "click": true, "order": 3 },
    "/input/b":          { "type": "button",   "click": true, "order": 4 },
    "/input/thumbstick": { "type": "joystick", "click": true, "order": 5 },
    "/input/finger/index":  { "type": "trigger", "visibility": "InputValueVisibility_AvailableButHidden" },
    "/input/finger/middle": { "type": "trigger", "visibility": "InputValueVisibility_AvailableButHidden" },
    "/input/finger/ring":   { "type": "trigger", "visibility": "InputValueVisibility_AvailableButHidden" },
    "/input/finger/pinky":  { "type": "trigger", "visibility": "InputValueVisibility_AvailableButHidden" },
    "/input/skeleton/left":  { "type": "skeleton", "skeleton": "/skeleton/hand/left",  "side": "left" },
    "/input/skeleton/right": { "type": "skeleton", "skeleton": "/skeleton/hand/right", "side": "right" },
    "/pose/raw":      { "type": "pose" },
    "/output/haptic": { "type": "haptic" }
  },
  "default_bindings": [
    { "app_key": "steam.app.438100",  "binding_url": "bindings/steam.app.438100_cyberfinger.json" },
    { "app_key": "steam.app.2519830", "binding_url": "bindings/steam.app.2519830_cyberfinger.json" },
    { "app_key": "openvr.component.vrcompositor", "binding_url": "bindings/vrcompositor_cyberfinger.json" }
  ]
}
```

- `/input/system/click` is reserved for the dashboard. It is created as a component but not listed in `input_source`.
- Also list the render-model locators (`/pose/tip`, `/pose/handgrip`, …) as `pose` sources, so applications can bind
  them.

### 9.2 Remapping: Index emulation by default

```json
{
  "to_controller_type": "cyberfinger",
  "layouts": [
    {
      "priority": 3,
      "from_controller_type": "knuckles",
      "simulate_controller_type": true,
      "simulate_render_model": true,
      "simulate_HMD": true,
      "autoremappings": [
        { "from": "/user/hand/right/input/trigger",    "to": "/user/hand/right/input/trigger" },
        { "from": "/user/hand/right/input/grip",       "to": "/user/hand/right/input/grip" },
        { "from": "/user/hand/right/input/thumbstick", "to": "/user/hand/right/input/thumbstick" },
        { "from": "/user/hand/right/input/a",          "to": "/user/hand/right/input/a" },
        { "from": "/user/hand/right/input/b",          "to": "/user/hand/right/input/b" }
      ],
      "remappings": [
        { "from": { "path": "/user/hand/right/input/trackpad" }, "remapping_mode": "delete" }
      ]
    },
    { "priority": 2, "from_controller_type": "oculus_touch", "…": "left x/y → a/b (mirror: false); thumbstick, trigger, grip as above" },
    { "priority": 1, "from_controller_type": "vive_controller", "…": "trackpad → thumbstick; menu → b" }
  ]
}
```

- **Knuckles first.** An application with an Index binding gets it converted, and believes it is talking to an Index:
  controller type, render model and, through `simulate_HMD`, the HMD profile. Paths mirror to the left hand by
  default.
- **Touch and Vive fallbacks.** These catch applications without an Index binding; such an application sees the
  controller its binding was written for.
- **No trackpad.** Trackpad bindings are dropped, because the hardware has none. If a title needs one (a
  trackpad-only teleport, say), ship a default binding for that title.
- **Touch and force.** The autoremapper converts Index capacitive-touch and grip-force bindings from the capabilities
  declared in both profiles (touch → click, force → value).
- **Legacy-input titles.** `legacy_bindings_cyberfinger.json` carries
  `"options": { "simulated_controller_type": "knuckles", "simulate_rendermodel": true }`, which is Valve's
  recommendation for legacy titles.
- **Binding priority** (SDK docs): user > shipped with the application > partner site > driver default bindings >
  compatibility mode > remapping. The VRChat and Resonite defaults therefore beat the remapping, and a user's own
  binding beats everything. The VRChat default binding is mandatory: without it, the remapping would emulate an Index
  in VRChat.
- **`simulate_HMD`.** This is the layout default. Some titles check the HMD to decide the controller type. Set it to
  `false` if a title misbehaves, for instance by showing Index-specific HMD UI.

### 9.3 VRChat (`steam.app.438100`)

- Ship a native binding with no `options`. Author it in the SteamVR binding UI with VRChat running, then export it:
  enable "Enable debugging options in the input bindings user interface" in the developer settings, and the export
  lands in `Documents/steamvr/input/exports`.
- Bind: the pose action, `SkeletonLeftHand`/`SkeletonRightHand` to `/input/skeleton/*`, move (left stick), turn
  (right stick), jump, use (trigger), grab (grip), menu, mic.
- VRChat retargets rotations only, and reads `WithoutController` (hence G5).
- Verify with VRChat's "Accurate Hands" view (Main Menu → Settings → Controls → Accurate), which is the closest view of
  the raw tracking data.
- VRChat derives its avatar-expression parameters (`GestureLeft`/`GestureRight`) from the skeleton on its own. They
  animate avatars and are not inputs; no work is planned for them.

### 9.4 Resonite (`steam.app.2519830`)

- Ship a native binding. Grab and precision pinch are built in Resonite from the skeleton; Stage B matters for
  fingertip accuracy.
- If the in-Resonite logic would rather bind an action than analyse bones, the Studio can publish pinch and grab
  strength as extra scalar components. Names are TBD (avoid the reserved `/input/pinch`), and this is off by default.
- ~~Check that Resonite animates fingers for a non-Index controller type at `Partial` (S3).~~ Settled (2026-09-26):
  Resonite ignores the tracking level and picks its controller mode by render model. The native binding emulates
  an Oculus Touch controller (Touch mode is the only one with a dash), binds the skeletons in its OculusTouch set
  and the pose in its Generic set (see Status).

### 9.5 SteamVR itself

- The dashboard opens from `/input/system/click` through `button_map`; the vrcompositor binding makes the trigger the
  laser click.
- SteamVR Home is covered by the remapping.

## 10. Timing

- **Rates.** Glove: one packet per BLE report. Fusion: 90 Hz target (the 60 Hz Tk tick in Phase 1). `CFOP`:
  ≤ 120 Hz. Driver pose updates: at least the display rate.
- **Added-latency budget, relative to plain Steam Link.**

  | Stage | Budget |
  |---|---|
  | tap → Studio | < 1 ms |
  | wait for the fusion tick | ≤ 11 ms (0 once event-driven) |
  | fusion | ≤ 5 ms |
  | Studio → driver | < 1 ms |
  | driver update | ≤ 4 ms |

  Target p95: ≤ 25 ms in Phase 1b, ≤ 15 ms in Phase 2, measured exactly through `optical_seq`.
- **Python jitter.** Tk drawing, the BLE asyncio loop and EMG processing share the GIL. Move the fusion onto the
  worker thread (Phase 2), and cap drawing at 30 fps while output is on.

## 11. Testing

Unit tests (pytest, no hardware):
- protocol: Python writes golden vectors and the C++ test executable parses them, and the reverse;
- Stage A: Python/C++ parity, and comparison with the reference poses;
- palm alignment identity (§8.4);
- Stage B round-trips (§8.5);
- tap bones → 26 joints, compared with OpenXR provider recordings (`bridge/skeleton_logs/`);
- re-rooting continuity (§8.6).

Integration:
- `bridge/tools/steamvr_readback.py`, an overlay client, reads our `/user/hand/*` skeleton, tracking level and summary
  curls, and compares them with what the Studio sent. They should match bone for bone.
- The driver log (`vrserver.txt`) records mode changes, and, every 10 s, packet rates and a latency histogram (rate
  limited).
- Soak test: a 2-hour session with headset standby cycles, Studio restarts, SteamVR restarts and glove disconnects.
  Pass means no role flip-flops and no stuck poses.

In-application checklists for VRChat, Resonite, and one Index-aware title (Half-Life: Alyx, or SteamVR Home).

## 12. Phases

### Phase 0: spikes

| Spike | Question | Pass |
|---|---|---|
| S1 roles | Does CyberFinger keep both hand roles while Steam Link hands are tracked, through tracking loss and regain, standby, and SteamVR restarts? Can priority or role change at runtime? Does `deviceIsConnected = false` hand the role back? Log Steam Link hands' priority and controller type | roles held for a whole 30-minute session |
| S2 tap | Can we hook Steam Link's `UpdateSkeletonComponent`? What bone count, rate, chirality and load order do we get? Do tap-derived joints match the OpenXR provider's (recorded together in the SteamVR void while our devices hold no roles)? Is SteamVR's OpenXR hand tracking served from the role holder (the G3 loop)? | ≥ 60 Hz per hand; joints within 5 mm after rigid alignment |
| S3 apps | A stub driver (fixed profile, `Partial`, Stage A synth animating curls as the SDK sample does) plus draft bindings. VRChat: fingers move in Accurate Hands, inputs work, no "unsupported" warning. Resonite: fingers and inputs. An Index-aware title sees an Index (via the remapping) with finger tracking | all three pass |
| S4 reference | Dump the reference poses; confirm the aux-bone rule | JSON committed |
| S5 latency | Echo test (the Studio returns the tap pose immediately): added-latency distribution, Tk tick vs thread | numbers recorded |

**If S2 fails,** ship the no-hook variant:
- tap only the poses (Steam Link wrist poses + HMD);
- take the fingers from EMG and key postures, with the camera-trained models frozen;
- calibrate and learn in a pre-session through the Studio's own OpenXR preview while our devices hold no roles. That
  needs the S1 role hand-back, or a SteamVR restart between calibration and play.

### Phase 1a: a correct controller (driver + resources)

§6.1–6.3, §6.5 fallback, §6.6 pose tap (plus the skeleton tap if S2 passed), PASSTHROUGH and NO_POSE modes, §6.7,
§6.8, all of §9's files, the render model, and `VRMode` → `CFG2` (§7.5).

Done when:
- in VRChat, Resonite and an Index-aware title, CyberFinger holds both hands with Steam Link's optical pose and
  skeleton, the stick and buttons work, and no duplicate hands appear;
- the dashboard opens from the glove.

### Phase 1b: fusion link (right hand fused, Stage A)

Driver: `CFOP` sender, `CFHS` receiver, the FUSED mode with prediction. Studio: §7.1, §7.3, §7.4 (Stage A), §7.6,
§7.7.

Done when:
- in VRChat and Resonite, the right hand tracks in view and out of view (behind the back), and its fingers follow
  camera, EMG and key postures;
- when the Studio is stopped, the hand drops to PASSTHROUGH within 150 ms and recovers when it returns;
- a SteamVR restart recovers without manual steps;
- added latency p95 ≤ 25 ms.

### Phase 2: fidelity

Stage B retarget, the per-hand engine for both hands, the worker thread, optional skeleton interpolation, hand-size
scaling, and Resonite pinch/grab components if requested.

Done when:
- fingertips meet in VRChat's Accurate Hands during a pinch (gap < 1 cm);
- the round-trip tests pass;
- p95 ≤ 15 ms;
- the left hand also continues out of view.

### Phase 3: robustness and distribution

- The installer registers the driver with `vrpathreg adddriver` instead of copying it into `SteamVR/drivers`, and
  ships the bindings.
- A SteamVR-version gate protects the hook.
- The docs are updated: new setup, and `TrackingOverrides` dropped from the README.
- Other tap sources (Virtual Desktop, ALVR) become serial-pattern presets.
- Battery and status icons.

The Linux stack (WiVRn/Monado with xrizer) runs no vrserver, so it would need a Monado-side device or an OpenXR layer;
that is out of scope here.

## 13. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| The hook breaks on a SteamVR update | no camera finger data | version gate, fail-safe to the pose tap, a clear log line; the same pattern has shipped in OpenVR-SpaceCalibrator for years |
| Steam Link keeps a hand role | duplicate hands in apps | S1; priority; if needed, demote Steam Link hands' role hint to OptOut from our driver (a property write on another driver's device, to verify) |
| Tap skeleton differs from the OpenXR joints the fusion was tuned on | gate thresholds and models drift | S2 comparison; re-validate `_FLEX_MAX`, the gate and the key-posture models on tap recordings |
| Python jitter and latency | swimming hands | event-driven worker, driver prediction, measurement via `optical_seq` |
| VRChat changes its handling of non-Index controllers | fingers not animated | follow VRChat's driver guide |
| Emulated titles draw Index controllers or expect a trackpad | cosmetic, or a missing function | per-title default bindings |
| A role-bound reader loops back | self-reinforcing calibration | §7.6 guard |
| UDP injection from the network | spoofed input | loopback bind |

## 14. Open questions

1. **Button map.** Which physical buttons exist on each unit (bits C 0x04, D 0x08, E 0x10, MENU 0x20, STSEL 0x80),
   and which become A, B and system (dashboard)? Proposal: the first two present become A and B, and the third
   becomes system. On two-button units, system is a long press (≥ 0.8 s) of B.
2. **Streaming stacks** besides Steam Link: Virtual Desktop, ALVR, Quest Link? Each needs its hand devices identified
   for the tap. For Quest Link, check whether its SteamVR driver exposes hand skeletons at all.
3. **Hook in distribution.** Is a vrserver hook acceptable, or should the first release be the no-hook variant?
4. **Left-arm EMG.** Is a second armband planned? It decides how much of Phase 2's per-hand engine the left hand
   uses.
5. **Render model.** Invisible, or a small visible glove/puck? Emulated titles show Index models regardless.
6. **Resonite inputs.** Skeleton only for the custom grab and pinch, or also the optional pinch/grab components?

## 15. References

- `openvr/docs/Driver_API_Documentation.md` (SDK v2.12.14): Controller roles; Input Profile JSON; Default Bindings;
  Skeletal Input; Application Compatibility; Automatic Rebinding; Emulating Devices in Bindings; Legacy Binding
  Simulation.
- `openvr/samples/drivers/drivers/handskeletonsimulation`: Valve's reference for producing skeletons in a driver
  (BSD-3).
- VRChat, *SteamVR Skeletal Hand Tracking Driver Guide*: https://creators.vrchat.com/platforms/pc/steamvr-drivers/
- OpenVR wiki, *Hand Skeleton*: https://github.com/ValveSoftware/openvr/wiki/Hand-Skeleton
- OpenVR-SpaceCalibrator, driver interface hooks with MinHook: https://github.com/pushrax/OpenVR-SpaceCalibrator
- MinHook: https://github.com/TsudaKageyu/minhook

## Appendix A: files to change or add

| Path | Change | Phase |
|---|---|---|
| `src/Protocol.h` (new) | packet structs and magics | 1a |
| `src/HandTrackingReceiver.*` → `src/StudioLink.*` | loopback bind, `CFG2`/`CFHS` parsing, legacy parsing, button map | 1a/1b |
| `src/MergedController.*` → `src/CyberFingerController.*` | properties, components, modes, prediction, both motion ranges | 1a/1b |
| `src/SkeletonSynth.*` (new) | C++ port of `hand_simulation.cpp` with aux bones; replaces the `BoneData.h` rest pose and `CurlFinger` | 1a |
| `src/OpticalTap.*` (new) | pose tap, MinHook skeleton tap, `CFOP` sender | 1a/1b |
| `src/ServerProvider.*` | wires the above, hook init/teardown, event polling | 1a |
| `src/SkeletonComposer.*` | fallback only | 1a |
| `resources/input/*` | profile, remapping, legacy binding, default bindings | 1a |
| `resources/rendermodels/*` (new) | model + locators | 1a |
| `resources/settings/*` | new keys, schema | 1a |
| `CMakeLists.txt` | MinHook, new sources, protocol test, render-model copy | 1a |
| `bridge/cf_protocol.py` (new) | packets | 1a |
| `bridge/fusion_studio.py` | `VRMode` → `CFG2` (1a); compute/draw split, source selection, loop guard, UI (1b) | 1a/1b |
| `bridge/steamvr_context.py` (new) | context source | 1b |
| `bridge/steamvr_skeleton.py` (new) | Stage A, FK, alignment; Stage B later | 1b/2 |
| `bridge/steamvr_out.py` (new) | publisher | 1b |
| `bridge/fusion_engine.py` (new) | per-hand `HandFusion` + worker thread | 2 |
| `bridge/tools/dump_steamvr_skeleton.py`, `bridge/tools/steamvr_readback.py` (new) | reference dump, readback check | 0/1b |
| `tests/` (new) | pytest + golden vectors | 1a onward |
| `README.md`, `bridge/FUSION_STUDIO.md` | setup instructions, drop `TrackingOverrides` | 3 |
