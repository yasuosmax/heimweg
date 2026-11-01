"""Datenquellen: Transitous sucht Verbindungen, die DB-Tafel prueft sie gegen.

Alles hier liest nur (GET). Faellt eine Quelle aus, kommt QuelleWeg, nie eine geratene Angabe.
Transitous-Regeln: User-Agent mit Name, Version und Kontakt, unter 720 /plan-Aufrufe je Tag.
"""
import datetime as dt
import json
import logging
import math
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from zoneinfo import ZoneInfo

import httpx

VERSION = "0.1.0"
USER_AGENT = f"heimweg/{VERSION} (+https://github.com/yasuosmax/heimweg)"
BASIS = "https://api.transitous.org/api"
DB_BASIS = "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1"
BERLIN = ZoneInfo("Europe/Berlin")
PLAN_DECKEL = 600                   # unter der Transitous-Schwelle von 720 je Tag
FUSSWEG_MAX_S = 45 * 60             # Transitous sucht sonst nur Haltestellen bis 15 Minuten Fussweg

ARTEN_FERN = ("HIGHSPEED_RAIL", "LONG_DISTANCE", "NIGHT_RAIL")
ARTEN_REGIO = ("TRAM", "SUBWAY", "FERRY", "BUS", "SUBURBAN", "REGIONAL_RAIL", "REGIONAL_FAST_RAIL",
               "FUNICULAR", "AERIAL_LIFT")
SCHLAGWORT_FERN = {"HIGHSPEED_RAIL": "ICE", "LONG_DISTANCE": "Fernzug", "NIGHT_RAIL": "Nachtzug"}
TOLERANZ_S = 60                     # so viel duerfen die Quellen auseinanderliegen und gelten als einig
GEPLANT_MERKEN_S = 600              # Der Plan einer Stunde aendert sich kaum, die Aenderungen schon
AENDERUNG_MERKEN_S = 20
TAFEL_VORLAUF = dt.timedelta(hours=18)      # weiter im Voraus hat die Tafel nichts

log = logging.getLogger("heimweg")


class QuelleWeg(Exception):
    """Die Quelle ist nicht erreichbar oder liefert Unbrauchbares."""


@dataclass(frozen=True)
class Ort:
    name: str
    lat: float
    lon: float
    stop_id: str | None = None


@dataclass
class Halt:
    name: str
    ab_plan: dt.datetime | None
    ab: dt.datetime | None
    gleis: str | None
    ausfall: bool
    an_plan: dt.datetime | None = None
    an: dt.datetime | None = None


@dataclass
class Abschnitt:
    art: str                        # "fuss", "nah" oder "fern"
    linie: str
    von: str
    nach: str
    ab_plan: dt.datetime
    ab: dt.datetime
    an_plan: dt.datetime
    an: dt.datetime
    gleis_ab: str | None
    gleis_an: str | None
    live: bool
    ausfall: bool
    trip_id: str | None
    halte: list[Halt] = field(default_factory=list)
    geprueft: str = "nur_fahrplan"  # "bestaetigt", "abweichung", "ohne_gegenprobe", "nur_fahrplan"
    zugnummer: str | None = None


@dataclass
class Verbindung:
    abschnitte: list[Abschnitt]

    @property
    def zuege(self) -> list[Abschnitt]:
        return [a for a in self.abschnitte if a.art != "fuss"]

    @property
    def ab(self) -> dt.datetime:
        return self.abschnitte[0].ab

    @property
    def an(self) -> dt.datetime:
        return self.abschnitte[-1].an

    @property
    def umstiege(self) -> int:
        return max(0, len(self.zuege) - 1)

    @property
    def dauer_min(self) -> int:
        return round((self.an - self.ab).total_seconds() / 60)

    @property
    def fern(self) -> bool:
        return any(a.art == "fern" for a in self.abschnitte)


_client = None
_plan_tag = None
_plan_zahl = 0


def client_setzen(client):
    global _client
    _client = client


def zaehler_leeren():
    global _plan_tag, _plan_zahl
    _plan_tag, _plan_zahl = None, 0


@dataclass
class TafelZug:
    linie: str
    nummer: str | None
    ab_plan: dt.datetime | None
    ab: dt.datetime | None
    gleis: str | None
    an_plan: dt.datetime | None
    an: dt.datetime | None
    gleis_an: str | None
    ausfall: bool


_db_schluessel = ("", "")
_eva: dict = {}
_gemerkt: dict = {}


