# 요구사항 정의서

- **프로젝트명**: music-bot
- **버전**: 1.0.0
- **작성일**: 2026-03-04
- **상태**: 구현 완료

---

## 1. 프로젝트 개요

### 1.1 배경

개인 음악 서버(Navidrome)를 운영하는 사용자가 ListenBrainz의 협업 필터링 추천을 기반으로 음악을 자동으로 수집하고, 메타데이터를 정규화하여 스트리밍 라이브러리에 추가하고자 하는 필요에서 출발.

### 1.2 목표

- ListenBrainz 추천 트랙을 자동으로 발견하고 다운로드
- MusicBrainz 기준 메타데이터(앨범명, 아티스트, 앨범아트)로 자동 정규화
- Navidrome 라이브러리에 실시간 반영
- 수동 다운로드 및 진행 상황 실시간 확인 Web UI 제공

### 1.3 범위

- **포함**: 추천 수집, YouTube 다운로드, 오디오 태깅, 라이브러리 스캔, Web UI
- **제외**: 음악 재생 기능 (Navidrome이 담당), 저작권 관리, 플랫폼 배포

---

## 2. 이해관계자

| 역할 | 설명 |
|------|------|
| 개인 사용자 | 자신의 Navidrome 서버에서 음악을 스트리밍하는 개인 |
| 시스템 자동화 | 스케줄러가 주기적으로 파이프라인 실행 |

---

## 3. 기능 요구사항 (Functional Requirements)

### FR-01. ListenBrainz 추천 자동 수집

| 항목 | 내용 |
|------|------|
| ID | FR-01 |
| 설명 | 설정된 ListenBrainz 계정의 CF 추천 트랙 목록을 주기적으로 가져온다 |
| 입력 | username, token, count |
| 출력 | mbid, artist, track_name 목록 |
| 조건 | 이미 처리된 트랙(mbid 기준)은 건너뜀 |
| 우선순위 | 필수 |

### FR-02. YouTube 자동 다운로드

| 항목 | 내용 |
|------|------|
| ID | FR-02 |
| 설명 | `ytsearch1:{artist} {track}` 쿼리로 YouTube 첫 번째 결과를 다운로드한다 |
| 포맷 우선순위 | FLAC 우선, 실패 시 Opus fallback |
| 출력 위치 | staging 디렉토리 (`{mbid}.{ext}`) |
| 우선순위 | 필수 |

### FR-03. beets 자동 태깅 및 라이브러리 임포트

| 항목 | 내용 |
|------|------|
| ID | FR-03 |
| 설명 | beets singleton 임포트로 MusicBrainz 기준 메타데이터 자동 매칭 및 음악 라이브러리에 복사 |
| 전처리 | mutagen으로 artist/title 선-태깅 (MusicBrainz 매칭 정확도 향상) |
| skip 감지 | import 로그 offset 비교로 beets skip 여부 판단 (exit code가 항상 0이므로) |
| duplicate 처리 | 이미 라이브러리에 있는 경우 성공으로 처리 |
| 우선순위 | 필수 |

### FR-04. 앨범 정보 enrichment

| 항목 | 내용 |
|------|------|
| ID | FR-04 |
| 설명 | beets singleton 임포트 후 앨범명/앨범아트가 없는 경우 MusicBrainz API로 보완 |
| 앨범명 | `/ws/2/recording/{mb_trackid}?inc=releases+release-groups` → Official Album 우선 |
| 앨범아트 | Cover Art Archive에서 직접 다운로드 → mutagen으로 파일에 임베딩 |
| 제약 | mb_albumid는 파일 태그에 기록하지 않음 (Navidrome 앨범 분리 방지) |
| 우선순위 | 필수 |

### FR-05. Navidrome 라이브러리 스캔 트리거

| 항목 | 내용 |
|------|------|
| ID | FR-05 |
| 설명 | 새 트랙 임포트 후 Navidrome Subsonic API로 스캔을 요청하고 완료를 기다린다 |
| 인증 | MD5(password + salt) token-auth |
| 타임아웃 | 300초 |
| 우선순위 | 필수 |

