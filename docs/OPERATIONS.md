# Operations

## Run locally

```
copy .env.example .env        (edit it; set SEC_USER_AGENT and MSC_DASH_PASSWORD)
python -m unittest discover -s tests -t .
python -m msc run-once        (one day's collection and model update)
python -m msc serve           (dashboard at http://localhost:8080, user "admin")
```

Python 3.10 or newer; nothing else to install.

## Deploy on Fly.io

```
fly launch --no-deploy --copy-config      (pick a unique app name and keep it in fly.toml)
fly volumes create msc_data --size 1 --region ord
fly secrets set MSC_DASH_PASSWORD=<long random password> SEC_USER_AGENT="Your Name you@example.com"
fly deploy --ha=false
```

- To stamp the running code version, deploy with `fly deploy --ha=false --build-arg MSC_COMMIT=$(git rev-parse --short HEAD)`
  (PowerShell: `fly deploy --ha=false --build-arg MSC_COMMIT=$(git rev-parse --short HEAD)` works the same). `/healthz`
  then shows `commit=`. Tests, `python -m unittest`, print `msc commit under test: <id>` first (`-dirty` means
  uncommitted changes); backfill reports carry a `commit` field.
- `--ha=false` keeps a single machine. The SQLite file lives on one volume, which attaches to one machine, so a
  second machine would hold a different, empty database.
- Fly requires a card on file for always-on machines. Without one, machines stop after 5 minutes.
- Expected cost is about $3.85 a month: $3.69 for a 512 MB machine plus $0.15 for the 1 GB volume. Shared IPv4 is
  free. Check Fly's current pricing page, which changes.

## Configuration

| Variable | Meaning | Default |
| --- | --- | --- |
| `MSC_DASH_PASSWORD` | Dashboard password (the server refuses to start without one) | none |
| `SEC_USER_AGENT` | Contact string SEC requires | none |
| `MSC_DB` | SQLite path | `/data/msc.sqlite` in the container |
| `MSC_SIGNALS` | Comma list of signals to run | all built-in |
| `MSC_WATCHLIST` | Tickers whose insider filings are tracked | 20 large caps |
| `MSC_PM_RULES` | JSON list of prediction-market rules (`match`, `exclude`, `bullish_if`, `search`) | recession, Fed cut, Fed hike |
| `MSC_PM_VENUES` | `polymarket`, `kalshi` | both |
| `MSC_KALSHI_SERIES` | Kalshi series tickers fetched directly | `KXRECSSNBER,KXFED,KXFEDDECISION` |
| `MSC_RSS_FEEDS` | News feed URLs | Fed, SEC, Yahoo, CNBC, MarketWatch |
| `MSC_EXTRA_MODULES` | Extra signal modules to import | none |
| `MSC_PRICE_SOURCE` | `fred` or `stooq` | `fred` |

## Routine care

- **Weekly:** open the dashboard and confirm every signal says `ok`.
- **After about 10 days of data:** `fly ssh console -C "python -m msc learn"`.
- **Backups:** `fly ssh sftp get /data/msc.sqlite`.
- **Rotate the password:** `fly secrets set MSC_DASH_PASSWORD=<new>`. The machine restarts and keeps its data.

## Uptime monitoring

`/healthz` needs no password and answers `ok last_run=<date> commit=<git commit>`: the day of the latest daily run and the code version. The
`.github/workflows/uptime.yml` workflow calls it every three hours and fails if the app is unreachable or the last run is
more than two days old; GitHub emails you when a scheduled run fails. To turn it on, add a repository secret named
`APP_URL` (Settings, Secrets and variables, Actions) set to your app's address, such as `https://your-app.fly.dev`.
GitHub pauses scheduled workflows after 60 days without repository activity, so re-enable it if it stops.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| Proxy error "could not find a good candidate" | Machine stopped. Check `fly status`; add a card at fly.io/trial, then `fly machine start <id>` |
| `Error: app not found` on deploy | `app` in `fly.toml` does not match your app; edit it or pass `-a <name>` |
| A signal shows `error` | Its row on the dashboard carries the reason; `fly logs --no-tail` for detail |
| Prediction markets match nothing | `fly ssh console -C "python -m msc pm-debug recession"` shows what each venue returns; adjust `MSC_PM_RULES` |
| `social` fails with 404 or 429 | Reddit blocks cloud hosts. Remove it: `fly secrets set MSC_SIGNALS=sec_filings,prediction_markets,journals_news,price_anchor,macro_curve,macro_credit,macro_vix,macro_labor,macro_conditions,anomaly_flow` |

## Logs

Long output goes to text files in `logs/` (override with `MSC_LOG_DIR`; git-ignored), named
`<kind>-<date>-<time>-<commit>.txt`. `python -m tests` runs every test and writes the full output to
`logs/tests-...txt`, printing only the verdict and the failing test names. `python -m msc backfill` writes the full JSON report
to `logs/backfill-...txt` and prints one summary line per run plus the path. Plain `python -m unittest` still works
and prints everything.

## Backfill: replaying history

`python -m msc backfill` replays the signals that can be rebuilt honestly from past data (the FRED macro series
and the S&P 500 trend) day by day, and reports how the model would have done.

```
python -m msc backfill --start 2021-01-01 --end 2026-11-20
```

Options: `--step 5` (every 5th trading day, faster), `--db path` (output file; default `<MSC_DB>.backfill.sqlite`;
an old backfill file is replaced, a database that is not a backfill is refused), `--refit-every 63` (walk-forward
re-fit of the model every 63 replayed days, after the first 120), and `--compare` (runs plain and re-fit and prints
them side by side).
It needs the same internet access as a normal run and is mostly waiting on the downloads (a minute or two for five years); the replay itself takes seconds.

