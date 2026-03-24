# Implementation Plan: Fix CodeRabbit PR #20 Review Comments

## Steps

### 1. Rebase onto origin/develop
- The PR #20 code (pagination, infinite scroll) is on the develop branch
- Rebase this worktree branch onto origin/develop to get the code

### 2. Fix stale response race condition in `loadHistory`
- File: `src/static/index.html`
- Location: stale check block (where `requestSeq !== _historyRequestSeq`)
  - Remove `_historyLoading = false` from the stale check -- just return
- Location: `finally` block at end of `loadHistory`
  - Wrap `_historyLoading = false` in `if (requestSeq === _historyRequestSeq)` guard

### 3. Fix 5-second auto-refresh breaking infinite scroll
- File: `src/static/index.html`
- Location: `setInterval` block that calls `resetAndLoadHistory()`
  - Early-return when any modal is open
  - Add scroll position check: only refresh when `history-table` top is within 200px of viewport top
