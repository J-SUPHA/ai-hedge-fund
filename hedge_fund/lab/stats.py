"""Overfitting-aware performance statistics.

The deflated Sharpe ratio (Bailey & López de Prado, 2014) answers: "given
that we tried N strategies whose Sharpe ratios varied this much, what is the
probability this one's true Sharpe is above zero?" A lab that runs thousands
of automated backtests MUST report this instead of the raw Sharpe — the best
of N noise strategies always looks good.

All Sharpe ratios here are per-period (not annualized) unless named
otherwise, because that is what the PSR/DSR formulas take.
"""

from __future__ import annotations

from math import isfinite, sqrt

import numpy as np
from scipy import stats as _st

EULER_GAMMA = 0.5772156649015329
TRADING_DAYS = 252


def returns_from_nav(nav: list[float]) -> list[float]:
    """Simple period returns from a NAV series."""
    arr = np.asarray(nav, dtype=float)
    if arr.size < 2:
        return []
    rets = arr[1:] / arr[:-1] - 1.0
    return [float(r) for r in rets if isfinite(r)]


def sharpe(returns: list[float]) -> float | None:
    """Per-period Sharpe (risk-free = 0). None when undefined."""
    arr = np.asarray(returns, dtype=float)
    if arr.size < 2:
        return None
    sd = arr.std(ddof=1)
    if not isfinite(sd) or sd == 0:
        return None
    return float(arr.mean() / sd)


def annualize(sr: float | None, periods_per_year: int = TRADING_DAYS) -> float | None:
    return None if sr is None else sr * sqrt(periods_per_year)


def moments(returns: list[float]) -> tuple[float, float]:
    """(skew, non-excess kurtosis); normal = (0, 3)."""
    arr = np.asarray(returns, dtype=float)
    if arr.size < 3 or arr.std() == 0:
        return 0.0, 3.0
    return float(_st.skew(arr)), float(_st.kurtosis(arr, fisher=False))


def probabilistic_sharpe(sr: float, sr_benchmark: float, n: int, skew: float, kurt: float) -> float:
    """P(true SR > sr_benchmark) given an observed per-period SR over n periods."""
    if n < 2:
        return float("nan")
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return float("nan")
    z = (sr - sr_benchmark) * sqrt(n - 1) / sqrt(denom)
    return float(_st.norm.cdf(z))


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """E[max SR] across n_trials independent zero-skill strategies (per period)."""
    if n_trials < 2 or sr_variance <= 0:
        return 0.0
    a = _st.norm.ppf(1.0 - 1.0 / n_trials)
    b = _st.norm.ppf(1.0 - 1.0 / (n_trials * np.e))
    return float(sqrt(sr_variance) * ((1.0 - EULER_GAMMA) * a + EULER_GAMMA * b))


def deflated_sharpe(returns: list[float], n_trials: int, sr_variance: float) -> dict:
    """DSR for one strategy's returns, deflated by the lab's trial history.

    `n_trials` counts every attempt (the honest denominator); `sr_variance`
    is the variance of per-period Sharpe across completed trials.
    """
    sr = sharpe(returns)
    if sr is None:
        return {"sharpe": None, "sharpe_annual": None, "sr0": None, "psr": None, "dsr": None,
                "n_trials": n_trials, "n_periods": len(returns)}
    skew, kurt = moments(returns)
    sr0 = expected_max_sharpe(n_trials, sr_variance)
    return {
        "sharpe": sr,
        "sharpe_annual": annualize(sr),
        "sr0": sr0,
        "sr0_annual": annualize(sr0),
        "psr": probabilistic_sharpe(sr, 0.0, len(returns), skew, kurt),
        "dsr": probabilistic_sharpe(sr, sr0, len(returns), skew, kurt),
        "skew": skew,
        "kurtosis": kurt,
        "n_trials": n_trials,
        "n_periods": len(returns),
    }
