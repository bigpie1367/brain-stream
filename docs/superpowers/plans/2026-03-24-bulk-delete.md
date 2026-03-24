# Bulk Delete Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bulk delete mode to download history — toggle mode with checkboxes, unified bulk DELETE API, single DB transaction.

**Architecture:** Replace single-item `DELETE /api/downloads/{mbid}` with `DELETE /api/downloads` accepting `{"mbids": [...]}`. Frontend adds a toggle delete-mode with per-row checkboxes, header select-all, and confirmation modal. DB gets `mark_ignored_bulk()` replacing `mark_ignored()` and removing unused `delete_download()`.

**Tech Stack:** Python/FastAPI (backend), vanilla JS (frontend), SQLite (DB)

**Spec:** `docs/superpowers/specs/2026-03-24-bulk-delete-design.md`

---

### Task 1: Database — Replace `mark_ignored` with `mark_ignored_bulk`

**Files:**
- Modify: `src/state.py:234-240` (replace `mark_ignored`)
- Modify: `src/state.py:323-325` (remove `delete_download`)
- Modify: `tests/unit/test_state.py:16,119-143` (update tests)

- [ ] **Step 1: Update test imports and rewrite tests for bulk function**

In `tests/unit/test_state.py`, replace `mark_ignored` import with `mark_ignored_bulk` and update all 4 test functions:

```python
# Change import line: mark_ignored → mark_ignored_bulk
from src.state import (
    ...
    mark_ignored_bulk,
    ...
)

def test_is_downloaded_true_after_mark_ignored(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-ign", "Song", "Artist")
    mark_ignored_bulk(tmp_state_db, ["mbid-ign"])
    assert is_downloaded(tmp_state_db, "mbid-ign") is True


def test_mark_ignored_bulk_sets_status(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-ign2", "Song", "Artist")
    mark_ignored_bulk(tmp_state_db, ["mbid-ign2"])
    row = get_download_by_mbid(tmp_state_db, "mbid-ign2")
    assert row["status"] == "ignored"


def test_mark_ignored_bulk_multiple(tmp_state_db):
    """Bulk call should mark all given mbids as ignored in one transaction."""
    mark_pending(tmp_state_db, "mbid-b1", "Song1", "Artist1")
    mark_pending(tmp_state_db, "mbid-b2", "Song2", "Artist2")
    mark_pending(tmp_state_db, "mbid-b3", "Song3", "Artist3")
    mark_ignored_bulk(tmp_state_db, ["mbid-b1", "mbid-b2", "mbid-b3"])
    for m in ["mbid-b1", "mbid-b2", "mbid-b3"]:
        row = get_download_by_mbid(tmp_state_db, m)
        assert row["status"] == "ignored"


def test_mark_ignored_excluded_from_retryable(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-ign3", "Song", "Artist")
    mark_ignored_bulk(tmp_state_db, ["mbid-ign3"])
    retryable = get_retryable(tmp_state_db, max_attempts=3)
    assert not any(r["mbid"] == "mbid-ign3" for r in retryable)


def test_mark_ignored_included_in_get_all_downloads(tmp_state_db):
    mark_pending(tmp_state_db, "mbid-ign4", "Song", "Artist")
    mark_ignored_bulk(tmp_state_db, ["mbid-ign4"])
    rows = get_all_downloads(tmp_state_db)
    assert any(r["mbid"] == "mbid-ign4" and r["status"] == "ignored" for r in rows)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/roh-suin/.superset/worktrees/brain-stream/feat/bulk-delete && python -m pytest tests/unit/test_state.py -v -k "mark_ignored"`
Expected: FAIL — `mark_ignored_bulk` not found in imports

- [ ] **Step 3: Implement `mark_ignored_bulk` and remove old functions**

In `src/state.py`:

1. Replace `mark_ignored` (lines 234-240) with:
```python
def mark_ignored_bulk(db_path: str, mbids: list[str]):
    """Bulk-mark tracks as ignored so the pipeline won't re-download them."""
    if not mbids:
        return
    with _conn(db_path) as conn:
        conn.executemany(
            "UPDATE downloads SET status = 'ignored' WHERE mbid = ?",
            [(m,) for m in mbids],
        )
```

2. Delete `delete_download` function (lines 323-325).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/roh-suin/.superset/worktrees/brain-stream/feat/bulk-delete && python -m pytest tests/unit/test_state.py -v -k "mark_ignored"`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/state.py tests/unit/test_state.py
git commit -m "feat: replace mark_ignored with mark_ignored_bulk, remove delete_download"
```

