import sys
from pathlib import Path

# Make the src-layout package importable without requiring installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
