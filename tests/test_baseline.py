from __future__ import annotations

from relational_schema_analyzer.baseline import infer_baseline
from relational_schema_analyzer.types import Column, ForeignKey, PhysicalSchema, Table


def _col(name, data_type="integer", nullable=False, pk=False):
    return Column(name=name, data_type=data_type, is_nullable=nullable, is_primary_key=pk)


def _schema(*tables: Table) -> PhysicalSchema:
    return PhysicalSchema(tables={t.name: t for t in tables})


def _rel_by_type(result, rel_type):
    return next(r for r in result["conceptualSchema"]["relationships"] if r["type"] == rel_type)


# ── Entities & properties ───────────────────────────────────────────


class TestEntities:
    def test_table_becomes_entity_with_pascal_name(self):
        users = Table(name="users", columns=[_col("id", pk=True), _col("name", "text")],
                      primary_key=["id"])
        result = infer_baseline(_schema(users))
        entities = result["conceptualSchema"]["entities"]
        assert [e["name"] for e in entities] == ["Users"]
        assert entities[0]["labels"] == ["Users"]
        assert entities[0]["source"] == "baseline"

    def test_columns_become_typed_properties(self):
        users = Table(
            name="users",
            columns=[_col("id", pk=True), _col("email", "varchar", nullable=True)],
            primary_key=["id"],
        )
        result = infer_baseline(_schema(users))
        props = {p["name"]: p for p in result["conceptualSchema"]["entities"][0]["properties"]}
        assert props["id"]["type"] == "integer"
        assert props["id"]["unique"] is True
        assert props["id"]["indexed"] is True
        assert props["email"]["type"] == "string"
        assert props["email"]["nullable"] is True
        assert "unique" not in props["email"]

    def test_physical_mapping_records_table_back_reference(self):
        users = Table(name="users", columns=[_col("id", pk=True)], primary_key=["id"])
        result = infer_baseline(_schema(users))
        em = result["physicalMapping"]["entities"]["Users"]
        assert em["style"] == "TABLE"
        assert em["tableName"] == "users"
        assert em["primaryKey"] == ["id"]
        assert em["properties"]["id"]["field"] == "id"
        assert em["properties"]["id"]["sqlType"] == "integer"

    def test_missing_primary_key_flags_review(self):
        t = Table(name="logs", columns=[_col("msg", "text", nullable=True)])
        result = infer_baseline(_schema(t))
        assert result["reviewRequired"] is True
        assert "missing_primary_key" in result["detectedPatterns"]


# ── Foreign keys ────────────────────────────────────────────────────


