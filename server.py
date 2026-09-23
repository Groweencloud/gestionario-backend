from core import (  # noqa: F401  (loads .env first)
    db,
    BaseDocument,
    PyObjectId,
    create_access_token,
    create_refresh_token,
    get_current_user,
    hash_password,
    iso_now,
    log_activity,
    now_utc,
    public_user,
    require_admin,
    verify_password,
)

import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from bson import ObjectId
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel, EmailStr, Field
from starlette.middleware.cors import CORSMiddleware

import storage
from emailer import queue_send
from seed import seed_demo_data

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Impasto Catering")
api = APIRouter(prefix="/api")

STATI_CATERING = ["programmato", "in_attesa_personale", "personale_completo", "in_corso", "terminato", "annullato"]
STATI_DISPONIBILITA = ["disponibile", "non_disponibile", "in_attesa"]


# ---------------------------------------------------------------- schemi
class LoginIn(BaseModel):
    email: str
    password: str


class ForgotIn(BaseModel):
    email: str


class RegisterIn(BaseModel):
    nome: str = Field(min_length=1)
    cognome: str = Field(min_length=1)
    email: EmailStr
    telefono: str = ""
    password: str = Field(min_length=6)


class ResetIn(BaseModel):
    token: str
    password: str = Field(min_length=6)


class ChangePasswordIn(BaseModel):
    password_attuale: str
    password_nuova: str = Field(min_length=6)


class UserIn(BaseModel):
    nome: str
    cognome: str
    email: EmailStr
    telefono: str = ""
    password: str = Field(min_length=6)
    ruolo: str = "dipendente"
    stato: str = "attivo"


class UserUpdate(BaseModel):
    nome: Optional[str] = None
    cognome: Optional[str] = None
    email: Optional[EmailStr] = None
    telefono: Optional[str] = None
    ruolo: Optional[str] = None
    stato: Optional[str] = None


class ProfileUpdate(BaseModel):
    nome: Optional[str] = None
    cognome: Optional[str] = None
    telefono: Optional[str] = None


class CateringIn(BaseModel):
    titolo: str
    descrizione: str = ""
    data: str
    ora_inizio: str
    ora_fine: str = ""
    luogo: str
    indirizzo: str = ""
    indicazioni: str = ""
    personale_richiesto: int = Field(ge=1)
    referente: str = ""
    referente_telefono: str = ""
    scadenza_risposta: Optional[str] = None
    stato: str = "programmato"
    compenso: Optional[float] = None
    prezzo_a_persona: Optional[float] = None
    numero_persone: Optional[int] = None
    servizi_extra: List[dict] = Field(default_factory=list)
    assigned: Optional[List[str]] = None


class PresenzaIn(BaseModel):
    presente: bool
    importo: Optional[float] = None


class AvailabilityIn(BaseModel):
    stato: str
    note: str = ""


class SettingsIn(BaseModel):
    nome_azienda: str = "Impasto Catering"
    colore_primario: str = "#C85A32"
    logo_url: Optional[str] = None
    notifiche_email: bool = True
    notifiche_interne: bool = True
    giorni_scadenza_default: int = 3
    compenso_default: float = 80.0


# ---------------------------------------------------------------- helper
async def notify(user_id: str, tipo: str, messaggio: str, catering_id: Optional[str] = None):
    await db.notifications.insert_one(
        {
            "user_id": user_id,
            "catering_id": catering_id,
            "tipo": tipo,
            "messaggio": messaggio,
            "letto": False,
            "created_at": iso_now(),
        }
    )


async def get_settings() -> dict:
    s = await db.settings.find_one({"key": "app"})
    if not s:
        s = {"key": "app", **SettingsIn().model_dump()}
        await db.settings.insert_one(dict(s))
    s.pop("_id", None)
    s.pop("key", None)
    return {**SettingsIn().model_dump(), **s}


async def active_employees() -> List[dict]:
    return await db.users.find({"ruolo": "dipendente", "stato": "attivo"}).to_list(500)


async def admins() -> List[dict]:
    return await db.users.find({"ruolo": "admin", "stato": "attivo"}).to_list(50)


async def broadcast_employees(tipo: str, messaggio: str, catering_id: str, titolo_email: str, righe: List[str]):
    settings = await get_settings()
    for emp in await active_employees():
        if settings.get("notifiche_interne", True):
            await notify(str(emp["_id"]), tipo, messaggio, catering_id)
        if settings.get("notifiche_email", True):
            queue_send(emp["email"], titolo_email, [f"Ciao {emp.get('nome','')},"] + righe)


def serialize_catering(doc: dict) -> dict:
    out = dict(doc)
    out["id"] = str(out.pop("_id"))
    return out


async def availability_map(catering_ids: List[str]) -> dict:
    rows = await db.availabilities.find({"catering_id": {"$in": catering_ids}}).to_list(20000)
    out = {}
    for r in rows:
        out.setdefault(r["catering_id"], []).append(r)
    return out


def scadenza_passata(catering: dict) -> bool:
    sc = catering.get("scadenza_risposta")
    if not sc or catering.get("risposte_riaperte"):
        return False
    dt = None
    try:
        dt = datetime.fromisoformat(sc)
    except ValueError:
        return False
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt < now_utc()


async def enrich_caterings(docs: List[dict], user: dict) -> List[dict]:
    ids = [str(d["_id"]) for d in docs]
    amap = await availability_map(ids)
    n_emp = await db.users.count_documents({"ruolo": "dipendente", "stato": "attivo"})
    result = []
    for d in docs:
        cid = str(d["_id"])
        rows = amap.get(cid, [])
        conf = len([r for r in rows if r["stato"] == "disponibile"])
        rif = len([r for r in rows if r["stato"] == "non_disponibile"])
        item = serialize_catering(d)
        item["assigned"] = d.get("assigned", [])
        if user.get("ruolo") == "admin":
            item["prezzo_a_persona"] = d.get("prezzo_a_persona")
            item["numero_persone"] = d.get("numero_persone")
            item["servizi_extra"] = d.get("servizi_extra", [])
            fin = await financial_summary_for_catering(d)
            item["incasso_totale"] = fin["incasso_totale"]
            item["costi_personale_stimati"] = fin["costi_personale_stimati"]
            item["guadagno_netto"] = fin["guadagno_netto"]
        item["confermati"] = conf
        item["non_disponibili"] = rif
        item["senza_risposta"] = max(n_emp - conf - rif, 0)
        item["mancanti"] = max(item["personale_richiesto"] - conf, 0)
        item["scadenza_superata"] = scadenza_passata(d)
        mine = next((r for r in rows if r["user_id"] == str(user["_id"])), None)
        item["mia_disponibilita"] = mine["stato"] if mine else "in_attesa"
        item["mia_nota"] = mine.get("note", "") if mine else ""
        result.append(item)
    return result


