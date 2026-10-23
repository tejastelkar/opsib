"""Regenerate docs/API_EXAMPLES.md from real requests against the real app.

    TEST_DATABASE_URL=postgresql+psycopg://opsib@127.0.0.1:55433/opsib_docs_test \
        uv run python scripts/capture_api_examples.py

The database named by TEST_DATABASE_URL must end in _test: its schema is dropped
and recreated. Generated responses are verbatim except where marked "trimmed".
"""

import json
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.api.main import create_app  # noqa: E402
from app.config import Settings  # noqa: E402
from app.container import build_services  # noqa: E402
from app.db.session import make_engine  # noqa: E402
from tests.helpers import derived, fixture_bytes  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL", "")
OUT = ROOT / "docs" / "API_EXAMPLES.md"


def reset_database() -> None:
    if not (make_url(URL).database or "").endswith("_test"):
        sys.exit("TEST_DATABASE_URL must name a database ending in _test")
    engine = make_engine(URL)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", URL)
    command.upgrade(config, "head")
    engine.dispose()


class Recorder:
    def __init__(self, client: TestClient):
        self.client = client
        self.parts: list[str] = []

    def heading(self, title: str, note: str = "") -> None:
        self.parts.append(f"\n## {title}\n")
        if note:
            self.parts.append(f"{note}\n")

    def call(
        self, method: str, path: str, *, title: str, headers=None, json_body=None, params=None, files=None, trim=None
    ):  # noqa: ANN001
        response = self.client.request(method, path, headers=headers, json=json_body, params=params, files=files)
        shown_path = path + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")
        request_lines = [f"{method} {shown_path}"]
        for key, value in (headers or {}).items():
            request_lines.append(f"{key}: {value}")
        if files:
            request_lines.append(f"(multipart form field `file`: {files['file'][0]})")
        if json_body is not None:
            request_lines.append("")
            request_lines.append(json.dumps(json_body, indent=2))
        body = response.json()
        shown = trim(body) if trim else body
        extra = [f"{k}: {v}" for k, v in response.headers.items() if k.lower() == "idempotent-replay"]
        self.parts.append(f"\n### {title}\n")
        self.parts.append("```http\n" + "\n".join(request_lines) + "\n```\n")
        status_line = f"HTTP {response.status_code}" + "".join(f"\n// {e}" for e in extra)
        self.parts.append(
            f"```json\n// {status_line}{' (trimmed)' if trim else ''}\n{json.dumps(shown, indent=2)}\n```\n"
        )
        return body


