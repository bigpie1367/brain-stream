# Pagination + Search + Duplicate Check — Design Spec

**Date**: 2026-03-21
**Scope**: #20 get_all_downloads 페이지네이션 + 검색, #21 수동 다운로드 중복 체크
**Strategy**: 단일 PR

---

## #20 페이지네이션 + 검색

### Backend — `src/state.py`

기존 `get_all_downloads(db_path, limit=100)` → `get_downloads_page(db_path, limit, offset, search)`:

```python
def get_downloads_page(
    db_path: str, limit: int = 100, offset: int = 0, search: str = ""
) -> dict:
    """Paginated download list with optional search.

    Returns: {"items": [...], "total": int, "limit": int, "offset": int}
    """
```

- `search` 비어있으면 전체 조회 (기존 동작)
- `search` 있으면 `WHERE artist LIKE ? OR track_name LIKE ? OR album LIKE ?` — `%search%` 패턴
- `total`은 동일 조건의 `COUNT(*)` — 무한 스크롤에서 "더 있는지" 판단용
- 기존 `get_all_downloads`는 내부적으로 `get_downloads_page` 호출하도록 래핑하여 하위 호환 유지

### Backend — `src/api.py`

```python
@app.get("/api/downloads")
async def list_downloads(
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    search: str = Query(default="", max_length=200),
):
```

반환: `{"items": [...], "total": 150, "limit": 100, "offset": 0}`

기존 반환이 리스트였으므로 **breaking change** — 프론트엔드도 함께 수정.

### Frontend — `src/static/index.html`

- 검색 input 추가 (debounce 300ms)
- 무한 스크롤: 하단 도달 시 offset += limit으로 추가 로드
- `total <= offset + items.length`이면 더 이상 로드하지 않음
- 검색어 변경 시 offset=0으로 리셋

---

## #21 수동 다운로드 중복 체크

### Backend — `src/state.py`

```python
def find_active_download(db_path: str, artist: str, track_name: str) -> dict | None:
    """done/downloading/pending 상태인 동일 artist+track_name 레코드 조회.
    Returns first match or None.
    """
```

SQLite: `WHERE artist = ? AND track_name = ? AND status IN ('done', 'downloading', 'pending')`

### Backend — `src/api.py`

`POST /api/download`에서 enqueue 전에 체크:

```python
existing = find_active_download(_cfg.state_db, req.artist, req.track)
if existing:
    raise HTTPException(
        status_code=409,
        detail=f"이미 존재: {existing['mbid']} ({existing['status']})"
    )
```

`failed` 상태는 중복으로 취급하지 않아 재다운로드 가능.
