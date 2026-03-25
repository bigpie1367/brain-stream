# BrainStream

ListenBrainz 추천을 기반으로 음악을 자동 수집하고 개인 스트리밍 서버(Navidrome)에 추가하는 파이프라인.

---

## 배포

### 사전 준비

- [ListenBrainz](https://listenbrainz.org) 계정 및 API 토큰
- [Navidrome](https://www.navidrome.org) 서버 (이미 운영 중이거나, 이 컴포즈에서 함께 실행 가능)
- Docker, Docker Compose

### 서버 배포 (권장)

`docker-compose.prod.yml`만 서버에 복사해 사용합니다. 이미지는 GHCR에서 자동으로 받아옵니다.

```bash
# 1. docker-compose.prod.yml 다운로드
curl -O https://raw.githubusercontent.com/bigpie1367/brain-stream/main/docker-compose.prod.yml

# 2. 환경변수 설정
cat > .env << 'EOF'
LB_USERNAME=your_listenbrainz_username
LB_TOKEN=your_listenbrainz_token
NAVIDROME_USER=admin
NAVIDROME_PASSWORD=your_navidrome_password
MUSIC_DIR=/path/to/your/music
EOF

# 3. 실행
docker compose -f docker-compose.prod.yml up -d
```

접속:
- **Web UI**: `http://server-ip:8080`
- **Navidrome**: `http://server-ip:8080/navidrome/`

### 로컬 개발

```bash
git clone https://github.com/bigpie1367/brain-stream.git
cd brain-stream

cp .env.example .env
# .env에서 LB_TOKEN, NAVIDROME_PASSWORD 입력

docker compose -f docker-compose.local.yml up --build -d
```

### 환경변수

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `LB_USERNAME` | ListenBrainz 사용자명 | 필수 |
| `LB_TOKEN` | ListenBrainz API 토큰 | 필수 |
| `NAVIDROME_PASSWORD` | Navidrome 관리자 비밀번호 | 필수 |
| `NAVIDROME_USER` | Navidrome 관리자 계정명 | `admin` |
| `NAVIDROME_URL` | Navidrome 내부 URL | `http://navidrome:4533/navidrome` |
| `MUSIC_DIR` | 호스트 음악 디렉토리 경로 (prod 전용) | 필수 |

---

## 소개

### 이 프로젝트가 연결하는 서비스들

BrainStream은 여러 공개 서비스를 하나의 파이프라인으로 엮습니다. 각각이 어떤 역할을 하는지 알면 전체 흐름이 쉽게 이해됩니다.

#### ListenBrainz

[ListenBrainz](https://listenbrainz.org)는 MetaBrainz 재단이 운영하는 오픈소스 음악 청취 기록 서비스입니다. Last.fm과 유사하게 재생 이력을 기록하지만, 데이터가 완전히 공개되고 API도 자유롭게 사용할 수 있습니다.

BrainStream은 ListenBrainz의 **협업 필터링(CF) 추천** 기능을 활용합니다. 나와 비슷한 청취 패턴을 가진 사용자들이 즐겨 듣는 트랙 중 내 라이브러리에 없는 것을 추천해줍니다. 이 추천 목록이 BrainStream 파이프라인의 시작점입니다.

#### MusicBrainz

[MusicBrainz](https://musicbrainz.org)는 위키피디아처럼 커뮤니티가 편집하는 오픈 음악 메타데이터 데이터베이스입니다. 아티스트, 앨범, 트랙, 발매 연도, 레이블 등 방대한 정보를 담고 있으며, 각 항목에 고유 ID(MBID)를 부여합니다.

BrainStream은 MusicBrainz를 통해 아티스트명·앨범명·트랙명의 **공식 표기**를 확정합니다. YouTube에서 받은 파일명이나 검색 키워드가 아닌, MusicBrainz 기준의 정규화된 메타데이터를 태그에 씁니다. 또한 커버아트는 MusicBrainz의 자매 서비스인 **Cover Art Archive**에서 가져옵니다.

#### Navidrome

[Navidrome](https://www.navidrome.org)은 자체 서버에서 운영하는 오픈소스 음악 스트리밍 서버입니다. Spotify처럼 브라우저나 모바일 앱으로 음악을 스트리밍할 수 있습니다. **Subsonic API**를 지원하기 때문에 Amperfy, DSub, Symfonium 등 다양한 서드파티 앱에서도 접속할 수 있습니다.

BrainStream은 음악 파일을 라이브러리에 추가한 뒤 Navidrome의 스캔 API를 호출해 즉시 라이브러리에 반영합니다. 별도로 Navidrome 관리 화면에서 수동 스캔을 실행할 필요가 없습니다.

![Navidrome 앨범 라이브러리](<docs/screenshots/navidrome.JPG>)

#### 그 외 메타데이터 소스

| 서비스 | 역할 |
|--------|------|
| **iTunes Search API** | 앨범명·커버아트 조회 (1순위). 인증 불필요, 한/영 스토어 모두 지원 |
| **Deezer API** | iTunes 실패 시 앨범명·커버아트 폴백 (2순위). 인증 불필요 |
| **Cover Art Archive** | MusicBrainz 연동 고해상도 앨범 커버아트 |
| **YouTube (yt-dlp)** | 오디오 다운로드 소스. 채널명은 앨범명 최후 수단으로도 활용 |

---

### 자동 파이프라인

컨테이너 시작 시 즉시 1회, 이후 **설정된 주기**(기본 6시간, 1~24시간 범위)마다 자동으로 실행됩니다. Web UI 또는 API(`PUT /api/settings/pipeline-interval`)로 재시작 없이 변경 가능합니다.

```
ListenBrainz CF 추천(80%) + LB Radio(탑 아티스트 시드, 20%) 수신
  → MusicBrainz에서 아티스트·트랙 메타데이터 조회 (4단계 폴백 검색)
  → YouTube에서 "official audio" 키워드로 오디오 검색 및 다운로드 (FLAC 우선, Opus fallback)
     라이브·커버 영상 자동 필터링 (strict 모드), 차단 영상 감지 시 다음 후보로 retry
     5개 후보 소진 시 ytsearch1 폴백
  → iTunes / Deezer / MusicBrainz / Cover Art Archive로 앨범·커버아트 자동 매칭
  → 메타데이터 태그 삽입 후 라이브러리 경로로 이동
     data/music/{아티스트}/{앨범}/{트랙명}.flac
  → Navidrome 라이브러리 스캔 자동 트리거
```

이미 처리된 트랙(MusicBrainz ID 기준)은 건너뜁니다. 실패한 트랙은 최대 3회까지 자동 재시도합니다.

---

### Web UI

`http://server-ip:8080`에서 접근 가능한 다크 테마 단일 페이지 앱입니다.

![BrainStream 대시보드](<docs/screenshots/dashboard.JPG>)

#### 수동 다운로드

추천 파이프라인과 별개로, 아티스트명과 트랙명을 직접 입력해 즉시 다운로드할 수 있습니다. 다운로드 → 태깅 → 스캔 각 단계의 진행 상황이 실시간으로 업데이트됩니다.

- **Auto 모드**: YouTube에서 최적 후보를 자동 선택해 바로 다운로드. 라이브·커버 영상을 자동으로 걸러냅니다.

  ![Auto 다운로드](<docs/screenshots/auto download.gif>)

- **Pick 모드**: YouTube 후보 5개를 썸네일·채널명·재생시간·Live/Cover 배지와 함께 카드 형태로 표시. 원하는 영상을 직접 선택해 다운로드합니다.

  ![Pick 모드](<docs/screenshots/pick download.gif>)

#### 다운로드 이력

다운로드 이력을 페이지네이션(무한 스크롤)으로 확인할 수 있습니다. 아티스트, 트랙명, 앨범명, 출처(ListenBrainz / Manual), 상태, ListenBrainz 링크, 다운로드 시각이 표시되며, 검색 기능으로 아티스트/트랙/앨범명 필터링이 가능합니다.

완료된 트랙은 ▶ 버튼으로 브라우저에서 바로 재생할 수 있고, ✏ 버튼으로 메타데이터(아티스트/앨범/트랙명)를 직접 편집할 수 있습니다. 일괄 삭제 모드로 여러 트랙을 한 번에 삭제할 수도 있습니다.

#### 라이브러리 탭

Navidrome 라이브러리를 아티스트 → 앨범 → 트랙 순서로 브라우징할 수 있습니다. Navidrome 관리 화면을 열지 않고도 보유 음악 전체를 한눈에 확인할 수 있습니다.

#### Rematch (앨범 재매칭)

자동 매칭 결과가 올바르지 않을 때, 원하는 앨범을 직접 선택해 메타데이터를 재적용할 수 있습니다.

1. 라이브러리 탭에서 트랙의 **Rematch** 클릭
2. MusicBrainz에서 앨범 후보 검색 (커버아트 미리보기 포함)
3. 원하는 앨범 선택 → 커버아트 포함 메타데이터 재태깅 + Navidrome 재스캔 자동 실행

![Rematch 모달](<docs/screenshots/rematch modal.png>)

---

## 아키텍처

```
인터넷 클라이언트 (Amperfy 등 Subsonic 앱, 브라우저)
        │
   [nginx 리버스 프록시] (옵션)
        │
        ▼
  brainstream :8080
  ├─ Web UI + REST API + SSE
  ├─ /rest/* → Navidrome Subsonic API 프록시
  └─ Pipeline Core
       ├─ ListenBrainz → MusicBrainz → yt-dlp
       ├─ mutagen (태깅) + shutil (파일 이동)
       └─ Navidrome startScan

  navidrome :4533 (Docker 내부 전용, 외부 직접 노출 없음)

External APIs:
  ListenBrainz, MusicBrainz, Cover Art Archive, iTunes, Deezer, YouTube
```

외부 Subsonic 앱(Amperfy, DSub 등)에서 접속할 때는 `/rest/*` 경로를 그대로 사용하면 brainstream이 Navidrome으로 프록시해줍니다. Navidrome 포트를 외부에 직접 노출할 필요가 없습니다.

---

## 주요 명령어

```bash
# 로그 확인
docker compose logs -f brainstream

# LB 파이프라인 수동 실행
curl -X POST http://localhost:8080/api/pipeline/run

# 다운로드 이력 확인
curl http://localhost:8080/api/downloads | python3 -m json.tool

# SQLite 상태 직접 조회
docker compose exec brainstream sqlite3 /app/db/state.db \
  "SELECT track_name, artist, status FROM downloads ORDER BY rowid DESC LIMIT 20;"
```

---

## 기술 스택

| 구성 요소 | 사용 기술 |
|-----------|-----------|
| 백엔드 | Python 3.12, FastAPI, uvicorn |
| 다운로드 | yt-dlp |
| 오디오 태깅 | mutagen (FLAC, Opus, MP4) |
| 스트리밍 서버 | Navidrome |
| 상태 저장 | SQLite |
| 메타데이터 API | MusicBrainz, Cover Art Archive, iTunes, Deezer |
| 컨테이너 | Docker, Docker Compose |
