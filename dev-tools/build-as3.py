#!/usr/bin/env python3

# build-as3.py -- compile a directory of AS3 classes into an .abc file with FFDec, for code the patcher
# adds to SWFs as a DoABC tag of its own (e.g. smoothui/patcher/fixes/gwent_speed_as3/ -> gwent_speed.abc).
#
# FFDec's -importScript only replaces classes that already exist, so this makes a stub SWF that
# defines an empty class for each .as file, compiles the sources over it, and extracts the ABC.
# (Taken from build-frame-stepper.py on the adaptive-fps branch.)
#
# The sources can't import the game's classes (FFDec only knows the stub's and Flash's own): reach
# them with getDefinitionByName and untyped (Object / *) access instead.
#
# Usage: python dev-tools/build-as3.py SOURCE_DIR OUT.abc --ffdec 'E:\path\to\JPEXS\ffdec-cli.exe'
# (or set ffdec = '...' in the [paths] section of config/config.toml)

import argparse
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from smoothui.build.shared import load_config
from smoothui.common import IN_WSL, fail, run_cli, to_native, to_windows
from smoothui.patcher.swf_patcher.abc_file import encode_u30, parse_abc
from smoothui.patcher.swf_patcher.swf_tags import (
    TagCode,
    encode_tag_header,
    parse_tags,
    split_do_abc,
)

# ABC 46.16, as Flash and FFDec write it
ABC_MINOR, ABC_MAJOR = 16, 46
CONSTANT_QNAME = 0x07
CONSTANT_PACKAGE_NAMESPACE = 0x16
TRAIT_CLASS = 4
# getlocal0, pushscope, returnvoid
EMPTY_METHOD = bytes([0xD0, 0x30, 0x47])


def parse_args():
    ap = argparse.ArgumentParser(description="Compile a directory of AS3 classes into an .abc file with FFDec")
    ap.add_argument("source", type=Path, help="the sources' root directory (package directories below it)")
    ap.add_argument("out", type=Path, help="the .abc file to write")
    ap.add_argument("--ffdec", help="path to ffdec-cli.exe (or ffdec.sh); default: ffdec in [paths] of config.toml")
    ap.add_argument("--work-dir", type=Path, help="where to put FFDec's temporary files (default: the system's)")
    return ap.parse_args()


def find_ffdec(arg) -> Path:
    if arg is None:
        arg = load_config(section="paths").get("ffdec")
        if arg is None:
            fail("FFDec not given: use --ffdec, or set ffdec in the [paths] section of config/config.toml")
    path = to_native(arg)
    if not path.is_file():
        fail(f"FFDec not found: {path}")
    return path


def class_names(source_dir: Path) -> list[str]:
    """The qualified names of the classes in source_dir, e.g. smoothui.gwent.GwentSpeed"""
    return sorted(".".join(p.relative_to(source_dir).with_suffix("").parts) for p in source_dir.rglob("*.as"))


def string(text: str) -> bytes:
    data = text.encode()
    return encode_u30(len(data)) + data


def stub_abc(names: list[str]) -> bytes:
    """An ABC that defines an empty class (extending Object) for each qualified name"""
    split = [name.rpartition(".")[::2] for name in names]   # (package, class name)
    strings = list(dict.fromkeys(["", "Object", *(package for package, _ in split), *(name for _, name in split)]))
    string_index = {text: i + 1 for i, text in enumerate(strings)}
    namespaces = list(dict.fromkeys(["", *(package for package, _ in split)]))
    namespace_index = {package: i + 1 for i, package in enumerate(namespaces)}
    # Multinames: Object, then one per class
    multinames = [("", "Object"), *split]

    count = len(names)
    method_count = 3 * count    # per class: instance initializer (i), class initializer (count + i), script (2 * count + i)
    abc = struct.pack("<HH", ABC_MINOR, ABC_MAJOR)
    abc += encode_u30(0) + encode_u30(0) + encode_u30(0)    # int, uint and double pools
    abc += encode_u30(len(strings) + 1) + b"".join(map(string, strings))
    abc += encode_u30(len(namespaces) + 1) + b"".join(
        bytes([CONSTANT_PACKAGE_NAMESPACE]) + encode_u30(string_index[package]) for package in namespaces)
    abc += encode_u30(0)    # namespace sets
    abc += encode_u30(len(multinames) + 1) + b"".join(
        bytes([CONSTANT_QNAME]) + encode_u30(namespace_index[package]) + encode_u30(string_index[name])
        for package, name in multinames)
    # Method signatures: no parameters, any return type, no name, no flags
    abc += encode_u30(method_count) + (encode_u30(0) * 3 + b"\0") * method_count
    abc += encode_u30(0)    # metadata
    abc += encode_u30(count)
    # Instances: name, super name (Object), flags, no interfaces, instance initializer, no traits
    abc += b"".join(encode_u30(i + 2) + encode_u30(1) + b"\0" + encode_u30(0) + encode_u30(i) + encode_u30(0)
                    for i in range(count))
    abc += b"".join(encode_u30(count + i) + encode_u30(0) for i in range(count))
    # Scripts: initializer, and a class trait for the class
    abc += encode_u30(count) + b"".join(
        encode_u30(2 * count + i) + encode_u30(1) + encode_u30(i + 2) + bytes([TRAIT_CLASS]) + encode_u30(0) + encode_u30(i)
        for i in range(count))
    # Method bodies: max stack, locals, initial and max scope depth; code; no exceptions or traits
    abc += encode_u30(method_count) + b"".join(
        encode_u30(m) + encode_u30(1) + encode_u30(1) + encode_u30(0) + encode_u30(1)
        + encode_u30(len(EMPTY_METHOD)) + EMPTY_METHOD + encode_u30(0) + encode_u30(0)
        for m in range(method_count))
    return abc


