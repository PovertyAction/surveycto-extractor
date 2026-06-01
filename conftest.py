"""pytest bootstrap: put the repo root on sys.path so tests can import the
top-level packages (`transformers`, `generators`, …) regardless of the
directory pytest is invoked from. The repo has no installable package, so
this stands in for an editable install during testing."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
