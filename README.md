Analyst Model

Practical Run Guide

Use this README to run the project. Keep the separate methodology document for the academic methodology, equations, and detailed research explanation.

Quick Start

1. Open the project

Open PowerShell or the PyCharm Terminal and go to the project folder.

cd "C:\Users\patri\Documents\Analyst Model code\analyst-model"

Check Python.

py -3.11 --version

You should see Python 3.11.x.

2. Test the project

py -3.11 master_pipeline.py --mock

This uses synthetic data and makes 0 FactSet calls.

3. Pull a new company

py -3.11 master_pipeline.py --live --ticker CME-US --quarters 12

This downloads new FactSet data and saves the results in outputs/.

4. Create the Excel workbook

py -3.11 export_to_excel.py --ticker CME-US

Open it.

ii .\outputs\live_CME_US_model.xlsx

Copy and Paste Commands

Enter the project folder

cd "C:\Users\patri\Documents\Analyst Model code\analyst-model"

Check Python 3.11

py -3.11 --version

Safe test with zero API calls

py -3.11 master_pipeline.py --mock

Pull one new company

py -3.11 master_pipeline.py --live --ticker CME-US --quarters 12

See every company already downloaded

Get-ChildItem outputs\live_*_raw_forecast_errors.csv | Select-Object -ExpandProperty Name

Build one company Excel workbook

py -3.11 export_to_excel.py --ticker CME-US

Open the Excel workbook

ii .\outputs\live_CME_US_model.xlsx

Combine existing companies and run pure FF5 with zero API calls

py -3.11 master_pipeline.py --from-outputs --tickers AAPL-US,APO-US,CME-US,EME-US,GFS-US,GME-US,GS-US,KLAC-US,NBIS-US,NRG-US,TSM-US --factor-model FF5 --factor-backtest

Check the important Python files for syntax errors

py -3.11 -m py_compile master_pipeline.py
py -3.11 -m py_compile export_to_excel.py
py -3.11 -m py_compile src\factset_data.py
py -3.11 -m py_compile src\factors.py

No output from py_compile means PASS.

Which Command Should I Use?

Test the code

py -3.11 master_pipeline.py --mock

FactSet usage: 0 calls

Pull a new company

py -3.11 master_pipeline.py --live --ticker CME-US --quarters 12

FactSet usage: Uses calls

Reuse companies already downloaded

Use the existing-output mode as part of the full command:

py -3.11 master_pipeline.py --from-outputs --tickers AAPL-US,CME-US,TSM-US,KLAC-US --factor-model FF5 --factor-backtest

FactSet usage: 0 calls

Create a company Excel workbook

py -3.11 export_to_excel.py --ticker CME-US

FactSet usage: 0 calls

Most important rule

--live downloads new data.

--from-outputs reuses data already saved in outputs/.

--mock uses synthetic test data.

If you are trying to save FactSet usage, do not use --live.

1. Open the Project

Open PowerShell or the PyCharm Terminal.

Go to the project folder.

cd "C:\Users\patri\Documents\Analyst Model code\analyst-model"

Keep the quotation marks because the folder path contains spaces.

Check Python.

py -3.11 --version

Expected result: Python 3.11.x.

2. Quick Safety Test

Before doing a live FactSet pull, run:

py -3.11 master_pipeline.py --mock

This uses synthetic data, checks the main pipeline, and costs 0 FactSet calls.

To test the FF5 path:

py -3.11 master_pipeline.py --mock --factor-model FF5 --factor-backtest

The mock run may say:

No Long-Short returns could be constructed

That is normally fine. The mock universe is intentionally too small for the full strategy.

3. Pull a New Company

This section uses FactSet requests.

Before running it, check whether you already have the company in outputs/.

Standard 12 quarter pull:

py -3.11 master_pipeline.py --live --ticker CME-US --quarters 12

Replace CME-US with the ticker you want.

Examples:

AAPL-US

MSFT-US

TSM-US

NVDA-US

CME-US

What the command does

Pulls actual EPS.

Pulls analyst estimates and revision dates.

Pulls the required price and market cap information.

Calculates forecast errors.

Runs the analyst and company calculations.

Saves the resulting CSV files in outputs/.

4. Check What Companies You Already Have

Before spending more API calls, run:

Get-ChildItem outputs\live_*_raw_forecast_errors.csv | Select-Object -ExpandProperty Name

Example results:

live_AAPL_US_raw_forecast_errors.csv

live_CME_US_raw_forecast_errors.csv

live_KLAC_US_raw_forecast_errors.csv

