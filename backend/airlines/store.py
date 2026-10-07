"""
store.py - where the Airlines reconciliation keeps what it must remember
between runs, behind one small interface with two interchangeable backends.

What is remembered
  * Fonepay RRN -> Ticket No history   (so a refund posted weeks after the
                                         sale can still find its ticket)
  * Pending airline refunds            (airline said "refunded", ledger side
                                         has not caught up yet)
  * Unresolved Fonepay refunds         (RRN not in the history yet - retried
                                         on every later run)
  * A log of runs                      (who ran what, saved or preview)

Backends
  PostgresStore  Shared, permanent, safe for many people at once. Used when a
                 database URL is configured (Neon / Supabase / any Postgres):
                 env var AIRLINES_DATABASE_URL or DATABASE_URL, or Streamlit
                 secret  [database] url = "postgresql://..."
  LocalStore     CSV files on the server's disk (the original offline
                 behaviour). Not shared and not durable on hosted Streamlit;
                 used only when no database URL is configured.

Concurrency rules (Postgres)
  * History is append-only with a UNIQUE (rrn, ticket) key: two people adding
    the same file at once cannot create duplicates or lose rows.
  * Pending refunds are one row per ticket. A run adds/updates rows that are
    still pending and marks matched ones resolved; it NEVER rewrites the whole
    list, and a resolved ticket is never re-opened by a slower, older run.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import date, datetime

import pandas as pd

from . import refund_state
from .prep import normalize_ticket

TICKET_COL = "Ticket No"
FP_UNRESOLVED_FILE = "fonepay_refund_unresolved.csv"
_BLANK = {"", "NAN", "NONE", "N/A", "NA", "N.A", "NULL"}


class StoreError(RuntimeError):
    """Storage problem with a message that is safe to show to the operator."""


def new_run_id() -> str:
    return datetime.now().strftime("run-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


# ───────────────────────────── helpers ────────────────────────────────────────

def row_key(values) -> str:
    """Stable identity for a list of values (order matters)."""
    joined = "|".join("" if _cell(v) is None else _cell(v).strip() for v in values)
    return hashlib.md5(joined.encode("utf-8")).hexdigest()


def row_key_from_dict(d: dict) -> str:
    """Stable identity for a raw row regardless of column order or whether a
    number came in as 1450, 1450.0 or "1450" (used for unresolved Fonepay refunds)."""
    return row_key([f"{k}={'' if _cell(d[k]) is None else _cell(d[k]).strip()}" for k in sorted(d)])


def _cell(v):
    """DataFrame cell -> JSON-safe string (or None)."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, (pd.Timestamp, datetime, date)):
        ts = pd.Timestamp(v)
        return ts.strftime("%Y-%m-%d") if ts == ts.normalize() else ts.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))          # 1450.0 -> "1450" so the same value always looks the same
    return str(v)


def _records(df: pd.DataFrame, drop=()) -> list[dict]:
    cols = [c for c in df.columns if c not in drop]
    return [{c: _cell(v) for c, v in zip(cols, row)} for row in df[cols].itertuples(index=False, name=None)]


def _frame_from_payloads(payloads: list[dict]) -> pd.DataFrame:
    if not payloads:
        return pd.DataFrame()
    cols: list[str] = []
    for p in payloads:
        for k in p:
            if k not in cols:
                cols.append(k)
    return pd.DataFrame([[p.get(c) for c in cols] for p in payloads], columns=cols, dtype=object)


def _clean_history_frame(df, rrn_col, date_col, lookup_fields):
    """Normalise an incoming history frame -> columns rrn, ticket, extra(dict), txn_date,
    rrn_norm, ticket_norm. Returns (clean_df, skipped_count)."""
    n_in = len(df)
    if df is None or df.empty or rrn_col not in df.columns or TICKET_COL not in df.columns:
        return pd.DataFrame(), n_in
    d = pd.DataFrame({
        "rrn": df[rrn_col].astype(str).str.strip(),
        "ticket": df[TICKET_COL].astype(str).str.strip(),
    })
    d["rrn_norm"] = d["rrn"].map(normalize_ticket)
    d["ticket_norm"] = d["ticket"].map(normalize_ticket)
    d["txn_date"] = (pd.to_datetime(df[date_col], errors="coerce").dt.date
                     if date_col and date_col in df.columns else None)
    extras = [c for c in (lookup_fields or []) if c not in (rrn_col, TICKET_COL) and c in df.columns]
    d["extra"] = [
        {c: _cell(v) for c, v in zip(extras, row)} for row in df[extras].itertuples(index=False, name=None)
    ] if extras else [{} for _ in range(len(d))]
    ok = ~d["rrn_norm"].isin(_BLANK) & ~d["ticket_norm"].isin(_BLANK)
    d = d[ok].drop_duplicates(subset=["rrn_norm", "ticket_norm"], keep="first")
    return d.reset_index(drop=True), n_in - len(d)


