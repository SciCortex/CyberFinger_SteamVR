# CyberFinger — SteamVR Driver

A SteamVR driver that turns **two CyberFingers** into a pair of SteamVR hand controllers: the glove's
push-stick, trigger, grip and buttons, plus a full hand skeleton and pose from the headset's hand tracking
(Quest via Steam Link) — or from the CyberFinger Fusion Studio, which keeps the hand tracked out of view.

Note: this is a *work-in-progress*, and currently alpha software.

## How it works

- The driver adds two controllers, `CYBERFINGER_L` and `CYBERFINGER_R`, which take the left/right hand roles
  (Steam Link's hand devices declare hand priority −1, CyberFinger 1000). Apps see CyberFinger, not a second
  pair of Steam Link hands.
- **Pose and fingers**, per hand, from the first source available:
  - **FUSED** — the Fusion Studio's fused hand (camera + glove IMUs + EMG), sent over UDP;
  - **PASSTHROUGH** — the headset's own hand tracking as streamed by Steam Link or Virtual Desktop,
    captured inside vrserver by observe-only hooks on the streamer's pose and skeleton updates. Each update
    is republished the moment the streamer submits it, with its own timing, so apps get the same stream as
    from the streamer's hands;
  - **NO_POSE** — nothing tracks the hand: the pose is reported invalid, buttons keep working.
- **The Quest left-palm pinch (≡)** that opens the SteamVR dashboard keeps working: the driver forwards
  Steam Link's system button to CyberFinger's.
- **Buttons first.** Actions come from the glove's buttons. The standard hand-tracking gestures (pinches,
  grasp, index point) are exposed for binding but unassigned by default — except the thumb-pinky pinch,
  which acts like MENU.
- **Haptics** requested by apps are forwarded to the bridge, which shows them per hand (a GATT link to the
  glove comes later).
- **Apps:** VRChat and Resonite get native bindings; every other app sees an Index controller (SteamVR
  automatic rebinding + legacy binding emulation).

## Building

### Prerequisites

