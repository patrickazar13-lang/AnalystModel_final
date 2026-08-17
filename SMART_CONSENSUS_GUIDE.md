# Smart Consensus Model — Practical Guide

This README explains how to run and understand the Smart Consensus model.

Smart Consensus is an **optional second stage** of the main Analyst Model.
Finish collecting and combining your company universe first, then run Smart
Consensus using the master CSV files already created by
`master_pipeline.py`.

**Smart Consensus makes 0 FactSet API calls.**

---

## Quick Start

Once your full company universe has been combined by the main pipeline,
run:

```powershell
py -3.11 smart_consensus.py
```

Then create the separate Excel workbook:

```powershell
py -3.11 export_smart_consensus.py
```

Open it:

```powershell
ii .\outputs\Smart_Consensus_Model.xlsx
```

That is the basic workflow.

---

## 1. When Should I Run Smart Consensus?

Do not worry about Smart Consensus while you are still pulling individual
companies from FactSet. First complete this process:

```
Pull individual companies
        ↓
Save their CSV files in outputs
        ↓
Combine the full universe with master_pipeline.py --from-outputs
        ↓
Create the master CSV files
```

Only after that should you run Smart Consensus.

**Step 1: Pull missing companies** (uses FactSet)

```powershell
py -3.11 master_pipeline.py --live --ticker MSFT-US --quarters 12
```

**Step 2: Combine your downloaded universe** (0 FactSet calls)

```powershell
py -3.11 master_pipeline.py --from-outputs --tickers AAPL-US,CME-US,MSFT-US,TSM-US,KLAC-US --factor-model FF5 --factor-backtest
```

Replace the example ticker list with your real full universe.

**Step 3: Run Smart Consensus** (0 FactSet calls)

```powershell
py -3.11 smart_consensus.py
```

**Step 4: Create the Smart Consensus Excel workbook** (0 FactSet calls)

```powershell
py -3.11 export_smart_consensus.py
```

**Step 5: Open the workbook**

```powershell
ii .\outputs\Smart_Consensus_Model.xlsx
```

---

## 2. What Does Smart Consensus Do?

The model asks one simple question:

> Can we forecast EPS better by giving historically better analysts more
> influence, instead of treating current analyst estimates equally?

The normal **Standard Consensus** takes the current analyst estimates and
calculates the median.

| Analyst | Current EPS Estimate |
|---|---|
| Analyst A | 5.00 |
| Analyst B | 5.50 |
| Analyst C | 6.00 |

The Standard Consensus is **5.50**.

**Smart Consensus** first looks at how those analysts performed before the
quarter being predicted.

| Analyst | Current Estimate | Historical Record |
|---|---|---|
| Analyst A | 5.00 | Strong |
| Analyst B | 5.50 | Average |
| Analyst C | 6.00 | Weak |

Smart Consensus gives Analyst A more influence and Analyst C less
influence. The result might be:

```
Standard Consensus = 5.50
Smart Consensus    = 5.25
```

When actual EPS is released, the model checks which forecast was closer.

---

## 3. The Model Cannot Look Into the Future

This is one of the most important rules.

Suppose the model is predicting **CME-US, 2025Q4**. The analyst weights may
use observations from earlier quarters such as `2023Q1, 2023Q2, 2023Q3, ...
2025Q2, 2025Q3`.

They **cannot** use `2025Q4 actual result`, `2026Q1`, `2026Q2` to determine
the 2025Q4 analyst weights.

This makes the comparison **out of sample** — the model tests what could
have been known at the time, rather than using future analyst performance
to improve an older prediction.

---

## 4. Where Does Smart Consensus Get Its Data?

Smart Consensus does not contact FactSet. It reads the master CSV files
created by your main pipeline:

```
outputs\master_raw_forecast_errors.csv
outputs\master_raw_estimates.csv
```

- `master_raw_forecast_errors.csv` provides the analysts' historical
  forecasting records.
- `master_raw_estimates.csv` provides the current company and quarter
  estimates, actual EPS, price information, analyst names, brokers, and
  related fields.

