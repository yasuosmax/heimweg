"""Sucht die beste Verbindung samt Plan B und baut den Satz, der gesagt wird.

Kein Modell beteiligt: Jede Zeit im Satz stammt aus einer Quelle. Ohne Quelle gibt es keine Zeit.
"""
import datetime as dt
from dataclasses import dataclass

from . import quellen
from .quellen import BERLIN, Ort, QuelleWeg, Verbindung

NAHE_KM = 1.0
GLEICHSTAND = dt.timedelta(minutes=10)      # So viel spaeter darf eine bequemere Verbindung ankommen
UMWEG_PLAN_B = dt.timedelta(minutes=30)     # So viel spaeter darf Plan B sein, um den Umstieg zu meiden
LINK_BAHN = "https://www.bahn.de/buchung/fahrplan/suche"
SATZ_KEINE_DATEN = ("Ich habe gerade keine verlässlichen Live-Daten, deshalb nenne ich dir keine Zeit. "
                    "Schau bitte im DB Navigator nach.")


@dataclass
class Einstellungen:
    fern_schwelle_min: int = 30
    knapp_min: int = 5
    meldeschwelle_min: int = 3
    nacht_luecke_min: int = 120


@dataclass
class Ergebnis:
    status: str                 # "ok", "heute_nichts", "keine", "quelle_weg", "schon_da"
    start: Ort
    ziel: Ort
    haupt: Verbindung | None
    plan_b: Verbindung | None
    fern: Verbindung | None
    warnungen: list[str]
    satz: str
    link: str
    stand: dt.datetime


def knappe_umstiege(v: Verbindung, einst: Einstellungen) -> list[int]:
    return [m for _, m in _umstiege(v) if m < einst.knapp_min]


def _umstiege(v: Verbindung) -> list[tuple[str, int]]:
    zuege = v.zuege
    return [(a.nach, round((b.ab - a.an).total_seconds() / 60)) for a, b in zip(zuege, zuege[1:])]


def _brauchbar(verbindungen: list[Verbindung]) -> list[Verbindung]:
    return [v for v in verbindungen if v.zuege and not any(a.ausfall for a in v.abschnitte)]


def _rangfolge(verbindungen: list[Verbindung], einst: Einstellungen) -> list[Verbindung]:
    if not verbindungen:
        return []
    beste = min(v.an for v in verbindungen)
    gruppe = [v for v in verbindungen if v.an - beste <= GLEICHSTAND]
    sieger = min(gruppe, key=lambda v: (bool(knappe_umstiege(v, einst)), v.umstiege, v.an))
    return [sieger] + sorted((v for v in verbindungen if v is not sieger), key=lambda v: v.an)


def _erste_echte(reihe: list[Verbindung], jetzt: dt.datetime) -> tuple[Verbindung | None, list[Verbindung]]:
    """Die erste Verbindung, die auch die Gegenprobe ohne Ausfall uebersteht, und der Rest dahinter."""
    for i, v in enumerate(reihe):
        geprueft = quellen.gegenprobe(v, jetzt)
        if not any(a.ausfall for a in geprueft.abschnitte):
            return geprueft, reihe[i + 1:]
    return None, []


def _plan_b(haupt: Verbindung, pool: list[Verbindung], jetzt: dt.datetime) -> Verbindung | None:
    erster = haupt.zuege[0].trip_id
    orte = {o for o, _ in _umstiege(haupt)}
    kandidaten = sorted((v for v in pool if v.zuege[0].trip_id != erster), key=lambda v: v.an)
    if not kandidaten:
        return None
    frueheste = kandidaten[0].an
    anders = [v for v in kandidaten if not orte & {o for o, _ in _umstiege(v)} and v.an - frueheste <= UMWEG_PLAN_B]
    return _erste_echte(anders + kandidaten, jetzt)[0]


def _abfahrt(a: quellen.Abschnitt) -> str:
    gleis = f" von Gleis {a.gleis_ab}" if a.gleis_ab else ""
    return f"{a.linie} um {a.ab:%H:%M}{gleis}"


def _satz_haupt(v: Verbindung, ziel: Ort) -> str:
    z = v.zuege[0]
    weg = ""
    if v.abschnitte[0].art == "fuss":
        minuten = round((z.ab - v.abschnitte[0].ab).total_seconds() / 60)
        if minuten:
            weg = f"{minuten} Minuten zu Fuß zu {z.von}, dann "
    orte = [o for o, _ in _umstiege(v)]
    rest = "direkt" if not orte else f"{len(orte)} Umstieg{'e' if len(orte) > 1 else ''} in {', '.join(orte)}"
    return f"Nach {ziel.name}: {weg}{_abfahrt(z)}, an {v.an:%H:%M}, {rest}."


def _daten_hinweis(v: Verbindung) -> str:
    zuege = v.zuege
    if any(a.geprueft == "abweichung" for a in zuege):
        return "Die Quellen widersprechen sich, ich nehme den schlechteren Wert."
    if not any(a.live for a in zuege):
        return "Das ist nach Fahrplan, ohne Live-Daten."
    if any(a.geprueft == "ohne_gegenprobe" for a in zuege):
        return "Das ist ohne Gegenprobe der Bahn."
    return ""


