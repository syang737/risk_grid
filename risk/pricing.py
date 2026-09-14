"""Vectorized option pricing and greeks.

Everything here operates on numpy arrays. There are no per-position loops: a
single call prices the whole book at once, which is what makes a full reprice
under 50 scenarios tractable in Python.

American options use Bjerksund-Stensland (1993). The 2002 refinement is more
accurate but needs a bivariate normal CDF, which has no fast vectorized
implementation in numpy/scipy -- at book scale that cost outweighs the accuracy
gain for stress scenarios.
"""

from __future__ import annotations

import numpy as np

SQRT_2 = np.sqrt(2.0)
INV_SQRT_2PI = 1.0 / np.sqrt(2.0 * np.pi)

# Below this many years to expiry an option is treated as expired and priced at
# intrinsic. Guards the 1/sqrt(T) terms.
MIN_T = 1e-8
MIN_VOL = 1e-8


def norm_cdf(x: np.ndarray) -> np.ndarray:
    """Standard normal CDF, vectorized via erf."""
    from scipy.special import ndtr

    return ndtr(x)


def norm_pdf(x: np.ndarray) -> np.ndarray:
    return INV_SQRT_2PI * np.exp(-0.5 * x * x)


def _d1_d2(S, K, T, r, q, vol):
    sqrt_t = np.sqrt(T)
    vol_sqrt_t = vol * sqrt_t
    d1 = (np.log(S / K) + (r - q + 0.5 * vol * vol) * T) / vol_sqrt_t
    return d1, d1 - vol_sqrt_t, sqrt_t


def black_scholes(S, K, T, r, q, vol, is_call):
    """European price.

    Branch-free: T and vol are clipped rather than masked, and expired rows are
    patched at the end. Masking looks cheaper but forces fancy-index copies of
    every input, which dominates at book scale.

    The put comes from put-call parity rather than two more normal CDF calls --
    at 50 scenarios x millions of rows those two calls are the difference
    between an interactive snapshot and a coffee break.
    """
    S, K, T, r, q, vol = (np.asarray(a, dtype=np.float64) for a in (S, K, T, r, q, vol))
    is_call = np.asarray(is_call, dtype=bool)

    T_ = np.maximum(T, MIN_T)
    v_ = np.maximum(vol, MIN_VOL)
    sqrt_t = np.sqrt(T_)
    vol_sqrt_t = v_ * sqrt_t

    d1 = (np.log(S / K) + (r - q + 0.5 * v_ * v_) * T_) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    disc_q = np.exp(-q * T_)
    disc_r = np.exp(-r * T_)

    s_dq = S * disc_q
    k_dr = K * disc_r
    call = s_dq * norm_cdf(d1) - k_dr * norm_cdf(d2)
    put = call - s_dq + k_dr

    price = np.where(is_call, call, put)
    intrinsic = np.maximum(np.where(is_call, S - K, K - S), 0.0)
    expired = (T <= MIN_T) | (vol <= MIN_VOL)
    return np.where(expired, intrinsic, np.maximum(price, 0.0))


def black_scholes_greeks(S, K, T, r, q, vol, is_call):
    """European price and greeks in one pass.

    Conventions match how a risk screen displays them: vega per 1 vol point,
    theta per calendar day, rho per 1bp. Put greeks come from parity.
    """
    S, K, T, r, q, vol = (np.asarray(a, dtype=np.float64) for a in (S, K, T, r, q, vol))
    is_call = np.asarray(is_call, dtype=bool)

    T_ = np.maximum(T, MIN_T)
    v_ = np.maximum(vol, MIN_VOL)
    sqrt_t = np.sqrt(T_)
    vol_sqrt_t = v_ * sqrt_t

    d1 = (np.log(S / K) + (r - q + 0.5 * v_ * v_) * T_) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    disc_q = np.exp(-q * T_)
    disc_r = np.exp(-r * T_)
    pdf_d1 = norm_pdf(d1)
    Nd1, Nd2 = norm_cdf(d1), norm_cdf(d2)

    s_dq = S * disc_q
    k_dr = K * disc_r
    call = s_dq * Nd1 - k_dr * Nd2

    carry = -s_dq * pdf_d1 * v_ / (2.0 * sqrt_t)
    theta_call = carry - r * k_dr * Nd2 + q * s_dq * Nd1
    theta_put = carry + r * k_dr * (1.0 - Nd2) - q * s_dq * (1.0 - Nd1)

    out = {
        "price": np.where(is_call, call, call - s_dq + k_dr),
        "delta": disc_q * np.where(is_call, Nd1, Nd1 - 1.0),
        "gamma": disc_q * pdf_d1 / (S * v_ * sqrt_t),
        "vega": s_dq * pdf_d1 * sqrt_t * 0.01,
        "theta": np.where(is_call, theta_call, theta_put) / 365.0,
        "rho": np.where(is_call, K * T_ * disc_r * Nd2, -K * T_ * disc_r * (1.0 - Nd2)) * 1e-4,
    }

    expired = (T <= MIN_T) | (vol <= MIN_VOL)
    if expired.any():
        intrinsic = np.maximum(np.where(is_call, S - K, K - S), 0.0)
        out["price"] = np.where(expired, intrinsic, out["price"])
        itm = np.where(is_call, S > K, S < K)
        out["delta"] = np.where(expired, np.where(itm, np.where(is_call, 1.0, -1.0), 0.0), out["delta"])
        for k in ("gamma", "vega", "theta", "rho"):
            out[k] = np.where(expired, 0.0, out[k])
    return out


