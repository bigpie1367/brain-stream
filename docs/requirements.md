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
| 설명 | `ytsearch5:{artist} {track} official audio` 쿼리로 YouTube 5개 후보를 수집하고 최적 결과를 다운로드한다 |
| 후보 선택 | 라이브/커버 영상 패널티, 공식 채널(VEVO 등) 보너스 점수 기반 최적 후보 선택 |
| 차단 영상 처리 | 결제/비공개/멤버십/접근불가 감지 → 다음 후보 retry. 5개 소진 시 `ytsearch1:{artist} {track} official audio` 폴백 |
| 포맷 우선순위 | FLAC 우선, 실패 시 Opus fallback |
| 출력 위치 | staging 디렉토리 (`{mbid}.{ext}`) |
| 우선순위 | 필수 |

### FR-03. 자동 태깅 및 라이브러리 임포트

| 항목 | 내용 |
|------|------|
| ID | FR-03 |
| 설명 | MB API 3단계 검색으로 recording 매칭 후 mutagen으로 직접 태깅, shutil로 최종 경로에 복사 |
| MB 검색 단계 | stage 1 (strict): artistname+recording+Album+Official / stage 2 (plain): artistname+recording / stage 3 (fallback): recording만, artist 유사도 0.3 이상 |
| 태그 쓰기 | mutagen: artist, title, mb_trackid 초기 태그 → _enrich_track()으로 album 태그 + 커버아트 임베딩 |
| 파일 복사 | `shutil.copy2`: staging → `data/music/{Artist}/{Album}/{Track}.ext` |
| 우선순위 | 필수 |

### FR-04. 앨범 정보 enrichment

| 항목 | 내용 |
|------|------|
| ID | FR-04 |
| 설명 | staging 파일에서 직접 앨범명/커버아트를 결정하여 mutagen으로 임베딩 |
| 앨범명 결정 순서 | 1. iTunes Search API (artist 유사도 0.4 이상) → 2. Deezer API → 3. MB `/recording/{id}?inc=releases+release-groups` → 4. YouTube 채널명 → 5. "Unknown Album" |
| 커버아트 결정 순서 | 1. Cover Art Archive (mb_albumid_candidates 최대 3개 시도) → 2. iTunes artwork URL → 3. Deezer artwork URL → 4. YouTube 썸네일 |
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

- 여러 manual 다운로드 잡이 동시에 실행될 수 있어야 함. 각 잡은 고유한 mbid 기반 파일명을 사용하므로 별도 lock 불필요
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
- config 파일 없이 환경변수만으로 동작 (LB_USERNAME, LB_TOKEN, NAVIDROME_USER, NAVIDROME_PASSWORD)
- Python 소스 변경 시 재빌드 후 재시작만으로 적용 가능

### NFR-05. API Rate Limit 준수

- MusicBrainz API: 각 호출 전 1초 대기
- 에러 발생 시 즉시 재시도하지 않음

---

## 5. 제약사항

| ID | 제약 | 이유 |
|----|------|------|
| CON-01 | mb_albumid를 파일 태그에 기록 금지 | 트랙마다 다른 release ID → Navidrome 앨범 분리 현상 |
| CON-02 | iTunes/Deezer artist 유사도 임계값 0.4 이상만 허용 | 동명 아티스트 오매칭 방지 |
| CON-03 | MB 검색 폴백 3단계: recording-only 단계에서 artist 유사도 0.3 미만 시 실패 처리 | 관련 없는 아티스트 recording 매칭 방지 |
| CON-04 | MusicBrainz API: 각 호출 전 1초 대기 | Rate limit 1 req/sec 준수 |
| CON-05 | Linux 파일시스템 대소문자 구분으로 인한 폴더 충돌 방지 | `_resolve_dir`로 대소문자 무시 기존 폴더 재사용 |

---

## 6. 외부 시스템 의존성

| 시스템 | 용도 | 비고 |
|--------|------|------|
| ListenBrainz API | CF 추천 트랙 조회 | 인증 Token 필요 |
| YouTube (yt-dlp) | 음원 다운로드 | 검색어: `ytsearch5:{artist} {track} official audio` |
| MusicBrainz API | recording → 앨범 정보 조회 | Rate limit 1 req/sec |
| Cover Art Archive | 앨범아트 다운로드 | MusicBrainz release ID 필요 |
| iTunes Search API | 앨범명/커버아트 조회 | 인증 불필요. `country` 파라미터로 US/KR 스토어 선택 |
| Deezer API | 앨범명/커버아트 폴백 조회 | 인증 불필요 |
| Navidrome | 음악 스트리밍 서버 | Subsonic API v1.16.1 |
| mutagen | 오디오 파일 태그 읽기/쓰기 | FLAC, OGG/Opus, MP4/M4A 지원 |
