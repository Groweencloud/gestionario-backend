"""Iterazione 4 — regressione hardening: auth via Bearer/cookie, seed DEMO_EMPLOYEE_PASSWORD,
DELETE catering con compensi pagati, upload foto + permessi /api/files, scadenza_passata."""
import io
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL mancante")
BASE_URL = base_url.rstrip("/") + "/api"

ADMIN = {"email": "admin@impastocatering.it", "password": "Impasto2026!"}
EMP1 = {"email": "marco.rossi@impastocatering.it", "password": "Dipendente2026!"}
EMP2 = {"email": "luca.bianchi@impastocatering.it", "password": "Dipendente2026!"}
DEMO_EMAILS = [
    "marco.rossi@impastocatering.it",
    "luca.bianchi@impastocatering.it",
    "andrea.romano@impastocatering.it",
    "matteo.esposito@impastocatering.it",
    "giovanni.russo@impastocatering.it",
]


def login(creds):
    r = requests.post(f"{BASE_URL}/auth/login", json=creds, timeout=60)
    if r.status_code != 200:
        pytest.fail(f"login {creds['email']} -> {r.status_code}: {r.text[:300]}")
    return r


@pytest.fixture(scope="module")
def admin_token():
    return login(ADMIN).json()["access_token"]


@pytest.fixture(scope="module")
def emp_token():
    return login(EMP1).json()["access_token"]


def h(token):
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ auth
class TestAuthTransport:
    def test_login_returns_token_and_httponly_cookies(self):
        r = login(ADMIN)
        data = r.json()
        assert isinstance(data["access_token"], str) and len(data["access_token"]) > 20
        assert data["user"]["ruolo"] == "admin"
        cookies = r.headers.get("set-cookie", "")
        assert "access_token=" in cookies and "HttpOnly" in cookies
        assert "refresh_token=" in cookies

    def test_me_with_bearer_header(self, admin_token):
        r = requests.get(f"{BASE_URL}/auth/me", headers=h(admin_token), timeout=60)
        assert r.status_code == 200
        d = r.json()
        assert d["email"] == ADMIN["email"]
        assert "_id" not in d
        assert "password_hash" not in d

    def test_me_with_cookie_only(self):
        s = requests.Session()
        s.post(f"{BASE_URL}/auth/login", json=ADMIN, timeout=60)
        assert "access_token" in s.cookies
        r = s.get(f"{BASE_URL}/auth/me", timeout=60)  # nessun header Authorization
        assert r.status_code == 200
        assert r.json()["email"] == ADMIN["email"]

    def test_protected_endpoints_bearer_no_regression(self, admin_token):
        for path in ["/caterings", "/users", "/notifications", "/stats", "/compensi", "/activity", "/dashboard", "/settings"]:
            r = requests.get(f"{BASE_URL}{path}", headers=h(admin_token), timeout=90)
            assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:200]}"

    def test_no_token_401(self):
        r = requests.get(f"{BASE_URL}/auth/me", timeout=60)
        assert r.status_code == 401

    def test_invalid_token_401(self):
        r = requests.get(f"{BASE_URL}/auth/me", headers=h("garbage.token.value"), timeout=60)
        assert r.status_code == 401

    def test_logout_clears_cookies(self):
        s = requests.Session()
        s.post(f"{BASE_URL}/auth/login", json=ADMIN, timeout=60)
        r = s.post(f"{BASE_URL}/auth/logout", timeout=60)
        assert r.status_code == 200
        r2 = s.get(f"{BASE_URL}/auth/me", timeout=60)
        assert r2.status_code == 401, "il cookie non è stato invalidato dal logout"

    def test_refresh_flow(self):
        s = requests.Session()
        s.post(f"{BASE_URL}/auth/login", json=ADMIN, timeout=60)
        r = s.post(f"{BASE_URL}/auth/refresh", timeout=60)
        assert r.status_code == 200
        assert "access_token" in r.json()


