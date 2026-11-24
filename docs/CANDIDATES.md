# Signal candidates

Ideas that are not built yet, or built but not proven. Nothing here is a recommendation. A candidate earns a place
only by beating its time-rotated placebo (`python -m msc backfill`, the `placebo` section) over a long history,
and then by showing up in live, out-of-sample scoring. As of 2026-11-23 none of the five backfilled signals
(`macro_curve`, `macro_credit`, `macro_vix`, `macro_labor`, `price_anchor`) beats its placebo for 20-day S&P direction.

## How a candidate is tested

1. Find a daily history that was knowable on each date (watch release lags and revised series).
2. Add it as a backfillable signal (see `msc/backfill.py`, `BACKFILL_SIGNALS`).
3. Run the backfill and read `placebo.p_adjusted`, the running base rate and the Brier breakdown.
4. Only then let it run live.

## Queue

| Candidate | Idea | Data | Status | Notes |
|---|---|---|---|---|
| Index moves | Trend of the S&P 500, Nasdaq and Dow as separate signals | FRED `SP500`, `NASDAQCOM`, `DJIA` (daily) | Not built | Highly correlated with each other and with `price_anchor`, so likely redundant. Test, don't assume. |
| Index spreads | Nasdaq vs Dow, Russell 2000 vs S&P 500 as a risk-on / risk-off gauge | FRED `NASDAQCOM`, `DJIA`, `SP500`, `RU2000PR` | Not built | Less redundant than raw moves. The `RU2000PR` id is unverified. |
| Other targets | Same signals against 5-day moves, volatility or drawdown instead of 20-day direction | Existing | Built: `--target` and `--horizon` on the backfill | Hypothesis: macro signals say more about risk than direction. Not yet run. |
| Insider filings (EDGAR) | Clusters of insider buying or selling | SEC EDGAR Form 4 | Shelved | Needs a historical backfill built from filing dates. |
| Unusual options or trade flow | Flag abnormal activity ahead of news | `anomaly_flow` covers prediction-market and price jumps only | Partly built | The options-flow idea has no free historical data and a high false-positive risk. |
| Academic ideas | Check recent papers (for example Guo, RFS 2025) for testable signals | Paper dependent | To review | Read the paper, list the exact inputs, then test as above. |
| Text signals (GPU) | Score text with a language model run offline: tone of filings, headlines and posts, or change in a company's filing language | SEC filings first (free, timestamped); news and transcripts need paid history | Later | Needs years of text with real publication times. Beware look-ahead leakage from models trained after the events. Effects tend to be stock-level and short-lived, so the per-stock tilts may fit better than the S&P forecast. Only start if a cheaper candidate shows something real. |
| Contrarian commentary (for example inverse Cramer) | Fade a pundit's calls | Scraped, unstable | Dropped | Not pursued. |

## Rules for adding to this list

Record the data source, the release lag and whether the series gets revised. Do not claim a result until the
placebo test and the running base rate have been run.