---

## 5. How Are Analyst Weights Calculated?

The current Smart Consensus model deliberately starts simple:

```
Historical forecast accuracy  +  Amount of historical evidence  =  Smart Consensus weight
```

**Historical accuracy.** The main model's normalized forecast error is:

```
(Estimated EPS - Actual EPS) / Price 10 trading days before earnings
```

Smart Consensus looks at the analyst's historical absolute forecast errors
— smaller historical error is better. An analyst with a lower historical
MAE receives a larger accuracy component.

**Credibility.** The model also considers how many historical observations
exist:

```
n observations / (n observations + 10)
```

| Prior Observations | Credibility |
|---|---|
| 4 | 28.6% |
| 10 | 50.0% |
| 20 | 66.7% |
| 40 | 80.0% |

This prevents an analyst with only a few lucky forecasts from automatically
dominating an analyst with a long record.

**Final weight.** Conceptually:

```
Historical accuracy  ×  Credibility  =  Raw analyst weight
```

The eligible analysts' weights are then normalized so their final weights
add to 100%. The Smart Consensus is the weighted average of their current
EPS estimates.

---

## 6. What Does `MIN_HISTORY_OBS` Mean?

Open `smart_consensus.py`. Near the top you will see:

```python
MIN_HISTORY_OBS = 4
```

This means an analyst needs at least 4 prior usable forecast observations
before the analyst is allowed to receive a Smart Consensus weight.

| Prior observations | Eligible? |
|---|---|
| 0 | No |
| 2 | No |
| 3 | No |
| 4 | Yes |
| 10 | Yes |

This is different from credibility: `MIN_HISTORY_OBS` decides **whether**
the analyst is allowed into Smart Consensus at all. Credibility determines
**how strongly** the analyst's historical record should be trusted once
they're already eligible.

---

## 7. Should `MIN_HISTORY_OBS` Stay at 4?

Not necessarily. The current value of 4 is useful while the dataset is
still small, because it gives the model enough eligible observations to
test whether the code works. Once the full company universe is populated,
test stricter thresholds:

```powershell
py -3.11 smart_consensus.py --min-history 4
py -3.11 smart_consensus.py --min-history 6
py -3.11 smart_consensus.py --min-history 8
py -3.11 smart_consensus.py --min-history 10
```

A threshold of 10 is particularly useful to investigate because it is
consistent with the stronger evidence standard used elsewhere in the main
analyst model.

**Do not choose a threshold only because it happens to produce the best
result on a small sample.** The better test is whether the result remains
useful when the threshold is chosen *before* evaluating the results, the
company universe is much larger, many more firm-quarters are available, and
Smart Consensus has reasonable coverage.

---

## 8. What Files Does Smart Consensus Create?

Running `py -3.11 smart_consensus.py` creates:

| File | Contents |
|---|---|
| `outputs\smart_consensus_predictions.csv` | Company- and quarter-level Standard vs. Smart comparison. |
| `outputs\smart_consensus_analyst_weights.csv` | Analyst-level weights used to construct Smart Consensus. |
| `outputs\smart_consensus_summary.csv` | Overall Standard vs. Smart performance statistics. |

---

## 9. Create the Excel Workbook

After `smart_consensus.py` finishes, run:

```powershell
py -3.11 export_smart_consensus.py
```

It creates `outputs\Smart_Consensus_Model.xlsx`. Open it with:

```powershell
ii .\outputs\Smart_Consensus_Model.xlsx
```

The Excel exporter also makes 0 FactSet calls.

---

## 10. How to Read the Excel Workbook

Read the workbook in this order:

1. Dashboard
2. Predictions
3. Consensus Detail
4. Analyst Weights
5. Diagnostics
6. Threshold Guide
7. Read Me

---

## 11. Dashboard

Start here. The Dashboard answers:

> Is Smart Consensus currently beating Standard Consensus, and how much of
> the dataset can Smart Consensus actually cover?

