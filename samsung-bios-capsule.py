#!/usr/bin/env python3
"""
삼성 Galaxy Book BIOS 업데이터(.exe) → UEFI 캡슐 변환기 (리눅스용)

삼성 Windows 업데이터 안에는 날 PFAT(AMI BIOS Guard) 이미지가 들어 있다.
그걸 꺼내서 28바이트 EFI_CAPSULE_HEADER를 씌우면 fwupd로 설치할 수 있다.

플래시는 하지 않는다. 캡슐을 만들고 검증해서 설치 명령만 찍어 준다.

사용법:
    ./samsung-bios-capsule.py ITEM_xxxxx_WIN_P11AMA.exe [-o 출력디렉터리]

헤더 값은 기기에서 직접 읽어 온다:
  - ESRT (/sys/firmware/efi/esrt) → CapsuleGuid, Flags, 현재/최저 지원 버전
  - exe 내장 WFU_*.inf            → FirmwareId, 목표 버전

근거는 README.ko.md 참조.
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
    sys.exit(f"오류: {msg}")


def warn(msg):
    print(f"  경고: {msg}")


# ---------------------------------------------------------------- exe 해체

def _gunzip_at(blob, off):
    """오프셋의 gzip 스트림을 해제. 뒤에 붙은 쓰레기 데이터를 허용한다."""
    try:
        return gzip.GzipFile(fileobj=io.BytesIO(blob[off:])).read()
    except Exception:
        return zlib.decompressobj(31).decompress(blob[off:])


def extract_embedded(exe_bytes, suffix):
    """'<이름><suffix>.gz' UTF-16 파일명 뒤의 첫 gzip 블롭을 해제해 돌려준다.

    삼성 UnPacker는 파일명 레코드 뒤 0x20c 바이트 지점에 gzip 데이터를 둔다.
    오프셋을 가정하지 않고 파일명 다음 첫 gzip 매직을 찾는다.
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
    """PFAT 페이로드(<모델>.CAP)를 찾아 (이름, 데이터)로 돌려준다."""
    for name_off, gz_off, data in extract_embedded(exe_bytes, ".CAP"):
        if data[8:16] == b"_AMIPFAT":
            # 파일명은 needle 앞쪽에 있다. UTF-16이라 문자당 2바이트이고
            # 창 시작점의 홀짝을 needle과 맞춰야 디코딩이 깨지지 않는다.
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
    die("exe 안에서 PFAT 페이로드(*.CAP)를 찾지 못했습니다")


def find_inf(exe_bytes):
    """내장 WFU INF를 파싱해 {FirmwareId, FirmwareVersion, DriverVer}를 돌려준다."""
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


# ---------------------------------------------------------------- ESRT

def read_esrt():
    """fw_type==1(시스템 펌웨어) ESRT 엔트리를 읽는다. root 아니면 None."""
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
    """ESRT GUID에 해당하는 fwupd 장치 ID를 best-effort로 찾는다."""
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


# ---------------------------------------------------------------- 캡슐 생성

def build_capsule(pfat, capsule_guid, flags):
    return (capsule_guid.bytes_le
            + struct.pack("<III", CAPSULE_HEADER_SIZE, flags,
                          CAPSULE_HEADER_SIZE + len(pfat))
            + pfat)


