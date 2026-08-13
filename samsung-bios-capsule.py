#!/usr/bin/env python3
"""
Samsung Galaxy Book BIOS updater (.exe) to UEFI capsule, for Linux.

Samsung's Windows updater carries a raw PFAT (AMI BIOS Guard) image. Pull that
out, wrap it in a 28-byte EFI_CAPSULE_HEADER, and fwupd can install it.

This does not flash anything. It builds the capsule, verifies it, and prints the
install command.

Usage:
    ./samsung-bios-capsule.py ITEM_xxxxx_WIN_P11AMA.exe [-o OUTDIR]

Header values are read off the machine:
  - ESRT (/sys/firmware/efi/esrt) -> CapsuleGuid, Flags, current/lowest version
  - WFU_*.inf inside the exe       -> FirmwareId, target version

See docs/reverse-engineering.md for the evidence behind the format.
"""

import argparse
import gzip
import hashlib
import io
import pathlib
import re
import struct
import subprocess
import sys
import uuid
import zlib

ESRT = pathlib.Path("/sys/firmware/efi/esrt/entries")
CAPSULE_HEADER_SIZE = 0x1C
FALLBACK_FLAGS = 0x00050000  # PERSIST_ACROSS_RESET | INITIATE_RESET


def die(msg):
    sys.exit(f"error: {msg}")


def warn(msg):
    print(f"  warning: {msg}")


# ------------------------------------------------------------ taking the exe apart

def _gunzip_at(blob, off):
    """Inflate the gzip stream at off, tolerating trailing garbage."""
    try:
        return gzip.GzipFile(fileobj=io.BytesIO(blob[off:])).read()
    except Exception:
        return zlib.decompressobj(31).decompress(blob[off:])


def extract_embedded(exe_bytes, suffix):
    """Inflate the first gzip blob after each '<name><suffix>.gz' UTF-16 filename.

    Samsung's UnPacker puts the gzip data 0x20c bytes past the filename record.
    Rather than trust that offset, look for the next gzip magic after the name.
    """
    needle = (suffix + ".gz").encode("utf-16-le")
    results = []
    start = 0
    while True:
        i = exe_bytes.find(needle, start)
        if i < 0:
            break
        start = i + 1
        gz = exe_bytes.find(b"\x1f\x8b\x08", i)
        if gz < 0:
            continue
        try:
            results.append((i, gz, _gunzip_at(exe_bytes, gz)))
        except Exception:
            pass
    return results


def find_pfat(exe_bytes):
    """Return (name, data) for the PFAT payload, i.e. <model>.CAP."""
    for name_off, gz_off, data in extract_embedded(exe_bytes, ".CAP"):
        if data[8:16] == b"_AMIPFAT":
            # The filename sits before the needle. UTF-16 is two bytes per
            # character, so the window start has to match the needle's parity
            # or the decode comes out shifted.
            needle_len = len((".CAP" + ".gz").encode("utf-16-le"))
            window = exe_bytes[max(0, name_off - 64):name_off + needle_len]
            nm = "BIOS.CAP"
            try:
                text = window.decode("utf-16-le", "ignore")
                found = re.findall(r"([0-9A-Za-z_\-]+\.CAP)\.gz$", text)
                if found:
                    nm = found[-1]
            except Exception:
                pass
            return nm, data
    die("no PFAT payload (*.CAP) found inside the exe")


def find_inf(exe_bytes):
    """Parse the embedded WFU INF into {guid, version, driver_ver}."""
    for _, _, data in extract_embedded(exe_bytes, ".inf"):
        for enc in ("utf-16-le", "latin1"):
            try:
                text = data.decode(enc)
            except Exception:
                continue
            if "FirmwareId" not in text:
                continue
            fid = re.search(r"FirmwareId,,\{([0-9A-Fa-f\-]+)\}", text)
            fver = re.search(r"FirmwareVersion,%REG_DWORD%,(\d+)", text)
            dver = re.search(r"DriverVer\s*=\s*(.+)", text)
            if fid and fver:
                return {
                    "guid": uuid.UUID(fid.group(1)),
                    "version": int(fver.group(1)),
                    "driver_ver": dver.group(1).strip() if dver else "?",
                }
    return None


