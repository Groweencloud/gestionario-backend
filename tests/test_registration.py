"""Iteration 2 — registration / approval flow + quick regressions."""
import os
import time
import uuid

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")
API = f"{BASE_URL}/api"

ADMIN = {"email": "admin@impastocatering.it", "password": "Impasto2026!"}
EMP = {"email": "marco.rossi@impastocatering.it", "password": "Dipendente2026!"}
PWD = "Registrato2026!"


def new_email():
    return f"test_reg_{uuid.uuid4().hex[:8]}@impastocatering.it"


class NoCookieSession(requests.Session):
    """Backend prefers the httpOnly cookie over the Authorization header, so a
    shared cookie jar would leak the last logged-in identity across tests."""

    def prepare_request(self, request):
        prepared = super().prepare_request(request)
        prepared.prepare_cookies({})
        prepared.headers.pop("Cookie", None)
        return prepared

    def send(self, request, **kwargs):
        response = super().send(request, **kwargs)
        self.cookies.clear()
        return response


@pytest.fixture(scope="module")
def client():
    s = NoCookieSession()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def admin_token(client):
    r = client.post(f"{API}/auth/login", json=ADMIN)
    if r.status_code != 200:
        pytest.fail(f"Admin login failed {r.status_code}: {r.text[:300]}")
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def admin_h(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(scope="module")
def created_emails():
    return []


@pytest.fixture(scope="module", autouse=True)
def cleanup(client, admin_h, created_emails):
    yield
    users = client.get(f"{API}/users", headers=admin_h).json()
    for u in users:
        if u["email"] in created_emails:
            client.delete(f"{API}/users/{u['id']}", headers=admin_h)


# ---------------------------------------------------------------- registration
class TestRegistration:
    def test_register_success_and_pending(self, client, admin_h, created_emails):
        email = new_email()
        created_emails.append(email)
        r = client.post(f"{API}/auth/register", json={
            "nome": "TESTReg", "cognome": "Uno", "email": email,
            "telefono": "3331112222", "password": PWD})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["message"] == "Richiesta inviata. Potrai accedere dopo l'approvazione dell'amministratore."

        # persisted with stato in_attesa, ruolo dipendente
        users = client.get(f"{API}/users", headers=admin_h, params={"q": email}).json()
        match = [u for u in users if u["email"] == email]
        assert len(match) == 1
        u = match[0]
        assert u["stato"] == "in_attesa"
        assert u["ruolo"] == "dipendente"
        assert u["telefono"] == "3331112222"
        assert "_id" not in u

    def test_pending_user_cannot_login(self, client, created_emails):
        email = created_emails[0]
        r = client.post(f"{API}/auth/login", json={"email": email, "password": PWD})
        assert r.status_code == 403, r.text
        assert r.json()["detail"] == "Account in attesa di approvazione da parte dell'amministratore."

    def test_duplicate_email_rejected(self, client, created_emails):
        r = client.post(f"{API}/auth/register", json={
            "nome": "TESTReg", "cognome": "Dup", "email": created_emails[0],
            "telefono": "", "password": PWD})
        assert r.status_code == 400
        assert r.json()["detail"] == "Esiste già un account con questa email"

    def test_short_password_rejected(self, client):
        r = client.post(f"{API}/auth/register", json={
            "nome": "A", "cognome": "B", "email": new_email(), "telefono": "", "password": "123"})
        assert r.status_code == 422

    def test_missing_fields_rejected(self, client):
        r = client.post(f"{API}/auth/register", json={"email": new_email(), "password": PWD})
        assert r.status_code == 422

    def test_invalid_email_rejected(self, client):
        r = client.post(f"{API}/auth/register", json={
            "nome": "A", "cognome": "B", "email": "not-an-email", "password": PWD})
        assert r.status_code == 422

    def test_empty_nome_rejected(self, client):
        r = client.post(f"{API}/auth/register", json={
            "nome": "", "cognome": "B", "email": new_email(), "password": PWD})
        assert r.status_code == 422

    def test_register_cannot_set_role_admin(self, client, admin_h, created_emails):
        email = new_email()
        created_emails.append(email)
        r = client.post(f"{API}/auth/register", json={
            "nome": "TESTReg", "cognome": "Escalation", "email": email,
            "telefono": "", "password": PWD, "ruolo": "admin", "stato": "attivo"})
        assert r.status_code == 200
        users = client.get(f"{API}/users", headers=admin_h, params={"q": email}).json()
        u = [x for x in users if x["email"] == email][0]
        assert u["ruolo"] == "dipendente"
        assert u["stato"] == "in_attesa"

    def test_admin_notification_created(self, client, admin_h, created_emails):
        notifs = client.get(f"{API}/notifications", headers=admin_h).json()
        items = notifs["items"] if isinstance(notifs, dict) else notifs
        texts = [n.get("messaggio", "") for n in items]
        assert any("Nuova richiesta di registrazione" in t for t in texts), texts[:5]


# ---------------------------------------------------------------- approve / reject
class TestApprovalFlow:
    def _register(self, client, created_emails, cognome):
        email = new_email()
        created_emails.append(email)
        r = client.post(f"{API}/auth/register", json={
            "nome": "TESTFlow", "cognome": cognome, "email": email,
            "telefono": "3339998888", "password": PWD})
        assert r.status_code == 200
        return email

    def _find(self, client, admin_h, email):
        users = client.get(f"{API}/users", headers=admin_h).json()
        m = [u for u in users if u["email"] == email]
        return m[0] if m else None

    def test_approve_enables_login(self, client, admin_h, created_emails):
        email = self._register(client, created_emails, "Approva")
        u = self._find(client, admin_h, email)
        r = client.post(f"{API}/users/{u['id']}/approva", headers=admin_h)
        assert r.status_code == 200, r.text
        assert r.json()["stato"] == "attivo"
        # persisted
        assert self._find(client, admin_h, email)["stato"] == "attivo"
        # login works now
        lr = client.post(f"{API}/auth/login", json={"email": email, "password": PWD})
        assert lr.status_code == 200, lr.text
        assert lr.json()["user"]["email"] == email
        # user got approval notification
        h = {"Authorization": f"Bearer {lr.json()['access_token']}"}
        notifs = client.get(f"{API}/notifications", headers=h).json()
        items = notifs["items"] if isinstance(notifs, dict) else notifs
        assert any("approvato" in n.get("messaggio", "") for n in items)

    def test_reject_deletes_account(self, client, admin_h, created_emails):
        email = self._register(client, created_emails, "Rifiuta")
        u = self._find(client, admin_h, email)
        r = client.post(f"{API}/users/{u['id']}/rifiuta", headers=admin_h)
        assert r.status_code == 200, r.text
        assert self._find(client, admin_h, email) is None
        lr = client.post(f"{API}/auth/login", json={"email": email, "password": PWD})
        assert lr.status_code == 401
        assert lr.json()["detail"] == "Email o password non corretti"

    def test_reject_already_active_returns_400(self, client, admin_h, created_emails):
        users = client.get(f"{API}/users", headers=admin_h).json()
        active = [u for u in users if u["email"] == EMP["email"]][0]
        r = client.post(f"{API}/users/{active['id']}/rifiuta", headers=admin_h)
        assert r.status_code == 400
        assert "non è più in attesa" in r.json()["detail"]

    def test_approve_requires_admin(self, client, admin_h, created_emails):
        email = self._register(client, created_emails, "NoAuth")
        u = self._find(client, admin_h, email)
        emp = client.post(f"{API}/auth/login", json=EMP)
        assert emp.status_code == 200
        h = {"Authorization": f"Bearer {emp.json()['access_token']}"}
        assert client.post(f"{API}/users/{u['id']}/approva", headers=h).status_code == 403
        assert client.post(f"{API}/users/{u['id']}/rifiuta", headers=h).status_code == 403
        assert client.post(f"{API}/users/{u['id']}/approva").status_code in (401, 403)

    def test_approve_unknown_id_404(self, client, admin_h):
        r = client.post(f"{API}/users/507f1f77bcf86cd799439011/approva", headers=admin_h)
        assert r.status_code == 404


# ---------------------------------------------------------------- pending excluded from counters
class TestPendingExcluded:
    def test_pending_not_counted_in_catering(self, client, admin_h, created_emails):
        # baseline counters
        cat = client.post(f"{API}/caterings", headers=admin_h, json={
            "titolo": "TEST_Conteggio Pending",
            "data": "2026-12-20", "ora_inizio": "18:00", "ora_fine": "23:00",
            "luogo": "Test", "personale_richiesto": 3, "descrizione": ""}).json()
        cid = cat["id"]
        try:
            before = client.get(f"{API}/caterings/{cid}", headers=admin_h).json()
            base_senza = before["senza_risposta"]

            email = new_email()
            created_emails.append(email)
            client.post(f"{API}/auth/register", json={
                "nome": "TESTPend", "cognome": "Conteggio", "email": email,
                "telefono": "", "password": PWD})
            after = client.get(f"{API}/caterings/{cid}", headers=admin_h).json()
            assert after["senza_risposta"] == base_senza, "pending user counted in senza_risposta"

            groups = after["gruppi"]
            all_emails = [m.get("email") for g in groups.values() if isinstance(g, list) for m in g]
            assert email not in all_emails, "pending user appears in catering groups"

            # after approval it should appear in 'in_attesa' group / senza_risposta
            users = client.get(f"{API}/users", headers=admin_h).json()
            uid = [u for u in users if u["email"] == email][0]["id"]
            client.post(f"{API}/users/{uid}/approva", headers=admin_h)
            after2 = client.get(f"{API}/caterings/{cid}", headers=admin_h).json()
            assert after2["senza_risposta"] == base_senza + 1
            pend = [m.get("email") for m in after2["gruppi"].get("in_attesa", [])]
            assert email in pend
        finally:
            client.delete(f"{API}/caterings/{cid}", headers=admin_h)


# ---------------------------------------------------------------- regressions
class TestRegressions:
    def test_files_query_token_rejected(self, client, admin_h):
        r = client.get(f"{API}/files/whatever/x.jpg?auth=sometoken")
        assert r.status_code in (401, 403), r.status_code

    def test_file_ownership_enforced(self, client, admin_h):
        emp = client.post(f"{API}/auth/login", json=EMP)
        h = {"Authorization": f"Bearer {emp.json()['access_token']}"}
        # unknown/unowned file -> 404 or 403, never 200
        r = client.get(f"{API}/files/profile/other-user.jpg", headers=h)
        assert r.status_code in (403, 404), r.status_code

    def test_refresh_blocks_inactive_user(self, client, admin_h, created_emails):
        email = new_email()
        created_emails.append(email)
        client.post(f"{API}/auth/register", json={
            "nome": "TESTRefresh", "cognome": "Stato", "email": email,
            "telefono": "", "password": PWD})
        users = client.get(f"{API}/users", headers=admin_h).json()
        uid = [u for u in users if u["email"] == email][0]["id"]
        client.post(f"{API}/users/{uid}/approva", headers=admin_h)
        s = requests.Session()
        lr = s.post(f"{API}/auth/login", json={"email": email, "password": PWD})
        assert lr.status_code == 200
        refresh = lr.json()["refresh_token"]
        ok = s.post(f"{API}/auth/refresh", headers={"X-Refresh-Token": refresh})
        assert ok.status_code == 200, ok.text
        # deactivate then refresh must fail
        client.put(f"{API}/users/{uid}", headers=admin_h, json={"stato": "disattivato"})
        s2 = requests.Session()
        bad = s2.post(f"{API}/auth/refresh", headers={"X-Refresh-Token": refresh})
        assert bad.status_code == 403, bad.status_code

    def test_lockout_after_5_failures(self, client):
        email = f"lock_{uuid.uuid4().hex[:8]}@impastocatering.it"
        codes = []
        for _ in range(6):
            codes.append(client.post(f"{API}/auth/login", json={"email": email, "password": "wrong"}).status_code)
            time.sleep(0.1)
        assert codes[:5] == [401] * 5, codes
        assert codes[5] == 429, codes

    def test_core_admin_and_employee_flow(self, client, admin_h):
        # dashboard + stats reachable for admin
        assert client.get(f"{API}/dashboard", headers=admin_h).status_code == 200
        assert client.get(f"{API}/stats", headers=admin_h).status_code == 200
        emp = client.post(f"{API}/auth/login", json=EMP)
        h = {"Authorization": f"Bearer {emp.json()['access_token']}"}
        assert client.get(f"{API}/users", headers=h).status_code == 403
        assert client.get(f"{API}/stats", headers=h).status_code == 403
        assert client.get(f"{API}/caterings", headers=h).status_code == 200

    def test_disponibilita_persistence(self, client, admin_h):
        cat = client.post(f"{API}/caterings", headers=admin_h, json={
            "titolo": "TEST_Disponibilita Reg", "data": "2026-12-21",
            "ora_inizio": "18:00", "ora_fine": "23:00", "luogo": "Test",
            "personale_richiesto": 2, "descrizione": ""}).json()
        cid = cat["id"]
        try:
            emp = client.post(f"{API}/auth/login", json=EMP)
            h = {"Authorization": f"Bearer {emp.json()['access_token']}"}
            r = client.put(f"{API}/caterings/{cid}/disponibilita", headers=h,
                            json={"stato": "disponibile"})
            assert r.status_code == 200, r.text
            lst = client.get(f"{API}/caterings", headers=h).json()
            mine = [c for c in lst if c["id"] == cid][0]
            assert mine["mia_disponibilita"] == "disponibile"
            det = client.get(f"{API}/caterings/{cid}", headers=admin_h).json()
            assert det["confermati"] >= 1
        finally:
            client.delete(f"{API}/caterings/{cid}", headers=admin_h)
