import sys
from pathlib import Path

# Make `from backend import …` work when pytest runs from the launcher root.
APP_DIR = Path(__file__).resolve().parents[1]      # .../app
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
