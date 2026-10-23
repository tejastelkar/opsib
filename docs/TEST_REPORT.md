# Test report

## Command

```bash
scripts/dev_postgres.sh start      # or: docker compose up -d db
TEST_DATABASE_URL=postgresql+psycopg://opsib@127.0.0.1:55433/opsib_test uv run pytest -v
uv run pytest tests/unit           # the unit tests need no database
```

The integration suite drops and recreates the schema of the database named by
`TEST_DATABASE_URL` through the Alembic migrations (so the migrations are tested too), and
truncates every table before each test. It refuses to run against a database whose name
does not end in `_test`.

## Result

Python 3.12.13, PostgreSQL 17.10, pytest 9.1.1, macOS:

```
============================= 253 passed in 13.48s =============================
```

| File | Tests | What it covers |
|---|---:|---|
| `tests/unit/test_shortfall_rule.py` | 35 | the rule in isolation: partial receipts, units, deadline boundaries, cancellation, posting statuses, identity mismatches, key collisions, conflicts, feed coverage, rejected rows, tolerance, explanation wording |
| `tests/unit/test_state_machine.py` | 86 | every declared transition, every undeclared (status, action) pair refused, system-only actions |
| `tests/unit/test_ingestion_validation.py` | 24 | typed validation (timestamps with offsets only, quantities, codes), content hashing, file-level checks, registry |
| `tests/unit/test_units_and_mapping.py` | 14 | BOX → EA, crosswalk identity per system, ambiguous and inconsistent aliases, employee scope from HRMS locations |
| `tests/unit/test_routing_policy.py` | 12 | HR-102 / HR-401 selection, off shift, inactive-but-on-shift, stale and future samples, shift window, unmapped location, tie-break, supervision scope |
| `tests/unit/test_ai_boundary.py` | 13 | evidence package minimisation, schema / citation / invented-number rejection, timeout, crash, refusal, stubbed Claude client |
| `tests/unit/test_fixture_integrity.py` | 1 | the supplied fixture files match their recorded SHA-256 |
| `tests/integration/test_fixture_scenarios.py` | 7 | the six required scenarios end to end through the API, routing, traceability, neutral wording |
| `tests/integration/test_idempotency.py` | 8 | double import, repeated runs, import order, concurrent imports, concurrent runs, DB constraints, the routing/verify race, an import landing mid-run |
| `tests/integration/test_data_quality_and_conflicts.py` | 23 | unmapped product, orphan receipt, conflicting re-sends and their review, rejected rows and files, receipt feed missing/stale, rejected receipt rows blocking (any format, broken delimiters, unreadable rows), steward-only decisions, an import crashing half way |
| `tests/integration/test_task_workflow.py` | 15 | full lifecycle, invalid transitions, who may act, retries with and without a key, concurrent duplicate completion, version conflicts, reassignment, self-verification, rework, append-only audit |
| `tests/integration/test_routing.py` | 6 | persisted selection reasons, manual queue and later routing, unmapped employee location, assignee deactivated or moved, task following a moved PO line |
| `tests/integration/test_late_data.py` | 4 | late receipt clears the case, partial late receipt updates it, future-dated receipt, reopening after resolution |
| `tests/integration/test_second_retailer_profile.py` | 2 | another export format through a registered profile, same findings |
| `tests/integration/test_ai_endpoint.py` | 3 | advisory endpoint degrades safely and never changes the case |

## The brief's minimum tests

| Required | Tests |
|---|---|
| 1. Unit tests for UOM conversion, partial receipts, deadline boundaries and cancelled PO lines | `test_cartons_are_converted_to_base_units_before_comparison`, `test_box_converts_to_twelve_each`, `test_unconvertible_unit_is_not_evaluable`, `test_split_receipts_are_summed_per_po_line`, `test_deadline_must_have_passed[...]` (before / at / 1 µs after / a day after), `test_cancelled_line_is_not_eligible_even_with_no_receipts` |
| 2. Integration test that loads the fixture pack and verifies the scenarios | `test_six_required_scenarios`, `test_imports_report_duplicates_and_accept_everything_else`, `test_tasks_are_routed_to_eligible_people` |
| 3. Idempotency: same receipt and PO data twice, business result unchanged | `test_importing_the_same_files_twice_changes_nothing`, `test_repeated_runs_add_no_exceptions_tasks_or_events`, `test_concurrent_imports_of_the_same_file_store_each_record_once`, `test_concurrent_runs_create_one_exception_and_one_task_per_case` |
| 4. Identity / assignment: source-location mapping, off shift, inactive, fallback manager | `test_erp_wms_and_hrms_location_codes_map_to_one_store`, `test_store_employee_location_is_mapped_before_use`, `test_store_014_goes_to_the_on_shift_active_receiving_associate`, `test_inactive_employee_stays_ineligible_although_the_shift_export_says_on_shift`, `test_store_015_falls_back_to_the_on_shift_store_manager`, `test_selection_reasons_are_persisted` |
| 5. Workflow: invalid transitions, repeated completion, audit history | `test_every_undeclared_transition_is_refused[...]`, `test_invalid_transitions_are_refused_with_the_allowed_actions`, `test_retried_completion_with_the_same_key_replays_the_stored_response`, `test_retried_completion_without_a_key_is_recognised_or_refused`, `test_concurrent_duplicate_completions_apply_once`, `test_a_delayed_retry_cannot_resubmit_a_rejected_completion`, `test_full_lifecycle_resolves_the_exception_only_after_verification`, `test_audit_history_is_append_only_in_the_database` |
| 6. Failure paths: unknown product mapping, conflicting duplicate ID, absent eligible assignee | `test_unknown_product_is_a_data_quality_issue_not_an_exception`, `test_conflicting_resend_is_flagged_and_does_not_overwrite`, `test_no_eligible_person_leaves_the_task_visible_in_the_manual_queue`, `test_import_that_fails_midway_rolls_back_and_can_be_retried`, `test_file_missing_columns_is_rejected_whole` |

## Other checks run

* `uv run alembic upgrade head`, `downgrade base`, `upgrade head` and `alembic check` (models
  and migration agree) on an empty database.
* `uvx ruff@0.14.0 check app tests migrations` and `ruff format --check`: clean.
* The README's curl walkthrough against `uvicorn` on a freshly migrated database (imports,
  run, acknowledge, start, complete with a key, the retried completion returning
  `Idempotent-Replay: true`, verify).
* `docs/API_EXAMPLES.md` is generated by `scripts/capture_api_examples.py` from real requests.
* The two concurrency regression tests (`test_router_does_not_reopen_work_on_a_case_being_resolved`,
  `test_an_import_cannot_land_in_the_middle_of_a_detection_run`) were run with their fixes
  removed and failed as expected, then passed with the fixes restored.

Not run: `docker compose` (no Docker on the build machine) and the Claude provider against
the live API (tested with a stub client only).
