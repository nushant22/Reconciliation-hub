"""Storage tests for the Airlines reconciliation.

Every behaviour is checked on BOTH backends (local files and a real Postgres)
where it should be identical; the Postgres-only tests cover what matters when
several people use the app at once. The Postgres tests use a throw-away
server from the `pgserver` package (or TEST_DATABASE_URL); they are skipped
if neither is available.
"""
import os
import threading
from datetime import date, timedelta

import pandas as pd
import pytest

from backend.airlines import engine, store as store_mod
from backend.tests.airlines_fixtures import build_history_file, build_sample_files

OLD = (date.today() - timedelta(days=30)).isoformat()


# ───────────────────────────── fixtures ───────────────────────────────────────

@pytest.fixture(scope="session")
def pg_url(tmp_path_factory):
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return
    pgserver = pytest.importorskip("pgserver")
    pytest.importorskip("psycopg")
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("pgdata")))
    yield srv.get_uri()
    srv.cleanup()


def _clean_pg(url):
    import psycopg
    with psycopg.connect(url) as con:
        con.execute("DROP TABLE IF EXISTS fonepay_history, pending_refunds, "
                    "fonepay_refund_unresolved, recon_runs CASCADE")
    store_mod._schema_ready.discard(url)


@pytest.fixture(params=["local", "postgres"])
def store(request, tmp_path):
    if request.param == "local":
        base = tmp_path / "state"
        return store_mod.LocalStore(
            pending_path=str(base / "pending_refunds.csv"),
            history_path=str(base / "fonepay_rrn_lookup.csv"),
            unresolved_path=str(base / store_mod.FP_UNRESOLVED_FILE),
            runs_path=str(base / "runs.jsonl"))
    url = request.getfixturevalue("pg_url")
    _clean_pg(url)
    return store_mod.PostgresStore(url)


def _uploads(folder, **kw):
    paths = build_sample_files(str(folder), **kw)
    out = []
    for p in paths.values():
        with open(p, "rb") as fh:
            out.append((os.path.basename(p), fh.read()))
    return out


def _history_upload(tmp_path, rows, name="old_fonepay.xlsx"):
    p = build_history_file(str(tmp_path / name), rows)
    with open(p, "rb") as fh:
        return [(name, fh.read())]


def _buddha(results):
    return {r["airline_key"]: r for r in results}["airline_3"]


# ───────────────────────────── the problem we are solving ─────────────────────────

def test_refund_posted_days_after_sale_is_matched_via_history(store, tmp_path):
    """The Fonepay refund has only an RRN whose sale was on an earlier day.
    1) Without history it is kept as 'unresolved' (not lost).
    2) After the historical file is uploaded it resolves on the NEXT run - even
       if no Fonepay Refund file is dropped that day."""
    uploads = _uploads(tmp_path / "day", late_refund=True)

    res, _, log = engine.run_airline_reconciliation(uploads, store=store)
    b = _buddha(res)
    assert "Fonepay Refund Recon" not in b["recon"]                         # nothing to match yet
    unres = b["unrecon"]["Fonepay Refund Unresolved"]
    assert list(unres["RETRIEVAL_REFERENCE_NUMBER"]) == ["R77"]
    assert list(unres["Reason"]) == ["RRN not found in Fonepay history"]
    assert "3330000000005" in set(b["unrecon"]["Refund Unrecon"]["TICKET NO"])   # airline refund still pending

    # upload the old sale day once
    rep = engine.add_history_files(
        _history_upload(tmp_path, [("R77", "3330000000005", OLD), ("R88", "3330000000099", OLD)]),
        store=store)
    assert rep[0]["error"] == "" and rep[0]["added"] == 2

    # next day: same airline files but NO fonepay refund file
    next_day = [u for u in uploads if u[0] != "fonepay_refund.xlsx"]
    res2, _, log2 = engine.run_airline_reconciliation(next_day, store=store)
    b2 = _buddha(res2)
    assert "retrying 1 Fonepay refund" in log2
    assert len(b2["recon"]["Fonepay Refund Recon"]) == 1
    assert "Fonepay Refund Unresolved" not in b2["unrecon"]
    assert "3330000000005" not in set(b2["unrecon"]["Refund Unrecon"]["TICKET NO"])   # no longer pending
    assert store.fp_unresolved_load().empty


def test_daily_fonepay_ledger_feeds_the_history_automatically(store, tmp_path):
    before = store.history_stats()["rows"]
    engine.run_airline_reconciliation(_uploads(tmp_path / "d1"), store=store)
    after = store.history_stats()["rows"]
    assert after == before + 4          # R2, R6, R9, R5 from the sample Fonepay ledger
    engine.run_airline_reconciliation(_uploads(tmp_path / "d2"), store=store)   # same file again
    assert store.history_stats()["rows"] == after                                # no duplicates


def test_preview_run_saves_nothing(store, tmp_path):
    uploads = _uploads(tmp_path / "day", late_refund=True)
    res, _, log = engine.run_airline_reconciliation(uploads, store=store, persist=False)
    assert "PREVIEW" in log
    assert store.history_stats()["rows"] == 0
    assert store.pending_load().empty
    assert store.fp_unresolved_load().empty
    # ...but the report is still produced from today's data
    assert len(res) == 3


