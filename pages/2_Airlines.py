"""Airlines Reconciliation — Streamlit page.

Reconciles airline sales files (Shree / Yeti / Buddha, or whatever
`backend/airlines/config.yaml` names them) against the eSewa and Fonepay
ledgers by ticket number, including the refund / cancellation / cashback
checks. Every rule — sources, header rows, match keys, amount columns,
report fields, prep switches, refund aging, carry-forward state — is read
from `backend/airlines/config.yaml`; you can view and edit that file on
this page.

Drop all of the day's files into the uploader in one go. File names do not
matter: each file is recognised by its column headers.
"""

from __future__ import annotations

import io
import logging
import os
import zipfile

import pandas as pd
import streamlit as st
import yaml

from backend.airlines import engine

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

    if st.button("Run Reconciliation", type="primary"):
        try:
            with st.spinner("Matching tickets and building reports…"):
                results, det, log_text = engine.run_airline_reconciliation(payload, cfg=cfg)
            st.session_state["airlines_results"] = {
                "results": results,
                "log": log_text,
                "files": [n for n, _ in payload],
            }
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
