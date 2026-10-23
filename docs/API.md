# API contract

Machine-readable schema: [`openapi.json`](openapi.json) (regenerate with `make openapi`), or
`/docs` and `/openapi.json` on a running server. Real request/response pairs for every
flow below: [`API_EXAMPLES.md`](API_EXAMPLES.md).

## Conventions

* JSON in and out (imports are `multipart/form-data` with a `file` field).
* Timestamps are ISO-8601 with an offset; responses are in UTC. Quantities are decimal
  strings (`"120"`, `"0.5"`) so nothing is lost to floating point.
* Errors always use one envelope:

  ```json
  {"error": {"code": "INVALID_TRANSITION", "message": "cannot complete a task that is ASSIGNED",
             "details": {"status": "ASSIGNED", "action": "COMPLETE", "allowed_actions": ["ACKNOWLEDGE", "REASSIGN"]},
             "request_id": "4f0c..."}}
  ```

* `X-Request-ID` is echoed (or generated) on every response and appears in the logs.
* Mutating task and conflict endpoints need `X-Actor-Id` (an HRMS employee ID). It stands in
  for an authenticated principal; see the README's production section.
* Task actions accept an optional `Idempotency-Key` header and an optional
  `expected_version` in the body.

| Status | Codes |
|---|---|
| 400/422 | `VALIDATION_ERROR` (request shape), `INVALID_REQUEST`, `ACTOR_REQUIRED`, `UNKNOWN_PROFILE`, `EXTRACT_TIME_IN_FUTURE`, `NAIVE_TIMESTAMP`, `ASSIGNEE_NOT_ELIGIBLE`, `UNKNOWN_ASSIGNEE`, `ALREADY_ASSIGNED`, `NOTE_REQUIRED`, `IDEMPOTENCY_KEY_REUSED`, `BAD_IDEMPOTENCY_KEY`, `NOT_A_REJECTED_ROW` |
| 403 | `ACTOR_NOT_PERMITTED`, `UNKNOWN_ACTOR`, `ACTOR_NOT_ACTIVE`, `ASSIGNEE_NO_LONGER_ELIGIBLE`, `SELF_VERIFICATION_NOT_ALLOWED`, `SYSTEM_ACTOR_FORBIDDEN` |
| 404 | `NOT_FOUND`, `UNKNOWN_DATASET` |
| 409 | `INVALID_TRANSITION`, `VERSION_CONFLICT`, `RESUBMISSION_UNCHANGED`, `CONFLICT_ALREADY_RESOLVED`, `CONFLICT_STALE`, `ALREADY_DISMISSED`, `IDEMPOTENCY_IN_FLIGHT` |
| 413 | `PAYLOAD_TOO_LARGE` |
| 500 | `IMPORT_FAILED` (rolled back; `details.import_id`), `RUN_FAILED`, `INTERNAL_ERROR` |

## Imports

### `POST /imports/{dataset}`

Datasets: `purchase_order_lines`, `goods_receipt_lines`, `product_aliases`,
`location_aliases`, `employees`, `workforce_status` (`GET /datasets` lists them).

Query: `profile` (default `opsib_v1`), `source_extracted_at` (optional, when the source
extracted the file; defaults to the business clock; may not be in the future).

* `201` — the file was processed. `status` is `COMPLETED` (every row accepted; identical
  repeats are fine) or `COMPLETED_WITH_ERRORS` (some rows rejected or in conflict). The
  response lists every row that needs attention.
* `422` — the file itself is unusable (`status: REJECTED`, `file_errors`: missing or duplicate
  columns, not UTF-8, empty). Nothing was loaded.
* `500 IMPORT_FAILED` — unexpected error; the import is recorded as `FAILED`, nothing from
  the file was committed, and re-sending is safe.

```json
{
  "import_id": "…", "dataset": "goods_receipt_lines", "profile": "opsib_v1", "source_system": "WMS",
  "status": "COMPLETED", "file_sha256": "…", "source_extracted_at": "2026-10-16T09:30:00+00:00",
  "counts": {"total_rows": 7, "accepted": 6, "inserted": 6, "unchanged": 0, "duplicates": 1, "rejected": 0, "conflicts": 0},
  "file_errors": [], "warnings": [],
  "rows": [{"row_number": 8, "outcome": "DUPLICATE_IN_FILE", "source_record_key": "WMS-GRN506-A-L1",
            "detail": "identical to row 7 of this file; counted once", "errors": [], "raw": {"…": "…"}}]
}
```

Row outcomes: `INSERTED`, `UNCHANGED` (same key, same content as stored), `DUPLICATE_IN_FILE`,
`REJECTED` (with `errors[]` of `{field, code, message}`), `CONFLICT` (same key, different
content; links `conflict_id`).

### `GET /imports`, `GET /imports/{import_id}?all_rows=false`

Counts, status, file errors and the rows needing attention (`all_rows=true` for every row).

## Data quality

### `GET /data-quality?include_resolved=false`