# ───────────────────────────── interface ──────────────────────────────────────

class BaseStore:
    backend = "base"
    shared = False

    def describe(self) -> str:  # pragma: no cover - trivial
        return self.backend

    # history
    def history_add(self, df, rrn_col, date_col, lookup_fields, retention_days=None,
                    operator="", source="") -> dict: raise NotImplementedError
    def history_frame(self, rrn_col, date_col, lookup_fields, extra=None) -> pd.DataFrame: raise NotImplementedError
    def history_stats(self) -> dict: raise NotImplementedError
    def history_export(self, rrn_col, date_col) -> pd.DataFrame: raise NotImplementedError
    # pending airline refunds
    def pending_load(self) -> pd.DataFrame: raise NotImplementedError
    def pending_sync(self, unmatched_df, resolved_tickets, ticket_col, run_id="", operator="") -> None: raise NotImplementedError
    # unresolved fonepay refunds
    def fp_unresolved_load(self) -> pd.DataFrame: raise NotImplementedError
    def fp_unresolved_sync(self, still_unresolved, resolved_keys, run_id="", operator="") -> None: raise NotImplementedError
    # runs
    def log_run(self, info: dict) -> None: raise NotImplementedError
    def recent_runs(self, limit=10) -> list[dict]: raise NotImplementedError


# ───────────────────────────── local files ────────────────────────────────────

