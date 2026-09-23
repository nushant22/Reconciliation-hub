# eSewa Reconciliation Engine (Ops MVP)

Upload File A (internal ledger) and File B (partner/bank/aggregator statement), pick or
override a mapping, and download either a lightweight CSV archive or traditional Excel workbook. 
Built for the reconciliation desk, not for a demo: the failure modes it defends against are 
the ones that actually eat an analyst's morning.

```
pip install -r requirements.txt
streamlit run app.py

# demo data (injects real break classes + formatting noise that must NOT break)
python scripts/generate_sample_data.py --rows 50000 --out sample_data
pytest
```

## What the engine guarantees

| Guarantee | How it is enforced |
|---|---|
| Five mutually-exclusive buckets | Every A row lands in exactly one of {exact, mismatch, orphan A}; every B row in one of {exact, mismatch, orphan B}. `BucketCounts.assert_conservation()` **fails the run** rather than shipping output that lost rows. |
| Formatting noise is never a break | `NPR 1,500.00` ≡ `1500`; `" TXN-01 "` ≡ `txn-01`; `021203341484` ≡ `21203341484`. |
| Real breaks are never hidden | Unparseable amount vs. a number is a mismatch, not a match. Junk parses to `null`, never `0.0` — a zero is a financial claim, a null is a data defect. |
| Duplicate keys reconcile honestly | Repeated references match occurrence-by-occurrence (1st↔1st, 2nd↔2nd). Only surplus occurrences fall out as orphans — that is a real quantity break, not an artefact. |
| Blank keys never match | Two empty references are not evidence of the same transaction; both sides are forced to orphans and counted as a warning. |
| Truncation is visible | A row cap stamps the summary **and** individual data files. A quietly shortened audit file is worse than a slow one. |

## Layout

```
esewa-recon-engine/
├── app.py                          Streamlit Ops UI (thin — no domain logic)
├── backend/
│   ├── core/
│   │   ├── config.py               MatchConfig / KeyPair / ValuePair, schema hash
│   │   ├── loader.py               encoding + delimiter + banner-row resilience
│   │   ├── sanitizer.py            key, money and date normalisation (pure exprs)
│   │   ├── matcher.py              Polars join/anti-join bucketing engine
│   │   ├── csv_exporter.py         Multi-file CSV archive generator (default)
│   │   ├── exporter.py             Multi-sheet Excel workbook generator (legacy)
│   │   ├── audit.py                SQLite run log + idempotency probe
│   │   ├── profiles.py             partner preset loading
│   │   └── pipeline.py             the only entry point a front-end needs
│   ├── profiles/company_schemas.json
│   └── tests/                      Tests: sanitizer, matcher, exporters, E2E
└── scripts/generate_sample_data.py
```

## The Nepali-specific problems it handles

- **UTC vs NPT (+05:45).** A ledger row stamped `2026-05-13 18:30 UTC` is `2026-05-14` in
  Nepal. With a date in the composite key that off-by-one-day silently orphans a chunk of
  every evening's volume. Set the key's date handling to *shift UTC → NPT*.
- **Bikram Sambat columns.** BS dates cannot be parsed as AD. `bs_text` mode normalises
  separators and zero-pads instead (`2081/5/2` → `2081-05-02`) so the key still joins.
- **Currency tokens.** `NPR`, `Rs.`, `Rs`, `₨`, `रू`, thousands separators, accounting
  negatives `(500.00)` and trailing-minus `500.00-` all parse.
- **Leading-zero loss.** References that round-trip through a numeric Excel cell lose their
  padding. Stripping is applied to *purely numeric* keys only, so alphanumeric reference
  formats can never be collapsed together, and `000` degrades to `0`, not to an empty key.
- **Banner rows.** Partner statements carry 1–3 marketing rows above the real header; the
  header row is per-file and part of the profile.

## Matching semantics

Composite key (up to 3 parts) is normalised symmetrically on both sides, concatenated with
a control separator, then given a per-key occurrence index. The join is
`inner on (key, occurrence)`; orphans are the two anti-joins. Compared attributes are
evaluated after the key matches:

- **Numeric** — `Float64`, rounded to 2dp, matched when `|A − B| ≤ epsilon`
  (default `0.00`). `delta = round(A − B, 2)` is written side by side with the raw values
  and conditionally formatted red when non-zero.
- **Text** — normalised (trim, collapse whitespace, case-fold) then compared exactly.
- **Break reason** names which attribute broke, so a mismatch sheet is triageable without
  eyeballing every column.

`epsilon` exists for partners who round differently; keep it at `0.00` unless a specific
partner's contract justifies otherwise, and note that it is recorded in the schema hash.

## Auditability

