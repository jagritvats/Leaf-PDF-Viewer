"""Windows binaries that code signing (signtool, the Microsoft Store's package signer) rejects.

A PE file whose certificate table points past the end of the file is malformed: some redistributions strip the
Authenticode signature from a DLL but leave the header entry pointing at it (uv's standalone CPython ships
vcruntime140.dll and vcruntime140_1.dll like this). Windows still loads such a file, but signing it fails with
0x800700C1 (ERROR_BAD_EXE_FORMAT), which is how the Store's certification reports it.

    python packaging/pecheck.py DIR    # lists bad files under DIR, exit status 1 if there are any
"""
from __future__ import annotations

import os
import struct
import sys

PE_EXTS = {".exe", ".dll", ".pyd", ".sys", ".ocx", ".cpl", ".scr", ".node"}


def problem(path: str) -> str | None:
    """Why signing would reject the PE file at `path`, or None if it's fine (or not a PE file)."""
    with open(path, "rb") as f:
        b = f.read()
    if b[:2] != b"MZ":
        return "not a PE file (no MZ header)" if os.path.splitext(path)[1].lower() in PE_EXTS else None
    try:
        pe = struct.unpack_from("<I", b, 0x3C)[0]
        if b[pe:pe + 4] != b"PE\0\0":
            return "no PE signature"
        magic = struct.unpack_from("<H", b, pe + 24)[0]
        if magic not in (0x10B, 0x20B):
            return f"unknown optional header magic {magic:#x}"
        opt = pe + 24
        count = struct.unpack_from("<I", b, opt + (92 if magic == 0x10B else 108))[0]
        if count <= 4:
            return None
        off, size = struct.unpack_from("<II", b, opt + (96 if magic == 0x10B else 112) + 8 * 4)
    except struct.error:
        return "truncated PE header"
    if size and off + size > len(b):
        return f"certificate table ({off}+{size}) points past the end of the file ({len(b)} bytes)"
    return None


def bad_files(root: str) -> list[tuple[str, str]]:
    out = []
    for d, _, files in os.walk(root):
        for name in files:
            p = os.path.join(d, name)
            why = problem(p)
            if why:
                out.append((os.path.relpath(p, root), why))
    return sorted(out)


if __name__ == "__main__":
    bad = bad_files(sys.argv[1])
    for rel, why in bad:
        print(f"{rel}: {why}")
    sys.exit(1 if bad else 0)
