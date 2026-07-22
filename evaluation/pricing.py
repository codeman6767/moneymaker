"""Executable-price, fee, slippage and reserve math.

Pure functions over an order-book view. Key rule (``CLAUDE.md`` / requirements):
we only ever fill up to the approved limit price -- :func:`walk_ladder` stops at
the limit and never sweeps beyond it, so slippage is bounded by construction.

All prices are integer cents in [1, 99]; a contract settles at 100 cents. Fair
value for a side is its model probability times 100.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

Ladder = Sequence[Tuple[int, int]]  # ascending (price_cents, size)


@dataclass(frozen=True)
class FeeModel:
    """Kalshi-style trading fee: coeff * contracts * p * (1 - p)."""

    coeff: float = 0.07

    def per_contract_cents(self, price_cents: float) -> float:
        p = price_cents / 100.0
        return self.coeff * p * (1.0 - p) * 100.0

    def total_cents(self, price_cents: float, contracts: int) -> float:
        # Kalshi rounds fees up to the next cent.
        return math.ceil(self.per_contract_cents(price_cents) * contracts)


def walk_ladder(ladder: Ladder, size: int, limit_price: int) -> Tuple[int, Optional[float]]:
    """Fill ``size`` contracts against ``ladder``, never above ``limit_price``.

    Returns ``(filled, avg_price_cents)``. ``avg_price_cents`` is ``None`` when
    nothing could be filled within the limit.
    """

    filled = 0
    cost = 0
    for price, avail in ladder:
        if price > limit_price:
            break  # do not sweep beyond the approved limit
        take = min(avail, size - filled)
        if take <= 0:
            break
        filled += take
        cost += take * price
        if filled >= size:
            break
    if filled == 0:
        return 0, None
    return filled, cost / filled


@dataclass(frozen=True)
class SideQuote:
    side: str
    fair_value_cents: float
    best_ask: Optional[int]
    limit_price: Optional[int]
    filled: int
    avg_fill_cents: Optional[float]
    slippage_cents: float
    fee_per_contract_cents: float
    uncertainty_reserve_cents: float
    adverse_reserve_cents: float
    edge_cents: float  # per-contract expected edge after all costs/reserves

    @property
    def tradeable(self) -> bool:
        return self.filled > 0 and self.avg_fill_cents is not None


def quote_side(
    *,
    side: str,
    ladder: Ladder,
    fair_prob: float,
    size: int,
    max_slip_ticks: int,
    fee_model: FeeModel,
    uncertainty_reserve_cents: float,
    adverse_reserve_cents: float,
) -> SideQuote:
    fair = fair_prob * 100.0
    if not ladder:
        return SideQuote(side, fair, None, None, 0, None, 0.0, 0.0,
                         uncertainty_reserve_cents, adverse_reserve_cents, edge_cents=-100.0)
    best_ask = ladder[0][0]
    limit_price = best_ask + max_slip_ticks  # the approved ceiling
    filled, avg = walk_ladder(ladder, size, limit_price)
    if filled == 0 or avg is None:
        return SideQuote(side, fair, best_ask, limit_price, 0, None, 0.0,
                         fee_model.per_contract_cents(best_ask),
                         uncertainty_reserve_cents, adverse_reserve_cents, edge_cents=-100.0)
    slippage = avg - best_ask
    fee_pc = fee_model.per_contract_cents(avg)
    edge = fair - avg - fee_pc - uncertainty_reserve_cents - adverse_reserve_cents
    return SideQuote(
        side=side,
        fair_value_cents=fair,
        best_ask=best_ask,
        limit_price=limit_price,
        filled=filled,
        avg_fill_cents=avg,
        slippage_cents=slippage,
        fee_per_contract_cents=fee_pc,
        uncertainty_reserve_cents=uncertainty_reserve_cents,
        adverse_reserve_cents=adverse_reserve_cents,
        edge_cents=edge,
    )


def uncertainty_reserve_cents(uncertainty_std: float, coeff: float) -> float:
    """Reserve that widens with model uncertainty (std is in probability units,
    converted to cents)."""

    return coeff * uncertainty_std * 100.0


def adverse_reserve_cents(provider_lag_ns: Optional[int], coeff: float) -> float:
    """Reserve that widens with data staleness (protects against being picked
    off on stale prices/state)."""

    if provider_lag_ns is None:
        return 0.0
    return coeff * (provider_lag_ns / 1_000_000_000.0)
