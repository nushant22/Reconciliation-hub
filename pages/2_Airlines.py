"""Airlines Reconciliation — Streamlit page.

Reconciles airline sales files (Shree / Yeti / Buddha, or whatever
`backend/airlines/config.yaml` names them) against the eSewa and Fonepay
ledgers by ticket number, including the refund / cancellation / cashback
checks. Every rule — sources, header rows, match keys, amount columns,
report fields, prep switches, refund aging, carry-forward state — is read
from `backend/airlines/config.yaml`; you can view and edit that file on
this page.

Remembered between runs (so a refund posted days after the sale can still find
its ticket): the Fonepay RRN -> Ticket No history, pending refunds, and Fonepay
refunds still waiting for a ticket. With a database configured (Streamlit secret
`[database] url = "postgresql://..."`) this is shared by everyone and permanent;
without one it falls back to local files on the server (not shared).

Drop all of the day's files into the uploader in one go. File names do not
matter: each file is recognised by its column headers.
"""

from __future__ import annotations

import io
import logging
import os
import zipfile
from datetime import date

import pandas as pd
import streamlit as st
import yaml

from backend.airlines import engine
from backend.airlines import store as store_mod

logging.basicConfig(
    level=os.environ.get("RECON_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)

BRAND_GREEN = "#60BB46"
CHARCOAL = "#1F2937"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

st.set_page_config(
    page_title="Airlines Reconciliation",
    page_icon="✈️",
    layout="wide",
)

st.markdown(
    f"""
    <style>
      .stApp {{ background: #F8FAFC; }}
      .recon-banner {{
        background: {BRAND_GREEN}; color: #fff; padding: 18px 24px; border-radius: 10px;
        display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px;
      }}
      .recon-banner h1 {{ font-size: 1.35rem; margin: 0; font-weight: 700; letter-spacing: -0.01em; }}
      .recon-badge {{ background: rgba(255,255,255,.22); padding: 6px 14px; border-radius: 999px;
                      font-size: .82rem; font-weight: 600; }}
      .step {{ color: {CHARCOAL}; font-weight: 700; font-size: .78rem; letter-spacing: .09em;
               text-transform: uppercase; margin: 14px 0 4px; }}
      .config-pill {{
        display: inline-block; background: #E8F5E1; color: {CHARCOAL};
        border: 1px solid #A3D98A; border-radius: 6px;
        padding: 3px 10px; font-size: .78rem; font-weight: 600; margin: 2px 4px 2px 0;
      }}
      div[data-testid="stMetric"] {{
        background: #fff; border: 1px solid #E2E8F0; border-radius: 10px; padding: 14px 16px;
        font-variant-numeric: tabular-nums;
      }}
      div[data-testid="stMetricValue"] {{ font-variant-numeric: tabular-nums; color: {CHARCOAL}; }}
      .stButton > button {{ background: {BRAND_GREEN}; color: #fff; border: 0; border-radius: 8px;
                            padding: .6rem 1.4rem; font-weight: 700; }}
      .stDownloadButton > button {{ background: {CHARCOAL}; color: #fff; border: 0; border-radius: 8px;
                                    font-weight: 700; }}
      .stDataFrame {{ font-variant-numeric: tabular-nums; }}
    </style>
    """,
    unsafe_allow_html=True,
)

# Human-readable label for each source key the engine can detect.
REQUIRED_SOURCES = ["ledger_a", "ledger_b", "airline_1", "airline_2", "airline_3"]
OPTIONAL_SOURCES = ["cancellation_report", "fonepay_refund"]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def banner() -> None:
    st.markdown(
        """<div class="recon-banner">
              <h1>✈️ Airlines Reconciliation</h1>
              <span class="recon-badge">Rules from config.yaml</span>
            </div>""",
        unsafe_allow_html=True,
    )


def _pill(text: str) -> str:
    return f'<span class="config-pill">{text}</span>'


def _display_frame(df: pd.DataFrame, limit: int = 200) -> pd.DataFrame:
    """Mixed-type object columns (e.g. 'Price Per Ticket' holding numbers AND
    the text 'Reschedule/Refund') break Streamlit's Arrow conversion, so
    object columns are shown as clean text."""
    out = df.head(limit).copy()
    for col in out.columns:
        if out[col].dtype == object:
            out[col] = out[col].map(
                lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)
            )
    return out


def _source_label(source: str, names: dict) -> str:
    labels = {
        "ledger_a": f"{names.get('ledger_a', 'Ledger A')} ledger",
        "ledger_b": f"{names.get('ledger_b', 'Ledger B')} ledger",
        "airline_1": f"{names.get('airline_1', 'Airline 1')} sales",
        "airline_2": f"{names.get('airline_2', 'Airline 2')} sales",
        "airline_3": f"{names.get('airline_3', 'Airline 3')} sales (+ Refunds sheet)",
        "cancellation_report": f"{names.get('cancellation_report', 'eSewa Refund')} report",
        "fonepay_refund": f"{names.get('fonepay_refund', 'Fonepay Refund')} report",
    }
    return labels.get(source, source)


def _zip_reports(results: list[dict]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in results:
            zf.writestr(r["filename"], r["bytes"])
    return buf.getvalue()


def _kpis(result: dict) -> dict:
    """Headline numbers for one airline, taken from its two summary sheets."""
    frames = list(result["summary"].values())
    a, b = frames[0].iloc[0], frames[1].iloc[0]
    return {
        "tickets": int(a["Total Airline Tickets"]),
        "matched_a": int(a["Recon Count"]),
        "matched_b": int(b["Recon Count"]),
        "unrecon": int(a["Unrecon Count"]),
        "unrecon_amt": float(a["Unrecon Amount"]),
        "diff_a": float(a["Difference"]),
        "diff_b": float(b["Difference"]),
    }


@st.cache_resource(show_spinner=False)
def _pg_store(url: str):
    return store_mod.PostgresStore(url)


def _get_store(cfg: dict):
    """Shared database when configured, else local files. A configured but
    unreachable database is an error (never a silent fall-back, which would
    split the history between two places)."""
    try:
        url = store_mod.database_url()
        return _pg_store(url) if url else engine.default_store(cfg)
    except store_mod.StoreError as exc:
        st.error(f"Storage problem: {exc}")
        st.caption("Check the `[database] url` secret and that the database is reachable, then reload.")
        return None


@st.cache_data(ttl=20, show_spinner=False)
def _stats(_store, key: str) -> dict:
    return _store.history_stats()


@st.cache_data(ttl=20, show_spinner=False)
def _waiting(_store, key: str):
    return _store.pending_load(), _store.fp_unresolved_load()


def _clear_caches() -> None:
    _stats.clear()
    _waiting.clear()


def history_tab(cfg: dict, store, operator: str) -> None:
    st.markdown('<div class="step">Fonepay RRN → Ticket history</div>', unsafe_allow_html=True)
    st.markdown(
        "A Fonepay **refund** report only has the RRN; the ticket number comes from the Fonepay "
        "**transaction** report of the day of the sale. This history remembers every RRN → ticket "
        "pair, so a refund posted on any later day can still be matched. It grows automatically "
        "every time you run **Reconcile** with a Fonepay transaction file; use this tab to load "
        "older days **once** (or to fill a gap)."
    )

    try:
        stt = _stats(store, store.describe())
    except store_mod.StoreError as exc:
        st.error(f"Storage problem: {exc}")
        return
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("RRN → ticket pairs", f"{stt['rows']:,}")
    m2.metric("Distinct RRNs", f"{stt['rrns']:,}")
    m3.metric("Earliest sale date", str(stt["min_date"] or "—"))
    m4.metric("Latest sale date", str(stt["max_date"] or "—"))
    if stt["last_added"]:
        st.caption(f"Last added to: {pd.Timestamp(stt['last_added']).strftime('%Y-%m-%d %H:%M')} · "
                   f"storage: {store.describe()}")

    st.markdown("**Add historical Fonepay transaction reports**")
    st.caption("Same files you drop into Reconcile as the Fonepay ledger — as many days or months as you "
               "have, in one go. Safe to repeat: pairs already on file are skipped. A backup CSV "
               "downloaded from this tab can be uploaded here to restore it.")
    files = st.file_uploader(
        "Fonepay transaction reports", type=["xlsx", "xlsm", "xls", "csv"],
        accept_multiple_files=True, key="airlines_hist_uploads", label_visibility="collapsed",
    )
    if files and st.button("Add to history", key="airlines_hist_add"):
        with st.spinner("Reading files and adding to the history…"):
            try:
                rep = engine.add_history_files([(f.name, f.getvalue()) for f in files],
                                               cfg=cfg, store=store, operator=operator)
                st.session_state["airlines_hist_report"] = rep
            except store_mod.StoreError as exc:
                st.error(f"Storage problem: {exc}")
        _clear_caches()
        st.rerun()

    rep = st.session_state.get("airlines_hist_report")
    if rep:
        df = pd.DataFrame(rep).rename(columns={"file": "File", "rows_read": "Rows read", "added": "New pairs added",
                                               "skipped": "Skipped (blank RRN/ticket)", "error": "Problem"})
        st.dataframe(df, hide_index=True, use_container_width=True)
        if any(r["error"] for r in rep):
            st.error("Some files could not be added — see the Problem column. The others were added.")
        else:
            st.success(f"Added {sum(r['added'] for r in rep):,} new pair(s).")

    st.markdown("**Backup**")
    if st.button("Prepare backup CSV", key="airlines_hist_prep"):
        st.session_state["airlines_hist_backup"] = engine.history_backup(cfg, store).to_csv(index=False).encode("utf-8")
    if st.session_state.get("airlines_hist_backup"):
        st.download_button("⬇ Download history backup (.csv)", st.session_state["airlines_hist_backup"],
                           file_name=f"fonepay_history_backup_{date.today():%Y%m%d}.csv", mime="text/csv",
                           key="airlines_hist_dl")

    pending, unresolved = _waiting(store, store.describe())
    with st.expander(f"Airline refunds still waiting for the ledger side ({len(pending):,})"):
        if pending.empty:
            st.caption("None.")
        else:
            st.dataframe(_display_frame(pending, 500), hide_index=True, use_container_width=True)
    with st.expander(f"Fonepay refunds waiting for a ticket — RRN not in history ({len(unresolved):,})"):
        if unresolved.empty:
            st.caption("None.")
        else:
            st.dataframe(_display_frame(unresolved.drop(columns=["_row_key"], errors="ignore"), 500),
                         hide_index=True, use_container_width=True)
    with st.expander("Recent runs"):
        runs = store.recent_runs(10)
        if not runs:
            st.caption("No runs recorded yet.")
        for r in runs:
            when = pd.Timestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M") if r.get("created_at") else "—"
            st.caption(f"**{r.get('operator') or '—'}** · {when} · "
                       f"{'saved' if r.get('persisted') else 'preview'} · {', '.join(r.get('files') or [])}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    banner()
    st.markdown(
        "Upload the day's ledger, airline and refund files together. Files are "
        "recognised by their column headers, so names don't matter. All rules "
        "come from `config.yaml`."
    )

    # ── Step 1 · Configuration (config.yaml) ─────────────────────────────────
    st.markdown('<div class="step">Step 1 · Configuration</div>', unsafe_allow_html=True)

    if "airlines_cfg_text" not in st.session_state:
        with open(engine.CONFIG_PATH, "r", encoding="utf-8") as fh:
            st.session_state["airlines_cfg_text"] = fh.read()

    cfg = None
    cfg_error = None
    try:
        cfg = yaml.safe_load(st.session_state["airlines_cfg_text"])
        if not isinstance(cfg, dict) or "names" not in cfg:
            raise ValueError("config.yaml must be a mapping with at least a 'names' section")
    except Exception as exc:  # noqa: BLE001
        cfg_error = f"{type(exc).__name__}: {exc}"

    if cfg and not cfg_error:
        names = cfg["names"]
        st.markdown(
            " ".join(
                _pill(f"{k.replace('_', ' ')}: {v}")
                for k, v in names.items()
                if k.startswith(("ledger", "airline"))
            ),
            unsafe_allow_html=True,
        )

    with st.expander("View / edit config.yaml", expanded=False):
        st.caption(
            "Edits apply to the next run straight away. Press **Save as default** "
            "to keep them for future sessions."
        )
        st.text_area(
            "config.yaml",
            key="airlines_cfg_text",
            height=420,
            label_visibility="collapsed",
        )
        c1, c2, c3 = st.columns([1, 1, 3])
        if c1.button("Save as default", disabled=bool(cfg_error)):
            with open(engine.CONFIG_PATH, "w", encoding="utf-8") as fh:
                fh.write(st.session_state["airlines_cfg_text"])
            st.success("Saved.")
        if c2.button("Reload from disk"):
            with open(engine.CONFIG_PATH, "r", encoding="utf-8") as fh:
                st.session_state["airlines_cfg_text"] = fh.read()
            st.rerun()
        if cfg_error:
            st.error(f"config.yaml is not valid: {cfg_error}")

    if cfg_error or not cfg:
        st.error("Fix config.yaml above to continue.")
        return

    # ── Storage ──────────────────────────────────────────────────────────────
    store = _get_store(cfg)
    if store is None:
        return
    c_op, c_st = st.columns([1, 3])
    operator = c_op.text_input("Your name", value=os.environ.get("USER", "ops.analyst"),
                               key="airlines_operator",
                               help="Recorded in the run log next to anything you save.").strip()
    with c_st:
        if store.shared:
            st.success(f"🟢 Shared history: {store.describe()}. Everyone using this app sees the same "
                       "RRN history and pending refunds.")
        else:
            st.warning(
                "🟠 No shared database configured — history is kept in local files on this server. "
                "On hosted Streamlit that is lost on restart and not shared between users. "
                "Add a `[database] url` secret to fix this (see README → Airlines storage).")

    tab_run, tab_hist = st.tabs(["Reconcile", "Fonepay history"])
    with tab_hist:
        history_tab(cfg, store, operator)
    with tab_run:
        reconcile_tab(cfg, store, operator)


def reconcile_tab(cfg: dict, store, operator: str) -> None:
    # ── Step 2 · Upload ──────────────────────────────────────────────────────
    st.markdown('<div class="step">Step 2 · Upload files</div>', unsafe_allow_html=True)

    names = cfg["names"]
    with st.expander("Which files do I need?", expanded=False):
        st.markdown(
            "**Required**\n"
            + "\n".join(f"- {_source_label(s, names)}" for s in REQUIRED_SOURCES)
            + "\n\n**Optional** (refund checks, Buddha)\n"
            + "\n".join(f"- {_source_label(s, names)}" for s in OPTIONAL_SOURCES)
        )

    uploads = st.file_uploader(
        "Upload all files for the day",
        type=["xlsx", "xlsm", "xls", "csv"],
        accept_multiple_files=True,
        key="airlines_uploads",
        help="Drop everything in at once. Each file is identified by its column headers.",
    )
    if not uploads:
        st.info("Upload the ledger, airline and (optionally) refund files to proceed.")
        _show_previous_results()
        return

    payload = [(u.name, u.getvalue()) for u in uploads]

    # Pre-flight detection: which file is which?
    detected = None
    try:
        with st.spinner("Recognising files..."):
            detected = engine.detect_uploaded_files(payload, cfg)
    except Exception as exc:  # noqa: BLE001
        st.error(str(exc))

    rows = []
    for s in REQUIRED_SOURCES + OPTIONAL_SOURCES:
        f = (detected or {}).get(s)
        rows.append({
            "Source": _source_label(s, names),
            "File": f or "—",
            "Status": "✅ found" if f else ("❌ missing" if s in REQUIRED_SOURCES else "➖ not provided"),
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    unused = sorted({n for n, _ in payload} - set((detected or {}).values()))
    if detected and unused:
        st.warning("Not recognised as any source (ignored): " + ", ".join(unused))

    if not detected:
        return

    # ── Step 3 · Run ─────────────────────────────────────────────────────────
    st.markdown('<div class="step">Step 3 · Execute</div>', unsafe_allow_html=True)
    st.caption(
        f"Carry-forward state (pending refunds, Fonepay RRN history) is kept in "
        f"`{engine.STATE_DIR}` unless config.yaml points elsewhere."
    )

    persist = st.checkbox(
        "Save this run to the shared history and pending refunds",
        value=True,
        key="airlines_persist",
        help="Untick for a preview/test run: it reads the shared data but saves nothing, so test "
             "files can't pollute the history or the pending-refund list.",
    )
    if not persist:
        st.info("Preview mode — results are shown but nothing will be saved.")

    if st.button("Run Reconciliation", type="primary"):
        try:
            with st.spinner("Matching tickets and building reports…"):
                results, det, log_text = engine.run_airline_reconciliation(
                    payload, cfg=cfg, store=store, persist=persist, operator=operator)
            st.session_state["airlines_results"] = {
                "results": results,
                "log": log_text,
                "files": [n for n, _ in payload],
                "persisted": persist,
            }
            _clear_caches()
        except Exception as exc:  # noqa: BLE001
            logging.exception("airline reconciliation failed")
            st.session_state.pop("airlines_results", None)
            st.error(f"The run failed: {type(exc).__name__}: {exc}")
            log_text = getattr(exc, "log_text", "")
            if log_text:
                with st.expander("Run log", expanded=True):
                    st.code(log_text, language="text")
            return

    _show_previous_results()


def _show_previous_results() -> None:
    """Results live in session_state so download-button reruns don't wipe them."""
    stored = st.session_state.get("airlines_results")
    if not stored:
        return
    results = stored["results"]
    if not stored.get("persisted", True):
        st.info("This was a preview run — nothing from it was saved.")
    for r in results:
        unres = r["unrecon"].get("Fonepay Refund Unresolved")
        if unres is not None and not unres.empty:
            st.warning(
                f"**{len(unres)} Fonepay refund(s)** have no ticket yet because their RRN isn't in the "
                "Fonepay history. They are kept and retried on every run — add the original sale day on "
                "the **Fonepay history** tab and they will match automatically.")

    # ── Step 4 · Summary ─────────────────────────────────────────────────────
    st.markdown('<div class="step">Step 4 · Summary</div>', unsafe_allow_html=True)

    for r in results:
        k = _kpis(r)
        st.markdown(f"**{r['airline_name']}**")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Airline tickets", f"{k['tickets']:,}")
        c2.metric("Matched via eSewa", f"{k['matched_a']:,}", f"Δ {k['diff_a']:,.2f}", delta_color="off")
        c3.metric("Matched via Fonepay", f"{k['matched_b']:,}", f"Δ {k['diff_b']:,.2f}", delta_color="off")
        c4.metric("Unreconciled", f"{k['unrecon']:,}")
        c5.metric("Unrecon amount", f"{k['unrecon_amt']:,.2f}")

    # ── Step 5 · Triage & download ───────────────────────────────────────────
    st.markdown('<div class="step">Step 5 · Triage & download</div>', unsafe_allow_html=True)

    st.download_button(
        "⬇ Download all reports (.zip)",
        data=_zip_reports(results),
        file_name="airline_recon_reports.zip",
        mime="application/zip",
        key="dl_all",
    )

    tabs = st.tabs([r["airline_name"] for r in results])
    for tab, r in zip(tabs, results):
        with tab:
            st.download_button(
                f"⬇ {r['filename']}",
                data=r["bytes"],
                file_name=r["filename"],
                mime=XLSX_MIME,
                key=f"dl_{r['airline_key']}",
            )
            sections = (
                [(f"Summary — {n.replace('Summary ', '')}", df) for n, df in r["summary"].items()]
                + list(r["recon"].items())
                + list(r["unrecon"].items())
            )
            sub = st.tabs([f"{name} ({len(df):,})" for name, df in sections])
            for stab, (name, df) in zip(sub, sections):
                with stab:
                    if df.empty:
                        st.info("No rows.")
                    else:
                        st.caption(f"{len(df):,} rows — showing the first 200. The full data is in the download.")
                        st.dataframe(_display_frame(df), use_container_width=True, hide_index=True)

    with st.expander("Run log", expanded=False):
        st.code(stored["log"], language="text")


if __name__ == "__main__":
    main()
