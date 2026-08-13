# 역공학 기록

캡슐 형식을 어떻게 알아냈는지, 필드별 근거를 남겨 둡니다.

기기는 Galaxy Book5 Pro(`NT960XHZ-AD52G` / `960XHA`). Intel Lunar Lake에 BIOS Guard 켜진 AMI
Aptio 펌웨어입니다. 2026-08-13에 `P09AMA.200.251125.01`(ESRT 920)에서
`P11AMA.220.260526.02`(ESRT 1122)로 올렸습니다. 끝나고 DMI, ESRT, `fwupd` 값이 다 같았고
`last_attempt_status`도 0으로 돌아왔습니다. `.exe`와 그 안의 ROM만 있으면 `pefile`,
`capstone`, 파이썬 `lzma`로 여기 있는 걸 다 다시 해 볼 수 있습니다.

## 1. 벤더 업데이터

`ITEM_20260622_22578_WIN_P11AMA.exe`는 삼성 `UnPacker.exe`입니다. DigiCert 경유 삼성전자
Authenticode 서명이 붙어 있고, 안에 gzip으로 압축된 파일이 6개 들어 있습니다.

| 파일 | 정체 |
|---|---|
| `P11AMA.CAP` | AMI BIOS Guard PFAT ROM 이미지, 25.4 MB. 확장자가 `.CAP`이지만 EFI 캡슐이 아닙니다 |
| `AFUWINx64_s.exe` | AMI AFU 플래셔 |
| `amigendrv64.sys` | AFU가 런타임 플래시에 쓰는 AMI 커널 드라이버 |
| `WFU_PAMA.inf` | Microsoft UEFI Firmware Update Platform 드라이버 패키지 |
| `p11ama.cat` | INF 서명 카탈로그 |
| `InstallInfDriverTest64.exe` | INF 설치 헬퍼 |

내장 파일마다 앞에 UTF-16 파일명 레코드가 있고 gzip 스트림은 거기서 `0x20C` 바이트 뒤에
시작합니다. 이 상수가 다른 업데이터에서도 같을지 몰라서, 도구는 파일명 다음에 나오는 첫 gzip
매직을 찾습니다.

`P11AMA.CAP`의 앞부분입니다.

```
c8 01 00 00  f9 8c 00 00  5f 41 4d 49 50 46 41 54   ...._AMIPFAT
63 41 4d 49 5f 42 49 4f 53 5f 47 55 41 52 44 5f   cAMI_BIOS_GUARD_
46 4c 41 53 48 5f 43 4f 4e 46 49 47 55 52 41 54   FLASH_CONFIGURAT
49 4f 4e 53 0d 0a 31 20 2f 4e 20 31 20 3b 4e 56   IONS..1 /N 1 ;NV
```

캡슐이면 맨 앞이 16바이트 GUID여야 합니다. `CapsuleImageSize`가 파일 크기와 맞는 헤더가
어디 있을까 해서 파일 전체를 훑었지만 없었습니다.

### INF에 파라미터가 다 적혀 있다

```ini
[Firmware.NTamd64.10.0...17134]
%FirmwareDesc% = Firmware_Install, UEFI\RES_{A51E51F4-5DE0-4C91-95FE-4197520E51D6}

[Firmware_AddReg]
HKR,,FirmwareId,,{A51E51F4-5DE0-4C91-95FE-4197520E51D6}
HKR,,FirmwareVersion,%REG_DWORD%,1122
HKR,,FirmwareFilename,,%13%\P11AMA.CAP
```

`UEFI\RES_{...}`가 이 업데이트가 겨냥하는 ESRT 리소스입니다. Windows 펌웨어 업데이트
플랫폼의 표준 표기입니다. 버전 숫자는 `P09AMA.200` → 9×100+20 = 920, `P11AMA.220` →
11×100+22 = 1122입니다.

## 2. 펌웨어 디스어셈블

ROM에 펌웨어 볼륨이 23개 있습니다. 필요했던 PEI 모듈 두 개를 비압축 영역에서 TE(Terse
Executable) 이미지로 뽑았습니다.