async def refresh_stato(catering_id: str):
    doc = await db.caterings.find_one({"_id": ObjectId(catering_id)})
    if not doc or doc["stato"] in ("annullato", "terminato", "in_corso"):
        return doc
    conf = await db.availabilities.count_documents({"catering_id": catering_id, "stato": "disponibile"})
    nuovo = "personale_completo" if conf >= doc["personale_richiesto"] else "in_attesa_personale"
    if nuovo != doc["stato"]:
        await db.caterings.update_one({"_id": ObjectId(catering_id)}, {"$set": {"stato": nuovo}})
        doc["stato"] = nuovo
        if nuovo == "personale_completo":
            for a in await admins():
                await notify(str(a["_id"]), "personale_completo",
                             f"Personale completo per «{doc['titolo']}».", catering_id)
    return doc


async def financial_summary_for_catering(catering: dict) -> dict:
    prezzo = float(catering.get("prezzo_a_persona") or 0)
    persone = int(catering.get("numero_persone") or 0)
    ricavo_base = prezzo * persone
    servizi_extra = []
    for servizio in catering.get("servizi_extra") or []:
        descrizione = str(servizio.get("descrizione", "")).strip()
        importo = float(servizio.get("importo") or 0)
        if descrizione or importo:
            servizi_extra.append({"descrizione": descrizione, "importo": round(importo, 2)})
    totale_extra = sum(servizio["importo"] for servizio in servizi_extra)
    incasso = round(ricavo_base + totale_extra, 2)
    catering_id = str(catering.get("_id") or catering.get("id") or "")
    costi = 0.0
    if catering_id:
        rows = await db.presenze.find({"catering_id": catering_id, "presente": True}).to_list(5000)
        costi = round(sum(float(r.get("importo", 0) or 0) for r in rows), 2)
    guadagno = round(incasso - costi, 2)
    return {
        "prezzo_a_persona": round(prezzo, 2),
        "numero_persone": persone,
        "ricavo_base": round(ricavo_base, 2),
        "servizi_extra": servizi_extra,
        "totale_extra": round(totale_extra, 2),
        "incasso_totale": incasso,
        "costi_personale_stimati": costi,
        "guadagno_netto": guadagno,
    }


# ---------------------------------------------------------------- auth
@api.post("/auth/register")
async def register(payload: RegisterIn):
    email = payload.email.lower()
    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="Esiste già un account con questa email")
    doc = {
        "nome": payload.nome.strip(),
        "cognome": payload.cognome.strip(),
        "email": email,
        "telefono": payload.telefono.strip(),
        "password_hash": hash_password(payload.password),
        "ruolo": "dipendente",
        "stato": "in_attesa",
        "foto_url": None,
        "created_at": iso_now(),
    }
    res = await db.users.insert_one(doc)
    nome_completo = f"{doc['nome']} {doc['cognome']}"
    for a in await admins():
        await notify(str(a["_id"]), "nuova_registrazione",
                     f"Nuova richiesta di registrazione da {nome_completo} ({email}).")
        queue_send(
            a["email"],
            "Nuova richiesta di registrazione",
            [
                f"{nome_completo} ha richiesto un account dipendente.",
                f"Email: {email} — Telefono: {doc['telefono'] or 'non indicato'}",
                "Accedi alla sezione Dipendenti del gestionale per approvare o rifiutare la richiesta.",
            ],
        )
    await db.activity_logs.insert_one(
        {
            "user_id": str(res.inserted_id),
            "user_nome": nome_completo,
            "azione": "registrazione",
            "dettaglio": f"{nome_completo} ha richiesto la registrazione",
            "catering_id": None,
            "created_at": iso_now(),
        }
    )
    return {
        "ok": True,
        "message": "Richiesta inviata. Potrai accedere dopo l'approvazione dell'amministratore.",
    }


@api.post("/auth/login")
async def login(payload: LoginIn, request: Request, response: Response):
    email = payload.email.strip().lower()
    fwd = request.headers.get("X-Forwarded-For", "")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")
    ident = f"login:{ip}:{email}"
    att = await db.login_attempts.find_one({"identifier": ident})
    if att and att.get("count", 0) >= 5:
        locked_until = datetime.fromisoformat(att["last"]) + timedelta(minutes=15)
        if locked_until.replace(tzinfo=timezone.utc) > now_utc():
            raise HTTPException(status_code=429, detail="Troppi tentativi. Riprova tra 15 minuti.")
        await db.login_attempts.delete_one({"identifier": ident})

    user = await db.users.find_one({"email": email})
    if not user or not verify_password(payload.password, user.get("password_hash", "")):
        await db.login_attempts.update_one(
            {"identifier": ident},
            {"$inc": {"count": 1}, "$set": {"last": now_utc().isoformat()}},
            upsert=True,
        )
        raise HTTPException(status_code=401, detail="Email o password non corretti")
    if user.get("stato") == "in_attesa":
        raise HTTPException(
            status_code=403,
            detail="Account in attesa di approvazione da parte dell'amministratore.",
        )
    if user.get("stato") != "attivo":
        raise HTTPException(status_code=403, detail="Account disattivato. Contatta l'amministratore.")

    await db.login_attempts.delete_one({"identifier": ident})
    uid = str(user["_id"])
    access = create_access_token(uid, email)
    refresh = create_refresh_token(uid)
    response.set_cookie("access_token", access, httponly=True, secure=True, samesite="none", max_age=43200, path="/")
    response.set_cookie("refresh_token", refresh, httponly=True, secure=True, samesite="none", max_age=604800, path="/")
    return {"access_token": access, "refresh_token": refresh, "user": public_user(user)}


@api.get("/auth/me")
async def me(user: dict = Depends(get_current_user)):
    return public_user(user)


@api.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")
    return {"ok": True}


@api.post("/auth/refresh")
async def refresh_token_endpoint(request: Request, response: Response):
    import jwt as pyjwt
    from core import get_jwt_secret, JWT_ALGORITHM

    token = request.cookies.get("refresh_token") or (request.headers.get("X-Refresh-Token") or "")
    if not token:
        raise HTTPException(status_code=401, detail="Refresh token mancante")
    try:
        payload = pyjwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
    except pyjwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Refresh token non valido")
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Tipo di token non valido")
    user = await db.users.find_one({"_id": ObjectId(payload["sub"])})
    if not user:
        raise HTTPException(status_code=401, detail="Utente non trovato")
    if user.get("stato") != "attivo":
        raise HTTPException(status_code=403, detail="Account non attivo")
    access = create_access_token(str(user["_id"]), user["email"])
    response.set_cookie("access_token", access, httponly=True, secure=True, samesite="none", max_age=43200, path="/")
    return {"access_token": access}