# ------------------------------------------------------------------ seed demo password
class TestSeedDemoPassword:
    @pytest.mark.parametrize("email", DEMO_EMAILS)
    def test_demo_employee_login(self, email):
        r = requests.post(
            f"{BASE_URL}/auth/login", json={"email": email, "password": "Dipendente2026!"}, timeout=60
        )
        assert r.status_code == 200, f"{email} -> {r.status_code} {r.text[:200]}"
        assert r.json()["user"]["ruolo"] == "dipendente"


# ------------------------------------------------------------------ delete catering
class TestDeleteCatering:
    def _create(self, token, titolo, extra=None):
        payload = {
            "titolo": titolo,
            "data": (datetime.now(timezone.utc) + timedelta(days=20)).date().isoformat(),
            "ora_inizio": "18:00",
            "luogo": "TEST_Luogo",
            "indirizzo": "Via Test 1",
            "personale_richiesto": 2,
            "note": "test iter4",
        }
        payload.update(extra or {})
        r = requests.post(f"{BASE_URL}/caterings", json=payload, headers=h(token), timeout=120)
        assert r.status_code in (200, 201), r.text[:300]
        return r.json()["id"] if "id" in r.json() else r.json()["_id"]

    def test_delete_removes_availability_and_presenze(self, admin_token, emp_token):
        cid = self._create(admin_token, f"TEST_DEL_{uuid.uuid4().hex[:6]}")
        # disponibilità del dipendente
        r = requests.put(
            f"{BASE_URL}/caterings/{cid}/disponibilita",
            json={"stato": "disponibile", "note": ""},
            headers=h(emp_token),
            timeout=60,
        )
        assert r.status_code == 200
        # presenza confermata NON pagata
        pres = requests.get(f"{BASE_URL}/caterings/{cid}/presenze", headers=h(admin_token), timeout=60)
        assert pres.status_code == 200
        uid = pres.json()["righe"][0]["user_id"]
        r = requests.put(
            f"{BASE_URL}/caterings/{cid}/presenze/{uid}",
            json={"presente": True, "importo": 50},
            headers=h(admin_token),
            timeout=120,
        )
        assert r.status_code == 200, r.text[:300]

        d = requests.delete(f"{BASE_URL}/caterings/{cid}", headers=h(admin_token), timeout=60)
        assert d.status_code == 200, d.text[:300]
        assert requests.get(f"{BASE_URL}/caterings/{cid}", headers=h(admin_token), timeout=60).status_code == 404
        assert (
            requests.get(f"{BASE_URL}/caterings/{cid}/presenze", headers=h(admin_token), timeout=60).status_code
            == 404
        )

    def test_delete_blocked_when_paid_compensi(self, admin_token):
        cid = self._create(admin_token, f"TEST_DELPAID_{uuid.uuid4().hex[:6]}")
        pres = requests.get(f"{BASE_URL}/caterings/{cid}/presenze", headers=h(admin_token), timeout=60)
        righe = pres.json()["righe"]
        uid = righe[-1]["user_id"]
        r = requests.put(
            f"{BASE_URL}/caterings/{cid}/presenze/{uid}",
            json={"presente": True, "importo": 40},
            headers=h(admin_token),
            timeout=120,
        )
        assert r.status_code == 200, r.text[:300]
        pres2 = requests.get(f"{BASE_URL}/caterings/{cid}/presenze", headers=h(admin_token), timeout=60).json()
        riga = next(x for x in pres2["righe"] if x["user_id"] == uid)
        assert riga["presente"] is True and riga["importo"] == 40.0
        # l'id della presenza si ottiene da GET /compensi
        comp = requests.get(f"{BASE_URL}/compensi", headers=h(admin_token), timeout=120).json()
        dip = next(d for d in comp["dipendenti"] if d["user_id"] == uid)
        riga_c = next(r for r in dip["righe"] if r.get("catering_id") == cid)
        presenza_id = riga_c["id"]
        assert presenza_id, f"presenza_id assente: {riga_c}"
        pay = requests.post(f"{BASE_URL}/compensi/{presenza_id}/paga", headers=h(admin_token), timeout=120)
        assert pay.status_code == 200, pay.text[:300]

        d = requests.delete(f"{BASE_URL}/caterings/{cid}", headers=h(admin_token), timeout=60)
        assert d.status_code == 400, f"attesa 400, ottenuto {d.status_code}"
        assert "pagat" in d.json()["detail"].lower()
        # il catering esiste ancora
        assert requests.get(f"{BASE_URL}/caterings/{cid}", headers=h(admin_token), timeout=60).status_code == 200

        # cleanup: annulla pagamento (toggle) poi elimina
        requests.post(f"{BASE_URL}/compensi/{presenza_id}/paga", headers=h(admin_token), timeout=120)
        d2 = requests.delete(f"{BASE_URL}/caterings/{cid}", headers=h(admin_token), timeout=60)
        assert d2.status_code == 200, d2.text[:300]

    def test_delete_requires_admin(self, admin_token, emp_token):
        cid = self._create(admin_token, f"TEST_DELPERM_{uuid.uuid4().hex[:6]}")
        r = requests.delete(f"{BASE_URL}/caterings/{cid}", headers=h(emp_token), timeout=60)
        assert r.status_code == 403
        requests.delete(f"{BASE_URL}/caterings/{cid}", headers=h(admin_token), timeout=60)

    def test_delete_unknown_id_404(self, admin_token):
        r = requests.delete(f"{BASE_URL}/caterings/{'a'*24}", headers=h(admin_token), timeout=60)
        assert r.status_code == 404


