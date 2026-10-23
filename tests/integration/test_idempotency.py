"""Re-imports, re-runs and concurrent workers must not change the business result."""

import threading
import time
import uuid

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from app.db.models import ExceptionCase, ExceptionEvent, ReceiptLineFact, ReconciliationRun, Task, TaskEvent
from app.detection.service import ShortfallDetector
from app.ingestion.service import ImportRequest
from app.workflows.service import TaskWorkflowService
from tests.helpers import FIXTURES, business_snapshot, exceptions_by_po, fixture_bytes, load_fixtures, reconcile, upload


def count(services, model, *where):
    with services.sessions() as session, session.begin():
        return session.scalar(select(func.count()).select_from(model).where(*where))


def test_importing_the_same_files_twice_changes_nothing(client, services):
    load_fixtures(client)
    reconcile(client)
    before = business_snapshot(client)
    facts_before = count(services, ReceiptLineFact)

    for dataset in ("purchase_order_lines", "goods_receipt_lines"):
        again = upload(client, dataset, fixture_bytes(dataset)).json()
        assert again["status"] == "COMPLETED"
        assert again["counts"] == {
            "total_rows": 7,
            "accepted": 6,
            "inserted": 0,
            "unchanged": 6,
            "duplicates": 1,
            "rejected": 0,
            "conflicts": 0,
        }
    reconcile(client)

    assert count(services, ReceiptLineFact) == facts_before == 6
    assert business_snapshot(client) == before
    assert exceptions_by_po(client)["PO-506"]["received_qty"] == "10"


def test_repeated_runs_add_no_exceptions_tasks_or_events(client, services):
    load_fixtures(client)
    reconcile(client)
    before = business_snapshot(client)
    events = (count(services, ExceptionEvent), count(services, TaskEvent))
    for _ in range(3):
        reconcile(client)
    assert business_snapshot(client) == before
    assert (count(services, ExceptionEvent), count(services, TaskEvent)) == events


def test_import_order_does_not_matter_and_late_mappings_are_picked_up(client):
    # Transactions and roster arrive before the crosswalks: nothing can be mapped yet.
    # Mapping is checked before the deadline, so even the not-yet-due PO-503 surfaces
    # its data-quality problem now rather than after its window closes.
    load_fixtures(client, skip=("product_aliases", "location_aliases"))
    run = reconcile(client)
    assert run["summary"]["detection"]["outcomes"] == {"NOT_ELIGIBLE": 1, "NOT_EVALUABLE": 5}
    assert client.get("/exceptions").json() == []
    codes = {i["code"] for i in client.get("/data-quality").json()["issues"]}
    assert {"PRODUCT_UNMAPPED", "LOCATION_UNMAPPED"} <= codes

    for dataset in ("product_aliases", "location_aliases"):
        upload(client, dataset, fixture_bytes(dataset))
    reconcile(client)
    assert {po: e["variance_qty"] for po, e in exceptions_by_po(client).items()} == {
        "PO-501": "8",
        "PO-505": "24",
        "PO-506": "10",
    }
    assert client.get("/data-quality").json()["summary"]["open_issues"] == 0


def test_concurrent_imports_of_the_same_file_store_each_record_once(services):
    content = fixture_bytes("goods_receipt_lines")
    results, errors = [], []

    def worker():
        try:
            results.append(services.imports.import_file(ImportRequest("goods_receipt_lines", content)))
        except Exception as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(b.inserted_rows for b in results) == [0, 0, 0, 6]
    assert sorted(b.unchanged_rows for b in results) == [0, 6, 6, 6]
    assert count(services, ReceiptLineFact) == 6


def test_concurrent_runs_create_one_exception_and_one_task_per_case(services):
    services.imports.import_directory(FIXTURES)
    errors = []

    def worker():
        try:
            services.reconciliation.run()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert count(services, ExceptionCase) == 3
    assert count(services, Task) == 3
    assert count(services, ExceptionEvent, ExceptionEvent.event_type == "DETECTED") == 3
    assert count(services, TaskEvent, TaskEvent.event_type == "ASSIGNED") == 3


