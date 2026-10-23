# OPSiB backend assessment: receipt shortfalls from source data to verified tasks

A small FastAPI + PostgreSQL service that imports the retailer's ERP, WMS, crosswalk, HRMS
and workforce exports, validates and reconciles them into canonical identities and base
units, detects receipt shortfalls with a deterministic rule, routes an investigation task
to an eligible employee, and records the task's lifecycle with an append-only audit trail.

* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): modules, data model, identity rules, the
  rule, routing, workflow, transaction boundaries.
* [docs/API.md](docs/API.md): API contract. [docs/API_EXAMPLES.md](docs/API_EXAMPLES.md): real
  request/response pairs. [docs/openapi.json](docs/openapi.json): OpenAPI export.
* [docs/TEST_REPORT.md](docs/TEST_REPORT.md): test command, output and coverage of the
  required cases.

## Result on the supplied fixtures

Evaluated at the frozen time 2026-10-16 15:00 Asia/Kolkata (`make demo`):

| PO line | Outcome | Why |
|---|---|---|
| PO-501 | **Shortfall 8 EA** (92 of 100) | two GRNs, 60 + 32, summed per PO line |
| PO-502 | no exception | 40 of 40 received by the deadline |
| PO-503 | not due | deadline 2026-10-17 18:00 has not passed; 20 of 50 so far is recorded |
| PO-504 | not eligible | PO line is CANCELLED |
| PO-505 | **Shortfall 24 EA** (96 of 120) | 10 BOX × 12 EA/BOX = 120 EA, compared in EA |
| PO-506 | **Shortfall 10 EA** (10 of 20) | the repeated receipt record is one fact, counted once |

Tasks: PO-501 and PO-506 (STORE-014) → **HR-102**; HR-101 is OFF_SHIFT and HR-103 is
INACTIVE although the shift export says ON_SHIFT. PO-505 (STORE-015) → **HR-401**, the
on-shift store manager, as the approved fallback (no receiving associate there). Each
choice is stored with every candidate considered and why.

Duplicates reported at import: one identical PO row and one identical receipt row
(`DUPLICATE_IN_FILE`). No rows rejected, no conflicts, no open data-quality issues.

## Running it

Requirements: Python 3.12 and [uv](https://docs.astral.sh/uv/), PostgreSQL 17 (16 should
work). Everything below runs from a clean checkout.

```bash
# 1. A database (pick one)
scripts/dev_postgres.sh start        # uses local initdb/pg_ctl binaries, port 55433, no Docker
docker compose up -d db              # or: PostgreSQL 17 in Docker on port 55433
                                     # (the compose file was not exercised here: no Docker on the build machine)

# 2. Install and migrate
uv sync
uv run alembic upgrade head          # OPSIB_DATABASE_URL defaults to ...@127.0.0.1:55433/opsib

# 3. See the result
uv run python -m app.cli demo        # load fixtures/opsib_v1, reconcile, print the table above

# 4. Or run the API (http://127.0.0.1:8000/docs)
uv run uvicorn --factory app.api.main:create_app --port 8000
```

Driving the API by hand:

```bash
for d in product_aliases location_aliases employees workforce_status purchase_order_lines goods_receipt_lines; do
  curl -s -F "file=@fixtures/opsib_v1/$d.csv" localhost:8000/imports/$d | jq '{dataset, status, counts}'
done
curl -s -X POST localhost:8000/reconciliation-runs | jq '.summary.detection.outcomes'
curl -s localhost:8000/exceptions | jq '.[] | {po: .po.po_number, variance_qty, unit, status}'
TASK=$(curl -s 'localhost:8000/tasks?assignee_id=HR-102' | jq -r '.[0].id')
curl -s -X POST -H 'X-Actor-Id: HR-102' localhost:8000/tasks/$TASK/acknowledge | jq .status
curl -s -X POST -H 'X-Actor-Id: HR-102' localhost:8000/tasks/$TASK/start | jq .status
curl -s -X POST -H 'X-Actor-Id: HR-102' -H 'Idempotency-Key: k-1' -H 'Content-Type: application/json' \
  -d '{"resolution_code":"SUPPLIER_CONFIRMED_SHORT_DISPATCH","evidence_ref":"ASN-123"}' \
  localhost:8000/tasks/$TASK/complete | jq .status
curl -s -X POST -H 'X-Actor-Id: HR-201' localhost:8000/tasks/$TASK/verify | jq .status
```

