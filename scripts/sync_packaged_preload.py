#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.sync_packaged_rust_artifacts import (
    main,
)

if __name__ == "__main__":
    main()
