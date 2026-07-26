"""Errors. Exit-code contract: 0 no diff, 1 diff over policy, 2 operational error."""


class DatadifferError(Exception):
    """Operational error (exit 2): bad input, missing table, unusable key."""


class DiffTimeout(DatadifferError):
    """The diff exceeded its time budget; the query was interrupted."""
