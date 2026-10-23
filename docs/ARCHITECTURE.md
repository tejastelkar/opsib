# Architecture

A single FastAPI service over PostgreSQL. No queue, no worker process, no cache: the
fixture-sized problem does not need them, and every step is written so it could move
to a background worker later without changing its transaction boundaries.

```
CSV export ──► ingestion ──► source facts ──► mapping ──► canonical model ──► detection ──► exceptions
 (per source    (parse,       (typed, one row   (aliases,    (projection,      (pure rule)     │
  profile)       validate,     per source ID;    units,       rebuilt per run)                 ▼
                 classify)     conflicts kept)   vocab)                                     routing ──► tasks ──► workflow
                                                                                            (policy)              (state machine,
                                                                                                                   audit, verification)
```

## Modules

| Module | Responsibility | Knows about |
|---|---|---|
| `app/ingestion/` | Parse a CSV, validate each row against a typed payload, classify it (inserted / unchanged / duplicate / rejected / conflict), persist raw rows and source facts in one transaction. `datasets.py` declares datasets and **source profiles** (one export format each). `conflicts.py` is the review path for conflicting re-sends. | CSV, payload models, fact tables |
| `app/mapping/` | Turn source facts into canonical records: product and location crosswalks, unit conversion, status vocabularies per source system, employee organisational scope. Pure functions plus a loader. | Facts, alias facts |
| `app/detection/` | `rules.py`: the receipt-shortfall rule, a pure function from canonical records to an `Evaluation`. `explanations.py`: neutral text. `service.py`: runs the rule for every PO line, persists evaluations, exceptions, data-quality issues and exception events. | Canonical model only (never CSV columns) |
| `app/routing/` | `policy.py`: eligibility and selection (pure). `service.py`: one active task per open exception, routes unassigned tasks, records why. | Canonical employees and availability |
| `app/workflows/` | Task state machine, task actions with permission checks, idempotent retries, append-only history. | Tasks, exceptions, canonical org |
| `app/pipeline.py` | A reconciliation run: detect, then route, each in its own locked transaction. | Detector, router |
| `app/ai/` | Optional advisory summary behind a provider interface, with output validation. | Exception evidence only |
| `app/api/`, `app/read_models.py` | HTTP surface and read views. | Services |
| `app/db/` | SQLAlchemy models, session factory, advisory locks. Migrations in `migrations/`. | — |

The detector imports nothing from `app/ingestion`: a new export format is a new
`SourceProfile` mapping its columns onto the same payload, and the rule does not change
(`tests/integration/test_second_retailer_profile.py` loads receipts in a different
column layout and gets the same three findings).

## Data model

Three layers (`app/db/models.py`, `migrations/versions/0001_initial_schema.py`).

### 1. Raw input

| Table | Grain | Notes |
|---|---|---|
| `import_batches` | one uploaded file | dataset, profile, source system, file SHA-256, `source_extracted_at`, status, row counts (a CHECK makes the counts add up), file-level errors and warnings. |
| `import_rows` | one physical line of one file | the raw cells as received (JSONB), source record key, content hash, outcome, reasons (a CHECK requires reasons for REJECTED), link to a conflict. Nothing is dropped. |

### 2. Source facts (validated, in the source's own vocabulary)

One table per dataset, all with `UNIQUE (tenant_id, source_system, source_record_key)`,
`content_hash`, `version`, `review_status` (OK / CONFLICT), `first_import_id`,
`last_import_id`.

| Table | Source record key | Typed business columns |
|---|---|---|
| `po_line_facts` | ERP `source_record_id` | po_number, line_number, supplier_code, location_ref, item_ref, ordered_qty (> 0), ordered_uom, status_code, expected_receipt_by (timestamptz) |
| `receipt_line_facts` | WMS `source_record_id` | receipt_number, receipt_line, po_number, po_line_number, location_ref, product_ref, accepted_qty (≥ 0), uom, received_at, posting_status |
| `employee_facts` | HRMS employee ID | display_name, employment_status, role, location_ref (nullable), manager_ref, region_id |
| `availability_facts` | `employee@observed_at` | availability_status, shift_start < shift_end, observed_at |
| `product_alias_facts` | `system:item_id` | canonical_product_id, source_uom, base_uom, to_base_factor (> 0; = 1 when units equal) |
| `location_alias_facts` | `system:location_code` | canonical_location_id, region_id |
| `source_conflicts` | one re-sent change | both payloads, the import row that brought it, decision (KEEP_EXISTING / ACCEPT_INCOMING / SUPERSEDED), who decided, why, the fact version a KEEP applied to; partial unique index: one *open* conflict per (record, stored hash, re-sent hash) |
| `rejected_row_dismissals` | one rejected row | append-only steward decision that a rejected row needs no correction |