# ------------------------------------------------------------------------- ESRT

def read_esrt():
    """Read the fw_type==1 (system firmware) ESRT entry. None unless root."""
    if not ESRT.is_dir():
        return None
    for entry in sorted(ESRT.iterdir()):
        vals = {}
        try:
            for key in ("fw_class", "fw_type", "fw_version",
                        "lowest_supported_fw_version", "capsule_flags",
                        "last_attempt_version", "last_attempt_status"):
                vals[key] = (entry / key).read_text().strip()
        except (PermissionError, OSError):
            return None
        if vals.get("fw_type") != "1":
            continue
        return {
            "guid": uuid.UUID(vals["fw_class"]),
            "version": int(vals["fw_version"]),
            "lowest": int(vals["lowest_supported_fw_version"]),
            "flags": int(vals["capsule_flags"], 16),
            "last_version": int(vals["last_attempt_version"]),
            "last_status": int(vals["last_attempt_status"]),
        }
    return None


def find_fwupd_device(guid):
    """Best-effort lookup of the fwupd device ID carrying the ESRT GUID."""
    try:
        out = subprocess.run(["fwupdmgr", "get-devices", "--no-unreported-check"],
                             capture_output=True, text=True, timeout=90).stdout
    except Exception:
        return None
    dev = None
    for line in out.splitlines():
        m = re.search(r"Device ID:\s*([0-9a-f]{40})", line)
        if m:
            dev = m.group(1)
        elif str(guid) in line.lower() and dev:
            return dev
    return None


# -------------------------------------------------------------- building the capsule

def build_capsule(pfat, capsule_guid, flags):
    return (capsule_guid.bytes_le
            + struct.pack("<III", CAPSULE_HEADER_SIZE, flags,
                          CAPSULE_HEADER_SIZE + len(pfat))
            + pfat)


def verify(cap, expect_guid, expect_flags):
    """Walk the capsule back in the order the firmware reads it."""
    ok = True
    guid = uuid.UUID(bytes_le=cap[:16])
    hs, fl, cis = struct.unpack("<III", cap[16:28])
    body = cap[hs:]

    def check(label, value, good):
        nonlocal ok
        ok = ok and good
        print(f"  {'OK  ' if good else 'FAIL'}  {label}: {value}")

    print("\nheader, as CapsulePei reads it:")
    check("CapsuleGuid", str(guid).upper(), guid == expect_guid)
    check("HeaderSize", f"0x{hs:X}", hs == CAPSULE_HEADER_SIZE)
    check("Flags", f"0x{fl:08X}", fl == expect_flags)
    check("CapsuleImageSize", f"{cis:,}", cis == len(cap))

    print("body at capsule+HeaderSize, as BiosGuardPei reads it:")
    check("body[8:16] signature", body[8:16].decode("latin1", "replace"),
          body[8:16] == b"_AMIPFAT")
    script_len, = struct.unpack("<I", body[0:4])
    check("body[0:4] script length", f"0x{script_len:X} (limit 0x2020000)",
          0 < script_len <= 0x2020000)
    crlf = body.find(b"\r\n", 4)
    tag = body[0x11:crlf].decode("latin1", "replace") if crlf > 0 else "(none)"
    check("text before first CRLF", tag, tag.startswith("AMI_BIOS_GUARD"))
    return ok


