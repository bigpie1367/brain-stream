# CodeRabbit PR #20 Review Fixes — Plan

## Steps

1. Fix `get_pipeline_interval` int parsing in `src/api.py`
2. Add CF model refresh detection probe in `src/main.py`
3. Wrap radio fallback CF re-fetch in try/except in `src/main.py`
4. Add MBID dedup for retryable tracks in `src/main.py`
5. Change worker thread to daemon=True in `src/main.py`
6. Complete Lucene escape in `src/pipeline/musicbrainz.py`
7. Use `.get()` for meta dict access in `src/pipeline/tagger.py`
8. Add dirname empty string guard in `src/utils/logger.py`
9. Fix healthcheck port in `docs/operations.md`

## Round 2 Steps

10. Fix daemon comment mismatch in `src/main.py`
11. Fix `#history-table` → `#section-history` in `src/static/index.html`
12. Add file collision check in `rematch_apply` in `src/api.py`
13. Make `_write_single_tag` raise on failure in `src/pipeline/tagger.py`
14. Atomic `INSERT ... WHERE NOT EXISTS` in `src/state.py`
15. Update tests for collision check mock and exception expectations
16. Fix `is_downloaded()` to include pending/downloading in `src/state.py`
17. Add `source` to `get_retryable()` SELECT in `src/state.py`
18. Use `track.get("source")` in pipeline enqueue in `src/main.py`
