"""Backend API tests — Impasto Catering (auth, users, caterings, disponibilita, dashboard, stats, activity, settings, notifications)."""
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")
API = f"{BASE_URL}/api"


# ---------------------------------------------------------------- fixtures
def _creds():
    p = Path("/app/memory/test_credentials.md")
    content = p.read_text(encoding="utf-8")
    email = re.search(r'(?im)^-\s*Email:\s*`([^`]+)`', content).group(1)
    pwd = re.search(r'(?im)^-\s*Password:\s*`([^`]+)`', content).group(1)
    emp_pwd = re.search(r'(?im)^Password:\s*`([^`]+)`', content).group(1)
    return {"admin_email": email, "admin_password": pwd,
            "emp_email": "marco.rossi@impastocatering.it", "emp_password": emp_pwd}


CREDS = _creds()


def _login(email, password):
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=30)
    if r.status_code != 200:
        pytest.fail(f"Login failed for {email}: {r.status_code} {r.text[:300]}")
    return r.json()


@pytest.fixture(scope="session")
def admin_token():
    return _login(CREDS["admin_email"], CREDS["admin_password"])["access_token"]


@pytest.fixture(scope="session")
def emp_login():
    return _login(CREDS["emp_email"], CREDS["emp_password"])


@pytest.fixture(scope="session")
def admin(admin_token):
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def emp(emp_login):
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {emp_login['access_token']}", "Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def created(admin):
    """Track created resources for teardown."""
    store = {"caterings": [], "users": []}
    yield store
    for cid in store["caterings"]:
        admin.delete(f"{API}/caterings/{cid}", timeout=30)
    for uid in store["users"]:
        admin.delete(f"{API}/users/{uid}", timeout=30)


def _catering_payload(**over):
    data = (datetime.now(timezone.utc) + timedelta(days=20)).strftime("%Y-%m-%d")
    p = {
        "titolo": f"TEST_Catering_{uuid.uuid4().hex[:6]}",
        "descrizione": "Evento di test",
        "data": data,
        "ora_inizio": "19:00",
        "ora_fine": "23:00",
        "luogo": "TEST_Villa Palermo",
        "indirizzo": "Via Roma 1, Palermo",
        "personale_richiesto": 2,
        "referente": "Mario",
        "referente_telefono": "+39 333 1111111",
    }
    p.update(over)
    return p


# ---------------------------------------------------------------- health / auth
class TestHealthAuth:
    def test_root(self):
        r = requests.get(f"{API}/", timeout=30)
        assert r.status_code == 200
        assert r.json().get("ok") is True

    def test_admin_login(self):
        data = _login(CREDS["admin_email"], CREDS["admin_password"])
        assert data["user"]["ruolo"] == "admin"
        assert data["user"]["email"] == CREDS["admin_email"]
        assert isinstance(data["access_token"], str) and len(data["access_token"]) > 20

    def test_employee_login(self, emp_login):
        assert emp_login["user"]["ruolo"] == "dipendente"

    def test_me(self, admin):
        r = admin.get(f"{API}/auth/me", timeout=30)
        assert r.status_code == 200
        assert r.json()["email"] == CREDS["admin_email"]
        assert "_id" not in r.json()

    def test_no_token_401(self):
        r = requests.get(f"{API}/auth/me", timeout=30)
        assert r.status_code == 401

    def test_bad_token_401(self):
        r = requests.get(f"{API}/auth/me", headers={"Authorization": "Bearer abc.def.ghi"}, timeout=30)
        assert r.status_code == 401

    def test_wrong_password(self):
        r = requests.post(f"{API}/auth/login",
                          json={"email": f"nonexistent_{uuid.uuid4().hex[:6]}@test.it", "password": "x"}, timeout=30)
        assert r.status_code == 401
        assert "detail" in r.json()

    def test_bcrypt_hash_format(self):
        """bcrypt hash must be $2b$ - checked via direct DB read."""
        import asyncio
        from motor.motor_asyncio import AsyncIOMotorClient
        from dotenv import dotenv_values as dv
        env = dv("/app/backend/.env")

        async def _run():
            c = AsyncIOMotorClient(env["MONGO_URL"])
            u = await c[env["DB_NAME"]].users.find_one({"email": CREDS["admin_email"]})
            c.close()
            return u
        u = asyncio.get_event_loop().run_until_complete(_run()) if False else asyncio.run(_run())
        assert u is not None
        assert u["password_hash"].startswith("$2b$"), u["password_hash"][:10]

    def test_brute_force_lockout(self):
        """5 failed attempts on the same identifier -> 429."""
        email = f"lockout_{uuid.uuid4().hex[:8]}@test.it"
        codes = []
        for _ in range(6):
            r = requests.post(f"{API}/auth/login", json={"email": email, "password": "wrong"}, timeout=30)
            codes.append(r.status_code)
        assert 429 in codes, f"no lockout, codes={codes}"

    def test_refresh_token(self, emp_login):
        r = requests.post(f"{API}/auth/refresh", headers={"X-Refresh-Token": emp_login["refresh_token"]}, timeout=30)
        assert r.status_code == 200
        assert "access_token" in r.json()

    def test_forgot_password_generic_response(self):
        r = requests.post(f"{API}/auth/forgot-password", json={"email": "unknown@test.it"}, timeout=60)
        assert r.status_code == 200
        assert r.json()["ok"] is True


