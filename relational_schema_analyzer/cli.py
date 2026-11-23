"""Command-line interface: ``snapshot`` / ``analyze`` / ``owl``.

Mirrors the ArangoDB analyzer's CLI shape (DESIGN §6). Each subcommand obtains a
:class:`PhysicalSchema` either by introspecting a live source (``--source`` +
``--url``) or by loading a previously captured snapshot (``--from-snapshot
physical.json``), then emits JSON / OWL to a file or stdout.

    relational-schema-analyzer snapshot --source postgresql --url ... -o physical.json
    relational-schema-analyzer analyze  --from-snapshot physical.json --pretty
    relational-schema-analyzer owl      --source postgresql --url ... --format turtle
    relational-schema-analyzer r2rml    --source postgresql --url ...

``--overlay keys.json`` may be added to any of them to merge human-declared keys into a
schema whose source declares none (see ``overlay.py``).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import __version__
from .analyzer import RelationalSchemaAnalyzer
from .connectors import SUPPORTED_SOURCE_TYPES, create_connector
from .r2rml_export import (
    DEFAULT_R2RML_DATA_IRI,
    DEFAULT_R2RML_MAPPING_IRI,
    export_r2rml_turtle,
)
from .owl_export import (
    DEFAULT_OWL_BASE_IRI,
    DEFAULT_OWL_PHYSICAL_IRI,
    export_owl_jsonld,
    export_owl_turtle,
)
from .bitemporal import PriorRun, stamp_bitemporal
from .log import keep_logs_off_stdout
from .overlay import OverlayError, apply_key_overlay, load_key_overlay
from .types import PhysicalSchema


def _add_source_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--source", help="Source type: " + " | ".join(SUPPORTED_SOURCE_TYPES))
    p.add_argument(
        "--url",
        help="Connection string / DSN, or a path for file sources "
        "(CSV directory, DuckDB / SQLite database, dbt manifest, OSI file)",
    )
    p.add_argument(
        "--schema",
        default="public",
        help="Source schema / namespace (default: public; SQL Server folds to dbo)",
    )
    p.add_argument("--delimiter", default=",", help="CSV delimiter (csv source only)")
    p.add_argument(
        "--no-header",
        action="store_true",
        help="CSV files have no header row (csv source only)",
    )
    p.add_argument(
        "--from-snapshot",
        metavar="FILE",
        help="Load a previously captured physical.json instead of introspecting a live source",
    )
    p.add_argument(
        "--prior-run",
        metavar="FILE",
        help=(
            "A previous run's bundle (or its metadata block) — enables the bitemporal "
            "safety rule: a catalog change-timestamp is trusted only when the schema "
            "fingerprint actually changed, otherwise the earlier valid_from is carried "
            "forward. Without it, catalog dates are reported as-is."
        ),
    )
    p.add_argument(
        "--overlay",
        metavar="FILE",
        help=(
            "Merge human-declared primary/foreign/unique keys from a JSON or YAML overlay "
            "(for sources whose catalog declares none, e.g. BigQuery, Glue, Hive)"
        ),
    )


def _add_output_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("-o", "--out", metavar="FILE", help="Write output to FILE (default: stdout)")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")


def _load_physical(args: argparse.Namespace) -> PhysicalSchema:
    physical = _introspect(args)
    # Applied last, and identically for live and captured schemas: an overlay describes the
    # *schema*, not how it was obtained, so `snapshot --overlay` and
    # `analyze --from-snapshot --overlay` must agree.
    if getattr(args, "overlay", None):
        try:
            physical = apply_key_overlay(physical, load_key_overlay(args.overlay))
        except OverlayError as err:
            raise SystemExit(f"error: {err}") from err
    # Bitemporal stamping runs last and unconditionally: RSA is the only component that can
    # observe valid time (DESIGN-ADDENDUM-bitemporal), so an unstamped emission throws away
    # information nothing downstream can recover. It runs after the overlay because an
    # overlay changes the structure, and therefore the fingerprint the §2.3 rule compares.
    return stamp_bitemporal(physical, prior=_load_prior_run(args))


def _load_prior_run(args: argparse.Namespace) -> "PriorRun | None":
    """Read ``--prior-run`` — a previous bundle or metadata block — if given.

    RSA is stateless, so the previous observation has to come from the caller. Without one
    RSA cannot tell a real DDL change from Snowflake's ``LAST_ALTERED`` moving on DML, and
    says so by reporting the catalog value as ``catalog`` rather than pretending to more
    certainty than it has.
    """
    path = getattr(args, "prior_run", None)
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as err:
        raise SystemExit(f"error: could not read --prior-run {path}: {err}") from err
    prior = PriorRun.from_metadata(payload)
    if prior is None:
        raise SystemExit(
            f"error: --prior-run {path} has no physicalSchemaFingerprint; "
            "pass a previous bundle or its metadata block"
        )
    return prior


def _introspect(args: argparse.Namespace) -> PhysicalSchema:
    if args.from_snapshot:
        return PhysicalSchema.load_from_file(args.from_snapshot)
    if not args.source or not args.url:
        raise SystemExit(
            "error: provide either --from-snapshot FILE or both --source and --url"
        )
    source_params = {"delimiter": args.delimiter, "has_header": not args.no_header}
    connector = create_connector(
        args.source, args.url, schema_name=args.schema, source_params=source_params
    )
    return connector.get_schema()


def _emit(payload: str, out: str | None) -> None:
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(payload if payload.endswith("\n") else payload + "\n")
    else:
        sys.stdout.write(payload if payload.endswith("\n") else payload + "\n")


def _dump_json(obj: Any, *, pretty: bool) -> str:
    if pretty:
        return json.dumps(obj, indent=2, ensure_ascii=False)
    return json.dumps(obj, ensure_ascii=False)


def _cmd_snapshot(args: argparse.Namespace) -> int:
    physical = _load_physical(args)
    _emit(_dump_json(physical.model_dump(mode="json"), pretty=args.pretty), args.out)
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    physical = _load_physical(args)
    bundle = RelationalSchemaAnalyzer().analyze(physical).to_bundle()
    _emit(_dump_json(bundle, pretty=args.pretty), args.out)
    return 0


def _cmd_owl(args: argparse.Namespace) -> int:
    physical = _load_physical(args)
    analysis = RelationalSchemaAnalyzer().analyze(physical)
    if args.format == "turtle":
        payload = export_owl_turtle(
            analysis, base_iri=args.iri_base, phys_iri=args.phys_iri_base
        )
    else:
        payload = _dump_json(
            export_owl_jsonld(analysis, base_iri=args.iri_base, phys_iri=args.phys_iri_base),
            pretty=args.pretty,
        )
    _emit(payload, args.out)
    return 0


def _cmd_r2rml(args: argparse.Namespace) -> int:
    physical = _load_physical(args)
    analysis = RelationalSchemaAnalyzer().analyze(physical)
    _emit(
        export_r2rml_turtle(
            analysis,
            base_iri=args.iri_base,
            data_iri=args.data_iri_base,
            mapping_iri=args.mapping_iri_base,
        ),
        args.out,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="relational-schema-analyzer",
        description="Analyze a relational schema into a conceptual model + OWL.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_snap = sub.add_parser("snapshot", help="Introspect a source and emit physical schema JSON")
    _add_source_args(p_snap)
    _add_output_args(p_snap)
    p_snap.set_defaults(func=_cmd_snapshot)

    p_analyze = sub.add_parser("analyze", help="Emit the conceptual bundle JSON")
    _add_source_args(p_analyze)
    _add_output_args(p_analyze)
    p_analyze.set_defaults(func=_cmd_analyze)

    p_owl = sub.add_parser("owl", help="Emit OWL (Turtle or JSON-LD)")
    _add_source_args(p_owl)
    _add_output_args(p_owl)
    p_owl.add_argument(
        "--format", choices=["turtle", "jsonld"], default="turtle", help="OWL serialization"
    )
    p_owl.add_argument("--iri-base", default=DEFAULT_OWL_BASE_IRI, help="Conceptual IRI base")
    p_owl.add_argument(
        "--phys-iri-base", default=DEFAULT_OWL_PHYSICAL_IRI, help="Physical-annotation IRI base"
    )
    p_owl.set_defaults(func=_cmd_owl)

    p_r2rml = sub.add_parser(
        "r2rml", help="Emit a W3C R2RML mapping document (Turtle)"
    )
    _add_source_args(p_r2rml)
    _add_output_args(p_r2rml)
    # Shares --iri-base with `owl` on purpose: the mapping has to populate the
    # same ontology the OWL export declares.
    p_r2rml.add_argument(
        "--iri-base", default=DEFAULT_OWL_BASE_IRI, help="Conceptual IRI base (match `owl`)"
    )
    p_r2rml.add_argument(
        "--data-iri-base", default=DEFAULT_R2RML_DATA_IRI, help="Base for row subject IRIs"
    )
    p_r2rml.add_argument(
        "--mapping-iri-base",
        default=DEFAULT_R2RML_MAPPING_IRI,
        help="Base for the TriplesMap resources themselves",
    )
    p_r2rml.set_defaults(func=_cmd_r2rml)

    return parser


def main(argv: list[str] | None = None) -> int:
    keep_logs_off_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
