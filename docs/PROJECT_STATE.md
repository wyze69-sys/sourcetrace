# Current Project State: SourceTrace

## 1. Project Phase
- **Current Stage**: Storage & Grounded Retrieval Architecture — Real Browser E2E Acceptance (RUNTIME-ACCEPT-005 Partial Pass)
- **Active Goal**: Production-quality retrieval, intent-aware evidence ranking, strict citation grounding, and real browser UI chat/navigation verification.

## 2. Capabilities & Verified Behaviors
- **Real Browser UI Chat (RUNTIME-ACCEPT-005 — PARTIAL PASS)**: Executed corrected Playwright harness (`backend/tests/run_real_browser_acceptance.py`) against live frontend (`http://127.0.0.1:5173`) and FastAPI backend (`http://127.0.0.1:8000`).
  - **Chat UI ACCEPTED**: Both FitSync and bottle scenarios produced visible, rendered assistant bubbles with correct citations; all 6 screenshots validated (56–91 KB, non-blank, at 1440×900 viewport).
  - **PATH_NAVIGATION_NOT_IMPLEMENTED**: Citation click handler (`App.tsx` line 1189–1191) only calls `setActiveSection('files')`. The cited file path is NOT passed to `RepoExplorerPanel`. The Files tab opens but no file is auto-selected/opened.
  - **LINE_NAVIGATION_NOT_IMPLEMENTED**: `RepoExplorerPanel` has no `selectedLine`, `highlightLine`, or scroll-to-line prop. No line-level selection or highlight behavior exists in the current product.
- **Harness Corrections Applied (vs RUNTIME-ACCEPT-004)**:
  - All element assertions now use `expect(locator).to_be_visible()` (Playwright visible locators, not DOM-existence checks).
  - Viewport set to 1440×900.
  - Screenshots validated for minimum byte count (≥ 10 KB threshold).
  - Token injection corrected from `localStorage.setItem('sourcetrace_access_token', ...)` to `sessionStorage.setItem('sourcetrace.access_token', ...)` matching `apiClient.ts` `TOKEN_STORAGE_KEY`.
  - Citation navigation reported honestly with product-gap labels (`PATH_NAVIGATION_NOT_IMPLEMENTED`, `LINE_NAVIGATION_NOT_IMPLEMENTED`).
- **RUNTIME-ACCEPT-004 Rejected**: All 6 prior screenshots were identical blank 2,791-byte images; DOM-existence assertions, wrong token storage key, and gutter-existence accepted as line-navigation passed.

## 3. Local Artifact Verification (RUNTIME-ACCEPT-005)
All 7 artifacts created in `backend/tests/artifacts_accept_005/`:
- `real_browser_acceptance_report.json` — 6,494 bytes, UTC timestamp `2026-07-29T16:42:02Z`
- `fitsync_01_question_typed.png` — 56,634 bytes (VALID)
- `fitsync_02_response_rendered.png` — 86,985 bytes (VALID, shows "Investigation Result" answer with [E1]...[E5] evidence citations)
- `fitsync_03_citation_clicked.png` — 36,145 bytes (VALID, shows Files tab opened with "Repository Explorer: FitSync / Loading repository files...")
- `bottle_01_question_typed.png` — 59,868 bytes (VALID)
- `bottle_02_response_rendered.png` — 91,050 bytes (VALID, shows bottle auth answer citing `BaseRequest.auth`, `parse_auth`, `auth_basic`)
- `bottle_03_citation_clicked.png` — 35,474 bytes (VALID, shows Files tab opened)

## 4. Active & Next Tasks
- **Completed**: `RUNTIME-ACCEPT-001` through `RUNTIME-ACCEPT-004` (004 was rejected, replaced by 005).
- **RUNTIME-ACCEPT-005**: PARTIAL PASS — chat UI accepted; citation navigation (path + line) not implemented.
- **Next Required Task**: Implement citation-click path navigation (`App.tsx` → `RepoExplorerPanel` with `selectedFilePath` prop) and citation line navigation (scroll-to / highlight start line in `CodeViewer`), then re-run acceptance to achieve full ACCEPTED status.

## 5. Verification Suite Results
- **Real Browser E2E Suite (RUNTIME-ACCEPT-005)**: PARTIAL PASS (Exit Code 0 — chat UI passes).
- **Backend Pytest**: `1442 passed, 5 skipped, 4 warnings` — 0 failures.
- **Grounding Regression Suite**: `5 passed, 0 failed`.
- **Offline Grounded Eval Suite**: `7/7 scenarios passed (100%)`.
- **Ruff check**: `All checks passed!` — 0 errors.
- **Git diff --check**: 0 whitespace errors.

## 6. Known Product Gaps (Citation Navigation)
| Gap | Location | Detail |
|-----|----------|--------|
| Path navigation | `App.tsx` L1189–1191 | Citation `onClick` only calls `setActiveSection('files')`; cited path not passed to `RepoExplorerPanel` |
| Line navigation | `RepoExplorerPanel.tsx` | No `selectedLine`/`highlightLine`/`scrollToLine` prop; gutter shows all lines sequentially |
