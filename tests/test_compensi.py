"""Backend tests — modulo presenze/compensi/portafoglio (iterazione 3)."""
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
API = f"{base_url.rstrip('/')}/api"


def _creds():
    content = Path("/app/memory/test_credentials.md").read_text(encoding="utf-8")
    email = re.search(r'(?im)^-\s*Email:\s*`([^`]+)`', content).group(1)
    pwd = re.search(r'(?im)^-\s*Password:\s*`([^`]+)`', content).group(1)
    emp_pwd = re.search(r'(?im)^Password:\s*`([^`]+)`', content).group(1)
    return {"admin_email": email, "admin_password": pwd, "emp_password": emp_pwd}


CREDS = _creds()
EMP_EMAIL = "luca.bianchi@impastocatering.it"
# NB: xdist loadscope distribuisce le classi su worker diversi: ogni classe che modifica
# presenze/pagamenti usa un dipendente dedicato per evitare interferenze.
EMP_EMAIL_ADMIN_TESTS = "matteo.esposito@impastocatering.it"


def _login(email, password):
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=30)
    if r.status_code != 200:
        pytest.fail(f"Login failed {email}: {r.status_code} {r.text[:300]}")
    return r.json()


@pytest.fixture(scope="module")
def admin():
    tok = _login(CREDS["admin_email"], CREDS["admin_password"])["access_token"]
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def emp_data():
    return _login(EMP_EMAIL, CREDS["emp_password"])


@pytest.fixture(scope="module")
def emp(emp_data):
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {emp_data['access_token']}", "Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def emp_id(emp_data):
    return emp_data["user"]["id"]


@pytest.fixture(scope="module")
def emp2_data():
    return _login(EMP_EMAIL_ADMIN_TESTS, CREDS["emp_password"])


@pytest.fixture(scope="module")
def emp2(emp2_data):
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {emp2_data['access_token']}", "Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def emp2_id(emp2_data):
    return emp2_data["user"]["id"]


@pytest.fixture(scope="module")
def created_caterings():
    return []


@pytest.fixture(scope="module", autouse=True)
def cleanup(admin, created_caterings):
    yield
    for cid in created_caterings:
        admin.delete(f"{API}/caterings/{cid}", timeout=30)


def _make_catering(admin, created_caterings, compenso=None, days=-3, titolo="TEST_Compensi"):
    d = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")
    payload = {
        "titolo": f"{titolo}", "data": d, "ora_inizio": "18:00", "luogo": "TEST_Palermo",
        "personale_richiesto": 3, "descrizione": "test", "indirizzo": "via test 1",
    }
    if compenso is not None:
        payload["compenso"] = compenso
    r = admin.post(f"{API}/caterings", json=payload, timeout=30)
    assert r.status_code in (200, 201), r.text
    cid = r.json()["id"]
    created_caterings.append(cid)
    return cid


