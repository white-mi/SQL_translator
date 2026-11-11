"""Out-of-search validation pairs: well-formed, disjoint from the generator, GP twins execute.

"Disjoint" means no holdout pair reuses a generator template name; the execution test needs
PostgreSQL (`pg_cfg`).
"""

from sqltr.gen.templates import BY_NAME
from sqltr.holdout import HOLDOUT, holdout_instances


def test_pairs_are_well_formed():
    names = [h["name"] for h in HOLDOUT]
    assert len(names) == len(set(names)) == 12
    for item in HOLDOUT:
        assert item["name"] not in BY_NAME
        for table in item["outputs"]:
            schema, name = table.split(".")
            assert f"{schema}.{name}" in item["gp"]
            assert f"iceberg.{schema}.{name}" in item["trino"]
    instances = holdout_instances()
    assert len(instances) == 24 and len({i.id for i in instances}) == 24
    assert {i.direction for i in instances} == {"gp2trino", "trino2gp"}


def test_gp_twins_execute_on_all_states(pg_cfg):
    from sqltr_metric.evaluate import run_on_states

    for item in HOLDOUT:
        runs = run_on_states(item["gp"], "gp", item["outputs"], pg_cfg)
        assert all(run.ok for run in runs), (item["name"], [r.error for r in runs if not r.ok])
        assert any(rows for run in runs for rows in run.rows.values()), item["name"]
