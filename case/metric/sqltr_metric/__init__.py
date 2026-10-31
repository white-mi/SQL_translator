"""Metric package for SQL script translation: isolation, execution, normalization, comparison.

This package is the single owner of the comparison rules. The dataset builder imports it to
compute references, so references and scores use exactly the same normalization.
"""

METRIC_VERSION = "sqltr-metric-v1"
