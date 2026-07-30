# Last Handoff: RUNTIME-ACCEPT-005 — Repair False-Positive Browser Evidence

**Task**: RUNTIME-ACCEPT-005
**Status**: partial

---

## Task Summary

Repaired the RUNTIME-ACCEPT-004 acceptance harness, which produced false-positive results. Re-executed against live servers and produced truthful browser evidence. Reported two concrete product gaps.

---

## 1. Evidence of RUNTIME-ACCEPT-004 Failure (Root Causes)

| Failure | Evidence |
|---------|----------|
| Blank screenshots | All 6 prior screenshots in `artifacts_accept_003/` are 2,791 bytes each — identical blank/minimal PNGs |
| Wrong token storage | `add_init_script` set `localStorage.setItem('sourcetrace_access_token', ...)` but `apiClient.ts` reads `sessionStorage.getItem('sourcetrace.access_token')` — wrong storage API and wrong key |
| DOM-existence assertions | `query_selector(...) is not None` passes even when element is off-screen or zero-size |
| False line-navigation pass | Asserted "some `.line-number` elements exist" — which is true whenever any file is open, regardless of cited range |
| No viewport set | Default headless viewport (800×600) produced tiny/blank screenshots |

---

## 2. Harness Corrections Applied

**File**: `backend/tests/run_real_browser_acceptance.py`

| Correction | Detail |
|------------|--------|
| Visible locators | All assertions use `expect(locator).to_be_visible()` (Playwright async assertion) |
| Viewport | Set to 1440×900 on both browser contexts |
| Token injection | `sessionStorage.setItem('sourcetrace.access_token', jwt)` — matches `TOKEN_STORAGE_KEY` in `apiClient.ts` |
| Screenshot validation | Size threshold ≥ 10,000 bytes; verdict recorded in report |
| Citation navigation | Inspected `App.tsx` source: click handler only calls `setActiveSection('files')`. Reported `PATH_NAVIGATION_NOT_IMPLEMENTED`. Reported `LINE_NAVIGATION_NOT_IMPLEMENTED` because `RepoExplorerPanel` has no selectedLine/scrollToLine prop |
| Fresh artifacts dir | `artifacts_accept_005/` — distinct from rejected `artifacts_accept_003/` |
| UTC timestamp | Recorded at actual run start `2026-07-29T16:42:02Z` |

---

## 3. Execution Results

**Command**: `cd backend && uv run python tests/run_real_browser_acceptance.py`
**Exit code**: 0 (partial pass)
**Actual timestamp**: `2026-07-29T16:42:02Z`

### Scenario 1 — FitSync UI Chat
| Assertion | Result |
|-----------|--------|
| FitSync repo card visible | PASSED |
| Chat input visible | PASSED |
| Ask button visible | PASSED |
| Question typed + screenshot (56,634 bytes) | VALID |
| New assistant bubble visible | PASSED |
| Response screenshot (86,985 bytes) | VALID — shows "Investigation Result" answer with [E1]–[E5] evidence citations |
| Citations rendered: 5 | PASSED |
| Central files cited (server.js, app.js, routes) | PASSED |
| Citation button visible | PASSED |
| Files tab opened after click | PASSED |
| Code viewer auto-opened to cited path | PATH_NAVIGATION_NOT_IMPLEMENTED |
| Cited line highlighted/visible | LINE_NAVIGATION_NOT_IMPLEMENTED |
| Citation screenshot (36,145 bytes) | VALID — shows Files tab "Repository Explorer: FitSync / Loading repository files..." |

### Scenario 2 — bottle UI Chat
| Assertion | Result |
|-----------|--------|
| bottle repo card visible | PASSED |
| Chat input visible | PASSED |
| Ask button visible | PASSED |
| Question typed + screenshot (59,868 bytes) | VALID |
| New assistant bubble visible | PASSED |
| Response screenshot (91,050 bytes) | VALID — shows bottle auth answer citing `BaseRequest.auth`, `parse_auth`, `auth_basic` |
| Citations rendered: 5 | PASSED |
| Startup citation violation | NONE |
| Genuine auth evidence | PASSED |
| Citation button visible | PASSED |
| Files tab opened after click | PASSED |
| Code viewer auto-opened to cited path | PATH_NAVIGATION_NOT_IMPLEMENTED |
| Cited line highlighted/visible | LINE_NAVIGATION_NOT_IMPLEMENTED |
| Citation screenshot (35,474 bytes) | VALID |

### Console & Network Health
- Browser console errors: 0
- Failed network requests: 0

---

## 4. Overall Verdict

| Area | Result |
|------|--------|
| Chat UI flows (question → answer → citations rendered) | **ACCEPTED** |
| Path navigation (citation auto-opens file) | **NOT IMPLEMENTED — product gap** |
| Line navigation (citation scrolls to / highlights cited line) | **NOT IMPLEMENTED — product gap** |
| **Full citation-location acceptance** | **NOT ACCEPTED** |

---

## 5. Product Gaps Identified

### Gap 1: Citation path navigation not implemented
- **Location**: `frontend/src/app/App.tsx` lines 1189–1191
- **Root cause**: `onClick` for citation buttons calls only `setActiveSection('files')`. The cited `relative_path` is available on the citation object but is not passed to `RepoExplorerPanel`.
- **Fix required**: Pass `openFilePath: string | null` prop to `RepoExplorerPanel` and call `loadFileContent(repositoryId, path)` on mount when the prop is set.

### Gap 2: Citation line navigation not implemented
- **Location**: `frontend/src/app/RepoExplorerPanel.tsx`
- **Root cause**: `RepoExplorerPanel` has no `selectedLine`, `highlightLine`, or `scrollToLine` prop. The `CodeViewer` renders all lines sequentially in a gutter but does not mark any line as selected.
- **Fix required**: Add `selectedLine: number | null` prop; highlight the matching `.line-number` element; scroll it into view on mount.

---

## 6. Files Changed

| File | Change |
|------|--------|
| `backend/tests/run_real_browser_acceptance.py` | Full harness rewrite — visible locators, correct token injection, screenshot validation, honest navigation reporting |
| `docs/PROJECT_STATE.md` | Updated to reflect RUNTIME-ACCEPT-005 partial pass and product gaps |
| `docs/LAST_HANDOFF.md` | This file |

---

## 7. Files NOT Changed

No retrieval, generation, ranking, schemas, credentials, database data, or production behavior was changed. All modifications are confined to the test harness and documentation.

---

## 8. Security Check

Reviewed `real_browser_acceptance_report.json` — no owner session IDs, JWTs, cookies, or secrets appear in plain text. All sensitive values are redacted via `redact_secrets()`.

---

## Recommended Next Task

Implement citation-click file path navigation and line-level navigation in the frontend, then re-run `RUNTIME-ACCEPT-005` to achieve full ACCEPTED status. Suggested task ID: `ACCEPT-NAV-001`.