# ---------------------------------------------------------------- permessi
class TestPermissions:
    @pytest.mark.parametrize("method,path", [
        ("get", "/users"), ("get", "/stats"), ("get", "/activity"), ("get", "/settings"),
    ])
    def test_employee_forbidden_get(self, emp, method, path):
        r = getattr(emp, method)(f"{API}{path}", timeout=30)
        assert r.status_code == 403, f"{path} -> {r.status_code}"

    def test_employee_cannot_create_catering(self, emp):
        r = emp.post(f"{API}/caterings", json=_catering_payload(), timeout=30)
        assert r.status_code == 403

    def test_employee_cannot_create_user(self, emp):
        r = emp.post(f"{API}/users", json={"nome": "X", "cognome": "Y",
                                          "email": f"x{uuid.uuid4().hex[:6]}@t.it", "password": "secret1"}, timeout=30)
        assert r.status_code == 403


# ---------------------------------------------------------------- catering CRUD + disponibilita
class TestCateringFlow:
    def test_create_and_get(self, admin, created):
        payload = _catering_payload()
        r = admin.post(f"{API}/caterings", json=payload, timeout=60)
        assert r.status_code == 200, r.text[:400]
        item = r.json()
        assert "id" in item and "_id" not in item
        created["caterings"].append(item["id"])
        assert item["titolo"] == payload["titolo"]
        assert item["personale_richiesto"] == 2
        assert item["confermati"] == 0
        assert item["scadenza_risposta"]

        g = admin.get(f"{API}/caterings/{item['id']}", timeout=30)
        assert g.status_code == 200
        d = g.json()
        assert d["luogo"] == payload["luogo"]
        assert set(d["gruppi"].keys()) == {"disponibile", "non_disponibile", "in_attesa"}
        # admin sees contact info
        assert "email" in d["gruppi"]["in_attesa"][0]

    def test_catering_in_list(self, admin, created):
        cid = created["caterings"][0]
        r = admin.get(f"{API}/caterings", timeout=30)
        assert r.status_code == 200
        assert cid in [i["id"] for i in r.json()]

    def test_employee_sees_catering_and_notification(self, emp, created):
        cid = created["caterings"][0]
        r = emp.get(f"{API}/caterings/{cid}", timeout=30)
        assert r.status_code == 200
        assert r.json()["mia_disponibilita"] == "in_attesa"
        # employee must NOT see other employees' email in gruppi
        assert "email" not in r.json()["gruppi"]["in_attesa"][0]
        n = emp.get(f"{API}/notifications", timeout=30)
        assert n.status_code == 200
        assert any(x.get("catering_id") == cid and x["tipo"] == "nuovo_catering" for x in n.json()), \
            "no nuovo_catering notification for employee"

    def test_set_availability_disponibile_persists(self, emp, created):
        cid = created["caterings"][0]
        r = emp.put(f"{API}/caterings/{cid}/disponibilita",
                    json={"stato": "disponibile", "note": "TEST nota"}, timeout=30)
        assert r.status_code == 200, r.text[:300]
        assert r.json()["mia_disponibilita"] == "disponibile"
        assert r.json()["confermati"] >= 1
        g = emp.get(f"{API}/caterings/{cid}", timeout=30)
        assert g.json()["mia_disponibilita"] == "disponibile"
        assert g.json()["mia_nota"] == "TEST nota"

    def test_admin_sees_employee_in_confermati(self, admin, created):
        cid = created["caterings"][0]
        d = admin.get(f"{API}/caterings/{cid}", timeout=30).json()
        names = [f"{x['nome']} {x['cognome']}" for x in d["gruppi"]["disponibile"]]
        assert "Marco Rossi" in names, names
        assert d["confermati"] == 1
        assert d["mancanti"] == 1

    def test_change_to_non_disponibile(self, emp, created):
        cid = created["caterings"][0]
        r = emp.put(f"{API}/caterings/{cid}/disponibilita", json={"stato": "non_disponibile"}, timeout=30)
        assert r.status_code == 200
        assert r.json()["mia_disponibilita"] == "non_disponibile"
        assert r.json()["non_disponibili"] == 1
        assert r.json()["confermati"] == 0

    def test_invalid_availability_state(self, emp, created):
        cid = created["caterings"][0]
        r = emp.put(f"{API}/caterings/{cid}/disponibilita", json={"stato": "boh"}, timeout=30)
        assert r.status_code == 400

    def test_stato_personale_completo(self, admin, created):
        """2 employees confirm on a catering requiring 2 -> stato personale_completo."""
        payload = _catering_payload(personale_richiesto=2)
        cid = admin.post(f"{API}/caterings", json=payload, timeout=60).json()["id"]
        created["caterings"].append(cid)
        for email in ("marco.rossi@impastocatering.it", "luca.bianchi@impastocatering.it"):
            tok = _login(email, CREDS["emp_password"])["access_token"]
            r = requests.put(f"{API}/caterings/{cid}/disponibilita", json={"stato": "disponibile"},
                             headers={"Authorization": f"Bearer {tok}"}, timeout=30)
            assert r.status_code == 200, r.text[:300]
        d = admin.get(f"{API}/caterings/{cid}", timeout=30).json()
        assert d["confermati"] == 2
        assert d["stato"] == "personale_completo", d["stato"]
        assert d["mancanti"] == 0

    def test_update_catering(self, admin, created):
        cid = created["caterings"][0]
        payload = _catering_payload(titolo="TEST_Catering_Updated", luogo="TEST_Nuovo Luogo",
                                    personale_richiesto=5)
        r = admin.put(f"{API}/caterings/{cid}", json=payload, timeout=60)
        assert r.status_code == 200, r.text[:300]
        assert r.json()["luogo"] == "TEST_Nuovo Luogo"
        assert r.json()["personale_richiesto"] == 5
        g = admin.get(f"{API}/caterings/{cid}", timeout=30).json()
        assert g["luogo"] == "TEST_Nuovo Luogo"
        assert g["titolo"] == "TEST_Catering_Updated"

    def test_scadenza_blocks_employee_and_reopen(self, admin, created):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT18:00:00")
        payload = _catering_payload(scadenza_risposta=past)
        item = admin.post(f"{API}/caterings", json=payload, timeout=60).json()
        cid = item["id"]
        created["caterings"].append(cid)
        assert item["scadenza_superata"] is True
        tok = _login("andrea.romano@impastocatering.it", CREDS["emp_password"])["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        r = requests.put(f"{API}/caterings/{cid}/disponibilita", json={"stato": "disponibile"}, headers=h, timeout=30)
        assert r.status_code == 400, r.status_code
        rp = admin.post(f"{API}/caterings/{cid}/riapri", timeout=60)
        assert rp.status_code == 200
        r2 = requests.put(f"{API}/caterings/{cid}/disponibilita", json={"stato": "disponibile"}, headers=h, timeout=30)
        assert r2.status_code == 200, r2.text[:300]
        assert r2.json()["scadenza_superata"] is False

    def test_filters(self, admin, created):
        r = admin.get(f"{API}/caterings", params={"periodo": "futuri"}, timeout=30)
        assert r.status_code == 200
        oggi = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert all(i["data"] >= oggi for i in r.json())
        r2 = admin.get(f"{API}/caterings", params={"periodo": "passati"}, timeout=30)
        assert r2.status_code == 200
        assert all(i["data"] < oggi for i in r2.json())
        r3 = admin.get(f"{API}/caterings", params={"luogo": "TEST_Nuovo"}, timeout=30)
        assert r3.status_code == 200
        assert all("test_nuovo" in i["luogo"].lower() for i in r3.json())
        r4 = admin.get(f"{API}/caterings", params={"stato": "personale_completo"}, timeout=30)
        assert all(i["stato"] == "personale_completo" for i in r4.json())

    def test_employee_availability_filter(self, emp):
        r = emp.get(f"{API}/caterings", params={"disponibilita": "disponibile"}, timeout=30)
        assert r.status_code == 200
        assert all(i["mia_disponibilita"] == "disponibile" for i in r.json())

    def test_get_invalid_catering_id_404(self, admin):
        r = admin.get(f"{API}/caterings/notanid", timeout=30)
        assert r.status_code == 404, r.status_code

    def test_delete_catering(self, admin):
        cid = admin.post(f"{API}/caterings", json=_catering_payload(titolo="TEST_ToDelete"), timeout=60).json()["id"]
        r = admin.delete(f"{API}/caterings/{cid}", timeout=30)
        assert r.status_code == 200
        assert admin.get(f"{API}/caterings/{cid}", timeout=30).status_code == 404

    def test_validation_personale_richiesto(self, admin):
        r = admin.post(f"{API}/caterings", json=_catering_payload(personale_richiesto=0), timeout=30)
        assert r.status_code == 422


# ---------------------------------------------------------------- utenti CRUD
class TestUsers:
    def test_full_user_lifecycle(self, admin, created):
        email = f"test_{uuid.uuid4().hex[:8]}@impastocatering.it"
        r = admin.post(f"{API}/users", json={"nome": "TEST", "cognome": "Utente", "email": email,
                                            "telefono": "+39 333 000", "password": "Dipendente2026!"}, timeout=30)
        assert r.status_code == 200, r.text[:300]
        u = r.json()
        uid = u["id"]
        created["users"].append(uid)
        assert u["email"] == email and u["ruolo"] == "dipendente" and u["stato"] == "attivo"
        assert "password_hash" not in u

        # duplicate email
        dup = admin.post(f"{API}/users", json={"nome": "A", "cognome": "B", "email": email,
                                               "password": "Dipendente2026!"}, timeout=30)
        assert dup.status_code == 400

        # login works
        assert _login(email, "Dipendente2026!")["user"]["email"] == email

        # update
        up = admin.put(f"{API}/users/{uid}", json={"nome": "TEST2", "telefono": "+39 111"}, timeout=30)
        assert up.status_code == 200 and up.json()["nome"] == "TEST2"
        assert admin.get(f"{API}/users/{uid}", timeout=30).json()["nome"] == "TEST2"

        # detail stats fields
        det = admin.get(f"{API}/users/{uid}", timeout=30).json()
        for k in ("risposte_totali", "disponibili", "non_disponibili", "eventi_totali"):
            assert k in det

        # reset password
        rp = admin.post(f"{API}/users/{uid}/reset-password", timeout=60)
        assert rp.status_code == 200
        temp = rp.json()["password_temporanea"]
        assert _login(email, temp)["user"]["email"] == email

        # deactivate -> login 403
        dz = admin.put(f"{API}/users/{uid}", json={"stato": "disattivato"}, timeout=30)
        assert dz.status_code == 200 and dz.json()["stato"] == "disattivato"
        bad = requests.post(f"{API}/auth/login", json={"email": email, "password": temp}, timeout=30)
        assert bad.status_code == 403, bad.status_code
        assert "disattivato" in bad.json()["detail"].lower()

        # delete
        d = admin.delete(f"{API}/users/{uid}", timeout=30)
        assert d.status_code == 200
        created["users"].remove(uid)
        assert admin.get(f"{API}/users/{uid}", timeout=30).status_code == 404

    def test_list_users_and_search(self, admin):
        r = admin.get(f"{API}/users", timeout=30)
        assert r.status_code == 200 and len(r.json()) >= 6
        assert all("password_hash" not in u for u in r.json())
        r2 = admin.get(f"{API}/users", params={"q": "marco"}, timeout=30)
        assert r2.status_code == 200 and len(r2.json()) >= 1
        assert all("marco" in (u["nome"] + u["cognome"] + u["email"]).lower() for u in r2.json())

    def test_cannot_delete_admin(self, admin):
        admin_id = admin.get(f"{API}/auth/me", timeout=30).json()["id"]
        r = admin.delete(f"{API}/users/{admin_id}", timeout=30)
        assert r.status_code == 400

    def test_user_not_found(self, admin):
        r = admin.get(f"{API}/users/{'a'*24}", timeout=30)
        assert r.status_code == 404


# ---------------------------------------------------------------- dashboard / stats / activity / settings / profilo
class TestDashboardStats:
    def test_dashboard_admin(self, admin):
        r = admin.get(f"{API}/dashboard", timeout=30)
        assert r.status_code == 200
        d = r.json()
        for k in ("prossimi", "totale_prossimi", "oggi", "totale_oggi", "disponibili",
                  "non_disponibili", "senza_risposta", "eventi_incompleti"):
            assert k in d
        assert isinstance(d["prossimi"], list)

    def test_dashboard_employee(self, emp):
        r = emp.get(f"{API}/dashboard", timeout=30)
        assert r.status_code == 200
        assert all("mia_disponibilita" in i for i in r.json()["prossimi"])

    def test_stats(self, admin):
        r = admin.get(f"{API}/stats", timeout=30)
        assert r.status_code == 200
        d = r.json()
        assert d["totale_catering"] > 0
        assert d["totale_dipendenti"] >= 5
        assert len(d["dipendenti"]) == d["totale_dipendenti"]
        assert all(0 <= x["disponibilita_pct"] <= 100 for x in d["dipendenti"])
        assert len(d["stati"]) == 6

    def test_activity_log(self, admin):
        r = admin.get(f"{API}/activity", timeout=30)
        assert r.status_code == 200
        azioni = {x["azione"] for x in r.json()}
        assert "creazione_catering" in azioni, azioni
        assert "disponibilita" in azioni, azioni
        assert all("_id" not in x for x in r.json())

    def test_settings_read_update(self, admin):
        r = admin.get(f"{API}/settings", timeout=30)
        assert r.status_code == 200
        original = r.json()
        upd = dict(original)
        upd["giorni_scadenza_default"] = 4
        w = admin.put(f"{API}/settings", json=upd, timeout=30)
        assert w.status_code == 200 and w.json()["giorni_scadenza_default"] == 4
        assert admin.get(f"{API}/settings", timeout=30).json()["giorni_scadenza_default"] == 4
        admin.put(f"{API}/settings", json=original, timeout=30)

    def test_notifications_read_all(self, emp):
        r = emp.post(f"{API}/notifications/read-all", timeout=30)
        assert r.status_code == 200
        assert all(n["letto"] for n in emp.get(f"{API}/notifications", timeout=30).json())

    def test_profile_update_and_password_change(self):
        email = "davide.conti@impastocatering.it"
        tok = _login(email, CREDS["emp_password"])["access_token"]
        s = requests.Session()
        s.headers.update({"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
        r = s.put(f"{API}/profile", json={"telefono": "+39 320 9999999"}, timeout=30)
        assert r.status_code == 200 and r.json()["telefono"] == "+39 320 9999999"
        assert s.get(f"{API}/auth/me", timeout=30).json()["telefono"] == "+39 320 9999999"

        bad = s.put(f"{API}/auth/change-password",
                    json={"password_attuale": "sbagliata", "password_nuova": "NuovaPwd1!"}, timeout=30)
        assert bad.status_code == 400
        ok = s.put(f"{API}/auth/change-password",
                   json={"password_attuale": CREDS["emp_password"], "password_nuova": "NuovaPwd1!"}, timeout=30)
        assert ok.status_code == 200
        assert _login(email, "NuovaPwd1!")["user"]["email"] == email
        # restore original password
        tok2 = _login(email, "NuovaPwd1!")["access_token"]
        s2 = requests.Session()
        s2.headers.update({"Authorization": f"Bearer {tok2}", "Content-Type": "application/json"})
        rest = s2.put(f"{API}/auth/change-password",
                      json={"password_attuale": "NuovaPwd1!", "password_nuova": CREDS["emp_password"]}, timeout=30)
        assert rest.status_code == 200