def _phi(S, T, gamma, H, X, r, b, vol):
    """Helper term from Bjerksund-Stensland."""
    vol2 = vol * vol
    sqrt_t = np.sqrt(T)
    vol_sqrt_t = vol * sqrt_t

    lam = (-r + gamma * b + 0.5 * gamma * (gamma - 1.0) * vol2) * T
    kappa = 2.0 * b / vol2 + (2.0 * gamma - 1.0)
    d = -(np.log(S / H) + (b + (gamma - 0.5) * vol2) * T) / vol_sqrt_t
    ratio = X / S
    d2 = d - 2.0 * np.log(ratio) / vol_sqrt_t

    return np.exp(lam) * np.power(S, gamma) * (norm_cdf(d) - np.power(ratio, kappa) * norm_cdf(d2))


def _american_call_bs93(S, K, T, r, b, vol):
    """American call via Bjerksund-Stensland 1993. Assumes b < r elsewhere handled."""
    vol2 = vol * vol
    beta = (0.5 - b / vol2) + np.sqrt((b / vol2 - 0.5) ** 2 + 2.0 * r / vol2)

    b_inf = beta / (beta - 1.0) * K
    # r - b == q; guarded by caller (only reached when b < r).
    b_zero = np.maximum(K, r / np.maximum(r - b, 1e-12) * K)

    denom = b_inf - b_zero
    h_t = -(b * T + 2.0 * vol * np.sqrt(T)) * (b_zero / np.where(np.abs(denom) < 1e-12, 1e-12, denom))
    X = b_zero + denom * (1.0 - np.exp(h_t))
    X = np.maximum(X, K * (1.0 + 1e-9))

    alpha = (X - K) * np.power(X, -beta)

    val = (
        alpha * np.power(S, beta)
        - alpha * _phi(S, T, beta, X, X, r, b, vol)
        + _phi(S, T, 1.0, X, X, r, b, vol)
        - _phi(S, T, 1.0, K, X, r, b, vol)
        - K * _phi(S, T, 0.0, X, X, r, b, vol)
        + K * _phi(S, T, 0.0, K, X, r, b, vol)
    )
    # Above the exercise boundary the approximation breaks down; exercise value holds.
    return np.where(S >= X, S - K, val)


def american_price(S, K, T, r, q, vol, is_call):
    """American option price (Bjerksund-Stensland 1993).

    Puts use the standard transform P(S,K,r,b) = C(K,S,r-b,-b).
    """
    S, K, T, r, q, vol = np.broadcast_arrays(
        *(np.asarray(a, dtype=np.float64) for a in (S, K, T, r, q, vol))
    )
    is_call = np.broadcast_to(np.asarray(is_call, dtype=bool), S.shape)

    out = np.where(is_call, np.maximum(S - K, 0.0), np.maximum(K - S, 0.0)).astype(np.float64)
    live = (T > MIN_T) & (vol > MIN_VOL)
    if not live.any():
        return out

    Sl, Kl, Tl, rl, ql, vl = (a[live] for a in (S, K, T, r, q, vol))
    call_l = is_call[live]
    b = rl - ql

    res = np.empty_like(Sl)

    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        # Calls: early exercise is only optimal when b < r (i.e. q > 0).
        c_mask = call_l
        if c_mask.any():
            euro = black_scholes(Sl[c_mask], Kl[c_mask], Tl[c_mask], rl[c_mask], ql[c_mask], vl[c_mask], True)
            needs_amer = b[c_mask] < rl[c_mask]
            amer = euro.copy()
            if needs_amer.any():
                idx = np.where(c_mask)[0][needs_amer]
                amer[needs_amer] = _american_call_bs93(
                    Sl[idx], Kl[idx], Tl[idx], rl[idx], b[idx], vl[idx]
                )
            res[c_mask] = np.maximum(amer, euro)

        # Puts: transform to a call.
        p_mask = ~call_l
        if p_mask.any():
            euro = black_scholes(Sl[p_mask], Kl[p_mask], Tl[p_mask], rl[p_mask], ql[p_mask], vl[p_mask], False)
            r_t = rl[p_mask] - b[p_mask]  # == q
            b_t = -b[p_mask]
            needs_amer = b_t < r_t
            amer = euro.copy()
            if needs_amer.any():
                idx = np.where(p_mask)[0][needs_amer]
                amer[needs_amer] = _american_call_bs93(
                    Kl[idx], Sl[idx], Tl[idx], (rl - b)[idx], (-b)[idx], vl[idx]
                )
            res[p_mask] = np.maximum(amer, euro)

    res = np.where(np.isfinite(res), res, 0.0)
    intrinsic_live = np.where(call_l, np.maximum(Sl - Kl, 0.0), np.maximum(Kl - Sl, 0.0))
    out[live] = np.maximum(res, intrinsic_live)
    return out
