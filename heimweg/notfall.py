"""Notfall-Seite: Heimweg von einem fremden Handy, mit PIN und sonst nichts.

Kennt keine Mails, keine Termine, keinen Assistenten. Ohne gueltige PIN-Einstellung bleibt alles zu.
Nichts wird gespeichert, Standort und PIN kommen nie ins Protokoll.
"""
import asyncio
import datetime as dt
import hashlib
import hmac
import json
import os
import pathlib
import secrets
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from . import quellen, suche
from .pin import hash_gueltig, pin_passt

ORDNER = pathlib.Path(__file__).parent
COOKIE = "heim"
SITZUNG_S = 15 * 60
KOERPER_MAX = 4096
ORT_MAX = 80
SPERRE_ADRESSE = (5, 15 * 60)       # Fehlversuche, Sekunden
SPERRE_GESAMT = (30, 60 * 60)
SUCHEN_SITZUNG = (10, 10 * 60)
SUCHEN_TAG = 60
KOPF = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                               "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    "Cache-Control": "no-store", "X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff", "Permissions-Policy": "geolocation=(self)",
}


def _adresse(request: Request) -> str:
    """Hinter Caddy steht die echte Adresse im letzten Eintrag, davor kann jeder alles schreiben."""
    host = request.client.host if request.client else ""
    weiter = request.headers.get("x-forwarded-for", "")
    if host in ("", "127.0.0.1", "::1") and weiter:      # leer: Unix-Socket, dort steht nur der Webserver
        return weiter.split(",")[-1].strip()
    return host


class Bremse:
    """Fehlversuche je Adresse und insgesamt.

    ponytail: nur im Speicher, ein Neustart leert sie. Reicht, weil von aussen niemand neu starten kann.
    """

    def __init__(self):
        self.je_adresse: dict[str, list[float]] = {}
        self.gesamt: list[float] = []

    def _aufraeumen(self, jetzt: float):
        self.gesamt = [t for t in self.gesamt if jetzt - t < SPERRE_GESAMT[1]]
        for adresse in list(self.je_adresse):
            self.je_adresse[adresse] = [t for t in self.je_adresse[adresse] if jetzt - t < SPERRE_ADRESSE[1]]
            if not self.je_adresse[adresse]:
                del self.je_adresse[adresse]

    def gesperrt(self, adresse: str) -> bool:
        self._aufraeumen(time.time())
        return (len(self.gesamt) >= SPERRE_GESAMT[0]
                or len(self.je_adresse.get(adresse, ())) >= SPERRE_ADRESSE[0])

    def fehlversuch(self, adresse: str):
        jetzt = time.time()
        self.gesamt.append(jetzt)
        self.je_adresse.setdefault(adresse, []).append(jetzt)


async def _koerper(request: Request) -> dict:
    roh = b""
    async for stueck in request.stream():
        roh += stueck
        if len(roh) > KOERPER_MAX:
            raise HTTPException(413)
    try:
        daten = json.loads(roh)
    except ValueError:
        return {}
    return daten if isinstance(daten, dict) else {}


def _zahl(wert, grenze: float) -> float | None:
    if isinstance(wert, bool) or not isinstance(wert, (int, float)) or abs(wert) > grenze:
        return None
    return float(wert)