def test_history_upload_is_idempotent_and_reports_bad_files(store, tmp_path):
    up = _history_upload(tmp_path, [("R1", "T1", OLD), ("R2", "T2", OLD), ("R2", "T3", OLD)])
    first = engine.add_history_files(up, store=store)[0]
    again = engine.add_history_files(up, store=store)[0]
    assert (first["rows_read"], first["added"]) == (3, 3)          # one RRN, two tickets kept
    assert again["added"] == 0 and store.history_stats()["rows"] == 3

    import openpyxl, io
    wb = openpyxl.Workbook(); wb.active.append(["Foo", "Bar"]); wb.active.append([1, 2])
    buf = io.BytesIO(); wb.save(buf)
    bad = engine.add_history_files([("wrong.xlsx", buf.getvalue()), up[0]], store=store)
    assert "not found" in bad[0]["error"] and bad[1]["error"] == ""   # one bad file doesn't stop the rest


def test_history_backup_round_trip(store, tmp_path):
    engine.add_history_files(_history_upload(tmp_path, [("R1", "T1", OLD), ("R2", "T2", OLD)]), store=store)
    backup = engine.history_backup(store=store)
    assert len(backup) == 2
    csv_path = tmp_path / "backup.csv"
    backup.to_csv(csv_path, index=False)

    fresh = store_mod.LocalStore(history_path=str(tmp_path / "fresh" / "h.csv"))
    rep = engine.add_history_files([("backup.csv", csv_path.read_bytes())], store=fresh)[0]
    assert rep["error"] == "" and rep["added"] == 2


# ───────────────────────────── many people at once (Postgres) ───────────────────────

@pytest.fixture()
def pg(pg_url):
    _clean_pg(pg_url)
    return store_mod.PostgresStore(pg_url)


def _hist_df(n, offset=0):
    return pd.DataFrame({
        "Retrieval Reference No": [f"RRN{i}" for i in range(offset, offset + n)],
        "Ticket No": [f"TKT{i}" for i in range(offset, offset + n)],
        "Recorded Date": [OLD] * n,
    })


def test_simultaneous_history_uploads_lose_nothing_and_never_duplicate(pg):
    """Six people load overlapping files at the same moment."""
    errors = []

    def work(offset):
        try:
            pg.history_add(_hist_df(400, offset), "Retrieval Reference No", "Recorded Date", ["Ticket No"])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(o,)) for o in (0, 100, 200, 300, 400, 500)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert not errors
    assert pg.history_stats()["rows"] == 900     # rows 0..899, each exactly once


def test_pending_refund_resolved_by_one_run_is_not_reopened_by_a_slower_run(pg):
    unmatched = pd.DataFrame({"TICKET NO": ["T1", "T2"], "REF": ["100", "200"],
                              "_RefundDate": pd.to_datetime(["2026-09-01", "2026-09-02"])})
    pg.pending_sync(unmatched, set(), "TICKET NO", run_id="A", operator="alice")
    assert len(pg.pending_load()) == 2

    # Bob's run matches T1 ...
    pg.pending_sync(unmatched.iloc[[1]], {"T1"}, "TICKET NO", run_id="B", operator="bob")
    assert list(pg.pending_load()["TICKET NO"]) == ["T2"]

    # ... then Alice's older/partial run (no cancellation file) still lists T1 as unmatched
    pg.pending_sync(unmatched, set(), "TICKET NO", run_id="C", operator="alice")
    assert list(pg.pending_load()["TICKET NO"]) == ["T2"]      # T1 stays resolved


def test_pending_and_history_survive_a_new_connection(pg_url, pg):
    pg.history_add(_hist_df(3), "Retrieval Reference No", "Recorded Date", ["Ticket No"])
    again = store_mod.PostgresStore(pg_url)          # like the app restarting
    assert again.history_stats()["rows"] == 3


def test_run_log_records_who_ran_what(store, tmp_path):
    engine.run_airline_reconciliation(_uploads(tmp_path / "d"), store=store, operator="alice")
    runs = store.recent_runs(5)
    assert runs and runs[0]["operator"] == "alice"
    assert "Shree Airlines" in runs[0]["summary"]


def test_blank_database_url_means_local_files(monkeypatch, tmp_path):
    monkeypatch.delenv("AIRLINES_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(engine, "STATE_DIR", str(tmp_path))
    assert engine.default_store(engine.load_config()).backend == "local"


def test_tables_dropped_while_running_are_recreated(pg_url, pg):
    pg.history_add(_hist_df(2), "Retrieval Reference No", "Recorded Date", ["Ticket No"])
    _clean_pg(pg_url)                                   # someone resets the database under a running app
    assert pg.history_stats()["rows"] == 0              # no crash: tables are re-created
    pg.history_add(_hist_df(2), "Retrieval Reference No", "Recorded Date", ["Ticket No"])
    assert pg.history_stats()["rows"] == 2


def test_unreachable_database_gives_a_readable_error():
    pytest.importorskip("psycopg")
    with pytest.raises(store_mod.StoreError, match="Could not connect"):
        store_mod.PostgresStore("postgresql://nobody:x@127.0.0.1:1/none?connect_timeout=2")
