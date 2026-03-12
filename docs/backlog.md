# 프로젝트 백로그

- **작성일**: 2026-03-04
- **현재 버전**: 1.0.1

---

## 완료된 기능 (Done)

### Epic 1: 자동 파이프라인 구축

| ID | User Story | 완료일 |
|----|-----------|--------|
| US-01 | ListenBrainz CF 추천 API로 트랙 목록을 가져올 수 있다 | 2026-03-03 |
| US-02 | YouTube에서 트랙을 자동으로 검색하고 FLAC/Opus로 다운로드할 수 있다 | 2026-03-03 |
| US-03 | MB API 직접 검색 + mutagen으로 MusicBrainz 기준 메타데이터를 자동 태깅할 수 있다 | 2026-03-03 |
| US-04 | 이미 처리된 트랙은 mbid 기준으로 중복 처리를 건너뛴다 | 2026-03-03 |
| US-05 | 실패한 트랙은 최대 3회까지 자동 재시도된다 | 2026-03-03 |
| US-06 | 설정된 시간 간격(6시간)으로 파이프라인이 자동 실행된다 | 2026-03-03 |

### Epic 2: 메타데이터 enrichment

| ID | User Story | 완료일 |
|----|-----------|--------|
| US-07 | iTunes/Deezer/MB API로 공식 앨범명을 자동으로 조회하여 태그에 반영한다 | 2026-03-03 |
| US-08 | Cover Art Archive에서 앨범아트를 다운로드하여 파일에 직접 임베딩한다 | 2026-03-03 |
| US-09 | 같은 앨범의 트랙들이 Navidrome에서 하나의 앨범으로 표시된다 (mb_albumid 미기록) | 2026-03-03 |

### Epic 3: Web UI 및 수동 다운로드

| ID | User Story | 완료일 |
|----|-----------|--------|
| US-10 | Web UI에서 artist + track 입력으로 즉시 다운로드를 시작할 수 있다 | 2026-03-03 |
| US-11 | SSE로 downloading → tagging → scanning → done/failed 단계별 실시간 상태를 확인할 수 있다 | 2026-03-03 |
| US-12 | 전체 다운로드 이력(LB + 수동)을 Web UI에서 확인할 수 있다 | 2026-03-03 |
| US-13 | LB 파이프라인을 Web UI에서 수동으로 즉시 실행할 수 있다 | 2026-03-03 |

### Epic 4: Navidrome 연동

| ID | User Story | 완료일 |
|----|-----------|--------|
| US-14 | 새 트랙 임포트 후 Navidrome 라이브러리 스캔이 자동으로 트리거된다 | 2026-03-03 |
| US-15 | 스캔 완료를 폴링으로 확인한 후 다음 단계로 진행한다 | 2026-03-03 |

### Epic 5: 안정성 및 운영

| ID | User Story | 완료일 |
|----|-----------|--------|
| US-16 | 여러 다운로드 잡이 동시에 실행되어도 충돌 없이 처리된다 (mbid 기반 고유 파일명, lock 불필요) | 2026-03-03 |
| US-17 | 개별 트랙 실패가 전체 파이프라인을 중단시키지 않는다 | 2026-03-03 |
| US-18 | 구조화 로그(structlog)로 파이프라인 각 단계를 추적할 수 있다 | 2026-03-03 |
| US-19 | Docker Compose 단일 명령으로 전체 스택을 실행할 수 있다 | 2026-03-03 |

### Epic 9: beets 의존성 제거 — MB API 직접 매칭으로 전환 (2026-03-10)

| ID | User Story | 완료일 |
|----|-----------|--------|
| US-37 | beet import 제거 — `_mb_search_recording` (artist 유사도 검증 포함)으로 직접 MB recording 검색 | 2026-03-10 |
| US-38 | shutil.copy2로 파일 복사, 경로 sanitize (특수문자 제거), `data/music/{Artist}/{Album}/{Track}` 구조 생성 | 2026-03-10 |
| US-39 | beet modify 제거 — mutagen 직접 태그 쓰기 (album, mb_trackid 등) | 2026-03-10 |
| US-40 | beet remove 제거 — `os.remove` + state.db file_path 조회로 대체 | 2026-03-10 |
| US-41 | beet list 제거 — state.db에 `file_path TEXT` 컬럼 추가, mbid로 파일 경로 직접 조회 | 2026-03-10 |
| US-42 | import log / `_beet_lock` 직렬화 로직 제거 | 2026-03-10 |
| US-43 | beets 관련 의존성 제거 (requirements.txt, Dockerfile, beets/config.yaml) | 2026-03-10 |

