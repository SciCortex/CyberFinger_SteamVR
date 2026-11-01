# CyberFinger Fusion Studio

A single-window application that fuses three sensor streams into one live picture of the body, the arm and the whole
hand:

| source | hardware | what it contributes |
|---|---|---|
| headset camera | Quest hand tracking through OpenXR | 26-joint hand skeleton and the head pose while the hand is in view |
| CyberFinger IMUs | CyberFinger over BLE (wrist + knuckle sensors) | hand orientation and wrist bend when the camera cannot see the hand |
| forearm EMG | MindRove 8-channel armband | finger posture and the key postures (open, fist, point, pinch, victory, rest) out of view |

When the camera loses the hand, the Studio keeps drawing it: the wrist position is predicted from the CyberFinger IMUs, the
orientation comes from the IMUs with a camera-taught mounting calibration, the finger posture from the EMG, and a
recognised key posture snaps the hand to its template. Every source has an on/off switch so the contribution of each
sensor can be shown on its own.

With **⇢ send this hand to SteamVR** (above the views) the fused hand goes to the CyberFinger driver as its FUSED pose
source, so SteamVR apps get the hand even where the headset camera cannot see it.

## Run

**The Windows app (no Python needed):** unzip `CyberFingerFusionStudio.zip` into a folder you can write to (e.g.
Documents — not Program Files: the Studio saves its refined EMG posture model next to itself) and run
`CyberFingerFusionStudio.exe`. To check an installation without any hardware, run
`CyberFingerFusionStudio.exe --selftest`: it starts the Studio, streams the built-in EMG simulator, starts the
headset helper, quits, and writes the result to `%APPDATA%\CyberFingerBridge\fusion_studio_selftest.txt`.

**From source** (Python 3.12):

```
cd bridge
pip install -r requirements-fusion-studio-lock.txt
python fusion_studio.py
```

`requirements-fusion-studio.txt` lists the same packages without versions. pyopenxr must stay at 1.1.5301 for now:
1.1.6302 ships an API-layer DLL that does not load.

Top bar:

- **▶ Start CyberFinger** — connects the CyberFinger over BLE (VR mode, as in the CyberFinger Bridge) and streams its IMUs.
- **▶ Start EMG** — starts the MindRove armband stream (`device`), or the built-in `simulator` for a demo without hardware.
- **◉ Start preview** — opens the OpenXR session and streams the headset's optical hand skeleton and head pose.
- **▲ Console** — shows the log.

The window's left column (it scrolls when the window is short) has the source switches (optical, wrist IMU, knuckle IMU,
EMG, position model, gate), the view and fusion options, the continual-learning switch and the hand-size slider. The centre shows the body
from the front and from above with the tracked arm and hand; the right column shows the raw signals: hand close-up with
per-finger curls, both IMUs, the eight EMG channels and the band accelerometer, the predicted position and its depth
trace, and the headset pose.

## A session

1. Put on the CyberFinger and the armband, start the three sources, look straight ahead and press **⌖ Set forward** so the body
   heading is locked.
2. Show the hand to the camera for a few seconds while moving it a little: this calibrates the CyberFinger IMUs against the
   camera (orientation, sensor mounting) and starts the continual learning of the EMG finger posture.
3. Show each key posture (open, fist, point, pinch, victory, and rest: the relaxed, half-curled hand) to the camera
   for a few seconds. The recogniser teaches
   itself today's EMG for each posture and then recognises them out of view.
4. Move the hand out of the camera's view (to the side, behind the back): the hand keeps its position, orientation and
   posture from the IMUs and the EMG. Toggle the source switches to see what each sensor contributes.

## Fusion and posture options (left column)

