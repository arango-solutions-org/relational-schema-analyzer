---
name: code-quality
description: Audit relational-schema-analyzer for unnecessary complexity, duplicated logic and reuse misses: a simplicity and necessity-ladder review, separate from pr-review's correctness and mergeability checks. Use for a whole-repo audit or a quality pass over one diff or PR, or when asked whether something "needs to be this complicated" or could reuse existing logic. NOT for r2g, arango-schema-analyzer or contextual-data-fabric (they keep their own copies).
---

# Code quality (relational-schema-analyzer)

## Invocation

`/code-quality audit`: scan the whole `relational_schema_analyzer` package for
unnecessary complexity, duplicated logic and reuse misses.
`/code-quality review <diff | branch | PR number>`: the same lens, limited to one
change.

## Purpose

`pr-review` asks "is this safe to merge?" This skill asks "does this need to
exist, and is it the simplest form that works?" A change can be safe to merge and
still be more complicated than it needs to be, so run this alongside `pr-review`,
not instead of it.

The method is the necessity ladder from the `ponytail` skill
(github.com/DietrichGebert/ponytail), ported from arango-query-core's copy.

## The necessity ladder

For each piece of logic, check in this order and stop at the first rung that holds:

1. Does it need to exist at all?
2. Is the same logic already somewhere in this package? Grep for the concept
   before writing it.
3. Could the standard library do it?
4. Does a dependency already installed do it?
5. Can it collapse to one line?
6. Is it the minimum that works?