### Epic 11: MB 매칭 정확도 및 태그 품질 개선 (2026-03-12)

| ID | User Story | 완료일 |
|----|-----------|--------|
| US-48 | LB 트랙은 `_lookup_recording_by_mbid(mbid)` 직접 조회로 오매칭 방지 — 직접 조회 실패 시 기존 `_mb_search_recording()`으로 폴백 | 2026-03-12 |
| US-49 | `_mb_search_recording()`에 Stage 2.5(artist-id 기반 검색) 추가 — MB Artist API로 아티스트 MBID 목록 획득 후 arid:{mbid} AND recording:{t} 재검색. 한국 아티스트 등 다른 언어/표기로 인덱싱된 경우 대응 | 2026-03-12 |
| US-50 | `rematch/apply`에서 `mb_recording_id`가 있을 때 `write_mb_trackid_tag()`로 파일 태그 업데이트 (기존 버그 수정: mb_recording_id를 받아놓고 파일에 쓰지 않음) | 2026-03-12 |
| US-51 | `_select_best_entry()`에 strict 모드 추가 — 라이브/커버 영상을 점수 패널티 대신 사전 필터링으로 제외. 클린 후보가 없을 때만 전체 후보 대상 기존 스코어링으로 폴백. Auto/LB 다운로드는 strict=True 기본값 사용 | 2026-03-12 |

### Epic 10: YouTube 후보 선택 다운로드 및 UI 개선 (2026-03-12)

| ID | User Story | 완료일 |
|----|-----------|--------|
| US-44 | Pick 모드에서 YouTube 후보 목록을 미리 확인하고 원하는 영상을 선택하여 다운로드할 수 있다 (`GET /api/download/candidates`, `POST /api/download` video_id 필드) | 2026-03-12 |
| US-45 | UI 텍스트 영어 통일 및 레이아웃 안정화 (버튼 min-width 고정, 테이블 fixed layout) | 2026-03-12 |
| US-46 | 다운로드 이력에 album 컬럼 표시 — 태깅된 canonical 앨범명을 state.db에 저장하고 이력 테이블에 노출한다 (`downloads.album TEXT`, `mark_done`/`update_track_info` album 파라미터, `GET /api/downloads` album 필드) | 2026-03-12 |
| US-47 | 다운로드 이력 미리듣기 — `GET /api/stream/{mbid}` 오디오 스트리밍 엔드포인트 + 다운로드 이력 Actions 열 ▶ Play 버튼 + 하단 고정 미니 플레이어 (HTML5 audio, 트랙명/아티스트명 표시, ✕ 닫기) | 2026-03-12 |

### Epic 8: 파이프라인 안정성 개선 (2026-03-09)

| ID | User Story | 완료일 |
|----|-----------|--------|
| US-31 | LB CF API가 `recording_mbid`만 반환하므로 MB API `_lookup_recording(mbid)`로 artist/track을 조회한다 | 2026-03-09 |
| US-32 | yt-dlp에서 결제/비공개/멤버십/접근불가 영상 감지 시 다음 후보로 자동 retry한다 (ytsearch5: 5개 후보) | 2026-03-09 |
| US-33 | retry 대상 트랙에 artist/track이 없으면 _lookup_recording()으로 재조회하고 여전히 비면 mark_failed한다 | 2026-03-09 |
| US-34 | ~~beet list 파일 경로 조회 시~~ title 조건 제거 후 Python측 `_normalize_for_match()` fuzzy 비교로 정확도 향상 (beets 제거로 state.db 직접 조회 방식에 계승됨) | 2026-03-09 |
| US-35 | state.db를 named volume `db-data:/app/db`으로 이동 | 2026-03-09 |
| US-36 | ytsearch5 후보 중 live/concert/tour/festival/acoustic version/unplugged 키워드 포함 영상을 최하위 우선순위로 처리 (단어 경계 기준, 전체가 live이면 그 중 최선 선택) | 2026-03-09 |

### Epic 7: 커버아트 및 싱글/커버곡 태깅 개선 (2026-03-06)

