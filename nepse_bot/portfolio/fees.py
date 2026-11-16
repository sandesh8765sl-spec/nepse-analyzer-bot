"""NEPSE trading costs and tax, read from config/market.yaml.

Every profit or loss the bot shows goes through this module, so the figures
are always after broker commission, SEBON fee, DP charge and capital gains tax.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from nepse_bot.settings import MARKET_CONFIG_PATH, load_yaml


@dataclass(frozen=True)
class FeeSchedule:
    """The cost rules in force."""

    commission_tiers: tuple[tuple[float | None, float], ...]  # (up to amount, percent)
    commission_minimum: float
    sebon_percent: float
    dp_charge: float
    cgt_short_percent: float
    cgt_long_percent: float
    cgt_long_after_days: int
    board_lot: int

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "FeeSchedule":
        commission = config.get("broker_commission") or {}
        tiers = tuple(
            (float(tier["up_to"]) if tier.get("up_to") is not None else None, float(tier["percent"]))
            for tier in commission.get("tiers") or []
        )
        tax = config.get("capital_gains_tax") or {}
        return cls(
            commission_tiers=tiers or ((None, 0.36),),
            commission_minimum=float(commission.get("minimum_rupees", 10)),
            sebon_percent=float(config.get("sebon_fee_percent", 0.015)),
            dp_charge=float(config.get("dp_charge_rupees", 25)),
            cgt_short_percent=float(tax.get("short_term_percent", 5)),
            cgt_long_percent=float(tax.get("long_term_percent", 3.75)),
            cgt_long_after_days=int(tax.get("long_term_after_days", 365)),
            board_lot=int(config.get("board_lot", 10)),
        )

    # ---------------------------------------------------------------- pieces
    def commission(self, amount: float) -> float:
        """Broker commission on one trade of this rupee amount."""
        if amount <= 0:
            return 0.0
        percent = self.commission_tiers[-1][1]
        for up_to, tier_percent in self.commission_tiers:
            if up_to is None or amount <= up_to:
                percent = tier_percent
                break
        return max(self.commission_minimum, amount * percent / 100.0)

    def sebon_fee(self, amount: float) -> float:
        """SEBON fee on one trade."""
        return max(0.0, amount) * self.sebon_percent / 100.0

    def buy_cost(self, quantity: float, price: float) -> float:
        """Total rupees paid to buy, including commission and SEBON fee."""
        amount = quantity * price
        return amount + self.commission(amount) + self.sebon_fee(amount)

    def cgt_percent(self, holding_days: int) -> float:
        """Capital gains tax rate for a holding period."""
        return self.cgt_long_percent if holding_days > self.cgt_long_after_days else self.cgt_short_percent

    def round_lot(self, quantity: float) -> int:
        """Round a share quantity down to a whole board lot."""
        lot = max(1, self.board_lot)
        return int(max(0.0, quantity) // lot) * lot

    # ------------------------------------------------------------ round trip
    def round_trip(self, quantity: float, buy_price: float, sell_price: float, holding_days: int = 30) -> "TradeResult":
        """Work out the full result of buying and later selling."""
        buy_amount = quantity * buy_price
        sell_amount = quantity * sell_price
        buy_commission = self.commission(buy_amount)
        sell_commission = self.commission(sell_amount)
        buy_sebon = self.sebon_fee(buy_amount)
        sell_sebon = self.sebon_fee(sell_amount)
        dp_charge = self.dp_charge if quantity > 0 else 0.0
        # Tax is charged on the gain after buy and sell commissions and SEBON fees.
        taxable_gain = (sell_amount - sell_commission - sell_sebon) - (buy_amount + buy_commission + buy_sebon)
        tax = max(0.0, taxable_gain) * self.cgt_percent(holding_days) / 100.0
        costs = buy_commission + sell_commission + buy_sebon + sell_sebon + dp_charge
        return TradeResult(
            quantity=quantity,
            buy_amount=buy_amount,
            sell_amount=sell_amount,
            costs=costs,
            tax=tax,
            gross_profit=sell_amount - buy_amount,
            net_profit=sell_amount - buy_amount - costs - tax,
        )

    def round_trip_cost_percent(self, quantity: float, price: float) -> float:
        """Buy-and-sell costs (no tax) as a percentage of the position, at one price."""
        if quantity <= 0 or price <= 0:
            return 0.0
        return 100.0 * self.round_trip(quantity, price, price).costs / (quantity * price)


@dataclass(frozen=True)
class TradeResult:
    """Outcome of one buy-then-sell."""

    quantity: float
    buy_amount: float
    sell_amount: float
    costs: float
    tax: float
    gross_profit: float
    net_profit: float

    @property
    def net_percent(self) -> float:
        return 100.0 * self.net_profit / self.buy_amount if self.buy_amount else 0.0


@lru_cache(maxsize=1)
def fee_schedule() -> FeeSchedule:
    """Return the cost rules from config/market.yaml."""
    return FeeSchedule.from_config(load_yaml(MARKET_CONFIG_PATH))
