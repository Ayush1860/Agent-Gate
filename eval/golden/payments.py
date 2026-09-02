"""Payment maths: money in integer minor units, never floats."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

TAX_RATES = {"GB": Decimal("0.20"), "DE": Decimal("0.19"), "US": Decimal("0.00")}
IDEMPOTENCY_KEY_BYTES = 32


class PaymentError(Exception):
    """Raised for any amount that must not be charged."""


@dataclass(frozen=True)
class LineItem:
    sku: str
    unit_price_minor: int
    quantity: int

    def subtotal_minor(self) -> int:
        if self.quantity < 0:
            raise PaymentError(f"negative quantity for {self.sku}")
        return self.unit_price_minor * self.quantity


def _round_minor(value: Decimal) -> int:
    """Round half-up to whole minor units. Bankers' rounding would lose money."""
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def subtotal(items: list[LineItem]) -> int:
    return sum(item.subtotal_minor() for item in items)


def tax_minor(subtotal_minor: int, country: str) -> int:
    rate = TAX_RATES.get(country)
    if rate is None:
        raise PaymentError(f"no tax rate configured for {country!r}")
    return _round_minor(Decimal(subtotal_minor) * rate)


def apply_discount(subtotal_minor: int, percent: Decimal) -> int:
    """Discounts are clamped to 0-100% so a bad coupon cannot invert a charge."""
    if percent < 0 or percent > 100:
        raise PaymentError("discount percentage must be between 0 and 100")
    reduction = _round_minor(Decimal(subtotal_minor) * percent / Decimal(100))
    return subtotal_minor - reduction


def total_minor(items: list[LineItem], country: str, discount_percent: Decimal) -> int:
    net = apply_discount(subtotal(items), discount_percent)
    return net + tax_minor(net, country)


def refund_minor(charged_minor: int, requested_minor: int) -> int:
    """A refund can never exceed what was charged, and can never be negative."""
    if requested_minor < 0:
        raise PaymentError("refund amount cannot be negative")
    if requested_minor > charged_minor:
        raise PaymentError("refund exceeds the amount charged")
    return charged_minor - requested_minor


def idempotency_key() -> str:
    """Unguessable, so a replayed request cannot collide with a real one."""
    return secrets.token_hex(IDEMPOTENCY_KEY_BYTES)