def tag(code: int, payload: bytes) -> bytes:
    return encode_tag_header(code, len(payload)) + payload


def stub_swf(names: list[str]) -> bytes:
    file_attributes_as3 = 0x08
    # A 1-bit frame size rectangle (all zero), 30 fps, 1 frame
    body = bytes([0x08, 0x00]) + struct.pack("<HH", 30 << 8, 1)
    body += tag(69, struct.pack("<I", file_attributes_as3))  # FileAttributes
    body += tag(TagCode.DO_ABC, struct.pack("<I", 1) + b"\0" + stub_abc(names))
    body += tag(TagCode.SHOW_FRAME, b"") + tag(TagCode.END, b"")
    return b"FWS" + bytes([15]) + struct.pack("<I", 8 + len(body)) + body


def extract_abc(swf: bytes) -> bytes:
    body = zlib.decompress(swf[8:]) if swf[:1] == b"C" else swf[8:]
    # (Not swf_get_header_offsets: FFDec writes an empty frame size rectangle, which it rejects.)
    # The rectangle's 5-bit field width, its 4 fields; then the frame rate and count
    first_tag_offset = (5 + 4 * (body[0] >> 3) + 7) // 8 + 4
    abc_tags = [t for t in parse_tags(body, first_tag_offset) if t.code == TagCode.DO_ABC]
    if len(abc_tags) != 1:
        fail(f"expected one DoABC tag in FFDec's output, found {len(abc_tags)}")
    return split_do_abc(abc_tags[0])[1]


def main():
    args = parse_args()
    if not args.source.is_dir():
        fail(f"not a directory: {args.source}")
    ffdec = find_ffdec(args.ffdec)
    names = class_names(args.source)
    if not names:
        fail(f"no .as files in {args.source}")
    print(f"Compiling {', '.join(names)}")

    if args.work_dir:
        args.work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.work_dir) as tmp:
        tmp = Path(tmp)
        stub, out, scripts = tmp / "stub.swf", tmp / "out.swf", tmp / "scripts"
        stub.write_bytes(stub_swf(names))
        for source in args.source.rglob("*.as"):
            target = scripts / source.relative_to(args.source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())

        native = lambda path: to_windows(path) if IN_WSL and ffdec.suffix.lower() == ".exe" else str(path)
        result = subprocess.run([str(ffdec), "-importScript", native(stub), native(out), native(scripts)],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                                check=False)    # checked below, to fail with FFDec's output
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0 or not out.is_file():
            fail(f"FFDec failed (exit code {result.returncode}):\n{output}")
        abc_bytes = extract_abc(out.read_bytes())

    # FFDec reports compile errors but still writes the file, with the stub class left in place
    abc = parse_abc(abc_bytes)
    defined = sorted(abc.full_name_of(instance.name) for instance in abc.instances)
    stubs = [abc.full_name_of(instance.name) for instance in abc.instances
             if abc.bodies[instance.constructor].code == EMPTY_METHOD]
    if defined != names or stubs:
        fail(f"the compiled ABC doesn't look right (defines {defined}; still stubs: {stubs}); "
             f"FFDec output:\n{output or '(none)'}")
    if output:
        print(output)

    args.out.write_bytes(abc_bytes)
    print(f"Wrote {args.out} ({len(abc_bytes)} bytes)")


if __name__ == "__main__":
    run_cli(main)
