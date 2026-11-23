# Design addendum — abstraction discovery, and RSA's place in the stack

**Status:** IMPLEMENTED (0.6.0 — `relational_schema_analyzer/taxonomy.py`, delegating to `conceptual-taxonomy`; see README "Class abstractions"). Companion to `arango-schema-analyzer/docs/prd-patch-proposal-relational-and-taxonomy.md`
and `conceptual-taxonomy/docs/SPEC.md`.

**Context.** Work on `arango-schema-analyzer` (ASA) to handle relational-style physical
patterns in ArangoDB surfaced a capability both analyzers need and neither fully has:
discovering **class abstractions** (`rdfs:subClassOf`) from a schema.

Three findings came out of that analysis, and this addendum records all three:

1. **RSA → ASA (§1).** RSA has already solved three things ASA was about to specify from
   scratch — FK inference, both relational mapping styles, and join-table classification. The
   ASA work should be a port, and the two style vocabularies should converge.
2. **New work for RSA (§2–§3).** RSA covers one and a half of the four classic relational
   inheritance encodings. Discriminator detection and specialization-constraint measurement
   are the gaps; both feed the shared taxonomy library.
3. **A live consumer bypasses RSA's conceptual layer (§4).** `arango-ontoextract` uses RSA's
   connectors as an introspector and does its own SQL→OWL mapping. §4.1 identifies a
   capability worth pulling back from it — SHACL export.

---

## 1. What RSA already has that ASA needs (port direction: RSA → ASA)

Recorded so the ASA work is a port rather than a parallel invention:

| RSA capability | Location | ASA equivalent being specified |
|---|---|---|
| FK inference — name heuristics, type compatibility, composite candidates, bounded value-overlap sampling, confidence, dedup | `fk_inference.py` (1,539 lines) | New `FOREIGN_KEY` detector |
| `FOREIGN_KEY` relationship mapping style | `baseline.py:290-299` | Same name adopted in ASA |
| `JOIN_TABLE` style + `joinFromColumns` / `joinToColumns` / `attributeColumns` | `baseline.py:245-264` | Same name adopted in ASA |
| Join-table classification heuristic | `baseline.py::_is_join_table` | ASA reification detector |
| "Unenforced FK is evidence, not proof" | commit `08be3fb` | Universal on the Arango side — nothing is ever enforced |

**Consequence for RSA: none, immediately.** These are ASA-side ports. The one thing to be
aware of is that ASA will adopt RSA's **style vocabulary** (`FOREIGN_KEY`, `JOIN_TABLE`)
rather than minting Arango-specific names, so the two `physicalMapping.relationships`
dialects converge instead of forking. If RSA renames or restructures either style, that is
now a cross-repo contract change.

**Sampler protocol is the reuse seam.** `Sampler = Callable[[str, str, str, str],
Optional[float]]` and the `create_value_sampler` factory are already paradigm-neutral in
shape. An `ArangoValueSampler` is a natural sixth implementation. Whether the engine
itself is eventually extracted into a shared package is deferred — see §3.

---

## 2. What RSA needs (new work)

RSA covers **one and a half** of the four classic relational inheritance encodings:

| Pattern | Encoding | Status |
|---|---|---|
| Single-table inheritance | discriminator column on one table | **absent** |
| Class-table inheritance | child PK *is* FK to parent PK | **present** — `_is_shared_pk_fk` → `entity["subClassOf"]`, `baseline.py:302-308` |
| Concrete-table inheritance | sibling tables, duplicated common columns, no parent table | **absent** |
| **Specialization / generalization** | supertype table carrying common attributes **and a discriminator**, plus subtype tables joined on shared PK | **half** — the shared-PK edge is detected; the discriminator, and everything it licenses, is not |

Two motivating cases:

**Concrete-table (row 3)** — `MortgageAccount`, `CheckingAccount`, `SavingsAccount`,
`InsuranceAccount` as four tables sharing `{account_id, name, description, balance}` and
differing in their differentia. There is no parent table; the abstraction must be
**synthesized**.