def erstellen(umgebung) -> FastAPI:
    pin_wert = umgebung.get("HEIM_PIN_HASH", "")
    ziele = [z.strip() for z in umgebung.get("HEIM_ZIELE", "").split(";") if z.strip()]
    offen = hash_gueltig(pin_wert) and bool(ziele)
    quellen.einrichten(umgebung.get("DB_CLIENT_ID", ""), umgebung.get("DB_API_KEY", ""))
    schluessel = secrets.token_bytes(32)
    bremse = Bremse()
    suchen: dict[str, list[float]] = {}
    tag = {"datum": None, "zahl": 0}
    ziel_orte: dict[str, quellen.Ort] = {}
    schloss = asyncio.Lock()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def unterschrift(ablauf: int) -> str:
        return hmac.new(schluessel, str(ablauf).encode(), hashlib.sha256).hexdigest()

    def sitzung_gueltig(wert: str) -> bool:
        ablauf, _, zeichen = str(wert or "").partition(".")
        return (ablauf.isdigit() and hmac.compare_digest(zeichen, unterschrift(int(ablauf)))
                and int(ablauf) > time.time())

    def suche_erlaubt(sitzung: str) -> bool:
        jetzt = time.time()
        heute = dt.date.today()
        if tag["datum"] != heute:
            tag.update(datum=heute, zahl=0)
        spuren = [t for t in suchen.get(sitzung, []) if jetzt - t < SUCHEN_SITZUNG[1]]
        if len(spuren) >= SUCHEN_SITZUNG[0] or tag["zahl"] >= SUCHEN_TAG:
            return False
        suchen[sitzung] = spuren + [jetzt]
        tag["zahl"] += 1
        return True

    @app.middleware("http")
    async def rahmen(request, call_next):
        if not offen:
            return PlainTextResponse("Gesperrt.", 503, headers=KOPF)
        antwort = await call_next(request)
        antwort.headers.update(KOPF)
        return antwort

    @app.get("/")
    def seite():
        return HTMLResponse((ORDNER / "notfall.html").read_text(encoding="utf-8"))

    @app.get("/notfall.js")
    def skript():
        return Response((ORDNER / "notfall.js").read_text(encoding="utf-8"), media_type="text/javascript")

    @app.get("/notfall.css")
    def stil():
        return Response((ORDNER / "notfall.css").read_text(encoding="utf-8"), media_type="text/css")

    @app.post("/pin")
    async def pin(request: Request):
        adresse = _adresse(request)
        eingabe = (await _koerper(request)).get("pin")
        async with schloss:     # Pruefen und Zaehlen am Stueck, sonst laeuft eine Salve an der Bremse vorbei
            if bremse.gesperrt(adresse):
                return JSONResponse({"ok": False}, 429, headers={"Retry-After": str(SPERRE_ADRESSE[1])})
            if isinstance(eingabe, str) and eingabe and await asyncio.to_thread(pin_passt, eingabe, pin_wert):
                ablauf = int(time.time()) + SITZUNG_S
                antwort = JSONResponse({"ok": True, "ziele": ziele})
                antwort.set_cookie(COOKIE, f"{ablauf}.{unterschrift(ablauf)}", max_age=SITZUNG_S, httponly=True,
                                   secure=True, samesite="strict")
                return antwort
            bremse.fehlversuch(adresse)
        return JSONResponse({"ok": False}, 403)

    @app.post("/suche")
    async def weg_suchen(request: Request):
        sitzung = request.cookies.get(COOKIE, "")
        if not sitzung_gueltig(sitzung):
            return JSONResponse({"ok": False}, 403)
        daten = await _koerper(request)
        ziel_name, ort = daten.get("ziel"), daten.get("ort")
        if ziel_name not in ziele:
            return JSONResponse({"ok": False}, 400)
        if ort is not None:
            if not isinstance(ort, str) or not ort.strip() or len(ort) > ORT_MAX:
                return JSONResponse({"ok": False}, 400)
            start = None
        else:
            lat, lon = _zahl(daten.get("lat"), 90), _zahl(daten.get("lon"), 180)
            if lat is None or lon is None:
                return JSONResponse({"ok": False}, 400)
            start = quellen.Ort("Dein Standort", lat, lon)
        if not suche_erlaubt(sitzung):
            return JSONResponse({"ok": False}, 429, headers={"Retry-After": "600"})
        try:
            if start is None:
                orte = await asyncio.to_thread(quellen.finde_ort, ort.strip())
                if not orte:
                    return {"status": "ort_unbekannt", "satz": "Diesen Ort finde ich nicht. Versuch es mit einem Bahnhof."}
                start = orte[0]
            if ziel_name not in ziel_orte:
                ziel_orte[ziel_name] = (await asyncio.to_thread(quellen.finde_ort, ziel_name))[0]
        except (quellen.QuelleWeg, IndexError):
            return {"status": "quelle_weg", "satz": suche.SATZ_KEINE_DATEN, "link": "https://www.bahn.de/"}
        ergebnis = await asyncio.to_thread(suche.finde_weg, start, ziel_orte[ziel_name], dt.datetime.now(quellen.BERLIN),
                                           nur_regio=daten.get("nur_regio") is True)
        return suche.karte(ergebnis)

    return app


app = erstellen(os.environ)