### `CapsulePei`, ROM 오프셋 `0x00f21d70`, ImageBase `0xfff18fd4`

RVA `0x19c0`의 `.data`입니다.

| RVA | 내용 |
|---|---|
| `0x19ec` | `711C703F-C285-4B10-A3B0-36ECBD3C8BE2`, `gEfiCapsuleVendorGuid` |
| `0x1a30` | `4A3CA68B-7723-48FB-803D-578CC1FEC44D`, 화이트리스트 엔트리 0 |
| `0x1a40` | `A51E51F4-5DE0-4C91-95FE-4197520E51D6`, 엔트리 1이면서 ESRT `fw_class` |
| `0x1a50` | UTF-16 `L"CapsuleUpdateData"` |

`0xfff19a40`, 캡슐 전달 부분입니다.

```asm
lea  r8,  [rip+0xf75]   ; gEfiCapsuleVendorGuid
lea  rdx, [rip+0xfc9]   ; L"CapsuleUpdateData"
mov  qword [rsp+0x50], 8
call qword ptr [rax]    ; GetVariable(...)
```

펌웨어가 `CapsuleUpdateData` EFI 변수를 읽습니다. 부팅할 때 `fwupdx64.efi`가 이 변수를 써서
`UpdateCapsule()`을 부릅니다. `fwupd` 경로가 여기로 들어옵니다. 그래서 `fwupd`가 이 장치를
"UEFI System Resource Table device (updated via NVRAM)"라고 부릅니다.

`0xfff19aa5`부터 `0xfff19bb4`까지, 캡슐 판별 부분입니다.

```asm
cmp  qword [rdx], 0x50706143    ; "CapP" = EFI_CAPSULE_PEIM_PRIVATE_DATA 시그니처
mov  rdi, [rdx+0x10]            ; CapsuleNumber
lea  r12, [rdx+0x18+rdi*8]      ; CapsuleOffset[] 뒤 = 캡슐 데이터 베이스
...
mov  r14, [rbx+rax*8+0x18]      ; CapsuleOffset[i]
add  r14, r12                   ; r14 = EFI_CAPSULE_HEADER*
mov  ebp, [r14+0x18]            ; CapsuleImageSize, 필드가 +0x18
mov  r8,  qword ptr [r14]       ; CapsuleGuid 하위 8바이트
lea  rcx, [rip+0xe6e]           ; .data 0x1a30 = GUID 테이블
cmp  r8, [rcx]
mov  rax, [rcx+8]
cmp  qword [r14+8], rax         ; CapsuleGuid 상위 8바이트
...
add  rcx, 0x10                  ; 다음 엔트리, 16바이트
cmp  edx, 2                     ; 엔트리 딱 2개
```

오프셋 0의 `EFI_CAPSULE_HEADER.CapsuleGuid`를 비교합니다. FMP `UpdateImageTypeId`는 쳐다보지도
않고, `6DCBD5ED-E82D-4C44-BDA1-7194199AD92A`는 테이블에 아예 없습니다.

### `BiosGuardPeiApRecoveryCapsule`, ROM 오프셋 `0x00f2be5c`, ImageBase `0xfff230c0`

`0xfff23e14`, 페이로드 파싱 부분입니다.

```asm
movsd xmm0, [rip+0x88e]         ; "_AMIPFAT"
...
lea  rcx, [rdi+8]               ; 버퍼 + 8
mov  r8d, 8
lea  rdx, [rsp+0x60]
call 0xfff233c0                 ; memcmp(버퍼+8, "_AMIPFAT", 8)
test rax, rax
jne  0xfff240a9                 ; 불일치면 에러
mov  esi, dword ptr [rdi]       ; 버퍼[0] = 스크립트 길이
mov  eax, 0x2020000
cmova esi, eax                  ; 클램프
mov  eax, edi
add  rax, 4                     ; 버퍼+4부터
mov  r8d, 0xa0d                 ; CRLF 찾기
```