**Specialization (row 4)** — the classic ER construct, and the one ORMs emit by default
(Fowler's *class table inheritance*, Hibernate's *table-per-subclass*, SQLAlchemy's *joined
table inheritance*). An `account` supertype table carries the common attributes plus an
`account_type` discriminator; `mortgage_account`, `checking_account`, … each have a PK that is
also an FK to `account.account_id`.

RSA detects the shared-PK edges here but treats them as four independent `subClassOf`
assertions. It does not read the discriminator, so it cannot tell that the subtype set is
**closed** (a subtype table that is empty or absent is still declared by the discriminator),
and it cannot derive the two ER specialization constraints:

- **disjoint vs. overlapping** — does any `account_id` appear in more than one subtype table?
- **total vs. partial** — does every `account_id` appear in some subtype table?

Those are worth having: they are the only sound route to `owl:disjointWith` between siblings
and to a covering axiom on the parent. Both need row-count measurements, so RSA measures and
the shared library asserts (SPEC §4.3.1).

### Proposed change

1. **Depend on `conceptual-taxonomy`** (see its `docs/SPEC.md`) and call
   `discover_abstractions` after the deterministic baseline, before OWL export.

2. **Move `_is_shared_pk_fk` subsumption into the shared library** (SPEC §4.3). Today it
   writes `entity["subClassOf"]` directly from the FK loop. Once the shared library also
   proposes parents via formal concept analysis, two independent channels would write
   competing hierarchies for the same entity. The library resolves conflicts by mechanism
   precedence and records losers in `evidence.rejectedParents`; a direct write bypasses that.

   RSA keeps *detecting* the shared-PK signal (it needs the physical schema); it stops
   *deciding* the hierarchy.

3. **Detect discriminator columns.** RSA does not do this at all today, and **one detector
   serves both missing patterns**: it supplies the value enumeration for single-table
   inheritance (SPEC §4.1) *and* the discriminator half of specialization (SPEC §4.3). The
   natural signal is a low-cardinality, high-coverage, non-key `*_type` / `*_kind` /
   `*_category` column. ASA's `type_detection.py` is a ready template — value-distribution
   gating on distinct count, coverage fraction, and value shape, with ID-suffixed columns
   excluded.

   Where a discriminator sits on a table that is *also* the parent of shared-PK FK edges, the
   two signals corroborate: emit one `mechanism: "specialization"` result rather than a
   discriminator taxonomy and four independent subclass edges.

4. **Measure the specialization constraints** (SPEC §4.3.1) for tables in a specialization
   cluster. Two bounded queries per cluster:
   - *disjointness* — does any parent key appear in more than one subtype table?
   - *completeness* — does every parent key appear in some subtype table, and if not, what
     fraction is uncovered?

   RSA measures; the shared library asserts `owl:disjointWith` / the covering axiom. When the
   measurement is skipped, the library emits `null` rather than `false` — absence of evidence
   must not read as evidence of absence.

5. **Surface abstractions in the OWL and R2RML exports.**
   - OWL: `owl_export.py` already emits `rdfs:subClassOf` from `e["subClassOf"]`
     (lines 140-141, 302-303). It needs to additionally emit synthesized abstract classes,
     which have no table behind them.
   - R2RML: this is where abstractions pay off most. Class hierarchies are core OBDA — an
     `Account` superclass over four `rr:TriplesMap`s is exactly what Ontop consumes. Worth
     noting the existing limitation documented in `r2rml_export.py:31` (an N:M join table's
     `attributeColumns` cannot be expressed, so they are reported in a comment) is the same
     shape of problem the ASA-side reification work addresses.

6. **Grounding validation.** Synthesized abstract classes have no `physicalMapping.entities`
   entry by design. Any validator that requires every conceptual entity to be grounded must
   be taught to expect `abstract: true` rather than flag it. Note this applies only to
   *synthesized* parents (concrete-table inheritance) — a specialization supertype is a real
   table and stays grounded, which is why the library distinguishes them with `synthesized`.

---

## 3. Deferred: extracting the FK engine

`infer_foreign_keys` is ~95% paradigm-neutral — it works over tables/columns/PKs and delegates
the only DB-touching step to the injected `Sampler`. Extracting it into the shared package
would let ASA reuse it wholesale rather than porting.

