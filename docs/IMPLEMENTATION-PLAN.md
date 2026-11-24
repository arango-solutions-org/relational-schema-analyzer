# Implementation Plan — relational-schema-analyzer

Status: Draft v0.1
Companion to [`DESIGN.md`](DESIGN.md).

This plan is **phased and low-risk**: Stage 1 is a mechanical extraction of already-tested
code from `r2g`; the new conceptual/OWL value is built in Stage 2+ where it benefits the
whole ecosystem rather than being trapped inside `r2g`.

---

## Guiding principles

- **Reverse the dependency.** `r2g` ends up depending on this library, not vice versa.
- **Baseline first.** The deterministic, no-LLM path must produce a complete, useful bundle.
  LLM is additive refinement only.
- **Contract compatibility.** Match `arango-schema-analyzer`'s tool-contract v1 wire shape so a
  single consumer (e.g. `arango-ontoextract`) handles relational and Arango sources.
- **No behavior change in r2g** during extraction — verified by r2g's existing test suite.

---

## Phase 0 — Repo bootstrap (this repo)

- [x] Create repo + git
- [x] `pyproject.toml` (hatchling, name `relational-schema-analyzer`, import
      `relational_schema_analyzer`, Python ≥ 3.10)
- [x] `relational_schema_analyzer/__init__.py` public API skeleton
- [x] `.gitignore`, license (Apache-2.0), CI stub (ruff + pytest)
- [x] `docs/tool-contract/v1/response.schema.json` (+ request schema, examples) copied/adapted
      from `arango-schema-analyzer/docs/tool-contract/v1/`
- [x] Create GitHub repo and push `main`

Optional extras layout (mirror Arango analyzer):
`[postgres] [mysql] [sqlserver] [snowflake] [openai] [anthropic] [openrouter] [mcp] [dev]`

---

## Phase 1 — Extract the physical core (mechanical, low risk)

Lift from `r2g` with minimal edits. **Extraction inventory** (source → destination):

