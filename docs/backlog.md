# 프로젝트 백로그

- **작성일**: 2026-03-04
- **현재 버전**: 1.0.0

---

## 완료된 기능 (Done)

### Epic 1: 자동 파이프라인 구축

| ID | User Story | 완료일 |
|----|-----------|--------|
| US-01 | ListenBrainz CF 추천 API로 트랙 목록을 가져올 수 있다 | 2026-03-03 |
| US-02 | YouTube에서 트랙을 자동으로 검색하고 FLAC/Opus로 다운로드할 수 있다 | 2026-03-03 |
| US-03 | beets singleton 임포트로 MusicBrainz 기준 메타데이터를 자동 태깅할 수 있다 | 2026-03-03 |
| US-04 | 이미 처리된 트랙은 mbid 기준으로 중복 처리를 건너뛴다 | 2026-03-03 |
| US-05 | 실패한 트랙은 최대 3회까지 자동 재시도된다 | 2026-03-03 |
| US-06 | 설정된 시간 간격(6시간)으로 파이프라인이 자동 실행된다 | 2026-03-03 |

### Epic 2: 메타데이터 enrichment

| ID | User Story | 완료일 |
|----|-----------|--------|
| US-07 | beets singleton 임포트 후 MB API로 공식 앨범명을 자동으로 조회하여 태그에 반영한다 | 2026-03-03 |
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
| US-16 | beet import가 동시에 여러 번 실행되어도 import 로그가 오염되지 않는다 (Lock) | 2026-03-03 |
| US-17 | 개별 트랙 실패가 전체 파이프라인을 중단시키지 않는다 | 2026-03-03 |
| US-18 | 구조화 로그(structlog)로 파이프라인 각 단계를 추적할 수 있다 | 2026-03-03 |
| US-19 | Docker Compose 단일 명령으로 전체 스택을 실행할 수 있다 | 2026-03-03 |

---

## 알려진 이슈 (Known Issues)

| ID | 심각도 | 설명 | 현황 |
|----|--------|------|------|
| BUG-01 | Low | staging 디렉토리에 이전 세션의 `.flac` 파일이 남아있을 수 있음 (컨테이너 재시작 시) | 미해결 |
| BUG-02 | Low | Navidrome 자동 스캔 비활성화 설정(`ND_SCANSCHEDULE: "0"`)이 docker-compose.yml에 하드코딩됨 | 미해결 |
| BUG-03 | Low | 수동 다운로드 잡의 SSE Queue가 메모리에만 존재하여 컨테이너 재시작 시 in-progress 잡 상태 유실 | 미해결 |
| BUG-04 | Low | beet list로 파일 경로 조회 시 artist/title 특수문자 포함 쿼리 일부 실패 가능 | 미해결 |

---

## 개선 후보 (Backlog)

### 우선순위: 높음

| ID | 설명 | 근거 |
|----|------|------|
| ENH-01 | 설정 파일 유효성 검증 추가 (기동 시 필수 필드 누락 조기 감지) | 현재 런타임 오류로만 발견됨 |
| ENH-02 | staging 디렉토리 기동 시 정리 로직 추가 (BUG-01 해결) | 디스크 낭비 방지 |
| ENH-03 | 실패한 트랙 수동 재시도 API 엔드포인트 (`POST /api/retry/{mbid}`) | 운영 편의성 |

### 우선순위: 중간

| ID | 설명 | 근거 |
|----|------|------|
| ENH-04 | Web UI에서 다운로드 이력 필터링 (source, status, 날짜) | 이력이 많아질수록 필요 |
| ENH-05 | 추천 소스 다양화 (Last.fm, Spotify 플레이리스트 등) | LB 추천 품질 편차 존재 |
| ENH-06 | 중복 다운로드 방지를 위한 beets 라이브러리 사전 확인 (`beet list`로 검색) | 불필요한 다운로드/태깅 방지 |
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
| TD-02 | `tagger.py`에서 `beet list` subprocess 의존 → mediafile/beets Python API 직접 사용 검토 |
| TD-03 | 테스트 코드 없음 — 핵심 파이프라인 단계(downloader, tagger, state) 단위 테스트 필요 |
| TD-04 | ~~`config.yaml`에 평문 비밀번호 저장~~ → 환경변수 전용으로 전환 완료 |
