# Fix CodeRabbit PR #20 Review Comments

## Problem

Two issues identified in CodeRabbit review of PR #20:

1. **Stale response race condition in `loadHistory`**: When a stale response returns, it sets `_historyLoading = false` which allows new concurrent loads before the current request finishes.

2. **5-second full reset breaks infinite scroll**: `setInterval` calls `resetAndLoadHistory()` every 5s unconditionally, which clears DOM and reloads from page 1, destroying scroll position when the user has scrolled deep into the list.

## Solution

### Fix 1: Stale response race condition
- In the stale check block, do NOT set `_historyLoading = false` -- just return silently.
- In the `finally` block, only reset `_historyLoading` if this is still the current request (check `requestSeq === _historyRequestSeq`).

### Fix 2: Auto-refresh scroll disruption
- Guard the auto-refresh with a scroll position check: only call `resetAndLoadHistory()` when the history table top is within 200px of the viewport top (user hasn't scrolled far down).

## Files Changed
- `src/static/index.html`