def verify(cap, expect_guid, expect_flags):
    """만든 캡슐을 펌웨어가 읽는 순서대로 되짚어 본다."""
    ok = True
    guid = uuid.UUID(bytes_le=cap[:16])
    hs, fl, cis = struct.unpack("<III", cap[16:28])
    body = cap[hs:]

    def check(label, value, good):
        nonlocal ok
        ok = ok and good
        print(f"  {'OK  ' if good else 'FAIL'}  {label}: {value}")

    print("\nCapsulePei가 보는 헤더:")
    check("CapsuleGuid", str(guid).upper(), guid == expect_guid)
    check("HeaderSize", f"0x{hs:X}", hs == CAPSULE_HEADER_SIZE)
    check("Flags", f"0x{fl:08X}", fl == expect_flags)
    check("CapsuleImageSize", f"{cis:,}", cis == len(cap))

    print("BiosGuardPei가 보는 본문 (캡슐+HeaderSize):")
    check("body[8:16] 시그니처", body[8:16].decode("latin1", "replace"),
          body[8:16] == b"_AMIPFAT")
    script_len, = struct.unpack("<I", body[0:4])
    check("body[0:4] 스크립트 길이", f"0x{script_len:X} (한계 0x2020000)",
          0 < script_len <= 0x2020000)
    crlf = body.find(b"\r\n", 4)
    tag = body[0x11:crlf].decode("latin1", "replace") if crlf > 0 else "(없음)"
    check("첫 CRLF 전 텍스트", tag, tag.startswith("AMI_BIOS_GUARD"))
    return ok


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="삼성 BIOS 업데이터 exe → fwupd용 UEFI 캡슐")
    ap.add_argument("exe", type=pathlib.Path, help="ITEM_*_WIN_*.exe")
    ap.add_argument("-o", "--outdir", type=pathlib.Path, default=None,
                    help="출력 디렉터리 (기본: exe와 같은 위치)")
    ap.add_argument("--guid", help="CapsuleGuid 직접 지정 (ESRT 못 읽을 때)")
    ap.add_argument("--flags", help="Flags 직접 지정 (예: 0x50000)")
    args = ap.parse_args()

    if not args.exe.is_file():
        die(f"파일 없음: {args.exe}")
    outdir = args.outdir or args.exe.parent
    outdir.mkdir(parents=True, exist_ok=True)

    exe = args.exe.read_bytes()
    print(f"입력: {args.exe.name}  ({len(exe):,} bytes)")

    # --- 페이로드 + 메타데이터
    pfat_name, pfat = find_pfat(exe)
    print(f"  PFAT 페이로드: {pfat_name}  {len(pfat):,} bytes")

    inf = find_inf(exe)
    if inf:
        print(f"  내장 INF: FirmwareId={str(inf['guid']).upper()}  "
              f"목표버전={inf['version']}  DriverVer={inf['driver_ver']}")
    else:
        warn("내장 WFU INF를 찾지 못했습니다 (버전 검사 생략)")

    esrt = read_esrt()
    if esrt:
        print(f"  ESRT: fw_class={str(esrt['guid']).upper()}")
        print(f"        현재={esrt['version']}  최저지원={esrt['lowest']}  "
              f"capsule_flags=0x{esrt['flags']:X}")
        print(f"        직전시도: 버전={esrt['last_version']} "
              f"상태={esrt['last_status']}")
    else:
        warn("ESRT를 읽지 못했습니다 (root로 실행하면 자동 검출됩니다)")

    # --- 헤더 값 결정: ESRT > INF > 기본값
    if args.guid:
        guid, guid_src = uuid.UUID(args.guid), "명령행"
    elif esrt:
        guid, guid_src = esrt["guid"], "ESRT fw_class"
    elif inf:
        guid, guid_src = inf["guid"], "INF FirmwareId"
    else:
        die("CapsuleGuid를 정할 수 없습니다. root로 실행하거나 --guid를 지정하세요")

    if args.flags:
        flags, flags_src = int(args.flags, 16), "명령행"
    elif esrt:
        flags, flags_src = esrt["flags"], "ESRT capsule_flags"
    else:
        flags, flags_src = FALLBACK_FLAGS, "기본값"

    print(f"\n헤더 값 출처: CapsuleGuid ← {guid_src},  Flags ← {flags_src}")

    # --- 정합성 검사
    print("\n사전 검사:")
    if esrt and inf:
        same = esrt["guid"] == inf["guid"]
        print(f"  {'OK  ' if same else 'FAIL'}  ESRT fw_class == INF FirmwareId")
        if not same:
            die("GUID 불일치. 이 exe는 이 기기용이 아닙니다")
        if inf["version"] <= esrt["version"]:
            warn(f"목표 {inf['version']} <= 현재 {esrt['version']} "
                 f"입니다. 다운그레이드나 재설치는 거부될 수 있습니다")
        else:
            print(f"  OK    업그레이드: {esrt['version']} → {inf['version']}")
        if inf["version"] < esrt["lowest"]:
            die(f"목표 {inf['version']} < 최저지원 {esrt['lowest']} "
                f"입니다. 펌웨어가 거부합니다")

    # --- 생성 + 검증
    cap = build_capsule(pfat, guid, flags)
    stem = pfat_name.rsplit(".", 1)[0]
    pfat_path = outdir / pfat_name
    cap_path = outdir / f"{stem}_esrt.cap"
    pfat_path.write_bytes(pfat)
    cap_path.write_bytes(cap)

    ok = verify(cap, guid, flags)
    print(f"\n출력: {cap_path}  ({len(cap):,} bytes)")
    print(f"  sha256 = {hashlib.sha256(cap).hexdigest()}")

    if not ok:
        die("검증 실패. 설치하지 마세요")

    # --- 설치 안내
    dev = find_fwupd_device(guid)
    print("\n" + "=" * 72)
    print("설치 (AC 어댑터 연결 필수):")
    print(f"  sudo fwupdtool install-blob {cap_path} \\")
    print(f"    {dev if dev else '<장치ID: fwupdmgr get-devices의 System Firmware>'}")
    print("\n재부팅 후 결과 확인:")
    print("  sudo grep -r . /sys/firmware/efi/esrt/entries/entry0/")
    print("    last_attempt_status  0=성공  3=버전거부  4=이미지형식오류  "
          "5=인증실패  6/7=전원")
    print("=" * 72)


if __name__ == "__main__":
    main()