def test_database_refuses_a_second_active_task_or_a_duplicate_case(services):
    services.imports.import_directory(FIXTURES)
    services.reconciliation.run()
    with services.sessions() as session, session.begin():
        task = session.scalars(select(Task)).first()
        exception = session.get(ExceptionCase, task.exception_id)
        values = {
            c.key: getattr(task, c.key) for c in Task.__table__.columns if c.key not in ("created_at", "updated_at")
        }
        exception_values = {
            c.key: getattr(exception, c.key)
            for c in ExceptionCase.__table__.columns
            if c.key not in ("created_at", "updated_at")
        }
    with (
        pytest.raises(IntegrityError, match="uq_tasks_one_active_per_exception_type"),
        services.sessions() as session,
        session.begin(),
    ):
        session.execute(insert(Task).values(**{**values, "id": uuid.uuid4()}))
    with (
        pytest.raises(IntegrityError, match="uq_exceptions_tenant_id_case_key"),
        services.sessions() as session,
        session.begin(),
    ):
        session.execute(insert(ExceptionCase).values(**{**exception_values, "id": uuid.uuid4()}))


def test_router_does_not_reopen_work_on_a_case_being_resolved(client, services, monkeypatch):
    """A verification commits while the router is mid-run: the router must see the
    case as RESOLVED, not open a second task for it."""
    load_fixtures(client)
    reconcile(client)
    exception_id = exceptions_by_po(client)["PO-501"]["id"]
    task_id = client.get(f"/exceptions/{exception_id}").json()["tasks"][0]["id"]
    for action, body in (
        ("acknowledge", None),
        ("start", None),
        ("complete", {"resolution_code": "BALANCE_TO_FOLLOW", "evidence_ref": "SUP07-1"}),
    ):
        assert client.post(f"/tasks/{task_id}/{action}", json=body, headers={"X-Actor-Id": "HR-102"}).status_code == 200

    paused, release = threading.Event(), threading.Event()
    original = TaskWorkflowService._resolve_exception_if_done

    def resolve_then_hold(self, ctx):  # noqa: ANN001, ANN202
        original(self, ctx)
        paused.set()
        release.wait(10)  # keep the verification transaction open

    monkeypatch.setattr(TaskWorkflowService, "_resolve_exception_if_done", resolve_then_hold)
    verifier = threading.Thread(target=lambda: services.workflow.verify(uuid.UUID(task_id), "HR-201"))
    verifier.start()
    assert paused.wait(10)

    def route():
        with services.sessions() as session, session.begin():
            run = session.scalars(select(ReconciliationRun).order_by(ReconciliationRun.created_at.desc())).first()
            services.reconciliation._router.ensure_tasks(session, run)

    router = threading.Thread(target=route)
    router.start()
    time.sleep(0.5)  # the router is now waiting on the exception row
    release.set()
    verifier.join(10)
    router.join(10)
    detail = client.get(f"/exceptions/{exception_id}").json()
    assert detail["status"] == "RESOLVED"
    assert [t["status"] for t in detail["tasks"]] == ["VERIFIED"]


def test_an_import_cannot_land_in_the_middle_of_a_detection_run(client, services, monkeypatch):
    """Receipts are read, then a receipt import commits, then the feed is read: the
    run would see a fresh feed with no receipts and report every line short."""
    load_fixtures(client, skip=("goods_receipt_lines",))
    original = ShortfallDetector._receipt_feed
    seen = {}

    def import_midway(self, session):  # noqa: ANN001, ANN202
        worker = threading.Thread(
            target=lambda: services.imports.import_file(
                ImportRequest("goods_receipt_lines", fixture_bytes("goods_receipt_lines"))
            )
        )
        worker.start()
        worker.join(1.0)
        seen["import_was_blocked"] = worker.is_alive()
        seen["worker"] = worker
        return original(self, session)

    monkeypatch.setattr(ShortfallDetector, "_receipt_feed", import_midway)
    run = reconcile(client)
    seen["worker"].join(10)
    monkeypatch.setattr(ShortfallDetector, "_receipt_feed", original)

    assert seen["import_was_blocked"] is True
    assert run["summary"]["detection"]["outcomes"]["NOT_EVALUABLE"] == 4  # no feed yet: nothing claimed
    assert client.get("/exceptions").json() == []
    reconcile(client)  # the import has committed now
    assert {po: e["variance_qty"] for po, e in exceptions_by_po(client).items()} == {
        "PO-501": "8",
        "PO-505": "24",
        "PO-506": "10",
    }
