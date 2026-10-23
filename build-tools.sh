#!/usr/bin/env bash
# Builds dist/SmoothUIMerger/ (SmoothUILauncher.exe and SmoothUIMerger.exe, see smoothui-merger.spec) from
# WSL, with env_gui's Windows Python.
#     ./build-tools.sh                   a build as released: release manifests only
#     ./build-tools.sh --dev-manifests   a test build that also knows Smooth UI's dev builds
#
# PyInstaller builds into build/dist/, which it deletes first; robocopy then mirrors that into
# dist/SmoothUIMerger/, keeping its data/ (the tools' settings and logs). So dist/SmoothUIMerger/ itself is
# never deleted: WSL1 can keep showing a deleted folder that Windows made again (while a WSL process, e.g.
# a file watcher, still holds the old one).
set -euo pipefail
cd "$(dirname "$0")"

spec_args=()
for arg in "$@"; do
    case "$arg" in
        --dev-manifests) spec_args+=("$arg") ;;
        *) echo "usage: $0 [--dev-manifests]" >&2; exit 2 ;;
    esac
done

root=$(wslpath -w "$PWD")
if [[ ! "$root" =~ ^[A-Za-z]:\\ ]]; then
    echo "error: the repo must be on a Windows drive, not $root" >&2
    exit 1
fi
built="$root\\build\\dist\\SmoothUIMerger"
target="$root\\dist\\SmoothUIMerger"

env_gui/Scripts/pyinstaller.exe --clean --noconfirm --distpath "$root\\build\\dist" smoothui-merger.spec \
    -- "${spec_args[@]}"

# /MIR: copy what changed, delete what the build no longer has, except data\ (/XD, a full path: only that
# folder). Exit codes below 8 mean success.
code=0
robocopy.exe "$built" "$target" /MIR /XD "$target\\data" /R:2 /W:1 /NJH /NJS /NDL /NFL /NP || code=$?
if (( code >= 8 )); then
    echo "error: robocopy failed (exit code $code): is SmoothUILauncher.exe or SmoothUIMerger.exe running?" >&2
    exit 1
fi
echo "Built: $target"