| ID | User Story | 완료일 |
|----|-----------|--------|
| US-26 | MusicBrainz 앨범 정보 없는 싱글/커버곡은 YouTube 채널명을 앨범명, 썸네일을 커버아트로 자동 대체한다 | 2026-03-06 |
| US-27 | CAA에서 선택한 release가 404이면 최대 3개까지 다른 release를 순차 시도한다 | 2026-03-06 |
| US-28 | 동일 트랙이 중복 임포트된 경우 모든 파일에 동일 커버아트가 임베딩된다 | 2026-03-06 |
| US-29 | LB 파이프라인에서 artist/track 정보 없는 트랙도 enrichment(앨범명·커버아트)가 실행된다 | 2026-03-06 |
| ~~US-30~~ | ~~beets `quiet_fallback: asis`로 strong match 실패 시 skip 대신 현재 태그로 임포트한다~~ (beets 제거, MB 3단계 폴백으로 대체됨) | 2026-03-06 |

### Epic 6: 라이브러리 브라우저 및 플레이어 (2026-03-05)

| ID | User Story | 완료일 |
|----|-----------|--------|
| US-20 | Web UI에서 아티스트/앨범/트랙을 탐색하고 브라우저 내에서 바로 재생할 수 있다 | 2026-03-05 |
| US-21 | 앨범 단위 전체 재생 및 셔플 재생을 지원한다 | 2026-03-05 |
| US-22 | 플레이어 바에 셔플 토글이 있어 곡 종료 시 랜덤 다음 곡으로 이어진다 | 2026-03-05 |
| US-23 | Subsonic API 프록시가 인증을 자동 주입하여 프론트엔드에 Navidrome 계정 정보 불필요 | 2026-03-05 |
| US-24 | Navidrome이 외부 포트 미노출 — brainstream 도메인 하나만으로 외부 앱 연동 가능 | 2026-03-05 |
| ~~US-25~~ | ~~beets MusicBrainz 연결 시 IPv6 비활성화(sysctls)로 컨테이너 내 연결 실패 방지~~ (beets 제거로 불필요) | 2026-03-05 |

---

## 알려진 이슈 (Known Issues)

| ID | 심각도 | 설명 | 현황 |
|----|--------|------|------|
| ~~BUG-08~~ | ~~Medium~~ | ~~앨범 매칭 성공 후에도 파일이 Unknown Album/ 폴더에 남아있음~~ | **수정 완료 (2026-03-10)** |
| ~~BUG-09~~ | ~~Medium~~ | ~~모든 enrichment 실패 시 album 태그 미기록 → Navidrome "Non-album" 표시~~ | **수정 완료 (2026-03-10)** |
| ~~BUG-10~~ | ~~Medium~~ | ~~rematch/apply에서 Navidrome getSong 절대 경로에 /app/data/music/ 이중 접두사 → 파일 미발견~~ | **수정 완료 (2026-03-10)** |
| ~~BUG-11~~ | ~~Medium~~ | ~~rematch/apply에서 mb_recording_id를 요청으로 받아도 파일 태그에 쓰지 않아 mb_trackid 태그가 누락됨~~ | **수정 완료 (2026-03-12)** |
| ~~BUG-12~~ | ~~Medium~~ | ~~LB 트랙을 artist/track 텍스트 검색으로 MB 매칭 → 동명 아티스트/트랙에 오매칭 위험. mbid 직접 조회로 대체~~ | **수정 완료 (2026-03-12)** |
| ~~BUG-13~~ | ~~Low~~ | ~~한국 아티스트 등 비영어권 아티스트명이 MB에 다른 표기로 인덱싱된 경우 Stage 1/2 검색 실패 → Stage 2.5 artist-id 기반 검색으로 대응~~ | **수정 완료 (2026-03-12)** |
| ~~BUG-14~~ | ~~Low~~ | ~~Auto/LB 다운로드 시 라이브/커버 영상이 점수 패널티만 적용되어 후보가 클린 영상뿐일 때도 선택될 수 있었음 → strict 모드 사전 필터링으로 강화~~ | **수정 완료 (2026-03-12)** |
| BUG-01 | Low | staging 디렉토리에 이전 세션의 `.flac` 파일이 남아있을 수 있음 (컨테이너 재시작 시) | 미해결 |
| BUG-02 | Low | Navidrome 자동 스캔 비활성화 설정(`ND_SCANSCHEDULE: "0"`)이 docker-compose.yml에 하드코딩됨 | 미해결 |
| BUG-03 | Low | 수동 다운로드 잡의 SSE Queue가 메모리에만 존재하여 컨테이너 재시작 시 in-progress 잡 상태 유실 | 미해결 |
| ~~BUG-04~~ | ~~Low~~ | ~~beet list로 파일 경로 조회 시 artist/title 특수문자 포함 쿼리 일부 실패 가능~~ | **해소됨 (beets 제거, state.db file_path 직접 조회로 대체)** |
| ~~BUG-05~~ | ~~Medium~~ | ~~MB recording-only fallback 재검색 시 동명이곡의 다른 아티스트 recording이 반환됨~~ | **수정 완료 (2026-03-09)** |
| ~~BUG-06~~ | ~~Medium~~ | ~~MB release 선택 시 리마스터판/다른 나라 에디션이 원본보다 앞에 선택될 수 있음~~ | **수정 완료 (2026-03-09)** |
| ~~BUG-07~~ | ~~Medium~~ | ~~iTunes/Deezer 결과 검증 없이 첫 번째 결과를 사용하여 관련 없는 아티스트의 앨범아트가 사용됨~~ | **수정 완료 (2026-03-09)** |

