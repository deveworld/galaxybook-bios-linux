# galaxybook-bios-linux

Update Samsung Galaxy Book BIOS from Linux without Windows.

한국어: [README.ko.md](README.ko.md)

Samsung ships BIOS updates as a Windows `.exe`. Crack one open and you get a raw AMI BIOS
Guard (PFAT) image rather than a finished UEFI capsule. The firmware still takes updates over
the ordinary UEFI capsule path that `fwupd` already drives, so wrapping that PFAT in a
28-byte `EFI_CAPSULE_HEADER` is enough to install it.

```console
$ sudo ./samsung-bios-capsule.py ITEM_20260622_22578_WIN_P11AMA.exe -o .
$ sudo fwupdtool install-blob P11AMA_esrt.cap <device-id>
$ sudo reboot
```

## Scope

This repo has the tool and the analysis, nothing else. Samsung owns the firmware and I'm not
redistributing it, so you download the `.exe` yourself and the script pulls the payload out
locally, with `.gitignore` blocking firmware blobs from commits.

## Warning

Flashing firmware can leave a machine unbootable. No warranty, see [LICENSE](LICENSE).
Samsung hasn't endorsed any of this.

Two things stop a bad wrapper from bricking anything: the payload carries Samsung's signature
and Intel BIOS Guard checks it inside the ACM before writing, so a malformed wrapper gets
rejected before the ROM is touched, and the firmware writes the result to ESRT
`last_attempt_status` so you can read what went wrong after the reboot. NVRAM is the part
that can still bite you. A BIOS update may clear your boot entries, and although the disk is
fine, an encrypted install with no boot entry looks a lot like a dead machine, so keep a live
USB around to put the entry back with `efibootmgr`. AC power is required and `fwupd` enforces
it, so don't reach for `--force`.

## Requirements

* A Samsung Galaxy Book whose ESRT exposes a system firmware resource (`fw_type: 1`). I've only
  tested Galaxy Book5 Pro (`NT960XHZ-AD52G` / `960XHA`, AMI Aptio with Intel BIOS Guard).
* `fwupd` with the `uefi_capsule` plugin, plus `fwupd-efi`.
* Python 3.9+, standard library only.
* The BIOS updater `.exe` from Samsung support.

You don't need the `efi_capsule_loader` module. Fedora ships without it
(`CONFIG_EFI_CAPSULE_LOADER` unset) and `fwupd` stages the capsule on the ESP and calls
`UpdateCapsule()` from `fwupdx64.efi` at boot instead.

## Usage

Run it as root so it can read ESRT and work the values out for itself.

```console
$ sudo ./samsung-bios-capsule.py ITEM_20260622_22578_WIN_P11AMA.exe -o .
입력: ITEM_20260622_22578_WIN_P11AMA.exe  (15,947,928 bytes)
  PFAT 페이로드: P11AMA.CAP  25,362,432 bytes
  내장 INF: FirmwareId=A51E51F4-...  목표버전=1122
  ESRT: fw_class=A51E51F4-...  현재=920  최저지원=920  capsule_flags=0x50000
  OK    업그레이드: 920 → 1122
```

It builds and verifies the capsule, then prints the install command; nothing reaches firmware
until you run `fwupdtool`. The script reads the header values off your own machine instead of
shipping them as constants, so a new BIOS release or a different model may just work:

| Header field | Source | Fallback |
|---|---|---|
| `CapsuleGuid` | ESRT `fw_class` | INF `FirmwareId` |
| `Flags` | ESRT `capsule_flags` | `0x50000` |

The target version comes from the INF's `FirmwareVersion`, and the script stops if that INF's
`FirmwareId` disagrees with ESRT `fw_class`, because a mismatch means the `.exe` is for a
different machine. It also refuses a target below the firmware's
`lowest_supported_fw_version` and warns on downgrades. Without root it falls back to the INF
values and the default flags, and `--guid` / `--flags` override both.

## How it works

The firmware dispatches on `EFI_CAPSULE_HEADER.CapsuleGuid`. It matches that against a
two-entry whitelist, and the ESRT `fw_class` is one of the two, so the capsule is just this:

```c
EFI_CAPSULE_HEADER {                       // 0x1C bytes
  EFI_GUID CapsuleGuid      = <ESRT fw_class>;
  UINT32   HeaderSize       = 0x1C;
  UINT32   Flags            = <ESRT capsule_flags>;   // 0x50000 observed
  UINT32   CapsuleImageSize = 0x1C + sizeof(PFAT);
}
// PFAT payload follows immediately
```

No FMP headers, no `EFI_FIRMWARE_IMAGE_AUTHENTICATION`. I built the standard FMP form first,
with `UpdateImageTypeId` under `6DCBD5ED-E82D-4C44-BDA1-7194199AD92A`, and it went nowhere.
[docs/reverse-engineering.md](docs/reverse-engineering.md) has the evidence: the `CapsulePei`
and `BiosGuardPeiApRecoveryCapsule` disassembly, the vendor updater's own capsule builder, the
embedded WFU INF, and the matching ESRT values.

## Optional cabinet archive

`install-blob` skips version checks and history. A `.cab` gets you `fwupdmgr install` with
proper reporting.

```console
$ fwupdtool build-cabinet firmware.cab P11AMA_esrt.cap firmware.metainfo.xml
$ fwupdmgr get-details firmware.cab      # confirm it matches your device
$ sudo fwupdmgr install firmware.cab
```

Copy [examples/firmware.metainfo.xml](examples/firmware.metainfo.xml) and edit the version,
date and GUID, keeping the `org.unofficial.*` component `id` because the reverse-DNS namespace
is the vendor's. I've tested this path less than `install-blob` and `fwupd` reports
`convert_version not implemented` when reinstalling the same version, so fall back to
`install-blob` if a `.cab` install fails.

## Why this isn't on LVFS

[LVFS](https://lvfs.readthedocs.io/en/latest/apply.html) takes firmware from the silicon
vendor, ODM or OEM only, and only with legal permission to redistribute. Promotion to
`testing` or `stable` needs a document from the vendor's legal department, and the GUID is
Samsung's anyway.

Samsung could open an LVFS vendor account tomorrow, and it's worth asking them to. Their
firmware already ships as ESRT capsules and they already build Windows WFU INF packages, so
they'd only need to pair the existing `.cap` with a `metainfo.xml`.

## Troubleshooting

After the reboot, check what the firmware recorded.

```console
$ sudo grep -r . /sys/firmware/efi/esrt/entries/entry0/
```

| `last_attempt_status` | Meaning | Next step |
|---|---|---|
| `0` | Success | Check that `fw_version` advanced |
| `3` | `INCORRECT_VERSION` | Compare target against `lowest_supported_fw_version` |
| `4` | `INVALID_IMAGE_FORMAT` | Wrapper rejected; try the FMP form or open an issue |
| `5` | `AUTH_ERROR` | Signature verification failed, stop here |
| `6` / `7` | `PWR_EVT_AC` / `PWR_EVT_BATT` | Connect AC, charge, retry |

Cross-check against `/sys/class/dmi/id/bios_version` and `fwupdmgr get-devices`.

## Contributing

Send results from other Galaxy Book models. That's the most useful thing I can get: your
`fw_class` and `capsule_flags`, whether the capsule took, and no firmware binaries please.

## License

[Apache-2.0](LICENSE) covers the tool and the docs.
