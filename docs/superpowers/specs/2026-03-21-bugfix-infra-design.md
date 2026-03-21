# Bugfix + Infrastructure — Design Spec

**Date**: 2026-03-21
**Scope**: #15 cover art fallback, #17 .dockerignore, #18 /health, #19 rate limiter cleanup
**Strategy**: 단일 PR — 4개 독립 수정

---

## #15 cover art fallback 수정

**파일**: `src/pipeline/tagger.py` — `_enrich_track` 내부 3곳 (line ~600, 614, 626)

`embed_art_from_url()` 반환값을 무시하고 `art_embedded = True`로 무조건 설정하여,
embed 실패 시에도 다음 fallback 소스로 내려가지 않는 버그.

```python
# Before
embed_art_from_url(dest_path, url)
art_embedded = True

# After
art_embedded = embed_art_from_url(dest_path, url)
```

3곳 (iTunes art, Deezer art, YouTube thumbnail) 모두 동일 패턴 수정.

## #17 .dockerignore 생성

**파일**: `.dockerignore` (신규)

```text
.git/
tests/
docs/
data/
.env
*.md
.superset/
__pycache__/
*.pyc
.pytest_cache/
```

빌드 컨텍스트에서 불필요한 파일 제외. 이미지 빌드 속도 개선.

## #18 /health 엔드포인트

**파일**: `src/api.py`

단순 liveness check:

```python
@app.get("/health")
async def health():
    return {"status": "ok"}
```

Docker healthcheck이나 로드밸런서에서 사용. DB/worker 체크는 하지 않음 (과도한 재시작 방지).

## #19 rate limiter 주기적 정리

**파일**: `src/api.py`

현재 `_rate_store`는 요청 시에만 만료 항목 정리 (lazy). 한 번 접근 후 다시 안 오는 IP 엔트리가 영구 잔류.

lifespan에 background task 추가:

```python
async def _periodic_rate_cleanup():
    while True:
        await asyncio.sleep(300)  # 5분마다
        now = time.time()
        expired = [k for k, v in _rate_store.items()
                   if all(now - t > _rate_window for t in v)]
        for k in expired:
            _rate_store.pop(k, None)
```

`_lifespan`에서 `asyncio.create_task()`로 시작, yield 후 cancel.

---

## 범위 외

- #20 get_all_downloads 페이지네이션
- #21 수동 다운로드 중복 체크
