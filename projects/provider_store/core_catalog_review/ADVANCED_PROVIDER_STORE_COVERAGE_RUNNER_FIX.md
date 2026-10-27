# Advanced Provider Store — Coverage Runner Regression

Date: 2026-10-27

## Defect reproduced and fixed in the reviewed local archive

The shipped `tools_coverage.py` failed on Python 3.13 because `trace._find_executable_linenos()` may return `None` entries and the script compared each entry with integer zero.

Fix: filter entries to integers in the valid source-line range before evaluating the `pragma: no cover` marker.

## Retest evidence

- `python tools_coverage.py 95`: PASS; 99 tests, 0 failures/errors; 825/828 lines = 99.6%.
- `python -W error -m unittest discover -s tests`: PASS; 99 tests, 0 failures/errors.
- `python -W error -m compileall -q advps tests`: PASS.
- Rebuilt 37-entry ZIP; `ZipFile.testzip()` returned no corrupt member.
- Recomputed SHA-256 manifest after the fix.

This fix is present in the reviewed local archive, not yet in the repository source tree. The current PR is documentation-only and does not claim to contain this archive or the code fix.