# ------------------------------------------------------------------ upload foto / files
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00"
    b"\x00IEND\xaeB`\x82"
)


class TestPhotoUpload:
    def test_upload_and_download_own_file(self, emp_token):
        r = requests.post(
            f"{BASE_URL}/profile/photo",
            files={"file": ("TEST_iter4.png", io.BytesIO(PNG), "image/png")},
            headers=h(emp_token),
            timeout=120,
        )
        assert r.status_code == 200, r.text[:300]
        path = r.json()["foto_url"]
        assert path
        g = requests.get(f"{BASE_URL}/files/{path}", headers=h(emp_token), timeout=60)
        assert g.status_code == 200
        assert g.headers.get("content-type", "").startswith("image/")
        assert len(g.content) > 0
        # il profilo riflette la foto
        me = requests.get(f"{BASE_URL}/auth/me", headers=h(emp_token), timeout=60).json()
        assert me["foto_url"] == path

    def test_other_employee_gets_403(self, emp_token, admin_token):
        up = requests.post(
            f"{BASE_URL}/profile/photo",
            files={"file": ("TEST_iter4b.png", io.BytesIO(PNG), "image/png")},
            headers=h(emp_token),
            timeout=120,
        )
        assert up.status_code == 200, up.text[:300]
        path = up.json()["foto_url"]
        other = login(EMP2).json()["access_token"]
        r = requests.get(f"{BASE_URL}/files/{path}", headers=h(other), timeout=60)
        assert r.status_code == 403, f"attesa 403, ottenuto {r.status_code}"
        # admin può vedere
        ra = requests.get(f"{BASE_URL}/files/{path}", headers=h(admin_token), timeout=60)
        assert ra.status_code == 200

    def test_unsupported_format_400(self, emp_token):
        r = requests.post(
            f"{BASE_URL}/profile/photo",
            files={"file": ("bad.txt", io.BytesIO(b"hello"), "text/plain")},
            headers=h(emp_token),
            timeout=60,
        )
        assert r.status_code == 400

    def test_file_not_found_404(self, admin_token):
        r = requests.get(f"{BASE_URL}/files/nope/does-not-exist.png", headers=h(admin_token), timeout=60)
        assert r.status_code == 404

    def test_file_requires_auth(self):
        r = requests.get(f"{BASE_URL}/files/whatever.png", timeout=60)
        assert r.status_code == 401