class TestForeignKeys:
    def test_fk_becomes_foreign_key_relationship_1_to_n(self):
        users = Table(name="users", columns=[_col("id", pk=True)], primary_key=["id"])
        orders = Table(
            name="orders",
            columns=[_col("id", pk=True), _col("user_id")],
            primary_key=["id"],
            foreign_keys=[ForeignKey(column="user_id", foreign_table="users",
                                     foreign_column="id")],
        )
        result = infer_baseline(_schema(users, orders))
        rel = _rel_by_type(result, "Orders_Users")
        assert rel["fromEntity"] == "Orders"
        assert rel["toEntity"] == "Users"
        assert rel["cardinality"] == "1:N"
        pm = result["physicalMapping"]["relationships"]["Orders_Users"]
        assert pm["style"] == "FOREIGN_KEY"
        assert pm["fromTable"] == "orders"
        assert pm["fromColumns"] == ["user_id"]
        assert pm["toTable"] == "users"
        assert pm["toColumns"] == ["id"]

    def test_fk_equal_to_pk_is_one_to_one(self):
        users = Table(name="users", columns=[_col("id", pk=True)], primary_key=["id"])
        profiles = Table(
            name="profiles",
            columns=[_col("user_id", pk=True)],
            primary_key=["user_id"],
            foreign_keys=[ForeignKey(column="user_id", foreign_table="users",
                                     foreign_column="id")],
        )
        result = infer_baseline(_schema(users, profiles))
        # single-col PK that is itself the FK → inheritance candidate + 1:1
        rel = _rel_by_type(result, "Profiles_Users")
        assert rel["cardinality"] == "1:1"
        assert "inheritance_via_shared_pk" in result["detectedPatterns"]
        assert result["reviewRequired"] is True
        profile = next(
            e for e in result["conceptualSchema"]["entities"] if e["name"] == "Profiles"
        )
        assert profile["subClassOf"] == "Users"

    def test_a_key_referencing_its_own_table_is_not_inheritance(self):
        """``acct.id -> acct.id`` is a self-reference, not a superclass (#54)."""
        acct = Table(
            name="acct",
            columns=[_col("id", pk=True)],
            primary_key=["id"],
            foreign_keys=[ForeignKey(column="id", foreign_table="acct", foreign_column="id")],
        )
        result = infer_baseline(_schema(acct))
        (entity,) = result["conceptualSchema"]["entities"]
        assert "subClassOf" not in entity, "an entity cannot be its own superclass"
        assert "inheritance_via_shared_pk" not in result["detectedPatterns"]

    def test_two_fks_between_same_entities_get_distinct_types(self):
        users = Table(name="users", columns=[_col("id", pk=True)], primary_key=["id"])
        messages = Table(
            name="messages",
            columns=[_col("id", pk=True), _col("sender_id"), _col("recipient_id")],
            primary_key=["id"],
            foreign_keys=[
                ForeignKey(column="sender_id", foreign_table="users", foreign_column="id"),
                ForeignKey(column="recipient_id", foreign_table="users", foreign_column="id"),
            ],
        )
        result = infer_baseline(_schema(users, messages))
        types = {
            r["type"]
            for r in result["conceptualSchema"]["relationships"]
            if r["fromEntity"] == "Messages"
        }
        assert len(types) == 2


# ── Join tables ─────────────────────────────────────────────────────


class TestJoinTables:
    def test_join_table_becomes_n_to_m_relationship_not_entity(self):
        students = Table(name="students", columns=[_col("id", pk=True)], primary_key=["id"])
        courses = Table(name="courses", columns=[_col("id", pk=True)], primary_key=["id"])
        enrollments = Table(
            name="enrollments",
            columns=[_col("student_id", pk=True), _col("course_id", pk=True),
                     _col("grade", "text", nullable=True)],
            primary_key=["student_id", "course_id"],
            foreign_keys=[
                ForeignKey(column="student_id", foreign_table="students", foreign_column="id"),
                ForeignKey(column="course_id", foreign_table="courses", foreign_column="id"),
            ],
        )
        result = infer_baseline(_schema(students, courses, enrollments))
        entity_names = {e["name"] for e in result["conceptualSchema"]["entities"]}
        assert entity_names == {"Students", "Courses"}
        assert "join_table" in result["detectedPatterns"]
        rel = _rel_by_type(result, "Students_Courses")
        assert rel["cardinality"] == "N:M"
        assert [p["name"] for p in rel["properties"]] == ["grade"]
        pm = result["physicalMapping"]["relationships"]["Students_Courses"]
        assert pm["style"] == "JOIN_TABLE"
        assert pm["joinTable"] == "enrollments"
        assert pm["joinFromColumns"] == ["student_id"]
        assert pm["joinToColumns"] == ["course_id"]
        assert pm["attributeColumns"] == ["grade"]


# ── Inferred FKs (no declared constraints) ──────────────────────────


