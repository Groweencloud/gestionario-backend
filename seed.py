from datetime import timedelta
import os
import secrets

from core import db, hash_password, iso_now, now_utc

DEMO_PASSWORD = os.environ.get("DEMO_EMPLOYEE_PASSWORD") or secrets.token_urlsafe(12)

DEMO_EMPLOYEES = [
    ("Marco", "Rossi", "marco.rossi@impastocatering.it", "+39 320 1112221"),
    ("Luca", "Bianchi", "luca.bianchi@impastocatering.it", "+39 320 1112222"),
    ("Andrea", "Romano", "andrea.romano@impastocatering.it", "+39 320 1112223"),
    ("Matteo", "Esposito", "matteo.esposito@impastocatering.it", "+39 320 1112224"),
    ("Giovanni", "Russo", "giovanni.russo@impastocatering.it", "+39 320 1112225"),
    ("Stefano", "Greco", "stefano.greco@impastocatering.it", "+39 320 1112226"),
    ("Paolo", "Marino", "paolo.marino@impastocatering.it", "+39 320 1112227"),
    ("Davide", "Conti", "davide.conti@impastocatering.it", "+39 320 1112228"),
]


async def seed_demo_data():
    if await db.caterings.count_documents({}) > 0:
        return

    ids = {}
    for nome, cognome, email, tel in DEMO_EMPLOYEES:
        existing = await db.users.find_one({"email": email})
        if existing:
            ids[email] = existing["_id"]
            continue
        res = await db.users.insert_one(
            {
                "nome": nome,
                "cognome": cognome,
                "email": email,
                "telefono": tel,
                "password_hash": hash_password(DEMO_PASSWORD),
                "ruolo": "dipendente",
                "stato": "attivo",
                "foto_url": None,
                "created_at": iso_now(),
            }
        )
        ids[email] = res.inserted_id

    base = now_utc()
    eventi = [
        {
            "titolo": "Matrimonio Rossi",
            "descrizione": "Servizio completo con buffet e cena placée. Divisa nera obbligatoria.",
            "data": (base + timedelta(days=12)).strftime("%Y-%m-%d"),
            "ora_inizio": "18:00",
            "ora_fine": "23:30",
            "luogo": "Villa Igiea",
            "indirizzo": "Salita Belmonte 43, Palermo",
            "indicazioni": "Ingresso di servizio sul lato mare.",
            "personale_richiesto": 12,
            "referente": "Mattia Lo Verde",
            "referente_telefono": "+39 331 4455667",
            "stato": "in_attesa_personale",
            "risposte": {"disponibile": 5, "non_disponibile": 2},
        },
        {
            "titolo": "Evento Aziendale Tecnolab",
            "descrizione": "Coffee break e finger food per 200 persone.",
            "data": (base + timedelta(days=5)).strftime("%Y-%m-%d"),
            "ora_inizio": "09:00",
            "ora_fine": "14:00",
            "luogo": "Centro Congressi Palermo",
            "indirizzo": "Via Ugo La Malfa 100, Palermo",
            "indicazioni": "Parcheggio interno disponibile.",
            "personale_richiesto": 6,
            "referente": "Giulia Marino",
            "referente_telefono": "+39 340 9988776",
            "stato": "personale_completo",
            "risposte": {"disponibile": 6, "non_disponibile": 1},
        },
        {
            "titolo": "Battesimo Esposito",
            "descrizione": "Pranzo in giardino, allestimento tavoli tondi.",
            "data": (base + timedelta(days=22)).strftime("%Y-%m-%d"),
            "ora_inizio": "12:30",
            "ora_fine": "17:00",
            "luogo": "Baglio Conca d'Oro",
            "indirizzo": "Via Aurelio Zancla 19, Monreale",
            "indicazioni": "Ritrovo staff alle 11:00.",
            "personale_richiesto": 8,
            "referente": "Antonio Esposito",
            "referente_telefono": "+39 328 1122334",
            "stato": "programmato",
            "risposte": {"disponibile": 2, "non_disponibile": 0},
        },
        {
            "titolo": "Gala Fondazione Sicilia",
            "descrizione": "Cena di gala con servizio al tavolo.",
            "data": (base - timedelta(days=20)).strftime("%Y-%m-%d"),
            "ora_inizio": "20:00",
            "ora_fine": "01:00",
            "luogo": "Palazzo Branciforte",
            "indirizzo": "Via Bara all'Olivella 2, Palermo",
            "indicazioni": "",
            "personale_richiesto": 10,
            "referente": "Chiara Fiore",
            "referente_telefono": "+39 333 7766554",
            "stato": "terminato",
            "risposte": {"disponibile": 7, "non_disponibile": 1},
        },
    ]

    emails = [e[2] for e in DEMO_EMPLOYEES]
    for ev in eventi:
        risposte = ev.pop("risposte")
        data_evento = ev["data"]
        ev["scadenza_risposta"] = f"{data_evento}T09:00:00"
        ev["created_at"] = iso_now()
        ev["updated_at"] = iso_now()
        ev["risposte_riaperte"] = False
        res = await db.caterings.insert_one(ev)
        cid = str(res.inserted_id)
        idx = 0
        for _ in range(risposte["disponibile"]):
            if idx >= len(emails):
                break
            await db.availabilities.insert_one(
                {"catering_id": cid, "user_id": str(ids[emails[idx]]), "stato": "disponibile",
                 "note": "", "updated_at": iso_now()}
            )
            idx += 1
        for _ in range(risposte["non_disponibile"]):
            if idx >= len(emails):
                break
            await db.availabilities.insert_one(
                {"catering_id": cid, "user_id": str(ids[emails[idx]]), "stato": "non_disponibile",
                 "note": "Impegno personale", "updated_at": iso_now()}
            )
            idx += 1