@api.post("/auth/forgot-password")
async def forgot_password(payload: ForgotIn):
    email = payload.email.strip().lower()
    user = await db.users.find_one({"email": email})
    if user:
        token = secrets.token_urlsafe(32)
        await db.password_reset_tokens.insert_one(
            {
                "token": token,
                "user_id": str(user["_id"]),
                "used": False,
                "expires_at": now_utc() + timedelta(hours=1),
            }
        )
        logger.info(f"[RESET PASSWORD] token per {email}: {token}")
        queue_send(
            email,
            "Reimposta la tua password",
            [
                f"Ciao {user.get('nome','')}, abbiamo ricevuto una richiesta di reimpostazione password.",
                f"Codice di reimpostazione: {token}",
                "Inseriscilo nella pagina «Password dimenticata» del gestionale. Scade tra un'ora.",
                "Se non hai richiesto tu l'operazione, ignora questo messaggio.",
            ],
        )
    return {"ok": True, "message": "Se l'email esiste, riceverai le istruzioni per il reset."}


@api.post("/auth/reset-password")
async def reset_password(payload: ResetIn):
    rec = await db.password_reset_tokens.find_one({"token": payload.token, "used": False})
    if not rec:
        raise HTTPException(status_code=400, detail="Codice non valido o già utilizzato")
    exp = rec["expires_at"]
    if isinstance(exp, str):
        exp = datetime.fromisoformat(exp)
    if exp.replace(tzinfo=timezone.utc) < now_utc():
        raise HTTPException(status_code=400, detail="Codice scaduto")
    await db.users.update_one(
        {"_id": ObjectId(rec["user_id"])}, {"$set": {"password_hash": hash_password(payload.password)}}
    )
    await db.password_reset_tokens.update_one({"_id": rec["_id"]}, {"$set": {"used": True}})
    return {"ok": True}


@api.put("/auth/change-password")
async def change_password(payload: ChangePasswordIn, user: dict = Depends(get_current_user)):
    if not verify_password(payload.password_attuale, user.get("password_hash", "")):
        raise HTTPException(status_code=400, detail="Password attuale non corretta")
    await db.users.update_one(
        {"_id": user["_id"]}, {"$set": {"password_hash": hash_password(payload.password_nuova)}}
    )
    return {"ok": True}


# ---------------------------------------------------------------- profilo
@api.put("/profile")
async def update_profile(payload: ProfileUpdate, user: dict = Depends(get_current_user)):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if updates:
        await db.users.update_one({"_id": user["_id"]}, {"$set": updates})
    fresh = await db.users.find_one({"_id": user["_id"]})
    return public_user(fresh)


@api.post("/profile/photo")
async def upload_photo(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    ext = (file.filename or "img.png").rsplit(".", 1)[-1].lower()
    if ext not in storage.MIME_TYPES:
        raise HTTPException(status_code=400, detail="Formato immagine non supportato")
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Immagine troppo grande (max 5MB)")
    path = f"{storage.APP_NAME}/uploads/{user['_id']}/{uuid.uuid4()}.{ext}"
    result = None
    try:
        result = storage.put_object(path, data, storage.MIME_TYPES[ext])
    except Exception as e:
        logger.error(f"Upload foto fallito: {e}")
        raise HTTPException(status_code=502, detail="Caricamento immagine non riuscito")
    if not result or "path" not in result:
        raise HTTPException(status_code=502, detail="Caricamento immagine non riuscito")
    await db.files.insert_one(
        {
            "storage_path": result["path"],
            "content_type": storage.MIME_TYPES[ext],
            "user_id": str(user["_id"]),
            "is_deleted": False,
            "created_at": iso_now(),
        }
    )
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"foto_url": result["path"]}})
    return {"foto_url": result["path"]}


@api.get("/files/{path:path}")
async def download_file(path: str, user: dict = Depends(get_current_user)):
    rec = await db.files.find_one({"storage_path": path, "is_deleted": False})
    if not rec:
        raise HTTPException(status_code=404, detail="File non trovato")
    if user["ruolo"] != "admin" and rec.get("user_id") != str(user["_id"]):
        raise HTTPException(status_code=403, detail="Accesso non consentito")
    data, ct = storage.get_object(path)
    return Response(content=data, media_type=rec.get("content_type", ct))


# ---------------------------------------------------------------- utenti
@api.get("/users")
async def list_users(admin: dict = Depends(require_admin), q: Optional[str] = None, stato: Optional[str] = None):
    query = {}
    if stato and stato != "tutti":
        query["stato"] = stato
    docs = await db.users.find(query).sort("nome", 1).to_list(1000)
    users = [public_user(u) for u in docs]
    if q:
        t = q.lower()
        users = [u for u in users if t in f"{u['nome']} {u['cognome']} {u['email']}".lower()]
    return users


@api.post("/users")
async def create_user(payload: UserIn, admin: dict = Depends(require_admin)):
    email = payload.email.lower()
    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="Email già registrata")
    doc = {
        "nome": payload.nome,
        "cognome": payload.cognome,
        "email": email,
        "telefono": payload.telefono,
        "password_hash": hash_password(payload.password),
        "ruolo": payload.ruolo if payload.ruolo in ("admin", "dipendente") else "dipendente",
        "stato": payload.stato if payload.stato in ("attivo", "disattivato") else "attivo",
        "foto_url": None,
        "created_at": iso_now(),
    }
    res = await db.users.insert_one(doc)
    doc["_id"] = res.inserted_id
    await log_activity(admin, "creazione_dipendente", f"Creato l'account di {payload.nome} {payload.cognome}")
    return public_user(doc)


@api.get("/users/{user_id}")
async def get_user(user_id: str, admin: dict = Depends(require_admin)):
    u = await db.users.find_one({"_id": ObjectId(user_id)})
    if not u:
        raise HTTPException(status_code=404, detail="Dipendente non trovato")
    rows = await db.availabilities.find({"user_id": user_id}).to_list(5000)
    tot = await db.caterings.count_documents({})
    return {
        **public_user(u),
        "risposte_totali": len(rows),
        "disponibili": len([r for r in rows if r["stato"] == "disponibile"]),
        "non_disponibili": len([r for r in rows if r["stato"] == "non_disponibile"]),
        "eventi_totali": tot,
    }


@api.put("/users/{user_id}")
async def update_user(user_id: str, payload: UserUpdate, admin: dict = Depends(require_admin)):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "email" in updates:
        updates["email"] = updates["email"].lower()
        other = await db.users.find_one({"email": updates["email"], "_id": {"$ne": ObjectId(user_id)}})
        if other:
            raise HTTPException(status_code=400, detail="Email già registrata")
    if updates:
        await db.users.update_one({"_id": ObjectId(user_id)}, {"$set": updates})
    u = await db.users.find_one({"_id": ObjectId(user_id)})
    if not u:
        raise HTTPException(status_code=404, detail="Dipendente non trovato")
    await log_activity(admin, "modifica_dipendente", f"Modificato l'account di {u['nome']} {u['cognome']}")
    return public_user(u)


