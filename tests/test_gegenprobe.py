"""Die DB-Proben sind nach der API-Doku von Hand geschrieben: Ohne Schluessel gibt es keine echte
Antwort. Sobald der Schluessel da ist, prueft die Abnahme das Format gegen die echte Tafel."""
import datetime as dt
import pathlib
import unittest
from zoneinfo import ZoneInfo

import httpx

from heimweg import quellen

PROBEN = pathlib.Path(__file__).parent / "proben"
BERLIN = ZoneInfo("Europe/Berlin")
JETZT = dt.datetime(2026, 11, 1, 4, 13, tzinfo=BERLIN)


def uhr(h, m):
    return dt.datetime(2026, 11, 1, h, m, tzinfo=BERLIN)


def zug(nummer, linie, ab, gleis, an=None, art="nah", live=True, von="Frankfurt (Main) Hauptbahnhof",
        nach="Darmstadt, Hauptbahnhof"):
    an = an or ab + dt.timedelta(hours=1)
    return quellen.Abschnitt(art, linie, von, nach, ab, ab, an, an, gleis, None, live, False, "t",
                             zugnummer=nummer)


class Basis(unittest.TestCase):
    def setUp(self):
        self.aufrufe = []
        self.kaputt = None

        def handler(anfrage):
            pfad = anfrage.url.path
            self.aufrufe.append(pfad)
            if self.kaputt is not None:
                if isinstance(self.kaputt, Exception):
                    raise self.kaputt
                return self.kaputt
            for wort, datei in (("/station/", "db_station.xml"), ("/plan/", "db_plan.xml"), ("/fchg/", "db_fchg.xml")):
                if wort in pfad:
                    return httpx.Response(200, text=(PROBEN / datei).read_text(encoding="utf-8"))
            return httpx.Response(404)

        quellen.client_setzen(httpx.Client(transport=httpx.MockTransport(handler)))
        quellen.einrichten("id", "key")
        self.addCleanup(quellen.client_setzen, None)
        self.addCleanup(quellen.einrichten, "", "")


class Tafel(Basis):
    def test_tafel_liest_verspaetung_gleis_ausfall(self):
        tafel = {t.nummer: t for t in quellen.tafel("Frankfurt (Main) Hauptbahnhof", JETZT)}
        s8 = tafel["36601"]
        self.assertEqual(s8.ab_plan, uhr(4, 14))
        self.assertEqual(s8.ab, uhr(4, 27))
        self.assertEqual(s8.an, uhr(4, 25))
        self.assertEqual(s8.gleis, "103")
        self.assertTrue(tafel["15303"].ausfall)
        self.assertFalse(tafel["4553"].ausfall)
        self.assertEqual(tafel["4553"].linie, "RE70")

    def test_schluessel_gehen_als_kopfzeilen_raus(self):
        gesehen = []

        def handler(anfrage):
            gesehen.append(anfrage.headers)
            return httpx.Response(200, text=(PROBEN / "db_station.xml").read_text(encoding="utf-8"))

        quellen.client_setzen(httpx.Client(transport=httpx.MockTransport(handler)))
        quellen.tafel("Frankfurt (Main) Hauptbahnhof", JETZT)
        self.assertEqual(gesehen[0]["db-client-id"], "id")
        self.assertEqual(gesehen[0]["db-api-key"], "key")

    def test_bahnhofsnummer_wird_gemerkt(self):
        quellen.tafel("Frankfurt (Main) Hauptbahnhof", JETZT)
        quellen.tafel("Frankfurt (Main) Hauptbahnhof", uhr(5, 1))
        self.assertEqual(sum("/station/" in a for a in self.aufrufe), 1)

    def test_bahnhofsnamen_werden_uebersetzt(self):
        self.assertEqual(quellen.bahnhof_muster("Darmstadt, Hauptbahnhof"), "Darmstadt Hbf")
        self.assertEqual(quellen.bahnhof_muster("Frankfurt (Main) Hauptbahnhof"), "Frankfurt(Main)Hbf")
        self.assertEqual(quellen.bahnhof_muster("Frankfurt (Main) Südbahnhof"), "Frankfurt(Main)Süd")


