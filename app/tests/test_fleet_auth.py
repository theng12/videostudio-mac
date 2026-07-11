import stat
import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend import fleet_auth
from backend.main import FLEET_TOKEN, app


class FleetAuthTests(unittest.TestCase):
    def test_public_and_protected_routes(self):
        client = TestClient(app)
        self.assertEqual(client.get("/api/health").status_code, 200)
        self.assertEqual(client.get("/api/capabilities").status_code, 200)
        self.assertEqual(client.get("/api/catalog").status_code, 401)
        authed = TestClient(app, headers={"X-Studio-Token": FLEET_TOKEN})
        self.assertEqual(authed.get("/api/catalog").status_code, 200)

    def test_cross_origin_write_rejected_even_with_token(self):
        client = TestClient(app, headers={"X-Studio-Token": FLEET_TOKEN})
        response = client.delete("/api/downloads", headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)

    def test_loopback_and_private_shared_token(self):
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        self.assertTrue(fleet_auth.is_loopback(request))
        source = fleet_auth.HUB_TOKEN_FILE if fleet_auth.HUB_TOKEN_FILE.exists() else fleet_auth.SHARED_TOKEN_FILE
        self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()