What keeps it honest:

- **Separate database.** The live database, the live weights and the trading gate never see replayed data. The
  command refuses to write to the live file.
- **No hindsight.** Each replayed day's reliability weights use only outcomes known by that day. The model is not
  re-fit on the replayed data, and its settings were not tuned on it.
- **Only replayable signals.** Prediction markets, news, social and `anomaly_flow` are left out: their sources
  cannot be rewound, and faking them would put hindsight in. Insider filings could be replayed by filing date
  but are shelved for now (volume).

The report includes `brier_breakdown`: `bias` (mean forecast minus the base rate), `reliability` (calibration error,
lower is better), `resolution` (discrimination, higher is better) and `brier_if_level_fixed` (the score if only the
overall level were corrected). A model can lose to the base rate purely because its average is off, with no signal
problem at all; this shows which case you are in.

Other targets: `--target direction|drawdown|vol_down` and `--horizon N` replay the same signals against a different
question. Every target is a yes/no outcome where "yes" is the good side, so the weights, calibration, Brier
score and placebo all work unchanged. `direction` is up over the horizon; `drawdown` is no close falling more than 3%
below today's close (the threshold is fixed in advance on purpose); `vol_down` is realized volatility over the next
horizon lower than over the last. These change only that replay, never the live forecast. The report names the
target. The assumed P(up) per regime is tuned for direction, so for other targets read the Brier score against
the running base rate, which the calibration step fits to the real level. Example:
`python -m msc backfill --start 2021-01-01 --target drawdown`.

Longer history and slices: `macro_credit_long` uses Moody's Baa minus the 10-year Treasury (FRED `BAA10Y`, back to 1986)
where the ICE high-yield spread only goes back about three years. It overlaps `macro_credit`; to replay one
without the other use `--signals price_anchor,macro_curve,macro_credit_long,macro_vix,macro_labor`. `--split N` also scores N
consecutive slices of the history on their own (hit rate, Brier against the running base rate, each signal's link
and placebo p inside the slice) and reports whether each signal keeps its sign in every slice. The default price
source (FRED) has only about ten years of S&P history. Stooq may refuse downloads without a key, in which case the replay stops with a clear message. For longer replays download daily prices yourself (a CSV with Date and Close columns, Open optional; Stooq, Yahoo Finance and brokers all export one) and set `MSC_PRICE_SOURCE=file` and `MSC_PRICE_FILE=C:\\path\\spx.csv`.
Example: `python -m msc backfill --start 1995-01-01 --split 3`.

Earnings drift is opt-in for the backfill because it needs `SEC_USER_AGENT` and one SEC request per company:
`python -m msc backfill --start 2013-01-01 --signals price_anchor,macro_curve,macro_credit,macro_vix,macro_labor,earnings_drift`.
XBRL data starts around 2009-2011 and the signal needs about two years of quarters first, so start no earlier than 2013.
A replay over today's watchlist has survivorship bias (today's winners). Only its market-wide breadth reading is
scored and placebo-tested; the per-ticker readings move the stock tilts but are not yet scored against outcomes.

Two yardsticks: `brier_base_rate` uses the whole period's up-rate (hindsight, nothing can know it in advance);
`brier_running_base_rate` uses only outcomes already known at each forecast date, which is what a naive forecaster
could really have said. Beating the running one is the fairer test; the gate still uses the hindsight one.
`placebo` rotates each signal's score series in time against the returns (200 times, `--placebo K`, 0 to skip) and
reports `p`: the share of rotations that correlated at least as strongly as the real series. `p_adjusted` multiplies
by the number of signals tested, since the best of many looks good by luck. A small p with a negative `corr` means
the signal points the wrong way. `macro_credit` now needs 120 readings instead of 250, because FRED serves only
about three years of that series.
`signal_errors` lists why a signal failed (error text, days, first and last day).

How to read the output: `hit_rate` and the Brier score against the base rate are the numbers that matter;
`gate_would_say` is informational only. Consecutive 20-day outcomes overlap, so the real sample is far smaller
than `scored_forecasts` suggests, and a few years is one market history. A pass here would be a reason to keep
collecting live evidence, not a reason to trade.

## Index strip

The row of index cards under the page title (S&P 500, Nasdaq, Dow, Russell 2000, VIX) is delayed or end-of-day
data, never live. Each card says its source and time.

- **Stooq quote CSV** gives a delayed intraday price. Its symbols and layout are from memory and unverified, so every
  Stooq price is checked against FRED's last close and ignored if implausibly far away.
- **FRED daily CSV** is the fallback: the last close, one trading day behind.
- If every card says "end of day (FRED)", Stooq is not answering from your host; the strip still works.
- Results are cached for 60 seconds on the server, so open dashboards do not multiply requests. A failure only
  removes cards, never breaks the page. The strip does not feed the model.

## Forecast calibration

The model's raw P(up) is pulled toward the track record once 60 forecasts have been scored (`msc/calibration.py`):
the average level moves toward the observed up-rate and the day-to-day swings are scaled by how well they have
predicted. Both moves are weighted by the effective sample size (scored outcomes divided by the 20-day horizon,
because consecutive outcomes overlap), so a young record barely changes the forecast. Calibration can quiet the
swings but never amplify or invert them.

- The dashboard says whether the forecast is raw or calibrated, and with how much weight.
- The raw number is always stored (`praw:<day>` in the database). The gate grades the calibrated forecast.
- Switch it off with `fly secrets set MSC_CALIBRATE=0`.
- The backfill report shows both: `brier_breakdown` (calibrated) and `brier_breakdown_raw`. If calibrated is not
  better than raw over a long replay, turn it off. On simulated data it is roughly neutral when signals are pure
  noise and helps only when the raw level is far from the true up-rate.
