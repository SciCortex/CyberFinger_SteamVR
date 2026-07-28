# CyberFinger — SteamVR Driver

A SteamVR driver that merges **two CyberFingers** + **hand tracking** into a pair of
virtual VR controllers with full skeletal hand data.

The bridge GUI fuses three sources into each controller:

| Source | Provides |
|--------|----------|
| CyberFinger controllers (BLE) | buttons, joystick, analog trigger, battery |
| Runtime hand tracking (e.g. Steam Link cameras, via OpenVR skeletal input) | 31-bone finger skeleton, 6DOF wrist pose |
| Controller IMUs | orientation — currently displayed and forwarded to SlimeVR; fusion into the controller pose is planned |

Note: this is a *work-in-progress*, and currently very alpha software. 

## Building

### Prerequisites

- CMake 3.16+
- C++17 compiler (MSVC 2019+, GCC 9+, Clang 10+)
- [OpenVR SDK](https://github.com/ValveSoftware/openvr) — clone it into the
  project root as `openvr/`, or set `-DOPENVR_SDK=<path>`
- Python WinRT
- "steamvr" branch of the CyberFingerFW_ESP32 installed on CyberFingers : https://github.com/DrSciCortex/CyberFingerFW_ESP32/tree/steamvr 

```bash
pip install winrt-Windows.Devices.Enumeration winrt-Windows.Devices.Bluetooth
pip install winrt-Windows.Devices.Bluetooth.GenericAttributeProfile winrt-Windows.Storage.Streams
```

Needs winrt v3.x 
```bash
pip show winrt-Windows.Devices.Bluetooth winrt-Windows.Devices.Enumeration winrt-runtime
```

### Windows

```bash
git clone https://github.com/ValveSoftware/openvr.git
mkdir build && cd build
cmake .. -G "Visual Studio 17 2022" -A x64
cmake --build . --config Release
```

or just use Visual Studio to build. It should install automatically in the standard steam driver location. 


### Linux

(This is WIP ... not yet confirmed working)

```bash
git clone https://github.com/ValveSoftware/openvr.git
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j$(nproc)
```

## Installation

### 1. Copy the driver to SteamVR

Copy the built driver folder into SteamVR's driver directory:

```
<Steam>/steamapps/common/SteamVR/drivers/cyberfinger/
├── driver.vrdrivermanifest
├── resources/
│   ├── settings/
│   │   ├── default.vrsettings      (default values)
│   │   └── settingsschema.vrsettings  (SteamVR Settings sliders)
│   └── input/
│       ├── cyberfinger_profile.json
│       └── cyberfinger_bindings.json
└── bin/
    └── win64/   (or linux64/)
        └── cyberfinger_controller.dll  (or .so)
```

(That is, if Visual Studio didn't do it already for you above)

**Alternative — register the build directory in place** (no copying; useful while
developing, since a rebuild is immediately live):

```bash
"<Steam>/steamapps/common/SteamVR/bin/win64/vrpathreg.exe" adddriver <repo>/out/build/x64-Release/driver/cyberfinger
```

Verify with `vrpathreg show`. A registered driver appears in *SteamVR Settings →
Startup/Shutdown → Manage Add-ons* — if `cyberfinger` is missing there, it is not
registered (or SteamVR is in safe mode after a crash, which disables add-ons).

### 2. Enable the driver

Add to `<Steam>/config/steamvr.vrsettings`:

```json
"driver_cyberfinger": {
    "enable": true,
    "handtracking_udp_port": 27015
},

"TrackingOverrides" : {
  "/devices/cyberfinger/CYBERFINGER_L" : "/user/hand/left",
  "/devices/cyberfinger/CYBERFINGER_R" : "/user/hand/right"
},
```

### 3. Run the cyberfinger bridge

Under the bridge directory:

```bash
pip install -r requirements.txt
python cyberfinger_gui.py
```

Select **VR Mode (controllers + hand tracking → SteamVR)** and press Start. The
headless `python cyberfinger_bridge.py` (add `--debug`) still exists, but it
forwards buttons only — no skeleton, no pose.

Launch *after* SteamVR is running but *before* your VR application.

Make sure your cyberfinger is in "VR mode" where each hand communicates directly over BLE with the cyberfinger_bridge using a custom protocol, 
not the legacy "Gamepad" mode (which has the two cyberfingers merged into one XInput device).
Note VR mode is available only for the steamvr branch of the firmware:
https://github.com/DrSciCortex/CyberFingerFW_ESP32/tree/steamvr
This version must be installed on your CyberFingers, or the steamvr driver will not work.  

## Configuration

All settings are in `default.vrsettings` or the global SteamVR settings file:

| Setting                    | Default          | Description                                  |
|----------------------------|------------------|----------------------------------------------|
| `enable`                   | `true`           | Enable/disable the driver                    |
| `serialNumber_left`        | `CYBERFINGER_L`  | Serial number for left controller            |
| `serialNumber_right`       | `CYBERFINGER_R`  | Serial number for right controller           |
| `handtracking_udp_port`    | `27015`          | UDP port for hand tracking data              |
| `grip_angle_x/y/z`         | `-60.0/35.0/0.0` | Wrist → controller grip rotation, degrees    |
| `pose_offset_x/y/z`        | `0.0/-0.10/0.0`  | Grip origin offset in controller-local metres |

The `grip_angle_*` and `pose_offset_*` values convert the tracked **wrist** pose
into the **grip** pose a controller is expected to publish. They are re-read at
10 Hz, so edits apply live — adjust them if your hands appear consistently
rotated or displaced from where they really are. `grip_angle_y` is mirrored
automatically for the left hand, as is `pose_offset_x`.

## Hand skeleton & 6DOF

The bridge reads the hand skeleton the VR runtime is already tracking (Steam
Link camera hand tracking, Ultraleap, …) through OpenVR **skeletal input**,
using its own action manifest in `bridge/assets/`. This requires the `openvr`
Python package (in `requirements.txt`) and works alongside whatever else is
running.

Practical notes, learned the hard way:

- **The skeleton only attaches once the hand devices deliver an input event.**
  In an empty SteamVR (no game, SteamVR Home disabled) that may not happen on
  its own. A **thumb-index pinch** attaches it instantly; so does starting any
  VR app. The bridge retries on its own and says so in its console.
- **Do not pick a binding in *Manage Controller Bindings* for CyberFinger
  Bridge.** Doing so can pin a legacy binding that silently disables the
  skeleton actions. The bridge detects this, warns, and clears the stale pin
  automatically the next time it starts while SteamVR is closed.
- 6DOF comes from the hand devices' poses, forwarded to the driver in the raw
  tracking universe. If no pose source is available the controllers report as
  untracked rather than teleporting to a fixed position.

# Wire Protocol (UDP)

Both packet types are sent to `127.0.0.1:<port>` (default 27015) and distinguished by their magic bytes.

## Hand Tracking Packet (bridge → driver)

Source: the bridge GUI (`cyberfinger_gui.py` + `vr_controller.py`), reading the
runtime hand skeleton via OpenVR skeletal input.
See `HandTrackingReceiver.h :: HandTrackingPacket`.

```
Offset  Size    Field
0       4       Magic: 0x4B535448 ('HTSK')
4       1       Version: 1
5       1       Hand: 0=left, 1=right
6       1       Confidence: 0-255
7       1       Flags: bit0 = pos/quat carry a real pose
8       12      Position: float[3] (xyz meters, RAW tracking universe)
20      16      Orientation: float[4] (wxyz quaternion)
36      868     Bones: float[31][7] (per bone: xyz pos + wxyz quat)
904     20      Curls: float[5] (thumb, index, middle, ring, pinky; 0-1)
─────────────────
Total: 924 bytes
```

Bones are **parent-relative** transforms in OpenVR's standard 31-bone hand
order, fed straight to `UpdateSkeletonComponent` — the driver performs no space
conversion or mirroring. Everything is little-endian and tightly packed.

The driver treats skeleton data as stale after 150 ms (falling back to a
gamepad-driven synthetic pose) and gamepad data after 250 ms (zeroing inputs),
so both packet types are streamed continuously rather than on change.

## Gamepad Packet (BLE bridge → driver)

Source: the Python bridge, forwarding VR GATT notifications from ESP32 CyberFinger devices.
See `HandTrackingReceiver.h :: GamepadPacket`.

```
Offset  Size    Field
0       4       Magic: 0x50474643 ('CFGP')
4       1       Hand: 0=left, 1=right
5       1       Buttons (bitmask):
                  bit0 = Trigger (digital)
                  bit1 = Grip
                  bit2 = B
                  bit3 = Joy click
                  bit4 = A
6       2       Joystick X: int16 (-32767..32767)
8       2       Joystick Y: int16 (-32767..32767)
10      1       Trigger analog: uint8 (0-255)
11      1       Battery percent: uint8 (0-100)
─────────────────
Total: 12 bytes
```

## SteamVR Input Mapping

| Gamepad | SteamVR Component | Notes |
|---------|-------------------|-------|
| Trigger (bit0) | `/input/trigger/value` | Analog from `trigger_analog`, digital fallback |
| Grip (bit1) | `/input/grip/value` | Digital (0 or 1) |
| B (bit2) | `/input/b/click` (R) `/input/y/click` (L) | Secondary button |
| Joy click (bit3) | `/input/joystick/click` | |
| A (bit4) | `/input/a/click` (R) `/input/x/click` (L) | Primary button |
| Joystick X | `/input/joystick/x` | Normalized to -1..1 |
| Joystick Y | `/input/joystick/y` | Normalized to -1..1, **inverted** |


## Troubleshooting

- **Controllers show up but no position**: no pose source. Check that hand
  tracking is enabled (Steam Link: controllers down, hands in camera view) and
  that the bridge's console reports the hands tracking. The driver logs its
  pose source every ~2 s in `vrserver.txt` as `POSE: bridge (HTSK)`,
  `POSE: source device` or `POSE: NO SOURCE (untracked)`.

- **Hands are consistently rotated or offset from where they really are**:
  tune `grip_angle_*` / `pose_offset_*` (see Configuration) — they apply live.

- **Buttons don't register**: check the bridge is running and receiving events,
  and that the CyberFingers are in "VR mode".

- **Skeleton not animating**: check the bridge's console. If it says the hands
  are inactive with `0 binding origin(s)`, do a **thumb-index pinch** — that
  usually completes binding attachment instantly. Also verify `vrserver.txt`
  shows `HandTrackingReceiver` messages.

- **Bridge appears in the binding UI but the skeleton stops working
  afterwards**: a legacy binding got pinned to the app. Close SteamVR and
  start the bridge — it clears stale pins automatically.

- **`cyberfinger` missing from Manage Add-ons**: the driver is not registered
  (`vrpathreg adddriver …`), or SteamVR is running in safe mode after a crash.

## Architecture

The project has two main components:

1. **SteamVR Driver** (`driver_cyberfinger.dll/.so`): Loaded by SteamVR,
   creates two virtual controller devices. Listens on a UDP port for skeleton,
   pose and button data, and updates SteamVR each frame. It performs no fusion
   of its own — it converts wrist → grip pose, estimates velocity, and
   publishes what it is given.

2. **Bridge GUI** (`bridge/cyberfinger_gui.py`, with `vr_controller.py`):
   Connects to the controllers over BLE, reads the runtime hand skeleton and hand
   device poses via OpenVR, fuses them, and streams both packet types to the
   driver. Also provides the visualisation, SlimeVR tracker emulation, and the
   non-VR gamepad modes.

The bridge exists as a separate process because SteamVR drivers cannot use the
client-side VR input API to read hand tracking from *other* drivers — they can
only provide input, not consume it.

Fusion deliberately lives in the bridge (`FusedVRMode._compose`), where the
controller IMU, camera skeleton and per-source uncertainty can be combined without
rebuilding the driver.

## License

The CyberFinger SteamVR driver is licensed under the GNU General Public License v3.0 (GPL-3.0-only).
Unless otherwise noted in individual source file headers, all source code in this repository is licensed under GPL-3.0-only. 
Any redistribution of this software—whether in source or binary form, including distribution in physical devices—must comply with the terms of GPL-3.0, 
including the obligation to provide corresponding source code and installation information for modified versions.

The full license text is provided in the LICENSE file. 

## Contributing

By contributing to this project, you agree to the Contributor License Agreement in CLA.md.