`P11AMA.CAP`에서 같은 레이아웃을 찾았습니다. `[0]`에 길이, `[8]`에 `_AMIPFAT`, `[0x11]`부터
CRLF로 끊긴 `AMI_BIOS_GUARD_FLASH_CONFIGURATIONS` 스크립트. 펌웨어가 배포본 PFAT을 그대로
받아 씁니다.

## 3. `Flags = 0x50000`은 어디서 나왔나

`UnPacker.exe`에 EFI 캡슐 빌더가 딱 하나 있습니다. 함수 `0x14001bc80`이고, 만드는 건 로고
캡슐(`FmpLogo.bin`)입니다. 그래도 삼성이 캡슐 헤더를 어떤 값으로 채우는지 볼 수 있는 곳은
패키지에서 여기뿐입니다.

프롤로그가 `lea rbp, [rsp-0x20d8]` 다음 `sub rsp, 0x21d8`이라 `rbp = rsp+0x100`입니다. 여기서
스택 오프셋을 풀었고, `fwrite` 세 번이 UEFI 구조체 세 개로 떨어졌습니다.

```asm
0x14001bfc1  movups xmm0, [rip+0x1de218]        ; CapsuleGuid
0x14001bfbb  mov    r14d, 0x1c                  ; HeaderSize
0x14001c00f  mov    dword [rbp-0x54], 0x50000   ; Flags
0x14001bfd1  lea    eax, [r15+0x54]             ; CapsuleImageSize = 페이로드 + 0x54
```

로고 경로의 헤더 합계는 `0x54`입니다. 캡슐 헤더 `0x1C`, FMP 헤더 `0x10`, FMP 이미지 헤더
`0x28`, `UpdateVendorCodeSize`는 0, `EFI_FIRMWARE_IMAGE_AUTHENTICATION`은 없음. 시스템 펌웨어
쪽은 FMP 계층이 빠집니다. 디스패치가 `CapsuleGuid`로 일어나고 BIOS Guard 파서는 본문 오프셋
8에서 `_AMIPFAT`을 찾으니까요.

BIOS용 캡슐 빌더는 패키지 어디에도 없습니다. AFU는 아예 다른 길로 갑니다.
`amigendrv64.sys`를 서비스로 올리고 `DeviceIoControl`로 런타임에 굽습니다. AFU가 `4A3CA68B`와
`414D94AD`를 참조하는 곳이 있는데, 빌더인 줄 알고 따라가 보니 ROM 기능 탐지용 버퍼
스캔이었습니다.

## 4. ESRT 교차 확인

`Flags` 값은 기기가 직접 알려줍니다.

```
/sys/firmware/efi/esrt/entries/entry0/fw_class:a51e51f4-5de0-4c91-95fe-4197520e51d6
/sys/firmware/efi/esrt/entries/entry0/fw_type:1                   # SYSTEMFIRMWARE
/sys/firmware/efi/esrt/entries/entry0/capsule_flags:0x50000        # 디스어셈블 값과 같음
/sys/firmware/efi/esrt/entries/entry0/fw_version:920
/sys/firmware/efi/esrt/entries/entry0/lowest_supported_fw_version:920
/sys/firmware/efi/esrt/entries/entry0/last_attempt_version:920
/sys/firmware/efi/esrt/entries/entry0/last_attempt_status:0        # SUCCESS
```

`last_attempt_version: 920`에 `status: 0`이니, 지금 깔린 P09AMA도 이 캡슐 경로로 들어와서
성공한 겁니다. 기계가 직접 남긴 기록이라, 손대기 전에 이 경로가 살아 있다는 걸 이걸로
확인했습니다. `OsIndicationsSupported`는 `0x1b`입니다.
`FMP_CAPSULE_SUPPORTED`(`0x08`)와 `CAPSULE_RESULT_VAR_SUPPORTED`(`0x10`)는 켜져 있고
`FILE_CAPSULE_DELIVERY`(`0x04`)는 꺼져 있습니다. Capsule-on-Disk는 못 쓰니 런타임 서비스로
넘겨야 합니다.

