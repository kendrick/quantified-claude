import sys
from pathlib import Path

# Make the repo root importable so tests can do `from skill_usage import ...`
# without installing the package. This file is auto-loaded by pytest before any
# test module runs.
sys.path.insert(0, str(Path(__file__).parent.parent))
