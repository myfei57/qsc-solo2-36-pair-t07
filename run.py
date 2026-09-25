"""Start the service from a checkout without installing it first."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from line_control.__main__ import main  # noqa: E402 - the path is set up above


if __name__ == "__main__":
    raise SystemExit(main())
