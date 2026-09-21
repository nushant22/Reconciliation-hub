"""Generate a realistic internal-vs-partner pair for demos and load tests.

    python scripts/generate_sample_data.py --rows 50000 --out sample_data

Deliberately injects the four break classes Ops actually sees: amount drift,
missing settlements (orphan A), unsolicited partner rows (orphan B), plus
formatting noise that must NOT produce a break (currency tokens, leading zeros,
whitespace, UTC/NPT skew).
"""

from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl


def build(rows: int, seed: int = 7) -> tuple[pl.DataFrame, pl.DataFrame]:
    rng = random.Random(seed)
    base = datetime(2026, 5, 13, 0, 0, 0)

    a_rows, b_rows = [], []
    for i in range(rows):
        ref = f"{i:012d}"  # zero-padded: File B will lose the padding
        amount = round(rng.uniform(50, 25_000), 2)
        stamp = base + timedelta(seconds=rng.randint(0, 86_399))
        status = rng.choices(["success", "success", "success", "failed"], k=1)[0]

        a_rows.append(
            {
                "txn_id": ref,
                "txn_date": stamp.strftime("%Y-%m-%d %H:%M:%S"),  # UTC
                "amount": f"NPR {amount:,.2f}" if i % 7 == 0 else f"{amount:.2f}",
                "status": status,
                "channel": rng.choice(["wallet", "bank", "card"]),
            }
        )

        roll = rng.random()
        if roll < 0.02:  # 2% never settled -> orphan in File A
            continue
        partner_amount = amount + (round(rng.uniform(0.5, 40), 2) if roll < 0.06 else 0.0)
        b_rows.append(
            {
                "transaction id": str(int(ref)),  # leading zeros lost via Excel
                "posting date": (stamp + timedelta(minutes=345)).strftime("%Y-%m-%d"),  # NPT
                "settlement amount": f" {partner_amount:,.2f} ",
                "txn status": status,
                "partner_batch": f"B{stamp.strftime('%Y%m%d')}",
            }
        )

    for j in range(max(rows // 100, 1)):  # partner-only rows -> orphan in File B
        b_rows.append(
            {
                "transaction id": f"9{j:011d}",
                "posting date": "2026-05-14",
                "settlement amount": f"{rng.uniform(50, 5000):.2f}",
                "txn status": "success",
                "partner_batch": "B20260514",
            }
        )

    return pl.DataFrame(a_rows), pl.DataFrame(b_rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=5_000)
    parser.add_argument("--out", type=str, default="sample_data")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    df_a, df_b = build(args.rows)
    df_a.write_csv(out / "internal_ledger.csv")

    # Partner file ships with two banner rows above the header, like the real thing.
    partner = out / "partner_settlement.csv"
    body = df_b.write_csv()
    partner.write_text("NIC ASIA BANK LTD\nDaily Settlement Statement\n" + body, encoding="utf-8")

    print(f"wrote {out/'internal_ledger.csv'} ({df_a.height:,} rows)")
    print(f"wrote {partner} ({df_b.height:,} rows, header on line 3)")


if __name__ == "__main__":
    main()
