"""Entry point required by the course template: generate one split of the dataset.

Equivalent to `python -m sqltr build-dataset ...` and takes the same arguments; writes
`<out>/instances.jsonl` and needs no database. It puts the repository root on sys.path, so it runs
without installing the package. See case/dataset/README.md.
Example: python case/dataset/generate.py --split train --families 250 --seed 20261101 --out data/train
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqltr.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main(["build-dataset", *sys.argv[1:]])