# ------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="Samsung BIOS updater exe to a UEFI capsule for fwupd")
    ap.add_argument("exe", type=pathlib.Path, help="ITEM_*_WIN_*.exe")
    ap.add_argument("-o", "--outdir", type=pathlib.Path, default=None,
                    help="output directory (default: alongside the exe)")
    ap.add_argument("--guid", help="set CapsuleGuid, for when ESRT is unreadable")
    ap.add_argument("--flags", help="set Flags, e.g. 0x50000")
    args = ap.parse_args()

    if not args.exe.is_file():
        die(f"no such file: {args.exe}")
    outdir = args.outdir or args.exe.parent
    outdir.mkdir(parents=True, exist_ok=True)

    exe = args.exe.read_bytes()
    print(f"input: {args.exe.name}  ({len(exe):,} bytes)")

    # --- payload and metadata
    pfat_name, pfat = find_pfat(exe)
    print(f"  PFAT payload: {pfat_name}  {len(pfat):,} bytes")

    inf = find_inf(exe)
    if inf:
        print(f"  embedded INF: FirmwareId={str(inf['guid']).upper()}  "
              f"target={inf['version']}  DriverVer={inf['driver_ver']}")
    else:
        warn("no embedded WFU INF found, skipping the version checks")

    esrt = read_esrt()
    if esrt:
        print(f"  ESRT: fw_class={str(esrt['guid']).upper()}")
        print(f"        current={esrt['version']}  lowest={esrt['lowest']}  "
              f"capsule_flags=0x{esrt['flags']:X}")
        print(f"        last attempt: version={esrt['last_version']} "
              f"status={esrt['last_status']}")
    else:
        warn("could not read ESRT, run as root to detect these automatically")

    # --- pick header values: ESRT, then INF, then the default
    if args.guid:
        guid, guid_src = uuid.UUID(args.guid), "command line"
    elif esrt:
        guid, guid_src = esrt["guid"], "ESRT fw_class"
    elif inf:
        guid, guid_src = inf["guid"], "INF FirmwareId"
    else:
        die("cannot determine CapsuleGuid, run as root or pass --guid")

    if args.flags:
        flags, flags_src = int(args.flags, 16), "command line"
    elif esrt:
        flags, flags_src = esrt["flags"], "ESRT capsule_flags"
    else:
        flags, flags_src = FALLBACK_FLAGS, "built-in default"

    print(f"\nheader values from: CapsuleGuid <- {guid_src},  Flags <- {flags_src}")

    # --- consistency checks
    if esrt and inf:
        print("\npre-flight:")
        same = esrt["guid"] == inf["guid"]
        print(f"  {'OK  ' if same else 'FAIL'}  ESRT fw_class == INF FirmwareId")
        if not same:
            die("GUID mismatch, this exe is for a different machine")
        if inf["version"] <= esrt["version"]:
            warn(f"target {inf['version']} <= current {esrt['version']}, "
                 f"a downgrade or reinstall may be refused")
        else:
            print(f"  OK    upgrade: {esrt['version']} -> {inf['version']}")
        if inf["version"] < esrt["lowest"]:
            die(f"target {inf['version']} < lowest supported {esrt['lowest']}, "
                f"the firmware will refuse it")

    # --- build and verify
    cap = build_capsule(pfat, guid, flags)
    stem = pfat_name.rsplit(".", 1)[0]
    pfat_path = outdir / pfat_name
    cap_path = outdir / f"{stem}_esrt.cap"
    pfat_path.write_bytes(pfat)
    cap_path.write_bytes(cap)

    ok = verify(cap, guid, flags)
    print(f"\noutput: {cap_path}  ({len(cap):,} bytes)")
    print(f"  sha256 = {hashlib.sha256(cap).hexdigest()}")

    if not ok:
        die("verification failed, do not install this")

    # --- how to install it
    dev = find_fwupd_device(guid)
    print("\n" + "=" * 72)
    print("install (AC adapter required):")
    print(f"  sudo fwupdtool install-blob {cap_path} \\")
    print(f"    {dev if dev else '<device ID: System Firmware in fwupdmgr get-devices>'}")
    print("\ncheck the result after rebooting:")
    print("  sudo grep -r . /sys/firmware/efi/esrt/entries/entry0/")
    print("    last_attempt_status  0=success  3=version refused  "
          "4=bad image format  5=auth failed  6/7=power")
    print("=" * 72)


if __name__ == "__main__":
    main()