| `r2g` source | New library destination | Notes |
| --- | --- | --- |
| `src/r2g/types.py` (`Schema`/`Table`/`Column`/`ForeignKey`) | `relational_schema_analyzer/types.py` (rename `Schema` → `PhysicalSchema`, alias kept) | drop `MappingConfig`/`CollectionMapping`/`EdgeDefinition` (those stay in r2g) |
| `src/r2g/connectors/base.py` | `connectors/base.py` | `SourceConnector` protocol + `create_connector` factory |
| `src/r2g/connectors/postgres.py` | `connectors/postgres.py` | incl. partition metadata rollup |
| `src/r2g/connectors/mysql.py` | `connectors/mysql.py` | |
| `src/r2g/connectors/mssql.py` | `connectors/mssql.py` | |
| `src/r2g/connectors/snowflake.py` | `connectors/snowflake.py` | |
| `src/r2g/connectors/csv_source.py` | `connectors/csv_source.py` | |
| `src/r2g/connectors/session.py` | `connectors/session.py` | bulk-read protocol (kept for r2g reuse) |
| `src/r2g/fk_inference.py` | `fk_inference.py` | name + value-overlap heuristics |
| `src/r2g/schema_diff.py` | `schema_diff.py` | reused for fingerprint/drift |
| `src/r2g/topo_sort.py` | `topo_sort.py` | |
| `src/r2g/config.py::pg_type_to_json_type`, `DEFAULT_TYPE_MAP`, `_is_likely_join_table` | `typemap.py` | the type map only (the join-table heuristic was ported as `heuristics.py`, never used, and removed in #60) |
| relevant `tests/` (`test_*_connector.py`, `test_fk_inference.py`, `test_schema_diff.py`, CSV) | `tests/` | port as-is |

**Stays in r2g** (not extracted): `config.py` (MappingConfig generation), `config_migrate.py`,
`transformers/`, `generators/`, `connectors/arango_reader.py`, `catalog.py`, `ui/`,
`mcp_server.py`, `main.py`.

Progress (extraction landed in this pass):
- [x] `types.py` (`PhysicalSchema` + `Schema` alias; `ForeignKey`/`Column`/`Table`)
- [x] `connectors/` (`base` + `create_connector` factory, `session`, `postgres`, `mysql`,
      `mssql`, `snowflake`, `csv_source`). Kafka **dropped** for v0 (decision §9.3).
- [x] `typemap.py` (`DEFAULT_TYPE_MAP` + `pg_type_to_json_type`), `heuristics.py`
      (`is_likely_join_table`; unused, removed in #60), `naming.py` (dependency-free subset), `dump_reader.py`
- [x] `schema_diff.py`, `topo_sort.py`
- [x] `fk_inference.py` — decoupled from the Arango `EdgeDefinition`: the
      `to_edge_definition()` helper is replaced by relational-native
      `InferredForeignKey.to_foreign_key() -> ForeignKey`; all four value samplers
      (Postgres/MySQL/SQL Server/CSV) + `create_value_sampler` ported
- [x] Ported tests pass (191): connectors (base/csv/mysql/mssql/snowflake), `schema_diff`,
      `topo_sort`, physical `types`, `fk_inference`; ruff clean
- [ ] `relational-schema-analyzer snapshot` CLI emits `physical.json` (CLI is Phase 3)

Exit criteria:
- [x] `create_connector(...)` builds a connector for PG/MySQL/MSSQL/Snowflake/CSV
      (URL-parsing + protocol conformance covered; live `get_schema()` needs a DB)
- [x] ported connector + fk-inference + diff tests pass
- [ ] `relational-schema-analyzer snapshot` CLI emits `physical.json`

---

## Phase 2 — Conceptual model + deterministic baseline (new value)

- [x] `conceptual.py` — `ConceptualSchema` (dict-based, shape-identical to the Arango
      analyzer for contract parity)
- [x] `mapping.py` — relational `PhysicalMapping` (`TABLE` / `FOREIGN_KEY` / `JOIN_TABLE`)
- [x] `baseline.py` — deterministic rules from DESIGN §4
  - [x] table → entity (PascalCase, matching Arango); column → datatype property
  - [x] FK → relationship (`1:1` when FK == local PK, else `1:N`)
  - [x] join table (2 FKs whose columns form the PK) → N:M relationship + association
        properties from the non-structural columns
  - [x] shared-PK-FK → `subClassOf` candidate (review-flagged)
  - [x] no declared FKs anywhere → name-based `fk_inference` fallback (relationships
        marked `inferred` + review-flagged)
- [x] `metadata.py` — confidence scoring, `reviewRequired`, `physicalSchemaFingerprint`
- [x] `analyzer.py` — `RelationalSchemaAnalyzer.analyze(physical) -> Analysis` (+ `to_bundle()`)
- [x] tests: baseline rules + analyzer + **contract conformance** (bundles validated against
      `response.schema.json` `$defs/AnalysisOutput`) + determinism
- [x] **Offline golden corpus** — the r2g CSV demo (`authors`/`books`/`members`/`loans`,
      copied to `tests/fixtures/csv_demo/`). Runs with **no DB/Docker** (CSV connector reads
      it directly) and exercises connector → baseline → inferred-FK end to end, with a
      committed golden bundle (`tests/fixtures/csv_demo_bundle.golden.json`). 215 tests, ruff clean.
- [ ] Golden bundles for Pagila / Chinook / Northwind (the **SQL-dump** corpora) — moved to
      Phase 5: these need a live DB to introspect (we don't parse DDL), so they belong with
      the Docker integration suite, gated behind `RUN_INTEGRATION`.

Exit criteria:
- [x] `analyze` produces a valid bundle conforming to `docs/tool-contract/v1/response.schema.json`
- [x] baseline runs with **no LLM** and flags ambiguous cases (`reviewRequired` +
      `detectedPatterns`: `join_table`, `inheritance_via_shared_pk`, `inferred_foreign_keys`,
      `missing_primary_key`)

---

## Phase 3 — Exports & tool contract

- [x] `exports.py` — `export_bundle()` (tool-contract JSON; accepts `Analysis` or dict).
      SQL-native physical "views" remain a future addition.
- [x] `owl_export.py` — Turtle + JSON-LD with `phys:*` annotations (DESIGN §5): `owl:Class`
      per entity (+ `rdfs:subClassOf`), `owl:DatatypeProperty` per column (PK/unique →
      `owl:FunctionalProperty` + `owl:InverseFunctionalProperty`), `owl:ObjectProperty` per
      relationship (domain/range, functional/inverse from cardinality), `phys:*` back-links.
      Default IRIs keep the `arangodb.com` host (physical ns identical to the Arango
      analyzer); both overridable.
- [x] `cli.py` — `snapshot` / `analyze` / `owl` subcommands; live source (`--source`/`--url`)
      or offline `--from-snapshot`; `-o`/stdout, `--pretty`, `--format`, `--iri-base`/
      `--phys-iri-base`.
- [x] validate emitted bundles against the JSON Schema in CI (contract-conformance tests run
      under pytest; CI installs `[dev,postgres,csv,owl]`).

Exit criteria:
- [x] `owl --format turtle` produces a `.ttl` that parses as valid RDF (rdflib round-trip in
      tests) using the same `phys:` namespace the ArangoDB analyzer publishes → importable by
      `arango-ontoextract` (full cross-repo ingestion test is Phase 5)
- [x] round-trip: OWL `phys:*` annotations resolve back to source tables/columns/FKs
      (asserted via rdflib triple queries)

---

## Phase 3.5 — Physical-model enrichment for the AOE contract

Triggered by `arango-ontoextract` feedback (2026-06): AOE wants a **mapping-agnostic, rich
physical schema model** and will own SQL→OWL/SHACL itself (it does *not* consume our OWL).
The conceptual model + OWL stay as **optional** outputs for the other consumers.

- [x] Enrich `types.py` (all additive / back-compatible): `Column` gains
      `is_unique` / `default` / `comment` / `ordinal` + computed `type_category`;
      `Table` gains `schema_name` / `comment` / `is_view` / `unique_constraints` /
      `check_constraints` / `indexes`; `ForeignKey` gains `is_unique` (cardinality hint);
      `PhysicalSchema` gains `source` provenance. New models `CheckConstraint`, `Index`,
      `SourceProvenance`.
- [x] `typemap.normalized_type_category()` — integer/decimal/boolean/string/temporal/
      binary/uuid/json/array (raw type stays authoritative).
- [x] Baseline consumes the new signals: FK `is_unique` (or FK==PK) → `1:1`; declared
      unique columns marked `unique`/`indexed`.
- [x] CSV connector populates provenance + ordinal + PK-uniqueness, with **opt-in**
      low-cardinality enum sampling (`sample_enums`) → `CheckConstraint.enum_values`.
- [x] Export the new model types; update DESIGN (consumer boundary, §3.1, S4) + tests
      (255 total, ruff clean; CSV golden bundle regenerated).
- [x] **Snowflake** catalog introspection enriched: column default/ordinal/comment, table
      comment + view flag, unique constraints (`SHOW UNIQUE KEYS`), FK cardinality hint,
      provenance + server version (`CURRENT_VERSION()`). Validated **always-on** via
      `fakesnow` (embedded emulator) + the mock-cursor unit tests.
- [x] **Postgres** catalog introspection enriched: column default/ordinal/comment
      (`pg_description`), table comment + view flag (`pg_class`/`obj_description`), unique
      constraints, CHECK constraints (`pg_get_constraintdef`), FK cardinality hint, provenance
      + server version. Assembly validated via a scripted fake-cursor unit test; live SQL by
      the Docker workflow (Postgres capabilities widened to the full set there).
- [x] **MySQL** catalog introspection enriched: default/ordinal + inline `COLUMN_COMMENT` /
      `TABLE_COMMENT`, view flag, unique constraints, CHECK constraints (8.0.16+, best-effort),
      FK cardinality hint, provenance (`VERSION()`). Assembly validated by a scripted
      fake-cursor test; Docker harness widened to the full set.
- [x] **SQL Server** catalog introspection enriched: default/ordinal, view flag, unique
      constraints, CHECK constraints (`sys.check_constraints`), comments
      (`sys.extended_properties`, best-effort), FK cardinality hint, provenance
      (`SERVERPROPERTY('ProductVersion')`). Assembly validated by a scripted fake-cursor test;
      Docker harness widened (comments asserted in the mock, not the plain-DDL live path).

---

## Testing strategy (datasource matrix)

Tiered so each engine is tested with the cheapest thing that still exercises the *real*
connector code (see the conformance harness in `tests/_conformance.py`, run against every
available backend with capability gating):

| Tier | Engines | Mechanism | Where |
| --- | --- | --- | --- |
| Embedded (always-on) | **DuckDB** | server-less; real connector over the full capability set | `tests/test_duckdb_connector.py`, main CI |
| Offline artifact (always-on) | **dbt** | `manifest.json` fixture; tests/contracts → constraints/FKs (full set minus DEFAULTS) | `tests/test_dbt_manifest_connector.py`, main CI |
| Offline artifact (always-on) | **OSI** | `*.osi.yaml` fixture; datasets/keys/relationships → tables/constraints/FKs (no types → temporal/string) | `tests/test_osi_connector.py`, main CI |
| Embedded (always-on) | **Snowflake** | `fakesnow` (DuckDB-backed, patches the driver in-process) | `tests/test_snowflake_fakesnow.py`, main CI |
| Embedded (always-on) | all dialects incl. **Databricks** | recorded result-set + mock-cursor unit tests | `tests/test_*_connector.py`, main CI |
| Embedded (always-on) | **DuckDB sampler** | the only place sampler *SQL* is executed against real rows — value overlap + all three denormalization probes, plus connector→inference→sampler end to end | `tests/test_duckdb_sampler.py`, main CI |
| Offline corpus (always-on) | CSV | real CSV connector + golden bundle | `tests/test_golden_csv.py`, main CI |
| Live Docker (CI) | **Postgres, MySQL** | service containers + `RUN_INTEGRATION` conformance | `tests/integration/`, `integration.yml` |
| Live opt-in (DSN) | SQL Server, **Snowflake**, **Databricks** | same harness, gated by a DSN env var | `tests/integration/` (skipped without DSN) |
| Live opt-in (DSN) | **Postgres DDL event log** | executes DESIGN-ADDENDUM-bitemporal §3.1's published trigger and checks RSA reads it back: DDL dated, `ALTER` later than an earlier `CREATE`, DML producing no row | `tests/integration/test_bitemporal_postgres.py` (skipped without `RSA_PG_DSN`) |
| Live opt-in (DSN) | **Postgres + MySQL sampler SQL** | value overlap + all three denormalization probes executed against real engines — the per-dialect SQL DuckDB cannot vouch for. Both verified correct; the CSV break was isolated to its Polars code, not systemic. **SQL Server and Databricks probe SQL remains unexecuted.** | `tests/integration/test_sampler_probes.py` (per-dialect DSN; each skips independently) |

- **Snowflake** → `fakesnow` for CI (real code path, no cloud) + opt-in live via `RSA_SNOWFLAKE_DSN`.
- **Databricks** (implemented) → Unity Catalog `information_schema` introspection (three-level
  `catalog.schema.table`); GA PK/FK/UNIQUE + comments in `information_schema`. Assembly
  validated by mock-cursor tests (no in-process emulator exists); live smoke is opt-in via
  `RSA_DATABRICKS_DSN`.
- **DuckDB** (implemented) → embeddable, always-on, exercises the full capability set and
  validates the generic `information_schema` FK/PK/unique resolution the RDBMS connectors reuse.
  It is also the **only tier that executes sampler SQL against real rows**. Every other sampler
  is covered by mock cursors returning a canned number, which verifies the plumbing and not one
  character of the SQL — and that gap is precisely how two CSV denormalization probes shipped a
  `TypeError` that fired the first time they touched data. A mock-only sampler test should be
  read as "unverified SQL".

---

## Phase 4 — LLM refinement (optional, additive)

- [x] `providers/` interface (openai / anthropic / openrouter) + registry
      (`register_provider` / `list_providers` / `create_provider`), copied from the Arango
      analyzer's pattern; SDKs imported lazily behind extras.
- [x] `refine.py` — generate / validate / repair loop that **refines** (not regenerates) the
      baseline: semantic renames + embed-vs-link / n-ary / description hints, applied only to
      existing elements on safe copies (no invent/drop; rename-collision validation).
- [x] `RelationalSchemaAnalyzer(llm_provider=...)` wired: provider name or object; **graceful
      fallback** to baseline on any provider/validation error; `metadata.llm` records outcome.
- [x] Refinement provenance: touched elements flip `source` to `llm`; hints are contract-valid
      additive keys. Tested with a fake provider (apply, repair-on-collision, JSON-failure,
      end-to-end analyzer path + fallback). 309 tests, ruff clean.
- [ ] **Deferred** — denormalization detection (needs value sampling) and an `eval/` harness
      comparing baseline vs LLM-refined against golden corpora (needs a live LLM + labeled
      corpora; better with the Phase 5 Docker/live corpora).

---

## Phase 5 — MCP + ecosystem integration

- [ ] **Docker integration suite** (`RUN_INTEGRATION=1`): load the r2g SQL-dump corpora
      (Pagila / Chinook / Northwind via `docker compose` + the r2g `docker/*.sql`), introspect
      with the live connectors, and assert golden conceptual bundles — the live-DB counterpart
      to the offline CSV golden corpus added in Phase 2.
- [x] `tool.py` — `run_tool(request) -> response`: the v1 tool-contract entrypoint
      (snapshot / analyze / owl), live source or captured `input.physical`; fully testable
      without the `mcp` package.
- [x] `mcp_server.py` + `relational-schema-analyzer-mcp` entry point — FastMCP wrapper
      (stdio + sse/streamable-http, bearer-token gate `RSA_MCP_TOKEN`), generic
      `..._run`/`..._run_json` + typed `snapshot`/`analyze`/`owl` tools.
- [ ] **r2g integration PR**: add dependency, replace embedded modules with imports/shims,
      delete duplicated code, wire conceptual schema into `MappingConfig` generation
      (this realizes r2g's planned Phase 10 ontology derivation via the shared lib)
- [ ] **arango-ontoextract integration**: add relational source path that calls
      `export_owl_turtle()` + provenance, alongside the existing Arango path
- [ ] Coordinate with `arango-schema-analyzer` maintainers on a **shared contract package** to
      retire `MappingBundle` duplication

---

## Risks & mitigations

| Risk | Mitigation |
| --- | --- |
| Contract drift vs `arango-schema-analyzer` (consumers duplicate `MappingBundle`) | Pin compatible ranges; copy the v1 JSON Schema; drive toward a shared contract package in Phase 5 |
| Extraction breaks r2g | Stage 1 is behavior-preserving; gate on r2g's existing test suite; ship re-export shims |
| Relational physical mapping mistaken as Arango-queryable | DESIGN §1 makes the boundary explicit; consumers documented per-artifact |
| `arango-ontoextract` moving to its own direct extractor | Position this lib as an *additive* TTL+provenance source, not the sole path |
| Scope creep (Kafka, DDL parsing, Oracle/SQLite) | v0 = RDBMS + CSV only; defer the rest behind connector plugins |

---

## Release history

Actual cut (GitHub releases / PyPI), which differs from the original phase-by-phase
projection — the whole core (Phases 0–5) landed together in the first release:

- **v0.1.0** — first release: physical core across 7 sources (PG / MySQL / SQL Server /
  Snowflake / DuckDB / Databricks / CSV), deterministic conceptual baseline + FK inference,
  OWL (Turtle / JSON-LD) exports, CLI, optional LLM refinement, and the tool-contract + MCP
  server.
- **v0.2.0** — additive `Column` / `Table` `extra: dict` consumer-metadata passthrough
  (serialized only when non-empty; unblocks the r2g dependency-reversal compat layer).
- **v0.3.0** — physical-model enrichment for the AOE contract + the source-scope ADR
  (DESIGN §9.3.1) and the first data-catalog source: **dbt** (`manifest.json`).
- **v0.4.0** — second data-catalog source: **OSI** (`*.osi.yaml`).
- **v0.5.0** — tagged locally, **never published**; its contents shipped in v0.6.0.
- **v0.6.0** — **R2RML** export (CLI + tool contract + MCP), `ForeignKey.enforced` (unenforced
  constraints as evidence, not proof), `DatabricksValueSampler`, class-abstraction discovery
  (type-discriminator detection + `conceptual-taxonomy` integration, emitted as
  `subClassOfProposals`), physical-mapping `schema` qualification and join-table parent columns,
  and Databricks `full_data_type` (precision/scale were being discarded).
- **v0.7.0** — the **declared-key overlay** (`overlay.py`, `--overlay FILE`): human-supplied
  PK/FK/UNIQUE merged onto a `PhysicalSchema` for sources whose catalog declares none, with the
  catalog always winning, overlay keys labelled rather than laundered, and typos failing loudly.
  Plus the **injected seams** (`samplers.py`) that make taxonomy discovery reachable — before
  them, discriminator detection ran only on declared CHECK constraints and specialization
  constraints were always `null`.

- **v0.7.1** — **FK inference targets any single-column candidate key, not only the primary
  key.** Candidate-target selection only ever proposed `table.primary_key`, so a schema with a
  surrogate PK beside the natural business key everything references (`accounts.id bigint PK`
  plus `accounts.account_id text UNIQUE`) generated one candidate, saw it correctly rejected on
  type, and inferred *nothing* — on exactly the constraint-free schemas the engine exists for.
  Unique targets rank just below PK targets, so schemas where the PK is the referent are
  unchanged. Fixes the same root cause for `r2g`, whose `fk_inference` is a re-export shim.

- **v0.7.2** — **the denormalization probes had never executed.** They exist in every
  sampler, are called by nothing in this library, and had one mock test asserting the
  methods return the fake value its own fake cursor was primed with — so two of the three
  CSV probes raised `TypeError` the first time they touched real rows. Fixed, plus real-data
  coverage: a `DuckDbValueSampler` (the factory previously returned `None` for `duckdb`, so
  value analysis silently degraded to name-only) with always-on tests, and live Postgres
  probe tests. Composite FK targets now include composite UNIQUE keys, completing the 0.7.1
  candidate-key fix. `r2g` needs this version: 0.4.0 **and** 0.7.1 both carry the broken
  probes, so a real-sampler test against either passes for the wrong reason —
  `_safe_probe` swallows the error and the sampling detectors emit nothing.

- **v0.8.0** — **bitemporal stamping** (`bitemporal.py`,
  `docs/DESIGN-ADDENDUM-bitemporal.md`): every schema records when RSA observed it
  (transaction time) and when the definition became true of the source (valid time), with a
  `valid_time_source` saying how the date was obtained. Valid time is capturable only at
  introspection, so RSA is the only component that can record it — and it records without
  storing: `schema_diff` stays pure and history remains the temporal store's job. Catalog
  dates are gated by the fingerprint, which is now explicitly structure-only, so Snowflake's
  `LAST_ALTERED` moving on DML cannot fake a schema change. Additive: unstamped schemas
  serialize byte-identically to before. Reachable from the CLI (`--prior-run`), the tool
  contract and MCP (`input.previousAnalysis` — the field the shared contract already
  declared, so RSA and `arango-schema-analyzer` converge rather than fork). Also fixes
  pre-existing request-contract drift: the published request schema described an entrypoint
  RSA does not have, so every real request was invalid against RSA's own contract.

- **v0.9.0** — **keys for sources that declare none.** Snowflake (by habit) and lakes and
  warehouses (by design) declare few or no keys, and FK inference targets only declared
  candidate keys, so those sources yielded no relationships at all.
  - *Unique indexes are candidate keys* (Postgres, SQL Server; MySQL already reported them).
    Partial, expression, invalid, hypothetical and computed-column indexes are excluded, and
    an index repeating a constraint or the primary key is not counted twice. Fixes MySQL
    functional-index keys, which were reported on their plain column alone. **Output
    change:** a schema with constraint-less unique indexes now lists them in
    `unique_constraints`, so its fingerprint changes once on upgrade.
  - *Cost-governed Snowflake value sampler* (`SnowflakeValueSampler`): FK overlap evidence
    under a query budget, a per-query timeout and a query tag, with overlap measured against
    the whole referenced column. Snowflake key-pair authentication (ported from `r2g`),
    with credentials masked in connection errors and kept out of tracebacks.
  - *Primary keys proposed from data* (`profile_primary_keys`): screened on a sample,
    confirmed over the whole table, ranked on naming and type with the reasons kept. A table
    whose search was not finished is reported as not evaluated, never as keyless.
  - *Draft key overlays* (`draft_key_overlay`): proposed primary keys plus the foreign keys
    inference finds once those keys exist, as an overlay a person reviews and then applies.
    Nothing is applied automatically. On `r2g`'s constraint-free Customer 360 schema the
    draft matches the hand-reviewed overlay exactly (5/5 primary keys, 6/6 foreign keys).

- **v0.9.1** — **value samplers test overlap against the whole referenced column.** Every
  sampler bounded *both* sides of the comparison and then joined them, so a valid foreign key
  into a table larger than the bound was measured against an arbitrary slice of its parent.
  Reported by `r2g` against Postgres; found in six of the seven samplers (Postgres, MySQL,
  SQL Server, Databricks, DuckDB, CSV). On Postgres 16, with foreign keys the database itself
  accepted as constraints — so the truth is exactly 1.0 — a 200k-row parent at the default
  bound of 10k scored **0.05** when children were spread across the parent, and **0.00** when
  they referenced only recent rows, which is the ordinary shape of an append-only parent:
  `LIMIT` without `ORDER BY` returns the physically-first rows, the intersection is empty, and
  zero is the hard veto in `_apply_sampler` — the real foreign key was not left unconfirmed, it
  was deleted. The 0.05 case is subtler and worse: the candidate survived carrying evidence
  reading `value overlap avg=0.05`, which reads as the data refuting it. Both score 1.000 after
  the fix. The local side stays bounded; a sample of the child values is a fair estimate of the
  whole. **The cause was a contract gap, not six independent mistakes:** `README` stated the
  semantics correctly, but the `Sampler` protocol — what an implementer codes against — said
  only that it returns a ratio in `[0, 1]`, so bounding both sides looked even-handed. The
  asymmetry now lives in the protocol docstring, asserted behaviourally over every sampler by
  `tests/test_sampler_contract.py`, with a registry test that fails if a new `*ValueSampler` is
  added without appearing in it. Behavioural rather than static because `CsvValueSampler` bounds
  with polars `n_rows` rather than SQL `LIMIT` — invisible to the grep that produced the issue,
  caught by the test on its first run. `SnowflakeValueSampler` needed no change: it was already
  correct (v0.9.0), which is why `r2g`'s Snowflake-only `suggest-keys` was never misled.

- **v0.9.2** — **value samplers no longer delete a foreign key whose values differ only in
  case or padding.** Comparison was strict, so `CUST1` against `cust1`, or against the
  space-padded `CUST1   ` a `CHAR(n)` column becomes when landed in a warehouse, scored
  **zero** — and zero is the hard veto in `_apply_sampler`, so the real key was not left
  unconfirmed, it was removed. Same failure class as the v0.9.1 fix, reported by neither:
  found by reading [HelixDB/helix-foundry](https://github.com/HelixDB/helix-foundry), which
  measures strict and relaxed overlap separately. Measured live on Postgres 16, MySQL 8.4
  and SQL Server 2022, each against a foreign key the database itself enforces, with a
  500-code parent whose every child value names a real parent: 0.9.1 scores **0.000** on
  lower-cased keys on all three; 0.9.2 scores **1.000**.
  - *The answer no longer depends on the deployment.* With 0.9.1 a trailing-space
    difference passed on MySQL and SQL Server, which blank-pad when comparing, and failed
    on Postgres, which does not — the same schema scoring differently depending on which
    engine held it, with nothing in the output saying so. The strict pass is now forced
    binary (MySQL `BINARY`, SQL Server `Latin1_General_BIN2`) and the relaxed pass is
    explicit, so the engine decides rather than the server's collation.
  - *Labelled, not laundered.* The result is an `Overlap` — a `float` **subclass** carrying
    one flag, so comparisons, arithmetic and `r2g`'s re-export of `SamplerResult` are
    untouched and no consumer changes — and `_apply_sampler` appends
    `(matched after normalizing case/whitespace)` to the evidence when the relaxed
    comparison is what rescued the candidate. The same rule the declared-key overlay follows.
  - *The relaxed pass is not free, so it is conditional.* Normalizing means a second full
    scan of the referenced column, which since v0.9.1 is read whole; doing it
    unconditionally took a 1M-row parent from 121 ms to 794 ms. The strict pass now runs
    alone and returns immediately at ≥ 0.9, where the confidence bump saturates and a
    better number cannot change the candidate's fate. A healthy foreign key costs what it
    did in 0.9.1 (87 ms against 88 ms, same machine and data); only the weak and vetoed
    pairs pay the second pass. Snowflake's server-side branch deliberately keeps both
    counts in one statement, because there the scarce resource is the query budget rather
    than wall-clock, and it still costs three queries per pair.
  - *Scope:* the change can only raise a score, never lower it, so it can newly accept a
    candidate but never newly veto one. Output changes only where a sampler is supplied;
    the baseline path passes none, so default bundles and their fingerprints are unaffected.
  `r2g` is reached by this one, unlike v0.9.1: `suggest-keys` is Snowflake-only, and
  Snowflake is case-sensitive, so `0.000` was reachable there.

- **v0.9.3** — **the source interface stops promising what four of nine connectors cannot
  do.** `create_source_connector` is public and declared to return a `SourceConnector`,
  which promised both *describe a schema* and *open a session to read rows*. DuckDB,
  Databricks, dbt and OSI have no `open_session`: a caller would read the declaration,
  call it, and get `AttributeError` — with their own type checker having approved the
  call, because RSA said it was safe.
  - *The four are not four of a kind, and that decided the shape.* DuckDB and Databricks
    are real databases that could stream and do not yet — a gap. A dbt `manifest.json`
    and an OSI `*.osi.yaml` are **data catalogs** (DESIGN §9.3.1: "read-only", "no live
    system") that say which tables exist and hold none of their rows, so there is nothing
    to stream and never will be. Making all four implement it would mean methods whose
    only honest behaviour is to raise.
  - *So the promise splits.* `SourceConnector` is what every source does; the new
    `BulkReadableSource` adds `open_session` for the five that hold rows; and
    `supports_bulk_read` is a `TypeGuard`, so inside its branch a checker knows
    `open_session` exists rather than the caller hoping. It asks about `open_session`
    directly rather than via `isinstance`, because a `runtime_checkable` protocol carrying
    data members also demands `connection_string` and `schema_name` *on the instance* — a
    half-constructed connector would answer "cannot stream" when it plainly can.
    `tests/test_source_connector_protocol.py` pins every connector's answer, so closing the
    DuckDB/Databricks gap is a deliberate edit and a catalog source cannot quietly start
    claiming to hold rows.
  - *mypy now passes clean across the package and runs in CI.* That interface defect was
    the only real finding among its 18 standing errors. Ten were optional extras mypy could
    not see — configured per module in `pyproject.toml`, derived from
    `[project.optional-dependencies]` rather than from what happens to be installed, so a
    *new* unresolved import is still a finding. Six were genuine but small and are fixed,
    not suppressed: `object` where an SDK module or response meant `Any`; a loop variable
    reused for a `dict` and then a `ForeignKey`; three `x if isinstance(x, dict) else {}`
    ternaries that do not narrow; one variable holding either the `str` from the Turtle
    exporter or the `dict` from the JSON-LD one. No baseline and no suppressions.
    `RELEASING.md` had asked for mypy as a release gate while it carried those 18 errors,
    so the step was always skipped and the document read as though three checks had run.
  - *Runtime behaviour is unchanged*: no connector gained or lost a method, and no output
    or fingerprint changes. The narrowed type is a compile-time promise only, and no
    consumer type-checks against it today — `r2g` keeps its own mirrored copy, which this
    release makes stale.

- **v0.9.4** — **keys discovered from the data on Postgres, including a natural key beside
  a declared surrogate one.** `profile_primary_keys` / `draft_key_overlay` exist for sources
  whose catalog declares no keys, and two things kept them from reaching a Postgres source
  at all.
  - *No Postgres probe.* They reach an engine through `KeyProbe` — `column_stats` and
    `combo_stats` — and `SnowflakeValueSampler` was the only sampler implementing them
    ([#18](https://github.com/arango-solutions-org/relational-schema-analyzer/issues/18)).
    `PostgresValueSampler` now does, in the same shape: screen on a bounded sample, where
    a duplicate or NULL is conclusive, then confirm survivors over the whole table, since
    only a full count can say *yes*. Both return `None` for "could not evaluate" rather
    than a guess, which is what keeps a table in `not_evaluated` instead of being called
    keyless.
  - *Profiling skipped every table that declared a primary key*
    ([#20](https://github.com/arango-solutions-org/relational-schema-analyzer/issues/20)),
    on the grounds that inference already treats a declared key as a candidate. True, and
    it assumes the declared key is the one children reference — the assumption **v0.7.1**
    was written to fix. So inference had learned to *target* a natural business key while
    profiling still refused to *look* for one, and a schema like
    `accounts(id PK, account_id)` yielded no relationships at all. Such tables are now
    profiled for an *additional* candidate key.
  - *Name evidence is the gate.* Uniqueness alone is far too weak on an already-keyed
    table: at 20k rows `note`, `primary_champion` and `deployment_date` are each distinct
    in every row and none is a key, and declaring those is worse than declaring nothing,
    because an overlay reads as a curator's assertion. A column qualifies only when its
    name carries the claim — named after its table, or an `_id`/`_key`/`_code`/`_no`/`_num`
    identifier. A bare `id` is excluded, being the declared key itself.
  - *Emitted as `uniqueConstraints`, never as a `primaryKey`.* The declared key stays the
    primary key; what is discovered is another column that is also unique, and therefore a
    legitimate FK target. Keyless tables are profiled first, so if the probe budget runs
    out it runs out on the bonus rather than on the purpose; `find_additional_keys=False`
    turns the second pass off.
  - Measured on a live constraint-free Postgres where inference alone finds nothing: the
    draft proposes the uniqueness *and* the references it unlocks. Cost is not the
    constraint — six already-keyed tables profile in 11 queries and 153 ms, because one
    query screens every column of a table at once.
  - Also fixes a pre-existing gap found while reviewing: `PostgresValueSampler` built
    identifiers without escaping an embedded quote, so a column legitimately named `a"b`
    produced a syntax error, the query was caught, and the probe reported "could not
    evaluate" — a silent capability gap, and exactly the case where *couldn't check* must
    not read as *nothing there*. The class now quotes through `_qi`/`_qt` as the DuckDB and
    Snowflake samplers already did.

- **v0.9.5** — **key profiling on Postgres can be bounded.** 0.9.4 let
  `PostgresValueSampler` profile keys, and confirming a candidate runs `COUNT(DISTINCT …)`
  over the whole table — with no query budget, no per-query timeout, no query tag and no
  `stats`, all of which `SnowflakeValueSampler` has. A caller that promises those (r2g's
  `suggest-keys`) could not offer Postgres without breaking them
  ([#22](https://github.com/arango-solutions-org/relational-schema-analyzer/pull/22),
  part of [#18](https://github.com/arango-solutions-org/relational-schema-analyzer/issues/18)).
  - *Opt-in governance.* `max_queries`, `statement_timeout_s` and `query_tag`
    (`application_name`) default to `None`, so FK inference and every existing caller are
    unchanged. `stats` reports the same keys as Snowflake's, including `connect_failed`;
    a failed connection is attempted once per sampler, not once per probe.
    `create_value_sampler` passes all three through to the Postgres and Snowflake samplers.
  - *The timeout has to be committed.* Postgres undoes a session setting when its
    transaction rolls back, and the first cancelled probe does roll back — so an
    uncommitted timeout protects one query and is then silently gone. Set with
    parameterised `set_config` and committed; a live test fails if the commit is removed.
  - *Contract.* `tests/test_sampler_contract.py` now requires every sampler that profiles
    keys to also accept the three settings and report `stats`, so a future MySQL or SQL
    Server key probe cannot land ungoverned.
  - No output or fingerprint change.

- **v0.9.6** — **an accepted inference is labelled, not laundered.**
  `InferredForeignKey.to_foreign_key()` folded a candidate back "as if it had been
  declared": `enforced` stayed `True`, which the serializer omits, and the name defaulted
  to `None`. The key then read as a constraint the source guarantees, and since
  `baseline` treats a table with an enforced FK as authoritative and skips fallback
  inference on it, one folded-in guess also stopped inference on that table
  ([#24](https://github.com/arango-solutions-org/relational-schema-analyzer/pull/24)).
  - The key is always `enforced=False`; an explicit `constraint_name` is still honoured.
  - The default name is `inferred:<table>_<cols>_fk`, matching the overlay's
    `overlay:<table>_<cols>_fk`. `INFERRED_CONSTRAINT_PREFIX` is exported from
    `overlay.py` beside `OVERLAY_CONSTRAINT_PREFIX`: the two provenances for keys that are
    asserted rather than declared.
  - r2g 0.6.1's `infer-fks --accept` already writes keys this way
    (arango-solutions-org/r2g-arango#20); this brings the library method in line.
  - Nothing in RSA calls `to_foreign_key`, so no output or fingerprint change.

- **v0.9.7** — **a partition is not a relationship, and inference is now measured.**
  - *The fix.* PostgreSQL gives each partition its own name, its own columns and its own
    copy of the parent's key, which to name-based inference is indistinguishable from a
    child table carrying its parent's key — the strongest signal it has. So a partitioned
    table produced a proposed composite key from the parent to every partition, from every
    partition back, and between each pair of siblings. The connector already knew:
    `annotate_partition_metadata` tags parent and children and rolls the children's
    declared keys up to the parent, precisely because PostgreSQL records them on the child.
    `fk_inference` consulted neither field. **Output change** for partitioned Postgres
    sources: fewer, and correct, relationships in the bundle. No fingerprint change — the
    fingerprint covers `PhysicalSchema`, which inference never mutates.
  - *The rule is narrow, and had to be.* Excluding partition children outright looked
    obviously right and cut Pagila's recall from 0.974 to 0.513: a partition's references
    to *other* tables are genuine, which is why PostgreSQL stores them on the child. Only
    references *within* a family are meaningless. Applied at both candidate paths — these
    arose on the composite one, and fixing only the single-column path changed nothing.
  - *The harness* (`tests/corpus_eval.py`, #27). A database that declares its keys is
    already labelled, so stripping the declarations and re-inferring them gives a scored
    comparison for free. Chinook, Northwind and Pagila supply 55 tables, 63 foreign keys
    and 47 primary keys. The number RSA could not previously state, and the effect of this
    release on it:

        before   precision=0.492  recall=0.921  f1=0.641
        after    precision=0.935  recall=0.921  f1=0.928

    Precision, not recall, was the weakness — inference found 92% of real keys while
    proposing nearly twice as many as existed, and **56 of the 60 wrong proposals came
    from one partitioned table**. Three of the four that remain are inference being right:
    Pagila's `payment_p2022_07` declares no foreign keys while its six siblings declare
    three each. The fourth is a view, which cannot declare one. So 0.935 is a floor.
  - *The instrument is tested.* It shipped reporting `precision=1.000` when nothing was
    proposed and `recall=1.000` when nothing was declared — treating `x/0` as a perfect
    score, so a change that broke inference entirely would have read as half-right, and a
    constraint-free schema (RSA's target domain) as flawless. Both are now `None`, rendered
    `n/a`. `tests/test_corpus_eval.py` covers the scorer against cases whose answer is
    obvious, including that pooling adds counts rather than averaging rates and that
    stripping does not mutate the schema the truth was read from.
  - Still open on #27: the 17 scoring constants remain asserted rather than calibrated, and
    primary-key discovery is not yet scored against the corpus.

- **v0.10.0** — **declared value lists survive introspection, and singularize stops cutting
  words short.** A minor version because both change output, and because r2g admits RSA only
  below 0.10.0: r2g, and through it contextual-data-fabric, adopt this release deliberately.
  - *Declared value lists survive introspection on Postgres, MySQL and SQL Server* (#32).
    `CheckConstraint.enum_values` is what declared-CHECK discriminator detection reads, and only the CSV connector and dbt's `accepted_values` ever filled it. The three
    live connectors kept the raw expression and nothing else, so on a real database the README's
    "discriminators come from declared `CHECK (col IN (…))` constraints" never happened:
    declared detection found nothing unless a value enumerator was injected.
    - **Output change — the schema fingerprint changes** for any Postgres, MySQL or SQL Server
      schema with a CHECK that lists the values of one column, and for any MySQL schema with
      an `ENUM` column. Consumers that key caches on `physicalSchemaFingerprint` will miss
      once on upgrade, and bitemporal stamping will not carry `valid_from` forward by
      fingerprint continuity on that first run (it falls back to the catalog date or the
      observation time). `schema_diff` does not compare CHECK constraints, so it reports *no*
      change while the fingerprint moves. Schemas with no such constraint fingerprint exactly
      as before.
    - *One shared parser* (`check_enum.py`, no dependencies) recognises a single-column
      enumeration in the shape each database actually stores it, captured from Postgres 16,
      MySQL 8.4 and SQL Server 2022 rather than guessed: Postgres's
      `((status)::text = ANY ((ARRAY['a'::character varying, …])::text[]))`, MySQL's
      `` (`status` in (_utf8mb4'a',…)) `` — which 8.4 reports with every quote escaped a
      second time — and SQL Server's rewrite of `IN` into a reversed
      `([status]='b' OR [status]='a')`. Doubled quotes, casts, integer lists and each
      database's spelling of negative numbers are handled.
    - *Conservative by design*: a wrong value list asserts a taxonomy the schema does not
      have. Multi-column checks, ranges, functions of the column, `NOT IN`, any `AND`, `NULL`
      in the list, decimals and string forms with uncertain escaping all leave `enum_values`
      unset. So do constraints that do not guarantee the rows: Postgres `NOT VALID`, MySQL
      `NOT ENFORCED`, and SQL Server disabled or untrusted (`WITH NOCHECK`) constraints.
    - *MySQL* checks now also name their column (MySQL reports none), so detection can use
      them, and `ENUM` columns on tables — whose values `DATA_TYPE` had been discarding — are
      reported as a `CheckConstraint` named `<column>_enum` with the raw `COLUMN_TYPE` as its
      expression, unless a CHECK already enumerates that column. The column's `data_type`
      stays `enum`; views, which declare no constraints, get none.
    - The raw expression is unchanged everywhere. Bundles, OWL and R2RML do not read CHECK
      constraints, so with default options only the fingerprint moves. With
      `discover_taxonomy=True`, a specialization whose parent carries an enumerating CHECK
      is now reported as `mechanism: specialization` (confidence 0.90) rather than
      `shared_key` (0.75), and the discriminator column leaves the abstract class's shared
      properties.
  - *singularize keeps a `-se` plural's `e` and leaves `-s` singulars alone* (#30, PR #33;
    arango-solutions-org/r2g-arango#30). It turned `warehouses` into `warehous`, `sizes` into
    `siz`, `caches` into `cach`, `status` into `statu`, `analysis` into `analysi`, `news` into
    `new` and `movies` into `movy`; `key_profiling` had been working around the `-us`/`-is`
    half. Now `-es` is dropped only after a sibilant (`addresses`, `boxes`, `batches`) or a
    singular that already ends in `s` (`statuses` → `status`), and otherwise only the `s`
    goes; `-ses` → `-sis` for `-is` words; `-ies` → `-y` except `-ie` words; named sets cover
    invariant words, singulars ending in `s` and a short irregular table; only the last word
    of a compound inflects. `pluralize` is the inverse and is now case-insensitive
    (`PARTITION` → `PARTITIONS`, not `PARTITIONs`).
    - **Output change, in practice additive**: a table named with a `-se` plural or an `-s`
      singular now matches its `<singular>_id` columns in FK inference, key profiling and
      the CSV connector's key guess. Over every table and column name in Chinook, Northwind,
      Pagila and contextual-data-fabric's committed CSI (265 identifiers), no table name or
      FK prefix changes, so inference on those schemas is unchanged.
    - *One list for every copy.* `relational_schema_analyzer/naming_known_answers.py` is
      public: r2g and arangodb-schema-analyzer each keep their own singularizer and test it
      against this list (ASA vendors it; r2g imports it). `tests/test_naming_known_answers.py`
      runs each entry lower, UPPER, Capitalized and as a compound.

- **v0.11.0** — **singularize consults a dictionary.** A minor version because it changes
  output, and because r2g admits RSA only below 0.11.0: r2g, and through it
  contextual-data-fabric, adopt this release deliberately. The 0.10.0 rules still failed
  wherever nobody had listed the word: `cactus` -> `cactu`, `knives` -> `knive`, `cacti`,
  `criteria`, `mice` and `alumni` unchanged, `analytics` -> `analytic`. A benchmark of eight
  techniques against a hand-labelled answer key (213 words) and a held-out set labelled
  before any technique ran (72 words) put a dictionary lookup far ahead:

        r2g/RSA 0.10.0 rules          answer key 169/213   held-out 44/72
        LemmInflect                   answer key 206/213   held-out 68/72
        WordNet (NLTK)                answer key 201/213   held-out 67/72
        Snowball (ArangoSearch stem)  answer key  93/213   held-out 36/72
        this release                  answer key 212/213   held-out 68/72

  Stemmers, including ArangoSearch's `stem` and `text` analyzers, return stems
  (`warehous`, `categori`), so they cannot name a class; as a matching key they also merge
  different words (`account` = `accounting`). `tools/singularize_benchmark/run.py`
  re-runs the comparison.
  - *The layers*, in order: the named lists (irregulars, invariant words, singulars ending
    in `s`), then the dictionary, then the 0.10.0 rules for words it does not know
    (`skus`, `kpis`). The dictionary is `relational_schema_analyzer/data/noun_lexicon.json.gz`
    (198 KB), generated from LemmInflect 0.2.3's noun lexicon by
    `tools/build_noun_lexicon.py` (byte-reproducible, MIT notice in
    `data/NOTICE-noun-lexicon.md`). RSA gains **no runtime dependency**. A word the
    dictionary lists as its own lemma stays as it is (`logistics`, `cactus`); `pluralize`
    takes the dictionary's preferred plural (`knife` -> `knives`, `criterion` -> `criteria`).
    `naming.noun_lexicon()` exposes the table and its SHA-256, so r2g can check that
    arangodb-schema-analyzer vendors the same bytes.
  - *One data-modelling decision overrides the dictionary:* `demographics` -> `demographic`.
    The dictionary treats "demographics" as a noun in its own right, but a table of them
    holds one demographic per row, and contextual-data-fabric's committed Northwind CSI
    names the class `CustomerDemographic`. Across all 42 CSI files CDF has committed, that was
    the only class name the dictionary would have changed.
  - *Known answers* grow from 74 to 142 pairs, 4 singularize-only entries, 24 invariant
    words and a new `SINGULARS` group (singulars to leave alone whose plural differs, such
    as `cactus`); the tests run each in five spellings (469 cases). 63 fail on 0.10.0.
  - **Output change**: over every table and column name in Chinook, Northwind, Pagila and
    contextual-data-fabric's committed CSI (265 identifiers), no table name or FK prefix
    changes except `freight` (now its own plural) and `ship_via` (`pluralize` gives
    `ship_viae`); FK inference on those schemas is unchanged.

- **v0.11.1** — **two bugs reported by r2g: the CLI's JSON, and a CI job red for twelve
  days.** A patch release: the bundle itself is byte-identical, so no fingerprint changes.
  - *`analyze --prior-run` emitted invalid JSON.* `log.py` defers to `structlog` when it is
    installed, and an **unconfigured** structlog writes through `PrintLogger` to **stdout**,
    at debug level — so one `bitemporal_fingerprint_unchanged` debug line landed in the
    middle of the bundle and the caller's `json.load` failed. `--prior-run` was implicated
    only because it is the one flag whose path logs at debug. The CLI now sends logs to
    stderr; an application that has already configured structlog keeps its own setup
    (`structlog.is_configured()` is checked first, and a test asserts a caller's destination
    is not stolen).
  - *Why it survived.* **RSA never installed structlog** — it was in no extra — so CI always
    took the stdlib fallback, which writes to *stderr* at *WARNING*. Both the stream and the
    level hid it, and only an application bringing its own structlog could see it, which is
    exactly what r2g does. `structlog>=23` is now in the `dev` extra, because otherwise the
    new tests skip — and skipping is how this survived in the first place.
  - *The live-database job had failed on every push to `main` since 2026-10-14*, always on
    `test_a_wrong_password_is_tried_once`. CI runs Postgres with
    `POSTGRES_HOST_AUTH_METHOD: trust`, so the server accepts **any** password: the
    connection succeeded, `connect_failed` stayed False, the assertion failed. It passed
    locally, where the container enforces one. The claim under test is the 0.9.5 governance
    guard — one connection attempt per sampler, not one per probe — which has nothing to do
    with authentication, so it now uses an unreachable port (refused on any server however it
    authenticates) and counts calls to `psycopg.connect` **directly** rather than inferring
    from a flag. The password variant is kept where it can be covered: it detects a
    trust-auth server by trying, and skips with a reason.
  - *The test was green when it was written*, because it was only ever run against a
    password-enforcing server. It asserted something about the environment rather than about
    RSA — the same shape as a check nobody watches. `integration.yml` still does not run on
    pull requests (#39), which is what let it sit red for twelve days.

- **v0.12.0** — **a SQLite source, and a corpus score that no longer punishes honesty.**
  Additive: no change to how existing sources are read or analysed.
  - *SQLite connector* (`sqlite`, alias `sqlite3`; PR #43), asked for by arango-ontoextract,
    which needs BIRD's SQLite databases for its with-and-without-ontology benchmark. It needs
    no extra: SQLite ships with Python. It reads the catalog through SQLite's table-valued
    pragma functions with the table name bound, opens the file read-only (a missing file is
    an error, never a new database), resolves foreign keys case-insensitively as SQLite does,
    and treats a reference with no column list as pointing at the primary key. A reference to
    a table or column that does not exist is skipped and logged, since SQLite accepts and
    never enforces one. Keys are named `fk_<table>_<n>`; SQLite keeps no names and no
    comments. Review on the PR added: generated columns are reported (`pragma_table_xinfo`),
    user tables named `sqlite…` are no longer hidden, one unreadable virtual table is skipped
    rather than failing the file, and a file that is not a database fails with a plain
    message. On BIRD's 11 databases: 75 tables, all 105 declared foreign keys.
  - *SQLite value sampler.* The DuckDB sampler's measurements through four dialect hooks
    (text cast, ratio, real cast, substring test), so the SQL is written once; DuckDB's
    queries are unchanged. It is in the sampler contract (#10) as an embedded engine.
  - *`corpus_eval`: a declined answer is not a wrong answer* (#27, PR #35). A table the
    profiler declined to judge (an empty table) was counted as a miss; it now leaves recall's
    denominator, and a corpus where everything was declined reports recall `None`. With
    Pagila's data loaded, primary-key discovery scores across the whole corpus for the first
    time: precision and recall 0.800 (36 of 45 proposed, 47 expected, 2 declined). Pooled
    declines are now kept per corpus, so two corpora declining a same-named table (`customer`
    in Chinook and Pagila) count twice.
  - *Known and filed:* FTS5 shadow tables appear as tables and read-only WAL files cannot be
    opened (#44); embedded engines hash the library version into the schema fingerprint
    (#45).

- **v0.13.0** — **fixes and cleanup from the first `/code-quality` audit, and two contract
  decisions.** A minor version because output changes for some callers; each change is
  listed below.
  - *Output changes.*
    - **Empty samples.** A reference column whose sampled values are all NULL is "not
      evaluated" on Postgres, DuckDB and SQLite, as it already was elsewhere. It used to
      score 0.0, the hard veto, and delete a correct foreign key (#52, PR #61).
    - **JSON-LD.** It now carries the join tables' parent columns and the relationships'
      `schemaName`. Turtle and JSON-LD are rendered from one model, so a fact can no
      longer be in one and missing from the other (#53, PR #64).
    - **Self-references.** A primary key that references its own table is no longer
      reported as inheritance (`Acct subClassOf Acct`) (#54, PR #65).
    - **One stamping rule.** The CLI and `run_tool` share `pipeline.load_physical`. A
      snapshot that already carries a stamp keeps it unless a prior run is given; the CLI
      used to re-stamp it with the time the file was re-read (#60, PR #72).
    - **A sampler turns sampling on.** `InferenceOptions.sample_overlap` defaults to
      `None`, meaning "sample when a sampler is passed". Before, a sampler passed on its
      own was ignored. An explicit `False` still turns it off, and every known caller sets
      the flag, so they are unaffected (#60, PR #72).
  - *Fixes with no output change for ordinary schemas.*
    - **MCP stdout.** The stdio MCP server keeps log lines off stdout, which is its
      protocol channel (PR #51).
    - **Postgres key order.** Composite primary keys are read in declared column order
      (#55, PR #66).
    - **Quotes in names.** A `"` inside a table or column name no longer breaks Postgres
      and Snowflake bulk reads or Snowflake's key statements (#55, PR #66).
    - **Discriminator and specialization probes.** They quote and cap rows per dialect:
      backticks for MySQL and Databricks, brackets and `TOP` for SQL Server. Before,
      every probe failed on MySQL, and SQL Server fell back to reading a whole column
      (#56, PR #67).
    - **Specialization counter.** It reads a bounded parent sample, as documented
      (#56, PR #67).
    - **MySQL and SQL Server samplers.** They skip the second query on an empty sample,
      and keep the strict score when the normalized query fails (#59, PR #69).
  - *Cleanup, verified output-identical.*
    - **Connectors.** One table builder and key marker are shared by every connector
      (#58, PR #68).
    - **Samplers.** Connection plumbing is shared, and MySQL and Databricks share one set
      of SQL (#59, PR #69).
    - **Dead code removed:** `heuristics.py`, `DumpReader.read_chunks`,
      `postgres.preview_table_rows`. r2g keeps its own copies.
    - **Written once:** `quoting.py` holds the three identifier-quoting rules, and key
      profiling's two passes share one loop (#60, PR #70).
    - **Log events renamed:** `mysql_sampler_*`, `sqlserver_sampler_*`, and
      `csv_sampler_read_failed`.
  - *Cross-repo.* arango-schema-analyzer needs the new `sample_overlap` default ported
    (arango-schema-analyzer#75). Overlay in the tool contract waits on its agreement (#71).

- **v0.14.0** — **a key overlay through the tool contract.** A minor version because the
  contract gains a field. Requests without it behave exactly as in 0.13.0.
  - *`analysisOptions.keyOverlay`* (#71, PR #75). Tool-contract callers can now declare
    primary, foreign and unique keys for a source whose catalog has none (BigQuery, Glue,
    Hive), as CLI users could with `--overlay`. `run_tool` applies it through the shared
    pipeline, the same way to a live source and to `input.physical`. A bad overlay fails
    the request with `INVALID_REQUEST`, naming the mistake. The MCP `snapshot`, `analyze`,
    `owl` and `r2rml` tools take it as `key_overlay`.
  - *Contract.* `request.schema.json` gains `KeyOverlay`, admitting exactly the keys
    `overlay.py` accepts, with a test that fails if the two drift. It sits in
    `analysisOptions` beside where arango-schema-analyzer keeps `declaredReferences`, under
    a different name, because ASA verifies those against the database and an overlay key
    is asserted, not verified. RSA's request schema is its own copy and already declared
    relational-only fields (`source`, `input.physical`), so no change on the ASA side was
    needed. The contract README lists the three.
  - *CI.* GitHub Actions moved to their Node 24 major versions (#73, PR #74).

- **v0.15.0** — **empty columns are "not measured", and JSON-LD names Turtle's
  resources.** A minor version because output changes for both, as below.
  - *Denormalization probes on an empty sample* (#62, PR #76). `distinct_ratio`,
    `group_single_valued` and `delimiter_rate` on a column with no non-null value now
    return `None` ("not measured") on every engine. Postgres, DuckDB, SQLite, MySQL,
    Databricks and Snowflake returned 0.0, and r2g's cross-source key check
    (`xsk.py`) read that 0.0 as "hub column is NOT unique" and lowered the key's score;
    SQL Server and CSV already returned `None`. A genuine zero is still 0.0. No r2g
    change is needed: it already treats `None` as "not measured". Also,
    `delimiter_rate` casts to text on Postgres, DuckDB and SQLite, where a non-text
    column made the query fail (r2g only probes text columns).
  - *JSON-LD ids are full IRIs* (#63, PR #77). `export_owl_jsonld` wrote `@id` as a bare
    local name, a relative IRI that a JSON-LD processor resolves against the
    document's location, so the JSON-LD and Turtle exports named different resources.
    Every JSON-LD `@id` now carries the base IRI; nothing else in the JSON-LD changes,
    and Turtle and R2RML are byte-identical. arango-schema-analyzer has the same fix
    (its PR #79). Open PR #49 (OWL import) needs one test updated when it rebases.

Planned next:

- **mcp 2.0 port** — the `[mcp]` extra is pinned `<2` because mcp 2.0 removed the bundled
  `mcp.server.fastmcp` in favour of a new `MCPServer` API (FastMCP moved to its own package).
  `mcp_server.py` targets the 1.x FastMCP API. Porting it is a contained piece of work; until
  then the pin is what keeps the extra installable.
- **v0.16.0 (target; was v0.15.0 until the probe and JSON-LD release took that one, v0.14.0
  until the key-overlay release took that one, v0.13.0 until the audit-fix release took that one, v0.12.0 until the SQLite release took that one, v0.10.0 and v0.11.0
  until the singularizer releases took those numbers, v0.9.0 until key discovery took that one, and v0.8.0 before bitemporal stamping
  took that one)** — the
  **BigQuery** connector with its cost governor (see
  [`PLAN-bigquery.md`](PLAN-bigquery.md)); live Docker introspection corpus (Pagila / Chinook /
  Northwind); the downstream `r2g` and `arango-ontoextract` integration PRs; shared contract
  package.
- **Deferred findings, recorded so they survive the session that found them.** All came
  from reading [HelixDB/helix-foundry](https://github.com/HelixDB/helix-foundry) (October
  2026), which solves the same inference problem with the data already copied onto the
  local machine, so a full scan costs it nothing. That architecture does not port to RSA,
  which samples a customer's warehouse under a budget; these are the ideas that survive
  the difference.
  - [#12](https://github.com/arango-solutions-org/relational-schema-analyzer/issues/12) —
    **shipped in v0.9.2.** Samplers deleted valid foreign keys whose values differed only
    in case or trailing spaces.
  - ~~#13~~ — **closed as invalid.** The claim that the contract test had no negative half
    was wrong: `test_a_genuine_non_match_still_scores_zero` has been in the file since it
    was created, so a sampler returning `1.0` unconditionally would already have failed.
    The real gap was case/whitespace coverage, which #12 closed.
  - [#14](https://github.com/arango-solutions-org/relational-schema-analyzer/issues/14) —
    **open.** Detect shortcut foreign keys that duplicate a longer path: an invoice
    pointing straight at an account *and* at a subscription that points at the same
    account. Decided on the data — helix-foundry requires 95% row agreement before
    treating the direct link as redundant — rather than by graph transitive reduction,
    which would wrongly collapse `shipping_address_id` and `billing_address_id` into one.
    In RSA it should be **reported with its agreement ratio, not deleted**: describing the
    schema is the job, and dropping an edge is a consumer's decision. Belongs with the
    denormalization work, not before it.