`source_record_key` is the source's own ID where it has one. Workforce samples and alias
rows have none, so their key is derived from their natural identity; two samples for one
employee at different times are two records, not a conflict.

### 3. Business state

| Table | Grain | Key constraints |
|---|---|---|
| `reconciliation_runs` | one run | evaluated_as_of, rule / routing-policy / mapping versions, parameters, summary |
| `rule_evaluations` | one PO line per run | `UNIQUE (run_id, case_key)`; outcome and reasons for every line, including the ones that raised nothing |
| `exceptions` | one business case | `UNIQUE (tenant_id, case_key)`; id = uuid5(tenant, case_key); CHECK variance = expected − received; CHECK a RESOLVED case has a resolution code, evidence ref and resolver |
| `exception_events` | append-only | DETECTED, EVIDENCE_UPDATED, REEVALUATION_BLOCKED/UNBLOCKED, CLEARED, RESOLVED, REOPENED |
| `data_quality_issues` | one (code, subject) | `UNIQUE (tenant_id, issue_key)`; opened, re-seen and auto-resolved by the run step that produced it |
| `tasks` | one piece of work | partial unique index: one *active* task per (exception, task type); CHECKs: assigned states have an assignee, completed states have a resolution and evidence, VERIFIED needs a verifier who is not the completer |
| `task_assignments` | append-only | person **xor** queue, reason code, policy version, the full candidate evaluation, actor, note |
| `task_events` | append-only | every transition with from/to status, actor, business time, wall time, request fingerprint, idempotency key |
| `idempotency_records` | one client key | `UNIQUE (tenant_id, key)`; request fingerprint and stored response |

`exception_events`, `task_events` and `task_assignments` reject UPDATE and DELETE with a
trigger. Every table carries `tenant_id`.

### Why the canonical model is a projection, not a table

Canonical PO lines, receipts and employees are rebuilt from facts and aliases on every
run (`app/mapping/canonicalize.py`). Persisting them would create a second mutable copy
that drifts whenever an alias arrives late or a conflict is resolved. What is persisted is
what was *decided* from them: evaluations, exceptions (with an evidence snapshot) and
issues, each stamped with the mapping version that produced it. At larger volume the
projection would be materialised incrementally, keyed by (fact, mapping version).

## Identity, units and vocabularies

* **Product and location identity** go only through an alias for that exact (source
  system, source ID). No joins on names or similar-looking codes. An alias under an open
  conflict is ambiguous and unused. If two aliases of one canonical product disagree on
  its base unit, or two of one location disagree on its region, every mapping to it is
  refused as an inconsistent crosswalk.
* **Units**: a quantity in the alias's declared unit is multiplied by its factor; a
  quantity already in the base unit converts with factor 1; anything else is
  `UOM_NOT_CONVERTIBLE`. `10 BOX × 12 = 120 EA`.
* **Status codes** are interpreted through a vocabulary declared per source system
  (`app/mapping/vocabularies.py`). ERP: OPEN, CANCELLED. WMS: POSTED counts; REVERSED,
  CANCELLED are final and count nothing; PENDING, DRAFT are not final. HRMS: ACTIVE,
  INACTIVE. WFM: ON_SHIFT, OFF_SHIFT. A code not declared is not guessed: it becomes a
  data-quality issue and the record is not used for a decision.
* **Employees** are scoped by mapping their HRMS location through the same crosswalk
  (`ST-014 → STORE-014`). No location but a region gives a region scope
  (HR-501). An unmappable location gives no scope: the employee is never routed to a store.
  If the HRMS region disagrees with the crosswalk's, the crosswalk wins and the
  disagreement is reported.