@api.post("/users/{user_id}/reset-password")
async def admin_reset_password(user_id: str, admin: dict = Depends(require_admin)):
    u = await db.users.find_one({"_id": ObjectId(user_id)})
    if not u:
        raise HTTPException(status_code=404, detail="Dipendente non trovato")
    nuova = "Imp" + secrets.token_urlsafe(6)
    await db.users.update_one({"_id": u["_id"]}, {"$set": {"password_hash": hash_password(nuova)}})
    await log_activity(admin, "reset_password", f"Password reimpostata per {u['nome']} {u['cognome']}")
    queue_send(
        u["email"],
        "La tua password è stata reimpostata",
        ["L'amministratore ha reimpostato la tua password.",
         "Accedi al gestionale con la password temporanea comunicata dal responsabile e modificala dal tuo profilo."],
    )
    return {"password_temporanea": nuova}


@api.post("/users/{user_id}/approva")
async def approva_utente(user_id: str, admin: dict = Depends(require_admin)):
    u = await db.users.find_one({"_id": ObjectId(user_id)})
    if not u:
        raise HTTPException(status_code=404, detail="Utente non trovato")
    if u.get("stato") != "in_attesa":
        raise HTTPException(status_code=400, detail="La richiesta non è in attesa di approvazione")
    await db.users.update_one({"_id": u["_id"]}, {"$set": {"stato": "attivo"}})
    nome = f"{u.get('nome','')} {u.get('cognome','')}".strip()
    await log_activity(admin, "approvazione_account", f"Ha approvato l'account di {nome}")
    await notify(user_id, "account_approvato", "Il tuo account è stato approvato: ora puoi accedere.")
    queue_send(
        u["email"],
        "Account approvato",
        [f"Ciao {u.get('nome','')}, il tuo account è stato approvato dall'amministratore.",
         "Ora puoi accedere al gestionale con l'email e la password scelte in fase di registrazione."],
    )
    fresh = await db.users.find_one({"_id": u["_id"]})
    return public_user(fresh)


@api.post("/users/{user_id}/rifiuta")
async def rifiuta_utente(user_id: str, admin: dict = Depends(require_admin)):
    u = await db.users.find_one({"_id": ObjectId(user_id)})
    if not u:
        raise HTTPException(status_code=404, detail="Utente non trovato")
    if u.get("stato") != "in_attesa":
        raise HTTPException(status_code=400, detail="La richiesta non è più in attesa")
    await db.users.delete_one({"_id": u["_id"]})
    nome = f"{u.get('nome','')} {u.get('cognome','')}".strip()
    await log_activity(admin, "rifiuto_account", f"Ha rifiutato la registrazione di {nome}")
    queue_send(
        u["email"],
        "Richiesta di registrazione non approvata",
        ["La tua richiesta di accesso al gestionale non è stata approvata.",
         "Per maggiori informazioni contatta il responsabile dell'azienda."],
    )
    return {"ok": True}


@api.delete("/users/{user_id}")
async def delete_user(user_id: str, admin: dict = Depends(require_admin)):
    u = await db.users.find_one({"_id": ObjectId(user_id)})
    if not u:
        raise HTTPException(status_code=404, detail="Dipendente non trovato")
    if u.get("ruolo") == "admin":
        raise HTTPException(status_code=400, detail="Non è possibile eliminare un amministratore")
    await db.users.delete_one({"_id": u["_id"]})
    await db.availabilities.delete_many({"user_id": user_id})
    await db.presenze.delete_many({"user_id": user_id})
    await db.notifications.delete_many({"user_id": user_id})
    await log_activity(admin, "eliminazione_dipendente", f"Eliminato l'account di {u['nome']} {u['cognome']}")
    return {"ok": True}


# ---------------------------------------------------------------- catering
@api.get("/caterings")
async def list_caterings(
    user: dict = Depends(get_current_user),
    stato: Optional[str] = None,
    luogo: Optional[str] = None,
    q: Optional[str] = None,
    periodo: Optional[str] = None,
    da: Optional[str] = None,
    a: Optional[str] = None,
    disponibilita: Optional[str] = None,
    dipendente_id: Optional[str] = None,
):
    query = {}
    if stato and stato != "tutti":
        query["stato"] = stato
    oggi = now_utc().strftime("%Y-%m-%d")
    if periodo == "futuri":
        query["data"] = {"$gte": oggi}
    elif periodo == "passati":
        query["data"] = {"$lt": oggi}
    if da or a:
        rng = query.get("data", {})
        if da:
            rng["$gte"] = da
        if a:
            rng["$lte"] = a
        query["data"] = rng
    if user.get("ruolo") != "admin":
        query["$or"] = [
            {"assigned": {"$in": [str(user["_id"]) ]}},
            {"assigned": {"$exists": False}},
            {"assigned": {"$size": 0}},
        ]
    docs = await db.caterings.find(query).sort("data", 1).to_list(2000)
    items = await enrich_caterings(docs, user)
    if luogo:
        t = luogo.lower()
        items = [i for i in items if t in (i.get("luogo", "") + " " + i.get("indirizzo", "")).lower()]
    if q:
        t = q.lower()
        items = [i for i in items if t in (i.get("titolo", "") + " " + i.get("luogo", "")).lower()]
    if disponibilita and disponibilita != "tutti":
        if user["ruolo"] == "admin" and dipendente_id:
            rows = await db.availabilities.find({"user_id": dipendente_id}).to_list(5000)
            m = {r["catering_id"]: r["stato"] for r in rows}
            items = [i for i in items if m.get(i["id"], "in_attesa") == disponibilita]
        else:
            items = [i for i in items if i["mia_disponibilita"] == disponibilita]
    elif dipendente_id:
        rows = await db.availabilities.find({"user_id": dipendente_id}).to_list(5000)
        ids = {r["catering_id"] for r in rows}
        items = [i for i in items if i["id"] in ids]
    return items


