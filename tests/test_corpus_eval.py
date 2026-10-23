"""Tests for the measuring instrument itself (#27, PR #31 review).

The harness decides whether a future change to inference helped or hurt. It shipped
with no tests, and reviewing it found two defects that would have hidden exactly the
failures it exists to catch:

* a scorer proposing **nothing** reported ``precision=1.000``;
* a schema declaring **no** foreign keys reported ``recall=1.000`` while the scorer
  proposed keys that were in no truth set.

Both came from the same reflex — treating ``x/0`` as a perfect score. An undefined
ratio has to read as undefined, or the instrument flatters the worst case.

These tests use hand-built cases where the right answer is obvious by inspection, so
they check the scorer rather than the thing being scored.
"""

from __future__ import annotations

from relational_schema_analyzer.types import Column, PhysicalSchema, Table

from tests.corpus_eval import (
    Score,
    score_primary_keys,
    declared_refs,
    score_fk_inference,
    total,
    without_any_keys,
    without_foreign_keys,
)


def _col(name: str, data_type: str = "integer", nullable: bool = True) -> Column:
    return Column(name=name, data_type=data_type, is_nullable=nullable)


def _schema_with_one_declared_fk() -> PhysicalSchema:
    from relational_schema_analyzer.types import ForeignKey

    return PhysicalSchema(
        tables={
            "customer": Table(
                name="customer",
                columns=[_col("customer_id", nullable=False)],
                primary_key=["customer_id"],
            ),
            "orders": Table(
                name="orders",
                columns=[_col("order_id", nullable=False), _col("customer_id")],
                primary_key=["order_id"],
                foreign_keys=[
                    ForeignKey(
                        columns=["customer_id"],
                        foreign_table="customer",
                        foreign_columns=["customer_id"],
                    )
                ],
            ),
        }
    )


class TestAnUndefinedRatioReadsAsUndefined:
    """The two defects the review found, pinned so they cannot come back."""

    def test_proposing_nothing_is_not_perfect_precision(self):
        s = Score(corpus="x", expected=10, proposed=0, correct=0)
        assert s.precision is None, (
            "inference found none of ten real keys; reporting precision 1.000 would "
            "make the largest possible regression look half-right"
        )
        assert s.recall == 0.0
        assert s.f1 is None

    def test_a_schema_with_no_declared_keys_is_not_perfect_recall(self):
        s = Score(corpus="y", expected=0, proposed=3, correct=0)
        assert s.recall is None, (
            "nothing was declared, so there is no answer to be measured against — and "
            "a constraint-free schema is RSA's target domain, not an edge case"
        )
        assert s.precision == 0.0

    def test_undefined_renders_as_n_a_not_as_a_number(self):
        line = Score(corpus="z", expected=0, proposed=0, correct=0).line()
        assert "n/a" in line
        assert "1.000" not in line


class TestTheScorerCountsCorrectly:
    def test_everything_right(self):
        s = Score(corpus="a", expected=4, proposed=4, correct=4)
        assert s.precision == 1.0 and s.recall == 1.0 and s.f1 == 1.0

    def test_everything_wrong(self):
        s = Score(corpus="b", expected=4, proposed=4, correct=0)
        assert s.precision == 0.0 and s.recall == 0.0
        assert s.f1 is None, "both zero: F1 is undefined, not zero"

    def test_half_right(self):
        s = Score(corpus="c", expected=4, proposed=2, correct=2)
        assert s.precision == 1.0
        assert s.recall == 0.5

    def test_pooling_adds_counts_rather_than_averaging_rates(self):
        """A three-table corpus must not weigh as much as a thirty-table one."""
        small = Score(corpus="small", expected=1, proposed=1, correct=1)
        big = Score(corpus="big", expected=99, proposed=99, correct=0)
        pooled = total([small, big])
        assert pooled.expected == 100 and pooled.correct == 1
        assert pooled.recall == 0.01, "averaging the two rates would give 0.5"


class TestStripping:
    def test_declared_refs_reads_the_truth_from_the_schema(self):
        assert declared_refs(_schema_with_one_declared_fk()) == {
            ("orders", ("customer_id",), "customer", ("customer_id",))
        }

    def test_without_foreign_keys_removes_them_and_keeps_the_primary_keys(self):
        stripped = without_foreign_keys(_schema_with_one_declared_fk())
        assert declared_refs(stripped) == set()
        assert stripped.tables["customer"].primary_key == ["customer_id"], (
            "the primary keys are the targets inference needs; removing them is a "
            "different and harder question"
        )

    def test_without_any_keys_removes_the_targets_too(self):
        stripped = without_any_keys(_schema_with_one_declared_fk())
        assert declared_refs(stripped) == set()
        assert all(not t.primary_key for t in stripped.tables.values())
        assert all(not t.unique_constraints for t in stripped.tables.values())

    def test_stripping_does_not_mutate_the_original(self):
        """The truth is read from the same object that gets stripped."""
        schema = _schema_with_one_declared_fk()
        without_any_keys(schema)
        assert declared_refs(schema), "the ground truth was destroyed by measuring it"
        assert schema.tables["customer"].primary_key == ["customer_id"]


