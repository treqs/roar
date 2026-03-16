from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    output_path = Path(sys.argv[1])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    message = "ROAR_OSMO_BASIC_OK"
    print(message)
    output_path.write_text(f"{message}\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