## Which system is authoritative

| Field | Authority | If missing or ambiguous |
|---|---|---|
| What was ordered, from whom, by when, PO status | ERP (`purchase_order_lines`) | unknown status → not evaluable; two records for one PO line → not evaluable |
| What was physically accepted and posted, when | WMS (`goods_receipt_lines`) | unknown or non-final posting status → line not evaluable; outstanding rejected receipt row naming the line → not evaluable; one naming no readable line → no line evaluable |
| Link receipt → PO line | the PO number and line on the WMS record, checked against the ERP line's canonical product and location | mismatch → not evaluable; no such PO line → orphan receipt issue |
| Canonical product, base unit, conversion | product crosswalk | unmapped / in conflict / inconsistent → not evaluable |
| Canonical location, region | location crosswalk | same |
| Employment status, role, home location, manager | HRMS | unknown status → not eligible; unmappable location → not routable |
| Availability (on shift) | workforce export | never overrides HRMS; stale, future-dated or unknown → not on shift |

## Detection

`app/detection/rules.py`, `evaluate_po_line(po_lines, receipts, feed, as_of, params)`.
The first check that applies decides the outcome:

1. More than one source record for the PO line, the record in conflict, or an unknown
   status → `NOT_EVALUABLE`.
2. Cancelled → `NOT_ELIGIBLE`.
3. Product, unit or location of the PO line cannot be mapped → `NOT_EVALUABLE`. (Checked
   before the deadline so data problems surface while the window is still open.)
4. `as_of <= deadline` → `NOT_DUE` (the position so far is recorded, nothing raised).
5. No completed receipt import, the latest receipt extract predates the deadline, an
   outstanding rejected receipt row names this line, or an outstanding rejected receipt row
   names no readable line at all → `NOT_EVALUABLE`. Rejected rows are read through the
   profile of the import that rejected them (positionally, if a stray delimiter broke the
   column count), and stay outstanding until the record is validly re-sent or a steward
   dismisses the row (`app/ingestion/rejections.py`).
6. Any linked receipt (received at or before `as_of`) is unmapped, for another product or
   location, in conflict, sharing its (receipt, line) with another source record anywhere in
   the tenant, or not finally posted →
   `NOT_EVALUABLE`. Reversed/cancelled receipts and receipts dated after `as_of` are
   excluded and listed.
7. `expected − Σ counted > tolerance` → `SHORTFALL`, else `NO_SHORTFALL` (over-receipt and
   within-tolerance are noted).

Receipts received after the deadline but before the evaluation time count (the goods
arrived) and are marked `on_time: false` in the evidence; lateness is a different rule.

**Grain.** Identical re-sends of a receipt are one fact, so summing distinct facts cannot
double-count (PO-506: 10 EA, not 20). Split receipts are distinct facts and are summed
(PO-501: 60 + 32). Nothing is matched on names or row counts.

**Exception lifecycle on re-evaluation** (`app/detection/service.py`):

| Existing case | New outcome | Result |
|---|---|---|
| none | SHORTFALL | insert (ON CONFLICT DO NOTHING), event DETECTED |
| OPEN | SHORTFALL, same evidence hash | nothing |
| OPEN | SHORTFALL, different evidence | update quantities and evidence, event EVIDENCE_UPDATED (before/after) |
| OPEN | NOT_EVALUABLE | stays OPEN and visible, event REEVALUATION_BLOCKED once |
| OPEN | NO_SHORTFALL / NOT_DUE / NOT_ELIGIBLE | CLEARED, event CLEARED, active tasks CANCELLED |
| CLEARED | SHORTFALL | REOPENED, new task |
| RESOLVED | SHORTFALL, same evidence | nothing (the verified outcome stands) |
| RESOLVED | SHORTFALL, different evidence | REOPENED, resolution cleared (kept in events), new task |

The evidence hash covers the PO record, counted and excluded receipts (key + content
hash), aliases used and the computation. It excludes import lineage, so re-importing the
same files is not "new evidence".

## Routing

