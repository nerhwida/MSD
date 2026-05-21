# Media Segment Downloader

Flask 기반의 미디어 세그먼트 다운로더입니다. 번호가 붙은 세그먼트 URL 템플릿 또는 m3u8 URL을 입력하면 `.ts` 파일을 작업별 폴더에 저장하고, 다운로드 완료 후 `ffmpeg`로 MP4를 만들 수 있는 명령문을 생성합니다.

## Features

- 번호 템플릿 다운로드
  - 예: `https://example.com/segment-{}.ts`
  - 예: `https://example.com/segment-{:06d}.ts`
- m3u8 playlist 파싱
  - media playlist 지원
  - master playlist의 하위 playlist 자동 탐색
- 여러 작업 동시 실행
- 작업별 실시간 로그와 진행률 표시
- 중단 후 자동 이어받기
  - 작업 폴더의 `job.json`을 기준으로 같은 작업을 찾습니다.
  - 이미 존재하고 크기가 0보다 큰 `indexN.ts` 파일은 건너뜁니다.
- AES-128 암호화 자동 복호화
  - `#EXT-X-KEY:METHOD=AES-128` 태그를 감지하면 자동으로 복호화하여 저장합니다.
  - `pycryptodome` 패키지가 필요합니다.
- 다운로드 완료 후 `ffmpeg` 명령문 생성
  - 서버에서 ffmpeg를 직접 실행하지 않습니다.
  - 생성된 명령문을 복사해서 콘솔에서 직접 실행합니다.

## Requirements

- Python 3.10+
- Flask
- requests
- pycryptodome
  - AES-128 암호화된 세그먼트 자동 복호화에 필요합니다.
- wasmtime (선택)
  - level7.wasm 기반 특수 복호화가 필요한 경우에만 사용합니다.
- ffmpeg
  - MP4 변환 명령을 실행하려면 별도로 설치되어 있어야 합니다.

## Installation

```bash
pip install flask requests pycryptodome
```

wasmtime이 필요한 경우:

```bash
pip install wasmtime
```

ffmpeg는 운영체제에 맞게 설치하고, 터미널에서 아래 명령이 동작하는지 확인하세요.

```bash
ffmpeg -version
```

## Run

```bash
python app.py
```

브라우저에서 접속합니다.

```text
http://127.0.0.1:5000
```

## Usage

### 1. 번호 템플릿 모드

1. `번호 템플릿` 탭을 선택합니다.
2. URL 템플릿을 입력합니다.
3. 시작 번호와 끝 번호를 입력합니다.
4. 저장 폴더를 입력합니다.
5. 필요한 경우 `User-Agent`, `Referer`를 수정합니다.
6. `다운로드 시작`을 누릅니다.

예:

```text
URL 템플릿: https://example.com/video/index{}.ts
시작 번호: 1
끝 번호: 100
저장 폴더: ./downloads
```

### 2. m3u8 모드

1. `m3u8` 탭을 선택합니다.
2. m3u8 URL을 입력합니다.
3. 저장 폴더를 입력합니다.
4. 필요한 경우 `User-Agent`, `Referer`를 수정합니다.
5. `다운로드 시작`을 누릅니다.

앱은 m3u8 파일을 파싱해서 세그먼트 목록을 만들고, 각 세그먼트를 `index1.ts`, `index2.ts` 형식으로 저장합니다.

## Output Structure

기본 저장 폴더가 `./downloads`라면 작업별로 다음처럼 저장됩니다.

```text
downloads/
  <job_id>/
    job.json
    playlist.m3u8
    playlist_1.m3u8
    index1.ts
    index2.ts
    index3.ts
    file_list.txt
    ffmpeg.txt
```

파일 설명:

- `job.json`: 자동 이어받기용 작업 메타데이터
- `playlist.m3u8`: 입력한 m3u8 원본
- `playlist_1.m3u8`: master playlist가 참조한 하위 playlist
- `indexN.ts`: 다운로드된 세그먼트 파일 (AES-128인 경우 복호화된 상태로 저장)
- `file_list.txt`: ffmpeg concat 입력 파일
- `ffmpeg.txt`: ffmpeg 실행 명령어

번호 템플릿 모드에서는 m3u8 파일이 생성되지 않습니다.

## Resume Downloads

같은 저장 폴더와 같은 요청을 다시 입력하면 기존 작업 폴더를 자동으로 찾습니다.

예를 들어 첫 작업이 아래처럼 저장되었다면:

```text
downloads/
  c4f95e0a/
    job.json
    index1.ts
    index2.ts
```

다음 실행 때 저장 폴더를 그대로 `./downloads`로 입력해도 `job.json`을 기준으로 `c4f95e0a` 폴더를 재사용합니다.

이미 존재하는 파일은 로그에 `(건너뜀)`으로 표시됩니다.

```text
[1/100] index1.ts (건너뜀)
[2/100] index2.ts (건너뜀)
```

주의:

- `job.json`이 없는 폴더는 자동 이어받기 대상으로 인식하지 않습니다.
- 크기가 0보다 큰 파일은 정상 파일로 보고 건너뜁니다.
- 크기가 0인 파일은 다시 다운로드합니다.

## Convert To MP4

다운로드가 완료되면 화면에 ffmpeg 명령문이 표시됩니다.

예:

```bash
ffmpeg -y -f concat -safe 0 -i "D:\path\downloads\c4f95e0a\file_list.txt" -c:v libx264 -c:a aac -movflags +faststart "D:\path\downloads\c4f95e0a\output.mp4"
```

명령문을 복사해서 터미널에서 직접 실행하면 MP4 파일이 생성됩니다.

## Notes

- 이 도구는 개인적인 다운로드 보조 도구입니다.
- 접근 권한이 없는 콘텐츠 또는 저작권을 침해하는 콘텐츠 다운로드에 사용하지 마세요.
- 다운로드된 `.ts`, `.mp4`, 작업 폴더는 크기가 커질 수 있으므로 Git에 커밋하지 않는 것을 권장합니다.