---

### Task 2: API — Replace single DELETE with bulk endpoint

**Files:**
- Modify: `src/api.py:40-48` (update import)
- Modify: `src/api.py:88-94` (update rate limit key)
- Modify: `src/api.py:346-387` (replace endpoint)

- [ ] **Step 1: Update import in `src/api.py`**

In `src/api.py` line 44, change:
```python
# old
    mark_ignored,
# new
    mark_ignored_bulk,
```

- [ ] **Step 2: Update rate limit key**

In `src/api.py` line 93, change:
```python
# old
    "DELETE /api/downloads/": 10,
# new
    "DELETE /api/downloads": 10,
```

- [ ] **Step 3: Add Pydantic request model**

After the existing `class DownloadRequest(BaseModel):` block in `src/api.py`, add:
```python
class BulkDeleteRequest(BaseModel):
    mbids: list[str] = Field(..., min_length=1)
```

- [ ] **Step 4: Replace the DELETE endpoint**

Replace `delete_download_entry` (lines 346-387) with:

```python
@app.delete("/api/downloads")
async def delete_downloads_bulk(body: BulkDeleteRequest):
    if not _cfg:
        raise HTTPException(status_code=503, detail="config not loaded yet")

    files_removed = 0
    errors = []

    for mbid in body.mbids:
        record = get_download_by_mbid(_cfg.state_db, mbid)
        if record is None:
            continue  # silently skip non-existent

        file_path = record.get("file_path") or ""
        if file_path:
            try:
                os.remove(file_path)
                files_removed += 1
                log.info("removed file", file=file_path)
                # Clean empty folders (album → artist)
                album_dir = os.path.dirname(file_path)
                artist_dir = os.path.dirname(album_dir)
                try:
                    if os.path.isdir(album_dir) and not os.listdir(album_dir):
                        os.rmdir(album_dir)
                        if os.path.isdir(artist_dir) and not os.listdir(artist_dir):
                            os.rmdir(artist_dir)
                except Exception as exc:
                    log.warning("delete: failed to remove empty dirs", error=str(exc))
            except FileNotFoundError:
                log.info("file already gone, skipping removal", file=file_path)
            except OSError as exc:
                log.warning("could not remove file", file=file_path, error=str(exc))
                errors.append({"mbid": mbid, "error": str(exc)})

    mark_ignored_bulk(_cfg.state_db, body.mbids)

    if files_removed:
        threading.Thread(
            target=trigger_scan,
            args=(_cfg.navidrome.url, _cfg.navidrome.username, _cfg.navidrome.password),
            daemon=True,
        ).start()

    log.info("bulk delete completed", count=len(body.mbids), files_removed=files_removed)
    return {"deleted": len(body.mbids), "files_removed": files_removed, "errors": errors}
```

- [ ] **Step 5: Run full test suite to verify nothing broke**

Run: `cd /Users/roh-suin/.superset/worktrees/brain-stream/feat/bulk-delete && python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/api.py
git commit -m "feat: replace single DELETE endpoint with bulk DELETE /api/downloads"
```

---

### Task 3: Frontend — Bulk delete mode UI

**Files:**
- Modify: `src/static/index.html`
  - ~line 971-986: Add checkbox and bulk-delete-mode CSS
  - ~line 1285-1305: Modify table header (add Delete button, checkbox column)
  - ~line 1374-1384: Update delete modal for bulk
  - ~line 1862-1956: Modify `_createRow()` to include checkbox cell
  - ~line 2333-2419: Rewrite delete modal handlers for bulk
  - ~line 2445-2458: Pause auto-refresh during delete mode

- [ ] **Step 1: Add CSS for bulk delete mode**

After the `.btn-expand-delete:hover` rule (~line 986), add:

