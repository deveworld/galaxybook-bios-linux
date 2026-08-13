# Reverse-engineering notes

How I worked out the capsule format, and the evidence for each field.

Machine: Samsung Galaxy Book5 Pro (`NT960XHZ-AD52G` / `960XHA`), AMI Aptio firmware on Intel
Lunar Lake with BIOS Guard. I flashed `P09AMA.200.251125.01` (ESRT 920) up to
`P11AMA.220.260526.02` (ESRT 1122) on 2026-08-13. DMI, ESRT and `fwupd` all agree afterwards
and `last_attempt_status` came back 0.

You can redo all of this with `pefile`, `capstone` and Python's `lzma`, given the `.exe` and
the ROM it carries.

## 1. The vendor updater

`ITEM_20260622_22578_WIN_P11AMA.exe` is Samsung's `UnPacker.exe`, Authenticode-signed by
Samsung Electronics via DigiCert. Six gzip-compressed files sit inside it:

| File | What it is |
|---|---|
| `P11AMA.CAP` | AMI BIOS Guard PFAT ROM image, 25.4 MB. Despite the name it's not an EFI capsule |
| `AFUWINx64_s.exe` | AMI AFU flasher |
| `amigendrv64.sys` | AMI kernel driver, used by AFU's runtime flash path |
| `WFU_PAMA.inf` | Microsoft UEFI Firmware Update Platform driver package |
| `p11ama.cat` | Signature catalog for the INF |
| `InstallInfDriverTest64.exe` | INF install helper |

Every embedded file has a UTF-16 filename record in front of it and the gzip stream starts
`0x20C` bytes further on. I didn't want to bet on that constant, so the tool just looks for
the next gzip magic after the filename.

`P11AMA.CAP` starts:

```
c8 01 00 00  f9 8c 00 00  5f 41 4d 49 50 46 41 54   ...._AMIPFAT
63 41 4d 49 5f 42 49 4f 53 5f 47 55 41 52 44 5f   cAMI_BIOS_GUARD_
46 4c 41 53 48 5f 43 4f 4e 46 49 47 55 52 41 54   FLASH_CONFIGURAT
49 4f 4e 53 0d 0a 31 20 2f 4e 20 31 20 3b 4e 56   IONS..1 /N 1 ;NV
```

A capsule would need a 16-byte GUID at the front. I also scanned the whole file for a header
whose `CapsuleImageSize` matched the file length and found nothing, so there's no capsule
hiding at an offset either.

### The INF hands you the parameters

```ini
[Firmware.NTamd64.10.0...17134]
%FirmwareDesc% = Firmware_Install, UEFI\RES_{A51E51F4-5DE0-4C91-95FE-4197520E51D6}

[Firmware_AddReg]
HKR,,FirmwareId,,{A51E51F4-5DE0-4C91-95FE-4197520E51D6}
HKR,,FirmwareVersion,%REG_DWORD%,1122
HKR,,FirmwareFilename,,%13%\P11AMA.CAP
```

`UEFI\RES_{...}` names the ESRT resource this update targets. This is the standard Windows
firmware-update-platform path, nothing Samsung-specific about it.

Version encoding: `P09AMA.200` gives 9×100+20 = 920, `P11AMA.220` gives 11×100+22 = 1122.

## 2. Firmware disassembly

The ROM holds 23 firmware volumes. I pulled two PEI modules out of uncompressed regions as TE
(Terse Executable) images.

### `CapsulePei`, ROM offset `0x00f21d70`, ImageBase `0xfff18fd4`

Its `.data` at RVA `0x19c0`:

| RVA | Contents |
|---|---|
| `0x19ec` | `711C703F-C285-4B10-A3B0-36ECBD3C8BE2`, `gEfiCapsuleVendorGuid` |
| `0x1a30` | `4A3CA68B-7723-48FB-803D-578CC1FEC44D`, whitelist entry 0 |
| `0x1a40` | `A51E51F4-5DE0-4C91-95FE-4197520E51D6`, whitelist entry 1 and the ESRT `fw_class` |
| `0x1a50` | `L"CapsuleUpdateData"` in UTF-16 |

Capsule delivery at `0xfff19a40`:

```asm
lea  r8,  [rip+0xf75]   ; gEfiCapsuleVendorGuid
lea  rdx, [rip+0xfc9]   ; L"CapsuleUpdateData"
mov  qword [rsp+0x50], 8
call qword ptr [rax]    ; GetVariable(...)
```

