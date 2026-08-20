"""
Central config for this project. Change values here instead of hardcoding
numbers elsewhere.
"""

# --- Smoke-test ticker (single identifier, cheap: 1 API point per call) ---
TEST_TICKER = "AAPL-US"

# --- Universe example from the FactSet training email ---
# The Stoxx Europe 600 ETF. FG_CONSTITUENTS(EXSA-DE,0,CLOSE))=1 as a
# *universe* pulls ~600 identifiers (~600 API points) in ONE call -- don't
# run that until the single-ticker tests below are confirmed working.
EXAMPLE_ETF_TICKER = "EXSA-DE"
EXAMPLE_UNIVERSE_FORMULA = f"(FG_CONSTITUENTS({EXAMPLE_ETF_TICKER},0,CLOSE))=1"

# --- Session budget guardrail ---
# Patrick originally asked to keep this session to ~35-50 total API requests
# while validating the approach on a single ticker (AAPL-US). Two explicit,
# informed raises since then, each after being shown the exact cost math:
#   1. LIVE_N_QUARTERS 6 -> 12 (2023-onward history) pushed usage to ~52,
#      so the cap went 40 -> 60.
#   2. Adding a SECOND ticker (KLAC-US) at the same 12-quarter depth costs
#      ~25 more (~77 total) -- past the 60 cap, so Patrick chose to raise
#      the cap again rather than shrink KLAC-US's history depth.
# Set to 150 here: covers the current ~77 (AAPL + KLAC) with room for
# roughly 2-3 MORE tickers at the same 12-quarter depth (~25 pts each)
# before hitting this ceiling again. This is no longer tied to the original
# 35-50 range Patrick first set -- that constraint was for the single-
# ticker validation phase, which is done. Raise further, same way, whenever
# you add more tickers than this covers.
SESSION_CALL_BUDGET = 150

# --- Live pull settings (src/factset_data.py, master_pipeline.fetch_live_ticker_data) ---
# How many trailing quarters of actual EPS / analyst estimates to pull per
# ticker. Cost for one ticker = 1 + 2*LIVE_N_QUARTERS API points (see
# src/factset_data.py's module docstring for the breakdown). 12 quarters (3
# years, back to 2023-03-31) = 25 points per ticker -- Patrick's explicit
# choice after "2 years might not be enough" (this session's earlier 6-
# quarter runs only reached back to late 2024). Raise further once you're
# ready to trade more budget for more per-analyst history (more quarters =
# more forecast-error observations per analyst = closer to the paper's
# MIN_TRAINING_OBS=10, though real cross-firm coverage still needs a
# multi-ticker universe, not just a longer single-ticker window).
LIVE_N_QUARTERS = 12

# Paper's Eq. 1 (p.11): price is measured this many NYSE TRADING days before
# the earnings announcement.
TRADING_DAYS_BEFORE_EARNINGS = 10

# How many trading days before the earnings announcement to snapshot each
# analyst's LAST EPS estimate (i.e. her most recent forecast going into the
# print). 1 = the trading day right before the announcement.
ANALYST_SNAPSHOT_DAYS_BEFORE_EARNINGS = 1

# --- Credibility weighting (Patrick's request: don't let an analyst with a
#     handful of lucky observations outrank one with a long, solid track
#     record) ---
# Both analyst_reliability_scores() and simple_accuracy_leaderboard() in
# master_pipeline.py multiply an analyst's raw z-scored composite by
# n_obs / (n_obs + CREDIBILITY_PRIOR_OBS) before ranking -- an analyst with
# exactly this many observations gets her score trusted at 50% strength;
# fewer observations, more shrinkage toward 0 (neutral); more observations,
# closer to 100%. Set to 10 to match MIN_TRAINING_OBS above (the paper's own
# "minimum history before we trust a model on this analyst" threshold, p.14)
# -- same number, same justification, so the explanation stays consistent:
# "we don't fully trust anyone's score until we've seen ~10 quarters of her
# calls."
CREDIBILITY_PRIOR_OBS = 10

# --- SIC code lookup (for FF48 industry mapping, src/ff48_industries.py) ---
# UPDATE: this dict is no longer required for every new ticker.
# fetch_live_ticker_data() (master_pipeline.py) now checks this dict FIRST,
# then automatically falls back to src/sic_lookup.py, which resolves SIC
# codes via SEC EDGAR's free public JSON API (0 FactSet calls -- a
# completely separate data source from CallBudget). Keep entries here only
# as a manual override for tickers SEC EDGAR can't resolve (e.g.
# non-US-domestic filers) or if you ever want to force a different code than
# what SEC has on file.
TICKER_SIC_OVERRIDES = {
    "AAPL-US": 3571,  # Electronic Computers -- confirmed via SEC EDGAR, FF48 industry "Computers"
    "KLAC-US": 3827,  # Optical Instruments & Lenses -- confirmed via SEC EDGAR
                      # (data.sec.gov/submissions/CIK0000319201.json, KLA Corp,
                      # CIK 0000319201) -- FF48 industry 37 "LabEq" (Measuring
                      # and Control Equipment)
}

# --- Fama-French factor / strategy-backtest data (Ken French's free data
#     library -- 0 FactSet calls; see src/factors.py) ---
# Directory containing the downloaded F-F_*.zip factor files (the ones already
# sitting in this repo's sibling Fama_French_Data folder).
FF_DATA_DIR = r"C:\Users\patri\Documents\Analyst Model code\Fama_French_Data\Fama_French_Data"

# Factor model for the industry Long-Short backtest (paper Section 4.5, Eq. 6).
# "FF3+MOM" = Carhart 4-factor (Mkt-RF, SMB, HML, MOM) -- matches the paper's
# quoted "four-factor alpha 0.62%, t=2.48". "FF5+MOM" adds RMW, CMA with zero
# rework (the regression only references factor columns that exist).
FACTOR_MODEL = "FF3+MOM"

# Paper's strategy: Long the K LOWEST-sentiment industries, Short the K highest,
# rebalanced monthly.
K_PER_LEG = 5

# How each leg is weighted across industries: "equal" (interim default -- French's
# industry portfolio returns are already value-weighted WITHIN industry, and we
# don't have cross-industry market caps yet). "value" is reserved for when a
# multi-ticker universe supplies industry-level market caps.
STRATEGY_WEIGHTS = "equal"

# Minimum QUARTERLY observations for per-analyst factor regression (see
# compute_analyst_factor_alpha() in src/factors.py). This used to require 60
# MONTHLY observations (5 years) by forward-filling each quarterly residual
# into 3 identical monthly copies -- a real methodological problem, not just
# a data-availability one: forward-filling manufactures 3 fully-correlated,
# non-independent "observations" out of 1 real one, which deflates the
# regression's standard errors and can make a factor-alpha look more
# statistically significant than the data actually supports. Fixed by
# regressing directly at the natural quarterly frequency instead (one real,
# independent observation per analyst per quarter, against quarter-compounded
# factor returns) -- so this constant is now a genuine "how many actual
# quarters of history" bar, not an artificially-inflated monthly count.
# Set to 10 to match CREDIBILITY_PRIOR_OBS/MIN_TRAINING_OBS above (this
# project's one consistent "minimum history before we trust a number"
# threshold) -- still thin for a 4-6 factor OLS (10 obs - 4 factors - 1
# intercept = 5 degrees of freedom for FF3+MOM), so factor_alpha_tstat is
# reported alongside factor_alpha specifically so a thin, noisy regression is
# visible rather than hidden. Override per-run with --min-factor-obs.
MIN_FACTOR_OBS = 10
