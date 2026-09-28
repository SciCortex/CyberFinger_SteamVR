# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Build the Resonite mods and stage them for the installer's "Resonite mods" option.

    python tools/stage_resonite_mods.py

Writes bridge/installer/resonite_mods/, laid out as the installer copies it:

    BepInEx/plugins/DrSciCortex-MoreFluxActions/MoreFluxActions/          engine plugin (BepisLoader)
    Renderer/BepInEx/plugins/DrSciCortex-MoreFluxActions/...              renderer plugins (BepInExRenderer)
    Renderer/BepInEx/plugins/DrSciCortex-SteamVRRoleFix/
    rml_mods/CyberFingerMod.dll, rml_mods/ProximityGrab.dll                ResoniteModLoader mods

and VERSIONS.txt, the commit each mod was built from. Each mod is built from a fresh shallow clone of its GitHub
repository (default branch) under out/resonite_mods_src/, made anew on every run: the installer gets what is
pushed, whatever is in local checkouts. Needs git, the .NET SDK and Resonite installed: the mods compile against
Resonite's assemblies.
"""

import os
import shutil
import stat
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "bridge", "installer", "resonite_mods")
CLONES = os.path.join(ROOT, "out", "resonite_mods_src")

# name, clone URL, [(project to build, extra build properties)], [(built file, staged folder)]
MODS = [
    ("MoreFluxActionsMod", "https://github.com/DrSciCortex/MoreFluxActionsMod",
     [("MoreFluxActions/MoreFluxActions.csproj", []),
      ("MoreFluxActions.Renderer/MoreFluxActions.Renderer.csproj", [])],
     [("MoreFluxActions/bin/Release/MoreFluxActions.dll",
       "BepInEx/plugins/DrSciCortex-MoreFluxActions/MoreFluxActions"),
      ("MoreFluxActions.Renderer/bin/Release/MoreFluxActions.Renderer.dll",
       "Renderer/BepInEx/plugins/DrSciCortex-MoreFluxActions/MoreFluxActions.Renderer")]),
    ("SteamVRRoleFix", "https://github.com/DrSciCortex/SteamVRRoleFix",
     [("SteamVRRoleFix.csproj", [])],
     [("bin/Release/SteamVRRoleFix.dll", "Renderer/BepInEx/plugins/DrSciCortex-SteamVRRoleFix")]),
    ("CyberFingerMod", "https://github.com/DrSciCortex/CyberFingerMod",
     [("CyberFingerMod/CyberFingerMod.csproj", ["-p:CopyToMods=false"])],
     [("CyberFingerMod/bin/Release/net10.0-windows/CyberFingerMod.dll", "rml_mods")]),
    ("ProximityGrab", "https://github.com/SciCortex/ProximityGrab",
     [("ProximityGrab/ProximityGrab.csproj", ["-p:CopyToMods=false"])],
     [("ProximityGrab/bin/Release/net10.0/ProximityGrab.dll", "rml_mods")]),
]


def run(args, cwd=None):
    print(">", " ".join(args), flush=True)
    subprocess.run(args, cwd=cwd, check=True)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout.strip()


def remove_tree(path):
    """shutil.rmtree, also for git's read-only object files on Windows."""
    def writable_again(func, target, _):
        os.chmod(target, stat.S_IWRITE)
        func(target)
    if os.path.isdir(path):
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=writable_again)
        else:
            shutil.rmtree(path, onerror=writable_again)


def fresh_clone(name, url):
    """A new shallow clone of the mod's repository: nothing left over from an earlier build."""
    clone = os.path.join(CLONES, name)
    remove_tree(clone)
    os.makedirs(CLONES, exist_ok=True)
    run(["git", "clone", "--depth", "1", "--quiet", url, clone])
    return clone


def main():
    remove_tree(OUT)
    versions = []
    for name, url, projects, outputs in MODS:
        repo = fresh_clone(name, url)
        for project, props in projects:
            run(["dotnet", "build", os.path.join(repo, project), "-c", "Release", "-nologo", "-v", "q", *props])
        for built, folder in outputs:
            dest = os.path.join(OUT, folder)
            os.makedirs(dest, exist_ok=True)
            shutil.copy2(os.path.join(repo, built), dest)
        commit = git(repo, "log", "-1", "--format=%h %cs %s")
        versions.append(f"{name}: {commit} ({url})")
    with open(os.path.join(OUT, "VERSIONS.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(versions) + "\n")
    print("\nStaged in " + OUT + ":\n  " + "\n  ".join(versions))


if __name__ == "__main__":
    try:
        main()
    except (subprocess.CalledProcessError, OSError) as e:
        remove_tree(OUT)                    # no half-staged mods in the installer
        sys.exit(f"Staging the Resonite mods failed: {e}\nThe mods need git, the .NET SDK and Resonite installed. "
                 "For tools\\build_installer.cmd, CF_RESONITE_MODS=keep packages a resonite_mods folder staged on "
                 "another PC, and CF_RESONITE_MODS=skip builds the installer without the mods.")
