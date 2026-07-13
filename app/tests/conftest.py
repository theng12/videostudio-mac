import sys
from pathlib import Path

import pytest

# Make `from backend import …` work when pytest runs from the launcher root.
APP_DIR = Path(__file__).resolve().parents[1]      # .../app
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point the spend ledger at a throwaway DB and stub settings so tests never
    touch the real spend.db or settings.json (which holds API keys)."""
    from backend import settings as S
    from backend import spend
    monkeypatch.setattr(spend, "DB_FILE", str(tmp_path / "spend.db"))
    store = {
        "providers": {"fal": {"key": "test-key", "paid": True}},
        "spend_caps": {"global": {"daily": 0, "monthly": 0}, "per_provider": {}},
    }
    monkeypatch.setattr(S, "get", lambda k: store.get(k))
    monkeypatch.setattr(S, "set_value", lambda k, v: store.__setitem__(k, v))
    return store