```json
{
  "summary": {"open_issues": 1, "open_conflicts": 1, "outstanding_rejected_rows": 1, "outstanding_rejected_files": 0},
  "issues": [{"code": "PRODUCT_UNMAPPED", "status": "OPEN", "producer": "detection",
              "subject": {"dataset": "purchase_order_lines", "record_key": "ERP-PO507-L1"},
              "message": "no product alias for ERP item ERP-SKU-999",
              "details": {"affected_cases": ["RECEIPT_SHORTFALL|ERP|PO-507|1"], "…": "…"}}],
  "conflicts": [{"id": "…", "source_record_key": "WMS-GRN501-B-L1", "changed_fields": ["accepted_qty"],
                 "existing": {"payload": {"accepted_qty": "32", "…": "…"}}, "incoming": {"payload": {"accepted_qty": "40"}}}],
  "rejected_rows": [{"dataset": "purchase_order_lines", "row_number": 9, "errors": [{"field": "ordered_qty", "…": "…"}]}],
  "rejected_files": []
}
```

Issue codes include `PRODUCT_UNMAPPED`, `LOCATION_UNMAPPED`, `UOM_NOT_CONVERTIBLE`,
`PRODUCT_MAPPING_IN_CONFLICT`, `PRODUCT_BASE_UOM_INCONSISTENT`, `SOURCE_RECORD_IN_CONFLICT`,
`UNRECOGNIZED_PO_STATUS`, `UNRECOGNIZED_POSTING_STATUS`, `RECEIPT_POSTING_NOT_FINAL`,
`PO_LINE_KEY_COLLISION`, `RECEIPT_LINE_KEY_COLLISION`, `RECEIPT_PRODUCT_MISMATCH`,
`RECEIPT_LOCATION_MISMATCH`, `RECEIPT_FEED_MISSING`, `RECEIPT_FEED_STALE`,
`REJECTED_RECEIPT_ROW_FOR_LINE`, `ORPHAN_RECEIPT`, `EMPLOYEE_LOCATION_UNMAPPED`,
`EMPLOYEE_UNSCOPED`, `EMPLOYEE_REGION_MISMATCH`, `UNRECOGNIZED_EMPLOYMENT_STATUS`,
`UNRECOGNIZED_AVAILABILITY_STATUS`, `AVAILABILITY_FOR_UNKNOWN_EMPLOYEE`.

`rejected_rows` lists every rejected row still outstanding, from any import: it stays until
the same record is validly re-sent or a steward dismisses it. `rejected_files` lists files
that could not be loaded at all, until a later import of that dataset succeeds.

### `POST /data-quality/conflicts/{conflict_id}/resolve`

Headers: `X-Actor-Id` (an active store manager or regional operations employee). Body:
`{"resolution": "ACCEPT_INCOMING" | "KEEP_EXISTING", "reason": "…"}`. `ACCEPT_INCOMING` makes
the re-sent content the record's next version; the next run uses it. Other open conflicts on
the same record stay open (re-based onto the new version after an acceptance).

### `POST /data-quality/rejected-rows/{row_id}/dismiss`

Headers: `X-Actor-Id` (a data steward, as above). Body: `{"reason": "…"}`. For a rejected row
that needs no correction (e.g. a garbage line); it stops blocking evaluation. The decision is
append-only.

## Reconciliation

### `POST /reconciliation-runs` → `201`

Runs detection and routing at the business clock. Repeating it with unchanged inputs
changes nothing. The response carries the versions used and one evaluation per PO line:

```json
{
  "run_id": "…", "status": "COMPLETED", "evaluated_as_of": "2026-10-16T09:30:00+00:00",
  "rule_version": "receipt_shortfall/1.0.0", "routing_policy_version": "shortfall_routing/1.0.0",
  "mapping_version": "map-6c878628fde9",
  "summary": {"detection": {"outcomes": {"SHORTFALL": 3, "NO_SHORTFALL": 1, "NOT_DUE": 1, "NOT_ELIGIBLE": 1}, "…": "…"},
              "routing": {"created": 3, "assigned": 3, "…": "…"}},
  "evaluations": [{"case_key": "RECEIPT_SHORTFALL|ERP|PO-503|1", "outcome": "NOT_DUE",
                   "reason_codes": ["DEADLINE_NOT_PASSED"], "details": {"expected_qty": "50", "received_qty": "20", "…": "…"}}]
}
```

### `GET /reconciliation-runs/{run_id}`

## Exceptions

### `GET /exceptions?status=&location_id=&exception_type=`

### `GET /exceptions/{exception_id}`