def main() -> None:
    reset_database()
    settings = Settings(_env_file=None, database_url=URL, log_level="WARNING")  # type: ignore[call-arg]
    client = TestClient(create_app(build_services(settings)))
    rec = Recorder(client)

    def upload(dataset: str, content: bytes, **params):  # noqa: ANN003, ANN202
        return {"file": (f"{dataset}.csv", content, "text/csv")}, params or None

    rec.heading("Imports", "Each dataset is uploaded as a CSV file. Row-level outcomes are in the response.")
    for dataset in ("product_aliases", "location_aliases", "employees", "workforce_status", "purchase_order_lines"):
        client.post(f"/imports/{dataset}", files={"file": (f"{dataset}.csv", fixture_bytes(dataset), "text/csv")})
    files, _ = upload("goods_receipt_lines", fixture_bytes("goods_receipt_lines"))
    grn = rec.call("POST", "/imports/goods_receipt_lines", title="Load the WMS receipt export", files=files)
    rec.call(
        "POST",
        "/imports/goods_receipt_lines",
        title="Load the same file again (idempotent: every row UNCHANGED, the repeat still a duplicate)",
        files=files,
        trim=lambda b: {k: b[k] for k in ("import_id", "status", "counts")},
    )
    rec.call("GET", f"/imports/{grn['import_id']}", title="Inspect an import (rows needing attention only)")

    rec.heading("Reconciliation")
    rec.call("POST", "/reconciliation-runs", title="Run detection and routing at the business clock")

    rec.heading("Exceptions")
    exceptions = client.get("/exceptions").json()
    by_po = {e["po"]["po_number"]: e for e in exceptions}
    rec.call(
        "GET",
        "/exceptions",
        title="List exceptions (first item shown)",
        params={"status": "OPEN"},
        trim=lambda b: [b[0], f"... {len(b) - 1} more"],
    )
    rec.call("GET", f"/exceptions/{by_po['PO-505']['id']}", title="One exception with evidence and source records")
    rec.call(
        "POST",
        f"/exceptions/{by_po['PO-505']['id']}/investigation-summary",
        title="Advisory AI summary (provider disabled: degrades safely)",
    )

    rec.heading("Tasks")
    rec.call("GET", "/tasks", title="Tasks for an assignee", params={"assignee_id": "HR-102"})
    task_id = client.get(f"/exceptions/{by_po['PO-501']['id']}").json()["tasks"][0]["id"]
    actor = {"X-Actor-Id": "HR-102"}
    rec.call(
        "POST",
        f"/tasks/{task_id}/complete",
        title="Refused: completing before starting",
        headers=actor,
        json_body={"resolution_code": "BALANCE_TO_FOLLOW", "evidence_ref": "SUP07-MAIL-1"},
    )
    rec.call(
        "POST",
        f"/tasks/{task_id}/acknowledge",
        title="Refused: someone other than the assignee",
        headers={"X-Actor-Id": "HR-101"},
    )
    rec.call("POST", f"/tasks/{task_id}/acknowledge", title="Acknowledge", headers=actor)
    client.post(f"/tasks/{task_id}/start", headers=actor)
    body = {"resolution_code": "SUPPLIER_CONFIRMED_SHORT_DISPATCH", "evidence_ref": "ASN-SUP07-PO501"}
    keyed = {**actor, "Idempotency-Key": "7d3c6f0e-complete-po501"}
    rec.call(
        "POST",
        f"/tasks/{task_id}/complete",
        title="Complete (after start) with an Idempotency-Key",
        headers=keyed,
        json_body=body,
    )
    rec.call(
        "POST",
        f"/tasks/{task_id}/complete",
        title="Client retries after a timeout: the stored response is replayed",
        headers=keyed,
        json_body=body,
    )
    rec.call(
        "POST",
        f"/tasks/{task_id}/complete",
        title="Refused: same key, different request",
        headers=keyed,
        json_body={**body, "evidence_ref": "ASN-OTHER"},
    )
    rec.call("POST", f"/tasks/{task_id}/verify", title="Refused: self-verification", headers=actor)
    rec.call(
        "POST",
        f"/tasks/{task_id}/verify",
        title="Verify by the store manager",
        headers={"X-Actor-Id": "HR-201"},
        json_body={"note": "ASN quantity matches the receipt"},
    )
    other = client.get(f"/exceptions/{by_po['PO-506']['id']}").json()["tasks"][0]["id"]
    rec.call(
        "POST",
        f"/tasks/{other}/reassign",
        title="Refused: reassigning to someone off shift",
        headers={"X-Actor-Id": "HR-201"},
        json_body={"assignee_employee_id": "HR-101", "reason": "HR-102 on dock"},
    )
    rec.call(
        "POST",
        f"/tasks/{other}/reassign",
        title="Reassign with a reason (regional operations, to the on-shift store manager)",
        headers={"X-Actor-Id": "HR-501"},
        json_body={"assignee_employee_id": "HR-201", "reason": "HR-102 on dock"},
    )
    rec.call("GET", f"/tasks/{other}", title="Task detail: assignment history and audit events")
    rec.call(
        "GET",
        f"/exceptions/{by_po['PO-501']['id']}",
        title="Exception resolved only after verification",
        trim=lambda b: {k: b[k] for k in ("id", "status", "resolution", "events")},
    )

    rec.heading("Data quality")
    conflicting = derived(
        "goods_receipt_lines",
        lambda rows: [dict(r, accepted_qty="40") if r["source_record_id"] == "WMS-GRN501-B-L1" else r for r in rows],
    )
    rec.call(
        "POST",
        "/imports/goods_receipt_lines",
        title="A re-send with changed content is a CONFLICT, not an update",
        files={"file": ("goods_receipt_lines.csv", conflicting, "text/csv")},
    )
    po_with_bad_rows = derived(
        "purchase_order_lines",
        lambda rows: rows
        + [
            dict(rows[0], source_record_id="ERP-PO507-L1", po_number="PO-507", item_code="ERP-SKU-999"),
            dict(
                rows[1],
                source_record_id="ERP-PO508-L1",
                po_number="PO-508",
                ordered_qty="-5",
                expected_receipt_by="2026-10-15T12:00:00",
            ),
        ],
    )
    rec.call(
        "POST",
        "/imports/purchase_order_lines",
        title="Rows that fail validation are rejected with reasons",
        files={"file": ("purchase_order_lines.csv", po_with_bad_rows, "text/csv")},
    )
    client.post("/reconciliation-runs")
    report = rec.call("GET", "/data-quality", title="Unresolved mappings, conflicts and rejected rows")
    rec.call(
        "POST",
        f"/data-quality/conflicts/{report['conflicts'][0]['id']}/resolve",
        title="A data steward decides the conflict",
        headers={"X-Actor-Id": "HR-201"},
        json_body={"resolution": "KEEP_EXISTING", "reason": "WMS replayed an old count; 32 EA confirmed"},
    )

    rec.heading("Errors")
    rec.call(
        "POST",
        "/imports/goods_receipt_lines",
        title="File missing a required column",
        files={"file": ("bad.csv", b"source_record_id,receipt_number\nX,Y\n", "text/csv")},
    )
    rec.call("GET", "/tasks/00000000-0000-0000-0000-000000000000", title="Unknown resource")

    header = (
        "# API examples\n\n"
        "Generated by `scripts/capture_api_examples.py` from real requests against the application "
        "(fixture pack, frozen clock 2026-10-16T15:00:00+05:30). IDs other than exception IDs differ between runs. "
        "Timestamps in responses are UTC. See [API.md](API.md) for the contract and `openapi.json` for the schema.\n"
    )
    OUT.write_text(header + "".join(rec.parts))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
