# galaxybook-bios-linux

Windows 없이 리눅스에서 삼성 Galaxy Book BIOS를 올립니다.

English: [README.md](README.md)

삼성 BIOS 업데이터 `.exe` 안에는 날 AMI BIOS Guard(PFAT) 이미지가 들어 있습니다. 확장자가
`.CAP`이지만 UEFI 캡슐은 아닙니다. 그런데 펌웨어는 `fwupd`가 이미 쓰는 평범한 UEFI 캡슐
경로로 업데이트를 받아서, PFAT에 28바이트 `EFI_CAPSULE_HEADER`만 씌우면 설치됩니다.

```console
$ sudo ./samsung-bios-capsule.py ITEM_20260622_22578_WIN_P11AMA.exe -o .
$ sudo fwupdtool install-blob P11AMA_esrt.cap <device-id>
$ sudo reboot
```

## 범위

펌웨어는 삼성 소유라 재배포하지 않고, 이 저장소에는 도구와 분석만 있습니다.

`.exe`는 직접 받으세요. 페이로드는 스크립트가 로컬에서 꺼냅니다. `.gitignore`가 펌웨어
바이너리 커밋을 막습니다.

## 경고

플래시가 잘못되면 기기가 부팅을 못 할 수 있습니다. 보증은 없습니다([LICENSE](LICENSE)).
삼성이 승인한 것도 아닙니다.

래퍼를 잘못 만들어도 벽돌이 되지는 않습니다. 페이로드에 삼성 서명이 들어 있어서 Intel BIOS
Guard ACM이 쓰기 전에 검증하고, 틀리면 ROM에 손대기 전에 거부합니다. 결과는 ESRT
`last_attempt_status`에 찍히니 재부팅한 뒤에 왜 실패했는지 읽을 수 있습니다.

실제로 물리는 건 NVRAM입니다.

* BIOS 업데이트가 부트 엔트리를 지울 수 있습니다. 디스크는 멀쩡한데 암호화된 설치에서 부트
  엔트리가 없으면 죽은 기기와 구분이 안 갑니다. 라이브 USB를 준비해서 `efibootmgr`로 엔트리를
  다시 넣으면 됩니다.
* AC 전원은 필수고 `fwupd`가 강제합니다. `--force`로 넘기지 마세요.

## 요구사항

* ESRT에 시스템 펌웨어 리소스(`fw_type: 1`)가 있는 삼성 Galaxy Book. 저는 Galaxy Book5
  Pro(`NT960XHZ-AD52G` / `960XHA`, AMI Aptio + Intel BIOS Guard)에서만 해 봤습니다.
* `uefi_capsule` 플러그인이 있는 `fwupd`, 그리고 `fwupd-efi`.
* Python 3.9 이상. 표준 라이브러리만 씁니다.
* 삼성 지원 사이트에서 받은 BIOS 업데이터 `.exe`.

`efi_capsule_loader` 커널 모듈은 필요 없습니다. Fedora는 이걸 끄고 빌드해서
(`CONFIG_EFI_CAPSULE_LOADER` 미설정) `/dev/efi_capsule_loader`가 없습니다. `fwupd`는 캡슐을
ESP에 올려두고 부팅할 때 `fwupdx64.efi`에서 `UpdateCapsule()`을 부릅니다.

## 사용법

root로 돌리면 ESRT를 읽어서 값을 알아서 채웁니다.

```console
$ sudo ./samsung-bios-capsule.py ITEM_20260622_22578_WIN_P11AMA.exe -o .
입력: ITEM_20260622_22578_WIN_P11AMA.exe  (15,947,928 bytes)
  PFAT 페이로드: P11AMA.CAP  25,362,432 bytes
  내장 INF: FirmwareId=A51E51F4-...  목표버전=1122
  ESRT: fw_class=A51E51F4-...  현재=920  최저지원=920  capsule_flags=0x50000
  OK    업그레이드: 920 → 1122
```

캡슐을 만들고 검증해서 설치 명령만 찍어 줍니다. `fwupdtool`을 직접 돌리기 전까지 펌웨어
쪽으로는 아무것도 안 갑니다. 헤더 값은 상수로 박지 않고 기기에서 읽습니다.

| 헤더 필드 | 출처 | 폴백 |
|---|---|---|
| `CapsuleGuid` | ESRT `fw_class` | INF `FirmwareId` |
| `Flags` | ESRT `capsule_flags` | `0x50000` |

목표 버전은 INF의 `FirmwareVersion`에서 가져옵니다. 그 INF의 `FirmwareId`가 ESRT
`fw_class`와 다르면 멈춥니다. 다른 기기용 `.exe`라는 뜻이니까요. 목표 버전이 펌웨어의
`lowest_supported_fw_version`보다 낮으면 거부하고, 다운그레이드면 경고만 띄웁니다. root가
아니면 INF 값과 기본 플래그로 넘어가고, `--guid`와 `--flags`로 직접 박을 수도 있습니다.