The firmware reads the `CapsuleUpdateData` EFI variable. `UpdateCapsule()` writes that
variable, and `fwupd` triggers it when `fwupdx64.efi` runs at boot, which is why `fwupd` calls
the device "UEFI System Resource Table device (updated via NVRAM)".

Capsule dispatch, `0xfff19aa5` through `0xfff19bb4`:

```asm
cmp  qword [rdx], 0x50706143    ; "CapP" = EFI_CAPSULE_PEIM_PRIVATE_DATA signature
mov  rdi, [rdx+0x10]            ; CapsuleNumber
lea  r12, [rdx+0x18+rdi*8]      ; past CapsuleOffset[] = base of capsule data
...
mov  r14, [rbx+rax*8+0x18]      ; CapsuleOffset[i]
add  r14, r12                   ; r14 = EFI_CAPSULE_HEADER*
mov  ebp, [r14+0x18]            ; CapsuleImageSize, field at +0x18
mov  r8,  qword ptr [r14]       ; CapsuleGuid, low 8 bytes
lea  rcx, [rip+0xe6e]           ; .data 0x1a30 = GUID table
cmp  r8, [rcx]
mov  rax, [rcx+8]
cmp  qword [r14+8], rax         ; CapsuleGuid, high 8 bytes
...
add  rcx, 0x10                  ; next entry, 16 bytes
cmp  edx, 2                     ; exactly two entries
```

It compares `EFI_CAPSULE_HEADER.CapsuleGuid` at offset 0. It never looks at the FMP
`UpdateImageTypeId`, and `6DCBD5ED-E82D-4C44-BDA1-7194199AD92A` isn't in the table at all.

### `BiosGuardPeiApRecoveryCapsule`, ROM offset `0x00f2be5c`, ImageBase `0xfff230c0`

Payload parsing at `0xfff23e14`:

```asm
movsd xmm0, [rip+0x88e]         ; "_AMIPFAT"
...
lea  rcx, [rdi+8]               ; buffer + 8
mov  r8d, 8
lea  rdx, [rsp+0x60]
call 0xfff233c0                 ; memcmp(buffer+8, "_AMIPFAT", 8)
test rax, rax
jne  0xfff240a9                 ; mismatch, error out
mov  esi, dword ptr [rdi]       ; buffer[0] = script length
mov  eax, 0x2020000
cmova esi, eax                  ; clamp
mov  eax, edi
add  rax, 4                     ; scan from buffer+4
mov  r8d, 0xa0d                 ; for CRLF
```

I found the same layout in `P11AMA.CAP`: length at `[0]`, `_AMIPFAT` at `[8]`, then the
CRLF-delimited `AMI_BIOS_GUARD_FLASH_CONFIGURATIONS` script from `[0x11]`. The firmware takes
the shipped PFAT as-is.

## 3. Where `Flags = 0x50000` came from

`UnPacker.exe` has exactly one EFI capsule builder in it, function `0x14001bc80`, and what it
writes is a logo capsule (`FmpLogo.bin`). It's the only place in the package where I could see
what Samsung puts in a capsule header.

The prologue does `lea rbp, [rsp-0x20d8]` then `sub rsp, 0x21d8`, so `rbp = rsp+0x100`. I resolved the
stack offsets from that, and three `fwrite` calls fell out as three UEFI structures:

```asm
0x14001bfc1  movups xmm0, [rip+0x1de218]        ; CapsuleGuid
0x14001bfbb  mov    r14d, 0x1c                  ; HeaderSize
0x14001c00f  mov    dword [rbp-0x54], 0x50000   ; Flags
0x14001bfd1  lea    eax, [r15+0x54]             ; CapsuleImageSize = payload + 0x54
```

The logo path's header totals `0x54`: capsule header `0x1C`, FMP header `0x10`, FMP image
header `0x28`, `UpdateVendorCodeSize = 0`, no `EFI_FIRMWARE_IMAGE_AUTHENTICATION`. For system
firmware the FMP layers drop away, because dispatch happens on `CapsuleGuid` and the BIOS
Guard parser wants `_AMIPFAT` at body offset 8.

There's no BIOS capsule builder anywhere in the package. AFU goes a different way. It
registers `amigendrv64.sys` as a service and flashes at runtime through `DeviceIoControl`. I
chased AFU's references to `4A3CA68B` and `414D94AD` expecting a builder and found buffer
scans that detect ROM features.

