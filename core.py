from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import os
from datetime import datetime, timezone, timedelta
from typing import Annotated, Any, Optional

import bcrypt
import jwt
from bson import ObjectId
from fastapi import HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

client = AsyncIOMotorClient(os.environ["MONGO_URL"])
db = client[os.environ["DB_NAME"]]

JWT_ALGORITHM = "HS256"


def _to_str_id(v: Any) -> Any:
    if isinstance(v, ObjectId):
        return str(v)
    return v


PyObjectId = Annotated[str, BeforeValidator(_to_str_id)]


class BaseDocument(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: Optional[PyObjectId] = Field(default=None, alias="_id")

    def to_mongo(self) -> dict:
        doc = self.model_dump(by_alias=True, exclude_none=True)
        doc.pop("_id", None)
        return doc

    @classmethod
    def from_mongo(cls, doc: dict):
        if doc is None:
            return None
        return cls(**doc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def get_jwt_secret() -> str:
    return os.environ["JWT_SECRET"]


def create_access_token(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "exp": now_utc() + timedelta(hours=12),
        "type": "access",
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    payload = {"sub": user_id, "exp": now_utc() + timedelta(days=7), "type": "refresh"}
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


def public_user(user: dict) -> dict:
    return {
        "id": str(user["_id"]),
        "nome": user.get("nome", ""),
        "cognome": user.get("cognome", ""),
        "email": user.get("email", ""),
        "telefono": user.get("telefono", ""),
        "ruolo": user.get("ruolo", "dipendente"),
        "stato": user.get("stato", "attivo"),
        "foto_url": user.get("foto_url"),
        "created_at": user.get("created_at"),
    }


async def get_current_user(request: Request) -> dict:
    token = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    if not token:
        token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Non autenticato")
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Sessione scaduta")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token non valido")
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Tipo di token non valido")
    try:
        user = await db.users.find_one({"_id": ObjectId(payload["sub"])})
    except Exception:
        raise HTTPException(status_code=401, detail="Token non valido")
    if not user:
        raise HTTPException(status_code=401, detail="Utente non trovato")
    if user.get("stato") != "attivo":
        raise HTTPException(status_code=403, detail="Account disattivato")
    return user


async def require_admin(request: Request) -> dict:
    user = await get_current_user(request)
    if user.get("ruolo") != "admin":
        raise HTTPException(status_code=403, detail="Accesso riservato all'amministratore")
    return user


async def log_activity(actor: dict, azione: str, dettaglio: str, catering_id: Optional[str] = None):
    await db.activity_logs.insert_one(
        {
            "user_id": str(actor["_id"]),
            "user_nome": f"{actor.get('nome','')} {actor.get('cognome','')}".strip(),
            "azione": azione,
            "dettaglio": dettaglio,
            "catering_id": catering_id,
            "created_at": iso_now(),
        }
    )