@api.post("/caterings")
async def create_catering(payload: CateringIn, admin: dict = Depends(require_admin)):
    settings = await get_settings()
    doc = payload.model_dump()
    if doc["stato"] not in STATI_CATERING:
        doc["stato"] = "programmato"
    if not doc.get("scadenza_risposta"):
        giorni = settings.get("giorni_scadenza_default", 3)
        try:
            base = datetime.fromisoformat(doc["data"])
        except ValueError:
            base = now_utc().replace(tzinfo=None)
        doc["scadenza_risposta"] = (base - timedelta(days=giorni)).strftime("%Y-%m-%dT18:00:00")
    doc["created_at"] = iso_now()
    doc["updated_at"] = iso_now()
    doc["created_by"] = str(admin["_id"])
    doc["risposte_riaperte"] = False
    if doc.get("assigned"):
        doc["assigned"] = [str(x) for x in doc.get("assigned") if x]

    res = await db.caterings.insert_one(doc)
    cid = str(res.inserted_id)
    await log_activity(admin, "creazione_catering", f"Ha creato il catering «{doc['titolo']}»", cid)
    if doc.get("assigned"):
        settings = await get_settings()
        for uid in doc.get("assigned", []):
            try:
                u = await db.users.find_one({"_id": ObjectId(uid)})
            except Exception:
                u = None
            if not u:
                continue
            if settings.get("notifiche_interne", True):
                await notify(str(u["_id"]), "nuovo_catering", f"Sei stato assegnato a: «{doc['titolo']}».", cid)
            if settings.get("notifiche_email", True):
                queue_send(u["email"], f"Nuovo catering: {doc['titolo']}", [
                    f"Sei stato assegnato al catering: {doc['titolo']}.",
                    f"Data: {doc['data']} — Orario: {doc['ora_inizio']} {('- ' + doc['ora_fine']) if doc['ora_fine'] else ''}",
                    f"Luogo: {doc['luogo']} — {doc['indirizzo']}",
                    "Controlla il gestionale per i dettagli.",
                ])
    else:
        await broadcast_employees(
            "nuovo_catering",
            f"Nuovo catering: «{doc['titolo']}» il {doc['data']}. Comunica la tua disponibilità.",
            cid,
            f"Nuovo catering: {doc['titolo']}",
            [
                f"È stato pubblicato un nuovo catering: {doc['titolo']}.",
                f"Data: {doc['data']} — Orario: {doc['ora_inizio']} {('- ' + doc['ora_fine']) if doc['ora_fine'] else ''}",
                f"Luogo: {doc['luogo']} — {doc['indirizzo']}",
                f"Personale richiesto: {doc['personale_richiesto']}",
                "Accedi al gestionale per comunicare la tua disponibilità.",
            ],
        )
    doc["_id"] = res.inserted_id
    return (await enrich_caterings([doc], admin))[0]


@api.get("/caterings/{catering_id}")
async def get_catering(catering_id: str, user: dict = Depends(get_current_user)):
    try:
        doc = await db.caterings.find_one({"_id": ObjectId(catering_id)})
    except Exception:
        raise HTTPException(status_code=404, detail="Catering non trovato")
    if not doc:
        raise HTTPException(status_code=404, detail="Catering non trovato")
    item = (await enrich_caterings([doc], user))[0]
    rows = await db.availabilities.find({"catering_id": catering_id}).to_list(5000)
    stato_by_user = {r["user_id"]: r for r in rows}
    assigned = [str(a) for a in (doc.get("assigned") or []) if a]
    if user.get("ruolo") != "admin":
        if assigned and str(user["_id"]) not in assigned:
            raise HTTPException(status_code=403, detail="Accesso non consentito")
        if assigned:
            obj_ids = []
            for a in assigned:
                try:
                    obj_ids.append(ObjectId(a))
                except Exception:
                    continue
            emp = await db.users.find({"_id": {"$in": obj_ids}, "ruolo": "dipendente", "stato": "attivo"}).sort("nome", 1).to_list(500)
        else:
            emp = await db.users.find({"ruolo": "dipendente", "stato": "attivo"}).sort("nome", 1).to_list(500)
    else:
        emp = await db.users.find({"ruolo": "dipendente", "stato": "attivo"}).sort("nome", 1).to_list(500)
    gruppi = {"disponibile": [], "non_disponibile": [], "in_attesa": []}
    for e in emp:
        r = stato_by_user.get(str(e["_id"]))
        st = r["stato"] if r else "in_attesa"
        entry = {
            "id": str(e["_id"]),
            "nome": e.get("nome", ""),
            "cognome": e.get("cognome", ""),
            "foto_url": e.get("foto_url"),
            "note": (r or {}).get("note", ""),
            "aggiornato": (r or {}).get("updated_at"),
        }
        if user["ruolo"] == "admin":
            entry["telefono"] = e.get("telefono", "")
            entry["email"] = e.get("email", "")
        gruppi[st].append(entry)
    item["gruppi"] = gruppi
    return item


@api.put("/caterings/{catering_id}")
async def update_catering(catering_id: str, payload: CateringIn, admin: dict = Depends(require_admin)):
    old = await db.caterings.find_one({"_id": ObjectId(catering_id)})
    if not old:
        raise HTTPException(status_code=404, detail="Catering non trovato")
    updates = payload.model_dump()
    if updates["stato"] not in STATI_CATERING:
        updates["stato"] = old["stato"]
    if not updates.get("scadenza_risposta"):
        updates["scadenza_risposta"] = old.get("scadenza_risposta")
    updates["updated_at"] = iso_now()
    if updates.get("assigned") is not None:
        updates["assigned"] = [str(x) for x in updates.get("assigned") if x]
    await db.caterings.update_one({"_id": old["_id"]}, {"$set": updates})
    cambi = [
        k for k in ("data", "ora_inizio", "ora_fine", "luogo", "indirizzo", "personale_richiesto", "stato")
        if old.get(k) != updates.get(k)
    ]
    await log_activity(admin, "modifica_catering", f"Ha modificato il catering «{updates['titolo']}»", catering_id)
    if cambi:
        if updates.get("assigned"):
            settings = await get_settings()
            for uid in updates.get("assigned", []):
                try:
                    u = await db.users.find_one({"_id": ObjectId(uid)})
                except Exception:
                    u = None
                if not u:
                    continue
                if settings.get("notifiche_interne", True):
                    await notify(str(u["_id"]), "modifica_catering", f"Il catering «{updates['titolo']}» è stato modificato.", catering_id)
                if settings.get("notifiche_email", True):
                    queue_send(u["email"], f"Catering modificato: {updates['titolo']}", [
                        f"Il catering {updates['titolo']} è stato aggiornato.",
                        f"Data: {updates['data']} — Orario: {updates['ora_inizio']}",
                        f"Luogo: {updates['luogo']} — {updates['indirizzo']}",
                    ])
        else:
            await broadcast_employees(
                "modifica_catering",
                f"Il catering «{updates['titolo']}» è stato modificato ({', '.join(cambi)}).",
                catering_id,
                f"Catering modificato: {updates['titolo']}",
                [
                    f"Il catering {updates['titolo']} è stato aggiornato.",
                    f"Data: {updates['data']} — Orario: {updates['ora_inizio']}",
                    f"Luogo: {updates['luogo']} — {updates['indirizzo']}",
                    "Controlla il gestionale e verifica la tua disponibilità.",
                ],
            )
    await refresh_stato(catering_id)
    doc = await db.caterings.find_one({"_id": old["_id"]})
    return (await enrich_caterings([doc], admin))[0]


