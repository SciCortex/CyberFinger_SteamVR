# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Runtime hand skeleton — reads what the VR runtime is tracking.

Reads the hand skeleton the VR runtime tracks (e.g. Steam Link's camera-based
hand tracking) plus 6DOF hand poses, for display under each hand panel and for
the fused VR mode's driver stream.

Backend contract (duck-typed, like the bridge modes): .start(), .stop(),
.status (short string for the panel placeholder), and .hands — a 2-list indexed
by hand (0=left, 1=right) holding either None or a tuple of (x, y, z) joint
positions in a wrist-origin space. Backends swap whole tuples in atomically, so
readers need no lock.

The backend here is OpenVR skeletal input, for SteamVR. Its Overlay app type
attaches to a running SteamVR without a graphics session and without contending
with the focused game — something OpenXR on SteamVR cannot do yet (no headless,
XR_EXTX_overlay still provisional).

It does NOT work through an OpenVR-on-OpenXR layer (xrizer, OpenComposite) in
front of Monado/WiVRn, and not for want of trying: those layers implement only
the Background and Scene app types, and more fundamentally OpenXR gives action
input only to a FOCUSED session, which requires submitting frames. A bridge
that renders nothing never gets focus. skeleton_openxr.py is the backend for
those stacks; create_skeleton_source() picks between them.
"""

import json
import math
import os
import sys
import threading
import time

from .graphics import matrix_to_quat, relative_pose
from .platform import (VRMANIFEST_BINARY_KEY, config_dir, shared_asset,
                       steamvr_settings_path)

# pyopenvr. Broad except: the import can also fail on a missing
# openvr_api.dll / libopenvr_api.so, not just an absent package.
#
# The hasattr guard is not paranoia: the repo root carries the OpenVR SDK in an
# `openvr/` directory, and if that ever precedes site-packages on sys.path it
# imports as an empty NAMESPACE PACKAGE. `import openvr` then succeeds while
# every API call raises AttributeError, which this client would otherwise
# swallow as "runtime not running" and retry forever. Require a real symbol.
try:
    import openvr
    HAS_OPENVR = hasattr(openvr, "init")
except Exception:
    HAS_OPENVR = False

# Hand skeleton indices: 0 root/palm, 1 wrist, then five finger chains off the
# wrist, tips at 5/10/15/20/25. This layout is shared by SteamVR's native
# 31-bone skeleton (26-30 are aux bones, ignored) and the 26-bone OpenXR-style
# set Steam Link reports; bone count itself is queried from the runtime.
SKELETON_CHAINS = (
    (1, 2, 3, 4, 5),          # thumb
    (1, 6, 7, 8, 9, 10),      # index
    (1, 11, 12, 13, 14, 15),  # middle
    (1, 16, 17, 18, 19, 20),  # ring
    (1, 21, 22, 23, 24, 25),  # pinky
)
SKELETON_TIPS = frozenset((5, 10, 15, 20, 25))

SKELETON_ACTION_SET = "/actions/cyberfinger"
SKELETON_ACTIONS = ("/actions/cyberfinger/in/skeleton_left",
                    "/actions/cyberfinger/in/skeleton_right")

SKELETON_APP_KEY = "drscicortex.cyberfinger.bridge"

ACTION_MANIFEST_NAME = "cyberfinger_actions.json"

# VR events worth narrating in the console — resolved by name at runtime so a
# pyopenvr build lacking one just skips it.
_SKELETON_EVENTS = (
    "VREvent_TrackedDeviceActivated",
    "VREvent_TrackedDeviceDeactivated",
    "VREvent_TrackedDeviceRoleChanged",
    "VREvent_TrackedDeviceUserInteractionStarted",   # headset put on
    "VREvent_TrackedDeviceUserInteractionEnded",     # headset taken off
    "VREvent_EnterStandbyMode",
    "VREvent_LeaveStandbyMode",
    "VREvent_Input_BindingLoadFailed",
    "VREvent_Input_BindingLoadSuccessful",
    "VREvent_Input_ActionManifestReloaded",
    "VREvent_SceneApplicationChanged",
)

_HMD_ACTIVITY_LEVELS = {
    "k_EDeviceActivityLevel_Unknown": "activity unknown",
    "k_EDeviceActivityLevel_Idle": "idle (not worn)",
    "k_EDeviceActivityLevel_UserInteraction": "active (worn)",
    "k_EDeviceActivityLevel_UserInteraction_Timeout": "recently active",
    "k_EDeviceActivityLevel_Standby": "standby",
    "k_EDeviceActivityLevel_Idle_Timeout": "idle timeout",
}


# Plain-English follow-ups for the InitError codes that mean something more
# specific than "VR is down".
_INIT_ERROR_HINTS = {
    "InitError_Init_NoServerForBackgroundApp": "start the VR runtime first",
    "InitError_Init_HmdNotFound": "headset not connected",
    "InitError_Init_HmdNotFoundPresenceFailed": "headset not connected",
    "InitError_Init_InvalidApplicationType":
        "this runtime rejects the app type — expected on OpenVR-on-OpenXR "
        "layers, which serve only the focused app",
    "InitError_Init_PathRegistryNotFound":
        "no runtime registered in openvrpaths.vrpath",
}


def _init_error_hint(exc):
    hint = _INIT_ERROR_HINTS.get(type(exc).__name__)
    return f" — {hint}" if hint else ""


def _clean_pinned_bindings_offline(log):
    """Drop workshop binding pins for our app key from steamvr.vrsettings.

    SteamVR's binding UI can autosave a legacy workshop binding as this app's
    pinned selection, which silently disables our skeleton actions (see
    _check_pinned_binding). Editing the file is only safe while vrserver is
    down — it rewrites the file on exit — so this runs from the retry path
    after openvr.init fails. Only vr-input-workshop:// pins are dropped; a
    deliberately hand-picked local binding survives. Returns True if the file
    was changed; a no-op under runtimes with no such file.
    """
    path = steamvr_settings_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False
    section = data.get(SKELETON_APP_KEY)
    if not isinstance(section, dict):
        return False
    removed = []
    for key in list(section.keys()):
        if not key.endswith("_steamvrinput"):
            continue
        val = section[key]
        if isinstance(val, str) and not val.startswith("vr-input-workshop://"):
            continue  # a non-workshop pin was chosen on purpose; keep it
        removed.append(key)
        del section[key]
    if not removed:
        return False
    if not section:
        del data[SKELETON_APP_KEY]
    try:
        tmp = path + ".cyberfinger.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=3)
        os.replace(tmp, path)
    except Exception as e:
        log(f"Skeleton: could not clean steamvr.vrsettings: {e!r}")
        return False
    log("Skeleton: removed stale binding pin(s) from steamvr.vrsettings: "
        + ", ".join(removed))
    return True


def _write_app_manifest():
    """Write a .vrmanifest reflecting how this process was actually launched.

    Registering it (plus identifyApplication) is what makes SteamVR show
    "CyberFinger Bridge" in Manage Controller Bindings instead of filing us
    under an auto-generated interpreter key. Generated at runtime because the
    truthful binary path differs between running from source and a frozen
    build. Returns the manifest path.
    """
    if getattr(sys, "frozen", False):
        binary, arguments = sys.executable, ""
    else:
        binary = sys.executable
        arguments = f'"{os.path.abspath(sys.argv[0])}"'
    manifest = {
        "applications": [{
            "app_key": SKELETON_APP_KEY,
            "launch_type": "binary",
            VRMANIFEST_BINARY_KEY: binary,
            "arguments": arguments,
            "is_dashboard_overlay": False,
            "strings": {
                "en_us": {
                    "name": "CyberFinger Bridge",
                    "description": "CyberFinger bridge — hand skeleton display",
                },
            },
        }],
    }
    cfg_dir = config_dir()
    os.makedirs(cfg_dir, exist_ok=True)
    path = os.path.join(cfg_dir, "cyberfinger.vrmanifest")
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)
    return path


def create_skeleton_source(log=None, bisect=False, backend="auto"):
    """Pick the skeleton backend for this host, or None if unavailable.

    "auto" prefers the native OpenXR backend whenever the ACTIVE OpenXR runtime
    advertises XR_MND_headless + XR_EXT_hand_tracking. That test is
    self-selecting rather than a platform check: XR_MND_headless is a Monado
    extension, so SteamVR (Windows or Linux) never advertises it and the OpenVR
    backend keeps the job, while a Monado/WiVRn stack — where OpenVR-on-OpenXR
    layers structurally cannot serve a non-focused app — gets the backend that
    works there. Override with "openvr"/"openxr" ("skeleton_backend" in
    settings.json) to pin one.

    bisect=True ("skeleton_bisect" in settings.json) brings the OpenVR session
    up in staged steps with 20 s holds, so if the runtime falls over the last
    stage announced in the console names the culprit. OpenVR backend only.

    Note the OpenXR backend leaves .driver_feed empty: the HTSK skeleton stream
    targets driver_cyberfinger, a SteamVR driver, which is not loaded in an
    OpenXR-only stack. VR mode still streams CFGP buttons there regardless.
    """
    log = log or (lambda msg: None)

    if backend not in ("auto", "openvr", "openxr"):
        log(f"Skeleton: unknown backend {backend!r}, using auto")
        backend = "auto"

    if backend != "openvr":
        try:
            from .skeleton_openxr import (OpenXRSkeletonSource,
                                          openxr_hand_tracking_available)
            if openxr_hand_tracking_available():
                log("Skeleton: using the native OpenXR backend "
                    "(headless hand tracking)")
                return OpenXRSkeletonSource(log)
            if backend == "openxr":
                log("Skeleton: OpenXR backend pinned but this runtime lacks "
                    "XR_MND_headless / XR_EXT_hand_tracking")
                return None
        except Exception as e:
            if backend == "openxr":
                log(f"Skeleton: OpenXR backend unavailable ({type(e).__name__})")
                return None

    if HAS_OPENVR:
        return OpenVRSkeletonSource(log, bisect=bisect)
    return None


class OpenVRSkeletonSource:
    """Polls the OpenVR runtime for hand skeletons in a background thread.

    Connects as a Background app first so it never launches SteamVR itself;
    while the runtime is down it just retries quietly.
    """

    RETRY_S = 5.0
    HOLD_S = 20.0  # per-stage hold in bisect mode

    def __init__(self, log=None, bisect=False):
        self._log = log or (lambda msg: None)
        self._bisect = bisect
        self._poll_actions = True
        self._ready_at = 0.0
        # Per hand: (rot_3x3_rows, head_local_pos_xyz, distance_m, head_local
        # velocity) or None. World pose of the hand device relative to the HMD,
        # for the 6DOF display. Swapped atomically like .hands.
        self.pose_info = [None, None]
        # Driver stream (VR fusion mode): when feed_enabled, each tracked hand
        # publishes (bones31x7 parent-space, curls5, confidence, (pos, quat),
        # pose_valid) — the payload FusedVRMode packs into HTSK packets.
        self.feed_enabled = False
        self.driver_feed = [None, None]
        # Per hand: (position, rotation rows) in the raw tracking universe —
        # what the driver needs, as opposed to pose_info's head-relative form.
        self.raw_pose = [None, None]
        self.hands = [None, None]   # 0 = left, 1 = right
        self.status = "starting..."
        self._running = False
        self._thread = None
        self._ready = False
        self._logged_waiting = False
        self._offline_cleaned = False
        self._reset_requested = False
        self._reset_count = 0
        self._vrin = None
        self._system = None
        self._actions = [None, None]
        self._action_set = None
        self._event_names = {getattr(openvr, n): n[8:] for n in _SKELETON_EVENTS
                             if hasattr(openvr, n)}
        self._activity_names = {getattr(openvr, k): v
                                for k, v in _HMD_ACTIVITY_LEVELS.items()
                                if hasattr(openvr, k)}

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._teardown()

    # ── OpenVR session ──

    def _init_openvr(self):
        self._stage_connect()
        self._hold("1/5 client connected (idle)")
        self._stage_identity()
        self._hold("2/5 app identity registered")
        self._stage_actions()
        self._hold("3/5 action manifest + handles loaded")

        # In bisect mode stage 4 is passive polling (events + HMD activity
        # only); _loop promotes to stage 5 (action polling) after the hold.
        self._poll_actions = not self._bisect
        self._ready = True
        self._ready_at = time.time()
        self.status = "connected"
        self._connected_at = time.time()
        self._hand_active = [None, None]   # tri-state: unknown / False / True
        self._ever_active = False
        self._bone_count = [0, 0]
        self._err_logged = [False, False]
        self._hmd_activity = None
        self._last_diag = time.time()
        self._inactive_since = None
        self._ctype_logged = {}
        self._logged_waiting = False
        self._reset_requested = False
        self._log("Skeleton: connected to the OpenVR runtime")
        # Binding attachment completes on the device's first delivered input
        # event (observed: a thumb-index pinch attaches instantly after
        # standby). A haptic pulse is the one output we can push without bound
        # actions; on some stacks it nudges that same path awake.
        for role in (openvr.TrackedControllerRole_LeftHand,
                     openvr.TrackedControllerRole_RightHand):
            try:
                idx = self._system.getTrackedDeviceIndexForControllerRole(role)
                if idx != openvr.k_unTrackedDeviceIndexInvalid:
                    self._system.triggerHapticPulse(idx, 0, 1000)
            except Exception:
                pass
        if self._bisect:
            self._log("Skeleton BISECT: stage 4/5 passive polling "
                      f"(events + HMD activity) — holding {int(self.HOLD_S)} s")
        self._log_controller_types()

    def _stage_connect(self):
        # Probe as Background first: that type never auto-launches SteamVR, so
        # the bridge stays passive while VR is down. A failure here is the
        # ordinary "VR is down" signal and belongs to the retry path.
        #
        # NEVER init as Scene: that type asks the runtime to bring a session
        # up, which launches SteamVR even when another runtime is registered.
        openvr.init(openvr.VRApplication_Background)

        # Then prefer Overlay where the runtime supports it — overlay apps'
        # action sets keep getting pumped even in the void (no scene app; this
        # machine runs with SteamVR Home disabled), where a Background app's
        # skeleton bindings may never attach origins.
        #
        # That is a SteamVR trait, not an OpenVR guarantee. xrizer and
        # OpenComposite (OpenVR-on-OpenXR, used with Monado/WiVRn) implement
        # only Background and Scene and reject the rest with
        # InitError_Init_InvalidApplicationType. Treating that as a dead
        # runtime is what pinned the skeleton at "will retry" there forever, so
        # fall back to the Background session that already works.
        self.app_type = "Background"
        overlay_type = getattr(openvr, "VRApplication_Overlay", None)
        if overlay_type is not None:
            openvr.shutdown()
            try:
                openvr.init(overlay_type)
                self.app_type = "Overlay"
            except Exception as e:
                if "InvalidApplicationType" not in type(e).__name__:
                    raise
                openvr.init(openvr.VRApplication_Background)
        self._log(f"Skeleton: connected as an OpenVR {self.app_type} app")
        self._system = openvr.VRSystem()
        self._vrin = openvr.VRInput()
        # Hold a real (hidden) overlay handle, not just the app type: after the
        # HMD cycles through standby in the void, vrserver stops attaching
        # binding origins for clients without one.
        try:
            self._overlay = openvr.VROverlay().createOverlay(
                "drscicortex.cyberfinger.bridge.anchor", "CyberFinger Bridge")
        except Exception as e:
            self._overlay = None
            self._log(f"Skeleton: overlay anchor failed: {type(e).__name__}")

    def _stage_identity(self):
        # Identify as our own app key so the binding UI lists us as
        # "CyberFinger Bridge" rather than an auto-generated interpreter entry.
        # Best-effort: skeleton reading works without it, rebinding does not.
        try:
            vrapps = openvr.VRApplications()
            vrapps.addApplicationManifest(_write_app_manifest(), True)  # temporary
            vrapps.identifyApplication(os.getpid(), SKELETON_APP_KEY)
        except Exception as e:
            self._log(f"Skeleton: app identity registration failed: {e!r}")

    def _stage_actions(self):
        self._manifest_path = shared_asset(ACTION_MANIFEST_NAME)
        self._vrin.setActionManifestPath(self._manifest_path)
        self._action_set = self._vrin.getActionSetHandle(SKELETON_ACTION_SET)
        self._actions = [self._vrin.getActionHandle(a) for a in SKELETON_ACTIONS]

    def _hold(self, label):
        """In bisect mode, announce the stage and idle through its window so a
        runtime-side death lands unambiguously inside one stage."""
        if not self._bisect:
            return
        self._log(f"Skeleton BISECT: stage {label} — holding {int(self.HOLD_S)} s")
        deadline = time.time() + self.HOLD_S
        while self._running and time.time() < deadline:
            time.sleep(0.2)
        if not self._running:
            raise RuntimeError("stopped during bisect hold")

    def _log_controller_types(self):
        """Log each hand's controller type — this is the string a binding file
        must name, so it is the first thing to check when nothing draws."""
        for role, name in ((openvr.TrackedControllerRole_LeftHand, "L"),
                           (openvr.TrackedControllerRole_RightHand, "R")):
            try:
                idx = self._system.getTrackedDeviceIndexForControllerRole(role)
                if idx == openvr.k_unTrackedDeviceIndexInvalid:
                    continue
                ctype = self._system.getStringTrackedDeviceProperty(
                    idx, openvr.Prop_ControllerType_String)
            except Exception:
                continue
            if ctype and ctype != self._ctype_logged.get(name):
                self._ctype_logged[name] = ctype
                self._log(f"Skeleton: {name} controller type '{ctype}'")
                self._check_pinned_binding(ctype)

    def _check_pinned_binding(self, ctype):
        """Warn if a saved workshop binding pins this controller type.

        Opening SteamVR's binding UI on an app can autosave a legacy workshop
        binding as the app's "current" selection (steamvr.vrsettings, key
        <ctype>_250820_CurrentURL_steamvrinput). A pin overrides our
        default_bindings entirely, and a legacy binding carries no skeleton
        actions — so the skeleton goes permanently inactive with no error
        anywhere.

        Reads the settings FILE, never the IVRSettings API: every vrserver
        c0000005 observed followed an IVRSettings call from this client within
        seconds (getString included), while runs without any settings IPC were
        crash-free — so this client does not speak IVRSettings at all. Repair
        also happens on the file, offline — see _clean_pinned_bindings_offline,
        run while the runtime is down.
        """
        try:
            with open(steamvr_settings_path(), "r", encoding="utf-8") as f:
                section = json.load(f).get(SKELETON_APP_KEY, {})
            val = section.get(f"{ctype}_250820_CurrentURL_steamvrinput")
        except Exception:
            return
        if val and str(val).startswith("vr-input-workshop://"):
            self._log(f"Skeleton: WARNING — saved binding {val} overrides the "
                      f"defaults for '{ctype}'; skeleton will stay inactive. "
                      "Fix: close SteamVR and relaunch this bridge (auto-clean), "
                      "or pick the CyberFinger default binding in SteamVR.")

    def _teardown(self):
        self._ready = False
        self.hands = [None, None]
        self.pose_info = [None, None]
        self.driver_feed = [None, None]
        self.raw_pose = [None, None]
        self._vrin = None
        self._system = None
        try:
            openvr.shutdown()
        except Exception:
            pass

    def _loop(self):
        while self._running:
            if not self._ready:
                try:
                    self._init_openvr()
                except Exception as e:
                    self._teardown()
                    self.status = "VR runtime not running"
                    if not self._logged_waiting:
                        self._logged_waiting = True
                        # Name the actual InitError. A bare "no runtime" hides
                        # causes that are not "VR is down" at all — an
                        # unsupported app type reads identically to a missing
                        # headset otherwise, and that cost real debugging time.
                        self._log(f"Skeleton: OpenVR unavailable "
                                  f"({type(e).__name__}), will retry"
                                  + _init_error_hint(e))
                    # With vrserver down it is safe to sweep out any stale
                    # workshop binding pin that would mute the skeleton.
                    if not self._offline_cleaned:
                        self._offline_cleaned = True
                        try:
                            _clean_pinned_bindings_offline(self._log)
                        except Exception:
                            pass
                    # Sleep in short slices so stop() stays responsive.
                    deadline = time.time() + self.RETRY_S
                    while self._running and time.time() < deadline:
                        time.sleep(0.2)
                    continue
            try:
                self._poll()
            except Exception:
                self._teardown()
                self.status = "VR runtime lost, retrying"
                self._log("Skeleton: lost the OpenVR runtime connection")
                continue
            if self._reset_requested:
                # Bindings never attached (input context built while the HMD
                # was asleep). A fresh client connect attaches immediately —
                # the manual-reload observation, automated.
                self._reset_requested = False
                self._log("Skeleton: bindings never attached — reconnecting")
                self._teardown()
                self.status = "reconnecting..."
                continue
            if (self._bisect and not self._poll_actions
                    and time.time() - self._ready_at >= self.HOLD_S):
                self._poll_actions = True
                self._log("Skeleton BISECT: stage 5/5 full action polling "
                          "(updateActionState + skeletal reads)")
            time.sleep(1.0 / 30.0)

    def _poll(self):
        # A quit event means the runtime is going down — raise into the retry
        # path so the session is torn down promptly instead of erroring out
        # call by call while the server waits on us to exit.
        ev = openvr.VREvent_t()
        while self._system.pollNextEvent(ev):
            if ev.eventType == openvr.VREvent_Quit:
                self._system.acknowledgeQuit_Exiting()
                raise RuntimeError("VR runtime quit")
            name = self._event_names.get(ev.eventType)
            if name:
                self._log(f"Skeleton: event {name} (device {ev.trackedDeviceIndex})")
            if ev.eventType in (openvr.VREvent_TrackedDeviceActivated,
                                openvr.VREvent_TrackedDeviceRoleChanged):
                self._log_controller_types()
                # Devices returning from standby may accept a different bone
                # count, and any earlier read failure is stale news — reset so
                # recovery is attempted and new failures get logged again.
                self._bone_count = [0, 0]
                self._err_logged = [False, False]
            # A scene app starting is the one event known to un-wedge
            # vrserver's binding attachment, so it re-arms fast reconnects.
            # Device churn does NOT — it's constant with camera hand tracking.
            if ev.eventType == getattr(openvr,
                                       "VREvent_SceneApplicationChanged", -1):
                self._reset_count = 0

        # HMD activity explains most "why is nothing tracking" confusion —
        # Steam Link only streams hand skeletons while the headset is worn.
        try:
            lvl = self._system.getTrackedDeviceActivityLevel(
                openvr.k_unTrackedDeviceIndex_Hmd)
        except Exception:
            lvl = None
        if lvl != self._hmd_activity:
            self._hmd_activity = lvl
            self._log("Skeleton: HMD "
                      + self._activity_names.get(lvl, f"activity {lvl}"))

        # World poses for the 6DOF display. Device poses come from IVRSystem,
        # not the skeletal actions, so this works even while the skeleton is
        # still warming up.
        try:
            poses = (openvr.TrackedDevicePose_t
                     * openvr.k_unMaxTrackedDeviceCount)()
            self._system.getDeviceToAbsoluteTrackingPose(
                openvr.TrackingUniverseStanding, 0.0, poses)
            hmd = self._extract_pose(poses[openvr.k_unTrackedDeviceIndex_Hmd])
            for hand, role in ((0, openvr.TrackedControllerRole_LeftHand),
                               (1, openvr.TrackedControllerRole_RightHand)):
                info = None
                raw = None
                idx = self._system.getTrackedDeviceIndexForControllerRole(role)
                if idx != openvr.k_unTrackedDeviceIndexInvalid:
                    dev = self._extract_pose(poses[idx])
                    if dev is not None:
                        raw = (dev[0], dev[1])
                        if hmd is not None:
                            info = self._relative_pose(hmd, dev)
                self.pose_info[hand] = info
                self.raw_pose[hand] = raw
        except Exception:
            self.pose_info = [None, None]
            self.raw_pose = [None, None]

        if not self._poll_actions:
            return  # bisect stage 4: passive only

        active = (openvr.VRActiveActionSet_t * 1)()
        active[0].ulActionSet = self._action_set
        self._vrin.updateActionState(active)

        for hand, action in enumerate(self._actions):
            hn = "L" if hand == 0 else "R"
            joints = None
            try:
                data = self._vrin.getSkeletalActionData(action)
                if bool(data.bActive) != self._hand_active[hand]:
                    # Don't log the initial unknown→False transition: hands
                    # simply not being tracked yet at startup is the normal
                    # case, not an event.
                    if data.bActive or self._hand_active[hand] is not None:
                        self._log(f"Skeleton: {hn} hand "
                                  + ("tracking" if data.bActive else "lost"))
                    self._hand_active[hand] = bool(data.bActive)
                if data.bActive:
                    self._ever_active = True
                    self._err_logged[hand] = False  # re-arm error reporting
                    self._reset_count = 0
                    bones = self._get_bones(action, hand)
                    if bones is not None:
                        joints = tuple(
                            (t.position.v[0], t.position.v[1], t.position.v[2])
                            for t in bones)
            except Exception as e:
                if not self._err_logged[hand]:
                    self._err_logged[hand] = True
                    self._log(f"Skeleton: {hn} read error: {e!r}")
            self.hands[hand] = joints
            feed = None
            if joints is not None and self.feed_enabled:
                try:
                    feed = self._make_feed(action, hand)
                except Exception:
                    feed = None
            self.driver_feed[hand] = feed

        # "connected" alone is misleading when the actions never go active —
        # surface the most likely cause right in the panel placeholder. Hands
        # leaving camera view is the everyday case; a hand that has never once
        # tracked long after connect suggests a binding problem instead.
        if any(self._hand_active):
            self.status = "connected"
        elif self._ever_active:
            self.status = "hands not in view"
        elif time.time() - self._connected_at > 30.0:
            self.status = "no data — try a finger pinch"

        # While nothing is tracking, narrate the state so the console answers
        # "why" instead of leaving a frozen status — including when tracking
        # worked earlier and then got stuck after a standby/wake cycle. Fast
        # cadence for the first minute of an inactive stretch, then slow, so an
        # idle bridge doesn't flood the console overnight.
        now = time.time()
        if any(self._hand_active):
            self._inactive_since = None
        else:
            if self._inactive_since is None:
                self._inactive_since = now
            cadence = 5.0 if now - self._inactive_since < 60.0 else 60.0
            if now - self._last_diag >= cadence:
                self._last_diag = now
                roles_held, total_origins = self._diag()
                # Stuck-state self-heal: devices hold hand roles and the HMD is
                # worn, yet after a grace period no origins ever attached.
                worn = getattr(openvr, "k_EDeviceActivityLevel_UserInteraction", 1)
                # Two quick reconnect attempts, then slow periodic retries
                # forever — the wedge clears on the runtime's schedule
                # (settling after boot, or a scene app starting), so give up
                # never, just quietly.
                grace = 20.0 if self._reset_count < 2 else 120.0
                if (roles_held and total_origins == 0
                        and self._hmd_activity == worn
                        and now - self._connected_at > grace):
                    if self._reset_count == 0:
                        self._log("Skeleton: tip — a thumb-index pinch "
                                  "usually completes attachment instantly")
                    elif self._reset_count == 2:
                        self._log(
                            "Skeleton: bindings still not attaching — "
                            "dropping to slow retries (every 2 min). "
                            "A finger pinch or starting any VR app "
                            "usually fixes it instantly")
                    self._reset_count += 1
                    self._reset_requested = True

    def _diag(self):
        roles_held = 0
        total_origins = 0
        for hand, action in enumerate(self._actions):
            hn = "L" if hand == 0 else "R"
            role = (openvr.TrackedControllerRole_LeftHand if hand == 0
                    else openvr.TrackedControllerRole_RightHand)
            parts = []
            try:
                idx = self._system.getTrackedDeviceIndexForControllerRole(role)
                if idx == openvr.k_unTrackedDeviceIndexInvalid:
                    parts.append("no device holds this hand role")
                else:
                    roles_held += 1
                    conn = self._system.isTrackedDeviceConnected(idx)
                    parts.append(f"device #{idx}"
                                 + ("" if conn else " (disconnected)"))
            except Exception as e:
                parts.append(f"role query failed: {e!r}")
            try:
                data = self._vrin.getSkeletalActionData(action)
                parts.append("action ACTIVE" if data.bActive else "action inactive")
            except Exception as e:
                parts.append(f"skeletal data error: {e!r}")
            # pyopenvr's getActionOrigins wrapper is broken (2.12 ends with
            # `originsOut.value` on a ctypes array) — call the C function table
            # directly instead.
            try:
                count = getattr(openvr, "k_unMaxActionOriginCount", 16)
                origins = (openvr.VRInputValueHandle_t * count)()
                # Pass the array itself: ctypes converts it to the pointer the
                # prototype wants. byref(origins[0]) is a TypeError, because
                # indexing a simple-type ctypes array yields a plain int — the
                # exact bug inside pyopenvr's own wrapper.
                err = self._vrin.function_table.getActionOrigins(
                    self._action_set, action, origins, count)
                if err == 0:
                    n = sum(1 for o in origins if o)
                    total_origins += n
                    parts.append(f"{n} binding origin(s)")
                else:
                    parts.append(f"origins error {err}")
            except Exception as e:
                parts.append(f"origins query failed: {type(e).__name__}")
            try:
                parts.append(
                    f"tracking level {int(self._vrin.getSkeletalTrackingLevel(action))}")
            except Exception:
                pass
            self._log(f"Skeleton: {hn} diag — " + ", ".join(parts))
        return roles_held, total_origins

    def _get_bones(self, action, hand):
        """Fetch bone transforms, discovering the count the runtime accepts.

        getBoneCount cannot be trusted: with Steam Link hand tracking it
        reports the standard 31-bone skeleton while GetSkeletalBoneData demands
        the count the driver actually submits (rejecting everything else as
        InvalidBoneCount). So probe — reported count first, then the two known
        skeleton sizes, then the rest — and cache what works.
        """
        n = self._bone_count[hand]
        if n < 0:
            return None  # probing already failed for this hand; stay quiet
        if n > 0:
            try:
                return self._fetch_bones(action, n)
            except Exception as e:
                if type(e).__name__ != "InputError_InvalidBoneCount":
                    raise
                self._bone_count[hand] = 0  # skeleton changed; re-probe

        hn = "L" if hand == 0 else "R"
        try:
            reported = self._vrin.getBoneCount(action)
        except Exception:
            reported = 0
        candidates = []
        for c in [reported, 26, 31] + list(range(1, 65)):
            if c > 0 and c not in candidates:
                candidates.append(c)
        for c in candidates:
            try:
                bones = self._fetch_bones(action, c)
            except Exception as e:
                if type(e).__name__ == "InputError_InvalidBoneCount":
                    continue
                raise
            self._bone_count[hand] = c
            extra = f" (runtime claims {reported})" if reported != c else ""
            self._log(f"Skeleton: {hn} using {c} bones{extra}")
            return bones
        self._bone_count[hand] = -1
        self._log(f"Skeleton: {hn} rejected every bone count 1-64 "
                  f"(runtime claims {reported})")
        return None

    @staticmethod
    def _extract_pose(pose):
        """TrackedDevicePose_t → (position, rotation rows, velocity), or None."""
        if not pose.bPoseIsValid:
            return None
        m = pose.mDeviceToAbsoluteTracking.m
        rot = tuple(tuple(float(m[r][c]) for c in range(3)) for r in range(3))
        pos = tuple(float(m[r][3]) for r in range(3))
        vel = tuple(float(pose.vVelocity.v[i]) for i in range(3))
        return pos, rot, vel

    # Pose maths lives in graphics.relative_pose so both backends hand the
    # panels identically-shaped data.
    _relative_pose = staticmethod(relative_pose)

    def _fetch_bones(self, action, n, space=None):
        # Must pass a caller-allocated ctypes array: pyopenvr's wrapper quietly
        # substitutes a 1-element array for any non-array argument and calls
        # the C API with count=1, which the runtime rejects as InvalidBoneCount
        # no matter what count we intended.
        if space is None:
            space = openvr.VRSkeletalTransformSpace_Model
        arr = (openvr.VRBoneTransform_t * n)()
        self._vrin.getSkeletalBoneData(
            action, space,
            openvr.VRSkeletalMotionRange_WithoutController, arr)
        return arr

    def _make_feed(self, action, hand):
        """Payload for the driver stream (VR fusion mode).

        The driver's UpdateSkeletonComponent expects PARENT-relative bone
        transforms — a separate fetch from the model-space set the panels draw.
        Curls come from the runtime's own summary; confidence maps the skeletal
        tracking level onto the packet's 0-255 scale.
        """
        n = self._bone_count[hand]
        if n <= 0:
            return None
        arr = self._fetch_bones(action, n,
                                openvr.VRSkeletalTransformSpace_Parent)
        bones = tuple(
            (t.position.v[0], t.position.v[1], t.position.v[2],
             t.orientation.w, t.orientation.x, t.orientation.y,
             t.orientation.z)
            for t in arr)
        try:
            summary = self._vrin.getSkeletalSummaryData(
                action, getattr(openvr, "VRSummaryType_FromDevice", 1))
            curls = tuple(float(summary.flFingerCurl[i]) for i in range(5))
        except Exception:
            curls = (0.0,) * 5
        try:
            level = int(self._vrin.getSkeletalTrackingLevel(action))
            confidence = {0: 128, 1: 192, 2: 255}.get(level, 255)
        except Exception:
            confidence = 255
        # Wrist pose for the driver, in the RAW tracking universe — the same
        # space driver poses are submitted in. Head-relative would be wrong
        # here (that form is only for the GUI's dome inset).
        pose = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0))
        pose_valid = False
        raw = self.raw_pose[hand]
        if raw is not None:
            pose = (raw[0], matrix_to_quat(raw[1]))
            pose_valid = True
        return bones, curls, confidence, pose, pose_valid