live_TSM_US_raw_forecast_errors.csv

If the company is already there, you normally do not need to pull it again.

Reuse it with the existing-output command instead of pulling it again:

py -3.11 master_pipeline.py --from-outputs --tickers AAPL-US,CME-US,TSM-US,KLAC-US --factor-model FF5 --factor-backtest

5. What One Company Pull Creates

For a company such as CME-US, the important files are:

Raw data

live_CME_US_raw_estimates.csv

live_CME_US_raw_actuals.csv

live_CME_US_raw_prices.csv

live_CME_US_raw_forecast_errors.csv

Analysis

live_CME_US_consensus.csv

live_CME_US_industry_sentiment.csv

live_CME_US_partial_leaderboard.csv

live_CME_US_run_info.csv

What the files mean

raw_estimates
Analyst, broker, estimate, revision date, snapshot date, and related raw fields.

raw_actuals
Actual EPS and earnings report dates.

raw_prices
Price date, price, market cap, and available shares information.

raw_forecast_errors
The main cleaned analyst forecast error dataset.

consensus
Company and quarter consensus calculations.

industry_sentiment
Company observations mapped into Fama French industries.

partial_leaderboard
Analyst ranking available from the current history.

run_info
Basic information about the run.

6. Create the Excel Workbook

Once the company CSVs exist:

py -3.11 export_to_excel.py --ticker CME-US

It creates:

outputs\live_CME_US_model.xlsx

Open it:

ii .\outputs\live_CME_US_model.xlsx

The workbook contains the raw data and analyst and broker analysis.

The Analyst Charts sheet is intentionally kept to the Analyst Bubble Map.

7. Change How Many Quarters Are Pulled

Change one run only

py -3.11 master_pipeline.py --live --ticker MSFT-US --quarters 20

Change the default

Open src/config.py.

Find:

LIVE_N_QUARTERS = 12

Change 12 to the default you want.

Approximate request usage

12 quarters: approximately 25 requests

20 quarters: approximately 41 requests

24 quarters: approximately 49 requests

28 quarters: approximately 57 requests

More history is useful, but company breadth is also important for the multi company model.

8. FactSet Limit Warning

The account currently has a 100 request per day limit.

If you see:

FactSet DAILY request limit would be exceeded

Stop --live pulls.

You can still use:

--mock

--from-outputs

export_to_excel.py

Those workflows do not require another FactSet pull.

9. Combine Companies With Zero API Calls

This is the important multi company command.

py -3.11 master_pipeline.py --from-outputs --tickers AAPL-US,APO-US,CME-US,EME-US,GFS-US,GME-US,GS-US,KLAC-US,NBIS-US,NRG-US,TSM-US --factor-model FF5 --factor-backtest

FactSet usage: 0 calls

What the command does

Loads the existing company CSVs.

Combines the raw forecast error data.

Runs the analyst expanding window models.

Calculates company consensus.

Builds industry sentiment.

Attempts to build the Long Short strategy.

Runs the FF5 factor backtest when enough data exists.

Never put ... in the ticker list.

AAPL-US,CME-US,... is wrong because the program will treat ... as a ticker.

10. Master Files

The combined run can create the following files.

Master raw data

master_raw_forecast_errors.csv

master_raw_estimates.csv

master_raw_actuals.csv

master_raw_prices.csv

Master analysis

master_consensus.csv

master_industry_sentiment.csv

master_analyst_scores.csv

master_run_info.csv

Created when the factor strategy successfully runs

master_ff_factors.csv

master_strategy_returns.csv

master_factor_regression.csv

If the factor files do not appear, the strategy probably did not have enough usable data yet.

11. Read the Multi Company Terminal Output

You may see:

[1] input panel: 932 rows, 162 analysts, 11 firms
[2] expanding-window NN/linear predictions: 1 rows with a valid NN prediction
[4a] industries seen: [...]
[6] analyst reliability scores computed for 1 analysts

Pay attention to these four things.

Firms

How many companies are included.

Valid NN predictions

Whether analysts have enough usable historical observations for the expanding window model.

Industries seen

How broad the company universe is across Fama French industries.

Analyst reliability scores

How many analysts reached the fuller model based scoring stage.

For example:

11 companies
8 industries
1 valid NN prediction

is useful data, but it is not enough for the full Long Short FF5 strategy.

12. Fama French Options

The project supports:

FF3+MOM

FF5

FF5+MOM

Pure FF5

Run the full command like this:

py -3.11 master_pipeline.py --from-outputs --tickers AAPL-US,CME-US,TSM-US,KLAC-US --factor-model FF5 --factor-backtest

