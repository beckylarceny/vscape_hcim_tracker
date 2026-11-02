"""Human-readable durations and counts shared by the reports."""

from __future__ import annotations


def plural(count: int, unit: str) -> str:
    return f"{count} {unit}" + ("s" if count != 1 else "")


def years_months(seconds: int) -> str:
    days = seconds // 86400
    years, remainder = divmod(days, 365)
    months = remainder // 30
    years += months // 12
    months %= 12
    parts: list[str] = []
    if years:
        parts.append(plural(years, "year"))
    if months:
        parts.append(plural(months, "month"))
    if not parts:
        return plural(days, "day")
    return " ".join(parts)


def hours(seconds: int) -> str:
    value = seconds / 3600
    return f"{value:g} {'hour' if value == 1 else 'hours'}"


def bucket_count(lookback: int) -> int:
    """One coverage bucket per day, clamped to a readable bar."""
    return max(30, min(300, (lookback + 86399) // 86400))


def frequency_name(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60} minute"
    return f"{seconds // 3600:g} hour"


def players_line(player_count: int, coverage_total: int) -> str:
    if coverage_total > player_count:
        return f"{player_count} / {coverage_total}"
    return str(player_count)
