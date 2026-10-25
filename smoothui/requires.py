"""What a Smooth UI release requires of the merger, so that it can patch other mods' files as the release
patched the game's: its manifest's "requires" lists them, e.g. "fix:hub-tab-hover-fix" or "setting:speed".
The build fills it in (see build/release.py's release_requires).

A merger refuses a release that requires something it doesn't know (from a release built with newer
code: the merger must be updated), or something RETIRED (a fix since removed: Smooth UI must be updated).
Light: the merger's launch check imports it, so it must not import the patcher.
"""

# The fixes in patcher/fixes/ (each one's NAME); patcher/fixes/__init__.py checks that this lists exactly
# them. To remove a fix, move its requirement to RETIRED.
FIX_NAMES = frozenset({
    "gwent-3d-bug-fix",
    "gwent-card-fx-filter-fix",
    "gwent-coin-shadow-fix",
    "gwent-fog-intro-fix",
    "gwent-frost-flicker-fix",
    "gwent-rain-loop-fix",
    "gwent-speed-options",
    "hub-tab-hover-fix",
})
# The profile's settings (see config/profile_default.txt), less "fix:" (each fix is its own requirement)
SETTING_NAMES = frozenset({"speed", "include", "exclude"})
# Requirements this merger no longer has, e.g. "fix:<name>" of a removed fix
RETIRED = frozenset()


def fix_requirement(name: str) -> str:
    return f"fix:{name}"

def setting_requirement(name: str) -> str:
    return f"setting:{name}"

KNOWN = frozenset({*map(fix_requirement, FIX_NAMES), *map(setting_requirement, SETTING_NAMES)})


def manifest_requires(manifest: dict) -> set[str]:
    """What a release requires (its manifest's "requires"). Manifests from before "requires" only need
    the fixes their reports name."""
    if "requires" in manifest:
        return set(manifest["requires"])
    return {fix_requirement(fix["name"]) for record in manifest["files"].values()
            for fix in record.get("report", {}).get("fixes", ())}
