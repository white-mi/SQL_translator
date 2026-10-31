"""Dataset builder: families -> instances (both directions) -> split files.

Split policy (case/dataset/README.md):
- a family is one template with fixed parameters; all its style variants and both directions go
  to the same split, so a paraphrase never leaks from train into public/private;
- train families come from a public seed (reproducible from the repository);
- public/private families come from secret operator seeds and must not collide with train
  families (collisions are resampled), so the three splits share one distribution and are disjoint.
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
    digest = hashlib.sha1(json.dumps([template, params], sort_keys=True).encode()).hexdigest()[:8]
    return f"{template}-{digest}"


def _example_id(split: str, direction: str, family: str, variant: int, secret: str) -> str:
    if split == "train":
        return f"tr-{direction}-{family}-v{variant}"
    digest = hashlib.sha256(f"{secret}|{split}|{direction}|{family}|{variant}".encode()).hexdigest()[:20]
    return f"{split[:2]}-{digest}"


def sample_families(n: int, seed: int, exclude: set[str] | None = None) -> list[tuple[str, dict]]:
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
    with open(path, "w", encoding="utf-8") as out:
        for inst in instances:
            out.write(json.dumps(asdict(inst), ensure_ascii=False) + "\n")


def read_instances(path) -> list[Instance]:
    with open(path, encoding="utf-8") as src:
        return [Instance(**json.loads(line)) for line in src if line.strip()]


__all__ = ["DIRECTIONS", "Instance", "R", "build_split", "family_id", "read_instances", "sample_families",
           "write_instances"]
