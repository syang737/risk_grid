"""Pricer correctness.

Reference values are from Hull, *Options, Futures and Other Derivatives*.
"""

from __future__ import annotations

import numpy as np
import pytest

from risk.pricing import american_price, black_scholes, black_scholes_greeks


def arr(*vals):
    return [np.atleast_1d(np.asarray(v, dtype=float if not isinstance(v, bool) else bool)) for v in vals]


# S=42, K=40, T=0.5, r=10%, q=0, vol=20%
HULL = dict(S=42.0, K=40.0, T=0.5, r=0.10, q=0.0, vol=0.20)


def test_hull_reference_prices():
    call = black_scholes(HULL["S"], HULL["K"], HULL["T"], HULL["r"], HULL["q"], HULL["vol"], True)
    put = black_scholes(HULL["S"], HULL["K"], HULL["T"], HULL["r"], HULL["q"], HULL["vol"], False)
    assert call == pytest.approx(4.76, abs=0.005)
    assert put == pytest.approx(0.81, abs=0.005)


def test_put_call_parity():
    rng = np.random.default_rng(0)
    n = 5000
    S = rng.uniform(5, 500, n)
    K = S * rng.uniform(0.5, 1.5, n)
    T = rng.uniform(0.01, 3.0, n)
    r = rng.uniform(0.0, 0.08, n)
    q = rng.uniform(0.0, 0.05, n)
    vol = rng.uniform(0.05, 1.2, n)
    t = np.ones(n, dtype=bool)

    call = black_scholes(S, K, T, r, q, vol, t)
    put = black_scholes(S, K, T, r, q, vol, ~t)
    lhs = call - put
    rhs = S * np.exp(-q * T) - K * np.exp(-r * T)
    np.testing.assert_allclose(lhs, rhs, rtol=1e-9, atol=1e-8)


def test_greeks_match_finite_difference():
    rng = np.random.default_rng(1)
    n = 2000
    S = rng.uniform(20, 300, n)
    K = S * rng.uniform(0.7, 1.3, n)
    T = rng.uniform(0.05, 2.0, n)
    r = np.full(n, 0.04)
    q = np.full(n, 0.01)
    vol = rng.uniform(0.1, 0.8, n)
    is_call = rng.random(n) < 0.5

    g = black_scholes_greeks(S, K, T, r, q, vol, is_call)

    h = 1e-4
    up = black_scholes(S * (1 + h), K, T, r, q, vol, is_call)
    dn = black_scholes(S * (1 - h), K, T, r, q, vol, is_call)
    fd_delta = (up - dn) / (2 * S * h)
    np.testing.assert_allclose(g["delta"], fd_delta, rtol=1e-4, atol=1e-6)

    mid = black_scholes(S, K, T, r, q, vol, is_call)
    fd_gamma = (up - 2 * mid + dn) / (S * h) ** 2
    np.testing.assert_allclose(g["gamma"], fd_gamma, rtol=1e-3, atol=1e-6)

    vh = 1e-5
    fd_vega = (black_scholes(S, K, T, r, q, vol + vh, is_call)
               - black_scholes(S, K, T, r, q, vol - vh, is_call)) / (2 * vh) * 0.01
    np.testing.assert_allclose(g["vega"], fd_vega, rtol=1e-5, atol=1e-8)


def test_expired_options_are_intrinsic():
    S = np.array([42.0, 38.0, 42.0, 38.0])
    K = np.full(4, 40.0)
    T = np.zeros(4)
    is_call = np.array([True, True, False, False])
    px = black_scholes(S, K, T, np.full(4, 0.05), np.zeros(4), np.full(4, 0.3), is_call)
    np.testing.assert_allclose(px, [2.0, 0.0, 0.0, 2.0])

    g = black_scholes_greeks(S, K, T, np.full(4, 0.05), np.zeros(4), np.full(4, 0.3), is_call)
    np.testing.assert_allclose(g["delta"], [1.0, 0.0, 0.0, -1.0])
    np.testing.assert_allclose(g["gamma"], np.zeros(4))


def test_american_never_below_european_or_intrinsic():
    rng = np.random.default_rng(2)
    n = 5000
    S = rng.uniform(10, 300, n)
    K = S * rng.uniform(0.6, 1.4, n)
    T = rng.uniform(0.02, 2.0, n)
    r = rng.uniform(0.005, 0.08, n)
    q = rng.uniform(0.0, 0.06, n)
    vol = rng.uniform(0.08, 1.0, n)
    is_call = rng.random(n) < 0.5

    amer = american_price(S, K, T, r, q, vol, is_call)
    euro = black_scholes(S, K, T, r, q, vol, is_call)
    intrinsic = np.maximum(np.where(is_call, S - K, K - S), 0.0)

    assert np.all(np.isfinite(amer))
    assert np.all(amer >= euro - 1e-8)
    assert np.all(amer >= intrinsic - 1e-8)


def test_american_call_without_dividend_equals_european():
    """Early exercise of a call is never optimal when there is no dividend."""
    rng = np.random.default_rng(3)
    n = 1000
    S = rng.uniform(10, 300, n)
    K = S * rng.uniform(0.6, 1.4, n)
    T = rng.uniform(0.02, 2.0, n)
    r = rng.uniform(0.005, 0.08, n)
    q = np.zeros(n)
    vol = rng.uniform(0.08, 1.0, n)
    is_call = np.ones(n, dtype=bool)

    np.testing.assert_allclose(
        american_price(S, K, T, r, q, vol, is_call),
        black_scholes(S, K, T, r, q, vol, is_call),
        rtol=1e-10,
    )


def test_american_put_premium_grows_with_moneyness():
    """The premium the fast model holds fixed is precisely the one that moves."""
    K = np.full(5, 100.0)
    S = np.array([120.0, 105.0, 100.0, 85.0, 70.0])
    T = np.full(5, 0.5)
    r = np.full(5, 0.05)
    q = np.zeros(5)
    vol = np.full(5, 0.3)
    put = np.zeros(5, dtype=bool)

    prem = american_price(S, K, T, r, q, vol, put) - black_scholes(S, K, T, r, q, vol, put)
    assert np.all(np.diff(prem) > 0), "premium should increase as the put goes deeper ITM"