@api.post("/caterings/{catering_id}/riapri")
async def riapri_risposte(catering_id: str, admin: dict = Depends(require_admin)):
    doc = await db.caterings.find_one({"_id": ObjectId(catering_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Catering non trovato")
    await db.caterings.update_one({"_id": doc["_id"]}, {"$set": {"risposte_riaperte": True}})
    await log_activity(admin, "riapertura_risposte", f"Ha riaperto le risposte per «{doc['titolo']}»", catering_id)
    await broadcast_employees(
        "riapertura",
        f"Le risposte per «{doc['titolo']}» sono state riaperte.",
        catering_id,
        f"Risposte riaperte: {doc['titolo']}",
        ["L'amministratore ha riaperto le risposte per questo catering.",
         "Puoi aggiornare la tua disponibilità dal gestionale."],
    )
    return {"ok": True}


@api.delete("/caterings/{catering_id}")
async def delete_catering(catering_id: str, admin: dict = Depends(require_admin)):
    doc = await db.caterings.find_one({"_id": ObjectId(catering_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Catering non trovato")
    if await db.presenze.count_documents({"catering_id": catering_id, "pagato": True}):
        raise HTTPException(
            status_code=400,
            detail="Il catering ha compensi già pagati: annullalo invece di eliminarlo per conservare lo storico.",
        )
    await db.caterings.delete_one({"_id": doc["_id"]})
    await db.availabilities.delete_many({"catering_id": catering_id})
    await db.presenze.delete_many({"catering_id": catering_id})
    await log_activity(admin, "eliminazione_catering", f"Ha eliminato il catering «{doc['titolo']}»", catering_id)
    return {"ok": True}


@api.put("/caterings/{catering_id}/disponibilita")
async def set_availability(catering_id: str, payload: AvailabilityIn, user: dict = Depends(get_current_user)):
    if payload.stato not in STATI_DISPONIBILITA:
        raise HTTPException(status_code=400, detail="Stato disponibilità non valido")
    doc = await db.caterings.find_one({"_id": ObjectId(catering_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Catering non trovato")
    if doc["stato"] == "annullato":
        raise HTTPException(status_code=400, detail="Il catering è stato annullato")
    if user["ruolo"] != "admin" and scadenza_passata(doc):
        raise HTTPException(status_code=400, detail="Il termine per rispondere è scaduto")
    await db.availabilities.update_one(
        {"catering_id": catering_id, "user_id": str(user["_id"])},
        {"$set": {"stato": payload.stato, "note": payload.note, "updated_at": iso_now()}},
        upsert=True,
    )
    label = {"disponibile": "Disponibile", "non_disponibile": "Non disponibile", "in_attesa": "In attesa"}[payload.stato]
    nome = f"{user.get('nome','')} {user.get('cognome','')}".strip()
    await log_activity(user, "disponibilita", f"{nome} ha comunicato «{label}» per «{doc['titolo']}»", catering_id)
    for a in await admins():
        await notify(str(a["_id"]), "disponibilita", f"{nome}: {label} — «{doc['titolo']}»", catering_id)
    await refresh_stato(catering_id)
    fresh = await db.caterings.find_one({"_id": ObjectId(catering_id)})
    return (await enrich_caterings([fresh], user))[0]


# ---------------------------------------------------------------- presenze e compensi
async def compenso_base(catering: dict) -> float:
    if catering.get("compenso") not in (None, ""):
        return float(catering["compenso"])
    settings = await get_settings()
    return float(settings.get("compenso_default", 80.0))


@api.get("/caterings/{catering_id}/presenze")
async def list_presenze(catering_id: str, admin: dict = Depends(require_admin)):
    doc = await db.caterings.find_one({"_id": ObjectId(catering_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Catering non trovato")
    base = await compenso_base(doc)
    disp = {
        r["user_id"]: r["stato"]
        for r in await db.availabilities.find({"catering_id": catering_id}).to_list(5000)
    }
    pres = {p["user_id"]: p for p in await db.presenze.find({"catering_id": catering_id}).to_list(5000)}
    emp = await db.users.find({"ruolo": "dipendente", "stato": "attivo"}).sort("nome", 1).to_list(500)
    righe = []
    for e in emp:
        uid = str(e["_id"])
        p = pres.get(uid)
        righe.append(
            {
                "user_id": uid,
                "nome": e.get("nome", ""),
                "cognome": e.get("cognome", ""),
                "disponibilita": disp.get(uid, "in_attesa"),
                "presente": bool(p and p.get("presente")),
                "importo": float(p["importo"]) if p else base,
                "pagato": bool(p and p.get("pagato")),
                "pagato_at": p.get("pagato_at") if p else None,
            }
        )
    return {"compenso_base": base, "righe": righe}


@api.put("/caterings/{catering_id}/presenze/{user_id}")
async def set_presenza(
    catering_id: str, user_id: str, payload: PresenzaIn, admin: dict = Depends(require_admin)
):
    doc = await db.caterings.find_one({"_id": ObjectId(catering_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Catering non trovato")
    u = await db.users.find_one({"_id": ObjectId(user_id)})
    if not u:
        raise HTTPException(status_code=404, detail="Dipendente non trovato")
    if payload.importo is not None and payload.importo < 0:
        raise HTTPException(status_code=400, detail="Il compenso non può essere negativo")
    esistente = await db.presenze.find_one({"catering_id": catering_id, "user_id": user_id})
    importo = payload.importo
    if importo is None:
        importo = float(esistente["importo"]) if esistente else await compenso_base(doc)
    if esistente and esistente.get("pagato") and not payload.presente:
        raise HTTPException(status_code=400, detail="Compenso già pagato: non è possibile rimuovere la presenza")
    await db.presenze.update_one(
        {"catering_id": catering_id, "user_id": user_id},
        {
            "$set": {
                "presente": payload.presente,
                "importo": float(importo),
                "updated_at": iso_now(),
            },
            "$setOnInsert": {"pagato": False, "pagato_at": None, "created_at": iso_now()},
        },
        upsert=True,
    )
    nome = f"{u.get('nome','')} {u.get('cognome','')}".strip()
    if payload.presente:
        await log_activity(
            admin, "presenza", f"Ha confermato la presenza di {nome} a «{doc['titolo']}» ({importo:.2f} €)", catering_id
        )
        await notify(
            user_id,
            "compenso",
            f"Presenza confermata per «{doc['titolo']}»: {importo:.2f} € accreditati nel tuo portafoglio.",
            catering_id,
        )
    else:
        await log_activity(admin, "presenza", f"Ha rimosso la presenza di {nome} a «{doc['titolo']}»", catering_id)
    return {"ok": True, "importo": float(importo), "presente": payload.presente}


async def righe_compensi(user_id: str) -> List[dict]:
    pres = await db.presenze.find({"user_id": user_id, "presente": True}).to_list(5000)
    if not pres:
        return []
    ids = [ObjectId(p["catering_id"]) for p in pres]
    caterings = {str(c["_id"]): c for c in await db.caterings.find({"_id": {"$in": ids}}).to_list(5000)}
    righe = []
    for p in pres:
        c = caterings.get(p["catering_id"])
        if not c:
            continue
        righe.append(
            {
                "id": str(p["_id"]),
                "catering_id": p["catering_id"],
                "titolo": c.get("titolo", ""),
                "data": c.get("data", ""),
                "luogo": c.get("luogo", ""),
                "importo": float(p.get("importo", 0)),
                "pagato": bool(p.get("pagato")),
                "pagato_at": p.get("pagato_at"),
            }
        )
    righe.sort(key=lambda r: r["data"], reverse=True)
    return righe


@api.get("/wallet")
async def wallet(user: dict = Depends(get_current_user)):
    righe = await righe_compensi(str(user["_id"]))
    da_riscuotere = sum(r["importo"] for r in righe if not r["pagato"])
    pagato = sum(r["importo"] for r in righe if r["pagato"])
    return {
        "totale": round(da_riscuotere + pagato, 2),
        "da_riscuotere": round(da_riscuotere, 2),
        "pagato": round(pagato, 2),
        "eventi_lavorati": len(righe),
        "righe": righe,
    }


@api.get("/compensi")
async def compensi(admin: dict = Depends(require_admin)):
    emp = await active_employees()
    settings = await get_settings()
    out = []
    for e in emp:
        righe = await righe_compensi(str(e["_id"]))
        da_riscuotere = sum(r["importo"] for r in righe if not r["pagato"])
        pagato = sum(r["importo"] for r in righe if r["pagato"])
        out.append(
            {
                "user_id": str(e["_id"]),
                "nome": f"{e.get('nome','')} {e.get('cognome','')}".strip(),
                "email": e.get("email", ""),
                "eventi_lavorati": len(righe),
                "da_riscuotere": round(da_riscuotere, 2),
                "pagato": round(pagato, 2),
                "totale": round(da_riscuotere + pagato, 2),
                "righe": righe,
            }
        )
    out.sort(key=lambda r: -r["da_riscuotere"])
    return {
        "compenso_default": float(settings.get("compenso_default", 80.0)),
        "totale_da_pagare": round(sum(r["da_riscuotere"] for r in out), 2),
        "totale_pagato": round(sum(r["pagato"] for r in out), 2),
        "dipendenti": out,
    }


@api.post("/compensi/{presenza_id}/paga")
async def paga_compenso(presenza_id: str, admin: dict = Depends(require_admin)):
    p = await db.presenze.find_one({"_id": ObjectId(presenza_id)})
    if not p or not p.get("presente"):
        raise HTTPException(status_code=404, detail="Compenso non trovato")
    nuovo = not p.get("pagato")
    
    # Registra o rimuove il timestamp esatto di pagamento
    timestamp_pagamento = iso_now() if nuovo else None
    await db.presenze.update_one(
        {"_id": p["_id"]},
        {"$set": {"pagato": nuovo, "pagato_at": timestamp_pagamento}},
    )
    
    u = await db.users.find_one({"_id": ObjectId(p["user_id"])})
    c = await db.caterings.find_one({"_id": ObjectId(p["catering_id"])})
    nome = f"{u.get('nome','')} {u.get('cognome','')}".strip() if u else "dipendente"
    titolo = c.get("titolo", "evento") if c else "evento"
    await log_activity(
        admin,
        "pagamento",
        f"{'Ha segnato come pagato' if nuovo else 'Ha annullato il pagamento di'} {p['importo']:.2f} € a {nome} per «{titolo}»",
        p["catering_id"],
    )
    if nuovo:
        await notify(p["user_id"], "pagamento", f"Compenso di {p['importo']:.2f} € per «{titolo}» segnato come pagato.", p["catering_id"])
    return {"ok": True, "pagato": nuovo, "pagato_at": timestamp_pagamento}


@api.post("/compensi/dipendente/{user_id}/paga-tutto")
async def paga_tutto(user_id: str, admin: dict = Depends(require_admin)):
    u = await db.users.find_one({"_id": ObjectId(user_id)})
    if not u:
        raise HTTPException(status_code=404, detail="Dipendente non trovato")
    righe = await db.presenze.find({"user_id": user_id, "presente": True, "pagato": False}).to_list(5000)
    totale = sum(float(r.get("importo", 0)) for r in righe)
    if not righe:
        raise HTTPException(status_code=400, detail="Nessun compenso da pagare")
    
    timestamp_corrente = iso_now()
    await db.presenze.update_many(
        {"user_id": user_id, "presente": True, "pagato": False},
        {"$set": {"pagato": True, "pagato_at": timestamp_corrente}},
    )
    
    nome = f"{u.get('nome','')} {u.get('cognome','')}".strip()
    await log_activity(admin, "pagamento", f"Ha saldato {totale:.2f} € a {nome} ({len(righe)} eventi)")
    await notify(user_id, "pagamento", f"Sono stati segnati come pagati {totale:.2f} € ({len(righe)} eventi).")
    queue_send(
        u["email"],
        "Compenso saldato",
        [f"Ciao {u.get('nome','')}, il tuo compenso di {totale:.2f} € relativo a {len(righe)} eventi è stato segnato come pagato.",
         "Puoi consultare il dettaglio nella sezione Portafoglio del gestionale."],
    )
    return {"ok": True, "totale": round(totale, 2), "eventi": len(righe), "pagato_at": timestamp_corrente}


# ---------------------------------------------------------------- dashboard e statistiche
@api.get("/dashboard")
async def dashboard(user: dict = Depends(get_current_user)):
    oggi = now_utc().strftime("%Y-%m-%d")
    query = {"data": {"$gte": oggi}, "stato": {"$ne": "annullato"}}
    if user.get("ruolo") != "admin":
        query["$or"] = [
            {"assigned": {"$in": [str(user["_id"]) ]}},
            {"assigned": {"$exists": False}},
            {"assigned": {"$size": 0}},
        ]
    docs = await db.caterings.find(query).sort("data", 1).to_list(500)
    items = await enrich_caterings(docs, user)
    oggi_items = [i for i in items if i["data"] == oggi]
    return {
        "prossimi": items[:20],
        "totale_prossimi": len(items),
        "oggi": oggi_items,
        "totale_oggi": len(oggi_items),
        "disponibili": sum(i["confermati"] for i in items),
        "non_disponibili": sum(i["non_disponibili"] for i in items),
        "senza_risposta": sum(i["senza_risposta"] for i in items),
        "eventi_incompleti": len([i for i in items if i["mancanti"] > 0]),
    }


@api.get("/stats")
async def stats(admin: dict = Depends(require_admin)):
    docs = await db.caterings.find({}).to_list(5000)
    emp = await active_employees()
    rows = await db.availabilities.find({}).to_list(50000)
    per_utente = {}
    for r in rows:
        per_utente.setdefault(r["user_id"], []).append(r)
    totale_eventi = len(docs)
    dipendenti = []
    for e in emp:
        rs = per_utente.get(str(e["_id"]), [])
        disp = len([r for r in rs if r["stato"] == "disponibile"])
        risposte = len([r for r in rs if r["stato"] in ("disponibile", "non_disponibile")])
        dipendenti.append(
            {
                "nome": f"{e.get('nome','')} {e.get('cognome','')}".strip(),
                "disponibilita_pct": round(disp / totale_eventi * 100) if totale_eventi else 0,
                "risposta_pct": round(risposte / totale_eventi * 100) if totale_eventi else 0,
                "disponibili": disp,
            }
        )
    dipendenti.sort(key=lambda d: -d["disponibilita_pct"])
    per_mese = {}
    for d in docs:
        mese = (d.get("data") or "")[:7]
        if mese:
            per_mese[mese] = per_mese.get(mese, 0) + 1
    bilancio = {"totale_incassi": 0.0, "totale_costi_personale": 0.0, "totale_guadagno_netto": 0.0, "catering": []}
    for d in docs:
        fin = await financial_summary_for_catering(d)
        bilancio["totale_incassi"] += fin["incasso_totale"]
        bilancio["totale_costi_personale"] += fin["costi_personale_stimati"]
        bilancio["totale_guadagno_netto"] += fin["guadagno_netto"]
        bilancio["catering"].append(
            {
                "id": str(d["_id"]),
                "titolo": d.get("titolo", ""),
                "data": d.get("data", ""),
                "luogo": d.get("luogo", ""),
                "prezzo_a_persona": fin["prezzo_a_persona"],
                "numero_persone": fin["numero_persone"],
                "totale_extra": fin["totale_extra"],
                "incasso_totale": fin["incasso_totale"],
                "costi_personale_stimati": fin["costi_personale_stimati"],
                "guadagno_netto": fin["guadagno_netto"],
            }
        )
    bilancio["catering"].sort(key=lambda x: x["data"], reverse=True)
    bilancio["totale_incassi"] = round(bilancio["totale_incassi"], 2)
    bilancio["totale_costi_personale"] = round(bilancio["totale_costi_personale"], 2)
    bilancio["totale_guadagno_netto"] = round(bilancio["totale_guadagno_netto"], 2)
    return {
        "totale_catering": totale_eventi,
        "completati": len([d for d in docs if d["stato"] == "terminato"]),
        "annullati": len([d for d in docs if d["stato"] == "annullato"]),
        "in_programma": len([d for d in docs if d["stato"] in ("programmato", "in_attesa_personale", "personale_completo")]),
        "totale_dipendenti": len(emp),
        "dipendenti": dipendenti,
        "eventi_per_mese": [{"mese": k, "eventi": v} for k, v in sorted(per_mese.items())],
        "stati": [
            {"stato": s, "valore": len([d for d in docs if d["stato"] == s])}
            for s in STATI_CATERING
        ],
        "bilancio": bilancio,
    }


# ---------------------------------------------------------------- notifiche / log / impostazioni
@api.get("/notifications")
async def list_notifications(user: dict = Depends(get_current_user)):
    docs = await db.notifications.find({"user_id": str(user["_id"])}).sort("created_at", -1).to_list(200)
    return [{**{k: v for k, v in d.items() if k != "_id"}, "id": str(d["_id"])} for d in docs]


@api.post("/notifications/{notification_id}/read")
async def read_notification(notification_id: str, user: dict = Depends(get_current_user)):
    await db.notifications.update_one(
        {"_id": ObjectId(notification_id), "user_id": str(user["_id"])}, {"$set": {"letto": True}}
    )
    return {"ok": True}


@api.post("/notifications/read-all")
async def read_all(user: dict = Depends(get_current_user)):
    await db.notifications.update_many({"user_id": str(user["_id"])}, {"$set": {"letto": True}})
    return {"ok": True}


@api.get("/activity")
async def activity(admin: dict = Depends(require_admin), limit: int = 100):
    docs = await db.activity_logs.find({}).sort("created_at", -1).to_list(limit)
    return [{**{k: v for k, v in d.items() if k != "_id"}, "id": str(d["_id"])} for d in docs]


@api.get("/settings")
async def read_settings(admin: dict = Depends(require_admin)):
    return await get_settings()


@api.put("/settings")
async def write_settings(payload: SettingsIn, admin: dict = Depends(require_admin)):
    await db.settings.update_one({"key": "app"}, {"$set": payload.model_dump()}, upsert=True)
    await log_activity(admin, "impostazioni", "Ha aggiornato le impostazioni dell'azienda")
    return await get_settings()


@api.get("/")
async def root():
    return {"app": "Impasto Catering API", "ok": True}


app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    await db.users.create_index("email", unique=True)
    await db.availabilities.create_index([("catering_id", 1), ("user_id", 1)], unique=True)
    await db.caterings.create_index("assigned")
    await db.notifications.create_index([("user_id", 1)])
    await db.presenze.create_index([("catering_id", 1), ("user_id", 1)], unique=True)
    await db.login_attempts.create_index("identifier")
    admin_email = os.environ["ADMIN_EMAIL"].lower()
    admin_password = os.environ["ADMIN_PASSWORD"]
    existing = await db.users.find_one({"email": admin_email})
    if not existing:
        await db.users.insert_one(
            {
                "nome": "Admin",
                "cognome": "Impasto",
                "email": admin_email,
                "telefono": "+39 091 000000",
                "password_hash": hash_password(admin_password),
                "ruolo": "admin",
                "stato": "attivo",
                "foto_url": None,
                "created_at": iso_now(),
            }
        )
    elif not verify_password(admin_password, existing.get("password_hash", "")):
        await db.users.update_one(
            {"_id": existing["_id"]}, {"$set": {"password_hash": hash_password(admin_password)}}
        )
    await seed_demo_data()
    try:
        storage.init_storage()
    except Exception as e:
        logger.error(f"Storage init fallita: {e}")


@app.on_event("shutdown")
async def shutdown():
    from core import client

    client.close()
