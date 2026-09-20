"""Shared numeric input validation, used across dc/ac/location/weather models."""

import math


def number_in_range(name: str, value: float, low: float, high: float) -> None:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not low <= value <= high):
        raise ValueError(f"{name} must be a finite number between {low} and {high}")