class Gegenprobe(Basis):
    def probe(self, *abschnitte):
        return quellen.gegenprobe(quellen.Verbindung(list(abschnitte)), JETZT).abschnitte

    def test_einigkeit_heisst_bestaetigt(self):
        a = self.probe(zug("4553", "RE70", uhr(5, 15), "17"))[0]
        self.assertEqual(a.geprueft, "bestaetigt")
        self.assertEqual(a.ab, uhr(5, 15))

    def test_widerspruch_nimmt_schlechteren_wert(self):
        a = self.probe(zug("36601", "S8", uhr(4, 14), "104"))[0]
        self.assertEqual(a.ab, uhr(4, 27))
        self.assertEqual(a.gleis_ab, "103")
        self.assertEqual(a.geprueft, "abweichung")

    def test_bahn_sagt_ausfall_gilt_als_ausfall(self):
        a = self.probe(zug("15303", "RB68", uhr(5, 6), "12"))[0]
        self.assertTrue(a.ausfall)
        self.assertEqual(a.geprueft, "abweichung")

    def test_transitous_ausfall_bleibt_ausfall(self):
        z = zug("4553", "RE70", uhr(5, 15), "17")
        z.ausfall = True
        self.assertTrue(self.probe(z)[0].ausfall)

    def test_spaeter_transitous_wert_bleibt_wenn_er_schlechter_ist(self):
        z = zug("4553", "RE70", uhr(5, 15), "17")
        z.ab = uhr(5, 40)
        a = self.probe(z)[0]
        self.assertEqual(a.ab, uhr(5, 40))
        self.assertEqual(a.geprueft, "abweichung")

    def test_ein_grad_mehr_ist_keine_abweichung(self):
        z = zug("4553", "RE70", uhr(5, 15), "17")
        z.ab = uhr(5, 16)
        self.assertEqual(self.probe(z)[0].geprueft, "bestaetigt")

    def test_bahn_nicht_erreichbar_heisst_ohne_gegenprobe(self):
        self.kaputt = httpx.ConnectError("weg")
        a = self.probe(zug("4553", "RE70", uhr(5, 15), "17"))[0]
        self.assertEqual(a.geprueft, "ohne_gegenprobe")
        self.assertEqual(a.ab, uhr(5, 15))

    def test_kaputtes_xml_heisst_ohne_gegenprobe(self):
        self.kaputt = httpx.Response(200, text="<kaputt")
        self.assertEqual(self.probe(zug("4553", "RE70", uhr(5, 15), "17"))[0].geprueft, "ohne_gegenprobe")

    def test_ohne_schluessel_ohne_gegenprobe(self):
        quellen.einrichten("", "")
        self.assertEqual(self.probe(zug("4553", "RE70", uhr(5, 15), "17"))[0].geprueft, "ohne_gegenprobe")
        self.assertEqual(self.aufrufe, [])

    def test_bus_und_tram_bleiben_ungeprueft(self):
        a = self.probe(zug(None, "33", uhr(5, 15), None))[0]
        self.assertEqual(a.geprueft, "nur_fahrplan")

    def test_zug_nicht_auf_der_tafel_bleibt_ungeprueft(self):
        a = self.probe(zug("99999", "RB1", uhr(5, 15), None))[0]
        self.assertEqual(a.geprueft, "ohne_gegenprobe")

    def test_weit_im_voraus_bleibt_nach_fahrplan(self):
        a = self.probe(zug("4553", "RE70", JETZT + dt.timedelta(hours=19), "17"))[0]
        self.assertEqual(a.geprueft, "nur_fahrplan")
        self.assertEqual(self.aufrufe, [])

    def test_fussweg_wird_nicht_geprueft(self):
        f = quellen.Abschnitt("fuss", "", "A", "B", uhr(5, 0), uhr(5, 0), uhr(5, 5), uhr(5, 5), None, None,
                              False, False, None)
        self.assertEqual(self.probe(f)[0].geprueft, "nur_fahrplan")

    def test_ankunft_auf_der_tafel_verschiebt_die_ankunft(self):
        ab = uhr(3, 30)
        z = zug("36601", "S8", ab, "1", an=uhr(4, 12), von="Offenbach Hbf", nach="Frankfurt (Main) Hauptbahnhof")
        a = self.probe(z)[0]
        self.assertEqual(a.an, uhr(4, 25))
        self.assertEqual(a.geprueft, "abweichung")