```css
/* Bulk delete mode */
.bulk-delete-active .history-row { cursor: pointer; }
.bulk-delete-active .history-row:hover { background: #2a1a1a; }
.bulk-delete-active .history-row.bulk-selected { background: #2a1a1a; }
.bulk-cb-cell { width: 32px; text-align: center; padding: 0 4px; }
.bulk-cb-cell input[type="checkbox"] {
  width: 15px; height: 15px; cursor: pointer;
  accent-color: #fc8181;
}
.bulk-cb-col { display: none; }
.bulk-delete-active .bulk-cb-col { display: table-cell; }
.btn-bulk-delete {
  padding: 5px 12px; font-size: 0.8rem; min-width: 0;
  background: #4a2020; border: 1px solid #fc8181; color: #fc8181;
  border-radius: 6px; cursor: pointer; font-weight: 500;
  transition: background 0.12s, opacity 0.12s;
}
.btn-bulk-delete:hover { background: #5a2525; }
.btn-bulk-delete:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-bulk-cancel {
  padding: 5px 12px; font-size: 0.8rem; min-width: 0;
  background: none; border: 1px solid #4a5568; color: #a0aec0;
  border-radius: 6px; cursor: pointer; font-weight: 500;
  transition: background 0.12s;
}
.btn-bulk-cancel:hover { background: #2d3748; }
```

- [ ] **Step 2: Update table header HTML**

Replace the table-header div and thead (lines 1287-1305) with:

```html
<div class="table-header">
  <h2 style="margin-bottom:0">Download History</h2>
  <div style="display:flex; gap:6px; align-items:center;">
    <!-- Normal mode buttons -->
    <span id="normal-mode-btns">
      <button class="btn-secondary" style="padding:5px 12px; font-size:0.8rem; min-width:0;" onclick="resetAndLoadHistory()">Refresh</button>
      <button class="btn-secondary" style="padding:5px 12px; font-size:0.8rem; min-width:0;" onclick="enterBulkDeleteMode()">Delete</button>
    </span>
    <!-- Bulk delete mode buttons (hidden by default) -->
    <span id="bulk-mode-btns" style="display:none;">
      <button class="btn-bulk-cancel" onclick="exitBulkDeleteMode()">Cancel</button>
      <button class="btn-bulk-delete" id="bulk-delete-btn" disabled onclick="openBulkDeleteModal()">Delete 0 selected</button>
    </span>
  </div>
</div>
```

Update the `<thead>` to include a checkbox column:

```html
<thead>
  <tr>
    <th class="bulk-cb-col bulk-cb-cell"><input type="checkbox" id="bulk-select-all" title="Select all" onchange="toggleSelectAll(this.checked)"></th>
    <th style="width:17%">Artist</th>
    <th style="width:24%">Track</th>
    <th style="width:18%">Album</th>
    <th style="width:13%">Source</th>
    <th style="width:10%">Status</th>
    <th style="width:9%">Link</th>
    <th style="width:9%">Time</th>
  </tr>
</thead>
```

- [ ] **Step 3: Update `_createRow()` to include checkbox cell**

In `_createRow()` (~line 1862), add a checkbox td as the first cell before `tdArtist`:

```javascript
// Add as first line inside _createRow, before tdArtist creation:
const tdCb = document.createElement('td');
tdCb.className = 'bulk-cb-col bulk-cb-cell';
const cb = document.createElement('input');
cb.type = 'checkbox';
cb.dataset.mbid = r.mbid;
cb.addEventListener('change', (e) => {
  e.stopPropagation();
  onBulkCheckboxChange(r.mbid, cb.checked, tr);
});
cb.addEventListener('click', (e) => e.stopPropagation());
tdCb.appendChild(cb);

// Then prepend it before other cells:
tr.appendChild(tdCb);  // <-- this goes BEFORE tr.appendChild(tdArtist)
```

Update `expandTd.colSpan` from `7` to `8` (~line 1923).

- [ ] **Step 4: Add row-click-to-toggle in delete mode**

In the row click handler inside `_createRow()` (~line 1936), wrap existing expand logic so that in delete mode, clicking toggles the checkbox instead:

```javascript
tr.addEventListener('click', (e) => {
  e.stopPropagation();
  if (_bulkDeleteMode) {
    const cb = tr.querySelector('.bulk-cb-cell input[type="checkbox"]');
    if (cb) { cb.checked = !cb.checked; onBulkCheckboxChange(r.mbid, cb.checked, tr); }
    return;
  }
  // ... existing expand logic unchanged ...
});
```

Also add this click handler for non-expandable rows (rows without the `isExpandable` branch):
After line 1953 (end of `if (isExpandable)` block), add:

```javascript
if (!isExpandable) {
  tr.addEventListener('click', (e) => {
    e.stopPropagation();
    if (_bulkDeleteMode) {
      const cb = tr.querySelector('.bulk-cb-cell input[type="checkbox"]');
      if (cb) { cb.checked = !cb.checked; onBulkCheckboxChange(r.mbid, cb.checked, tr); }
    }
  });
}
```

