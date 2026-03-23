# Documentation Sync v2.0.0 — Design Spec

- **작성일**: 2026-03-21
- **범위**: 문서 전용 (코드 변경 없음)

---

## 배경

PR #14~#17에서 다수의 코드 변경이 이루어졌으나 문서가 동기화되지 않아 17개 갭이 발생.

## 변경 대상

| 문서 | 갭 | 수정 내용 |
|------|-----|----------|
| `docs/architecture.md` | 신규 모듈 누락 | `src/jobs.py`, `src/pipeline/musicbrainz.py`, `src/utils/fs.py` 추가 |
| `docs/api-spec.md` | `/health` 미문서화, pagination 미반영 | `/health` 엔드포인트 추가, `GET /api/downloads` 페이지네이션 스펙, `POST /api/download` 중복 감지 |
| `docs/data-model.md` | 신규 함수 미문서화 | `get_downloads_page`, `find_active_download`, `mark_pending_if_not_duplicate` 등 11개 함수 참조 |
| `docs/operations.md` | 헬스체크/dockerignore 미문서화 | 섹션 3.5 헬스체크, `.dockerignore` 설명 |
| `docs/requirements.md` | FR-08 구식, 중복방지 미문서화 | FR-08 페이지네이션/검색 반영, FR-09 중복 방지 추가 |
| `CLAUDE.md` | Key Files 테이블 미갱신 | 3개 신규 모듈 추가 |
| `.gitignore` | worktrees 미제외 | `.worktrees/` 추가 |

## 코드 변경 없음

이 PR은 문서와 `.gitignore`만 수정하며 Python/HTML/Docker 파일은 변경하지 않음.