---

## 개선 후보 (Backlog)

### 우선순위: 높음

| ID | 설명 | 근거 |
|----|------|------|
| ENH-01 | 설정 파일 유효성 검증 추가 (기동 시 필수 필드 누락 조기 감지) | 현재 런타임 오류로만 발견됨 |
| ENH-02 | staging 디렉토리 기동 시 정리 로직 추가 (BUG-01 해결) | 디스크 낭비 방지 |
| ENH-03 | 실패한 트랙 수동 재시도 API 엔드포인트 (`POST /api/retry/{mbid}`) | 운영 편의성 |
| ~~ENH-12~~ | ~~라이브러리 트랙 삭제 기능~~ (`DELETE /api/downloads/{mbid}`, Web UI 삭제 버튼) → **구현 완료 (2026-03-10)** | 잘못 다운로드된 트랙 운영 편의성 |

### 우선순위: 중간

| ID | 설명 | 근거 |
|----|------|------|
| ENH-04 | Web UI에서 다운로드 이력 필터링 (source, status, 날짜) | 이력이 많아질수록 필요 |
| ENH-05 | 추천 소스 다양화 (Last.fm, Spotify 플레이리스트 등) | LB 추천 품질 편차 존재 |
| ~~ENH-06~~ | ~~중복 다운로드 방지를 위한 beets 라이브러리 사전 확인 (`beet list`로 검색)~~ | **해소됨 (state.db mbid 기준 중복 체크로 처리)** |
| ENH-07 | acoustid 핑거프린팅 활성화 (현재 `apikey: ""` 미설정) | 파일명/태그 없는 경우 매칭 정확도 향상 |

### 우선순위: 낮음

| ID | 설명 | 근거 |
|----|------|------|
| ENH-08 | Web UI 개선: 진행 중인 잡 목록, 이력 페이지네이션 | UX 개선 |
| ENH-09 | Prometheus 메트릭 엔드포인트 (`/metrics`) | 모니터링 인프라 연동 |
| ENH-10 | 다중 사용자 ListenBrainz 계정 지원 | 현재 단일 계정만 지원 |
| ENH-11 | 다운로드 파일 포맷 후처리 설정 (예: FLAC → AAC 변환) | 저장 공간 최적화 |

---

## 기술 부채 (Technical Debt)

| ID | 설명 |
|----|------|
| TD-01 | `src/api.py`의 `_cfg` 전역 변수 주입 방식 → FastAPI의 Dependency Injection으로 전환 권장 |
| ~~TD-02~~ | ~~`tagger.py`에서 `beet list` subprocess 의존 → mediafile/beets Python API 직접 사용 검토~~ → **해소됨 (beets 제거, mutagen 직접 사용)** |
| ~~TD-03~~ | ~~테스트 코드 없음 — 핵심 파이프라인 단계(downloader, tagger, state) 단위 테스트 필요~~ → **해소됨 (tests/ 추가, 242개 통과)** |
| TD-04 | ~~`config.yaml`에 평문 비밀번호 저장~~ → 환경변수 전용으로 전환 완료 |
