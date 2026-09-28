# AGENTS.md

Conventions for the Sentinel project (real-time log anomaly detector over a simulated
Medicaid claims platform). The spec lives in `docs/sentinel-plan.md`; it is the source of
truth. This file is the working agreement on how to change the code.

## Ground rules

1. **`docs/sentinel-plan.md` is the spec.** When code and spec disagree, fix the code.
   When the spec is wrong or ambiguous, ask before changing behavior.
2. **Never change the log format (section 5) without asking.** The 46-byte fixed-width
   header is a cross-component contract:
   - byte 0-12 timestamp (13-digit epoch ms), 13 `|`
   - byte 14 level, 15 `|`
   - byte 16-18 service, 19 `|`
   - byte 20-22 component, 23 `|`
   - byte 24-28 code, 29 `|`
   - byte 30-35 latency ms (6 digits, zero padded), 36 `|`
   - byte 37-44 trace id (8 hex), 45 `|`
   - byte 46+ free-text message, newline terminated, ASCII
   Any change to widths, separator positions, or the meaning of a field requires explicit
   approval. A width change invalidates the numpy batch parser offsets, the tests, and any
   pre-generated dump files.
3. **The detector reads only `platform.log`.** Never import from `sim/` inside `detector/`
   and never read `ground_truth.jsonl` from detection code. Shared vocabulary comes from
   `sim/catalog.yaml`, which is a data file, not a code dependency.
4. **Rate changes are never written to the log.** The control API and the fault layer must
   not emit, hint, or encode rate state into any line. The detector infers faults from the
   log alone.
5. **Don't claim numbers we haven't measured.** Speed, detection delay, F1, and PHI claims
   come from `eval/` runs. Say "to be measured" until then.

## Python

- Python 3.12 (`python3.12`). Full type hints on every public function and module-level
  constant. No `Any` unless a third-party shape forces it, and then annotate why.
- Standard library first. Runtime deps are `pyyaml`, `fastapi`, `uvicorn`, `httpx`
  (control API), `numpy` (batch parser), `boto3` (delivery, optional), `pytest`,
  `pytest-asyncio`, `ruff`.
- `ruff` for lint and format. Line length 100. No wildcard imports, no unused imports.
- Modules stay small and single-purpose. If a file grows past ~400 lines, split it along
  the seam that already exists in the spec (reader / parser / rings / detectors / output).
- Errors: raise specific exceptions. The detector side must never crash on bad input
  (section 8.9) - classify and count instead.
- Concurrency: one asyncio loop, explicit task names, bounded queues, no blocking calls
  (boto3, file reads of large blobs) on the loop.
- Determinism matters. Anything seeded (`--seed`) must produce byte-identical output.
  Use `random.Random(seed)` instances per component, never the global `random`.

## Tests

- `pytest`, tests live in `tests/`, named `test_*.py`.
- Every change to the log format, the catalog, the parser, or the generator needs a test.
  Properties to protect:
  - emitted lines: 46-byte header, all 7 separator bytes are `|`, level in `IWE F`,
    13-digit ts, 6-digit latency, 8 hex trace, ASCII only, single trailing `\n`
  - `--seed` determinism: same seed, same bytes
  - reference parser and batch parser produce identical counts (section 8.4)
  - reference parser classifies malformed lines into the 8.9 classes, never raises
  - control API never produces a log line, always appends to `ground_truth.jsonl`
- Run `ruff check .`, `ruff format --check .`, and `pytest` before reporting done.

## Commits

- Small and single-purpose. One logical change per commit, a passing test suite per commit.
- Conventional-ish prefixes: `feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `chore:`.
- Imperative subject line under 72 chars, blank line, then a short body saying why when the
  reason is not obvious. Reference the plan section when relevant, e.g. `(section 6.2)`.
- Never commit: `platform.log`, `ground_truth.jsonl`, dumps, `.env`, AWS credentials,
  `__pycache__`, `.venv`. These are gitignored.
- Never force-push, never rewrite shared history, never commit straight to `main` without a
  reviewable PR.

## Housekeeping

- `PROGRESS.md` is a running checklist. Update it at the end of every task, including
  status, test results, and any deviations from the plan.
- If you deviate from the plan, record the deviation in `PROGRESS.md` with a one-line
  reason. Do not silently drift.
- All sample data is synthetic. No real PHI, no real member data, no real Acentra internals.
  Do not claim HIPAA or HITRUST compliance anywhere; the framing is "designed with these
  controls in mind".