def finde_weg(start: Ort, ziel: Ort, jetzt: dt.datetime, nur_regio: bool = False,
              einst: Einstellungen = Einstellungen()) -> Ergebnis:
    jetzt = jetzt.astimezone(BERLIN)
    leer = {"start": start, "ziel": ziel, "haupt": None, "plan_b": None, "fern": None, "warnungen": [],
            "link": LINK_BAHN, "stand": jetzt}
    if quellen.abstand_km(start, ziel) < NAHE_KM:
        return Ergebnis("schon_da", satz=f"Du bist schon an {ziel.name}.", **leer)
    try:
        regio = _brauchbar(quellen.plane(start, ziel, jetzt, nur_regio=True))
        alle = regio if nur_regio else _brauchbar(quellen.plane(start, ziel, jetzt))
    except QuelleWeg:
        return Ergebnis("quelle_weg", satz=SATZ_KEINE_DATEN, **leer)

    warnungen = []
    haupt, rest = _erste_echte(_rangfolge(regio, einst), jetzt)
    schnellster = None if nur_regio else _erste_echte(_rangfolge([v for v in alle if v.fern], einst), jetzt)[0]
    fern = None
    if haupt is None and schnellster is not None:
        haupt, schnellster = schnellster, None
        warnungen.append("Das geht nur mit Fernverkehr, der kostet extra.")
    if haupt is None:
        return Ergebnis("keine", satz=f"Ich finde keine Verbindung nach {ziel.name}. Schau bitte im DB Navigator nach.",
                        **leer)
    if schnellster is not None and haupt.an - schnellster.an >= dt.timedelta(minutes=einst.fern_schwelle_min):
        fern = schnellster
    plan_b = _plan_b(haupt, rest or alle, jetzt)

    status = "ok"
    nacht = jetzt.hour >= 20 or jetzt.hour < 5
    luecke = dt.timedelta(minutes=einst.nacht_luecke_min)
    abfahrt = haupt.zuege[0].ab
    erste = min([abfahrt] + ([schnellster.zuege[0].ab] if schnellster else []))
    if nacht and erste - jetzt >= luecke:
        status = "heute_nichts"
        warnungen.insert(0, f"Heute fährt nichts mehr. Die erste Verbindung geht um {erste:%H:%M}.")
    elif nacht and plan_b is not None and plan_b.zuege[0].ab - abfahrt >= luecke:
        warnungen.append(f"Das ist die letzte Verbindung heute, danach erst um {plan_b.zuege[0].ab:%H:%M}.")
    warnungen += [f"Knapper Umstieg in {o}: {m} Minuten." for o, m in _umstiege(haupt) if m < einst.knapp_min]
    hinweis = _daten_hinweis(haupt)
    if hinweis:
        warnungen.append(hinweis)

    saetze = warnungen + [_satz_haupt(haupt, ziel)]
    if plan_b is None:
        saetze.append("Ich habe keinen Plan B gefunden.")
    else:
        saetze.append(f"Plan B: {_abfahrt(plan_b.zuege[0])}, an {plan_b.an:%H:%M}.")
    if fern is not None:
        gewinn = round((haupt.an - fern.an).total_seconds() / 60)
        zubringer = fern.zuege[0]
        fernzug = next(a for a in fern.zuege if a.art == "fern")
        weg = _abfahrt(zubringer) if zubringer is fernzug else f"{_abfahrt(zubringer)}, dann {fernzug.linie}"
        saetze.append(f"Mit Fernverkehr {gewinn} Minuten schneller: {weg}, an {fern.an:%H:%M}, kostet extra.")
    leer.update(haupt=haupt, plan_b=plan_b, fern=fern, warnungen=warnungen)
    return Ergebnis(status, satz=" ".join(saetze), **leer)


def plan_b_satz(e: Ergebnis, meiden: set[str]) -> str:
    """Fuer eine Meldung unterwegs: die beste Verbindung, die nicht mit einem der gemiedenen Zuege beginnt."""
    for v in (e.haupt, e.plan_b):
        if v is not None and v.zuege[0].trip_id not in meiden:
            return f"Plan B: {_abfahrt(v.zuege[0])}, an {v.an:%H:%M}."
    return "Einen Plan B finde ich gerade nicht, schau im DB Navigator nach."


def _abschnitt_karte(a: quellen.Abschnitt) -> dict:
    return {"art": a.art, "linie": a.linie, "von": a.von, "nach": a.nach, "ab": f"{a.ab:%H:%M}", "an": f"{a.an:%H:%M}",
            "gleis_ab": a.gleis_ab, "gleis_an": a.gleis_an, "live": a.live, "geprueft": a.geprueft,
            "verspaetung": round((a.ab - a.ab_plan).total_seconds() / 60)}


def _verbindung_karte(v: Verbindung | None, einst: Einstellungen) -> dict | None:
    if v is None:
        return None
    return {"los": f"{v.ab:%H:%M}", "ab": f"{v.zuege[0].ab:%H:%M}", "an": f"{v.an:%H:%M}", "umstiege": v.umstiege,
            "dauer": v.dauer_min, "abschnitte": [_abschnitt_karte(a) for a in v.abschnitte],
            "wechsel": [{"ort": o, "minuten": m, "knapp": m < einst.knapp_min} for o, m in _umstiege(v)]}


def karte(e: Ergebnis, einst: Einstellungen = Einstellungen()) -> dict:
    """Das Ergebnis als reine Daten fuer eine Anzeige, Zeiten schon in Berliner Zeit."""
    return {"status": e.status, "ziel": e.ziel.name, "satz": e.satz, "warnungen": e.warnungen, "link": e.link,
            "stand": f"{e.stand:%H:%M}", "haupt": _verbindung_karte(e.haupt, einst),
            "plan_b": _verbindung_karte(e.plan_b, einst), "fern": _verbindung_karte(e.fern, einst)}