**All target firm quarters** — the total number of company/quarter
observations where Standard Consensus can be evaluated.

**Smart Consensus available** — how many of those targets had enough
historical analyst information to calculate Smart Consensus.

**Smart coverage**, e.g.:

```
All target firm quarters = 21
Smart available          = 14
Smart coverage           = 66.7%
```

This means Standard Consensus can be calculated for 21 targets, but only 14
have enough analyst history for Smart Consensus.

**Mean absolute forecast error** — lower is better.

```
Standard = 0.0360
Smart    = 0.0350   →  Smart is better on average

Standard = 0.0360
Smart    = 0.0370   →  Standard is better on average
```

**Median absolute forecast error** — also lower is better, and less
affected by a few unusually large misses.

**Win rate** answers: on what percentage of directly comparable firm
quarters was each method closer to actual EPS?

```
Standard win rate = 45%
Smart win rate    = 55%   →  Smart won more individual comparisons
```

---

## 12. Predictions Sheet

One row per company and quarter. Important columns:

```
firm, quarter, standard_consensus, smart_consensus, actual_eps,
standard_fe, smart_fe, standard_abs_fe, smart_abs_fe,
n_current_estimates, n_smart_weighted_analysts, smart_weight_coverage,
smart_available, winner, analysts_used, top_analyst, top_weight
```

This sheet lets you answer: what did Standard predict? What did Smart
predict? What actually happened? Which method won? How many analysts were
used? Who was the most influential analyst? Which analysts contributed?

If `smart_consensus` is blank, check `smart_available` — the model
probably did not have enough analyst history for that company and quarter.

---

## 13. Consensus Detail Sheet

The easiest place to understand exactly how a Smart Consensus number was
built. Filter `firm` and `quarter` to the prediction you want to inspect.
You'll see rows such as:

| Analyst | Broker | Estimate | Prior Obs | Historical MAE | Credibility | Final Weight |
|---|---|---|---|---|---|---|
| Analyst A | Broker A | 3.10 | 18 | 0.012 | 64.3% | 31.2% |
| Analyst B | Broker B | 3.18 | 14 | 0.019 | 58.3% | 25.8% |
| Analyst C | Broker C | 3.25 | 9 | 0.027 | 47.4% | 17.4% |

The final Smart Consensus is approximately:

```
(Analyst A estimate × Analyst A final weight)
+ (Analyst B estimate × Analyst B final weight)
+ (Analyst C estimate × Analyst C final weight)
+ ...
= Smart Consensus
```

This sheet is the audit trail for the model.

---

## 14. Analyst Weights Sheet

Detailed weighting information used by the model:

- **Analyst** — the analyst's name.
- **Broker** — the analyst's brokerage.
- **Estimate value** — the analyst's current EPS estimate for the target
  company and quarter.
- **Prior observations** — how many historical observations existed before
  the target quarter.
- **Historical MAE** — the analyst's historical mean absolute normalized
  forecast error (lower is better).
- **Credibility weight** — how much confidence the model places in the
  analyst's historical record based on sample size.
- **Final weight** — the analyst's actual percentage contribution to the
  Smart Consensus for that company and quarter.

---

## 15. Diagnostics Sheet

Use this sheet to answer: *do I actually have enough data for Smart
Consensus to be meaningful?*

Important diagnostics: all target firm quarters, Smart Consensus available,
Smart Consensus unavailable, Smart coverage, number of firms, number of
quarters, analysts receiving Smart weights, average number of current
analysts, average number of Smart-weighted analysts.

The sheet also identifies company/quarter targets where Smart Consensus was
unavailable — normally because no current analyst met the minimum
historical observation requirement. This is a **data coverage limitation**,
not a failure of Standard Consensus.

---

## 16. Threshold Guide Sheet

Reminds you how to test different values of `MIN_HISTORY_OBS`:

```powershell
py -3.11 smart_consensus.py --min-history 4
py -3.11 smart_consensus.py --min-history 10
```

