"""Primary-key profiling against a live warehouse (opt-in; spends credits).

Targets r2g's constraint-free Customer 360 rehearsal schema, which declares no
keys in Snowflake; its reviewed key overlay names the five primary keys. Measured
2026-10-02: 5/5 rediscovered in 5 queries.

Run: RUN_INTEGRATION=1 RSA_SNOWFLAKE_DSN='snowflake://USER:@ACCOUNT/CDF_FORGE/R2G_CUSTOMER_360?...' pytest ...
"""

from __future__ import annotations

import os

import pytest

from relational_schema_analyzer.connectors.snowflake import SnowflakeConnector
from relational_schema_analyzer.fk_inference import SnowflakeValueSampler
from relational_schema_analyzer.key_profiling import profile_primary_keys

_DSN = os.environ.get("RSA_SNOWFLAKE_DSN")
pytestmark = [
    pytest.mark.skipif(
        os.environ.get("RUN_INTEGRATION") != "1", reason="RUN_INTEGRATION=1 not set"
    ),
    pytest.mark.skipif(not _DSN, reason="RSA_SNOWFLAKE_DSN not set"),
]

REVIEWED = {
    "ACCOUNTS": ("ACCOUNT_ID",),
    "CONTACTS": ("CONTACT_ID",),
    "EMAIL_EVENTS": ("EMAIL_EVENT_ID",),
    "ZOOM_TELEMETRY": ("MEETING_ID",),
    "HEALTH_SIGNALS": ("SIGNAL_ID",),
}


def test_reviewed_primary_keys_are_rediscovered_from_data():
    schema = SnowflakeConnector(_DSN).get_schema()
    assert not any(t.primary_key for t in schema.tables.values()), "fixture must declare no keys"
    with SnowflakeValueSampler(_DSN, max_queries=40, statement_timeout_s=30) as probe:
        profile = profile_primary_keys(schema, probe, tables=list(REVIEWED))
        for table, key in REVIEWED.items():
            best = profile.best(table)
            assert best is not None and best.columns == key, (table, best)
        assert profile.not_evaluated == {}
        # Each table was small enough for the sample to cover it: one query each.
        assert probe.stats["queries_run"] == len(REVIEWED)
