# Tool contract v1 (relational variant)

These schemas define the wire shape of the analyzer's JSON bundle:

```json
{ "conceptualSchema": {...}, "physicalMapping": {...}, "metadata": {...} }
```

## Provenance & compatibility

`response.schema.json` and `request.schema.json` are **copied from**
[`arango-schema-analyzer/docs/tool-contract/v1/`](https://github.com/ArthurKeen/arango-schema-analyzer)
(the `arangodb-schema-analyzer` contract). This is deliberate — per `docs/DESIGN.md`
§9.2, we **copy the v1 schema now and converge to a shared contract package later** rather
than block on extracting one.

`conceptualSchema` and `metadata` are **identical in shape** to the ArangoDB analyzer, which
is what lets a single downstream consumer handle both relational and ArangoDB sources.

## Relational `physicalMapping` variant

The only intended divergence is in `physicalMapping` **style enums and back-reference
fields**, which carry relational rather than ArangoDB semantics:

| Element | ArangoDB analyzer | This (relational) analyzer |
| --- | --- | --- |
| Entity `style` | `COLLECTION` / `LABEL` | `TABLE` |
| Relationship `style` | `DEDICATED_COLLECTION` / `GENERIC_WITH_TYPE` | `FOREIGN_KEY` / `JOIN_TABLE` |
| Entity back-refs | `collectionName` | `tableName`, `schemaName`, `primaryKey` |
| Relationship back-refs | `edgeCollectionName` | `fromTable`/`fromColumns`/`toTable`/`toColumns`, `joinTable`/`joinFromColumns`/`joinToColumns`/`attributeColumns` |

> **Status:** the relational fields above are in place, and emitted bundles are validated
> against `response.schema.json` in the test suite (`tests/test_golden_csv.py::…::
> test_validates_against_contract`; success criterion S2). The schema is intentionally a
> **superset** — it retains inherited Arango-only blocks (sharding, tenant scope, graphRag
> roles, vertex-centric indexes) so the same contract shape is shared with
> `arangodb-schema-analyzer` — rather than a pruned relational-only schema. Consumers should
> read the relational back-refs and ignore the Arango-only blocks (which the relational
> analyzer never populates).

## Relational request additions

`request.schema.json` also declares three request fields that arango-schema-analyzer's copy
does not have. ASA's contract describes an ArangoDB endpoint, and these have no ArangoDB
equivalent:

| Field | What it carries |
| --- | --- |
| `source` | `{type, url, schema?, params?}`: the relational source to introspect, in place of `connection` |
| `input.physical` | a previously captured `PhysicalSchema`, in place of introspecting `source` |
| `analysisOptions.keyOverlay` | human-declared primary, foreign and unique keys (the CLI's `--overlay`), see `KeyOverlay` in the schema (#71) |

`keyOverlay` sits in `analysisOptions` because that is where ASA carries its own curator
declarations (`declaredReferences`). It has a different name because the two mean different
things: ASA verifies a declared reference against the database and fails the request if it
does not hold, while an overlay key is asserted, not verified, and is labelled that way in
the output.

