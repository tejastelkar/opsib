#!/usr/bin/env bash
# Start a throwaway local PostgreSQL for development and tests, without Docker.
# Needs the PostgreSQL server binaries (initdb, pg_ctl) on PATH, e.g. `brew install postgresql@17`.
#
#   scripts/dev_postgres.sh start   # initialise (first time) and start on $PGPORT (default 55433)
#   scripts/dev_postgres.sh stop
#   scripts/dev_postgres.sh status
#
# Creates two databases: opsib (app) and opsib_test (tests). Data lives in .local/pgdata.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PGDATA_DIR="${PGDATA_DIR:-$ROOT/.local/pgdata}"
PGPORT="${PGPORT:-55433}"
PGUSER_NAME="${PGUSER_NAME:-opsib}"

cmd="${1:-start}"

case "$cmd" in
  start)
    if [ ! -f "$PGDATA_DIR/PG_VERSION" ]; then
      mkdir -p "$PGDATA_DIR"
      initdb -D "$PGDATA_DIR" -U "$PGUSER_NAME" --auth=trust --encoding=UTF8 --locale=C >/dev/null
    fi
    if ! pg_ctl -D "$PGDATA_DIR" status >/dev/null 2>&1; then
      pg_ctl -D "$PGDATA_DIR" -l "$PGDATA_DIR/server.log" -w \
        -o "-p $PGPORT -c listen_addresses=127.0.0.1 -c unix_socket_directories=''" start >/dev/null
    fi
    for db in opsib opsib_test; do
      psql -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER_NAME" -d postgres -tAc \
        "SELECT 1 FROM pg_database WHERE datname = '$db'" | grep -q 1 \
        || psql -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER_NAME" -d postgres -qc "CREATE DATABASE $db"
    done
    echo "PostgreSQL running on 127.0.0.1:$PGPORT"
    echo "  DATABASE_URL=postgresql+psycopg://$PGUSER_NAME@127.0.0.1:$PGPORT/opsib"
    echo "  TEST_DATABASE_URL=postgresql+psycopg://$PGUSER_NAME@127.0.0.1:$PGPORT/opsib_test"
    ;;
  stop)
    pg_ctl -D "$PGDATA_DIR" -m fast stop
    ;;
  status)
    pg_ctl -D "$PGDATA_DIR" status
    ;;
  *)
    echo "usage: $0 {start|stop|status}" >&2
    exit 2
    ;;
esac
