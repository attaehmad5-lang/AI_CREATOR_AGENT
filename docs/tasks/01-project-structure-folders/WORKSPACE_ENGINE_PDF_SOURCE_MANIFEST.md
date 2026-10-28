# Workspace Engine PDF Source Manifest

## Canonical scope

Source: `Workspace_Engine_Source_Code.pdf` (120 pages). The PDF describes a separate `wsengine/` engine and defines exactly 200 numbered feature rows in `FEATURE_STATUS.md`. Its reported distribution is 153 Full and 47 Partial. Those are source-PDF claims, not repository integration status.

## PDF feature order → source modules and tests

| PDF section / feature rows | Source modules | Test suite |
|---|---|---|
| 1. Core Folder Engine (1–20) | `wsengine/folders.py`, `wsengine/core.py`, `wsengine/util.py` | `tests/test_s01_folders.py` |
| 2. Project Explorer & Navigation (21–40) | `wsengine/explorer.py` | `tests/test_s02_explorer.py` |
| 3. File Operations (41–60) | `wsengine/files.py`, `wsengine/formats.py` | `tests/test_s03_files.py` |
| 4. Search (61–80) | `wsengine/search.py` | `tests/test_s04_search.py` |
| 5. AI (81–100) | `wsengine/ai.py` | `tests/test_s05_ai.py` |
| 6. Media (101–120) | `wsengine/media.py` | `tests/test_s06_media.py` |
| 7. Recovery (121–140) | `wsengine/recovery.py`, `wsengine/storage.py` | `tests/test_s07_recovery.py` |
| 8. Security (141–160) | `wsengine/security.py`, `wsengine/errors.py` | `tests/test_s08_security.py` |
| 9. Performance (161–180) | `wsengine/perf.py` | `tests/test_s09_perf.py` |
| 10. Integration & Operations (181–200) | `wsengine/integration.py`, `wsengine/workspace.py` | `tests/test_s10_integration.py` |
| Web/API layer | `web/server.py`, `web/index.html` | `tests/test_web.py` |
| Shared test infrastructure and regressions | `tests/base.py`, `tests/test_regressions.py` | shared |
| Packaging/demo/docs | `wsengine/__init__.py`, `pydroid_demo.py`, `README.md`, `FEATURE_STATUS.md` | manual smoke test / docs review |

Additional engine modules in the PDF file listing: `wsengine/formats.py`, `wsengine/storage.py`, `wsengine/util.py`; see the section mapping above and preserve their imports when integrating.

## Verification record

- Repaired local reconstruction: **225 unittest methods passed** with `python -W error::ResourceWarning -m unittest discover -s tests -p 'test_*.py' -t .`.
- Scope: this is a pass for the repaired local reconstruction only. The PDF's printed code had syntax/indentation and line-wrap defects, and one reconstructed test expectation was corrected.
- Not yet verified: original PDF source as printed runs unchanged; Pydroid 3 execution; production security; repository integration; GitHub Actions on the integrated engine.
- Known PDF limitations that must remain visible in reviews: custom SHAKE-256 + HMAC at-rest encryption (not audited AES-GCM); no built-in TLS or per-client rate limiting; non-production CSRF posture; heuristic AI; EICAR-only malware signature detection; in-memory fake cloud adapter; mobile integrations are server-side adapters, not native apps; in-process locks/limits/metrics.

## Integration rule

Do not mark any of the 200 repository features complete from the PDF's own Full/Partial label. Each feature needs a traceable code path, repository test evidence, CI evidence, and a reviewed commit. Keep the PDF's numbering 1–200 intact; supplemental feature ideas belong in a separate list.
