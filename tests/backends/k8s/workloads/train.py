"""Roar-unaware synthetic trainer for the k8s smoke test.

Reads a CSV dataset, "trains" a scalar weight, and writes a model
artifact plus metrics. Deliberately torch-free so Tier-1 runs stay
fast on a slim CPU image; distributed torchrun workloads arrive with
the Tier-2 harness.
"""

import hashlib
import json
import sys
from pathlib import Path


def main() -> None:
    dataset = Path(sys.argv[1] if len(sys.argv) > 1 else "dataset.csv")
    rows = dataset.read_text(encoding="utf-8").strip().splitlines()

    weight = 0.0
    for _epoch in range(3):
        for row in rows[1:]:
            x_raw, y_raw = row.split(",")[:2]
            x, y = float(x_raw), float(y_raw)
            weight += 0.01 * (y - weight * x) * x

    digest = hashlib.blake2b(f"{weight:.9f}".encode()).digest()
    Path("model.bin").write_bytes(digest * 8)
    Path("metrics.json").write_text(
        json.dumps({"rows": len(rows) - 1, "weight": weight}) + "\n",
        encoding="utf-8",
    )
    print(f"trained weight={weight:.6f} over {len(rows) - 1} rows")


if __name__ == "__main__":
    main()