### FR-06. 주기적 파이프라인 스케줄링

| 항목 | 내용 |
|------|------|
| ID | FR-06 |
| 설명 | 설정된 시간 간격(기본 6시간)으로 파이프라인을 자동 실행한다 |
| 시작 | 컨테이너 기동 시 즉시 1회 실행 후 스케줄 등록 |
| 재시도 | 실패한 트랙은 최대 3회(attempts < 3)까지 재시도 |
| 우선순위 | 필수 |

### FR-07. 수동 다운로드 Web UI

| 항목 | 내용 |
|------|------|
| ID | FR-07 |
| 설명 | 사용자가 artist + track_name을 직접 입력해 즉시 다운로드할 수 있는 Web UI 제공 |
| 실시간 상태 | SSE(Server-Sent Events)로 downloading → tagging → scanning → done/failed 단계별 표시 |
| 우선순위 | 필수 |

### FR-08. 다운로드 이력 조회

| 항목 | 내용 |
|------|------|
| ID | FR-08 |
| 설명 | 전체 다운로드 이력(최신 100건)을 Web UI 및 API로 조회 가능 |
| 표시 항목 | mbid, artist, track_name, status, source, attempts, downloaded_at, error_msg |
| 우선순위 | 필수 |

---

## 4. 비기능 요구사항 (Non-Functional Requirements)

### NFR-01. 동시성 안전성

- beet import는 threading.Lock으로 직렬화 (import 로그 오염 방지)
- 여러 manual 다운로드 잡이 동시에 실행될 수 있어야 함
- duplicate-skip 발생 시에도 enrichment 수행

### NFR-02. 장애 격리

- 개별 트랙 다운로드 실패가 전체 파이프라인을 중단시키지 않아야 함
- 각 단계(다운로드/태깅/스캔) 실패 시 상태 DB에 에러 기록 후 다음 트랙 처리

### NFR-03. 운영 투명성

- structlog 기반 구조화 로그 (TTY: 콘솔 렌더러, non-TTY: JSON)
- 모든 파이프라인 단계별 로그 기록
- Web UI에서 실시간 진행 상황 확인 가능

### NFR-04. 배포 단순성

- Docker Compose 단일 명령으로 전체 스택 실행
- beets 설정은 볼륨 마운트로 재빌드 없이 변경 가능
- 환경변수로 민감 정보 오버라이드 가능 (LB_USERNAME, LB_TOKEN, NAVIDROME_USER, NAVIDROME_PASSWORD)

### NFR-05. API Rate Limit 준수

- MusicBrainz API: 각 호출 전 1초 대기
- 에러 발생 시 즉시 재시도하지 않음

---

## 5. 제약사항

| ID | 제약 | 이유 |
|----|------|------|
| CON-01 | beets는 pip으로 설치 (apt 금지) | apt beets는 시스템 Python → pip 패키지 접근 불가 |
| CON-02 | beets 2.x: musicbrainz를 plugins에 명시 필수 | beets 2.x에서 플러그인으로 분리됨 |
| CON-03 | beet import는 반드시 `-s` (singleton) 플래그 사용 | 앨범 모드는 단일 파일 skip 처리 |
| CON-04 | mb_albumid를 파일 태그에 기록 금지 | 트랙마다 다른 release ID → Navidrome 앨범 분리 현상 |
| CON-05 | strong_rec_thresh ≥ 0.15 | 0.04 이하면 정상 매치(88.9%)도 거부됨 |

---

## 6. 외부 시스템 의존성

| 시스템 | 용도 | 비고 |
|--------|------|------|
| ListenBrainz API | CF 추천 트랙 조회 | 인증 Token 필요 |
| YouTube (yt-dlp) | 음원 다운로드 | 검색어: `ytsearch1:{artist} {track}` |
| MusicBrainz API | recording → 앨범 정보 조회 | Rate limit 1 req/sec |
| Cover Art Archive | 앨범아트 다운로드 | MusicBrainz release ID 필요 |
| Navidrome | 음악 스트리밍 서버 | Subsonic API v1.16.1 |
| beets | 오디오 메타데이터 태거 | pip 설치 필수 (v2.x) |