# ---------------------------------------------------------------- GET presenze
class TestPresenze:
    def test_list_presenze_default_compenso(self, admin, created_caterings):
        cid = _make_catering(admin, created_caterings)
        r = admin.get(f"{API}/caterings/{cid}/presenze", timeout=30)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["compenso_base"] == 80.0
        assert isinstance(d["righe"], list) and len(d["righe"]) > 0
        for row in d["righe"]:
            assert set(["user_id", "nome", "cognome", "disponibilita", "presente", "importo", "pagato"]) <= set(row)
            assert row["presente"] is False
            assert row["importo"] == 80.0
            assert row["pagato"] is False

    def test_catering_compenso_overrides_default(self, admin, created_caterings):
        cid = _make_catering(admin, created_caterings, compenso=150)
        r = admin.get(f"{API}/caterings/{cid}/presenze", timeout=30)
        assert r.status_code == 200
        assert r.json()["compenso_base"] == 150.0
        assert all(row["importo"] == 150.0 for row in r.json()["righe"])

    def test_presenze_404_unknown_catering(self, admin):
        r = admin.get(f"{API}/caterings/507f1f77bcf86cd799439011/presenze", timeout=30)
        assert r.status_code == 404

    def test_confirm_presenza_persists_and_custom_importo(self, admin, emp, emp_id, created_caterings):
        cid = _make_catering(admin, created_caterings)
        # default importo
        r = admin.put(f"{API}/caterings/{cid}/presenze/{emp_id}", json={"presente": True}, timeout=30)
        assert r.status_code == 200, r.text
        assert r.json()["importo"] == 80.0 and r.json()["presente"] is True
        g = admin.get(f"{API}/caterings/{cid}/presenze", timeout=30).json()
        row = next(x for x in g["righe"] if x["user_id"] == emp_id)
        assert row["presente"] is True and row["importo"] == 80.0

        # custom importo overrides
        r = admin.put(f"{API}/caterings/{cid}/presenze/{emp_id}", json={"presente": True, "importo": 120}, timeout=30)
        assert r.status_code == 200 and r.json()["importo"] == 120.0
        g = admin.get(f"{API}/caterings/{cid}/presenze", timeout=30).json()
        row = next(x for x in g["righe"] if x["user_id"] == emp_id)
        assert row["importo"] == 120.0

        # wallet reflects it
        w = emp.get(f"{API}/wallet", timeout=30).json()
        assert any(x["catering_id"] == cid and x["importo"] == 120.0 and x["pagato"] is False for x in w["righe"])

        # remove presenza
        r = admin.put(f"{API}/caterings/{cid}/presenze/{emp_id}", json={"presente": False}, timeout=30)
        assert r.status_code == 200
        w = emp.get(f"{API}/wallet", timeout=30).json()
        assert not any(x["catering_id"] == cid for x in w["righe"])

    def test_negative_importo_rejected(self, admin, emp_id, created_caterings):
        cid = _make_catering(admin, created_caterings)
        r = admin.put(f"{API}/caterings/{cid}/presenze/{emp_id}", json={"presente": True, "importo": -10}, timeout=30)
        assert r.status_code == 400, r.text

    def test_unknown_user_404(self, admin, created_caterings):
        cid = _make_catering(admin, created_caterings)
        r = admin.put(f"{API}/caterings/{cid}/presenze/507f1f77bcf86cd799439011", json={"presente": True}, timeout=30)
        assert r.status_code == 404


# ---------------------------------------------------------------- wallet
class TestWallet:
    def test_wallet_totals_consistent(self, admin, emp, emp_id, created_caterings):
        cid = _make_catering(admin, created_caterings, compenso=90)
        admin.put(f"{API}/caterings/{cid}/presenze/{emp_id}", json={"presente": True}, timeout=30)
        w = emp.get(f"{API}/wallet", timeout=30).json()
        assert w["eventi_lavorati"] == len(w["righe"])
        assert round(w["da_riscuotere"] + w["pagato"], 2) == w["totale"]
        assert round(sum(r["importo"] for r in w["righe"] if not r["pagato"]), 2) == w["da_riscuotere"]
        for r in w["righe"]:
            assert r["titolo"] and r["data"]
            assert "_id" not in r
        admin.put(f"{API}/caterings/{cid}/presenze/{emp_id}", json={"presente": False}, timeout=30)

    def test_wallet_excludes_only_available_without_presenza(self, admin, emp, created_caterings):
        cid = _make_catering(admin, created_caterings, days=5)
        r = emp.put(f"{API}/caterings/{cid}/disponibilita", json={"stato": "disponibile"}, timeout=30)
        assert r.status_code == 200, r.text
        w = emp.get(f"{API}/wallet", timeout=30).json()
        assert not any(x["catering_id"] == cid for x in w["righe"]), "compenso senza presenza confermata!"

    def test_wallet_only_own_compensi(self, admin, emp, emp_id, created_caterings):
        """Wallet must be scoped to the caller."""
        cid = _make_catering(admin, created_caterings)
        # confirm for a DIFFERENT employee
        rows = admin.get(f"{API}/caterings/{cid}/presenze", timeout=30).json()["righe"]
        other = next(x for x in rows if x["user_id"] != emp_id)
        admin.put(f"{API}/caterings/{cid}/presenze/{other['user_id']}", json={"presente": True}, timeout=30)
        w = emp.get(f"{API}/wallet", timeout=30).json()
        assert not any(x["catering_id"] == cid for x in w["righe"])


