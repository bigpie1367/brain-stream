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
