# 회의 녹음기 (meetrec)

마이크 + 시스템 소리(상대방 목소리)를 한 파일에 담고, 원하면 화면도 같이 녹화하는 GTK4 앱.

## 실행

```bash
meetrec            # ~/.local/bin/meetrec
```
GNOME 앱 목록에 "회의 녹음기"로 등록돼 있음. Dock에 고정해서 클릭 → 창 → 큰 버튼 클릭으로 시작/중지.

## 저장

- 기본 경로: `~/Videos/Meetings/`
- 화면 포함 → `.mp4` / 소리만 → `.m4a`
- 녹화 중에는 `.partial.mkv`로 쓰고, 중지할 때 검증 후 재인코딩 없이 remux.
  MP4로 바로 쓰면 강제 종료 시 moov atom이 없어 파일 전체를 잃는데, MKV는 잘려도 앞부분이 살아남는다.

## 설계상 중요한 것

| 항목 | 이유 |
|---|---|
| ffmpeg를 `systemd-run --user` transient **service**로 실행 | GUI가 죽어도 녹화가 계속됨. 앱을 다시 켜면 진행 중인 녹화에 재연결 |
| 중지 = `systemctl --user stop` (`KillSignal=SIGINT`) | systemd가 정상 종료를 소유. 90초 안에 안 끝나야 SIGKILL |
| 장치는 **이름 대신 하드웨어 지문**(serial → bus_path → vendor/product)으로 저장 | PipeWire가 재연결·프로필 변경마다 노드 이름을 바꾼다. 실측: 같은 G733이 `mono-fallback` → `.2` → `.3`, 출력은 `iec958-stereo` → `analog-stereo` |
| 시스템 소리는 sink의 `monitor_source`를 조회 | 이름 뒤에 `.monitor`를 붙여 만들면 깨진다 |
| `amix=...:normalize=0` + `alimiter` | normalize를 빼면 각 소스가 절반 볼륨. 대신 마이크 부스트와 합쳐질 때 clipping이 생겨 리미터로 방어 |
| 모니터별 캡처는 `h264_nvenc`, 전체 화면은 `hevc_nvenc` | h264 NVENC는 폭 4096 초과를 거부한다("No capable devices found"). 이 머신 전체 캔버스는 5526×2520이라 HEVC로만 가능 |
| 녹화 대상에 **선택 영역**(드래그로 사각형 지정) | GNOME 기본 녹화기와 달리 `-draw_mouse 1`이라 커서가 찍힌다. GNOME 내장 녹화는 X11에서 커서 위치 갱신이 Wayland 전용 코드에 막혀 있어([mutter #3182](https://gitlab.gnome.org/GNOME/mutter/-/issues/3182)) 커서가 안 나온다 |
| 영역은 짝수로 스냅 + 루트 윈도우 안으로 클램프 + 최소 146×50 | yuv420p는 홀수 크기를 못 쓰고, 화면 밖을 잡으면 x11grab이 첫 프레임에서 죽는다. 최소값은 실측: `144x300`·`200x48`은 h264_nvenc가 "Frame Dimension less than the minimum supported value"로 거부, `146x146`·`200x50`은 통과 |
| 레벨 미터는 녹화 중인 ffmpeg의 `astats`를 재사용 | 미터 전용 프로세스를 따로 돌리지 않음. 브랜치마다 `ametadata=...:file=`로 tmpfs에 10Hz 기록(`asetnsamples=4800`), GUI는 마지막 줄만 tail |
| 미터 행은 장치 행과 분리 | 녹화 중에는 장치 콤보를 비활성화하는데, 미터가 그 안에 있으면 정작 봐야 할 표시가 같이 회색으로 죽는다 |
| 마이크가 8초 이상 무음이면 경고 | 음소거된 헤드셋은 정상 동작과 화면상 구분이 안 된다 |
| 유휴 시 타이머 없음 | 녹화 중에만 500ms 폴링. 유휴 CPU 0% |

## 파일

```
meetrec/core.py   장치 탐색, RecordingPlan, 서비스 제어, 검증·remux  (GUI 의존성 없음 → 셸에서 테스트 가능)
meetrec/app.py    GTK4/libadwaita UI
meetrec/region.py 영역 선택 오버레이(모니터마다 전체화면 dim 창, 드래그 → 루트 좌표 사각형)
bin/meetrec       런처
icons/            앱 아이콘(마이크 + 레코드 닷)
```

설정은 `~/.config/meetrec/config.json`, 진행 중 세션은 `~/.local/state/meetrec/current.json`.

## 진단

```bash
systemctl --user status meetrec-recording.service
journalctl --user -u meetrec-recording.service -n 50 -o cat
```
