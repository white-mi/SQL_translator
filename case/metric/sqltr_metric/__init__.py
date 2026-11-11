"""Metric package for SQL script translation: isolation, execution, normalization, comparison.

This package is the single owner of the comparison rules. The dataset builder imports it to
compute references, so references and scores use exactly the same normalization.

Modules: config (settings), engines (PostgreSQL and Trino adapters), sqltext and rewrite (SQL
text handling, isolation, static checks), normalize (canonical table hashes), evaluate (running
a script over the data states, scoring, the score cache). The package lives in case/metric and
imports only once that directory is on sys.path: score.py adds it, and in the repository
importing `sqltr` first does.
"""

# Recorded in every result's statistics and part of the score-cache key (evaluate.score_answer),
# so change it whenever the scoring rules change. Matches "version" in metric.json.
METRIC_VERSION = "sqltr-metric-v1"