`app/routing/policy.py`. For a shortfall task at location L, tiers in order:
`receiving_associate` at L (`PRIMARY_ROLE_AT_LOCATION`), then `store_manager` at L
(`FALLBACK_STORE_MANAGER`). A candidate is eligible when HRMS says ACTIVE (checked first,
nothing overrides it), the record is not in conflict, and the latest workforce sample at or
before the evaluation time says ON_SHIFT, is at most 60 minutes old (configurable), and the
evaluation time falls inside its shift window. Ties go to the person with fewer active
tasks, then the lower employee ID. If nobody qualifies the task stays `CREATED` in the
`MANUAL_ASSIGNMENT` queue and every later run retries it. Regional roles are not
auto-assigned store work; they supervise (reassign, verify) within their region.

On every run the router also keeps existing tasks in step: if the exception's location
changed (the ERP moved the line), the task follows it (`LOCATION_CHANGED`); if the assignee is
no longer active, in an assignable role and in scope for the task's location, the task is
`RELEASED` back to `CREATED` and routed again. Going off shift is not a reason to take work
away.

Fixture outcome: STORE-014 → HR-102 (HR-101 OFF_SHIFT, HR-103 EMPLOYMENT_NOT_ACTIVE
despite ON_SHIFT); STORE-015 → HR-401 as fallback. Each decision is stored with every
candidate considered and why.

## Task workflow

```
CREATED ─assign/reassign─► ASSIGNED ─acknowledge─► ACKNOWLEDGED ─start─► IN_PROGRESS ─complete─► COMPLETED
                              ▲                        │  block                │ ▲ block/unblock        │ (server, same request)
                              └──── reassign ──────────┴───────► BLOCKED ──────┘ │                        ▼
                                                                                 └──── reject ──── PENDING_VERIFICATION ─verify─► VERIFIED
any active state ─(system: exception cleared)─► CANCELLED
ASSIGNED..BLOCKED ─(system: assignee no longer eligible)─► CREATED, then routed again
```

* Clients send actions, never statuses. `next_status()` refuses anything not in the table
  (409 with the allowed actions).
* Acknowledge, start, block, unblock, complete: the current assignee only, and only while
  HRMS still has them active and in scope for the task's location.
* Reassign: an active store manager of the location, a regional operations lead of its
  region, or the assignee's HRMS manager. The new assignee must pass the same eligibility as
  automatic routing: active, assignable role, in scope, on shift now.
* Complete requires a resolution code and an evidence reference; the server moves the
  task to PENDING_VERIFICATION in the same transaction. Resubmitting exactly the code and
  evidence that verification rejected is refused (`RESUBMISSION_UNCHANGED`), which also stops
  a delayed client retry from re-completing a task sent back for rework.
* Verify / reject: a supervisor as above who is not the person who completed it. The
  exception becomes RESOLVED only when a verified task carries a resolution code and
  evidence reference and no other task for it is active.

## Transactions and locks

| Operation | Transaction | Lock | Backstop constraint |
|---|---|---|---|
| Register import | short, commits PROCESSING | — | — |
| Load a file | one transaction for the whole file | `pg_advisory_xact_lock(tenant:import:dataset)`, plus `FOR UPDATE` on the facts it touches | `UNIQUE (tenant, source_system, source_record_key)` |
| Import failed | rolled back; a new transaction marks FAILED | — | — |
| Resolve conflict / dismiss rejected row | one transaction | same advisory lock as imports of that dataset | conflict CHECKs, partial unique index on open conflicts |
| Detection step | one transaction | `pg_advisory_xact_lock(tenant:reconcile)` plus the *shared* import lock of every dataset (no import commits mid-read); exceptions `FOR UPDATE` | `UNIQUE (tenant, case_key)` with ON CONFLICT DO NOTHING |
| Routing step | one transaction | same locks; open exceptions `FOR UPDATE`, then their tasks | partial unique index on active tasks, ON CONFLICT DO NOTHING |
| Task action | one transaction | exception row, then task row `FOR UPDATE` (same order as the detector and router, so no deadlock) | idempotency key UNIQUE; task CHECKs |

Wall-clock time appears only in operational columns (`created_at`, `recorded_at`); every
business decision reads the injected clock (`app/clock.py`).
