# Last Handoff

**Task**: REPO-001 Phase 7 — Repository-staleness gaps for Flow Trace and Impact
**Status**: completed
**Date**: 2026-07-27

---

## Files Changed

- `backend/src/sourcetrace/retrieval/trace.py` — Updated `FlowTraceService.trace(...)` to accept `repository: RepositoryRecord | None = None`, append `repo_stale` gap when `repository.is_stale is True`, and optimize `stale_index` detection via authoritative `repository.parser_versions`
- `backend/src/sourcetrace/retrieval/impact.py` — Updated `ChangeImpactService.impact(...)` and `preview_diff(...)` to accept `repository: RepositoryRecord | None = None`, append `repo_stale` gap when `repository.is_stale is True`, and optimize `stale_index` detection via `repository.parser_versions`
- `backend/src/sourcetrace/api/routes/trace.py` — Passed `repository=repo` into `service.trace(...)`
- `backend/src/sourcetrace/api/routes/impact.py` — Passed `repository=repo` into `service.impact(...)` and `service.preview_diff(...)`
- `docs/api/v1/trace-impact.md` — Documented `repo_stale` gap kind in Flow Trace and Change Impact contract specifications
- `backend/tests/retrieval/test_repo_stale_gaps_phase7.py` — Added unit and integration test suite covering `repo_stale` gaps, fresh/unknown handling, and legacy metadata fallback

---

## Behavior Added / Changed

- **`repo_stale` Gap Detection**: When `repository.is_stale is True`, Flow Trace and Change Impact (symbol and diff preview) append a `repo_stale` gap. Detail text includes the indexed commit SHA and last-indexed timestamp when present (e.g. `Repository index is out of date (indexed commit abc1234; last indexed 2026-07-27T12:00:00+00:00); refresh repository to update.`).
- **`stale_index` Optimization**: When `repository.parser_versions` is non-empty and `flow_evidence_complete` is `True`, `stale_index` gap scanning is bypassed. If `flow_evidence_complete` is `False`, only actual outdated chunks trigger `stale_index`.
- **Legacy Fallback**: Legacy repositories with empty `parser_versions` retain chunk-level `stale_index` checks and are not falsely marked stale merely because `flow_evidence_complete` defaults `False`.
- **Preserved Invariants**: Zero-token static mode, confidence scores, symbol citations, explain-mode degradation, and ownership controls are unchanged.

---

## Commands Run & Results

```
pytest tests/retrieval/test_repo_stale_gaps_phase7.py  -> 3 passed
pytest (backend)                                       -> 0 failures (all passed)
npm test -- --run (frontend)                           -> 67 passed (4 test files)
npm run build (frontend)                               -> 32 modules, tsc -b clean
ruff check src/ tests/                                 -> All checks passed!
git diff --check                                       -> 0 whitespace errors
```

---

## API / Schema Impact

- Flow Trace and Change Impact responses may include gap kind `repo_stale`.
- Documented in `docs/api/v1/trace-impact.md`.

---

## Security Considerations

- Flow Trace and Change Impact analysis remains owner-isolated and deterministic.
- Zero external calls made during static mode trace/impact analysis.
