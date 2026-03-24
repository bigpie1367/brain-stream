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
