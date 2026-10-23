# SPDX-License-Identifier: GPL-3.0-only
"""Fusion Studio self-test: `CyberFingerFusionStudio.exe --selftest [report.txt]` (or `python fusion_studio.py
--selftest`). Starts the Studio, checks that every component and model loaded, streams the built-in EMG simulator
for a few seconds, starts the headset (OpenXR) helper process, then quits. The packaged .exe has no console, so the
result goes to a report file (default %APPDATA%\\CyberFingerBridge\\fusion_studio_selftest.txt); exit code 0 = healthy.
No CyberFinger, armband or headset is needed: without a running OpenXR runtime the helper only has to answer."""
import os
import sys
import time
import traceback


def _default_report():
    d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "CyberFingerBridge")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "fusion_studio_selftest.txt")


def run(args=()):
    path = args[0] if args else _default_report()
    lines = []; failed = []

    def check(name, ok, detail=""):
        lines.append(f"{'OK  ' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            failed.append(name)

    app = None
    try:
        import fusion_studio as FS
        lines.append(f"Fusion Studio self-test · {time.strftime('%Y-%m-%d %H:%M:%S')} · "
                     + ("packaged .exe" if getattr(sys, "frozen", False) else f"Python {sys.version.split()[0]}"))
        for flag, what in (("HAS_ARMBAND", "EMG armband support"), ("HAS_OPENXR", "OpenXR (pyopenxr + loader)"),
                           ("HAS_GATE", "occlusion gate"), ("HAS_FUSION", "camera ↔ IMU fusion"),
                           ("HAS_GESTURE", "EMG features"), ("HAS_TRAY", "tray icon")):
            check(what, bool(getattr(FS, flag, False)))
        for mod in ("key_postures", "transition_events", "finger_fusion", "steamvr_link", "mount_calib", "_occl"):
            check(f"module {mod.lstrip('_')}", getattr(FS, mod, None) is not None)
        app = FS.FusionStudioApp()
        errs = []
        app.root.report_callback_exception = lambda *a: errs.append("".join(traceback.format_exception(*a)))

        def pump(sec):
            t_end = time.time() + sec
            while time.time() < t_end:
                app.root.update(); time.sleep(0.01)

        M = app._kp_model
        check("key-posture recogniser", M is not None, ", ".join(M["classes"]) if M else "key_postures_model2.npz")
        check("wrist-position model", app._hyb_nets is not None)
        check("EMG finger-posture model", app._ppose is not None)
        check("anatomical hand prior", app._hand_prior is not None)
        pump(1.0)
        app.emg_src_var.set("sim"); app._toggle_armband(); pump(3.0)
        arm = app.armband
        check("EMG simulator streams", arm is not None and arm.running and getattr(arm.dash, "total_samples", 0) > 0,
              f"{getattr(arm.dash, 'total_samples', 0)} samples" if arm is not None and arm.dash is not None else "")
        tr = app._tr
        check("transition tracker", tr is not None,
              (("learned detector" if tr.use_det else "score threshold") + f", {len(tr.dirm.classes)} postures") if tr else "")
        # the headset helper process: it must answer (a status packet, or its own log line when no runtime is up)
        log = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "CyberFingerBridge", "openxr_provider.log")
        t0 = time.time(); app._toggle_preview(); status = None; tail = []
        started = app._preview_running and app._preview_skeleton is not None
        while started and time.time() - t0 < 20.0:         # SteamVR can take a while to accept or refuse a session
            pump(0.25)
            src = app._preview_skeleton
            status = getattr(src, "status", None) if src is not None else None
            try:                                            # only this run's helper log (the file outlives runs)
                tail = ([ln.strip() for ln in open(log, encoding="utf-8", errors="replace") if ln.strip()][-1:]
                        if os.path.getmtime(log) >= t0 - 1.0 else [])
            except Exception:
                tail = []
            if status not in (None, "starting...") or tail:
                break
        answered = started and ((status not in (None, "starting...")) or bool(tail))
        check("OpenXR helper process answers", answered, f"after {time.time() - t0:.1f} s · status: {status!r}"
              + (f" · log: {tail[0][:120]}" if tail else ""))
        if app._preview_running:
            app._toggle_preview()
        pump(0.5)
        check("no errors in the window's callbacks", not errs, errs[0].strip().splitlines()[-1][:160] if errs else "")
    except Exception:
        check("self-test ran to the end", False, traceback.format_exc().strip().splitlines()[-1][:200])
        lines.append(traceback.format_exc())
    finally:
        if app is not None:
            try:
                app._quit_app()
            except Exception:
                lines.append("quit: " + traceback.format_exc().strip().splitlines()[-1])
    lines.append("RESULT: " + ("healthy" if not failed else "FAILED — " + ", ".join(failed)))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0 if not failed else 1
