# CyberFinger Bridge — Linux

GUI application that connects CyberFinger BLE controllers to your Linux PC
in three modes:

- **VR Mode** — fuses controller buttons, the runtime hand skeleton and 6DOF hand
  poses into the CyberFinger SteamVR driver's virtual controllers
- **Gamepad (Resonite)** — virtual Xbox 360 gamepad via uinput, mapped for Resonite
- **Gamepad (VRChat)** — virtual Xbox 360 gamepad via uinput with VRChat-optimal
  mapping plus OSC (UseLeft, Grab, Voice)

Independently of the mode, it can also emulate **SlimeVR trackers** from the
controller IMUs, and it visualises hand state, IMU orientation and the tracked
hand skeleton.

This is the Linux counterpart of `../bridge/`. The two share everything above
the platform transports — see [Shared code](#shared-code).

## Install (Arch Linux)

### 1. System packages

```bash
sudo pacman -S uv tk
sudo pacman -S gtk3 gobject-introspection-runtime
sudo pacman -S libayatana-appindicator
```

`gtk3` and `libayatana-appindicator` enable the AppIndicator system tray backend,
which supports a proper right-click context menu. Without them pystray falls back
to the bare X11 backend which has no menu support.

### 2. Bluetooth

Follow the [Arch Linux Bluetooth wiki](https://wiki.archlinux.org/title/Bluetooth)
for initial setup, then:

```bash
sudo systemctl enable --now bluetooth
```

Pair the CyberFinger controllers once:

```bash
bluetoothctl
# scan on
# pair XX:XX:XX:XX:XX:XX
# trust XX:XX:XX:XX:XX:XX
# connect XX:XX:XX:XX:XX:XX
```

### 3. uinput (Gamepad Mode)

```bash
# Load the kernel module
sudo modprobe uinput

# Make it load on boot
echo "uinput" | sudo tee /etc/modules-load.d/uinput.conf

# Create uinput group and add your user
sudo groupadd -f uinput
sudo usermod -aG uinput "$USER"

# Create udev rule
echo 'KERNEL=="uinput", GROUP="uinput", MODE="0660"' | \
    sudo tee /etc/udev/rules.d/99-uinput.rules

sudo udevadm control --reload-rules
sudo udevadm trigger

# Log out and back in for group membership to take effect
```

### 4. Python environment

```bash
cd bridge_linux
uv venv
uv pip install -r requirements.txt
```

### 5. Run

```bash
source .venv/bin/activate
python cyberfinger_gui_linux.py &
```

Run it from inside the repo checkout — it imports the shared `bridge_common`
package from the repo root.

## Python Dependencies

| Package | Purpose |
|---------|---------|
| bleak | BLE communication via BlueZ D-Bus |
| evdev | Virtual gamepad via uinput |
| pystray | System tray icon |
| pillow | Tray icon images |
| python-osc | VRChat OSC (optional) |
| pynput | F12 screenshot in VRChat mode (optional) |
| pyopenxr | Hand skeleton on Monado/WiVRn (optional) |
| openvr | Hand skeleton on SteamVR (optional) |

tkinter is provided by the system `tk` package (installed above).

Neither skeleton package is required: without them VR mode still streams
buttons/stick/trigger to the driver and the skeleton panels show a placeholder,
and the gamepad modes are unaffected.

## Hand skeleton backends

There are two, chosen automatically at startup:

| Backend | Module | Use on |
|---------|--------|--------|
| **OpenXR** (headless) | `../bridge_common/skeleton_openxr.py` | Monado / WiVRn |
| **OpenVR** (skeletal input) | `../bridge_common/skeleton.py` | SteamVR |

Selection probes the *active* OpenXR runtime: if it advertises
`XR_MND_headless` **and** `XR_EXT_hand_tracking`, the OpenXR backend wins. That
test is self-selecting rather than a platform check — `XR_MND_headless` is a
Monado extension, so SteamVR never advertises it and the OpenVR backend keeps
the job. Pin one explicitly with `"skeleton_backend": "openvr"` or `"openxr"`
in `settings.json`.

### Why WiVRn needs the OpenXR backend

Driving OpenVR through **xrizer** or **OpenComposite** does not work for this
bridge, and it isn't a bug that can be patched out:

- those layers implement only the `Background` and `Scene` application types and
  reject `Overlay` with `InitError_Init_InvalidApplicationType`; and, decisively,
- OpenXR delivers action input only to a session in `XR_SESSION_STATE_FOCUSED`,
  and a session reaches `FOCUSED` only by submitting frames. A bridge that
  renders nothing therefore never gets focus, so its skeletal actions stay
  inactive — this holds even while a scene app is running. SteamVR's `vrserver`
  serves every client at once; Monado focuses exactly one.

The OpenXR backend sidesteps both: `XR_MND_headless` gives a session with no
graphics binding that reaches `FOCUSED` without a single frame submitted, and
`xrLocateHandJointsEXT` is not action-based, so it needs no action manifest, no
binding files, and never contends with the running game for focus.

Its 26 joints (`PALM`, `WRIST`, then five finger chains with tips at
5/10/15/20/25) are exactly the layout the panels already draw, so nothing is
remapped anywhere.

> **Note:** the OpenXR backend leaves the driver's `HTSK` skeleton stream empty —
> that stream targets `driver_cyberfinger`, a SteamVR driver, which is not loaded
> in an OpenXR-only stack. VR mode still sends `CFGP` (buttons, stick, trigger,
> battery) regardless, and SlimeVR forwarding is unaffected.

## Shared code

Everything above the platform transports lives in `../bridge_common/` and is
imported by both this bridge and the Windows one, so the two cannot drift:

| Module | Contents |
|--------|----------|
| `protocol.py` | GATT wire format, variable-length IMU report decoding, `HandState` |
| `slimevr.py` | SlimeVR tracker emulation |
| `skeleton.py` | OpenVR hand-skeleton + 6DOF client (SteamVR) |
| `skeleton_openxr.py` | Native OpenXR headless hand tracking (Monado/WiVRn) |
| `vr_controller.py` | `FusedVRMode` — HTSK/CFGP driver stream |
| `panels.py` | Hand + skeleton visualisation canvases |
| `graphics.py` | Palette and 3D maths |
| `platform.py` | The only OS branch: config dir, SteamVR settings path, font |

What stays here is genuinely Linux-specific: BLE via bleak/BlueZ, the uinput
gamepad, tray icon compositing and the app shell.

## Settings

Stored in `~/.config/cyberfinger-bridge/settings.json`, written by the GUI.
A few options have no UI:

| Key | Default | Description |
|-----|---------|-------------|
| `slimevr_host` / `slimevr_port` | `127.0.0.1` / `6969` | SlimeVR server address |
| `skeleton_enabled` | `true` | Set false to disable the skeleton client entirely |
| `skeleton_backend` | `"auto"` | `"openvr"` / `"openxr"` to pin a backend |
| `skeleton_bisect` | `false` | Bring the OpenVR session up in logged stages (debugging; OpenVR backend only) |

## Platform Notes

| Feature | Windows | Linux |
|---------|---------|-------|
| BLE library | WinRT | bleak (BlueZ) |
| Device discovery | System enumeration | Paired devices via bluetoothctl |
| Hand assignment | Report `hand` byte | Which paired device the notification arrived on |
| Virtual gamepad | ViGEmBus + vgamepad | uinput + evdev |
| Config location | `%APPDATA%\CyberFingerBridge` | `~/.config/cyberfinger-bridge` |
| Skeleton source | OpenVR / SteamVR | OpenVR on SteamVR, or native OpenXR on Monado/WiVRn |

## IMU / GATT protocol

The CF01 notification is **variable length** — see
`CyberFingerFW_ESP32/src/vr_gatt.h`. A frozen 28-byte prefix (buttons, stick,
trigger, battery, seq, primary body quaternion) is followed by an `imu_present`
bitmask, then one block per set bit; absent IMUs are omitted entirely to cut
airtime. It is parsed by `imu_present`, never by total length — different slot
combinations can yield the same byte count.

| `imu_present` | Slot | Appended block |
|---------------|------|----------------|
| `0x01` | Body 1 (primary) | accel only — quaternion is in the header |
| `0x02` | Body 2 (secondary) | quaternion + accel |
| `0x04` | Joint | quaternion + accel |

Body 1 and Body 2 are redundant sensors at the *same* physical location, so only
one is forwarded to SlimeVR — the **body IMU 1/2** radio picks which, falling
back to the other when the chosen one is absent.

Accelerometer values are raw sensor-frame counts at 2048 LSB/g. Gravity is
removed using the same packet's quaternion before forwarding to SlimeVR.

Older firmware still works: a 12-byte report parses as buttons-only, and a
28-byte one as buttons + primary quaternion with no accel.

## Gamepad (Resonite) Mapping

Standard Xbox 360 layout — all CyberFinger inputs go to gamepad buttons/axes.

| CyberFinger   | Right Hand       | Left Hand        |
|---------------|------------------|------------------|
| Joystick      | ABS_RX / ABS_RY  | ABS_X / ABS_Y    |
| Trigger       | ABS_RZ (analog)  | ABS_Z (analog)   |
| Trigger btn   | BTN_A            | BTN_X            |
| Grip          | BTN_B            | BTN_Y            |
| Menu          | BTN_TR (RB)      | BTN_TL (LB)      |
| Joy click     | BTN_THUMBR (R3)  | BTN_THUMBL (L3)  |
| ST/SE         | BTN_START        | BTN_SELECT       |
| C             | DPAD_UP          | DPAD_RIGHT       |
| D             | DPAD_DOWN        | BTN_MODE (guide) |
| E             | DPAD_LEFT        | —                |

## Gamepad (VRChat) Mapping

VRChat-optimal mapping. Some inputs route to the gamepad, others go through
OSC on UDP `127.0.0.1:9000`, and right-C triggers an F12 screenshot via
synthetic keyboard input.

Enable OSC in VRChat: **Action Menu → Options → OSC → Enabled**.
See the [VRChat OSC-as-input docs](https://docs.vrchat.com/docs/osc-as-input-controller).

### Gamepad side

| CyberFinger       | Right Hand          | Left Hand                  |
|-------------------|---------------------|----------------------------|
| Joystick          | ABS_RX / ABS_RY (turn + look) | ABS_X / ABS_Y (move) |
| Trigger (analog)  | ABS_RZ (Use/Interact) | — (routed to OSC only)   |
| Trigger btn       | (folded into ABS_RZ) | (routed to OSC only)      |
| Grip              | (OSC only)          | (OSC only)                 |
| Menu              | BTN_THUMBR (Action Menu R) | BTN_START (Quick Menu) |
| Joy click         | BTN_A (Jump — either hand)  | BTN_A (Jump — either hand) |
| C                 | (F12 screenshot)    | BTN_X (Mute)               |
| D                 | DPAD_RIGHT          | DPAD_LEFT                  |
| E                 | DPAD_UP             | DPAD_DOWN                  |
| ST/SE             | (OSC only — chatbox) | (OSC only — voice mute)   |

### OSC side (UDP 9000, path `/input/*` and `/chatbox/*`)

| CyberFinger input | OSC address          | Value / behaviour                          |
|-------------------|----------------------|--------------------------------------------|
| Left Trigger      | `/input/UseLeft`     | `1` on press, `0` on release               |
| Right Grip        | `/input/GrabRight`   | tap (<200 ms) toggles; hold releases on lift |
| Left Grip         | `/input/GrabLeft`    | tap (<200 ms) toggles; hold releases on lift |
| Right ST/SE       | `/chatbox/input`     | `["", false, false]` — opens chatbox (rising edge) |
| Left ST/SE        | `/input/Voice`       | `1` on press, `0` on release (mute toggle) |
| Right C           | (keyboard)           | F12 screenshot on rising edge (pynput)     |

## VR Mode → driver

VR mode streams two packet types to the CyberFinger SteamVR driver over UDP
(default port 27015, driver setting `handtracking_udp_port`):

| Packet | Size | Carries |
|--------|------|---------|
| `CFGP` | 12 B | buttons, joystick, trigger, battery |
| `HTSK` | 924 B | 31 parent-space bones, 5 finger curls, confidence, wrist 6DOF pose |

`HTSK` is only sent while a hand skeleton is actually being tracked; with no
skeleton source, VR mode degrades to `CFGP` only — the behaviour of the older
Linux bridge.

## Files

```
cyberfinger_gui_linux.py    Main application (BLE, gamepad modes, app shell)
requirements.txt            Python dependencies
test_ble_connect.py         BLE connection approach tester (debugging)
assets/
  icon.png                 Full-size icon
  icon_32x32.png           Tray icon (running)
  icon_32x32_bw.png        Tray icon (idle)
../bridge_common/           Shared with the Windows bridge — see above
```