```json
{
  "id": "…", "case_key": "RECEIPT_SHORTFALL|ERP|PO-505|1", "exception_type": "RECEIPT_SHORTFALL",
  "status": "OPEN", "last_evaluation_outcome": "SHORTFALL", "detected_at": "2026-10-16T09:30:00+00:00",
  "rule_version": "receipt_shortfall/1.0.0", "mapping_version": "map-6c878628fde9", "rule_params": {"tolerance_base_qty": "0"},
  "po": {"source_system": "ERP", "po_number": "PO-505", "line_number": 1, "source_record_key": "ERP-PO505-L1",
         "supplier_code": "SUP-21", "expected_receipt_by": "2026-10-15T09:30:00+00:00"},
  "canonical_product_id": "SKU-002", "canonical_location_id": "STORE-015", "region_id": "REGION-MH",
  "expected_qty": "120", "received_qty": "96", "variance_qty": "24", "unit": "EA",
  "explanation": {"summary": "Receipt shortfall of 24 EA on PO PO-505 line 1 at STORE-015: 96 of 120 EA received and posted.",
                  "known": ["…"], "unknown": ["Whether the supplier dispatched the full ordered quantity (no supplier dispatch / ASN data was supplied).", "…"],
                  "evidence_needed": ["Supplier ASN or dispatch note for this PO line, with dispatched quantity.", "…"],
                  "not_established": "… It does not establish supplier fault, carrier loss, store error or a financial loss."},
  "evidence": {"po_line": {"source_record_key": "ERP-PO505-L1", "ordered_qty": "10", "ordered_uom": "BOX", "to_base_factor": "12",
                           "import_rows": [{"import_id": "…", "row_number": 6, "outcome": "INSERTED"}], "…": "…"},
               "receipts_counted": [{"source_record_key": "WMS-GRN505-A-L1", "accepted_qty_base": "96", "on_time": true, "…": "…"}],
               "mappings_used": ["…4 alias records…"], "computation": {"formula": "variance = ordered_qty x to_base_factor - …"}},
  "supporting_records": ["current state of each cited fact, the raw import rows that carried it, changed_since_detection"],
  "tasks": ["…"], "events": [{"event_type": "DETECTED", "run_id": "…", "actor_id": "system:detector", "…": "…"}],
  "resolution": {"code": null, "evidence_ref": null, "resolved_by": null, "resolved_at": null}
}
```

### `POST /exceptions/{exception_id}/investigation-summary`

Advisory only. `status` is `OK` (validated summary), `REJECTED` (the model's output failed
validation and is not shown), or `UNAVAILABLE` (disabled, timed out, errored). Never
changes the case.

## Tasks

### `GET /tasks?assignee_id=&location_id=&status=&queue=`

### `GET /tasks/{task_id}` — the task, its `assignments` history and its `events`.

Task representation:

```json
{"id": "…", "exception_id": "…", "task_type": "INVESTIGATE_RECEIPT_SHORTFALL", "status": "ASSIGNED",
 "allowed_actions": ["ACKNOWLEDGE", "REASSIGN"], "location_id": "STORE-014", "region_id": "REGION-MH",
 "assignee_employee_id": "HR-102", "queue": null, "opened_at": "2026-10-16T09:30:00+00:00",
 "due_at": "2026-10-17T09:30:00+00:00", "resolution_code": null, "evidence_ref": null,
 "completed_by": null, "verified_by": null, "version": 2}
```

### Actions (`POST /tasks/{task_id}/<action>`)

All take `X-Actor-Id`, optional `Idempotency-Key`, optional `{"expected_version": n}`, and
return the task. A replayed response carries `Idempotent-Replay: true`.

| Action | Body | Who | From → to |
|---|---|---|---|
| `acknowledge` | — | assignee | ASSIGNED → ACKNOWLEDGED |
| `start` | — | assignee | ACKNOWLEDGED → IN_PROGRESS |
| `block` | `reason` | assignee | ACKNOWLEDGED / IN_PROGRESS → BLOCKED |
| `unblock` | `note?` | assignee | BLOCKED → IN_PROGRESS |
| `complete` | `resolution_code`, `evidence_ref`, `note?` (required for `OTHER`); not the pair that was just rejected | assignee | IN_PROGRESS → COMPLETED → PENDING_VERIFICATION |
| `verify` | `note?` | supervisor, not the completer | PENDING_VERIFICATION → VERIFIED (may resolve the exception) |
| `reject` | `reason` | supervisor, not the completer | PENDING_VERIFICATION → IN_PROGRESS |
| `reassign` | `assignee_employee_id` (active, in scope, on shift), `reason` | supervisor | CREATED / ASSIGNED / ACKNOWLEDGED / IN_PROGRESS / BLOCKED → ASSIGNED |

*Assignee* means the current assignee while HRMS still has them active and in scope for the
task's location. *Supervisor* means an active store manager of the location, regional
operations for its region, or the assignee's HRMS manager.

Resolution codes: `RECEIPT_MISSED_NOW_POSTED`, `SUPPLIER_CONFIRMED_SHORT_DISPATCH`,
`CARRIER_LOSS_CLAIM_RAISED`, `BALANCE_TO_FOLLOW`, `PO_QUANTITY_AMENDED`,
`STORE_COUNT_CONFIRMED_SHORT`, `OTHER`.

## Health

`GET /health` → `{"status": "ok", "tenant_id": "tenant-demo", "clock_mode": "frozen", "business_now": "2026-10-16T15:00:00+05:30", "ai_provider": "disabled"}`