def einrichten(db_client_id: str, db_api_key: str) -> None:
    """Schluessel fuer die DB-Tafel. Ohne sie bleibt jede Verbindung "ohne_gegenprobe"."""
    global _db_schluessel
    _db_schluessel = (db_client_id or "", db_api_key or "")
    _eva.clear()
    _gemerkt.clear()


def _holen() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=10)
    return _client


def _anfrage(pfad: str, params: dict):
    """GET an Transitous, ein zweiter Versuch bei Netzfehlern. Alles Unbrauchbare wird QuelleWeg."""
    fehler = None
    for _ in range(2):
        try:
            antwort = _holen().get(BASIS + pfad, params=params)
            if antwort.status_code >= 500:
                fehler = f"Status {antwort.status_code}"
                continue
            antwort.raise_for_status()
            return antwort.json()
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            fehler = type(e).__name__
    raise QuelleWeg(f"Transitous: {fehler}")


def zeit(text: str) -> dt.datetime:
    """Zeitstempel von Transitous (UTC) als Zeit in Europe/Berlin."""
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(BERLIN)


def _zeit_oder_nichts(text):
    return zeit(text) if text else None


def abstand_km(a: Ort, b: Ort) -> float:
    breite = math.radians((a.lat + b.lat) / 2)
    x = math.radians(b.lon - a.lon) * math.cos(breite)
    y = math.radians(b.lat - a.lat)
    return math.hypot(x, y) * 6371


def _platz(ort: Ort) -> str:
    """Haltestellen ueber ihre Kennung, Standorte auf rund 100 m gerundet: Transitous ist ein fremder Dienst."""
    return ort.stop_id or f"{round(ort.lat, 3)},{round(ort.lon, 3)}"


def _linie(leg: dict) -> str:
    kurz = str(leg.get("routeShortName") or leg.get("displayName") or "").strip()
    wort = SCHLAGWORT_FERN.get(leg.get("mode"))
    return f"{wort} {kurz}".strip() if wort else (kurz or "Zug")


def _nummer(leg: dict) -> str | None:
    nummer = str(leg.get("tripShortName") or "").lstrip("0")
    return nummer if nummer.isdigit() else None


def _halt(platz: dict) -> Halt:
    return Halt(platz.get("name") or "", _zeit_oder_nichts(platz.get("scheduledDeparture")),
                _zeit_oder_nichts(platz.get("departure")), platz.get("track"), bool(platz.get("cancelled")),
                _zeit_oder_nichts(platz.get("scheduledArrival")), _zeit_oder_nichts(platz.get("arrival")))


def _abschnitt(leg: dict) -> Abschnitt:
    modus = leg.get("mode") or ""
    fuss = modus == "WALK"
    von, nach = leg.get("from") or {}, leg.get("to") or {}
    return Abschnitt(
        art="fuss" if fuss else "fern" if modus in ARTEN_FERN else "nah",
        linie="" if fuss else _linie(leg), von=von.get("name") or "", nach=nach.get("name") or "",
        ab_plan=zeit(leg.get("scheduledStartTime") or leg["startTime"]), ab=zeit(leg["startTime"]),
        an_plan=zeit(leg.get("scheduledEndTime") or leg["endTime"]), an=zeit(leg["endTime"]),
        gleis_ab=von.get("track"), gleis_an=nach.get("track"), live=bool(leg.get("realTime")),
        ausfall=bool(leg.get("cancelled") or leg.get("tripCancelled") or von.get("cancelled") or nach.get("cancelled")),
        trip_id=leg.get("tripId"), zugnummer=None if fuss else _nummer(leg),
        halte=[] if fuss else [_halt(von)] + [_halt(p) for p in leg.get("intermediateStops") or []] + [_halt(nach)])


def plane(von: Ort, nach: Ort, jetzt: dt.datetime, nur_regio: bool = False) -> list[Verbindung]:
    global _plan_tag, _plan_zahl
    if _plan_tag != jetzt.date():
        _plan_tag, _plan_zahl = jetzt.date(), 0
    if _plan_zahl >= PLAN_DECKEL:
        raise QuelleWeg("Tagesdeckel fuer Suchen erreicht")
    _plan_zahl += 1
    arten = ARTEN_REGIO if nur_regio else ARTEN_REGIO + ARTEN_FERN
    daten = _anfrage("/v5/plan", {
        "fromPlace": _platz(von), "toPlace": _platz(nach),
        "time": jetzt.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "numItineraries": 6, "transitModes": ",".join(arten), "maxPreTransitTime": FUSSWEG_MAX_S})
    try:
        return [Verbindung([_abschnitt(l) for l in i["legs"]]) for i in daten["itineraries"]]
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise QuelleWeg(f"Transitous: Antwort nicht lesbar ({type(e).__name__})") from e


