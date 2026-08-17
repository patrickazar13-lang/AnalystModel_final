# Analyst Model — Practical Run Guide

Use this guide to actually run the project, day to day. Keep the separate
methodology document for the academic methodology, the equations, and the
detailed research explanation — this one is just "how do I use it."

---

## 0. Before you start: what you need installed

You only need to do this section once, the first time you set the project up
on a machine. If `py -3.11 --version` already works for you, skip to
**Quick Start** below.

### Python 3.11

1. Go to [python.org/downloads](https://www.python.org/downloads/) and
   download the Windows installer for **Python 3.11** (not 3.12+ — this
   project is built and tested against 3.11).
2. Run the installer. On the first screen, **check the box "Add python.exe to
   PATH"** before clicking Install — this step is easy to miss and causes
   most setup problems if skipped.
3. Open a fresh PowerShell window (close and reopen if you already had one
   open) and confirm it worked:
   ```powershell
   py -3.11 --version
   ```
   You should see `Python 3.11.x`. If you instead see a message about the
   Microsoft Store, Python didn't install correctly, or PATH wasn't set —
   see the **Common Problems** section near the bottom.

### Git (only needed if the project lives in a repository you need to clone)

1. Go to [git-scm.com/downloads](https://git-scm.com/downloads) and download
   the Windows installer.
2. Run it, keeping the default options, unless you know you need something
   different.
3. Confirm it worked:
   ```powershell
   git --version
   ```
If the project folder was just handed to you as a folder of files (not a
repository you need to clone/pull), you can skip Git entirely.

### Project dependencies

Once Python is installed, from inside the project folder (see step 1 of
Quick Start below to get there), install the Python packages this project
needs:

```powershell
py -3.11 -m pip install -r requirements.txt
```

You only need to re-run this if `requirements.txt` changes, or you're
setting the project up on a new machine.

### FactSet credentials

Live pulls (`--live`) need a FactSet Formula API key. That goes in a `.env`
file in the project folder — ask whoever set the project up originally for
this file, or for the key itself if you're generating a fresh one. You don't
need this for `--mock` or `--from-outputs` runs.

---

## Quick Start

**1. Open the project.**

Open PowerShell (or the PyCharm terminal) and go to the project folder:

```powershell
cd "C:\Users\patri\Documents\Analyst Model code\analyst-model"
```

Keep the quotation marks — the folder path contains spaces.

Check Python:

```powershell
py -3.11 --version
```

Expected result: `Python 3.11.x`.

**2. Test the project.**

```powershell
py -3.11 master_pipeline.py --mock
```

This uses synthetic data and makes **0 FactSet calls**.

**3. Pull a new company.**

```powershell
py -3.11 master_pipeline.py --live --ticker CME-US --quarters 12
```

This downloads new FactSet data and saves the results in `outputs/`.

**4. Create the Excel workbook.**

```powershell
py -3.11 export_to_excel.py --ticker CME-US
```

Open it:

```powershell
ii .\outputs\live_CME_US_model.xlsx
```

---

## Copy-and-Paste Commands

Enter the project folder:

```powershell
cd "C:\Users\patri\Documents\Analyst Model code\analyst-model"
```

Check Python 3.11:

```powershell
py -3.11 --version
```

Safe test with zero API calls:

```powershell
py -3.11 master_pipeline.py --mock
```

Pull one new company:

```powershell
py -3.11 master_pipeline.py --live --ticker CME-US --quarters 12
```

See every company already downloaded:

```powershell
Get-ChildItem outputs\live_*_raw_forecast_errors.csv | Select-Object -ExpandProperty Name
```

Build one company's Excel workbook:

```powershell
py -3.11 export_to_excel.py --ticker CME-US
```

Open the Excel workbook:

```powershell
ii .\outputs\live_CME_US_model.xlsx
```

Combine existing companies and run pure FF5 with zero API calls:

```powershell
py -3.11 master_pipeline.py --from-outputs --tickers AAPL-US,APO-US,CME-US,EME-US,GFS-US,GME-US,GS-US,KLAC-US,NBIS-US,NRG-US,TSM-US --factor-model FF5 --factor-backtest
```

Check the important Python files for syntax errors:

```powershell
py -3.11 -m py_compile master_pipeline.py
py -3.11 -m py_compile export_to_excel.py
py -3.11 -m py_compile src\factset_data.py
py -3.11 -m py_compile src\factors.py
```

No output from `py_compile` means **PASS**.

---

## Which Command Should I Use?

**Test the code**

```powershell
py -3.11 master_pipeline.py --mock
```
FactSet usage: 0 calls

**Pull a new company**

```powershell
py -3.11 master_pipeline.py --live --ticker CME-US --quarters 12
```
FactSet usage: uses calls

**Reuse companies already downloaded**

```powershell
py -3.11 master_pipeline.py --from-outputs --tickers AAPL-US,CME-US,TSM-US,KLAC-US --factor-model FF5 --factor-backtest
```
FactSet usage: 0 calls

**Create a company Excel workbook**

```powershell
py -3.11 export_to_excel.py --ticker CME-US
```
FactSet usage: 0 calls

**Most important rule:** `--live` downloads new data. `--from-outputs`
reuses data already saved in `outputs/`. `--mock` uses synthetic test data.
If you are trying to save FactSet usage, do not use `--live`.

---

## 1. Open the Project

Open PowerShell or the PyCharm Terminal. Go to the project folder:

```powershell
cd "C:\Users\patri\Documents\Analyst Model code\analyst-model"
```

Keep the quotation marks because the folder path contains spaces.

Check Python:

```powershell
py -3.11 --version
```

Expected result: `Python 3.11.x`.

## 2. Quick Safety Test

Before doing a live FactSet pull, run:

```powershell
py -3.11 master_pipeline.py --mock
```

This uses synthetic data, checks the main pipeline, and costs **0 FactSet
calls**.

To test the FF5 path:

```powershell
py -3.11 master_pipeline.py --mock --factor-model FF5 --factor-backtest
```

The mock run may say:

```
No Long-Short returns could be constructed
```

That is normally fine — the mock universe is intentionally too small for the
full strategy.

## 3. Pull a New Company

This section uses FactSet requests. Before running it, check whether you
already have the company in `outputs/` (see section 4).

Standard 12-quarter pull:

```powershell
py -3.11 master_pipeline.py --live --ticker CME-US --quarters 12
```

Replace `CME-US` with the ticker you want. Examples: `AAPL-US`, `MSFT-US`,
`TSM-US`, `NVDA-US`, `CME-US`.

**What the command does:**
- Pulls actual EPS.
- Pulls analyst estimates and revision dates.
- Pulls the required price and market cap information.
- Calculates forecast errors.
- Runs the analyst and company calculations.
- Saves the resulting CSV files in `outputs/`.

## 4. Check What Companies You Already Have

Before spending more API calls, run:

```powershell
Get-ChildItem outputs\live_*_raw_forecast_errors.csv | Select-Object -ExpandProperty Name
```

Example results:

```
live_AAPL_US_raw_forecast_errors.csv
live_CME_US_raw_forecast_errors.csv
live_KLAC_US_raw_forecast_errors.csv
live_TSM_US_raw_forecast_errors.csv
```

If the company is already there, you normally do not need to pull it again.
Reuse it with the existing-output command instead:

```powershell
py -3.11 master_pipeline.py --from-outputs --tickers AAPL-US,CME-US,TSM-US,KLAC-US --factor-model FF5 --factor-backtest
```

## 5. What One Company Pull Creates

For a company such as CME-US, the important files are:

**Raw data**
```
live_CME_US_raw_estimates.csv
live_CME_US_raw_actuals.csv
live_CME_US_raw_prices.csv
live_CME_US_raw_forecast_errors.csv
```

**Analysis**
```
live_CME_US_consensus.csv
live_CME_US_industry_sentiment.csv
live_CME_US_partial_leaderboard.csv
live_CME_US_run_info.csv
```

**What the files mean**

| File | What it contains |
|---|---|
| `raw_estimates` | Analyst, broker, estimate, revision date, snapshot date, and related raw fields. |
| `raw_actuals` | Actual EPS and earnings report dates. |
| `raw_prices` | Price date, price, market cap, and available shares information. |
| `raw_forecast_errors` | The main cleaned analyst forecast error dataset. |
| `consensus` | Company and quarter consensus calculations. |
| `industry_sentiment` | Company observations mapped into Fama-French industries. |
| `partial_leaderboard` | Analyst ranking available from the current history. |
| `run_info` | Basic information about the run. |

## 6. Create the Excel Workbook

Once the company CSVs exist:

```powershell
py -3.11 export_to_excel.py --ticker CME-US
```

It creates:

```
outputs\live_CME_US_model.xlsx
```

Open it:

```powershell
ii .\outputs\live_CME_US_model.xlsx
```

The workbook contains the raw data and analyst and broker analysis. The
Analyst Charts sheet is intentionally kept to the Analyst Bubble Map.

## 7. Change How Many Quarters Are Pulled

**Change one run only:**

```powershell
py -3.11 master_pipeline.py --live --ticker MSFT-US --quarters 20
```

**Change the default:** open `src/config.py`, find:

```python
LIVE_N_QUARTERS = 12
```

and change `12` to the default you want.

**Approximate request usage:**

| Quarters | Approx. requests |
|---|---|
| 12 | ~25 |
| 20 | ~41 |
| 24 | ~49 |
| 28 | ~57 |

More history is useful, but company breadth is also important for the
multi-company model.

## 8. FactSet Limit Warning

The account currently has a **100 request per day** limit. If you see:

```
FactSet DAILY request limit would be exceeded
```

Stop `--live` pulls. You can still use `--mock`, `--from-outputs`, and
`export_to_excel.py` — those workflows do not require another FactSet pull.

## 9. Combine Companies With Zero API Calls

This is the important multi-company command:

```powershell
py -3.11 master_pipeline.py --from-outputs --tickers AAPL-US,APO-US,CME-US,EME-US,GFS-US,GME-US,GS-US,KLAC-US,NBIS-US,NRG-US,TSM-US --factor-model FF5 --factor-backtest
```

FactSet usage: 0 calls.

**What the command does:**
- Loads the existing company CSVs.
- Combines the raw forecast error data.
- Runs the analyst expanding-window models.
- Calculates company consensus.
- Builds industry sentiment.
- Attempts to build the Long-Short strategy.
- Runs the FF5 factor backtest when enough data exists.

**Never put `...` in the ticker list.** `AAPL-US,CME-US,...` is wrong
because the program will treat `...` as a ticker.

## 10. Master Files

The combined run can create the following files.

**Master raw data**
```
master_raw_forecast_errors.csv
master_raw_estimates.csv
master_raw_actuals.csv
master_raw_prices.csv
```

**Master analysis**
```
master_consensus.csv
master_industry_sentiment.csv
master_analyst_scores.csv
master_run_info.csv
```

**Created when the factor strategy successfully runs**
```
master_ff_factors.csv
master_strategy_returns.csv
master_factor_regression.csv
```

If the factor files do not appear, the strategy probably did not have
enough usable data yet.

## 11. Read the Multi-Company Terminal Output

You may see something like:

```
[1] input panel: 932 rows, 162 analysts, 11 firms
[2] expanding-window NN/linear predictions: 1 rows with a valid NN prediction
[4a] industries seen: [...]
[6] analyst reliability scores computed for 1 analysts
```

Pay attention to these four things:

- **Firms** — how many companies are included.
- **Valid NN predictions** — whether analysts have enough usable historical
  observations for the expanding-window model.
- **Industries seen** — how broad the company universe is across
  Fama-French industries.
- **Analyst reliability scores** — how many analysts reached the fuller,
  model-based scoring stage.

For example, "11 companies, 8 industries, 1 valid NN prediction" is useful
data, but it is not enough for the full Long-Short FF5 strategy.

## 12. Fama-French Options

The project supports:

- FF3+MOM
- FF5
- FF5+MOM

**Pure FF5:**

```powershell
py -3.11 master_pipeline.py --from-outputs --tickers AAPL-US,CME-US,TSM-US,KLAC-US --factor-model FF5 --factor-backtest
```

Factors: `Mkt-RF`, `SMB`, `HML`, `RMW`, `CMA`.

**FF5 + Momentum:**

```powershell
py -3.11 master_pipeline.py --from-outputs --tickers AAPL-US,CME-US,TSM-US,KLAC-US --factor-model FF5+MOM --factor-backtest
```

Factors: `Mkt-RF`, `SMB`, `HML`, `RMW`, `CMA`, `MOM`.

The factor data is loaded through `src/factors.py` from the Ken French
factor files configured for the project.

## 13. Why the Strategy Sometimes Says No Result

The strategy currently uses 5 long industries and 5 short industries — it
needs at least 10 usable industries. It also needs enough analyst history
to generate the sentiment signal.

If you see:

```
No Long-Short returns could be constructed
```

check:
- How many firms are included?
- How many industries are represented?
- How many valid NN predictions were produced?

The normal solution is to add more usable company data.

## 14. Adding a Large Company Universe

Do it in batches.

**Step 1:** List existing companies.
```powershell
Get-ChildItem outputs\live_*_raw_forecast_errors.csv | Select-Object -ExpandProperty Name
```

**Step 2:** Remove those companies from the new pull list. Do not spend
FactSet calls pulling data you already have unless you intentionally want
to refresh or extend it.

**Step 3:** Pull only missing companies.
```powershell
py -3.11 master_pipeline.py --live --ticker MSFT-US --quarters 12
```

**Step 4:** Stop before the FactSet limit — watch the usage messages
printed by the pipeline.

**Step 5:** Rerun the combined analysis.
```powershell
py -3.11 master_pipeline.py --from-outputs --tickers TICKER1,TICKER2,TICKER3 --factor-model FF5 --factor-backtest
```

**Step 6:** Check the important numbers — number of firms, industries seen,
valid NN predictions.

**Step 7:** Add more companies later if needed. Repeat the process without
re-pulling the companies already stored in `outputs/`.

## 15. Project File Map

| File / folder | What it is |
|---|---|
| `master_pipeline.py` | Main program. |
| `export_to_excel.py` | Creates company Excel workbooks. |
| `src/config.py` | Project settings such as `LIVE_N_QUARTERS`. |
| `src/factset_data.py` | FactSet data collection and cleaning. |
| `src/factors.py` | Fama-French factors and strategy regression. |
| `src/api_usage_tracker.py` | API usage protection. |
| `src/ff48_industries.py` | SIC to Fama-French industry mapping. |
| `src/sic_lookup.py` | SIC lookup. |
| `outputs/` | Generated CSV and Excel files. |

## 16. Common Problems

**"Python 2.7 does not support an f-prefix"**
Use Python 3.11:
```powershell
py -3.11 master_pipeline.py --mock
```

**PowerShell shows hundreds of errors about `def`, `from`, variables, or
brackets**
Python source code was pasted into PowerShell. Open a fresh terminal and
paste only commands, not the contents of a `.py` file.

**`cd` fails on "Analyst Model code"**
Use quotation marks:
```powershell
cd "C:\Users\patri\Documents\Analyst Model code\analyst-model"
```

**`py_compile` prints nothing**
That's a **PASS** — that is what success looks like.

**Missing raw forecast error CSV**
The requested ticker does not exist in `outputs/`. Check:
```powershell
Get-ChildItem outputs\live_*_raw_forecast_errors.csv | Select-Object -ExpandProperty Name
```
It searches for `live_..._raw_forecast_errors.csv`. If you put `...` in the
ticker command by mistake, remove it and use only real tickers.

**FF5 files or sheets are missing**
The strategy did not yet produce a valid return series. Check industry
coverage and valid NN predictions first (see section 11).

**`py -3.11` isn't recognized at all, or opens the Microsoft Store**
Python either isn't installed, or wasn't added to PATH during install (see
section 0). Reinstall from python.org and make sure "Add python.exe to
PATH" is checked, or disable the Windows Store alias under Settings > Apps
> Advanced app settings > App execution aliases.

---

## 17. Command Cheat Sheet

**SAFE: zero FactSet calls**

```powershell
# Mock test
py -3.11 master_pipeline.py --mock

# Check existing companies
Get-ChildItem outputs\live_*_raw_forecast_errors.csv | Select-Object -ExpandProperty Name

# Combine downloaded companies
py -3.11 master_pipeline.py --from-outputs --tickers AAPL-US,CME-US,TSM-US,KLAC-US --factor-model FF5 --factor-backtest

# Create Excel
py -3.11 export_to_excel.py --ticker CME-US

# Open Excel
ii .\outputs\live_CME_US_model.xlsx
```

**LIVE: uses FactSet**

```powershell
# Pull one company
py -3.11 master_pipeline.py --live --ticker CME-US --quarters 12
```

**CODE CHECKS: zero FactSet calls**

```powershell
py -3.11 -m py_compile master_pipeline.py
py -3.11 -m py_compile export_to_excel.py
py -3.11 -m py_compile src\factset_data.py
py -3.11 -m py_compile src\factors.py
```

No output = PASS.

---

## The Three Commands to Understand

| Command | What it does | FactSet usage |
|---|---|---|
| `--mock` | Synthetic test data. | 0 calls |
| `--live` | Downloads new real company data. | Uses requests |
| `--from-outputs` | Reuses real company data already saved in `outputs/`. | 0 calls |

If you are unsure whether you need another FactSet call, check `outputs/`
first.