Every run writes to `recon_runs` (SQLite by default, schema is plain enough to point at
Postgres): timestamp, operator, profile, **schema hash**, per-file SHA-256, all six counts,
duration, the full mapping as JSON, and any warnings. The same mapping over the same two
files is detected on the next run and flagged in the UI as a duplicate — six weeks later,
"which mapping produced the workbook that closed that NPR 40,000 break?" has an answer.

An audit-write failure is logged loudly but never destroys a reconciliation the operator
already waited for.

## Performance

200,000 × 198,006 rows on a modest container: **matching 1.3s**, CSV export ~8s, Excel export ~14.7s. 
CSV is the recommended default for most use cases. The Excel tail is workbook serialization (a 188k-row 
exact-match sheet is ~12MB), which is why `RECON_SHEET_ROW_CAP` exists and why XlsxWriter switches to 
`constant_memory` streaming above 50k rows per sheet. If a batch ever outgrows RAM entirely, the 
ingestion seam in `loader.py` is where `pl.scan_csv` replaces `pl.read_csv` — nothing downstream changes.

### Output Format

The system supports **two output formats** (user-selectable in the UI):

#### CSV Archive (Recommended - Default)
A **ZIP archive** containing 8 CSV files:
- **summary.csv** — Account-wise overview + run metadata
- **detail.csv** — Three-section reconciliation report (volume, matched, unmatched)
- **unrecon_summary.csv** — Success-only unreconciled transaction counts
- **unreconciled_details.csv** — Raw unreconciled rows (both sides)
- **value_mismatches.csv** — Amount breaks with delta columns
- **exact_matches.csv** — All perfectly matched transactions
- **orphans_a.csv** — Transactions only in File A
- **orphans_b.csv** — Transactions only in File B

**Benefits of CSV:**
- **Lightweight:** 40-60% smaller file size
- **Faster processing:** No Excel formatting/styling overhead (~8s vs ~14.7s)
- **Better compatibility:** Works with any spreadsheet app, database import tools, or scripts
- **Lower memory footprint:** Reduced server load on Streamlit Cloud
- **Individual access:** Open only the files you need

#### Excel Workbook (Legacy)
A traditional **multi-sheet Excel workbook** (.xlsx) with 5 sheets:
- Summary, Detail (per-account), Unrecon_Summary, Unreconciled_Details, Value_Mismatches

**When to use Excel:**
- Downstream processes expect .xlsx format
- Need formatted, color-coded worksheets
- Macros or formulas reference specific sheet structures
- Organizational policy requires Excel output

### Handling Large Files on Streamlit Community Cloud

**Memory Limits:** Streamlit Community Cloud provides ~1GB RAM. For datasets exceeding 500k total rows or 100MB+ files:

- The app automatically caps output files at **50,000 rows** to prevent memory exhaustion
- All rows are still processed for matching statistics and counts
- Only the detail files (exact matches, mismatches, orphans) are truncated in the output
- A truncation notice appears in the summary (CSV or Excel)
- **Tip:** Use CSV format for better memory efficiency with large datasets

**Best Practices for Large Datasets:**
1. **Choose CSV format** — 40-60% smaller output, faster processing
2. **Split by time period** — Reconcile monthly batches instead of yearly files
3. **Filter before export** — Remove test/cancelled transactions in your source system
4. **Expect 1-3 minutes** for CSV, 2-5 minutes for Excel with 500k-1M rows
5. **Consider self-hosting** for regular multi-million row reconciliations (see Deployment Options below)

**If the app crashes:**
- Reduce file size by splitting data into smaller chunks
- Filter out non-essential columns in your export
- For production workloads with >1M rows regularly, deploy to AWS/GCP with 4GB+ RAM

## Deployment Options

### Streamlit Community Cloud (Current)
- **Pros:** Free, zero setup, auto-deploys from GitHub
- **Limits:** 1GB RAM, 2 CPU cores
- **Best for:** Files up to 500k rows, occasional reconciliations

### Self-Hosted (Recommended for Production)
```bash
# AWS EC2 / GCP Compute Engine (recommended: 2 vCPU, 4GB RAM)
pip install -r requirements.txt
streamlit run app.py --server.port 8080

# Or Docker
docker build -t recon-engine .
docker run -p 8080:8080 recon-engine
```

### Environment Variables for Tuning
- `RECON_SHEET_ROW_CAP` — Max rows per output sheet (default: unlimited, set to 50k on Streamlit Cloud)
- `RECON_LOG_LEVEL` — Logging verbosity (DEBUG, INFO, WARNING, ERROR)
- `RECON_AUDIT_DB` — Path to SQLite audit database (default: `recon_audit.db`)

## Extending

Add a partner to `backend/profiles/company_schemas.json`; the UI picks it up on reload. If a
partner needs a genuine pre-match transform (e.g. stripping an `FP-`/`FT-` prefix from a
reference before joining, as the legacy Fonepay mapping does), add it as an expression in
`sanitizer.py` and reference it from the profile — keep it out of the UI layer so the cron
path and the web path stay identical.
