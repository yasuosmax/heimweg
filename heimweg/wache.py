"""Vergleicht den neuen Stand einer Verbindung mit dem gemerkten und sagt, was zu melden ist.

Reine Logik ohne Netz und ohne Zustand: Wer sie aufruft, merkt sich `gemeldet` und holt die Daten.
"""
import datetime as dt
from dataclasses import dataclass

from .quellen import Verbindung
from .suche import Einstellungen

STUMM_FRIST = dt.timedelta(minutes=5)
HARTE_GRENZE = dt.timedelta(hours=3)        # So lange nach der Plan-Ankunft endet jede Begleitung
VOR_ABFAHRT = dt.timedelta(minutes=10)
VOR_UMSTIEG = dt.timedelta(minutes=5)


@dataclass
class Ereignis:
    art: str        # "verspaetung", "gleis", "ausfall", "anschluss", "lebenszeichen", "quelle_stumm", "angekommen"
    text: str
    schluessel: str
    braucht_plan_b: bool


def _minuten(spaeter: dt.datetime, frueher: dt.datetime) -> int:
    return round((spaeter - frueher).total_seconds() / 60)


def _letzte_meldung(gemeldet: set[str], trip_id: str) -> int:
    kopf = f"verspaetung:{trip_id}:"
    return max((int(s[len(kopf):]) for s in gemeldet if s.startswith(kopf)), default=0)


def _verspaetung(alt, a, jetzt, gemeldet, einst) -> Ereignis | None:
    if a.ab > jetzt:
        minuten = _minuten(a.ab, alt.ab_plan)
        text = f"{a.linie} hat {minuten} Minuten Verspätung, Abfahrt jetzt {a.ab:%H:%M}."
    elif jetzt < a.an:
        minuten = _minuten(a.an, alt.an_plan)
        text = f"{a.linie} kommt {minuten} Minuten später an, jetzt um {a.an:%H:%M}."
    else:
        return None
    if minuten < einst.meldeschwelle_min or minuten - _letzte_meldung(gemeldet, a.trip_id) < einst.meldeschwelle_min:
        return None
    return Ereignis("verspaetung", text, f"verspaetung:{a.trip_id}:{minuten}", True)


def pruefe(gemerkt: Verbindung, neu: Verbindung | None, jetzt: dt.datetime, gemeldet: set[str],
           stumm_seit: dt.datetime | None, einst: Einstellungen = Einstellungen()) -> list[Ereignis]:
    ende = Ereignis("angekommen", "Du bist da.", "angekommen", False)
    if jetzt >= gemerkt.abschnitte[-1].an_plan + HARTE_GRENZE:
        return [ende]
    if neu is None:
        if stumm_seit is None or jetzt - stumm_seit < STUMM_FRIST:
            return []
        schluessel = f"quelle_stumm:{stumm_seit:%H%M}"
        if schluessel in gemeldet:
            return []
        return [Ereignis("quelle_stumm", "Ich sehe deinen Zug gerade nicht mehr. Schau bitte im DB Navigator nach.",
                         schluessel, False)]
    if jetzt >= neu.an:
        return [ende]

    ereignisse = []
    paare = list(zip(gemerkt.zuege, neu.zuege))
    for alt, a in paare:
        if a.ausfall:
            ereignisse.append(Ereignis("ausfall", f"{a.linie} um {alt.ab_plan:%H:%M} fällt aus.",
                                       f"ausfall:{a.trip_id}", True))
            continue
        spaeter = _verspaetung(alt, a, jetzt, gemeldet, einst)
        if spaeter:
            ereignisse.append(spaeter)
        if a.ab > jetzt and alt.gleis_ab and a.gleis_ab and alt.gleis_ab != a.gleis_ab:
            ereignisse.append(Ereignis("gleis", f"{a.linie}: Gleiswechsel in {a.von}, Gleis {a.gleis_ab} statt {alt.gleis_ab}.",
                                       f"gleis:{a.trip_id}:{a.gleis_ab}", True))

    for (alt1, a1), (alt2, a2) in zip(paare, paare[1:]):
        rest = _minuten(a2.ab, a1.an)
        if a2.ab > jetzt and rest < einst.knapp_min and rest < _minuten(alt2.ab_plan, alt1.an_plan):
            text = (f"Der Anschluss in {a1.nach} klappt nicht mehr." if rest <= 0
                    else f"Der Anschluss in {a1.nach} wird eng, noch {rest} Minuten.")
            ereignisse.append(Ereignis("anschluss", text, f"anschluss:{a1.nach}:{'weg' if rest <= 0 else 'eng'}", True))

    zuege = neu.zuege
    if not ereignisse:
        erster = zuege[0]
        if dt.timedelta(0) < erster.ab - jetzt <= VOR_ABFAHRT:
            gleis = f" von Gleis {erster.gleis_ab}" if erster.gleis_ab else ""
            ereignisse.append(Ereignis("lebenszeichen", f"Noch im Plan: {erster.linie} um {erster.ab:%H:%M}{gleis}.",
                                       f"leben:{erster.trip_id}", False))
        for a1, a2 in zip(zuege, zuege[1:]):
            if dt.timedelta(0) < a1.an - jetzt <= VOR_UMSTIEG:
                gleis = f" von Gleis {a2.gleis_ab}" if a2.gleis_ab else ""
                ereignisse.append(Ereignis("lebenszeichen", f"Noch im Plan: Umstieg in {a1.nach}, {a2.linie} um {a2.ab:%H:%M}{gleis}.",
                                           f"leben:{a2.trip_id}", False))
    return [e for e in ereignisse if e.schluessel not in gemeldet]


def naechster_erreichbarer_halt(v: Verbindung, jetzt: dt.datetime) -> str:
    """Von hier aus sucht Plan B: Wer im Zug sitzt, kommt erst am naechsten Halt wieder raus."""
    zuege = v.zuege
    for a in zuege:
        if jetzt < a.ab:
            return a.von
        if jetzt < a.an:
            return next((h.name for h in a.halte if h.ab and h.ab > jetzt), a.nach)
    return zuege[-1].nach