def zug_stand(trip_id: str) -> Abschnitt:
    daten = _anfrage("/v5/trip", {"tripId": trip_id})
    try:
        return next(_abschnitt(l) for l in daten["legs"] if l.get("mode") != "WALK")
    except (KeyError, TypeError, ValueError, AttributeError, StopIteration) as e:
        raise QuelleWeg("Transitous: Zug nicht gefunden") from e


def stand_holen(a: Abschnitt) -> Abschnitt:
    """Der neue Stand eines Abschnitts. Transitous liefert den ganzen Zug, gebraucht wird nur die eigene Strecke."""
    zug = zug_stand(a.trip_id)
    namen = [h.name for h in zug.halte]
    try:
        i = namen.index(a.von)
        j = namen.index(a.nach, i + 1)
    except ValueError as e:
        raise QuelleWeg("Transitous: Halt nicht im Zug") from e
    von, nach = zug.halte[i], zug.halte[j]
    return replace(a, ab=von.ab or a.ab, an=nach.an or a.an, gleis_ab=von.gleis or a.gleis_ab,
                   gleis_an=nach.gleis or a.gleis_an, live=zug.live, ausfall=zug.ausfall or von.ausfall or nach.ausfall,
                   halte=zug.halte[i:j + 1])


def finde_ort(text: str) -> list[Ort]:
    """Treffer zu einem Orts- oder Bahnhofsnamen, Haltestellen zuerst."""
    daten = _anfrage("/v1/geocode", {"text": text, "language": "de"})
    try:
        treffer = sorted(daten, key=lambda t: t.get("type") != "STOP")
        return [Ort(t["name"], t["lat"], t["lon"], t["id"] if t.get("type") == "STOP" else None) for t in treffer]
    except (KeyError, TypeError) as e:
        raise QuelleWeg("Transitous: Ortssuche nicht lesbar") from e


def bahnhof_muster(name: str) -> str:
    """Transitous sagt "Frankfurt (Main) Hauptbahnhof", die Bahn "Frankfurt(Main)Hbf"."""
    s = name.replace(", ", " ").replace("Hauptbahnhof", "Hbf").replace("Südbahnhof", "Süd")
    s = s.replace(" tief", "").replace(" Bahnhof", "")
    return re.sub(r"\s*\(([^)]*)\)\s*", r"(\1)", s).strip()


def _db_xml(pfad: str) -> ET.Element:
    if not all(_db_schluessel):
        raise QuelleWeg("DB: kein Schluessel")
    try:
        antwort = _holen().get(DB_BASIS + pfad, headers={
            "DB-Client-Id": _db_schluessel[0], "DB-Api-Key": _db_schluessel[1], "Accept": "application/xml"})
        antwort.raise_for_status()
        return ET.fromstring(antwort.text)
    except (httpx.HTTPError, ET.ParseError) as e:
        log.warning("DB-Tafel nicht lesbar (%s)", type(e).__name__)
        raise QuelleWeg(f"DB: {type(e).__name__}") from e


def _gemerkt_oder_holen(pfad: str, dauer: float) -> ET.Element:
    stand = _gemerkt.get(pfad)
    if stand and time.monotonic() - stand[0] < dauer:
        return stand[1]
    wurzel = _db_xml(pfad)
    _gemerkt[pfad] = (time.monotonic(), wurzel)
    return wurzel


def _bahnhofsnummer(bahnhof: str) -> str:
    if bahnhof not in _eva:
        muster = bahnhof_muster(bahnhof)
        stationen = _db_xml("/station/" + urllib.parse.quote(muster, safe="")).findall("station")
        passend = [s for s in stationen if s.get("name") == muster] or stationen[:1]
        _eva[bahnhof] = passend[0].get("eva") if passend else None
    if not _eva[bahnhof]:
        raise QuelleWeg(f"DB: Bahnhof unbekannt ({bahnhof})")
    return _eva[bahnhof]


def _db_zeit(text):
    return dt.datetime.strptime(text, "%y%m%d%H%M").replace(tzinfo=BERLIN) if text else None


