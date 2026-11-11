"""Dataset builder: families -> instances (both directions) -> split files.

Split policy (case/dataset/README.md):
- a family is one template with fixed parameters; all its style variants and both directions go
  to the same split, so a paraphrase never leaks from train into public/private;
- train families come from a public seed (reproducible from the repository);
- public/private families come from secret operator seeds and must not collide with train
  families (collisions are resampled), so the three splits share one distribution and are disjoint.

`python -m sqltr build-dataset` calls `build_split` and `write_instances`; it fills `exclude` with
the families of the `instances.jsonl` files given in `--exclude`. The written file holds the
oracle translations, so it stays with the operator (ADR 0009).
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass

from .render import R, sample_style
from .templates import TEMPLATES, render

DIRECTIONS = {"gp2trino": ("gp", "trino"), "trino2gp": ("trino", "gp")}


@dataclass
class Instance:
    """One example of a split: a source script in one direction plus its oracle translation.

    Written one per line to `instances.jsonl` by `write_instances`; `references.build_references`
    turns it into the arena example and its `meta.jsonl` row, `references.self_check` executes both
    scripts. Generator instances come from `build_split`, holdout ones from
    `holdout.holdout_instances`.

    Attributes:
        id: Example id. Train: readable `tr-<direction>-<family>-v<variant>`; public and private:
            opaque `<pu|pr>-<hash>`, so the id does not reveal the template; holdout:
            `ho-<direction>-<name>`.
        split: "train", "public", "private" or "holdout" (tests also use other names).
        direction: "gp2trino" or "trino2gp"; see `DIRECTIONS`.
        family: `family_id` of the template and parameters (`holdout-<name>` for the holdout).
        template: Template name (`holdout:<name>` for the holdout).
        variant: Index of the style variant within the family.
        params: The sampler's parameters (empty for the holdout).
        style: The `sample_style` knobs shared by both renderings (empty for the holdout).
        source_dialect: "gp" or "trino": dialect of `source_script`, the solver's input.
        target_dialect: Dialect of `oracle_script`.
        oracle_script: The paired rendering in the target dialect, a known-correct translation.
            Never shown to the researcher.
        outputs: Logical names of the tables the script creates, such as `wrk.rfm`, without the
            `iceberg.` prefix; the metric hashes exactly these.
        features: Sorted divergence tags of both renderings, used for per-class breakdowns.
    """

    id: str
    split: str
    direction: str
    family: str
    template: str
    variant: int
    params: dict
    style: dict
    source_dialect: str
    target_dialect: str
    source_script: str
    oracle_script: str
    outputs: list[str]
    features: list[str]


def family_id(template: str, params: dict) -> str:
    """Return the stable family id `<template>-<8 hex digits>` of a template and its parameters.

    The digest covers the JSON of both with sorted keys, so equal parameters give the same id in
    any process; `sample_families` uses it to deduplicate and to exclude other splits' families.
    """
    digest = hashlib.sha1(json.dumps([template, params], sort_keys=True).encode()).hexdigest()[:8]
    return f"{template}-{digest}"


def _example_id(split: str, direction: str, family: str, variant: int, secret: str) -> str:
    """Return the example id: readable for train, an opaque keyed hash for every other split.

    Outside train the id is `<first two letters of split>-<20 hex digits of SHA-256>` over the
    operator's `secret` and the example's coordinates, so an id reveals neither the template nor
    the family; with an empty secret anyone holding the generator can recompute it.
    """
    if split == "train":
        return f"tr-{direction}-{family}-v{variant}"
    digest = hashlib.sha256(f"{secret}|{split}|{direction}|{family}|{variant}".encode()).hexdigest()[:20]
    return f"{split[:2]}-{digest}"


def sample_families(n: int, seed: int, exclude: set[str] | None = None) -> list[tuple[str, dict]]:
    """Draw `n` distinct families, each a `(template name, params)` pair, from `seed`.

    Each draw picks a template uniformly and calls its sampler; a family already drawn or listed
    in `exclude` (family ids) is skipped and another is drawn. The result depends only on the
    arguments, the order of `TEMPLATES` and the samplers' draws: changing any of those changes
    every split built from the same seed.

    Raises:
        RuntimeError: Fewer than `n` new families were found in `200 * n` draws.
    """
    r = random.Random(seed)
    exclude = set(exclude or ())
    seen: set[str] = set()
    families: list[tuple[str, dict]] = []
    attempts = 0
    while len(families) < n:
        attempts += 1
        if attempts > n * 200:
            raise RuntimeError(f"parameter space exhausted after {len(families)} families")
        template = TEMPLATES[(len(families) + r.randrange(len(TEMPLATES))) % len(TEMPLATES)]
        params = template.sample(r)
        fid = family_id(template.name, params)
        if fid in seen or fid in exclude:
            continue
        seen.add(fid)
        families.append((template.name, params))
    return families


def build_split(split: str, n_families: int, seed: int, variants: int = 1,
                exclude: set[str] | None = None, secret: str = "") -> list[Instance]:
    """Build every instance of one split: `n_families * variants * 2` instances, deterministic.

    For each family from `sample_families(n_families, seed, exclude)`, draw `variants` styles from
    a second generator seeded from `seed`, render each style in both dialects, and emit one
    instance per direction, whose source and oracle are the two renderings swapped. All variants
    and both directions of a family therefore land in this split.

    Args:
        split: Split name; it changes only the ids and the `split` field, not what is sampled.
        seed: The split's seed: public for train, an operator secret for public and private.
        exclude: Family ids of other splits that must not reappear here.
        secret: Operator key for the opaque ids of non-train splits (see `_example_id`).
    """
    r = random.Random(seed ^ 0x5EED)
    instances: list[Instance] = []
    for template, params in sample_families(n_families, seed, exclude):
        fid = family_id(template, params)
        for variant in range(variants):
            style = sample_style(r)
            renders = {d: render(template, params, d, style) for d in ("gp", "trino")}
            features = sorted(set(renders["gp"][2]) | set(renders["trino"][2]))
            for direction, (src, dst) in DIRECTIONS.items():
                instances.append(Instance(
                    id=_example_id(split, direction, fid, variant, secret), split=split, direction=direction,
                    family=fid, template=template, variant=variant, params=params, style=style,
                    source_dialect=src, target_dialect=dst, source_script=renders[src][0],
                    oracle_script=renders[dst][0], outputs=renders[src][1], features=features))
    return instances


def write_instances(instances: list[Instance], path) -> None:
    """Write instances to `path` as UTF-8 JSONL, one per line, replacing the file.

    The file holds oracle translations, so it belongs to the operator (ADR 0009).
    """
    with open(path, "w", encoding="utf-8") as out:
        for inst in instances:
            out.write(json.dumps(asdict(inst), ensure_ascii=False) + "\n")


def read_instances(path) -> list[Instance]:
    """Read instances written by `write_instances`, skipping blank lines.

    A line whose keys differ from the `Instance` fields raises `TypeError`.
    """
    with open(path, encoding="utf-8") as src:
        return [Instance(**json.loads(line)) for line in src if line.strip()]


__all__ = ["DIRECTIONS", "Instance", "R", "build_split", "family_id", "read_instances", "sample_families",
           "write_instances"]
