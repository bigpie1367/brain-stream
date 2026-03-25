# Plan: 히스토리 테이블 너비 고정

## 단계
1. `src/static/index.html`의 `table` CSS에서 `table-layout: auto` → `table-layout: fixed` 변경
2. Track/Album 컬럼 너비 확대, Time 컬럼 축소
3. auto-refresh 시 `resetAndLoadHistory()` 대신 `loadHistory(false)` 호출하여 깜빡임 제거