- Visual Studio 2022 (or newer) with the **Desktop development with C++** workload — MSVC and a Windows SDK
- CMake 3.16+ (bundled with Visual Studio, or `pip install cmake ninja`)
- The submodules: [OpenVR SDK](https://github.com/ValveSoftware/openvr) (`openvr/`) and
  [MinHook](https://github.com/TsudaKageyu/minhook) (`third_party/minhook/`)
- Python with WinRT for the bridge (`pip install -r bridge/requirements.txt`, winrt v3.x)
- The "steamvr" branch of the CyberFingerFW_ESP32 firmware on the CyberFingers:
  https://github.com/DrSciCortex/CyberFingerFW_ESP32/tree/steamvr

### Windows

Double-click `tools\build_driver.cmd` (it finds Visual Studio, fetches missing submodules, builds Release
and runs the tests), or from a *Developer PowerShell for VS*:

```bash
git submodule update --init
cmake -S . -B out/build/x64-Release -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build out/build/x64-Release
ctest --test-dir out/build/x64-Release
```

The driver is assembled in `out/build/x64-Release/driver/cyberfinger/`. It links the C runtime statically,
so it needs no Visual C++ redistributable.

### The installer

`tools\build_installer.cmd` builds the driver, the bridge app (`bridge\build.bat`, PyInstaller) and
packages both with Inno Setup 6.3+ into `bridge\dist\installer\CyberFingerBridge_Setup_<version>.exe`.
Run it where `python` is the bridge's Python with PyInstaller installed (e.g. `conda activate cybrgui`).
Inno Setup is found in its default folders, or set `INNO_ISCC` to `ISCC.exe`. The ViGEmBus installer is
bundled when `bridge\installer\ViGEmBus_1.22.0_x64_x86_arm64.exe` is present.

### Linux

Builds, but untested; the Steam Link skeleton hook is Windows-only (headset hand poses still work).

## Installation

Quit SteamVR first: it holds the driver while running and reads the registration at startup.

- **Installer** — run `CyberFingerBridge_Setup_<version>.exe`. It installs the bridge app and the SteamVR
  driver, and registers the driver with SteamVR for your user. An earlier CyberFinger Bridge is upgraded in
  place (same folder, one entry in *Settings → Apps*). Uninstall from Windows *Settings → Apps*, which also
  unregisters the driver.
- **From a build** — `tools\install_driver.cmd` copies `out\build\x64-Release\driver\cyberfinger` to
  `%LOCALAPPDATA%\CyberFinger\SteamVR\cyberfinger` and registers it; `tools\uninstall_driver.cmd` undoes it.
  `install_driver.cmd -InPlace` registers the build folder itself, and `-Status` shows what is installed.

Either way only one CyberFinger driver is registered at a time, and what the previous proof-of-concept
driver had you set up by hand is cleaned up:

- a copy inside `SteamVR\drivers\cyberfinger` is deleted (the installer asks first);
- `TrackingOverrides` entries for `/devices/cyberfinger/...` are removed from
  `<Steam>/config/steamvr.vrsettings`, keeping a backup as `steamvr.vrsettings.cyberfinger-backup`. This driver
  takes the hand roles itself, and the overrides would hide your hands whenever it is switched off.

Then:

1. Stream with hand tracking on. **Steam Link:** keep *Enable Hand Tracking Passthrough* on in its SteamVR
   settings. **Virtual Desktop:** nothing to set; CyberFinger follows VD's hand devices (`HANDL`/`HANDR`, type
   `vd_hand_controller`).
2. Start SteamVR. *Settings → Startup/Shutdown → Manage Add-ons* should list `cyberfinger` as on.

## Switching it off

- **For a while, live:** *SteamVR Settings → CyberFinger → CyberFinger controllers active* (setting `active`).
  Off, both CyberFinger devices report disconnected and the headset's own hand tracking takes the hands
  back; on, CyberFinger takes them again. No restart needed.
- **Completely:** *Settings → Startup/Shutdown → Manage Add-ons → cyberfinger* off. SteamVR then does not
  load the driver at all (no devices, no hooks) from its next start.

## Running

1. Start SteamVR (Steam Link connected, hands visible to the headset).
2. Start a bridge in **VR mode**: `python bridge/cyberfinger_gui.py`, or the Fusion Studio
   (`python bridge/fusion_studio.py`, *▶ Start glove*). Put the gloves in "VR mode".
3. Check what the driver sees: `python bridge/tools/cf_driver_probe.py` (stop the bridge's glove link first —
   both listen on UDP 27016). With your hands in view it shows `PASSTHROUGH`, `skeleton live 31 bones`, and
   `[SYSTEM pressed]` while you do the left-palm pinch; haptic requests are printed as they arrive.
   `--fake-hand right` drives the right hand from a synthetic fused stream to test the FUSED path.

## Controls

| Glove | SteamVR component | VRChat default | Resonite default |
|---|---|---|---|
| Primary (trigger, analog + click) | `/input/trigger` | Use / interact | Primary |
| Grab (grip) | `/input/grab` (tap to hold) | Grab: a quick tap holds until the next press, a longer press grabs while held | same |
| Joystick (+ push) | `/input/thumbstick` | Move (L), turn (R) | Axis, secondary (push) |
| MENU — context / rotary-dial button | `/input/b` (B/Y) | Menu: tap quick menu, hold action menu | Context menu |
| Thumb-pinky pinch (hand tracking) | `/input/pinky_pinch` | same as MENU | same as MENU |
| Start/Select (black wrist button) | `/input/a` (A/X) | Mic (L), jump (R) | Dash |
| C, D, E (if fitted) | `/input/c`, `/input/d`, `/input/e` | unbound | unbound |
| Index / middle / ring pinch, grasp, index point | `/input/index_pinch` … `/input/index_point` | unbound | unbound |
| Quest left-palm pinch | `/input/system` (left) | SteamVR dashboard | SteamVR dashboard |

Everything unbound can be bound per app in SteamVR's *Controller bindings* UI, which shows the controls on a
picture of the glove. A glove button can also be made the dashboard button with the `button_system` setting.

In the SteamVR dashboard: the trigger clicks (a light press first locks the laser, so the click lands where it
points), the grip right-clicks, the stick scrolls (push: middle click), B (and the thumb-pinky pinch) goes
back, A (black button) goes home. The system button (the Quest left-palm pinch) toggles the dashboard; hold
it to recenter.

**Grab, tap to hold.** SteamVR's binding modes either toggle on every press or follow the button, so the driver
does this itself: `/input/grab` is the grip button with a latch. A press shorter than `grab_tap_ms` (200 ms)
holds the grab until the next press; a longer press grabs while held. `/input/grip` is the plain button.

Gesture values come from Steam Link's own hand-tracking gestures while it tracks the hand, and from the
hand skeleton otherwise.

## Haptics

When an app vibrates a CyberFinger hand, the driver sends the request (duration, frequency, amplitude) to
the bridge (`CFHP`, UDP 27016). The CyberFinger GUI shows it at the bottom of each hand panel, and the Fusion
Studio in its top bar: the LED lights while the vibration is requested (brightness = amplitude), the
waveform is drawn at the requested frequency, and a bar shows the time left. `cf_driver_probe.py` prints
the requests. Forwarding them to the glove over GATT will hook into `_on_haptic` in the bridges.

## Application support

- **VRChat** — native binding, no controller emulation (VRChat's
  [driver guide](https://creators.vrchat.com/platforms/pc/steamvr-drivers/) asks drivers not to emulate other
  controllers). At the default skeletal tracking level *Full*, VRChat also turns on its hand-gesture
  controls; `skeletal_tracking_level` = `partial` keeps them off. Check the fingers with *Settings → Controls →
  Accurate* hands. VRChat comes second for now: the defaults are tuned for Resonite.
- **Resonite** — native binding that emulates an Oculus Touch controller. Resonite picks its controller mode
  from the render model of the devices it registers as hands, and only its Touch mode has a dash button, so
  CyberFinger shows itself to Resonite as a Touch controller: the black button (A) opens the dash, B is the
  context menu, plus hand skeletons, trigger, grab and stick, on any streamer. Steam Link and Virtual Desktop
  also emulate Touch controllers from hand tracking; while CyberFinger holds the hands the driver marks those
  *never tracked* (`hide_other_hand_controllers`), or Resonite would register them instead of CyberFinger or
  draw them as trackers on the hands. A custom binding needs the skeletons in the *OculusTouch* set: without
  them Resonite draws rigid canned hands at a Touch offset. Resonite builds the hand from the *Generic* set's
  pose (`/pose/raw`) and the skeleton (*WithoutController*, model space), undoing its Touch offset for the
  hand, and ignores the tracking level. Precision grab is left to Resonite-side logic reading the skeleton.
- **Other SteamVR Input and OpenXR apps** — SteamVR converts the app's Index binding (then Touch, then Vive)
  and tells the app it is talking to an Index controller (`resources/input/cyberfinger_remapping.json`).
  Trackpad bindings are dropped: the CyberFinger has no trackpad.
- **Legacy-input apps** — Index emulation through `legacy_bindings_cyberfinger.json`.

The bindings are generated from the installed apps' own files: `python tools/generate_bindings.py`.
The render models (a small marker at the wrist plus the Index controller's pose locators) come from
`python tools/make_rendermodels.py`.

## Configuration

Settings live in `resources/settings/default.vrsettings` (section `driver_cyberfinger`); override them in
`<Steam>/config/steamvr.vrsettings`. The main ones also appear in SteamVR's settings UI.

| Setting | Default | Description |
|---|---|---|
| `active` | `true` | Live on/off: off hands the hand roles back to the headset's own hand tracking |
| `loadPriority` | `2000` | Loads the driver before Virtual Desktop's (1000) and Steam Link's (110), so the hooks see their hands being created |
| `hand_selection_priority` | `1000` | Hand-role priority (Steam Link hands: −1) |
| `skeletal_tracking_level` | `full` | Reported to apps: `full` (camera-tracked hands) or `partial` (VRChat keeps its hand-gesture controls off). Resonite ignores it |
| `optical_tap` / `optical_tap_hook` | `true` / `true` | Use the headset's hand tracking; capture its skeleton with the hook |
| `tap_serial_left` / `_right` | `Hand_Left` / `Hand_Right` | Serial substrings of the headset hand devices (Steam Link: `VRLINKQ_Hand_Left`); `a\|b` lists |
| `tap_controller_types` | `svl_hand_interaction_augmented\|vd_hand_controller` | Controller types of headset hand devices (Steam Link, Virtual Desktop) |
| `forward_tap_system_button` | `true` | Forward the headset hand's system button (Quest left-palm pinch) |
| `grab_tap_ms` | `200` | `/input/grab`: a press shorter than this holds until the next press (0 = plain button) |
| `passthrough_events` | `true` | Republish the headset hands' updates as they arrive; `false`: once per frame |
| `pose_filter` | `true` | Filter the headset hand pose in PASSTHROUGH, for the sources in `pose_filter_types` (live) |
| `pose_filter_types` | `svl_hand_interaction_augmented` | Source controller types whose poses are filtered: Steam Link's. Others (Virtual Desktop's `vd_hand_controller`) pass through as streamed; add types with `\|` (live) |
| `pose_filter_min_cutoff` / `pose_filter_beta` | `1.0` / `15` | One Euro filter: cutoff (Hz) of a still hand, and how fast it opens with speed (live) |
| `pose_prediction` | `0.5` | Scale of the linear velocity SteamVR extrapolates with: 0 = no prediction (live) |
| `pose_rotation_prediction` | `0.0` | Likewise for the angular velocity; above 0 a pointing laser jitters (live) |
| `pose_filter_gate_cm` | `5` | A pose this far beyond plausible hand motion is treated as a glitch (live) |
| `yield_to_controllers` | `true` | When a hand's headset hand tracking stops while a controller for that hand is tracked (you picked the Touch controllers up), release the hand to the controller (after ~0.5 s); take it back as soon as hand tracking is live again |
| `hide_other_hand_controllers` | `true` | While CyberFinger holds the hands, mark other drivers' hand controllers (the Touch controllers Steam Link and Virtual Desktop emulate from hand tracking) *never tracked*, so apps skip them; undone when CyberFinger is switched off |
| `debug_captures` | `false` | Record the hand data on request (`tools/analyze_tap_capture.py --capture N`); a debugging tool |
| `button_a` / `button_b` / `button_system` | `STSEL` / `MENU` / `NONE` | Glove buttons for A, B and the dashboard; names: `TRIGGER GRIP C D E MENU STICK STSEL`, combine with `\|` |
| `legacy_5bit_buttons` | `false` | Decode legacy `CFGP` packets with the pre-2026 firmware button layout |
| `fused_timeout_ms` | `150` | Fall back from FUSED to PASSTHROUGH after this silence |
| `disconnect_after_ms` | `0` | Report the device disconnected after this long without any data (0 = never) |
| `handtracking_udp_port` / `context_udp_port` | `27015` / `27016` | Bridge → driver / driver → bridge |
| `bind_loopback_only` | `true` | Accept packets from this PC only |
| `grip_angle_*`, `pose_offset_*` | −60, 35, 0 / 0, −0.1, 0 | Only used if no Steam Link skeleton was ever seen (hook off): offset from the Steam Link hand pose to CyberFinger's |

## Wire protocol (v2)

Little-endian, packed, localhost. The layouts are defined in [`src/Protocol.h`](src/Protocol.h) and mirrored in
[`bridge/cf_protocol.py`](bridge/cf_protocol.py); `tests/` keep them in sync.

| Packet | Direction | Port | Content |
|---|---|---|---|
| `CFG2` (34 B) | bridge → driver | 27015 | Glove buttons (firmware bit layout), analog trigger, centred stick (+y up), battery |
| `CFHS` (1140 B) | Fusion Studio → driver | 27015 | Fused hand: `/pose/raw` + velocities in SteamVR raw space, curls/splay, 31 bones |
| `CFOP` (2200 B) | driver → bridge | 27016 | HMD pose, driver mode per hand, Steam Link hand pose + skeleton + system button |
| `CFHP` (36 B) | driver → bridge | 27016 | Haptic request: hand, duration, frequency, amplitude |
| `CFIM` (96 B) | bridge → driver | 27015 | Raw glove IMU slots (quaternions, accel) per BLE report, sent only while the driver records a capture (`CFOP` header flag `0x1`) |
| `CFGP` (12 B) | bridge → driver | 27015 | Legacy glove packet, still accepted (stick uncentred, +y down) |

Firmware button bits: `0x01` trigger, `0x02` grip, `0x04` C, `0x08` D, `0x10` E, `0x20` MENU, `0x40` stick click,
`0x80` Start/Select.

## Tests

```bash
python -m unittest discover -s tests          # protocol (Python)
ctest --test-dir out/build/x64-Release        # protocol vectors, skeleton synthesis, re-rooting (C++)
```

## Troubleshooting

The driver logs to `<Steam>/logs/vrserver.txt` with the prefix `cyberfinger:`, including a status line per
hand every 10 s.

- **Steam Link's hands show instead of CyberFinger, or buttons do nothing in apps** — check for
  `activated as device … hand priority 1000` in the log. `tools\install_driver.cmd -Status` also lists old
  `TrackingOverrides` entries left in place.
- **Hand follows the headset tracking but the fingers don't move** — the skeleton hook isn't capturing:
  look for `OpticalTap:` lines in the log; the probe shows `no skeleton`.
- **Hands stutter or lag behind the streamer's** — the status line should read `mode=2 (event)` with
  matching `rates(src …, republished …)`: the driver passes each update on as it arrives. Without the pose
  hook (`OpticalTap: … pose tap unavailable`) it falls back to once per frame.
- **Hands overshoot or jitter (Steam Link)** — Steam Link streams the hand pose already predicted forward, with
  noisy velocities, and SteamVR extrapolates those again to the display time. The pose filter (`pose_filter`)
  smooths the pose, drops runaways and publishes its own velocities; compare it on and off live in SteamVR's
  settings, against Steam Link's own pointer (drawn on the headset from local data). It applies to Steam Link
  only (`pose_filter_types`): Virtual Desktop's hands are clean and pass through untouched; the log says which
  (`headset hand source …: pose filter …`). With *Debug captures* on (advanced settings), record the raw stream
  with `python tools/analyze_tap_capture.py --capture 10`: it also records what CyberFinger publishes over the
  same seconds and compares the two (wrist and a laser along the index finger: jitter, error, delay, occlusion
  holds). With the bridge running, the glove IMUs are recorded too, on the driver's clock: their timing against
  the optical rotation, and rotation jitter side by side. Replay a capture through the filter offline with
  `out\build\x64-Release\filter_eval.exe <capture.csv> [name=value ...]` (e.g. `beta=10 rotPrediction=0.5`):
  it reports position and laser-target (2 m) jitter and error, as displayed 0 and 40 ms ahead, for a still,
  slowly moving and fast moving hand.
- **Hands are noisy** — hold them still in view for ten seconds. The status line's `noise(held still …)`
  gives the source's jitter: RMS spread of the wrist and fingertips in the world, and of the fingertips
  relative to the wrist (`fingers`). Compare streamers or settings with it; `OpticalTap: … now follows …`
  names the source.
- **Buttons don't register** — the bridge must run in VR mode; the log shows
  `StudioLink: first glove packet (CFG2)`.
- **The left-palm pinch doesn't open the dashboard** — the probe should show `[SYSTEM pressed]` for the left
  hand; check `forward_tap_system_button`.

## Architecture

- `src/ServerProvider` — driver setup and the per-frame loop
- `src/CyberFingerController` — the two controller devices, output modes, inputs
- `src/OpticalTap` — Steam Link hand capture: MinHook observation hooks on `IVRDriverInput` and
  `IVRServerDriverHost::TrackedDevicePoseUpdated`
- `src/StudioLink` — UDP link to the bridges
- `src/SkeletonSynth` — procedural SteamVR hand (port of Valve's `handskeletonsimulation` sample), used when
  no tracked skeleton is available
- `bridge/` — the CyberFinger GUI (`cyberfinger_gui.py`), the protocol module (`cf_protocol.py`), the driver
  link and haptics display (`driver_link.py`, `haptics_view.py`), tools
- `archive/` — files from the earlier proof-of-concept driver, kept for reference

## License

The CyberFinger SteamVR driver is licensed under the GNU General Public License v3.0 (GPL-3.0-only).
Unless otherwise noted in individual source file headers, all source code in this repository is licensed under GPL-3.0-only. 
Any redistribution of this software—whether in source or binary form, including distribution in physical devices—must comply with the terms of GPL-3.0, 
including the obligation to provide corresponding source code and installation information for modified versions.

The full license text is provided in the LICENSE file. 

Third-party code: [MinHook](https://github.com/TsudaKageyu/minhook) (BSD-2-Clause, `third_party/minhook`);
`src/SkeletonSynth.cpp` ports Valve's OpenVR SDK sample `handskeletonsimulation` (BSD-3-Clause, notice in the file).

## Contributing

By contributing to this project, you agree to the Contributor License Agreement in CLA.md.