## 4. ESRT cross-check

The machine reports the `Flags` value itself:

```
/sys/firmware/efi/esrt/entries/entry0/fw_class:a51e51f4-5de0-4c91-95fe-4197520e51d6
/sys/firmware/efi/esrt/entries/entry0/fw_type:1                   # SYSTEMFIRMWARE
/sys/firmware/efi/esrt/entries/entry0/capsule_flags:0x50000        # matches disassembly
/sys/firmware/efi/esrt/entries/entry0/fw_version:920
/sys/firmware/efi/esrt/entries/entry0/lowest_supported_fw_version:920
/sys/firmware/efi/esrt/entries/entry0/last_attempt_version:920
/sys/firmware/efi/esrt/entries/entry0/last_attempt_status:0        # SUCCESS
```

`last_attempt_version: 920` with `status: 0` is the machine's own record that the P09AMA
already installed came in through this capsule path and worked. That told me the path was live before I
touched anything.

`OsIndicationsSupported` is `0x1b`, so `FMP_CAPSULE_SUPPORTED` (`0x08`) and
`CAPSULE_RESULT_VAR_SUPPORTED` (`0x10`) are set and `FILE_CAPSULE_DELIVERY` (`0x04`) is clear.
Capsule-on-Disk is out, so delivery has to go through the runtime service.

## 5. The wrong turn I took first

My first capsule used the UEFI standard FMP form: `CapsuleGuid =`
`6DCBD5ED-E82D-4C44-BDA1-7194199AD92A`, then `EFI_FIRMWARE_MANAGEMENT_CAPSULE_HEADER`
(`Version 1`, `PayloadItemCount 1`, `ItemOffsetList[0] = 0x10`) and
`EFI_FIRMWARE_MANAGEMENT_CAPSULE_IMAGE_HEADER` (`Version 2`,
`UpdateImageTypeId = A51E51F4-...`, `UpdateImageIndex 1`), for an `0x54`-byte header.

It wouldn't have matched. I caught it reading the `CapsulePei` dispatch loop. If you port this
to another AMI-based machine, read that whitelist before you assume the standard form.

## 6. Environment notes

* You don't need `efi_capsule_loader`. Fedora builds with `CONFIG_EFI_CAPSULE_LOADER` unset so
  `/dev/efi_capsule_loader` never shows up, and `fwupd` doesn't want it anyway: it stages the
  capsule on the ESP, sets `BootNext` to `fwupdx64.efi`, and that calls `UpdateCapsule()` from
  EFI context.
* AC power is enforced twice over, by ESRT and by `fwupd`.
* LVFS carries nothing for this device because Samsung doesn't publish Galaxy Book firmware
  there. If they published there, all of this would collapse into `fwupdmgr update`.
* A BIOS update can clear NVRAM boot entries. Disk untouched; recover with `efibootmgr` from a
  live USB.

## 7. Not verified

* The branch on a successful GUID match (`call qword ptr [rax+0x40]` to `0xfff1a40c`) I never
  traced through to the BIOS Guard parser. I inferred that nothing extra sits between the
  capsule header and the PFAT from both ends agreeing, since a `0x1C` header puts `_AMIPFAT`
  at body offset 8 which is exactly where the parser reads. Then the flash worked, so the inference
  held.
* Only one of the ROM's firmware volumes came out via LZMA (5.3 MB), so I never got DXE-side
  coverage. An FMP (`6DCBD5ED`) handler could live in DXE, in which case the FMP form might
  work too. The form in this doc is the one I actually flashed.
* Whether `fwupd` prepends a capsule header itself when you hand it a raw PFAT, I don't know.
  `fwupd` exposes no `uefi-capsule` firmware GType to inspect and the explicit header made the
  question moot.

## Reproducing

The tool does section 1 and builds the header. For the firmware side:

```python
# TE image extraction: scan back for "VZ", validate Machine/NumberOfSections,
# then fileOffset = PointerToRawData - (StrippedSize - 0x28)
# RIP-relative xrefs: for each offset p in .text, target = base + p + 4 + int32(code[p:p+4])
```

Walking the firmware volumes (FFS files, GUID-defined and compression sections, LZMA via
`lzma.FORMAT_ALONE`, recursing into nested FV images) is standard EDK2 structure parsing.