class TestInferredForeignKeys:
    def test_inference_runs_when_no_fks_declared(self):
        author = Table(name="author", columns=[_col("id", pk=True)], primary_key=["id"])
        book = Table(
            name="book",
            columns=[_col("id", pk=True), _col("author_id")],
            primary_key=["id"],
        )
        result = infer_baseline(_schema(author, book))
        assert "inferred_foreign_keys" in result["detectedPatterns"]
        assert result["reviewRequired"] is True
        rel = _rel_by_type(result, "Book_Author")
        assert rel["inferred"] is True
        assert 0.0 < rel["confidence"] <= 1.0
        pm = result["physicalMapping"]["relationships"]["Book_Author"]
        assert pm["inferred"] is True
        # Invariant: a missing `enforced` means the source vouches for the join,
        # so an inferred FK must say so explicitly.
        assert pm["enforced"] is False

    def test_no_inference_when_fks_declared(self):
        author = Table(name="author", columns=[_col("id", pk=True)], primary_key=["id"])
        book = Table(
            name="book",
            columns=[_col("id", pk=True), _col("author_id")],
            primary_key=["id"],
            foreign_keys=[ForeignKey(column="author_id", foreign_table="author",
                                     foreign_column="id")],
        )
        result = infer_baseline(_schema(author, book))
        assert "inferred_foreign_keys" not in result["detectedPatterns"]
        rel = _rel_by_type(result, "Book_Author")
        assert "inferred" not in rel

    def test_declared_fk_on_one_table_does_not_suppress_inference_on_another(self):
        # The gate is per table, not per schema: `book` declares its FK, `review`
        # declares nothing, so `review.book_id` is still inferred. Previously one
        # declared FK anywhere disabled inference for the whole schema.
        author = Table(name="author", columns=[_col("id", pk=True)], primary_key=["id"])
        book = Table(
            name="book",
            columns=[_col("id", pk=True), _col("author_id")],
            primary_key=["id"],
            foreign_keys=[ForeignKey(column="author_id", foreign_table="author",
                                     foreign_column="id")],
        )
        review = Table(
            name="review",
            columns=[_col("id", pk=True), _col("book_id")],
            primary_key=["id"],
        )
        result = infer_baseline(_schema(author, book, review))
        assert "inferred_foreign_keys" in result["detectedPatterns"]
        assert _rel_by_type(result, "Review_Book")["inferred"] is True
        # The authoritative FK is untouched.
        assert "inferred" not in _rel_by_type(result, "Book_Author")
        assert any("declared no foreign keys" in a for a in result["assumptions"])


# ── Schema qualification & join-table parent columns ────────────────


class TestPhysicalMappingCompleteness:
    """Both are prerequisites for building a valid R2RML mapping."""

    def test_entity_mapping_records_the_source_schema(self):
        users = Table(name="users", columns=[_col("id", pk=True)], primary_key=["id"],
                      schema_name="sales")
        result = infer_baseline(_schema(users))
        assert result["physicalMapping"]["entities"]["Users"]["schema"] == "sales"

    def test_schema_omitted_when_source_reports_none(self):
        users = Table(name="users", columns=[_col("id", pk=True)], primary_key=["id"])
        result = infer_baseline(_schema(users))
        assert "schema" not in result["physicalMapping"]["entities"]["Users"]

    def test_join_table_mapping_records_schema_and_parent_columns(self):
        # `basket.product_sku` references `products.sku` — a parent column that
        # is *not* named like the child, so it can't be reconstructed by guesswork.
        orders = Table(name="orders", columns=[_col("id", pk=True)], primary_key=["id"],
                       schema_name="sales")
        products = Table(name="products", columns=[_col("sku", "varchar", pk=True)],
                         primary_key=["sku"], schema_name="sales")
        basket = Table(
            name="basket",
            schema_name="sales",
            columns=[_col("order_id"), _col("product_sku", "varchar")],
            primary_key=["order_id", "product_sku"],
            foreign_keys=[
                ForeignKey(column="order_id", foreign_table="orders", foreign_column="id"),
                ForeignKey(column="product_sku", foreign_table="products",
                           foreign_column="sku"),
            ],
        )
        result = infer_baseline(_schema(orders, products, basket))
        jm = result["physicalMapping"]["relationships"]["Orders_Products"]
        assert jm["style"] == "JOIN_TABLE"
        assert jm["schema"] == "sales"
        assert jm["joinFromColumns"] == ["order_id"]
        assert jm["joinFromParentColumns"] == ["id"]
        assert jm["joinToColumns"] == ["product_sku"]
        assert jm["joinToParentColumns"] == ["sku"]


