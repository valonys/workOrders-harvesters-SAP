"""Double-click entry point: opens the desktop app with no console window."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from iw29_export.cli import main  # noqa: E402 - path set up above

if __name__ == "__main__":
    raise SystemExit(main(["gui"]))