**Not now.** It would mean refactoring RSA's largest and most battle-tested module and
re-releasing a package `r2g` depends on in production, in service of a consumer that does not
exist yet. Ship the shared library with taxonomy only (new code, zero risk to shipping
consumers), let ASA adapt the algorithm, and revisit convergence once the shared package has
proven itself.

This is the same call DESIGN §9.2 already recorded for the tool-contract schema: **copy now,
converge later.**

---

## 4. `arango-ontoextract` uses RSA's connectors but bypasses its conceptual layer

Recorded because it is the clearest available signal about where RSA's conceptual layer falls
short of a real consumer's needs.

AOE's `backend/app/services/relational_schema_extraction.py` imports
`relational_schema_analyzer.create_connector` to obtain a typed `PhysicalSchema` — and then
performs its own SQL→OWL/SHACL mapping. Its docstring is explicit: *"exactly like
`_direct_extract_schema` owns the ArangoDB→OWL mapping — AOE owns the SQL→OWL/SHACL mapping
here."* It never calls `RelationalSchemaAnalyzer().analyze()`.

**RSA is being consumed as an introspector, not as an analyzer.** The conceptual layer — the
part that distinguishes this library from `r2g`'s original extraction core — has a downstream
consumer that skips it entirely.

What AOE consequently does not get:

- `JOIN_TABLE` collapse — a junction table becomes an `owl:Class` rather than an N:M
  relationship
- `_is_shared_pk_fk` → `subClassOf` — the one inheritance pattern RSA detects never fires
- `infer_foreign_keys` — undeclared FKs stay invisible, which is the entire purpose of that
  1,539-line module
- `naming.py` / CC-12 conceptual naming
- the `{conceptualSchema, physicalMapping}` split itself

RSA is not obliged to act on this — AOE owns its mapping choices. But it is worth knowing that
the library's most substantial capabilities are invisible to a live consumer, and worth asking
AOE why. Two plausible reasons are worth distinguishing: the conceptual layer genuinely lacked
something (fixable), or the integration predates it (a documentation/outreach problem).

### 4.1 SHACL export — pull this capability back from AOE

AOE's relational path generates something RSA does not have at all: **SHACL shapes from
relational constraints.**

- `NOT NULL` → `sh:minCount 1`
- `UNIQUE` → `sh:maxCount 1`
- column type → `sh:datatype`
- a recognized `col IN (...)` CHECK → `sh:in`
- PK / unique columns → `owl:FunctionalProperty` + `owl:InverseFunctionalProperty`
- FK with unique columns (1:1) → functional + inverse-functional

RSA's exports today are OWL (Turtle / JSON-LD) and R2RML — *what the concepts are* and *how to
reach them in SQL*. A `shacl` target would add *what makes an instance valid*, and every
consumer would get it rather than only AOE. The constraint metadata already lives in RSA's
`Schema` / `Table` / `Column` types, so this is an export-layer addition, not new
introspection.

It also completes a natural triad alongside the OWL and R2RML exports, and pairs with the
taxonomy work: a specialization's disjointness and completeness constraints (§2 item 4) are
expressible as SHACL as well as OWL.

**Proposed:** a `shacl` export target on the same adapter principle as `sparql` and the planned
`sql` — a mapping contract, not a validator. Sizing and gating are a separate decision; this
addendum only records that the capability exists downstream and belongs here.

## 5. Acceptance

The eight-encoding fixture set in `conceptual-taxonomy/docs/SPEC.md` §9 is shared. RSA's suite
consumes encodings #1, #2, #3, and #7 (relational); ASA's consumes #4, #5, #6, and #8. All
eight must produce an identical conceptual schema modulo `realizations`, `synthesized`, and
the constraint fields only #7/#8 can measure — if RSA and ASA diverge on the same taxonomy,
one of them is leaking physical structure into the conceptual layer.

Encoding #7 carries two RSA-specific expectations: exactly one `Account` class (the supertype
table, `synthesized: false`) with **no** competing synthesized parent, and measured
`disjoint` / `complete` values rather than `null`.