class LocalStore(BaseStore):
    """The original CSV-on-disk behaviour. NOT shared between servers/users."""
    backend = "local"
    shared = False

    def __init__(self, pending_path=None, history_path=None, unresolved_path=None, runs_path=None):
        self.pending_path = pending_path
        self.history_path = history_path
        self.unresolved_path = unresolved_path
        self.runs_path = runs_path

    @classmethod
    def from_cfg(cls, cfg, resolve_path):
        pending = resolve_path((cfg.get("refund_reconciliation") or {}).get("state_file"))
        history = resolve_path((cfg.get("fonepay_rrn_lookup") or {}).get("state_file"))
        base = os.path.dirname(pending or history or "") or None
        return cls(
            pending_path=pending, history_path=history,
            unresolved_path=os.path.join(base, FP_UNRESOLVED_FILE) if base else None,
            runs_path=os.path.join(base, "recon_runs.jsonl") if base else None,
        )

    def describe(self):
        return "Local files on this server (not shared, may reset on hosted Streamlit)"

    # history ---------------------------------------------------------------
    def history_add(self, df, rrn_col, date_col, lookup_fields, retention_days=None,
                    operator="", source=""):
        if not self.history_path:
            raise StoreError("No fonepay_rrn_lookup.state_file configured for local storage.")
        rows_in = len(df) if df is not None else 0
        if df is None or df.empty or rrn_col not in df.columns or TICKET_COL not in df.columns:
            return {"rows_in": rows_in, "added": 0, "skipped": rows_in,
                    "total": self.history_stats()["rows"]}
        ok = (~df[rrn_col].astype(str).map(normalize_ticket).isin(_BLANK)
              & ~df[TICKET_COL].astype(str).map(normalize_ticket).isin(_BLANK))
        keep = [c for c in [rrn_col, TICKET_COL, *(lookup_fields or []), date_col]
                if c and c in df.columns]
        slim = df.loc[ok, list(dict.fromkeys(keep))].copy()
        before = self.history_stats()["rows"]
        merged = refund_state.merge_reference_lookup(
            slim, rrn_col, self.history_path,
            retention_days=180 if retention_days is None else retention_days,
            date_col=date_col,
        )
        return {"rows_in": rows_in, "added": max(0, len(merged) - before),
                "skipped": int((~ok).sum()), "total": len(merged)}

    def history_frame(self, rrn_col, date_col, lookup_fields, extra=None):
        if extra is not None and not extra.empty and self.history_path:
            return refund_state.merge_reference_lookup(
                extra, rrn_col, self.history_path, date_col=date_col, save=False)
        if extra is not None and not extra.empty:
            return extra
        return refund_state.load_reference_lookup(self.history_path)

    def history_stats(self):
        df = refund_state.load_reference_lookup(self.history_path)
        if df.empty:
            return {"rows": 0, "rrns": 0, "min_date": None, "max_date": None, "last_added": None}
        seen = pd.to_datetime(df.get("_seen_date"), errors="coerce")
        rrn_col = df.columns[0]
        last = None
        if self.history_path and os.path.exists(self.history_path):
            last = datetime.fromtimestamp(os.path.getmtime(self.history_path))
        return {"rows": len(df), "rrns": int(df[rrn_col].nunique()),
                "min_date": seen.min().date() if seen.notna().any() else None,
                "max_date": seen.max().date() if seen.notna().any() else None,
                "last_added": last}

    def history_export(self, rrn_col, date_col):
        df = refund_state.load_reference_lookup(self.history_path)
        if df.empty:
            return pd.DataFrame(columns=[rrn_col, TICKET_COL, date_col])
        out = pd.DataFrame({rrn_col: df[rrn_col] if rrn_col in df.columns else df.iloc[:, 0],
                            TICKET_COL: df.get(TICKET_COL)})
        out[date_col] = pd.to_datetime(df.get("_seen_date"), errors="coerce").dt.strftime("%Y-%m-%d")
        return out

    # pending ----------------------------------------------------------------
    def pending_load(self):
        return refund_state.load_pending(self.pending_path)

    def pending_sync(self, unmatched_df, resolved_tickets, ticket_col, run_id="", operator=""):
        # original behaviour: the still-unmatched list simply replaces the file
        refund_state.save_pending(
            unmatched_df.drop(columns=["Days Outstanding", "Refund Status"], errors="ignore"),
            self.pending_path)

    # unresolved fonepay -------------------------------------------------------
    def fp_unresolved_load(self):
        if not self.unresolved_path or not os.path.exists(self.unresolved_path):
            return pd.DataFrame()
        try:
            df = pd.read_csv(self.unresolved_path, dtype=str, keep_default_na=False, na_values=[""])
        except Exception:
            return pd.DataFrame()
        if "_first_seen" in df.columns:
            df["_first_seen"] = pd.to_datetime(df["_first_seen"], errors="coerce")
        return df

    def fp_unresolved_sync(self, still_unresolved, resolved_keys, run_id="", operator=""):
        if not self.unresolved_path:
            return
        os.makedirs(os.path.dirname(self.unresolved_path), exist_ok=True)
        prev = self.fp_unresolved_load()
        first = {}
        if not prev.empty and "_row_key" in prev.columns and "_first_seen" in prev.columns:
            first = dict(zip(prev["_row_key"], prev["_first_seen"]))
        out = still_unresolved.copy()
        if not out.empty:
            today = pd.Timestamp.now().normalize()
            out["_first_seen"] = [first.get(k, today) for k in out["_row_key"]]
            out["_first_seen"] = pd.to_datetime(out["_first_seen"]).dt.strftime("%Y-%m-%d")
        out.to_csv(self.unresolved_path, index=False)

    # runs ---------------------------------------------------------------------
    def log_run(self, info):
        if not self.runs_path:
            return
        os.makedirs(os.path.dirname(self.runs_path), exist_ok=True)
        with open(self.runs_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(info, default=str) + "\n")

    def recent_runs(self, limit=10):
        if not self.runs_path or not os.path.exists(self.runs_path):
            return []
        with open(self.runs_path, encoding="utf-8") as fh:
            lines = fh.readlines()[-limit:]
        return [json.loads(x) for x in reversed(lines)]


# ───────────────────────────── postgres ───────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fonepay_history (
    rrn_norm     text        NOT NULL,
    ticket_norm  text        NOT NULL,
    rrn          text        NOT NULL,
    ticket_no    text        NOT NULL,
    extra        json,
    txn_date     date,
    seen_date    date        NOT NULL,
    added_at     timestamptz NOT NULL DEFAULT now(),
    added_by     text,
    source       text,
    PRIMARY KEY (rrn_norm, ticket_norm)
);
CREATE INDEX IF NOT EXISTS fonepay_history_ticket_idx ON fonepay_history (ticket_norm);
CREATE INDEX IF NOT EXISTS fonepay_history_seen_idx   ON fonepay_history (seen_date);