## 5. 처음에 잘못 든 길

처음 만든 캡슐은 UEFI 표준 FMP 형식이었습니다. `CapsuleGuid =
6DCBD5ED-E82D-4C44-BDA1-7194199AD92A` 뒤에
`EFI_FIRMWARE_MANAGEMENT_CAPSULE_HEADER`(`Version 1`, `PayloadItemCount 1`,
`ItemOffsetList[0] = 0x10`), 그 뒤에
`EFI_FIRMWARE_MANAGEMENT_CAPSULE_IMAGE_HEADER`(`Version 2`,
`UpdateImageTypeId = A51E51F4-...`, `UpdateImageIndex 1`), 헤더 `0x54`바이트. 그 GUID는
화이트리스트에 없으니 매칭될 수가 없습니다. `CapsulePei` 디스패치 루프를 읽다가 알았습니다.
다른 AMI 기반 기기로 옮기신다면 표준 형식을 가정하기 전에 그 화이트리스트부터 읽어 보세요.

## 6. 환경 메모

* `efi_capsule_loader` 불필요. Fedora가 `CONFIG_EFI_CAPSULE_LOADER`를 끄고 빌드해서
  `/dev/efi_capsule_loader` 자체가 없음. `fwupd`도 안 씀. 캡슐을 ESP에 올리고 `BootNext`를
  `fwupdx64.efi`로 잡아서 거기서 `UpdateCapsule()` 호출.
* AC 전원은 ESRT와 `fwupd` 양쪽에서 강제.
* LVFS에 이 기기 펌웨어 없음. 삼성이 Galaxy Book 펌웨어를 안 올려서. 올라가면 이 문서 전체가
  `fwupdmgr update` 한 줄.
* BIOS 업데이트가 NVRAM 부트 엔트리를 지울 수 있음. 디스크는 무사. 라이브 USB에서
  `efibootmgr`로 복구.

## 7. 확인 못 한 것

* GUID가 맞았을 때 타는 분기(`call qword ptr [rax+0x40]` → `0xfff1a40c`)는 BIOS Guard
  파서까지 안 따라가 봤음. 캡슐 헤더와 PFAT 사이에 뭐가 더 안 들어간다는 건 양끝이 맞물리는
  걸로 추론. `0x1C` 헤더면 파서가 읽는 본문 오프셋 8에 `_AMIPFAT`이 정확히 놓임. 플래시가
  성공한 뒤에야 확실해진 부분.
* ROM 펌웨어 볼륨 중 LZMA로 풀린 건 하나(5.3 MB)뿐. DXE 쪽은 못 봤음. FMP(`6DCBD5ED`)
  핸들러가 DXE에 있으면 FMP 형식도 통할 수 있음. 이 문서에 적은 형식은 직접 플래시해서 확인한
  쪽.
* 날 PFAT을 넘기면 `fwupd`가 캡슐 헤더를 알아서 붙여 주는지는 모름. `fwupd`가 `uefi-capsule`
  펌웨어 GType을 안 내놔서 확인 불가. 헤더를 직접 붙이니 물어볼 일이 없어짐.

## 재현

도구가 1절과 헤더 구성을 합니다. 펌웨어 쪽은 TE 이미지를 뽑을 때 `"VZ"`를 역방향으로 스캔해서
`Machine`과 `NumberOfSections`를 검증하고 `fileOffset = PointerToRawData - (StrippedSize -
0x28)`로 계산했습니다. RIP-상대 xref는 `.text`의 각 오프셋 `p`에서
`target = base + p + 4 + int32(code[p:p+4])`로 역산했습니다. 펌웨어 볼륨 순회는 표준 EDK2
구조 파싱입니다. FFS 파일, GUID-defined와 compression 섹션, `lzma.FORMAT_ALONE`으로 LZMA
해제, 중첩 FV 이미지는 재귀.