Tests (the suite needs its own database whose name ends in `_test`; it recreates the schema):

```bash
TEST_DATABASE_URL=postgresql+psycopg://opsib@127.0.0.1:55433/opsib_test uv run pytest    # 253 tests
uv run pytest tests/unit                                                                  # no database needed
```

`make install db-up migrate demo run test lint openapi` wraps the same commands.

### Configuration

All settings are environment variables prefixed `OPSIB_` (or a `.env` file; see
[`.env.example`](.env.example)).

| Variable | Default | Meaning |
|---|---|---|
| `OPSIB_DATABASE_URL` | `postgresql+psycopg://opsib@127.0.0.1:55433/opsib` | SQLAlchemy URL |
| `OPSIB_TENANT_ID` | `tenant-demo` | tenant written on every row |
| `OPSIB_CLOCK_MODE` | `frozen` | `frozen` uses `OPSIB_FROZEN_TIME`; `system` uses the wall clock |
| `OPSIB_FROZEN_TIME` | `2026-10-16T15:00:00+05:30` | the fixture's evaluation time (must carry an offset) |
| `OPSIB_BUSINESS_TIMEZONE` | `Asia/Kolkata` | used only to render explanation text |
| `OPSIB_SHORTFALL_TOLERANCE_BASE_QTY` | `0` | shortfall when variance > tolerance (base units) |
| `OPSIB_AVAILABILITY_MAX_AGE_MINUTES` | `60` | how old a shift sample may be and still count |
| `OPSIB_TASK_DUE_HOURS` | `24` | task due time after it opens |
| `OPSIB_MAX_UPLOAD_BYTES` | `5000000` | larger uploads get 413 |
| `OPSIB_AI_PROVIDER` | `disabled` | `claude` enables the advisory summary (`uv sync --extra ai` + Anthropic credentials) |
| `OPSIB_AI_TIMEOUT_SECONDS` | `15` | summary call timeout |
| `OPSIB_ANTHROPIC_MODEL` | `claude-opus-5-5` | model for the summary |
| `OPSIB_LOG_LEVEL` | `INFO` | JSON-lines logs on stdout |
| `TEST_DATABASE_URL` | `…/opsib_test` | tests only |

### Layout

```
app/
  ingestion/   CSV parsing, typed payloads, source profiles, idempotent import, conflict review
  mapping/     crosswalks, unit conversion, status vocabularies, canonical projections
  detection/   rules.py (pure rule), explanations.py, service.py (persist findings)
  routing/     policy.py (pure eligibility/selection), service.py (tasks + assignment)
  workflows/   state machine, task actions, idempotency, audit records
  ai/          optional advisory summary behind a provider interface
  api/         FastAPI app, routes, request schemas
  db/          models, session, advisory locks
  pipeline.py  one reconciliation run      read_models.py  read views     cli.py
migrations/    Alembic (0001: schema, constraints, partial unique index, append-only triggers)
fixtures/      opsib_v1/ the supplied files, unchanged; SHA256SUMS checked by a test
tests/         unit/ (no DB) and integration/ (PostgreSQL)
```

## The two design questions

### How a second import or a concurrent worker cannot produce a different result

* Every source fact has a natural identity, `UNIQUE (tenant_id, source_system,
  source_record_key)`. A row is classified against what is stored: same key and same
  content hash → `UNCHANGED`; same key, different content → `CONFLICT` (stored fact kept);
  new key → `INSERTED`. The content hash is over typed values, so `100` and `100.0`, or the
  same instant written in two offsets, are the same content.