BIOS가 새로 나오면 그대로 동작할 겁니다. 다른 모델에서는 안 해 봤습니다.

## 원리

펌웨어는 `EFI_CAPSULE_HEADER.CapsuleGuid`로 디스패치합니다. 2-엔트리 화이트리스트와 대조하고
ESRT `fw_class`가 그중 하나라서, 캡슐은 헤더 하나로 끝납니다.

```c
EFI_CAPSULE_HEADER {                       // 0x1C 바이트
  EFI_GUID CapsuleGuid      = <ESRT fw_class>;
  UINT32   HeaderSize       = 0x1C;
  UINT32   Flags            = <ESRT capsule_flags>;   // 관측값 0x50000
  UINT32   CapsuleImageSize = 0x1C + sizeof(PFAT);
}
// 바로 뒤에 PFAT 페이로드
```

FMP 헤더도, `EFI_FIRMWARE_IMAGE_AUTHENTICATION`도 안 붙습니다. 처음에는
`UpdateImageTypeId`를 `6DCBD5ED-E82D-4C44-BDA1-7194199AD92A` 아래에 넣는 표준 FMP 형식으로
만들었는데 안 됐습니다. 근거는 [docs/reverse-engineering.ko.md](docs/reverse-engineering.ko.md)에
정리했습니다. `CapsulePei`와 `BiosGuardPeiApRecoveryCapsule`을 디스어셈블한 결과, 벤더
업데이터가 직접 짠 캡슐 빌더, 내장 WFU INF, 값이 맞아떨어지는 ESRT 실측치입니다.

## 선택: cabinet 아카이브

`install-blob`은 버전 검사와 히스토리를 건너뜁니다. `.cab`으로 묶으면 `fwupdmgr install`이
제대로 기록을 남깁니다.

```console
$ fwupdtool build-cabinet firmware.cab P11AMA_esrt.cap firmware.metainfo.xml
$ fwupdmgr get-details firmware.cab      # 내 기기에 붙는지 확인
$ sudo fwupdmgr install firmware.cab
```

[examples/firmware.metainfo.xml](examples/firmware.metainfo.xml)을 복사해서 버전, 날짜,
GUID만 고치세요. 컴포넌트 `id`는 `org.unofficial.*`로 두세요. reverse-DNS 이름공간은 벤더
겁니다. 이 경로는 `install-blob`보다 덜 써 봤고, 같은 버전을 다시 설치할 때 `fwupd`가
`convert_version not implemented`를 뱉습니다. `.cab`이 안 되면 `install-blob`으로 가세요.

## LVFS에 없는 이유

[LVFS](https://lvfs.readthedocs.io/en/latest/apply.html)는 실리콘 벤더나 ODM, OEM만 펌웨어를
올릴 수 있고, 재배포 법적 권한이 있어야 합니다. `testing`이나 `stable`로 올리려면 벤더 법무팀
문서가 필요하고, GUID도 어차피 삼성 겁니다.

삼성 쪽에서 LVFS 벤더 계정만 열면 됩니다. 어차피 펌웨어는 ESRT 캡슐로 나가고 WFU INF
패키지도 이미 만들고 있으니, 지금 있는 `.cap`에 `metainfo.xml`만 붙이면 됩니다. 이 기기
쓰신다면 삼성에 한번 요청해 주세요.

## 문제 해결

재부팅하면 펌웨어가 결과를 남겨 놓습니다.

```console
$ sudo grep -r . /sys/firmware/efi/esrt/entries/entry0/
```

| `last_attempt_status` | 의미 | 조치 |
|---|---|---|
| `0` | 성공 | `fw_version`이 올라갔는지 확인 |
| `3` | `INCORRECT_VERSION` | 목표 버전과 `lowest_supported_fw_version` 비교 |
| `4` | `INVALID_IMAGE_FORMAT` | 래퍼 거부. FMP 형식으로 시도하거나 이슈 등록 |
| `5` | `AUTH_ERROR` | 서명 검증 실패. 여기서 멈추세요 |
| `6` / `7` | `PWR_EVT_AC` / `PWR_EVT_BATT` | AC 연결, 충전 후 재시도 |

`/sys/class/dmi/id/bios_version`과 `fwupdmgr get-devices`로도 같이 확인하세요.

## 기여

다른 Galaxy Book 모델에서 해 본 결과를 보내 주세요. `fw_class`와 `capsule_flags`, 캡슐이
먹혔는지가 제일 도움이 됩니다. 이슈에 펌웨어 바이너리는 올리지 마세요.

## 라이선스

[Apache-2.0](LICENSE)이 도구와 문서에 적용됩니다. 삼성 펌웨어는 대상이 아니고 여기서 배포하지
않습니다.