def _zug_von_der_tafel(s: ET.Element, aenderung: ET.Element | None) -> TafelZug:
    tl, dp, ar = s.find("tl"), s.find("dp"), s.find("ar")
    neu_dp = aenderung.find("dp") if aenderung is not None else None
    neu_ar = aenderung.find("ar") if aenderung is not None else None

    def wert(alt, neu, plan, geaendert):
        spaeter = neu.get(geaendert) if neu is not None else None
        return spaeter or (alt.get(plan) if alt is not None else None)

    ausfall = any(e is not None and e.get("cs") == "c" for e in (neu_dp, neu_ar))
    art = tl.get("c", "") if tl is not None else ""
    linie = (dp if dp is not None else ar)
    return TafelZug(
        linie=art + (linie.get("l", "") if linie is not None else ""),
        nummer=tl.get("n") if tl is not None else None,
        ab_plan=_db_zeit(dp.get("pt")) if dp is not None else None,
        ab=_db_zeit(wert(dp, neu_dp, "pt", "ct")), gleis=wert(dp, neu_dp, "pp", "cp"),
        an_plan=_db_zeit(ar.get("pt")) if ar is not None else None,
        an=_db_zeit(wert(ar, neu_ar, "pt", "ct")), gleis_an=wert(ar, neu_ar, "pp", "cp"), ausfall=ausfall)


def tafel(bahnhof: str, jetzt: dt.datetime) -> list[TafelZug]:
    """Alle Zuege der Stunde von `jetzt` an einem DB-Bahnhof, mit dem aktuellen Stand."""
    eva = _bahnhofsnummer(bahnhof)
    ort = jetzt.astimezone(BERLIN)
    plan = _gemerkt_oder_holen(f"/plan/{eva}/{ort:%y%m%d}/{ort:%H}", GEPLANT_MERKEN_S)
    aenderungen = {s.get("id"): s for s in _gemerkt_oder_holen(f"/fchg/{eva}", AENDERUNG_MERKEN_S).findall("s")}
    return [_zug_von_der_tafel(s, aenderungen.get(s.get("id"))) for s in plan.findall("s")]


def _zug_suchen(bahnhof: str, wann: dt.datetime, nummer: str, feld: str) -> TafelZug | None:
    try:
        return next((z for z in tafel(bahnhof, wann) if z.nummer == nummer and getattr(z, feld) == wann), None)
    except QuelleWeg:
        return None


def _gleis(wert):
    return (wert or "").replace("Fern", "").strip() or None


def _pruefe(a: Abschnitt, jetzt: dt.datetime) -> Abschnitt:
    if a.art == "fuss" or not a.zugnummer or a.ab_plan - jetzt > TAFEL_VORLAUF:
        return a
    if not all(_db_schluessel):
        return replace(a, geprueft="ohne_gegenprobe")
    ab_zug = _zug_suchen(a.von, a.ab_plan, a.zugnummer, "ab_plan")
    an_zug = _zug_suchen(a.nach, a.an_plan, a.zugnummer, "an_plan")
    if ab_zug is None and an_zug is None:
        return replace(a, geprueft="ohne_gegenprobe")

    neu, abweichung = {}, a.ausfall
    for zug, zeit_feld, gleis_feld, tafel_gleis, tafel_zeit in (
            (ab_zug, "ab", "gleis_ab", "gleis", "ab"), (an_zug, "an", "gleis_an", "gleis_an", "an")):
        if zug is None:
            continue
        unsere, ihre = getattr(a, zeit_feld), getattr(zug, tafel_zeit)
        if ihre and abs((unsere - ihre).total_seconds()) > TOLERANZ_S:
            abweichung = True
        if ihre:
            neu[zeit_feld] = max(unsere, ihre)      # Bei Widerspruch gilt der schlechtere Wert
        gleis_unser, gleis_ihr = _gleis(getattr(a, gleis_feld)), _gleis(getattr(zug, tafel_gleis))
        if gleis_ihr:
            abweichung = abweichung or bool(gleis_unser and gleis_unser != gleis_ihr)
            neu[gleis_feld] = gleis_ihr
    ausfall = a.ausfall or any(z is not None and z.ausfall for z in (ab_zug, an_zug))
    abweichung = abweichung or ausfall
    return replace(a, ausfall=ausfall, geprueft="abweichung" if abweichung else "bestaetigt", **neu)


def gegenprobe(verbindung: Verbindung, jetzt: dt.datetime) -> Verbindung:
    """Vergleicht jeden Zug mit der DB-Tafel. Widersprechen sich die Quellen, gilt der schlechtere Wert."""
    return Verbindung([_pruefe(a, jetzt) for a in verbindung.abschnitte])