# ---------------------------------------------------------------- compensi admin
class TestCompensiAdmin:
    def test_compensi_overview_and_pay_single(self, admin, emp2, emp2_id, created_caterings):
        cid = _make_catering(admin, created_caterings, compenso=70)
        admin.put(f"{API}/caterings/{cid}/presenze/{emp2_id}", json={"presente": True}, timeout=30)

        c = admin.get(f"{API}/compensi", timeout=30)
        assert c.status_code == 200, c.text
        data = c.json()
        assert isinstance(data["compenso_default"], (int, float)) and data["compenso_default"] > 0
        assert round(sum(d["da_riscuotere"] for d in data["dipendenti"]), 2) == data["totale_da_pagare"]
        assert round(sum(d["pagato"] for d in data["dipendenti"]), 2) == data["totale_pagato"]
        vals = [d["da_riscuotere"] for d in data["dipendenti"]]
        assert vals == sorted(vals, reverse=True), "elenco non ordinato per da_riscuotere"
        mine = next(d for d in data["dipendenti"] if d["user_id"] == emp2_id)
        riga = next(r for r in mine["righe"] if r["catering_id"] == cid)
        assert riga["importo"] == 70.0 and riga["pagato"] is False

        before = emp2.get(f"{API}/wallet", timeout=30).json()

        # pay single row
        p = admin.post(f"{API}/compensi/{riga['id']}/paga", timeout=30)
        assert p.status_code == 200, p.text
        assert p.json()["pagato"] is True

        after = emp2.get(f"{API}/wallet", timeout=30).json()
        assert round(after["da_riscuotere"], 2) == round(before["da_riscuotere"] - 70.0, 2)
        assert round(after["pagato"], 2) == round(before["pagato"] + 70.0, 2)
        assert next(r for r in after["righe"] if r["catering_id"] == cid)["pagato"] is True

        # notifica interna al dipendente
        notes = emp2.get(f"{API}/notifications", timeout=30).json()
        items = notes if isinstance(notes, list) else notes.get("items", [])
        assert any("pagato" in (n.get("messaggio") or "").lower() for n in items), "nessuna notifica di pagamento"

        # cannot remove presenza once paid
        r = admin.put(f"{API}/caterings/{cid}/presenze/{emp2_id}", json={"presente": False}, timeout=30)
        assert r.status_code == 400, f"expected 400, got {r.status_code}"

        # unpay toggle to allow cleanup
        p = admin.post(f"{API}/compensi/{riga['id']}/paga", timeout=30)
        assert p.status_code == 200 and p.json()["pagato"] is False

    def test_paga_tutto(self, admin, emp2, emp2_id, created_caterings):
        c1 = _make_catering(admin, created_caterings, compenso=60, days=-10)
        c2 = _make_catering(admin, created_caterings, compenso=40, days=-11)
        for cid in (c1, c2):
            admin.put(f"{API}/caterings/{cid}/presenze/{emp2_id}", json={"presente": True}, timeout=30)
        before = emp2.get(f"{API}/wallet", timeout=30).json()
        assert before["da_riscuotere"] >= 100.0

        r = admin.post(f"{API}/compensi/dipendente/{emp2_id}/paga-tutto", timeout=90)  # invio email inline: risposta lenta
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["totale"] == round(before["da_riscuotere"], 2)
        assert body["eventi"] >= 2

        after = emp2.get(f"{API}/wallet", timeout=30).json()
        assert after["da_riscuotere"] == 0
        assert round(after["pagato"], 2) == round(before["totale"], 2)

        comp = admin.get(f"{API}/compensi", timeout=30).json()
        mine = next(d for d in comp["dipendenti"] if d["user_id"] == emp2_id)
        assert mine["da_riscuotere"] == 0

        # second call -> 400 nothing to pay
        r2 = admin.post(f"{API}/compensi/dipendente/{emp2_id}/paga-tutto", timeout=30)
        assert r2.status_code == 400, r2.text

        # rollback: unpay the two rows created here
        for riga in [r for r in mine["righe"] if r["catering_id"] in (c1, c2)]:
            admin.post(f"{API}/compensi/{riga['id']}/paga", timeout=30)

    def test_paga_unknown_ids(self, admin):
        assert admin.post(f"{API}/compensi/507f1f77bcf86cd799439011/paga", timeout=30).status_code == 404
        assert admin.post(f"{API}/compensi/dipendente/507f1f77bcf86cd799439011/paga-tutto", timeout=30).status_code == 404

    def test_activity_log_contains_presenza_and_pagamento(self, admin):
        r = admin.get(f"{API}/activity", timeout=30)
        assert r.status_code == 200, r.text
        items = r.json() if isinstance(r.json(), list) else r.json().get("items", [])
        tipi = {i.get("azione") for i in items}
        assert "presenza" in tipi, f"tipi presenti: {tipi}"
        assert "pagamento" in tipi, f"tipi presenti: {tipi}"


