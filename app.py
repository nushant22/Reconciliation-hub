"""eSewa Reconciliation Operations Hub — Streamlit front-end.

Account definitions (key columns, filters, transforms, status normalisation)
are driven entirely by `backend/profiles/accounts.json`.  The operator only
needs to:

  1. Pick the account from the sidebar.
  2. Upload the two files (labels come from the config).
  3. Hit Run.

No manual attribute-mapping dropdowns.

Run:  streamlit run app.py
"""

from __future__ import annotations

import logging
import os

import streamlit as st

from backend.core import audit
from backend.core.account_config import get_account, account_names
from backend.core.account_pipeline import run_account_reconciliation
from backend.core.errors import ReconError
from backend.core.loader import read_table
from backend.core.config import LoadSpec

logging.basicConfig(
    level=os.environ.get("RECON_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)

BRAND_GREEN = "#60BB46"
CHARCOAL    = "#1F2937"

st.set_page_config(
    page_title="eSewa Reconciliation Operations Hub",
    page_icon="🟢",
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


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def banner(account_name: str) -> None:
    st.markdown(
        f"""<div class="recon-banner">
              <h1>eSewa Reconciliation Operations Hub</h1>
              <span class="recon-badge">Account · {account_name}</span>
            </div>""",
        unsafe_allow_html=True,
    )


def _pill(text: str) -> str:
    return f'<span class="config-pill">{text}</span>'


def _show_side_config(cfg) -> None:
    """Render a compact read-only summary of one side's config."""
    parts = [
        _pill(f"key: {cfg.key_col}"),
        _pill(f"header row: {cfg.header_row}"),
    ]
    if cfg.amount_col:
        parts.append(_pill(f"amount: {cfg.amount_col}"))
    if cfg.status_col:
        parts.append(_pill(f"status: {cfg.status_col}"))
    if cfg.filters:
        parts.append(_pill(f"{len(cfg.filters)} filter(s)"))
    if cfg.transforms:
        parts.append(_pill(f"{len(cfg.transforms)} transform(s)"))
    st.markdown(" ".join(parts), unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    names = account_names()

    # ── Sidebar ───────────────────────────────────────────────────────────────
    with st.sidebar:
        st.subheader("Run context")
        account_name = st.selectbox("Account", names)
        operator = st.text_input(
            "Operator", value=os.environ.get("USER", "ops.analyst")
        )
        st.caption(
            "Account settings (key column, filters, transforms) are loaded "
            "automatically from `accounts.json`."
        )
        st.divider()
        st.subheader("Recent runs")
        for run in audit.recent_runs(limit=5):
            st.caption(
                f"**{run['profile_name']}** · {run['created_at'][:16]}  \n"
                f"{run['exact_matches']:,} matched · "
                f"{run['orphans_a']:,}/{run['orphans_b']:,} orphans"
            )

    # Load the account config (cached by lru_cache inside account_config)
    try:
        account = get_account(account_name)
    except ReconError as exc:
        st.error(str(exc))
        return

    left_cfg  = account.left
    right_cfg = account.right

    banner(account_name)

    # ── Step 1 · Ingestion ────────────────────────────────────────────────────
    st.markdown('<div class="step">Step 1 · Ingestion</div>', unsafe_allow_html=True)

    col_l, col_r = st.columns(2)

    with col_l:
        st.markdown(f"**File A — {left_cfg.label}** (`{left_cfg.file}`)")
        _show_side_config(left_cfg)
        up_left = st.file_uploader(
            f"Upload {left_cfg.label} export",
            type=["csv", "tsv", "xlsx", "xls"],
            key=f"up_left_{account_name}",
            help="Maximum file size: 1GB per file"
        )

    with col_r:
        st.markdown(f"**File B — {right_cfg.label}** (`{right_cfg.file}`)")
        _show_side_config(right_cfg)
        up_right = st.file_uploader(
            f"Upload {right_cfg.label} export",
            type=["csv", "tsv", "xlsx", "xls"],
            key=f"up_right_{account_name}",
            help="Maximum file size: 1GB per file"
        )

    if not (up_left and up_right):
        st.info("Upload both files to proceed — no column mapping needed.")
        return

    # Quick parse preview (validate the files load cleanly before the Run button)
    raw_left  = up_left.getvalue()
    raw_right = up_right.getvalue()

    # File size warnings removed - no limit on file size
    
    try:
        with st.spinner("Loading and validating files..."):
            df_left_preview  = read_table(raw_left,  up_left.name,
                                          LoadSpec(header_row=left_cfg.header_row))
            df_right_preview = read_table(raw_right, up_right.name,
                                          LoadSpec(header_row=right_cfg.header_row))
    except ReconError as exc:
        st.error(str(exc))
        return

    st.success(
        f"Parsed **{df_left_preview.height:,}** rows × {df_left_preview.width} cols "
        f"from {left_cfg.label} and "
        f"**{df_right_preview.height:,}** rows × {df_right_preview.width} cols "
        f"from {right_cfg.label}."
    )

    # Memory warning for extremely large datasets
    total_rows = df_left_preview.height + df_right_preview.height
    if total_rows > 1_000_000:
        st.warning(
            f"⚠️ Processing {total_rows:,} total rows may exceed available memory on the free hosting tier. "
            f"If the app crashes, consider splitting your data into smaller batches (e.g., by month)."
        )

    # ── Step 2 · Config summary (read-only, replaces the mapping dropdowns) ───
    st.markdown('<div class="step">Step 2 · Account configuration</div>',
                unsafe_allow_html=True)

    with st.expander("View full account config", expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            st.markdown(f"**{left_cfg.label} side**")
            st.json({
                "key_col":    left_cfg.key_col,
                "amount_col": left_cfg.amount_col,
                "status_col": left_cfg.status_col,
                "header_row": left_cfg.header_row,
                "filters":    left_cfg.filters,
                "transforms": left_cfg.transforms,
                "status_rules": left_cfg.status_rules,
            }, expanded=False)
        with c2:
            st.markdown(f"**{right_cfg.label} side**")
            st.json({
                "key_col":    right_cfg.key_col,
                "amount_col": right_cfg.amount_col,
                "status_col": right_cfg.status_col,
                "header_row": right_cfg.header_row,
                "filters":    right_cfg.filters,
                "transforms": right_cfg.transforms,
                "status_rules": right_cfg.status_rules,
            }, expanded=False)

    # ── Step 3 · Execute ──────────────────────────────────────────────────────
    st.markdown('<div class="step">Step 3 · Execute</div>', unsafe_allow_html=True)

    if not st.button("Run Reconciliation", type="primary"):
        return

    # No row cap - process all rows
    row_cap = None
    
    try:
        with st.spinner(
            f"Applying filters, transforms and matching {account_name}… "
            f"This may take 2-5 minutes for large files."
        ):
            outcome = run_account_reconciliation(
                account_name=account_name,
                file_left=raw_left,
                name_left=up_left.name,
                file_right=raw_right,
                name_right=up_right.name,
                operator=operator,
                row_cap=row_cap,
            )
    except ReconError as exc:
        st.error(str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        logging.exception("unhandled reconciliation failure")
        st.error(
            f"The run failed unexpectedly: {type(exc).__name__}. "
            "Details are in the server log."
        )
        return

    result = outcome.result

    if outcome.duplicate_of:
        st.warning(
            f"Identical inputs were already reconciled as "
            f"`{outcome.duplicate_of['run_id']}` "
            f"on {outcome.duplicate_of['created_at'][:16]}."
        )
    for warning in result.counts.warnings:
        st.warning(warning)

    # ── Step 4 · Summary KPIs ─────────────────────────────────────────────────
    st.markdown('<div class="step">Step 4 · Summary</div>', unsafe_allow_html=True)

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric(
        "Rows read A / B",
        f"{result.counts.rows_read_a:,} / {result.counts.rows_read_b:,}",
    )
    k2.metric(
        "Exact matches",
        f"{result.counts.exact_matches:,}",
        f"{result.match_rate:.2f}% of A",
    )
    k3.metric("Value mismatches", f"{result.counts.value_mismatches:,}")
    k4.metric("Orphans — A", f"{result.counts.orphans_a:,}")
    k5.metric("Orphans — B", f"{result.counts.orphans_b:,}")

    st.caption(
        f"Run `{outcome.run_id}` · "
        f"completed in {result.duration_s:.2f}s"
    )

    # ── Step 5 · Download & preview ───────────────────────────────────────────
    st.markdown('<div class="step">Step 5 · Triage & download</div>',
                unsafe_allow_html=True)

    st.download_button(
        "⬇ Download CSV archive (.zip)",
        data=outcome.workbook,
        file_name=outcome.filename,
        mime="application/zip",
    )

    tabs = st.tabs([
        "Value mismatches",
        f"Orphans — {left_cfg.label}",
        f"Orphans — {right_cfg.label}",
        "Exact matches",
    ])
    frames = [
        result.value_mismatches,
        result.orphans_a,
        result.orphans_b,
        result.exact_matches,
    ]
    for tab, frame in zip(tabs, frames):
        with tab:
            st.caption(f"{frame.height:,} rows — showing the first 200.")
            st.dataframe(frame.head(200), use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
