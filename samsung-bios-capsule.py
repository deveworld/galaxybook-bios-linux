#!/usr/bin/env python3
"""
Samsung Galaxy Book BIOS updater (.exe) to UEFI capsule, for Linux.

Samsung's Windows updater carries a raw PFAT (AMI BIOS Guard) image. Pull that
out, wrap it in a 28-byte EFI_CAPSULE_HEADER, and fwupd can install it.

    sudo ./samsung-bios-capsule.py ITEM_xxxxx_WIN_P11AMA.exe --install
    sudo reboot
    sudo ./samsung-bios-capsule.py --check

Without --install it stops after building and verifying the capsule and prints
the fwupdtool command for you to run yourself. --check reads the result the
firmware left in ESRT after the reboot.

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
POWER = pathlib.Path("/sys/class/power_supply")
DMI_VERSION = pathlib.Path("/sys/class/dmi/id/bios_version")
CAPSULE_HEADER_SIZE = 0x1C
FALLBACK_FLAGS = 0x00050000  # PERSIST_ACROSS_RESET | INITIATE_RESET

# ESRT last_attempt_status, UEFI spec table "ESRT and FMP Fields"
ATTEMPT_STATUS = {
    0: "success",
    1: "unsuccessful",
    2: "insufficient resources",
    3: "incorrect version",
    4: "invalid image format",
    5: "authentication error",
    6: "AC power not connected",
    7: "insufficient battery",
}


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


def signer_names(exe_bytes):
    """Subject strings from the PE Authenticode block, or None if there is none.

    This pulls certificate subject strings out of the PKCS#7 blob. It does not
    verify the signature cryptographically, so a Samsung match means "claims to
    be Samsung", not "proven to be Samsung".
    """
    try:
        e_lfanew, = struct.unpack_from("<I", exe_bytes, 0x3C)
        if exe_bytes[e_lfanew:e_lfanew + 4] != b"PE\0\0":
            return None
        opt = e_lfanew + 24
        magic, = struct.unpack_from("<H", exe_bytes, opt)
        dirs = opt + (112 if magic == 0x20B else 96)
        addr, size = struct.unpack_from("<II", exe_bytes, dirs + 4 * 8)
        if not addr or not size:
            return None
        blob = exe_bytes[addr:addr + size]
    except Exception:
        return None
    names = set()
    for m in re.finditer(rb"[\x20-\x7e]{6,}", blob):
        s = re.sub(r"^[^A-Za-z]*", "", m.group().decode("latin1"))
        # A printable run usually spills one byte into the next DER tag, which
        # is 0x30 or 0x31, i.e. "0" or "1" landing right after "Ltd." or "Inc,".
        if len(s) > 2 and s[-1] in "01" and s[-2] in ".,":
            s = s[:-1]
        if re.search(r"Samsung|DigiCert|Sectigo|GlobalSign|Entrust|Certum", s):
            names.add(s)
    return sorted(names)


# ------------------------------------------------------------------------- system

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
            "entry": entry.name,
            "guid": uuid.UUID(vals["fw_class"]),
            "version": int(vals["fw_version"]),
            "lowest": int(vals["lowest_supported_fw_version"]),
            "flags": int(vals["capsule_flags"], 16),
            "last_version": int(vals["last_attempt_version"]),
            "last_status": int(vals["last_attempt_status"]),
        }
    return None


def read_power():
    """Return (ac_online, battery_percent). Either may be None if unknown."""
    ac, batt = None, None
    if not POWER.is_dir():
        return ac, batt
    for supply in sorted(POWER.iterdir()):
        try:
            kind = (supply / "type").read_text().strip()
            if kind == "Mains":
                online = (supply / "online").read_text().strip() == "1"
                ac = True if online else (ac or False)
            elif kind == "Battery" and (supply / "capacity").exists():
                batt = int((supply / "capacity").read_text().strip())
        except (OSError, ValueError):
            continue
    return ac, batt


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


def dmi_version():
    try:
        return DMI_VERSION.read_text().strip()
    except OSError:
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


# --------------------------------------------------------------------- --check mode

def report_result():
    """Read what the firmware recorded in ESRT after a reboot."""
    esrt = read_esrt()
    if esrt is None:
        die("cannot read ESRT, run this as root")
    status = esrt["last_status"]
    print(f"ESRT {esrt['entry']}  fw_class={str(esrt['guid']).upper()}")
    print(f"  current version:      {esrt['version']}")
    print(f"  lowest supported:     {esrt['lowest']}")
    print(f"  last attempt version: {esrt['last_version']}")
    print(f"  last attempt status:  {status} "
          f"({ATTEMPT_STATUS.get(status, 'unrecognised')})")
    dmi = dmi_version()
    if dmi:
        print(f"  DMI bios_version:     {dmi}")
    print()

    if status == 0 and esrt["version"] == esrt["last_version"]:
        print(f"Flashed. The firmware is now at {esrt['version']}.")
    elif status == 0:
        print("No failure recorded, but the running version does not match the last")
        print("attempt, so nothing was flashed on the last boot.")
    elif status in (6, 7):
        print("Refused on power. Connect the charger, let it charge, then retry.")
    elif status == 5:
        print("BIOS Guard rejected the payload signature. Stop here, and do not retry")
        print("with the same file.")
    elif status == 4:
        print("The firmware rejected the capsule format, so the wrapper is wrong for")
        print("this machine. See docs/reverse-engineering.md section 5.")
    elif status == 3:
        print(f"Version refused. The target has to be at least {esrt['lowest']}.")
    else:
        print("The flash failed without a specific reason. Look for a BIOS recovery")
        print("option in the firmware setup menu before retrying.")
    return 0 if status == 0 and esrt["version"] == esrt["last_version"] else 1


# ------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="Samsung BIOS updater exe to a UEFI capsule for fwupd")
    ap.add_argument("exe", type=pathlib.Path, nargs="?",
                    help="ITEM_*_WIN_*.exe from Samsung support")
    ap.add_argument("-o", "--outdir", type=pathlib.Path, default=None,
                    help="output directory (default: alongside the exe)")
    ap.add_argument("--install", action="store_true",
                    help="run fwupdtool install-blob once every check passes")
    ap.add_argument("--check", action="store_true",
                    help="read the post-reboot result out of ESRT and exit")
    ap.add_argument("--guid", help="set CapsuleGuid, for when ESRT is unreadable")
    ap.add_argument("--flags", help="set Flags, e.g. 0x50000")
    args = ap.parse_args()

    if args.check:
        return report_result()
    if args.exe is None:
        ap.error("give an exe to convert, or --check to read the last result")
    if not args.exe.is_file():
        die(f"no such file: {args.exe}")
    outdir = args.outdir or args.exe.parent
    outdir.mkdir(parents=True, exist_ok=True)

    exe = args.exe.read_bytes()
    print(f"input: {args.exe.name}  ({len(exe):,} bytes)")

    # --- who signed the exe
    names = signer_names(exe)
    if names is None:
        warn("no Authenticode signature block in this exe")
    elif any("Samsung" in n for n in names):
        print(f"  signed by: {next(n for n in names if 'Samsung' in n)}")
    else:
        warn(f"signature block present, no Samsung subject: {', '.join(names)}")

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
        print(f"  ESRT {esrt['entry']}: fw_class={str(esrt['guid']).upper()}")
        print(f"        current={esrt['version']}  lowest={esrt['lowest']}  "
              f"capsule_flags=0x{esrt['flags']:X}")
        print(f"        last attempt: version={esrt['last_version']} "
              f"status={esrt['last_status']} "
              f"({ATTEMPT_STATUS.get(esrt['last_status'], '?')})")
        if esrt["flags"] != FALLBACK_FLAGS:
            warn(f"capsule_flags is 0x{esrt['flags']:X}, not the "
                 f"0x{FALLBACK_FLAGS:X} this was developed against")
    else:
        warn("could not read ESRT. Run as root so it gets detected; if you already "
             "are root, this machine exposes no system firmware resource and the "
             "capsule will not install")

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

    # --- power state, then either install or print the command
    dev = find_fwupd_device(guid)
    ac, batt = read_power()
    print()
    if ac is False:
        warn("AC adapter not connected, fwupd will refuse to install")
    elif ac is None:
        warn("could not read the power supply state")
    if batt is not None and batt < 30:
        warn(f"battery at {batt}%, charge it before flashing firmware")

    self_cmd = sys.argv[0] if sys.argv[0].startswith(("/", ".")) else f"./{sys.argv[0]}"

    if args.install:
        if not dev:
            die("could not resolve the fwupd device ID, install it manually")
        if ac is not True:
            die("connect the AC adapter and run this again")
        cmd = ["fwupdtool", "install-blob", str(cap_path), dev]
        print("staging: " + " ".join(cmd))
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            die(f"fwupdtool exited {rc}, nothing was staged")
        print("\nStaged on the ESP. The flash happens during the next boot.")
        print("  sudo reboot")
        print(f"  sudo {self_cmd} --check")
        return 0

    print("=" * 72)
    print("re-run with --install to stage it, or do that step yourself:")
    print(f"  sudo fwupdtool install-blob {cap_path} \\")
    print(f"    {dev if dev else '<device ID: System Firmware in fwupdmgr get-devices>'}")
    print("\nthen reboot and read the result:")
    print("  sudo reboot")
    print(f"  sudo {self_cmd} --check")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