# ------------------------------------------------------------------ scadenza risposta
class TestScadenzaPassata:
    def test_expired_deadline_blocks_employee_allows_admin(self, admin_token, emp_token):
        payload = {
            "titolo": f"TEST_SCAD_{uuid.uuid4().hex[:6]}",
            "data": (datetime.now(timezone.utc) + timedelta(days=15)).date().isoformat(),
            "ora_inizio": "19:00",
            "luogo": "TEST_Luogo",
            "indirizzo": "Via Test 2",
            "personale_richiesto": 1,
            "scadenza_risposta": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        }
        c = requests.post(f"{BASE_URL}/caterings", json=payload, headers=h(admin_token), timeout=120)
        assert c.status_code in (200, 201), c.text[:300]
        cid = c.json().get("id") or c.json().get("_id")
        try:
            r = requests.put(
                f"{BASE_URL}/caterings/{cid}/disponibilita",
                json={"stato": "disponibile", "note": ""},
                headers=h(emp_token),
                timeout=60,
            )
            assert r.status_code == 400, f"il dipendente non dovrebbe poter rispondere: {r.status_code}"
            assert "scadut" in r.json()["detail"].lower()
            ra = requests.put(
                f"{BASE_URL}/caterings/{cid}/disponibilita",
                json={"stato": "disponibile", "note": ""},
                headers=h(admin_token),
                timeout=60,
            )
            assert ra.status_code == 200
        finally:
            requests.delete(f"{BASE_URL}/caterings/{cid}", headers=h(admin_token), timeout=60)

    def test_malformed_deadline_does_not_crash(self, admin_token, emp_token):
        """scadenza_risposta non ISO: scadenza_passata() deve tornare False senza 500."""
        payload = {
            "titolo": f"TEST_SCADBAD_{uuid.uuid4().hex[:6]}",
            "data": (datetime.now(timezone.utc) + timedelta(days=16)).date().isoformat(),
            "ora_inizio": "19:00",
            "luogo": "TEST_Luogo",
            "indirizzo": "Via Test 3",
            "personale_richiesto": 1,
            "scadenza_risposta": "non-una-data",
        }
        c = requests.post(f"{BASE_URL}/caterings", json=payload, headers=h(admin_token), timeout=120)
        if c.status_code in (400, 422):
            pytest.skip("il backend valida il formato della scadenza in input")
        cid = c.json().get("id") or c.json().get("_id")
        try:
            r = requests.put(
                f"{BASE_URL}/caterings/{cid}/disponibilita",
                json={"stato": "disponibile", "note": ""},
                headers=h(emp_token),
                timeout=60,
            )
            assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
            lst = requests.get(f"{BASE_URL}/caterings", headers=h(emp_token), timeout=90)
            assert lst.status_code == 200
        finally:
            requests.delete(f"{BASE_URL}/caterings/{cid}", headers=h(admin_token), timeout=60)


# ------------------------------------------------------------------ permessi dipendente
class TestEmployeePermissions:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/users"),
            ("GET", "/compensi"),
            ("GET", "/activity"),
            ("GET", "/stats"),
        ],
    )
    def test_admin_only_endpoints_403(self, emp_token, method, path):
        r = requests.request(method, f"{BASE_URL}{path}", headers=h(emp_token), timeout=60)
        assert r.status_code == 403, f"{path} -> {r.status_code}"

    def test_employee_wallet_scoped(self, emp_token):
        r = requests.get(f"{BASE_URL}/wallet", headers=h(emp_token), timeout=60)
        assert r.status_code == 200
        body = r.json()
        assert "totale_da_riscuotere" in body or "da_riscuotere" in body
        assert "_id" not in str(body)