# ---------------------------------------------------------------- permessi
class TestPermessi:
    def test_employee_forbidden_endpoints(self, emp, emp_id, admin, created_caterings):
        cid = _make_catering(admin, created_caterings)
        checks = [
            ("get", f"{API}/compensi", None),
            ("get", f"{API}/caterings/{cid}/presenze", None),
            ("put", f"{API}/caterings/{cid}/presenze/{emp_id}", {"presente": True}),
            ("post", f"{API}/compensi/507f1f77bcf86cd799439011/paga", None),
            ("post", f"{API}/compensi/dipendente/{emp_id}/paga-tutto", None),
        ]
        failures = []
        for method, url, body in checks:
            r = getattr(emp, method)(url, json=body, timeout=30) if body else getattr(emp, method)(url, timeout=30)
            if r.status_code != 403:
                failures.append(f"{method.upper()} {url} -> {r.status_code}")
        assert not failures, failures

    def test_unauthenticated_401(self, created_caterings):
        r = requests.get(f"{API}/compensi", timeout=30)
        assert r.status_code == 401
        r = requests.get(f"{API}/wallet", timeout=30)
        assert r.status_code == 401


# ---------------------------------------------------------------- settings compenso
class TestSettingsCompenso:
    def test_compenso_default_roundtrip(self, admin, created_caterings):
        cur = admin.get(f"{API}/settings", timeout=30)
        assert cur.status_code == 200
        original = cur.json().get("compenso_default")
        assert original is not None, "GET /api/settings non restituisce compenso_default"
        payload = {k: v for k, v in cur.json().items() if k in
                   {"nome_azienda", "colore_primario", "logo_url", "notifiche_email",
                    "notifiche_interne", "giorni_scadenza_default", "compenso_default"}}
        try:
            payload["compenso_default"] = 95.0
            r = admin.put(f"{API}/settings", json=payload, timeout=30)
            assert r.status_code == 200, r.text
            assert admin.get(f"{API}/settings", timeout=30).json()["compenso_default"] == 95.0
            cid = _make_catering(admin, created_caterings)
            assert admin.get(f"{API}/caterings/{cid}/presenze", timeout=30).json()["compenso_base"] == 95.0
        finally:
            payload["compenso_default"] = original
            admin.put(f"{API}/settings", json=payload, timeout=30)


# ---------------------------------------------------------------- cascata delete
class TestCascade:
    def test_delete_catering_removes_presenze(self, admin, emp, emp_id):
        d = (datetime.now(timezone.utc) - timedelta(days=4)).strftime("%Y-%m-%d")
        r = admin.post(f"{API}/caterings", json={
            "titolo": "TEST_Cascade", "data": d, "ora_inizio": "10:00", "luogo": "TEST_luogo",
            "personale_richiesto": 2, "compenso": 55}, timeout=30)
        cid = r.json()["id"]
        admin.put(f"{API}/caterings/{cid}/presenze/{emp_id}", json={"presente": True}, timeout=30)
        w = emp.get(f"{API}/wallet", timeout=30).json()
        assert any(x["catering_id"] == cid for x in w["righe"])
        assert admin.delete(f"{API}/caterings/{cid}", timeout=30).status_code in (200, 204)
        w2 = emp.get(f"{API}/wallet", timeout=30).json()
        assert not any(x["catering_id"] == cid for x in w2["righe"])


# ---------------------------------------------------------------- regressione rapida
class TestRegressione:
    def test_auth_me_and_dashboard(self, admin, emp):
        assert admin.get(f"{API}/auth/me", timeout=30).status_code == 200
        d = emp.get(f"{API}/dashboard", timeout=30)
        assert d.status_code == 200, d.text

    def test_catering_crud_and_disponibilita_counts(self, admin, emp, created_caterings):
        cid = _make_catering(admin, created_caterings, days=8, titolo="TEST_Regressione")
        r = emp.put(f"{API}/caterings/{cid}/disponibilita", json={"stato": "disponibile", "note": "ok"}, timeout=30)
        assert r.status_code == 200, r.text
        det = admin.get(f"{API}/caterings/{cid}", timeout=30)
        assert det.status_code == 200
        body = det.json()
        assert body.get("confermati", 0) >= 1, body
        assert body.get("mia_disponibilita") in ("in_attesa", "disponibile")

    def test_bcrypt_hash_format(self):
        import subprocess
        out = subprocess.run(
            ["python", "-c",
             "import os,asyncio;from motor.motor_asyncio import AsyncIOMotorClient;from dotenv import dotenv_values;"
             "e=dotenv_values('/app/backend/.env');"
             "c=AsyncIOMotorClient(e['MONGO_URL']);"
             "print(asyncio.get_event_loop().run_until_complete(c[e['DB_NAME']].users.find_one({'ruolo':'admin'}))['password_hash'][:4])"],
            capture_output=True, text=True, timeout=60)
        assert "$2b$" in out.stdout, out.stdout + out.stderr