CREATE TABLE IF NOT EXISTS pending_refunds (
    ticket_norm  text PRIMARY KEY,
    ticket       text,
    refund_date  date,
    payload      json        NOT NULL,
    status       text        NOT NULL DEFAULT 'pending',
    first_seen   date        NOT NULL DEFAULT current_date,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text,
    run_id       text,
    resolved_at  timestamptz,
    resolved_by  text
);
CREATE INDEX IF NOT EXISTS pending_refunds_status_idx ON pending_refunds (status);

CREATE TABLE IF NOT EXISTS fonepay_refund_unresolved (
    row_key      text PRIMARY KEY,
    rrn_norm     text,
    payload      json        NOT NULL,
    status       text        NOT NULL DEFAULT 'pending',
    first_seen   date        NOT NULL DEFAULT current_date,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text,
    run_id       text,
    resolved_at  timestamptz,
    resolved_by  text
);
CREATE INDEX IF NOT EXISTS fp_unresolved_status_idx ON fonepay_refund_unresolved (status);

CREATE TABLE IF NOT EXISTS recon_runs (
    run_id      text PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    operator    text,
    persisted   boolean,
    files       json,
    summary     json
);
"""

_schema_ready: set[str] = set()
_schema_lock = threading.Lock()


def _db_safe(fn):
    """Database errors become readable StoreErrors; a missing table (database
    reset / restored while the app was running) is re-created and the call retried once."""
    import functools

    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        import psycopg
        for attempt in (1, 2):
            try:
                return fn(self, *a, **kw)
            except StoreError:
                raise
            except psycopg.errors.UndefinedTable:
                if attempt == 2:
                    raise StoreError("The database tables are missing and could not be re-created.")
                _schema_ready.discard(self.url)
                self._ensure_schema()
            except psycopg.Error as exc:
                msg = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
                raise StoreError(f"Database error ({type(exc).__name__}): {msg}") from exc
    return wrapper


class PostgresStore(BaseStore):
    backend = "postgres"
    shared = True

    def __init__(self, url: str):
        try:
            import psycopg  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise StoreError("The 'psycopg' package is not installed (pip install \"psycopg[binary]\").") from exc
        self.url = url
        self._ensure_schema()

    # connection -----------------------------------------------------------------
    def _connect(self):
        import psycopg
        try:
            return psycopg.connect(self.url, connect_timeout=20)
        except Exception as exc:
            raise StoreError(f"Could not connect to the database: {type(exc).__name__}: "
                             f"{str(exc).splitlines()[0] if str(exc) else ''}") from exc

    def _ensure_schema(self):
        with _schema_lock:
            if self.url in _schema_ready:
                return
            with self._connect() as con:
                con.execute("SELECT pg_advisory_xact_lock(727001)")   # one creator at a time
                con.execute(_SCHEMA)
            _schema_ready.add(self.url)

    def describe(self):
        host = self.url.split("@")[-1].split("/")[0].split("?")[0] if "@" in self.url else ""
        return f"Shared database ({host})" if host else "Shared database"

    # history ---------------------------------------------------------------------
    def history_add(self, df, rrn_col, date_col, lookup_fields, retention_days=None,
                    operator="", source=""):
        clean, skipped = _clean_history_frame(df, rrn_col, date_col, lookup_fields)
        rows_in = len(df) if df is not None else 0
        if clean.empty:
            return {"rows_in": rows_in, "added": 0, "skipped": skipped, "total": self.history_stats()["rows"]}
        today = date.today()
        data = [
            (r.rrn_norm, r.ticket_norm, r.rrn, r.ticket,
             json.dumps(r.extra) if r.extra else None,
             r.txn_date if isinstance(r.txn_date, date) and not pd.isna(r.txn_date) else None,
             r.txn_date if isinstance(r.txn_date, date) and not pd.isna(r.txn_date) else today,
             operator or None, source or None)
            for r in clean.itertuples(index=False)
        ]
        with self._connect() as con:
            with con.cursor() as cur:
                added = 0
                for i in range(0, len(data), 5000):
                    cur.executemany(
                        "INSERT INTO fonepay_history (rrn_norm, ticket_norm, rrn, ticket_no, extra, txn_date,"
                        " seen_date, added_by, source) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                        " ON CONFLICT (rrn_norm, ticket_norm) DO NOTHING", data[i:i + 5000])
                    added += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                if retention_days:
                    cur.execute("DELETE FROM fonepay_history WHERE seen_date < current_date - %s::int",
                                (int(retention_days),))
                cur.execute("SELECT count(*) FROM fonepay_history")
                total = cur.fetchone()[0]
        return {"rows_in": rows_in, "added": added, "skipped": skipped, "total": total}

    def history_frame(self, rrn_col, date_col, lookup_fields, extra=None):
        with self._connect() as con:
            rows = con.execute(
                "SELECT rrn, ticket_no, extra, txn_date, seen_date FROM fonepay_history").fetchall()
        extras = [c for c in (lookup_fields or []) if c not in (rrn_col, TICKET_COL)]
        recs = []
        for rrn, ticket, ex, txn_date, seen in rows:
            ex = ex if isinstance(ex, dict) else (json.loads(ex) if ex else {})
            rec = {rrn_col: rrn, TICKET_COL: ticket}
            for c in extras:
                rec[c] = ex.get(c)
            if date_col:
                rec[date_col] = txn_date.isoformat() if txn_date else None
            rec["_seen_date"] = pd.Timestamp(seen)
            recs.append(rec)
        hist = pd.DataFrame(recs)
        if extra is not None and not extra.empty:
            clean, _ = _clean_history_frame(extra, rrn_col, date_col, lookup_fields)
            if not clean.empty:
                add = pd.DataFrame({rrn_col: clean["rrn"], TICKET_COL: clean["ticket"]})
                if date_col:
                    add[date_col] = [d.isoformat() if isinstance(d, date) and not pd.isna(d) else None
                                     for d in clean["txn_date"]]
                add["_seen_date"] = [pd.Timestamp(d) if isinstance(d, date) and not pd.isna(d)
                                     else pd.Timestamp.now().normalize() for d in clean["txn_date"]]
                if hist.empty:
                    hist = add
                else:
                    key_h = hist[rrn_col].map(normalize_ticket) + "||" + hist[TICKET_COL].map(normalize_ticket)
                    key_a = add[rrn_col].map(normalize_ticket) + "||" + add[TICKET_COL].map(normalize_ticket)
                    hist = pd.concat([hist, add[~key_a.isin(set(key_h))]], ignore_index=True, sort=False)
        return hist

    def history_stats(self):
        with self._connect() as con:
            n, rrns, lo, hi, last = con.execute(
                "SELECT count(*), count(DISTINCT rrn_norm), min(seen_date), max(seen_date), max(added_at)"
                " FROM fonepay_history").fetchone()
        return {"rows": n, "rrns": rrns, "min_date": lo, "max_date": hi, "last_added": last}

    def history_export(self, rrn_col, date_col):
        with self._connect() as con:
            rows = con.execute(
                "SELECT rrn, ticket_no, coalesce(txn_date, seen_date) FROM fonepay_history"
                " ORDER BY seen_date, rrn").fetchall()
        return pd.DataFrame(rows, columns=[rrn_col, TICKET_COL, date_col])

    # pending ------------------------------------------------------------------------
    def pending_load(self):
        with self._connect() as con:
            rows = con.execute(
                "SELECT payload FROM pending_refunds WHERE status='pending' ORDER BY first_seen, ticket_norm"
            ).fetchall()
        df = _frame_from_payloads([r[0] if isinstance(r[0], dict) else json.loads(r[0]) for r in rows])
        if not df.empty and "_RefundDate" in df.columns:
            df["_RefundDate"] = pd.to_datetime(df["_RefundDate"], errors="coerce")
        return df

    def pending_sync(self, unmatched_df, resolved_tickets, ticket_col, run_id="", operator=""):
        base = unmatched_df.drop(columns=["Days Outstanding", "Refund Status"], errors="ignore")
        payloads = _records(base, drop=("_norm_ticket",))
        data = []
        for rec in payloads:
            tnorm = normalize_ticket(rec.get(ticket_col) or "")
            if tnorm in _BLANK:
                tnorm = "ROW:" + row_key(rec.values())
            rd = rec.get("_RefundDate")
            data.append((tnorm, rec.get(ticket_col), rd if rd else None, json.dumps(rec),
                         run_id or None, operator or None))
        with self._connect() as con:
            with con.cursor() as cur:
                if data:
                    cur.executemany(
                        "INSERT INTO pending_refunds (ticket_norm, ticket, refund_date, payload, status,"
                        " run_id, updated_by) VALUES (%s,%s,%s,%s,'pending',%s,%s)"
                        " ON CONFLICT (ticket_norm) DO UPDATE SET payload=EXCLUDED.payload,"
                        " refund_date=EXCLUDED.refund_date, updated_at=now(), run_id=EXCLUDED.run_id,"
                        " updated_by=EXCLUDED.updated_by WHERE pending_refunds.status='pending'", data)
                resolved = sorted(t for t in (resolved_tickets or []) if t)
                if resolved:
                    cur.execute(
                        "UPDATE pending_refunds SET status='resolved', resolved_at=now(), resolved_by=%s,"
                        " run_id=%s WHERE ticket_norm = ANY(%s) AND status='pending'",
                        (operator or None, run_id or None, resolved))

    # unresolved fonepay ---------------------------------------------------------------
    def fp_unresolved_load(self):
        with self._connect() as con:
            rows = con.execute(
                "SELECT payload, row_key, first_seen FROM fonepay_refund_unresolved WHERE status='pending'"
                " ORDER BY first_seen, row_key").fetchall()
        df = _frame_from_payloads([r[0] if isinstance(r[0], dict) else json.loads(r[0]) for r in rows])
        if not df.empty:
            df["_row_key"] = [r[1] for r in rows]
            df["_first_seen"] = pd.to_datetime([r[2] for r in rows])
        return df

    def fp_unresolved_sync(self, still_unresolved, resolved_keys, run_id="", operator=""):
        keys = list(still_unresolved["_row_key"]) if not still_unresolved.empty else []
        payloads = _records(still_unresolved, drop=("_row_key", "_first_seen")) if keys else []
        data = [(k, normalize_ticket(next(iter(p.values()), "") or ""), json.dumps(p), run_id or None,
                 operator or None) for k, p in zip(keys, payloads)]
        with self._connect() as con:
            with con.cursor() as cur:
                if data:
                    cur.executemany(
                        "INSERT INTO fonepay_refund_unresolved (row_key, rrn_norm, payload, status, run_id,"
                        " updated_by) VALUES (%s,%s,%s,'pending',%s,%s) ON CONFLICT (row_key) DO UPDATE SET"
                        " updated_at=now(), run_id=EXCLUDED.run_id WHERE fonepay_refund_unresolved.status='pending'",
                        data)
                resolved = sorted(set(resolved_keys or []) - set(keys))
                if resolved:
                    cur.execute(
                        "UPDATE fonepay_refund_unresolved SET status='resolved', resolved_at=now(),"
                        " resolved_by=%s, run_id=%s WHERE row_key = ANY(%s) AND status='pending'",
                        (operator or None, run_id or None, resolved))

    # runs ---------------------------------------------------------------------------------
    def log_run(self, info):
        with self._connect() as con:
            con.execute(
                "INSERT INTO recon_runs (run_id, operator, persisted, files, summary) VALUES (%s,%s,%s,%s,%s)"
                " ON CONFLICT (run_id) DO NOTHING",
                (info.get("run_id") or new_run_id(), info.get("operator"), bool(info.get("persisted")),
                 json.dumps(info.get("files") or []), json.dumps(info.get("summary") or {}, default=str)))

    def recent_runs(self, limit=10):
        with self._connect() as con:
            rows = con.execute(
                "SELECT run_id, created_at, operator, persisted, files, summary FROM recon_runs"
                " ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
        return [{"run_id": r[0], "created_at": r[1], "operator": r[2], "persisted": r[3],
                 "files": r[4], "summary": r[5]} for r in rows]


for _name in ("history_add", "history_frame", "history_stats", "history_export", "pending_load",
              "pending_sync", "fp_unresolved_load", "fp_unresolved_sync", "log_run", "recent_runs"):
    setattr(PostgresStore, _name, _db_safe(getattr(PostgresStore, _name)))


# ───────────────────────────── factory ─────────────────────────────────────────────────

def database_url() -> str | None:
    """Configured database URL: env var first, then Streamlit secrets."""
    for name in ("AIRLINES_DATABASE_URL", "DATABASE_URL"):
        if os.environ.get(name):
            return os.environ[name]
    try:  # only present when running under Streamlit with a secrets file
        import streamlit as st
        sec = st.secrets
        if "database" in sec and "url" in sec["database"]:
            return str(sec["database"]["url"])
        for name in ("AIRLINES_DATABASE_URL", "DATABASE_URL"):
            if name in sec:
                return str(sec[name])
    except Exception:  # noqa: BLE001 - no streamlit / no secrets file
        pass
    return None


def get_store(cfg, resolve_path, url: str | None = None) -> BaseStore:
    """Postgres when a URL is configured, otherwise local files."""
    url = url or database_url()
    if url:
        return PostgresStore(url)
    return LocalStore.from_cfg(cfg, resolve_path)