The factors are:

Mkt-RF

SMB

HML

RMW

CMA

FF5 + Momentum

Run the full command like this:

py -3.11 master_pipeline.py --from-outputs --tickers AAPL-US,CME-US,TSM-US,KLAC-US --factor-model FF5+MOM --factor-backtest

The factors are:

Mkt-RF

SMB

HML

RMW

CMA

MOM

The factor data is loaded through src/factors.py from the Ken French factor files configured for the project.

13. Why the Strategy Sometimes Says No Result

The strategy currently uses:

5 Long industries

5 Short industries

That means it needs at least 10 usable industries.

It also needs enough analyst history to generate the sentiment signal.

If you see:

No Long-Short returns could be constructed

check:

How many firms are included?

How many industries are represented?

How many valid NN predictions were produced?

The normal solution is to add more usable company data.

14. Adding a Large Company Universe

Do it in batches.

Step 1: List existing companies

Get-ChildItem outputs\live_*_raw_forecast_errors.csv | Select-Object -ExpandProperty Name

Step 2: Remove those companies from the new pull list

Do not spend FactSet calls pulling data you already have unless you intentionally want to refresh or extend it.

Step 3: Pull only missing companies

py -3.11 master_pipeline.py --live --ticker MSFT-US --quarters 12

Step 4: Stop before the FactSet limit

Watch the usage messages printed by the pipeline.

Step 5: Rerun the combined analysis

py -3.11 master_pipeline.py --from-outputs --tickers TICKER1,TICKER2,TICKER3 --factor-model FF5 --factor-backtest

Step 6: Check the important numbers

Watch:

number of firms

industries seen

valid NN predictions

Step 7: Add more companies later if needed

Repeat the process without repulling the companies already stored in outputs/.

15. Project File Map

master_pipeline.py
Main program.

export_to_excel.py
Creates company Excel workbooks.

src/config.py
Project settings such as LIVE_N_QUARTERS.

src/factset_data.py
FactSet data collection and cleaning.

src/factors.py
Fama French factors and strategy regression.

src/api_usage_tracker.py
API usage protection.

src/ff48_industries.py
SIC to Fama French industry mapping.

src/sic_lookup.py
SIC lookup.

outputs/
Generated CSV and Excel files.

16. Common Problems

Python 2.7 does not support an F prefix

Use Python 3.11:

py -3.11 master_pipeline.py --mock

PowerShell shows hundreds of errors about def, from, variables, or brackets

Python source code was pasted into PowerShell.

Open a fresh terminal and paste only commands, not the contents of a .py file.

cd fails on Analyst Model code

Use quotation marks:

cd "C:\Users\patri\Documents\Analyst Model code\analyst-model"

py_compile prints nothing

PASS. That is what success looks like.

Missing raw forecast error CSV

The requested ticker does not exist in outputs/.

Check:

Get-ChildItem outputs\live_*_raw_forecast_errors.csv | Select-Object -ExpandProperty Name

It searches for live_..._raw_forecast_errors.csv

You put ... in the ticker command. Remove it and use only real tickers.

FF5 files or sheets are missing

The strategy did not yet produce a valid return series.

Check industry coverage and valid NN predictions first.

17. Command Cheat Sheet

SAFE: zero FactSet calls

Mock test

py -3.11 master_pipeline.py --mock

Check existing companies

Get-ChildItem outputs\live_*_raw_forecast_errors.csv | Select-Object -ExpandProperty Name

Combine downloaded companies

py -3.11 master_pipeline.py --from-outputs --tickers AAPL-US,CME-US,TSM-US,KLAC-US --factor-model FF5 --factor-backtest

Create Excel

py -3.11 export_to_excel.py --ticker CME-US

Open Excel

ii .\outputs\live_CME_US_model.xlsx

LIVE: uses FactSet

Pull one company

py -3.11 master_pipeline.py --live --ticker CME-US --quarters 12

CODE CHECKS: zero FactSet calls

py -3.11 -m py_compile master_pipeline.py
py -3.11 -m py_compile export_to_excel.py
py -3.11 -m py_compile src\factset_data.py
py -3.11 -m py_compile src\factors.py

No output = PASS.

The Three Commands to Understand

--mock

Synthetic test data.

FactSet usage: 0 calls

--live

Downloads new real company data.

FactSet usage: Uses requests

--from-outputs

Reuses real company data already saved in outputs/.

FactSet usage: 0 calls

If you are unsure whether you need another FactSet call, check outputs/ first.