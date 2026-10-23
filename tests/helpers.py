"""Test helpers.

Derived test data is built in memory from the fixture CSVs (rows changed, added
or removed by the test that needs them). The fixture files themselves are never
modified; tests/unit/test_fixture_integrity.py checks their checksums.
"""

import csv
import io
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.ingestion.datasets import FIXTURE_LOAD_ORDER

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "opsib_v1"
FROZEN = "2026-10-16T15:00:00+05:30"


def fixture_bytes(dataset: str) -> bytes:
    return (FIXTURES / f"{dataset}.csv").read_bytes()


def fixture_rows(dataset: str) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(fixture_bytes(dataset).decode("utf-8")))
    return list(reader.fieldnames or []), list(reader)


def to_csv(header: list[str], rows: Iterable[dict[str, str]]) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=header, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return out.getvalue().encode("utf-8")


def derived(dataset: str, transform: Callable[[list[dict[str, str]]], list[dict[str, str]]]) -> bytes:
    header, rows = fixture_rows(dataset)
    return to_csv(header, transform([dict(r) for r in rows]))


def upload(client: TestClient, dataset: str, content: bytes, **params: Any) -> Any:
    return client.post(
        f"/imports/{dataset}",
        files={"file": (f"{dataset}.csv", content, "text/csv")},
        params={k: v for k, v in params.items() if v is not None},
    )


def load_fixtures(client: TestClient, skip: Iterable[str] = (), replace: dict[str, bytes] | None = None) -> dict:
    replace = replace or {}
    results = {}
    for dataset in FIXTURE_LOAD_ORDER:
        if dataset in skip:
            continue
        response = upload(client, dataset, replace.get(dataset, fixture_bytes(dataset)))
        assert response.status_code == 201, response.text
        results[dataset] = response.json()
    return results


def reconcile(client: TestClient) -> dict:
    response = client.post("/reconciliation-runs")
    assert response.status_code == 201, response.text
    return response.json()


def exceptions_by_po(client: TestClient, **filters: str) -> dict[str, dict]:
    response = client.get("/exceptions", params=filters)
    assert response.status_code == 200, response.text
    return {e["po"]["po_number"]: e for e in response.json()}


def evaluations_by_po(run: dict) -> dict[str, dict]:
    return {e["case_key"].split("|")[2]: e for e in run["evaluations"]}


def active_task(client: TestClient, exception_id: str) -> dict:
    detail = client.get(f"/exceptions/{exception_id}").json()
    active = [t for t in detail["tasks"] if t["status"] not in ("VERIFIED", "CANCELLED")]
    assert len(active) == 1, detail["tasks"]
    return active[0]


def act(
    client: TestClient, task_id: str, action: str, actor: str, body: dict | None = None, key: str | None = None
) -> Any:
    headers = {"X-Actor-Id": actor}
    if key:
        headers["Idempotency-Key"] = key
    return client.post(f"/tasks/{task_id}/{action}", json=body, headers=headers)


def business_snapshot(client: TestClient) -> dict:
    """Everything that defines the business result, minus per-run bookkeeping."""
    exceptions = client.get("/exceptions").json()
    tasks = client.get("/tasks").json()
    snapshot = {
        "exceptions": sorted(
            (
                e["id"],
                e["case_key"],
                e["status"],
                e["expected_qty"],
                e["received_qty"],
                e["variance_qty"],
                e["unit"],
                e["mapping_version"],
            )
            for e in exceptions
        ),
        "tasks": sorted(
            (t["id"], t["exception_id"], t["status"], t["assignee_employee_id"], t["version"]) for t in tasks
        ),
        "exception_events": sorted(
            (e["id"], len(client.get(f"/exceptions/{e['id']}").json()["events"])) for e in exceptions
        ),
        "task_events": sorted((t["id"], len(client.get(f"/tasks/{t['id']}").json()["events"])) for t in tasks),
    }
    return snapshot