- [ ] **Step 5: Add bulk delete mode state and functions**

Before the delete modal section (~line 2333), add:

```javascript
// ── Bulk delete mode ─────────────────────────────────────────────────────
let _bulkDeleteMode = false;
const _bulkSelectedMbids = new Set();

function enterBulkDeleteMode() {
  _bulkDeleteMode = true;
  _bulkSelectedMbids.clear();
  document.getElementById('section-history').classList.add('bulk-delete-active');
  document.getElementById('normal-mode-btns').style.display = 'none';
  document.getElementById('bulk-mode-btns').style.display = '';
  document.getElementById('bulk-select-all').checked = false;
  collapseAllExpandRows();
  _expandedMbid = null;
  updateBulkDeleteBtn();
}

function exitBulkDeleteMode() {
  _bulkDeleteMode = false;
  _bulkSelectedMbids.clear();
  document.getElementById('section-history').classList.remove('bulk-delete-active');
  document.getElementById('normal-mode-btns').style.display = '';
  document.getElementById('bulk-mode-btns').style.display = 'none';
  document.getElementById('bulk-select-all').checked = false;
  // Uncheck all checkboxes
  document.querySelectorAll('#history-body .bulk-cb-cell input[type="checkbox"]').forEach(cb => {
    cb.checked = false;
  });
  document.querySelectorAll('#history-body .history-row.bulk-selected').forEach(tr => {
    tr.classList.remove('bulk-selected');
  });
}

function onBulkCheckboxChange(mbid, checked, tr) {
  if (checked) {
    _bulkSelectedMbids.add(mbid);
    tr.classList.add('bulk-selected');
  } else {
    _bulkSelectedMbids.delete(mbid);
    tr.classList.remove('bulk-selected');
  }
  updateBulkDeleteBtn();
  // Update select-all checkbox state
  const allCbs = document.querySelectorAll('#history-body .bulk-cb-cell input[type="checkbox"]');
  const allChecked = allCbs.length > 0 && [...allCbs].every(cb => cb.checked);
  document.getElementById('bulk-select-all').checked = allChecked;
}

function toggleSelectAll(checked) {
  document.querySelectorAll('#history-body .bulk-cb-cell input[type="checkbox"]').forEach(cb => {
    cb.checked = checked;
    const mbid = cb.dataset.mbid;
    const tr = cb.closest('tr');
    if (checked) {
      _bulkSelectedMbids.add(mbid);
      if (tr) tr.classList.add('bulk-selected');
    } else {
      _bulkSelectedMbids.delete(mbid);
      if (tr) tr.classList.remove('bulk-selected');
    }
  });
  updateBulkDeleteBtn();
}

function updateBulkDeleteBtn() {
  const btn = document.getElementById('bulk-delete-btn');
  const count = _bulkSelectedMbids.size;
  btn.textContent = 'Delete ' + count + ' selected';
  btn.disabled = count === 0;
}
```

- [ ] **Step 6: Rewrite delete modal handlers for bulk support**

Replace the entire delete modal section (~lines 2333-2419) with:

