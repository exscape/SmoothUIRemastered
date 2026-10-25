#!/usr/bin/env bash
# Builds dist/SmoothUIMerger/ (SmoothUILauncher.exe and SmoothUIMerger.exe, see smoothui-tools.spec) from
# WSL, with env_gui's Windows Python.
#     ./build-tools.sh                   a build as released: release manifests only
#     ./build-tools.sh --dev-manifests   a test build that also knows Smooth UI's dev builds
#     ./build-tools.sh --version X.Y.Z   a test build that says it is that version (not config.toml's)
#     ./build-tools.sh --zip             make dist/SmoothUITools-X.Y.Z.zip (what a release ships, without data/)
#                                        instead of mirroring into dist/SmoothUIMerger/
#
# PyInstaller builds into build/dist/, which it deletes first; robocopy then mirrors that into
# dist/SmoothUIMerger/, keeping its data/ (the tools' settings and logs). So dist/SmoothUIMerger/ itself is
# never deleted: WSL1 can keep showing a deleted folder that Windows made again (while a WSL process, e.g.
# a file watcher, still holds the old one).
set -euo pipefail
cd "$(dirname "$0")"

spec_args=()
make_zip=0
while (( $# )); do
    case "$1" in
        --dev-manifests) spec_args+=("$1") ;;
        --version)
            [[ $# -ge 2 && "$2" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "error: --version needs X.Y.Z" >&2; exit 2; }
            spec_args+=(--version "$2"); shift ;;
        --zip) make_zip=1 ;;
        *) echo "usage: $0 [--dev-manifests] [--version X.Y.Z] [--zip]" >&2; exit 2 ;;
    esac
    shift
done

root=$(wslpath -w "$PWD")
if [[ ! "$root" =~ ^[A-Za-z]:\\ ]]; then
    echo "error: the repo must be on a Windows drive, not $root" >&2
    exit 1
fi
built="$root\\build\\dist\\SmoothUIMerger"
target="$root\\dist\\SmoothUIMerger"

env_gui/Scripts/pyinstaller.exe --clean --noconfirm --distpath "$root\\build\\dist" smoothui-tools.spec \
    -- "${spec_args[@]}"

if (( make_zip )); then
    # The version in the build, whichever way it was set
    version=$(<"build/dist/SmoothUIMerger/_internal/smoothui/merger/tools_version.txt")
    zip="dist/SmoothUITools-${version%$'\r'}.zip"
    env_wsl/bin/python dev-tools/make-tools-zip.py build/dist/SmoothUIMerger "$zip"
    exit 0
fi

# /MIR: copy what changed, delete what the build no longer has, except data\ (/XD, a full path: only that
# folder). Exit codes below 8 mean success.
code=0
robocopy.exe "$built" "$target" /MIR /XD "$target\\data" /R:2 /W:1 /NJH /NJS /NDL /NFL /NP || code=$?
if (( code >= 8 )); then
    echo "error: robocopy failed (exit code $code): is SmoothUILauncher.exe or SmoothUIMerger.exe running?" >&2
    exit 1
fi
echo "Built: $target"