* A whole file loads in **one transaction** that first takes
  `pg_advisory_xact_lock(tenant:import:dataset)` and then locks the facts it touches
  `FOR UPDATE`. A concurrent import of the same dataset waits for the first to commit and
  then sees its rows, so it classifies them `UNCHANGED`. If anything bypassed the lock, the
  unique constraint would reject the second insert and that import would roll back and be
  marked `FAILED`; nothing half-loaded is ever visible.
* Quantities are never accumulated at import time. Detection sums *distinct facts*, so a
  re-import has nothing to add. `test_concurrent_imports_of_the_same_file_store_each_record_once`
  runs four imports at once and gets 6 facts, one import with 6 inserts and three with 6 unchanged.
* Detection and routing run under `pg_advisory_xact_lock(tenant:reconcile)` **and** the
  *shared* form of every dataset's import lock, so no import or conflict decision can commit
  while a run is reading: a run never sees a receipt feed that looks fresh while its receipts
  are not yet visible (`test_an_import_cannot_land_in_the_middle_of_a_detection_run`).
* Exceptions are inserted `ON CONFLICT (tenant_id, case_key) DO NOTHING`; tasks
  `ON CONFLICT DO NOTHING` against a partial unique index that allows one *active* task per
  (exception, task type). The router locks the open exceptions before looking at their tasks,
  so a verification committing at the same moment cannot leave a resolved case with a fresh
  task (`test_router_does_not_reopen_work_on_a_case_being_resolved`).
  `test_concurrent_runs_create_one_exception_and_one_task_per_case` runs five reconciliations
  at once and gets 3 exceptions, 3 tasks, 3 DETECTED events.

### What happens when a client retries a completion that already committed

Implemented in `app/workflows/service.py`, tested in `tests/integration/test_task_workflow.py`.

* **With an `Idempotency-Key`** (the intended client behaviour): the key, a fingerprint of
  (action, task, actor, body) and the response are written **in the same transaction** as the
  state change. The retry finds the key and returns the stored response with
  `Idempotent-Replay: true`; no event is added and the version does not move. The same key
  with a different body is refused (`422 IDEMPOTENCY_KEY_REUSED`). Two copies of the request
  in flight at once: the second blocks on the key's unique index until the first commits,
  then replays it (`test_concurrent_duplicate_completions_apply_once`: four threads, one
  COMPLETED event). A request that failed stored nothing and can simply be retried.
* **Without a key**: if the actor's most recent action on the task is this exact request and
  nothing has happened since (the event records the request fingerprint and the version it
  produced), the retry is acknowledged as a replay. Otherwise the request is judged afresh:
  a different body, or a retry after verification, gets `409 INVALID_TRANSITION`; a retry
  after the submission was rejected gets `409 RESUBMISSION_UNCHANGED`. The server cannot
  give exactly-once without the key, so clients should always send one.
* `expected_version` gives optimistic concurrency for clients that want it
  (`409 VERSION_CONFLICT`).

## Other questions the walkthrough lists

**Source IDs that change or collide.** One source record = one fact. A re-send with changed
content does not overwrite: it opens a `source_conflicts` row, marks the fact `CONFLICT`, and
the detector treats it as not evaluable (a data-quality issue, never an exception) until a
data steward accepts the new version or keeps the old one (`POST
/data-quality/conflicts/{id}/resolve`). Deciding one conflict never closes another nobody
reviewed: after an acceptance the others are re-based onto the new version; a re-send is
treated as "already reviewed" only if a steward rejected that exact change for the record's
current version. The fixtures carry no version or last-modified field, so the service cannot
tell a correction from a bad replay; with a source change sequence it would accept strictly
newer versions automatically. Two *different* source IDs claiming the same PO line, or the
same GRN line anywhere in the tenant, make every line they touch not evaluable.

