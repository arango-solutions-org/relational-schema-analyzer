"""Each place key profiling can stop early records its own "not evaluated" reason.

Both the keyless pass and the additional-key pass share one screen, confirm and score
loop (#60). A reviewer reads these reasons to decide what to do next ("the table is
empty" and "the budget ran out" call for different actions), so sharing the loop must
not merge them. Offline: a scripted probe stands in for an engine.
"""

from __future__ import annotations

from typing import Optional, Sequence

import pytest

from relational_schema_analyzer.key_profiling import ColumnStats, profile_primary_keys
from relational_schema_analyzer.types import Column, PhysicalSchema, Table


class _Probe:
    """Unique, non-null data for every column; declines the call numbers in ``decline``."""

    def __init__(self, rows: int, *, limit: int = 100, decline: Sequence[int] = ()) -> None:
        self.rows, self.limit, self.decline, self.calls = rows, limit, set(decline), 0

    def column_stats(
        self, table: str, columns: Sequence[str], *, sample: bool
    ) -> Optional[dict[str, ColumnStats]]:
        self.calls += 1
        if self.calls in self.decline:
            return None
        n = min(self.rows, self.limit) if sample else self.rows
        return {c: (n, n, n) for c in columns}

    def combo_stats(
        self, table: str, columns: Sequence[str], *, sample: bool
    ) -> Optional[ColumnStats]:
        return None


def _schema(table: Table) -> PhysicalSchema:
    return PhysicalSchema(tables={table.name: table})


_KEYLESS = Table(name="orders", columns=[Column(name="order_id", data_type="integer")])
_KEYED = Table(
    name="accounts",
    columns=[
        Column(name="id", data_type="integer"),
        Column(name="account_id", data_type="integer"),
    ],
    primary_key=["id"],
)


@pytest.mark.parametrize(
    ("rows", "decline", "reason"),
    [
        (500, [1], "probe declined (budget spent or query failed)"),
        (0, [], "empty table"),
        (500, [2], "probe declined during confirmation"),
    ],
)
def test_a_keyless_table_says_where_it_stopped(rows, decline, reason):
    profile = profile_primary_keys(_schema(_KEYLESS), _Probe(rows, decline=decline))
    assert profile.not_evaluated == {"orders": reason}
    assert "orders" not in profile.candidates


@pytest.mark.parametrize(
    ("decline", "reason"),
    [
        ([1], "probe declined while looking for additional keys"),
        ([2], "probe declined confirming an additional key"),
    ],
)
def test_a_keyed_table_says_where_the_additional_key_search_stopped(decline, reason):
    profile = profile_primary_keys(_schema(_KEYED), _Probe(500, decline=decline))
    assert profile.not_evaluated == {"accounts": reason}
    assert profile.additional == {}


def test_an_empty_keyed_table_is_not_reported_as_undecided():
    # An empty table has no additional key to find, and it already has its primary key.
    profile = profile_primary_keys(_schema(_KEYED), _Probe(0))
    assert profile.not_evaluated == {}
    assert profile.additional == {}
    assert profile.declared == ["accounts"]


def test_an_additional_key_carries_both_its_score_reasons_and_the_note():
    profile = profile_primary_keys(_schema(_KEYED), _Probe(500))
    (found,) = profile.additional["accounts"]
    assert found.columns == ("account_id",)
    assert found.reasons[0] == "non-null and unique over every row"
    assert found.reasons[-1].startswith("the table already declares a primary key")
