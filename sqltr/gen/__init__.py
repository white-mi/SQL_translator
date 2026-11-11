"""Script generator: scenario templates rendered in two dialects with identical semantics.

`templates` holds the 14 scenario templates (a parameter sampler and a renderer each), `render`
the class `R` that spells every construct in Greenplum or Trino, and `build` turns sampled families
into the instances of one split. Run it as `python -m sqltr build-dataset`, or call
`build.build_split` and `build.write_instances` from Python.

Operator only: the paired renderings are correct translations, so this package never enters the
researcher workspace (ADR 0009).
"""