**Late receipt, repeated record, partial failure, ambiguous mapping.** A late receipt is a
new fact; the next run recomputes the line: a shortfall that closes becomes `CLEARED` and its
open task is cancelled, a partial one updates the same case (`EVIDENCE_UPDATED`, before and
after), and new evidence after a verified resolution reopens it with a new task. Receipts
dated after the evaluation time are not counted until they are in the past. A repeated record
is one fact. A failed import rolls back entirely and is marked `FAILED`; rejected rows are a
normal partial success, reported row by row. A rejected receipt row stays outstanding until
that record is validly re-sent or a steward dismisses it: while it names a PO line (read
through its export profile, positionally if it has a stray delimiter) that line is blocked
rather than allowed to look short, and if it names no readable line every line is blocked.
An ambiguous or missing mapping makes
the line `NOT_EVALUABLE` with an issue in `/data-quality`; once the alias arrives, the next
run evaluates it and the issue resolves itself.

**Two workers, one business issue.** See the first design question: advisory lock for
serialisation, `UNIQUE (tenant_id, case_key)` and the partial unique index on active tasks as
the guarantee, both used with `ON CONFLICT DO NOTHING`. Task actions lock the exception row
and then the task row, the same order the detector uses, so the two cannot deadlock.

**Why a receipt shortfall and not a supplier fault.** The data shows the PO ordered N and
the WMS has posted fewer than N for that line after the receiving window closed. There is
no supplier dispatch or ASN data and no carrier proof of delivery, so the shortage could be a
short dispatch, a transit loss, a mis-delivery, a receipt not yet posted or posted against
another line, or a store counting error. The finding says exactly what is known, lists what
is unknown and the evidence that would settle it, and states that it does not establish
fault or loss. Cause is recorded later, by a person, as a resolution code with an evidence
reference, and only counts once a second person verifies it.

**Adding another retailer's export.** Register a `SourceProfile` (dataset, source system,
column map) and, if its status codes differ, a vocabulary entry for its source system. The
payload models, fact tables, mapping and rule do not change
(`tests/integration/test_second_retailer_profile.py`).

**Before accepting data from several tenants.** See *Production gaps* below; the schema
already carries `tenant_id` everywhere and every uniqueness constraint and lock includes it,
but the tenant comes from configuration and the actor from a header.

## Assumptions and interpretations

* **Deadline**: actionable only when the evaluation time is *after* the expected-receipt
  time. At exactly the deadline the window is still open.
* **Late receipts count.** A receipt after the deadline but before the evaluation time
  reduces the shortfall (the goods arrived) and is marked `on_time: false`. Lateness would be
  a separate rule.
* **Receipt evidence must cover the window.** No shortfall is raised without at least one
  completed receipt import extracted at or after the line's deadline. `source_extracted_at`
  defaults to the business clock when the file arrives; a source that supplies its extract
  time should pass it.
* **Posting**: only `POSTED` counts. `REVERSED`/`CANCELLED` count nothing and are listed;
  `PENDING`/`DRAFT` or an unknown status block the line rather than guess.
* **Receipts link** to PO lines by the PO number and line they carry, and must agree with
  the line on canonical product and location. PO numbers are assumed unique within the
  tenant's ERP; with several ERPs the receipt export would have to name the ERP.
* **A quantity already in the base unit** converts with factor 1 even if the alias declares
  another source unit for that item; any other unit is refused.
* **Codes** (statuses, units) are case-insensitive and normalised to upper case; IDs are
  case-sensitive and only trimmed.
* **Routing**: requiring ON_SHIFT is the policy, for automatic routing and manual
  reassignment alike; a sample older than 60 minutes, dated after the evaluation time, or with
  the evaluation time outside its shift window does not count. Regional operations staff are
  not auto-assigned store work; they supervise within their region. A task whose assignee
  becomes inactive, moves store or leaves the roster is released and re-routed on the next
  run, and that person can no longer act on it; going off shift does not take work away.
  If the ERP moves a PO line to another store, its open task follows it.
* **Data stewards** (who decide source conflicts and dismiss rejected rows) are active store
  managers and regional operations staff. In production this would be a separate permission.
