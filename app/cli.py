"""Command line entry points.

uv run python -m app.cli load-fixtures [DIR]   import every <dataset>.csv in DIR
uv run python -m app.cli reconcile             detect shortfalls and route tasks
uv run python -m app.cli demo [DIR]            both, then print the scenario table
uv run python -m app.cli export-openapi PATH   write the OpenAPI document
"""

import argparse
import json
import sys
from pathlib import Path

from app.config import get_settings
from app.container import build_services
from app.read_models import import_view, list_exceptions, list_tasks, run_view

DEFAULT_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "opsib_v1"


def _print(data: object) -> None:
    print(json.dumps(data, indent=2, default=str))


def load_fixtures(services, directory: Path) -> list[dict]:  # type: ignore[no-untyped-def]
    return [import_view(b) for b in services.imports.import_directory(directory)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    p_load = sub.add_parser("load-fixtures")
    p_load.add_argument("directory", nargs="?", type=Path, default=DEFAULT_FIXTURES)
    sub.add_parser("reconcile")
    p_demo = sub.add_parser("demo")
    p_demo.add_argument("directory", nargs="?", type=Path, default=DEFAULT_FIXTURES)
    p_openapi = sub.add_parser("export-openapi")
    p_openapi.add_argument("path", type=Path)
    args = parser.parse_args(argv)

    if args.command == "export-openapi":
        from app.api.main import create_app

        args.path.write_text(json.dumps(create_app().openapi(), indent=2) + "\n")
        print(f"wrote {args.path}")
        return 0

    services = build_services(get_settings())
    if args.command == "load-fixtures":
        _print([{k: v[k] for k in ("dataset", "status", "counts")} for v in load_fixtures(services, args.directory)])
    elif args.command == "reconcile":
        _print(run_view(services.reconciliation.run()))
    elif args.command == "demo":
        imports = load_fixtures(services, args.directory)
        run = services.reconciliation.run()
        with services.sessions() as session, session.begin():
            exceptions = list_exceptions(session, services.tenant_id, None, None, None)
            tasks = {t["exception_id"]: t for t in list_tasks(session, services.tenant_id, None, None, None, None)}
        print("Imports:")
        for v in imports:
            c = v["counts"]
            print(
                f"  {v['dataset']:<22} {v['status']:<22} rows={c['total_rows']} accepted={c['accepted']} "
                f"duplicates={c['duplicates']} rejected={c['rejected']} conflicts={c['conflicts']}"
            )
        print(f"\nRun {run.id} as of {run.evaluated_as_of.isoformat()} ({run.mapping_version}):")
        print(f"  outcomes: {run.summary['detection']['outcomes']}")
        print("\nExceptions:")
        for e in exceptions:
            task = tasks.get(e["id"], {})
            who = task.get("assignee_employee_id") or task.get("queue") or "-"
            print(
                f"  {e['po']['po_number']} line {e['po']['line_number']}: {e['status']} shortfall "
                f"{e['variance_qty']} {e['unit']} ({e['received_qty']}/{e['expected_qty']}) at "
                f"{e['canonical_location_id']} -> task {task.get('status', '-')} for {who}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
