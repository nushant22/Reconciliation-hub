# eSewa Reconciliation Engine (Ops MVP)

Upload File A (internal ledger) and File B (partner/bank/aggregator statement), pick or
override a mapping, and download a five-sheet triage workbook. Built for the
reconciliation desk, not for a demo: the failure modes it defends against are the ones
that actually eat an analyst's morning.

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
| Five mutually-exclusive buckets | Every A row lands in exactly one of {exact, mismatch, orphan A}; every B row in one of {exact, mismatch, orphan B}. `BucketCounts.assert_conservation()` **fails the run** rather than shipping a workbook that lost rows. |
| Formatting noise is never a break | `NPR 1,500.00` ≡ `1500`; `" TXN-01 "` ≡ `txn-01`; `021203341484` ≡ `21203341484`. |
| Real breaks are never hidden | Unparseable amount vs. a number is a mismatch, not a match. Junk parses to `null`, never `0.0` — a zero is a financial claim, a null is a data defect. |
| Duplicate keys reconcile honestly | Repeated references match occurrence-by-occurrence (1st↔1st, 2nd↔2nd). Only surplus occurrences fall out as orphans — that is a real quantity break, not an artefact. |
| Blank keys never match | Two empty references are not evidence of the same transaction; both sides are forced to orphans and counted as a warning. |
| Truncation is visible | A row cap stamps the Summary sheet **and** the sheet itself. A quietly shortened audit file is worse than a slow one. |

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
│   │   ├── exporter.py             5-sheet XlsxWriter workbook
│   │   ├── audit.py                SQLite run log + idempotency probe
│   │   ├── profiles.py             partner preset loading
│   │   └── pipeline.py             the only entry point a front-end needs
│   ├── profiles/company_schemas.json
│   └── tests/                      68 tests: sanitizer, matcher, exporter, E2E
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

200,000 × 198,006 rows on a modest container: **matching 1.3s**, end-to-end 14.7s. The tail
is workbook serialisation (a 188k-row exact-match sheet is ~12MB), which is why
`RECON_SHEET_ROW_CAP` exists and why XlsxWriter switches to `constant_memory` streaming
above 50k rows per sheet. If a batch ever outgrows RAM entirely, the ingestion seam in
`loader.py` is where `pl.scan_csv` replaces `pl.read_csv` — nothing downstream changes.

## Extending

Add a partner to `backend/profiles/company_schemas.json`; the UI picks it up on reload. If a
partner needs a genuine pre-match transform (e.g. stripping an `FP-`/`FT-` prefix from a
reference before joining, as the legacy Fonepay mapping does), add it as an expression in
`sanitizer.py` and reference it from the profile — keep it out of the UI layer so the cron
path and the web path stay identical.
