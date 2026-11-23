"""Discriminator detection — single-table inheritance and the specialization half."""

import pytest

from relational_schema_analyzer.discriminator import (
    DiscriminatorOptions,
    detect_discriminators,
    looks_like_discriminator_name,
    specialization_parents,
)
from relational_schema_analyzer.types import CheckConstraint, Column, ForeignKey, Schema, Table

ACCOUNT_TYPES = ["mortgage", "checking", "savings", "insurance"]


def col(name, data_type="text", **kw):
    return Column(name=name, data_type=data_type, **kw)


def accounts_table(*, check=True, extra_columns=()):
    checks = []
    if check:
        checks.append(
            CheckConstraint(
                name="account_type_chk",
                expression="account_type IN ('mortgage','checking','savings','insurance')",
                columns=["account_type"],
                enum_values=list(ACCOUNT_TYPES),
            )
        )
    return Table(
        name="account",
        columns=[
            col("account_id", "integer", is_primary_key=True),
            col("name"),
            col("balance", "numeric"),
            col("account_type"),
            *extra_columns,
        ],
        primary_key=["account_id"],
        check_constraints=checks,
    )


def subtype_table(name, extra):
    """Class-table inheritance: the PK is also the FK to the parent."""
    return Table(
        name=name,
        columns=[col("account_id", "integer", is_primary_key=True), *extra],
        primary_key=["account_id"],
        foreign_keys=[
            ForeignKey(
                columns=["account_id"], foreign_table="account", foreign_columns=["account_id"]
            )
        ],
    )


def schema_of(*tables):
    return Schema(tables={t.name: t for t in tables})


# ── name affinity ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["type", "kind", "account_type", "asset_category", "sub_type"])
def test_recognises_discriminator_names(name):
    assert looks_like_discriminator_name(name)


@pytest.mark.parametrize("name", ["typography", "prototype_id", "typed_at", "category_id_ref"])
def test_rejects_lookalike_names(name):
    """Whole-name or trailing-word only — a stem buried mid-name is not a signal."""
    assert not looks_like_discriminator_name(name)


# ── declared path (no database) ──────────────────────────────────────────────


def test_check_constraint_is_a_declared_discriminator():
    found = detect_discriminators(schema_of(accounts_table()))
    assert len(found) == 1
    assert found[0].column == "account_type"
    assert found[0].values == sorted(ACCOUNT_TYPES)
    assert found[0].is_declared


def test_declared_path_needs_no_enumerator():
    """The whole point: a CHECK constraint costs nothing to read."""
    assert detect_discriminators(schema_of(accounts_table()), enumerator=None)


def test_declaration_alone_is_evidence_without_name_affinity():
    table = accounts_table(check=False)
    table.check_constraints = [
        CheckConstraint(
            name="c",
            expression="lifecycle IN ('a','b')",
            columns=["lifecycle"],
            enum_values=["a", "b"],
        )
    ]
    table.columns.append(col("lifecycle"))
    found = detect_discriminators(schema_of(table))
    assert [c.column for c in found] == ["lifecycle"]
    # ...but scored below a name-affine column, which is the stronger signal.
    assert found[0].confidence < 0.90


def test_key_columns_are_never_discriminators():
    table = accounts_table(check=False)
    table.check_constraints = [
        CheckConstraint(name="c", expression="", columns=["account_id"], enum_values=["1", "2"])
    ]
    assert detect_discriminators(schema_of(table)) == []


def test_foreign_key_columns_are_never_discriminators():
    table = subtype_table("mortgage_account", [col("principal", "numeric")])
    table.check_constraints = [
        CheckConstraint(name="c", expression="", columns=["account_id"], enum_values=["1", "2"])
    ]
    assert detect_discriminators(schema_of(table)) == []


def test_multi_column_check_is_not_a_discriminator():
    table = accounts_table(check=False)
    table.check_constraints = [
        CheckConstraint(name="c", expression="", columns=["a", "b"], enum_values=["x", "y"])
    ]
    assert detect_discriminators(schema_of(table)) == []


# ── acceptance gates (aligned with ASA's type_detection) ─────────────────────


def test_rejects_single_valued_enum():
    table = accounts_table(check=False)
    table.check_constraints = [
        CheckConstraint(name="c", expression="", columns=["account_type"], enum_values=["only"])
    ]
    assert detect_discriminators(schema_of(table)) == []


def test_rejects_high_cardinality_enum():
    table = accounts_table(check=False)
    table.check_constraints = [
        CheckConstraint(
            name="c",
            expression="",
            columns=["account_type"],
            enum_values=[f"v{i}" for i in range(200)],
        )
    ]
    assert detect_discriminators(schema_of(table)) == []


