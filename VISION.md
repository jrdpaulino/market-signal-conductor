# Vision

## The problem

Retail investors are surrounded by signals that sound informative: insider filings, prediction-market odds, social
chatter, financial news, macro data. Almost nobody measures whether any of them predicts anything. Dashboards show
them, newsletters quote them, and conviction follows, with no record of how often the signal was right.

## What Market Signal Conductor is

A small, always-on web service that does three things, in this order:

1. **Gathers** a handful of free market signals every day, each through the same plug-in interface.
2. **Combines** them in a layered Markov cycle model: a market regime on top (recovery, expansion, late cycle,
   contraction) and a weak / neutral / strong state for each stock underneath, tilted by the regime.
3. **Keeps score.** Every day's forecast is stored and judged against what the market actually did over the next
   20 trading days. Each signal's influence rises or falls with its own measured track record.

The product is the track record. The dashboard is a window onto it.

## Principles

- **Evidence before trust.** Starting weights, state means and transition odds are assumptions and are labelled as
  such. A signal earns influence only from outcomes already known on the day it is weighed; nothing looks ahead.
- **No orders until the gate opens.** The repository contains no broker code. `msc/execution/gate.py` defines what
  the model's own forecasts must show first: at least 60 scored forecasts, a better Brier score than always
  predicting the base rate, and a hit rate whose 95% lower bound is above 50%. If the gate never opens, the system
  has still done its job.
- **Free first, paid later.** Every built-in source is free and keyless. Paid sources slot in as ordinary plug-ins
  that read their key from the environment.
- **One broken source never stops the run.** Signals fail independently, and the dashboard shows each one's last
  error in plain words.
- **Small and legible.** Python standard library only, one SQLite file, one machine. A single person can read the
  whole thing in an afternoon and trust what it does.
- **Runs without you.** It lives on a host and keeps working when your computer is off.

## Who it is for

One person making their own investment decisions who wants a disciplined, falsifiable second opinion and is
willing to wait months for it. It is not advice, not a recommendation engine and not a product for other people's
money.

## Non-goals

- Predicting individual stock returns with confidence. Per-stock states are a ranking aid, and stock-level
  reliability is not measured yet.
- High-frequency or intraday anything. The model steps once a day.
- Replacing judgement. It reports a probability and the evidence behind it.
- Hiding uncertainty. A flat, unconvincing reading is a valid output.

## How we will know it works

| Question | Measure | Where it shows |
| --- | --- | --- |
| Is the cycle forecast better than guessing? | Brier score versus the base rate over 60+ scored forecasts | Dashboard, "Track and gate" |
| Is the hit rate real? | Wilson 95% lower bound above 50% | Dashboard, "Track and gate" |
| Which signals deserve influence? | Correlation of each signal with the next 20-day market return, shrunk for small samples | Dashboard, "Signals" weight column |
| Is it dependable? | Daily run completes; each signal reports `ok` | Dashboard, "Signals" status column |

Realistically the first scored forecast arrives after 20 trading days and the gate cannot open for about four
months. Anything before that is plumbing, not evidence.

## Roadmap

**Now (built).** Nine signals across six sources: SEC Form 4 insider buying, Polymarket and Kalshi odds, RSS news
tone, S&P 500 trend, and three FRED macro series. Layered Markov model, reliability weights, forecast scoring, the
gate, a password-protected dashboard, daily scheduler with restart catch-up, Fly.io deployment.

**Next.**
- Let the data accumulate. Run `python -m msc learn` after about 10 days to fit the model to what it has seen.
- Replace the dead Reddit source (cloud hosts are blocked) or drop `social` for good.
- More free signals: weekly jobless claims, market breadth, positioning data (AAII, CFTC), 8-K stress language,
  short interest.
- Measure per-stock reliability, so the stock layer stops borrowing market-level weights.

**Later, only if the gate opens.**
- Paper trading against the forecasts, with the same scoring.
- Paid data plug-ins where a free signal has proven useful and a better version exists.
- Live trading code, behind the gate and a human confirmation step.

**Or, if it never opens.** Publish the null result and retire the model. That is a legitimate outcome and the
reason the system exists.

## Risks

| Risk | Mitigation |
| --- | --- |
| The signals carry no information (earlier insider-copytrading and multi-signal backtests showed no edge) | Scoring is the point; reliability weights fall to zero for useless signals; the gate stays shut |
| Overfitting the model to a short history | EM re-fit is anchored to the starting values by pseudo-counts; weights shrink small samples toward zero |
| Free sources change or block the host (Reddit already did) | Per-source error messages, independent failure, plug-in replacement |
| Prediction-market wording changes break the rules | Rules are configuration with exclusions; `pm-debug` shows what each venue returns |
| Single machine and single SQLite file | Cheap volume backups; the daily run is idempotent per day |
| Mistaking a probability for a promise | The dashboard states chances, labels assumptions, and shows the gate as closed until evidence says otherwise |

*This project is a research tool, not financial advice. I am not a financial advisor.*
