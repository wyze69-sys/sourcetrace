# Current Project State: SourceTrace

## 1. Project Phase
- **Current Stage**: Storage & Grounded Retrieval Architecture — Source-Coordinate Citation Navigation & Real Browser E2E Acceptance (ACCEPT-NAV-001-FIX Completed)
- **Active Goal**: Production-quality retrieval, intent-aware evidence ranking, strict citation grounding, and real browser UI chat/navigation verification.

## 2. Capabilities & Verified Behaviors
- **Source-Coordinate Citation File & Line Navigation (ACCEPT-NAV-001 / ACCEPT-NAV-001-FIX — COMPLETED)**:
  - Clicking a chat citation in `App.tsx` sets `focusedCitation` state (`filePath`, `startLine`, `endLine`) and switches to `Files` section (`activeSection = 'files'`).
  - `<RepoExplorerPanel>` accepts `targetFilePath` and `targetLineRange` props.
  - On `targetFilePath` change, `RepoExplorerPanel` expands parent folders, selects `targetFilePath`, and fetches file content via `getRepositoryFileContent`.
  - `CodeViewer` renders `.cited-line-highlight` on lines in range `[start_line, end_line]` with `data-testid="cited-code-line"`, `.cited-line-number-highlight` in gutter with `data-testid="cited-line-number"`, and automatically scrolls the start line into view.
  - `CodeViewer` evaluates `isCitationLineUnavailable`. If cited lines are out of bounds or unavailable, it renders an honest notice (`data-testid="citation-line-unavailable-notice"`), omits false line highlighting, and prevents false navigation claims.
  - Manual file selection clears line highlighting for un-cited files, and selecting another repository resets `focusedCitation` to `null`.
  - `run_real_browser_acceptance.py` updated with robust citation parser (`parse_citation_text`) and 25s timeouts. Both FitSync (`backend/server.js:11-11`) and bottle (`bottle.py:1463-1475`) passed `PATH_NAVIGATION_PASSED` and `LINE_NAVIGATION_PASSED` with overall status `ACCEPTED`.

## 3. Active & Next Tasks
- **Completed**: `PRE-FEATURE-GATE-001`, `PRE-FEATURE-CLEANUP-001`, `REPO-BASELINE-001` (`c737745`), `ACCEPT-NAV-001`, `ACCEPT-NAV-001-FIX`.
- **Next Task**: Select next ready task from `docs/AGENT_TASKS.yaml`.

## 4. Verification Suite Results
- **Frontend Vitest Suite**: `103 passed` across 5 test files — 0 failures.
- **Frontend ESLint**: `0 errors, 4 warnings` — 0 errors.
- **Frontend Build (`tsc -b && vite build`)**: `dist/` built successfully in 1.34s.
- **Backend Pytest**: `1442 passed, 5 skipped` — 0 failures.
- **Real Browser E2E Acceptance Harness (`run_real_browser_acceptance.py`)**: `OVERALL STATUS: ACCEPTED` (FitSync: PATH & LINE PASSED, bottle: PATH & LINE PASSED).
- **Git diff --check**: 0 whitespace errors.
