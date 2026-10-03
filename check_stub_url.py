"""Exit 0 when stub.py has a real DOWNLOAD_URL, else exit 1.

Used by build.bat to decide whether the stub installer can be built.
"""

import pathlib
import sys

stub = pathlib.Path(__file__).resolve().parent / "stub.py"
sys.exit(1 if "REPLACE-ME" in stub.read_text(encoding="utf-8") else 0)