def test_rejects_free_text_values():
    """A label is short and token-shaped; a sentence is not."""
    table = accounts_table(check=False)
    table.check_constraints = [
        CheckConstraint(
            name="c",
            expression="",
            columns=["account_type"],
            enum_values=["a perfectly ordinary sentence", "another one entirely"],
        )
    ]
    assert detect_discriminators(schema_of(table)) == []


def test_cardinality_bounds_are_configurable():
    table = accounts_table(check=False)
    table.check_constraints = [
        CheckConstraint(
            name="c", expression="", columns=["account_type"], enum_values=["a", "b", "c"]
        )
    ]
    assert (
        detect_discriminators(schema_of(table), options=DiscriminatorOptions(max_distinct=2)) == []
    )
    assert detect_discriminators(schema_of(table), options=DiscriminatorOptions(max_distinct=3))


# ── sampled path (opt-in) ────────────────────────────────────────────────────


def test_sampling_requires_an_enumerator():
    assert detect_discriminators(schema_of(accounts_table(check=False))) == []


def test_sampled_discriminator_is_found_when_undeclared():
    def enumerate_values(table, column, limit):
        return list(ACCOUNT_TYPES) if column == "account_type" else None

    found = detect_discriminators(
        schema_of(accounts_table(check=False)), enumerator=enumerate_values
    )
    assert [(c.column, c.source) for c in found] == [("account_type", "sampled")]


def test_sampling_only_probes_name_affine_columns():
    """Probing every column of every table is cost with no signal behind it."""
    probed = []

    def enumerate_values(table, column, limit):
        probed.append(column)
        return ["a", "b"]

    detect_discriminators(schema_of(accounts_table(check=False)), enumerator=enumerate_values)
    assert probed == ["account_type"]


def test_declared_columns_are_not_re_sampled():
    probed = []

    def enumerate_values(table, column, limit):
        probed.append(column)
        return ["x", "y"]

    found = detect_discriminators(schema_of(accounts_table()), enumerator=enumerate_values)
    assert probed == []
    assert found[0].is_declared


def test_enumerator_failure_is_not_a_schema_fact():
    def boom(table, column, limit):
        raise RuntimeError("connection lost")

    assert detect_discriminators(schema_of(accounts_table(check=False)), enumerator=boom) == []


def test_declared_outranks_sampled():
    def enumerate_values(table, column, limit):
        return ["p", "q"]

    other = Table(
        name="asset",
        columns=[col("asset_id", "integer", is_primary_key=True), col("asset_kind")],
        primary_key=["asset_id"],
    )
    found = detect_discriminators(schema_of(accounts_table(), other), enumerator=enumerate_values)
    assert [c.source for c in found] == ["check_constraint", "sampled"]


def test_output_is_deterministic():
    schema = schema_of(accounts_table())
    assert detect_discriminators(schema) == detect_discriminators(schema)


# ── specialization corroboration ─────────────────────────────────────────────


def test_discriminator_on_a_shared_pk_parent_is_a_specialization():
    """The ER pattern: supertype table with a type column plus subtype tables."""
    schema = schema_of(
        accounts_table(),
        subtype_table("mortgage_account", [col("principal", "numeric")]),
        subtype_table("checking_account", [col("overdraft_limit", "numeric")]),
    )
    parents = specialization_parents(schema, detect_discriminators(schema))
    assert parents == {"account": ["checking_account", "mortgage_account"]}


def test_no_specialization_without_a_discriminator():
    """Shared-PK edges alone are plain class-table inheritance, not specialization."""
    schema = schema_of(
        accounts_table(check=False),
        subtype_table("mortgage_account", [col("principal", "numeric")]),
    )
    assert specialization_parents(schema, detect_discriminators(schema)) == {}


def test_a_parent_referencing_itself_is_not_its_own_subtype():
    """Its own key pointing at itself made ``account`` a child of ``account`` (#54)."""
    parent = accounts_table()
    parent.foreign_keys = [
        ForeignKey(columns=["account_id"], foreign_table="account", foreign_columns=["account_id"])
    ]
    schema = schema_of(parent, subtype_table("mortgage_account", [col("principal", "numeric")]))
    parents = specialization_parents(schema, detect_discriminators(schema))
    assert parents == {"account": ["mortgage_account"]}


def test_ordinary_foreign_key_is_not_a_subtype():
    """The PK must *be* the FK — a plain reference to a discriminated table is not."""
    child = Table(
        name="statement",
        columns=[col("statement_id", "integer", is_primary_key=True), col("account_id", "integer")],
        primary_key=["statement_id"],
        foreign_keys=[
            ForeignKey(
                columns=["account_id"], foreign_table="account", foreign_columns=["account_id"]
            )
        ],
    )
    schema = schema_of(accounts_table(), child)
    assert specialization_parents(schema, detect_discriminators(schema)) == {}
