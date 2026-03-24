# Bulk Delete Design

## Problem

Deleting multiple download history entries requires clicking one-by-one, and the DELETE endpoint rate limit (10/min) caps it at 10 deletions per minute.

## Solution

Add a toggle-based bulk delete mode to the download history table, backed by a unified bulk delete API endpoint.

## Frontend

### Normal State

- Add a "Delete" button next to the existing "Refresh" button in the table header
- Button uses `btn-secondary` style (same tone as Refresh)

### Delete Mode (activated by clicking Delete button)

- Header buttons change: Refresh/Delete → Cancel / "Delete N selected" (red, disabled when 0)
- Checkbox column appears on the left of each row
- Header row gets a checkbox for select-all/deselect-all (only affects currently loaded rows)
- Per-row Delete button is hidden
- Row click-to-expand is disabled; row click toggles checkbox instead
- "Delete N selected" count updates as checkboxes are toggled

### Confirmation Modal

```
Title: Delete Tracks
Body:  Delete N tracks?
       Associated files will also be removed.
Actions: [ Cancel ] [ Delete ]
```

### After Deletion

- Delete mode exits, UI returns to normal state
- Deleted rows are removed from DOM
- Auto-refresh resumes (paused during delete mode)

## Backend

### API Change

**Remove:** `DELETE /api/downloads/{mbid}`

**Add:** `DELETE /api/downloads`

```
Request body:
{ "mbids": ["mbid1", "mbid2", ...] }

Response 200:
{
  "deleted": 3,
  "files_removed": 2,
  "errors": [
    { "mbid": "mbid3", "error": "file permission denied" }
  ]
}
```

- Empty mbids array → 400 error
- Non-existent mbid → silently skipped (not an error)
- File deletion failure → recorded in `errors`, but DB marking (ignored) still proceeds
- Navidrome scan triggered once after all file deletions
- Rate limit: 10 calls/60s (unchanged; single call handles multiple items)

### Existing single-delete frontend

Update `confirmDelete()` to call the new bulk endpoint with a single-element array.

## Database (state.py)

- **Remove:** `mark_ignored(mbid)` — replaced by bulk function
- **Remove:** `delete_download(mbid)` — unused
- **Add:** `mark_ignored_bulk(mbids)` — single transaction, updates all mbids to `status='ignored'`

All existing callers of `mark_ignored` are updated to use `mark_ignored_bulk`.