The model to follow is the DuckDB and SQLite samplers (#43).
`SqliteValueSampler` subclasses `DuckDbValueSampler` and overrides only four
dialect hooks (`_as_text`, `_ratio`, `_real`, `_contains`). It does not copy the
overlap, distinctness, dependency and delimiter queries. The DuckDB sampler's SQL
was proven unchanged by rerunning its own test suite. That is rung 2 done right,
and the proof is what made it safe.

## Never cut

The repository's rules (listed in `pr-review`) come first. A simplification that
breaks one of them is not a simplification. Do not flag or suggest removing:

- **"Couldn't check" kept apart from "no".** A sampler returning `None`,
  `KeyProfile.not_evaluated`, the `budget_exhausted` stat and every "declined"
  reason. Collapsing `None` into `0.0`, `False` or an empty list looks tidy, but
  it turns an unanswered question into a negative answer.
- **The two-pass overlap.** `_conclusive`, `_better` and `Overlap.normalized` in
  `fk_inference.py` run a strict comparison, then a case- and padding-insensitive
  one only when the strict one is inconclusive (#12). The referenced side is
  deliberately not sampled (#10). A "sampled parent" looks like an optimisation,
  but it scores a valid key into a large table near zero.
- **Credential handling.** Snowflake's `_safe_connection_error`, every
  `raise … from None` (the driver's error can echo the password, #5), and
  `providers/openrouter_provider._scrub_secrets`. `from None` reads like a lint
  smell, but it is deliberate.
- **Read-only access and bounds.** DuckDB's `read_only=True`, SQLite's
  `connect_read_only` (`mode=ro`, and a missing file is an error rather than a
  new database), and the samplers' row limits, query budgets and statement
  timeouts.
- **Identifier quoting and bound parameters.** Every `_qi` and `_qt`, every
  `?` or `%s` parameter, and SQLite's pragma functions taking the table name as a
  bound parameter. There are three quoting rules across five `_qi` copies:
  double quotes (Postgres, DuckDB), backticks (MySQL, Databricks) and brackets
  (SQL Server). Merge them only if each dialect keeps its exact escaping.
- **The fingerprint.** `metadata._without_temporal` and the `*_TEMPORAL_FIELDS`
  lists. Any change to what a schema serializes (a field, a key in `extra`, a
  default) changes `fingerprint_physical_schema`. That is a release decision, not
  a cleanup.
- **Stdout carries only the bundle.** Logs go to stderr in `cli.py` and
  `mcp_server.py` (#38, `tests/test_cli_stdout_is_only_the_bundle.py`).

Two more rules decide whether something counts as a finding:

- **Converge, don't fork.** `fk_inference.py`'s inference core (candidates,
  scoring, the sampler fold, `InferenceOptions`) is hand-ported to
  arango-schema-analyzer (its divergences are marked `ARANGO:`), and
  `r2g.fk_inference` re-exports it. A simplification there needs porting too, so
  say so in the finding. Code that looks similar between the two repos on purpose
  is not a duplication finding.
- **Wiring over deletion.** A symbol in `__all__`, or one that r2g, AOE or
  contextual-data-fabric imports, is public API even with no caller here. Before
  calling something dead, grep the siblings (`../r2g`, `../arango-schema-analyzer`,
  `../arango-ontoextract`, `../contextual-data-fabric`) as well as this package
  and its tests.

## Whole-repo audit protocol (`/code-quality audit`)

1. Read every file under `relational_schema_analyzer/` in full. Skip `tests/` and
   `docs/` except to find callers. The package was about 14,200 lines across 49
   files when this skill was written. `fk_inference.py` is 2,674 of them, mostly
   the eight value samplers, so read it in parts.
2. Map the paths many callers depend on: `create_connector` and the connectors,
   `infer_foreign_keys` with `_apply_sampler`, `profile_primary_keys`, `baseline.py`,
   and `fingerprint_physical_schema`. Edge plumbing includes `providers/`,
   `refine.py` and `mcp_server.py`.
3. Check each file against the ladder. Flag logic written again when it already
   exists, duplicated blocks that should be one helper, dead code (see "Wiring over
   deletion"), and abstractions broader than any caller needs.
4. Rank findings by reach and impact, not file order. A duplication on the
   sampler or connector path outranks one in a provider adapter.
5. Report the places that already hold the line too: reuse done right, and two
   similar-looking things deliberately kept apart because merging them would cost
   something real.

## Diff-scoped protocol (`/code-quality review <target>`)

Apply the same ladder to the lines the change touches and the code they call into,
not the whole repo. For a PR, run it after `pr-review`'s correctness pass, and
review only what the PR adds on top of its base.

## Proving a simplification

A simplification must change no output. Before calling one done:

- Run the unit suite and `tests/test_golden_csv.py`.
- For a sampler or connector SQL change, render the SQL before and after and
  compare it per dialect. Run that dialect's tests, and run it live where
  `pr-review` says CI is blind (SQL Server, and Snowflake via fakesnow only).
- Compare `fingerprint_physical_schema` on the conformance fixtures before and
  after. If it moves, it is not a cleanup.

## Report

Plain language, in CAR form for each finding:
- **Context:** the file and line range, and what the code does today.
- **Action:** the rung it should sit on, and the change.
- **Result:** what becomes simpler, and what proves nothing else changed.

Lead with the highest-reach finding. Say which findings were verified by running
something, and which were only read. File the findings you are not fixing now as
issues on `arango-solutions-org/relational-schema-analyzer`. Don't leave them in
the chat.

## History

- **2026-11-23: skill ported from arango-query-core.** Leads for the first audit,
  each checked only as far as stated:
  - `DuckDbConnector._build_table` and `SqliteConnector._build_table` are the
    same function, except that SQLite passes no comments (compared with `diff`).
    `databricks_source.py` and `osi.py` have their own `_build_table` too. Check
    whether one builder in `connectors/base.py` could serve all four, and whether
    Postgres, MySQL and SQL Server already share one.
  - `create_value_sampler` calls `normalize_source_type(source_type)` five times
    in one `if` chain (counted). Rung 5: normalize once.
  - Five `_qi` copies in `fk_inference.py`, with three quoting rules (above).
    This is never-cut code: merge per rule only.
  - The DuckDB/SQLite hook pattern could extend to the Postgres, MySQL,
    SQL Server and Databricks samplers (not checked). Their overlap SQL differs by
    dialect, and Snowflake's value rendering differs from Python's (#5), so prove
    each one with rendered SQL and a live run.