| switch | default | what it does |
|---|---|---|
| smooth camera ↔ prediction switches | on | when the camera loses or finds the hand, the hand blends between the camera and the prediction instead of jumping |
| distrust the camera when the IMU disagrees | on | a camera hand that turns away from what the knuckle IMU measures (a mislabelled or swapped hand) is set aside |
| camera ↔ EMG weight w = f(m, d) | on | per finger, how much the camera is trusted against the EMG, from the camera's own confidence and the disagreement |
| keep the hand anatomical | on | joint limits and the user's bone lengths, so a hidden finger never folds into an impossible pose |
| hold the posture while the arm turns | on | turning the forearm changes the EMG by itself; the key posture then needs a clearer lead to switch |
| expected next posture (prior) | on | which posture usually follows which (open is the hub; a relaxing fist rests or opens) — shapes the hand only passes through on the way are not shown |
| ⚡ transition flags | off | the EMG burst that starts a posture change (~60 ms before the fingers move): right after one a new posture is shown quickly, without one the hand holds steadier |
| ◍ soft hand + flag HMM | off | out of view the hand is drawn from a belief over the postures that the transition flags release: faster than the snap, and it holds while the arm turns |
| ↳ anticipate from MY transitions | on | with the soft hand, right after a flag the hand leans toward the posture this user usually goes to next |

The transition flags come from `transition_events.py`, fed with the armband's EMG every frame; `transition_model.npz`
is the learned flag detector (trained on recordings of real posture changes) and the user's own transition counts.

## Files

- `fusion_studio.py` — the application (CyberFinger BLE, SteamVR driver link, OpenXR preview, EMG dashboard, fusion, drawing).
- `fusion_studio_selftest.py` — `--selftest`: checks that every component and model loaded (no hardware needed).
- `fusion_studio.spec`, `build_fusion_studio.bat` — the Windows app build.
- `openxr_skeleton.py`, `openxr_skeleton_provider.py` — OpenXR hand-tracking source (runs the session in a helper process).
- `armband_panel.py`, `armband/` — MindRove EMG stream, filters, simulator, feature extraction.
- `gesture_skeleton.py` — EMG feature vector shared by the posture models.
- `../fusion/extrinsic.py`, `../fusion/orient_fusion.py` — camera ↔ IMU calibration and orientation fusion.
- `../fusion/gate.py`, `../fusion/occlusion.py` — occlusion gate (out of view / behind an object / self-occluded) and per-finger visibility from the joints and head pose.
- `mount_calib.py` — camera-taught IMU mounting calibration (live wrist bend out of view).
- `hybrid_position.py`, `hybrid_position_weights.npz` — out-of-view wrist-position model (forearm kinematics + learned elbow motion).
- `key_postures.py`, `key_postures_model2.npz` — key-posture recogniser (EMG covariance features, MLP, camera self-teaching),
  the posture gate and the soft hand's belief; the camera labels the six postures from the joints (`cam_label`).
- `transition_events.py`, `transition_model.npz` — transition flags (the EMG burst that starts a posture change) and where
  they lead.
- `finger_fusion.py` — the per-finger camera ↔ EMG weight and the anatomical hand.
- `steamvr_link.py` — the fused hand to the CyberFinger driver (FUSED pose source) and the driver's headset hand as the camera.
- `posture_pose_model.npz`, `hand_prior.npz` — EMG finger-posture regressor and the anatomical hand prior.

Settings (CyberFinger options, camera field of view, IMU slots) are shared with the CyberFinger Bridge in
`%APPDATA%\CyberFingerBridge\settings.json`.

## Build the Windows app

```
cd bridge
pip install -r requirements-fusion-studio-lock.txt pyinstaller
build_fusion_studio.bat
```

`fusion_studio.spec` makes a folder build, `dist\CyberFingerFusionStudio\` (`CyberFingerFusionStudio.exe` and its
`_internal` folder: Python, the libraries, the MindRove SDK and OpenXR loader DLLs, the models). The script then runs the
exe's self-test and zips the folder into `dist\CyberFingerFusionStudio.zip`. The same exe is the headset's OpenXR
helper process (`--openxr-provider`, started by the Studio itself). Build from a path without your user name (e.g.
`C:\Build\CyberFinger\`): module paths end up in the exe.
