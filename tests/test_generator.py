"""Generator properties: determinism, disjoint splits, executable Greenplum renderings.

Also: a family keeps all its variants and both directions in one split, public ids do not reveal
the template, and paired renderings name the same output tables. Only the execution test needs
PostgreSQL (`pg_cfg`).
"""

import random

from sqltr.gen.build import build_split, family_id, sample_families
from sqltr.gen.render import sample_style
from sqltr.gen.templates import TEMPLATES, render
from sqltr_metric.evaluate import run_on_states


def test_same_seed_same_dataset():
    a = build_split("train", 30, seed=123)
    b = build_split("train", 30, seed=123)
    assert [(i.id, i.source_script) for i in a] == [(i.id, i.source_script) for i in b]


def test_family_keeps_variants_and_directions_together():
    instances = build_split("train", 10, seed=5, variants=2)
    by_family = {}
    for inst in instances:
        by_family.setdefault(inst.family, set()).add((inst.direction, inst.variant))
    assert all(v == {("gp2trino", 0), ("gp2trino", 1), ("trino2gp", 0), ("trino2gp", 1)} for v in by_family.values())


def test_excluded_families_never_reappear():
    train = {family_id(t, p) for t, p in sample_families(200, seed=1)}
    public = {family_id(t, p) for t, p in sample_families(100, seed=2, exclude=train)}
    assert not train & public


def test_public_ids_do_not_reveal_templates():
    inst = build_split("public", 3, seed=9, secret="s3cret")[0]
    assert inst.template not in inst.id


def test_paired_renderings_have_same_outputs():
    r = random.Random(4)
    for template in TEMPLATES:
        params, style = template.sample(r), sample_style(r)
        assert render(template.name, params, "gp", style)[1] == render(template.name, params, "trino", style)[1]


def test_greenplum_renderings_execute_on_all_states(pg_cfg):
    r = random.Random(42)
    for template in TEMPLATES:
        params, style = template.sample(r), sample_style(r)
        script, outputs, _ = render(template.name, params, "gp", style)
        runs = run_on_states(script, "gp", outputs, pg_cfg)
        assert all(run.ok for run in runs), (template.name, [(x.state, x.error) for x in runs if not x.ok])
        assert all(h is not None for run in runs for h in run.hashes.values()), template.name


def test_chain_scripts_cross_reference_member_outputs():
    """A composite script carries both member scripts and the linker over their outputs."""
    r = random.Random(7)
    for template in [t for t in TEMPLATES if t.name.startswith("chain_")]:
        params, style = template.sample(r), sample_style(r)
        gp = render(template.name, params, "gp", style)
        trino = render(template.name, params, "trino", style)
        assert gp[1] == trino[1] and len(gp[1]) >= 3  # two members plus the linker table
        for output in gp[1]:
            assert output.split(".", 1)[1] in gp[0], (template.name, output)
        assert gp[0].count(";") >= 3  # several statements, not a single-select script
        assert params["a"] != params["b"]


def test_chain_families_are_deterministic_and_distinct():
    """Composite draws are reproducible and never chain a template with itself."""
    from sqltr.gen.chain import STORE
    sampler = next(t for t in TEMPLATES if t.name == "chain_store")
    r = random.Random(11)
    ids = set()
    for _ in range(50):
        params = sampler.sample(r)
        assert params["a"] in STORE and params["b"] in STORE and params["a"] != params["b"]
        ids.add(family_id(sampler.name, params))
    assert len(ids) == 50
    replay = sampler.sample(random.Random(11))
    assert family_id(sampler.name, replay) in ids
