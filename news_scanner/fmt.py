"""Zahlen im deutschen Format."""

from __future__ import annotations


def de(x: float | None, nd: int = 2) -> str:
    """1234.5 -> '1.234,50'."""
    if x is None:
        return "–"
    return f"{x:,.{nd}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def sde(x: float, nd: int = 1) -> str:
    """Mit Vorzeichen: 1.5 -> '+1,5'."""
    return ("+" if x >= 0 else "") + de(x, nd)