class TestEndToEnd:
    def test_a_recoverable_key_scores_perfectly(self):
        """`orders.customer_id` names `customer`'s key, so inference must find it."""
        s = score_fk_inference(_schema_with_one_declared_fk(), corpus="t")
        assert s.expected == 1 and s.correct == 1
        assert s.precision == 1.0 and s.recall == 1.0
        assert not s.missed and not s.spurious

    def test_a_key_no_name_rule_can_reach_is_reported_as_missed(self):
        """Not a scorer bug — the measurement working. See #26."""
        from relational_schema_analyzer.types import ForeignKey

        schema = PhysicalSchema(
            tables={
                "language": Table(
                    name="language",
                    columns=[_col("language_id", nullable=False)],
                    primary_key=["language_id"],
                ),
                "film": Table(
                    name="film",
                    columns=[_col("film_id", nullable=False), _col("original_lang_ref")],
                    primary_key=["film_id"],
                    foreign_keys=[
                        ForeignKey(
                            columns=["original_lang_ref"],
                            foreign_table="language",
                            foreign_columns=["language_id"],
                        )
                    ],
                ),
            }
        )
        s = score_fk_inference(schema, corpus="t")
        assert s.expected == 1 and s.correct == 0
        assert s.missed == {("film", ("original_lang_ref",), "language", ("language_id",))}


class TestPrimaryKeyScoring:
    """`score_primary_keys` was shipped unexercised; this is the cover for it."""

    class _Profile:
        """The shape `KeyProfile` presents: ranked candidates, best-first."""

        def __init__(self, best_by_table):
            self.candidates = {t: [object()] for t in best_by_table}
            self._best = best_by_table

        def best(self, table):
            cols = self._best[table]
            return type("C", (), {"columns": cols})() if cols else None

    def test_a_rediscovered_primary_key_scores_correct(self):
        schema = _schema_with_one_declared_fk()  # customer PK, orders PK
        profile = self._Profile({"customer": ("customer_id",), "orders": ("order_id",)})
        s = score_primary_keys(schema, profile, corpus="t")
        assert s.expected == 2 and s.correct == 2
        assert s.precision == 1.0 and s.recall == 1.0

    def test_proposing_the_wrong_column_is_counted_wrong(self):
        schema = _schema_with_one_declared_fk()
        profile = self._Profile({"customer": ("customer_id",), "orders": ("customer_id",)})
        s = score_primary_keys(schema, profile, corpus="t")
        assert s.correct == 1
        assert ("orders", ("order_id",)) in s.missed
        assert ("orders", ("customer_id",)) in s.spurious

    def test_a_table_declaring_no_primary_key_is_not_counted_against(self):
        """No declared key means no answer — it cannot be got right or wrong."""
        schema = _schema_with_one_declared_fk()
        schema.tables["orders"].primary_key = []
        profile = self._Profile({"customer": ("customer_id",)})
        s = score_primary_keys(schema, profile, corpus="t")
        assert s.expected == 1, "only `customer` declares a key"


class TestDecliningIsNotFailing:
    """"Could not check" must not score as "got it wrong" (#27).

    Pagila loaded without its data reported **recall 0.000 across 22 tables** that
    the profiler had correctly reported as "empty table". The engine was being
    honest and the harness punished it for it — the exact category error this
    codebase refuses everywhere else.
    """

    class _Profile:
        def __init__(self, best_by_table, not_evaluated=None):
            self.candidates = {t: [object()] for t in best_by_table}
            self._best = best_by_table
            self.not_evaluated = not_evaluated or {}

        def best(self, table):
            cols = self._best.get(table)
            return type("C", (), {"columns": cols})() if cols else None

    def test_a_declined_table_is_not_counted_as_missed(self):
        schema = _schema_with_one_declared_fk()  # customer + orders, both keyed
        profile = self._Profile(
            {"customer": ("customer_id",)}, not_evaluated={"orders": "empty table"}
        )
        s = score_primary_keys(schema, profile, corpus="t")
        assert s.declined == {"orders": "empty table"}
        assert not any(m[0] == "orders" for m in s.missed), (
            "the profiler declined to judge `orders`; calling that a miss scores it "
            "wrong for refusing to guess"
        )

    def test_recall_is_measured_over_what_was_attempted(self):
        schema = _schema_with_one_declared_fk()
        profile = self._Profile(
            {"customer": ("customer_id",)}, not_evaluated={"orders": "empty table"}
        )
        s = score_primary_keys(schema, profile, corpus="t")
        assert s.expected == 2, "both tables declare a key"
        assert s.evaluated == 1, "but only one was attempted"
        assert s.recall == 1.0, "and that one was found"

    def test_declining_everything_is_unmeasurable_not_zero(self):
        schema = _schema_with_one_declared_fk()
        profile = self._Profile(
            {}, not_evaluated={"customer": "empty table", "orders": "empty table"}
        )
        s = score_primary_keys(schema, profile, corpus="t")
        assert s.evaluated == 0
        assert s.recall is None, "nothing was attempted, so there is no recall to report"
        assert "declined=  2" in s.line()

    def test_a_wrong_answer_is_still_wrong(self):
        """The guard must not become an excuse — only declining is exempt."""
        schema = _schema_with_one_declared_fk()
        profile = self._Profile({"orders": ("customer_id",)})  # wrong column
        s = score_primary_keys(schema, profile, corpus="t")
        assert ("orders", ("order_id",)) in s.missed
        assert not s.declined
