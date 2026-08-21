"""Pulizia dati di test residui (catering TEST_*, utente Test Prova)."""
import os

import requests
from dotenv import dotenv_values

BASE = (os.environ.get("REACT_APP_BACKEND_URL") or dotenv_values("/app/frontend/.env")["REACT_APP_BACKEND_URL"]).rstrip("/") + "/api"
tok = requests.post(f"{BASE}/auth/login", json={"email": "admin@impastocatering.it", "password": "Impasto2026!"}, timeout=60).json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}

comp = requests.get(f"{BASE}/compensi", headers=H, timeout=120).json()
paid = {r["catering_id"]: r["id"] for d in comp["dipendenti"] for r in d["righe"] if r["pagato"]}

cat = requests.get(f"{BASE}/caterings", headers=H, timeout=120).json()
items = cat if isinstance(cat, list) else cat.get("items", [])
for c in items:
    if not c.get("titolo", "").startswith("TEST_"):
        continue
    cid = c.get("id") or c.get("_id")
    if cid in paid:  # annulla il pagamento (toggle) per poter eliminare
        requests.post(f"{BASE}/compensi/{paid[cid]}/paga", headers=H, timeout=120)
    r = requests.delete(f"{BASE}/caterings/{cid}", headers=H, timeout=60)
    print("delete", c["titolo"], r.status_code)

for u in requests.get(f"{BASE}/users", headers=H, timeout=60).json():
    if u.get("email", "").startswith("test.prova@") or u.get("email", "").startswith("TEST_"):
        r = requests.delete(f"{BASE}/users/{u['id']}", headers=H, timeout=60)
        print("delete user", u["email"], r.status_code)