# ── Unenforced (lakehouse) foreign keys ─────────────────────────────


class TestUnenforcedForeignKeys:
    """Unity Catalog / Glue / Iceberg declare FKs but never validate them."""

    @staticmethod
    def _uc_fk(column, foreign_table, foreign_column="id"):
        return ForeignKey(
            column=column,
            foreign_table=foreign_table,
            foreign_column=foreign_column,
            enforced=False,
        )

    def test_unenforced_fk_still_becomes_a_relationship(self):
        users = Table(name="users", columns=[_col("id", pk=True)], primary_key=["id"])
        orders = Table(
            name="orders",
            columns=[_col("id", pk=True), _col("user_id")],
            primary_key=["id"],
            foreign_keys=[self._uc_fk("user_id", "users")],
        )
        result = infer_baseline(_schema(users, orders))
        rel = _rel_by_type(result, "Orders_Users")
        assert rel["cardinality"] == "1:N"
        assert "inferred" not in rel

    def test_unenforced_fk_is_flagged_in_mapping_and_patterns(self):
        users = Table(name="users", columns=[_col("id", pk=True)], primary_key=["id"])
        orders = Table(
            name="orders",
            columns=[_col("id", pk=True), _col("user_id")],
            primary_key=["id"],
            foreign_keys=[self._uc_fk("user_id", "users")],
        )
        result = infer_baseline(_schema(users, orders))
        assert "unenforced_foreign_keys" in result["detectedPatterns"]
        assert result["physicalMapping"]["relationships"]["Orders_Users"]["enforced"] is False

    def test_enforced_fk_carries_no_enforced_key(self):
        users = Table(name="users", columns=[_col("id", pk=True)], primary_key=["id"])
        orders = Table(
            name="orders",
            columns=[_col("id", pk=True), _col("user_id")],
            primary_key=["id"],
            foreign_keys=[ForeignKey(column="user_id", foreign_table="users",
                                     foreign_column="id")],
        )
        result = infer_baseline(_schema(users, orders))
        assert "unenforced_foreign_keys" not in result["detectedPatterns"]
        assert "enforced" not in result["physicalMapping"]["relationships"]["Orders_Users"]

    def test_unenforced_fk_does_not_suppress_inference_on_same_table(self):
        # The heart of the Databricks case: `orders` declares user_id but not
        # product_id. An unenforced declaration is a hint, so inference still
        # runs for the table — and never duplicates the column already declared.
        users = Table(name="users", columns=[_col("id", pk=True)], primary_key=["id"])
        products = Table(name="products", columns=[_col("id", pk=True)], primary_key=["id"])
        orders = Table(
            name="orders",
            columns=[_col("id", pk=True), _col("user_id"), _col("product_id")],
            primary_key=["id"],
            foreign_keys=[self._uc_fk("user_id", "users")],
        )
        result = infer_baseline(_schema(users, products, orders))
        assert _rel_by_type(result, "Orders_Products")["inferred"] is True
        assert "inferred" not in _rel_by_type(result, "Orders_Users")
        rels = result["conceptualSchema"]["relationships"]
        assert sum(1 for r in rels if r["toEntity"] == "Users") == 1
        assert any("informational only" in a for a in result["assumptions"])

    def test_review_is_not_forced_by_unenforced_fks_alone(self):
        # A fully-declared lakehouse schema shouldn't lose confidence purely for
        # its dialect — the pattern is informational, not a review trigger.
        users = Table(name="users", columns=[_col("id", pk=True)], primary_key=["id"])
        orders = Table(
            name="orders",
            columns=[_col("id", pk=True), _col("user_id")],
            primary_key=["id"],
            foreign_keys=[self._uc_fk("user_id", "users")],
        )
        result = infer_baseline(_schema(users, orders))
        assert result["reviewRequired"] is False
