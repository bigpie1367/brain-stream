# CodeRabbit PR #20 Review Fixes — Spec

## Problem
PR #20 received several review comments from CodeRabbit identifying potential bugs and robustness issues across multiple files.

## Fixes

1. **api.py**: `int(value)` in `get_pipeline_interval` can throw ValueError on bad DB data — add try/except with fallback
2. **main.py**: CF model refresh detection — stored `cf_first_mbid` is never compared on subsequent runs; add probe check when offset > 0
3. **main.py**: Radio fallback CF re-fetch lacks exception handling — wrap in try/except
4. **main.py**: Retryable tracks can duplicate MBIDs already in `new_tracks` — add dedup
5. **main.py**: Worker thread should be daemon=True since shutdown logic already handles join(30)
6. **musicbrainz.py**: `_escape_mb_query` only escapes `\` and `"` but Lucene has many more special chars
7. **tagger.py**: `meta["artist"]` can raise KeyError — use `meta.get()` instead
8. **logger.py**: `os.path.dirname(log_file)` can return empty string — add guard
9. **operations.md**: Healthcheck port should be 8000 (container internal), not 8080

## Round 2 Fixes

10. **main.py**: Worker thread comment said "non-daemon" but code is daemon=True — fix comment
11. **index.html**: `#history-table` selector doesn't exist in DOM — fix to `#section-history` (auto-refresh was broken)
12. **api.py**: `rematch_apply` file collision check missing before `move_to_music_dir` — add `os.path.exists` guard with 409
13. **tagger.py**: `_write_single_tag` swallowed exceptions — make it raise so API callers can fail-fast
14. **state.py**: `mark_pending_if_not_duplicate` race condition — replace SELECT+INSERT with atomic `INSERT ... WHERE NOT EXISTS`
15. **tests**: Update rematch_apply tests for collision check mock, tagger tests for raised exceptions
16. **state.py**: `is_downloaded()` should also filter pending/downloading to prevent double-enqueue on restart
17. **state.py**: `get_retryable()` missing `source` column — manual failed jobs lose source context
18. **main.py**: `run_pipeline()` enqueue should use `track.get("source")` instead of hardcoded `"listenbrainz"`
