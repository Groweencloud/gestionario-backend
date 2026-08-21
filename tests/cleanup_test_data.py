"""One-off cleanup of TEST_ / test_reg_ / ui_ leftovers created during testing."""
import requests
from dotenv import dotenv_values

API = dotenv_values("/app/frontend/.env")["REACT_APP_BACKEND_URL"].rstrip("/") + "/api"
s = requests.Session()
tok = s.post(f"{API}/auth/login", json={"email": "admin@impastocatering.it", "password": "Impasto2026!"}).json()["access_token"]
h = {"Authorization": f"Bearer {tok}"}

users = s.get(f"{API}/users", headers=h).json()
for u in users:
    if u["email"].startswith(("test_reg_", "ui_reg_", "ui_rej_", "lock_")):
        r = s.delete(f"{API}/users/{u['id']}", headers=h)
        print("deleted user", u["email"], r.status_code)

cats = s.get(f"{API}/caterings", headers=h).json()
for c in cats:
    if c["titolo"].startswith("TEST_"):
        r = s.delete(f"{API}/caterings/{c['id']}", headers=h)
        print("deleted catering", c["titolo"], r.status_code)
