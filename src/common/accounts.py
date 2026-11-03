"""Account type filters shared by collection and reporting."""

from __future__ import annotations

# Query value accepted by the highscores API for each named filter.
ACCOUNT_FILTER_CODES: dict[str, int] = {
    "NOFILTER": 0,
    "ALL_IRONMAN": 1,
    "HARDCORE_IRONMAN": 2,
    "ULTIMATE_IRONMAN": 3,
}

# Account codes returned in each highscores row for a named filter.
ACCOUNT_FILTER_MEMBERS: dict[str, frozenset[int]] = {
    "NOFILTER": frozenset({0, 1, 2, 3}),
    "ALL_IRONMAN": frozenset({1, 3}),
    "HARDCORE_IRONMAN": frozenset({2}),
    "ULTIMATE_IRONMAN": frozenset({3}),
}

DEFAULT_ACCOUNT_TYPES: tuple[str, ...] = ("HARDCORE_IRONMAN",)


def parse_account_types(names) -> tuple[str, ...]:
    """Validated account filter names, in configured order, without duplicates."""
    result: list[str] = []
    for value in names:
        name = str(value).strip()
        if not name:
            continue
        if name not in ACCOUNT_FILTER_MEMBERS:
            valid = ", ".join(ACCOUNT_FILTER_MEMBERS)
            raise ValueError(
                f"unknown account type {name!r}; expected one of {valid}"
            )
        if name not in result:
            result.append(name)
    return tuple(result) or DEFAULT_ACCOUNT_TYPES


def tracked_codes(names) -> frozenset[int]:
    """Every account code accepted by the configured filters."""
    codes: set[int] = set()
    for name in parse_account_types(names):
        codes.update(ACCOUNT_FILTER_MEMBERS[name])
    return frozenset(codes)