```javascript
// ── Delete modal (supports both single and bulk) ─────────────────────────
let _pendingDeleteMbids = [];
let _pendingDeleteRows  = [];  // {mainTr, expandTr} pairs

function openDeleteModal(mbid, artist, trackName, rowEl, expandRowEl) {
  // Single delete — called from expand row button
  _pendingDeleteMbids = [mbid];
  _pendingDeleteRows = [{ mainTr: rowEl, expandTr: expandRowEl }];

  const msgEl = document.getElementById('modal-msg');
  msgEl.textContent = '';
  const strong = document.createElement('strong');
  strong.textContent = artist + ' - ' + trackName;
  msgEl.appendChild(strong);
  msgEl.appendChild(document.createTextNode(' will be deleted from history. Are you sure?'));

  const titleEl = document.getElementById('modal-title-text');
  titleEl.textContent = 'Delete Track';
  const confirmBtn = document.getElementById('modal-confirm-btn');
  confirmBtn.disabled = false;
  confirmBtn.textContent = 'Delete';
  document.getElementById('delete-modal').classList.remove('hidden');
}

function openBulkDeleteModal() {
  const count = _bulkSelectedMbids.size;
  if (count === 0) return;
  _pendingDeleteMbids = [..._bulkSelectedMbids];
  _pendingDeleteRows = [];
  _pendingDeleteMbids.forEach(mbid => {
    const mainTr = document.querySelector('#history-body tr[data-mbid="' + mbid + '"]');
    const expandTr = mainTr ? mainTr.nextElementSibling : null;
    _pendingDeleteRows.push({
      mainTr,
      expandTr: (expandTr && expandTr.classList.contains('expand-row')) ? expandTr : null,
    });
  });

  const msgEl = document.getElementById('modal-msg');
  msgEl.textContent = 'Delete ' + count + ' track' + (count > 1 ? 's' : '') + '? Associated files will also be removed.';

  const titleEl = document.getElementById('modal-title-text');
  titleEl.textContent = 'Delete Tracks';
  const confirmBtn = document.getElementById('modal-confirm-btn');
  confirmBtn.disabled = false;
  confirmBtn.textContent = 'Delete';
  document.getElementById('delete-modal').classList.remove('hidden');
}

function closeDeleteModal() {
  document.getElementById('delete-modal').classList.add('hidden');
  _pendingDeleteMbids = [];
  _pendingDeleteRows = [];
}

async function confirmDelete() {
  if (!_pendingDeleteMbids.length) return;

  const confirmBtn = document.getElementById('modal-confirm-btn');
  confirmBtn.disabled = true;
  confirmBtn.textContent = 'Deleting...';

  try {
    const res = await fetch('/api/downloads', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mbids: _pendingDeleteMbids }),
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || 'HTTP ' + res.status);
    }

    // Remove rows from DOM
    _pendingDeleteRows.forEach(({ mainTr, expandTr }) => {
      if (expandTr && expandTr.parentNode) expandTr.parentNode.removeChild(expandTr);
      if (mainTr && mainTr.parentNode) mainTr.parentNode.removeChild(mainTr);
    });

    // Clear expanded state if deleted
    if (_pendingDeleteMbids.includes(_expandedMbid)) {
      _expandedMbid = null;
    }

    // Show "no data" if empty
    const tbody = document.getElementById('history-body');
    if (!tbody.querySelectorAll('tr.history-row').length) {
      document.getElementById('no-data').style.display = 'block';
    }

    closeDeleteModal();

    // Exit bulk mode if we were in it
    if (_bulkDeleteMode) {
      exitBulkDeleteMode();
    }
  } catch (e) {
    confirmBtn.disabled = false;
    confirmBtn.textContent = 'Delete';
    const msgEl = document.getElementById('modal-msg');
    const errSpan = document.createElement('span');
    errSpan.style.color = '#fc8181';
    errSpan.style.display = 'block';
    errSpan.style.marginTop = '8px';
    errSpan.style.fontSize = '0.8rem';
    errSpan.textContent = 'Delete failed: ' + e.message;
    const prevErr = msgEl.querySelector('.delete-err');
    if (prevErr) prevErr.remove();
    errSpan.className = 'delete-err';
    msgEl.appendChild(errSpan);
  }
}
```

- [ ] **Step 7: Update auto-refresh to pause during bulk delete mode**

In the auto-refresh `setInterval` (~line 2448), add `_bulkDeleteMode` check:

```javascript
setInterval(() => {
  if (_bulkDeleteMode) return;  // <-- add this line
  const deleteModalOpen  = !document.getElementById('delete-modal').classList.contains('hidden');
  // ... rest unchanged
}, 5000);
```

- [ ] **Step 8: Manual test**

Build and run:
```bash
/Users/roh-suin/.superset/worktrees/brain-stream/feat/bulk-delete/restart_local_docker.sh
```

Verify:
1. Normal mode: Refresh + Delete buttons visible, no checkboxes
2. Click Delete → enters delete mode, checkboxes appear, Cancel + "Delete 0 selected" buttons
3. Check individual rows → count updates, row highlights
4. Header checkbox → select all / deselect all
5. Click row in delete mode → toggles checkbox (no expand)
6. "Delete N selected" → modal shows "Delete N tracks? Associated files will also be removed."
7. Confirm → tracks deleted, mode exits, rows removed
8. Single delete from expand row still works (uses same API)
9. Cancel → exits mode, checkboxes cleared

- [ ] **Step 9: Commit**

```bash
git add src/static/index.html
git commit -m "feat: add bulk delete mode UI with toggle, checkboxes, and confirmation modal"
```
