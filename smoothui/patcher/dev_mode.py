import struct
import sys
import zlib
from pathlib import Path

from ..common import profile_key
from ..swf import SWF_COMPRESSED_SIGS, SWF_HEADER_SIZE

# Dev hooks for manually exploring and/or editing a target SWF payload
# DEV_MODE:
#   None  : off; read the payload, patch it, write it back
#   dump  : write the target decompressed SWF to DEV_OUTPUT_FILE and exit
#   insert: read the SWF back from DEV_OUTPUT_FILE and insert it into the .redswf
#   pause : dump, wait for the user to type "continue", then read it back and keep going
DEV_MODE = None
DEV_TARGET_RAW = None   # raw name given to set_dev (e.g. "panel_inventory")
DEV_TARGET_FILE = None  # resolved full relative path (e.g. "gameplay/gui_new/swf/inventory/panel_inventory.redswf")
DEV_OUTPUT_FILE = None  # output filename, no relative path (e.g. "panel_inventory_payload.swf")

def set_dev(mode, target_file_name):
    """Called by the build scripts to enable DEV_MODE"""
    global DEV_MODE, DEV_TARGET_RAW
    DEV_MODE = mode
    DEV_TARGET_RAW = target_file_name

def _dev_matches(rel_key, target):
    """Check whether a normalized profile key matches a normalized dev target name.

    rel_key: normalized profile path, e.g. "gameplay/gui_new/swf/inventory/panel_inventory.redswf"
    target: user input, e.g. "panel_inventory", "inventory/panel_inventory.redswf"
    """
    # Exact path-suffix match (target already includes extension or is a full sub-path)
    if rel_key.endswith("/" + target):
        return True

    # Target is a base name without extension → match .redswf files
    return rel_key.endswith("/" + target + ".redswf")

def resolve_dev_target(profile):
    """Resolve DEV_TARGET_RAW against profile keys. Sets DEV_TARGET_FILE and DEV_OUTPUT_FILE.

    Fails if the the user input matches anything except exactly one target .redswf.
    """
    global DEV_TARGET_FILE, DEV_OUTPUT_FILE

    if DEV_MODE is None or DEV_TARGET_RAW is None:
        return

    target_norm = DEV_TARGET_RAW.replace("\\", "/").lower().strip("/")
    matches = [rel for rel in profile if _dev_matches(profile_key(rel), target_norm)]
    if len(matches) == 0:
        raise ValueError(f"Dev mode: '{DEV_TARGET_RAW}' matched no files in profile")
    if len(matches) > 1:
            raise ValueError(f"Dev mode: '{DEV_TARGET_RAW}' is ambiguous: it matches "
                             f"{len(matches)} files in profile; need exactly one.")
    DEV_TARGET_FILE = matches[0]
    DEV_OUTPUT_FILE = Path(DEV_TARGET_FILE).name.replace(".redswf", "_payload.swf")

def build_dev_file_bytes(header, body):
    """Build a valid, *uncompressed* SWF/GFX file from the container's header and the
    decompressed body, so that it can be edited (e.g. with FFDec, or a hex editor).

    The container header will be CWS/CFX (compressed), but the body here is already
    decompressed, so the signature must be switched to FWS/GFX. The declared length is
    the total uncompressed size, which is the same for both forms."""
    header = bytes(header)
    UNCOMPRESSED_SIG_MAP = {b"CWS": b"FWS", b"CFX": b"GFX"}
    sig = UNCOMPRESSED_SIG_MAP[header[:3]]
    version = header[3:4]
    return sig + version + struct.pack("<I", SWF_HEADER_SIZE + len(body)) + bytes(body)

def dev_hook(header, body):
    p = Path(DEV_OUTPUT_FILE)

    if DEV_MODE == "dump":
        p.write_bytes(build_dev_file_bytes(header, body))
        print(f"\nDev mode: wrote {p} ({SWF_HEADER_SIZE} header bytes + {len(body)} body bytes).\n"
              f"Edit it (e.g. in FFDec), then rerun with --dev-insert {p.name} to keep patching.")
        sys.exit(0)
    if DEV_MODE == "insert":
        saved = p.read_bytes()
        new_body = dev_read_body(saved)
        print(f"\nDev mode: using {p} ({len(saved)} bytes); body = {len(new_body)} bytes.")
        return new_body
    if DEV_MODE == "pause":
        p.write_bytes(build_dev_file_bytes(header, body))
        print(f"\nDev mode: wrote {p} ({SWF_HEADER_SIZE} header bytes + {len(body)} body bytes).\n"
              "Edit it, then type 'continue' on a line of its own to resume "
              "(anything else is ignored).")

        while True:
            try:
                line = sys.stdin.readline()
            except KeyboardInterrupt:
                print(f"\n\nAborting; file {p.name} remains on disk.")
                sys.exit(0)
            if not line:
                continue # Can this happen?
            if line.strip().lower() == "continue":
                break
            else:
                print("Not recognized. Type \"continue\", or abort with ^C.")
        return dev_read_body(p.read_bytes())
    return body

def dev_make_body_hook(rel):
    """Return a dev_hook wrapper for this file, or None if it isn't the dev target."""
    if DEV_MODE is None or DEV_TARGET_FILE is None:
        return None
    if profile_key(rel) != profile_key(DEV_TARGET_FILE):
        return None
    return dev_hook

# Kind of belongs in patcher/dev_mode.py, but depends entirely on SWF stuff
def dev_read_body(saved):
    """Extract the SWF body from a file that was dumped and then edited/saved by a tool.

    Accepts uncompressed (FWS/GFX) as well as zlib-compressed (CWS/CFX) files, since
    external tools may save in either form. The header is ignored beyond its signature."""
    if len(saved) < SWF_HEADER_SIZE:
        raise ValueError("Dev mode: edited file is too short to contain an SWF header")
    sig = saved[:3]
    if sig in (b"FWS", b"GFX"):
        return bytearray(saved[SWF_HEADER_SIZE:])
    if sig in SWF_COMPRESSED_SIGS:
        d = zlib.decompressobj()
        try:
            body = d.decompress(saved[SWF_HEADER_SIZE:])
        except zlib.error as e:
            raise ValueError(f"Dev mode: edited file has a compressed signature but bad zlib data: {e}")
        if not d.eof:
            raise ValueError("Dev mode: edited file's zlib stream is truncated")
        return bytearray(body)
    raise ValueError(f"Dev mode: unsupported signature {sig!r} in edited file "
                     "(LZMA 'ZWS' is not supported; save as uncompressed or zlib)")