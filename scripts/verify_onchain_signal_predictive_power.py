"""Empirical check: do MVRV / SOPR / ETF flows predict Polymarket BTC binaries?

STATUS (2026-05-22): BLOCKED ON PAID DATA.

Article 3 (BTC four-dim resonance) claims:
  - MVRV Z-Score signals BTC bottoms (within ±2 weeks).
  - SOPR < 1.0 = capitulation = bottom.
  - ETF netflow > $1B for 5+ days = institutional accumulation.
  - Macro (Fed + M2) sets the directional regime.

The proposed validation: for each resolved BTC binary contract on
Polymarket ("BTC > $X by date Y"), pair the entry-day signal vector
with the resolution outcome and run a logistic regression. AUC > 0.60
would justify wiring the signals into research_overlay.

Free-data inventory
-------------------
  - BTC price history: CoinGecko / Binance public API ✓
  - Fear & Greed: alternative.me ✓ (already used by research_signal)
  - ETF netflow: SoSoValue / Coinglass — free tier rate-limited
  - M2 / Fed funds: FRED ✓
  - MVRV Z-Score: Glassnode (PAID) — free CryptoQuant has 1-day lag
  - SOPR: Glassnode (PAID) — same

So we'd need a CryptoQuant or Glassnode subscription (~$30-50/mo) to
do this properly. Decision deferred until budget is available; in the
meantime Stage G ships in SHADOW MODE — the collector pulls the
free signals (ETF + F&G + macro) and surfaces them in the
research_overlay telemetry, but the model_prob_provider and tail_risk
classifier do NOT consume them.

Re-running this verification once paid data is acquired:
  1. Fill `_load_signal_value(...)` for MVRV and SOPR using whichever
     endpoint you've subscribed to.
  2. Run with `--n-markets 80 --min-volume 1000000` to target
     well-traded BTC binaries.
  3. The script writes `data/research/onchain_signal_verification.json`
     with per-market signal vectors and a logistic-regression AUC
     scored on the holdout set.
  4. AUC > 0.60 → implement Stage G as a non-shadow research_overlay
     contributor; AUC < 0.55 → archive the idea.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPORT_FILE = Path("data/research/onchain_signal_verification.json")


def main() -> int:
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(
        json.dumps(
            {
                "generated_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                ),
                "status": "blocked_on_paid_data",
                "blocker": (
                    "MVRV Z-Score and SOPR require Glassnode or "
                    "CryptoQuant paid tier ($30-50/mo). Without them "
                    "the four-dim resonance from Article 3 cannot be "
                    "validated, only Fear & Greed + ETF flow + macro."
                ),
                "decision": (
                    "Stage G ships in SHADOW MODE: collector pulls the "
                    "free signals (ETF, F&G, macro) and surfaces them "
                    "via research_signal telemetry but does NOT modify "
                    "trading. Validate empirically once paid data is "
                    "acquired."
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote: {REPORT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
