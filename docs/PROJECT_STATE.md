# Current Project State: SourceTrace

## 1. Project Phase
- **Current Stage**: Storage & Grounded Retrieval Architecture — Source-Coordinate Citation Navigation, Real Browser E2E Acceptance, and Chat Workspace UX Refinement (`RUNTIME-UX-008` completed)
- **Active Goal**: Production-quality retrieval, intent-aware evidence ranking, strict citation grounding, and a clear source-led browser chat experience.

## 2. Capabilities & Verified Behaviors
- **Source-led repository chat workspace redesign (`CHAT-UX-REDESIGN-001` — COMPLETED)**:
  - Understand presents repository context, freshness/index metadata, mode status, refresh/delete controls, and a source-aware composer in a warmer paper/ink visual system.
  - Starter questions remain selectable actions, presented as lens cards with keyboard-visible focus and responsive layout.
  - Conversation messages have distinct user/assistant hierarchy, answer-mode labels, timestamps, loading state, source chips, and evidence-trail grouping.
  - Citation buttons retain path/line accessible names and existing file/line navigation behavior.
- **Normal-chat conversation scrolling (`CHAT-UX-SCROLL-001` — COMPLETED)**:
  - Before the first question, the full prompt hero and starter questions remain available.
  - After a conversation starts, the hero collapses into a compact follow-up context and starter cards are removed from the active view.
  - The evidence trail is a bounded scroll region with a sticky header, and new messages auto-scroll into view.
- **Bottom active-chat composer (`CHAT-UX-COMPOSER-001` — COMPLETED)**:
  - The initial no-message state keeps the composer inside the prompt hero.
  - Once messages exist, the single shared composer renders after the evidence trail in document and visual order.
  - The active composer uses a sticky bottom dock with responsive mobile behavior.
- **Source-coordinate citation file & line navigation (`ACCEPT-NAV-001 / ACCEPT-NAV-001-FIX` — COMPLETED)**:
  - Clicking a chat citation opens the cited file in Files, highlights the cited line range, and honestly reports unavailable coordinates without false highlighting.
  - Real browser acceptance previously passed for FitSync and bottle with `PATH_NAVIGATION_PASSED` and `LINE_NAVIGATION_PASSED`.

## 3. Active & Next Tasks
- **Completed**: `PRE-FEATURE-GATE-001`, `PRE-FEATURE-CLEANUP-001`, `REPO-BASELINE-001`, `ACCEPT-NAV-001`, `ACCEPT-NAV-001-FIX`, `RUNTIME-UX-006`, `RUNTIME-UX-007`, `RUNTIME-UX-008`.
- **Next Task**: Select or create the next uniquely scoped ready task; no duplicate chat implementation is needed.

## 4. Verification Suite Results
- **Frontend Vitest Suite**: `104 passed` across 5 test files — 0 failures.
- **Focused App suite**: `28 passed` — 0 failures.
- **Frontend ESLint**: `0 errors, 4 existing warnings`.
- **Frontend Build (`tsc -b && vite build`)**: production build succeeded; 34 modules transformed.
- **Backend Pytest**: `1442 passed, 5 skipped` — prior verified baseline; backend was not changed by `RUNTIME-UX-008`.
- **Real Browser E2E Acceptance Harness**: prior verified `OVERALL STATUS: ACCEPTED`; not rerun for this React/CSS-only UX refinement.
- **Git diff --check**: 0 whitespace errors; Git reported line-ending normalization warnings only.