Each run overwrites the current Smart Consensus CSV outputs. After changing
the threshold, regenerate the Excel workbook:

```powershell
py -3.11 export_smart_consensus.py
```

Then inspect how Smart coverage, Smart MAE, Standard MAE, Smart win rate,
and the number of weighted analysts changed.

---

## 17. Why Might Smart Consensus Be Unavailable?

Suppose a company has 12 current analysts. Smart Consensus does not
automatically use all 12 — each analyst must have enough prior history.

If `MIN_HISTORY_OBS = 4` and the current analysts have:

```
Analyst A = 12 prior observations   →  eligible
Analyst B = 7 prior observations    →  eligible
Analyst C = 2 prior observations    →  not eligible
Analyst D = 0 prior observations    →  not eligible
```

If no current analyst is eligible, Standard Consensus is still calculated
but Smart Consensus is left unavailable. As the company universe becomes
larger, analysts who cover multiple companies should accumulate more
usable historical observations.

---

## 18. What Does a Good Result Look Like?

Do not judge the model from one number. A useful result would eventually
have:

- **Large sample** — hundreds of evaluated firm-quarters are much more
  meaningful than 10 or 20.
- **Good coverage** — Smart Consensus should be available for a meaningful
  percentage of the target universe.
- **Lower error** — Smart Consensus should have lower out-of-sample mean
  and median absolute forecast error.
- **Consistent wins** — Smart Consensus should win more than just a
  handful of observations.
- **Stability** — the result should not disappear completely when
  reasonable settings such as `MIN_HISTORY_OBS` are changed.

---

## 19. What Does the Current Smart Model NOT Use Yet?

The current version is intentionally simple. It uses:

```
Historical analyst accuracy + Credibility based on amount of history
```

It does not yet use the entire analyst reliability model. Future versions
could test:

**Smart Consensus V2**
```
Accuracy + Consistency + Predictability + Freshness + Credibility
```

**Smart Consensus V3**
```
Full analyst reliability + Current estimate revisions
+ Revision recency + Revision magnitude
```

Keeping the first version simple gives you a clean baseline. If the simple
model cannot beat Standard Consensus, that is useful information before
adding more complexity.

---

## 20. Full Workflow From Scratch

**A. Check what companies already exist**

```powershell
Get-ChildItem outputs\live_*_raw_forecast_errors.csv | Select-Object -ExpandProperty Name
```

**B. Pull only missing companies**

```powershell
py -3.11 master_pipeline.py --live --ticker MSFT-US --quarters 12
```

Repeat as FactSet limits allow.

**C. Combine the completed universe**

```powershell
py -3.11 master_pipeline.py --from-outputs --tickers YOUR-FULL-TICKER-LIST --factor-model FF5 --factor-backtest
```

**D. Run Smart Consensus**

```powershell
py -3.11 smart_consensus.py
```

**E. Create Smart Consensus Excel**

```powershell
py -3.11 export_smart_consensus.py
```

**F. Open Smart Consensus Excel**

```powershell
ii .\outputs\Smart_Consensus_Model.xlsx
```

---

## 21. Commands Worth Remembering

```powershell
# Run Smart Consensus with the default threshold
py -3.11 smart_consensus.py

# Run with 10 prior observations required
py -3.11 smart_consensus.py --min-history 10

# Create the Excel workbook
py -3.11 export_smart_consensus.py

# Open the workbook
ii .\outputs\Smart_Consensus_Model.xlsx

# Check the Smart Consensus Python file
py -3.11 -m py_compile smart_consensus.py

# Check the Excel exporter
py -3.11 -m py_compile export_smart_consensus.py
```

No output from `py_compile` means **PASS**.

---

## 22. The One Thing to Remember

Smart Consensus runs after the main multi-company dataset exists:

```
Main Analyst Model
        ↓
Master company dataset
        ↓
Smart Consensus
        ↓
Smart Consensus Excel
```

Once the FactSet company data has already been collected, Smart Consensus
itself requires **0 additional FactSet calls**.