* **Verification** is always by a second person: a store manager of the location, regional
  operations for the region, or the assignee's HRMS manager. That is why HR-401's own
  STORE-015 task is verified by HR-501.
* **Employee region** in HRMS that disagrees with the location crosswalk is reported; the
  crosswalk is used.
* **Clock**: every business decision uses the injected clock; wall-clock time appears only in
  operational `created_at` / `recorded_at` columns.

## Trade-offs and what is intentionally left out

* **Synchronous processing.** Imports and runs execute inside the request. The import record
  already has a `PROCESSING` state and each step has its own transaction, so moving them to a
  worker (and returning `202` with the import ID) does not change the design. Large files
  would also need batched inserts rather than one ORM object per row.
* **Canonical model as a projection** rebuilt per run (see ARCHITECTURE.md). Simple and
  always consistent with the current aliases; at scale it would be materialised
  incrementally.
* **Full-table loads per run.** Detection reads all PO and receipt facts of the tenant. Fine
  for a pilot; production would evaluate only lines touched since the last run plus lines
  whose deadline passed.
* **One rule, one task type.** The structure (rule version, case key, task type, policy
  version) is there for more, but only the shortfall rule and the investigation task exist.
* **No UI, no auth service, no queue, no cache.**
* **Conflict policy is manual** because the sources carry no change sequence, and rejected
  rows clear only by a valid re-send or an explicit dismissal, because a clean later file may
  be a delta that says nothing about them.
* **Runs and imports exclude each other** (shared/exclusive advisory locks). Simple and safe
  at pilot volume; at scale, detection would read from a snapshot and imports would not wait.
* **An import whose process dies** between registering and loading stays `PROCESSING`; a
  production sweeper would mark stale ones `FAILED`.
* **The Claude provider** (`app/ai/investigation.py`) is written against the Anthropic SDK and
  tested with a stub client only. No live API call was made.

## Production gaps (documented, not implemented)

* **Tenant isolation and authorisation.** Today the tenant comes from configuration and the
  actor from `X-Actor-Id`. Production needs an authenticated principal (OIDC/JWT) carrying
  tenant and employee identity, the tenant taken from the token only, PostgreSQL row-level
  security on `tenant_id` as a second wall, and role checks for data stewards (conflict
  resolution) and integration clients (imports).
* **Secrets and integration credentials** from a secret manager, per tenant and per source
  system, rotated; never in `.env` or the repository. Source pulls (SFTP/API) would replace
  manual upload, with the source's extract timestamp recorded.
* **Migrations** applied by a release job (`alembic upgrade head`) before the app rolls out,
  with backwards-compatible steps. Only one migration exists here.
* **Monitoring**: the JSON logs carry request IDs, import/run/task IDs and counts. Production
  would add metrics (import outcomes, rejected-row rate, open issues, run duration, tasks in
  the manual queue, overdue tasks), alerts on FAILED imports/runs and stale feeds, and tracing.
* **Retention**: raw import rows keep source payloads (including employee display names)
  indefinitely here. Production needs a retention period for raw rows, minimisation of
  personal data in facts, and audit-event retention matching the retailer's policy.
* **Scale**: background workers, batched upserts, incremental evaluation, pagination on list
  endpoints, connection pooling settings.

## AI tooling disclosure

* **Tool**: Claude Code (Anthropic's coding agent, model Claude Opus 5.5). It read the brief
  and fixtures, designed and wrote the implementation, migration, tests and documentation,
  ran PostgreSQL and the test suite locally, and fixed the defects the tests and lint found.
  A second, independent Claude review pass then found nine correctness and concurrency
  defects (among them a routing race, a mixed-snapshot read during detection, and conflict
  de-duplication that could hide an unreviewed re-send). Each was fixed and covered by a
  regression test; the two concurrency tests were confirmed to fail with their fix removed.
* **At runtime** no AI is required: `OPSIB_AI_PROVIDER=disabled` is the default and no API
  calls were made while building or testing.
* **Reviewed or changed independently by the candidate**: _to be completed by the candidate
  before submission._
