import datetime as dt
import json
import pathlib
import unittest
from zoneinfo import ZoneInfo

import httpx

from heimweg import quellen

PROBEN = pathlib.Path(__file__).parent / "proben"
BERLIN = ZoneInfo("Europe/Berlin")
FF = quellen.Ort("Frankfurt Hbf", 50.10712, 8.66381)
HD = quellen.Ort("Darmstadt Hbf", 49.40361, 8.67568, "de-DELFI_de:08221:1160")
JETZT = dt.datetime(2026, 11, 1, 4, 13, tzinfo=BERLIN)


def probe(name):
    return json.loads((PROBEN / name).read_text(encoding="utf-8"))


class Basis(unittest.TestCase):
    def setUp(self):
        self.anfragen = []
        self.antwort = {}
        quellen.zaehler_leeren()

        def handler(anfrage):
            self.anfragen.append(anfrage)
            if isinstance(self.antwort, Exception):
                raise self.antwort
            if isinstance(self.antwort, httpx.Response):
                return self.antwort
            return httpx.Response(200, json=self.antwort)

        quellen.client_setzen(httpx.Client(transport=httpx.MockTransport(handler),
                                           headers={"User-Agent": quellen.USER_AGENT}))
        self.addCleanup(quellen.client_setzen, None)


class Zeiten(unittest.TestCase):
    def test_zeiten_sind_berlin(self):
        z = quellen.zeit("2026-11-01T03:06:00Z")
        self.assertEqual((z.hour, z.minute), (4, 6))
        self.assertEqual(z.tzinfo.key, "Europe/Berlin")

    def test_sommerzeit_wechsel(self):
        self.assertEqual(quellen.zeit("2027-03-28T01:30:00Z").hour, 3)
        self.assertEqual(quellen.zeit("2027-03-28T00:30:00Z").hour, 1)


class Plan(Basis):
    def test_re70_aus_probe(self):
        self.antwort = probe("plan_ff_hd.json")
        verbindungen = quellen.plane(FF, HD, JETZT)
        re70 = next(a for v in verbindungen for a in v.abschnitte if a.linie == "RE70")
        self.assertEqual(re70.gleis_ab, "17")
        self.assertEqual(re70.art, "nah")
        self.assertEqual(re70.zugnummer, "4553")
        self.assertEqual(re70.ab.strftime("%H:%M"), "05:15")
        self.assertTrue(re70.live)

    def test_verbindung_rechnet_umstiege_und_dauer(self):
        self.antwort = probe("plan_ff_hd.json")
        v = next(v for v in quellen.plane(FF, HD, JETZT)
                 if any(a.linie == "RE70" for a in v.abschnitte))
        self.assertEqual(v.umstiege, 1)
        self.assertEqual(v.dauer_min, 96)
        self.assertFalse(v.fern)
        self.assertEqual([a.linie for a in v.zuege], ["RE70", "S1"])

    def test_fernzug_ist_fern(self):
        self.antwort = probe("plan_ff_hd.json")
        v = quellen.plane(FF, HD, JETZT)[0]
        self.assertTrue(v.fern)

    def test_regio_filter_schickt_keine_fernarten(self):
        self.antwort = {"itineraries": []}
        quellen.plane(FF, HD, JETZT, nur_regio=True)
        arten = self.anfragen[0].url.params["transitModes"].split(",")
        self.assertIn("REGIONAL_RAIL", arten)
        for fern in ("HIGHSPEED_RAIL", "LONG_DISTANCE", "NIGHT_RAIL", "COACH", "AIRPLANE"):
            self.assertNotIn(fern, arten)

    def test_alle_zuege_ohne_bus_fernlinie_und_flugzeug(self):
        self.antwort = {"itineraries": []}
        quellen.plane(FF, HD, JETZT)
        arten = self.anfragen[0].url.params["transitModes"].split(",")
        self.assertIn("HIGHSPEED_RAIL", arten)
        self.assertNotIn("COACH", arten)
        self.assertNotIn("AIRPLANE", arten)

    def test_koordinaten_gerundet(self):
        self.antwort = {"itineraries": []}
        quellen.plane(quellen.Ort("Hier", 49.40361, 8.67568), HD, JETZT)
        self.assertEqual(self.anfragen[0].url.params["fromPlace"], "49.404,8.676")

    def test_haltestelle_geht_mit_kennung_raus(self):
        self.antwort = {"itineraries": []}
        quellen.plane(FF, HD, JETZT)
        self.assertEqual(self.anfragen[0].url.params["toPlace"], HD.stop_id)

    def test_user_agent_hat_name_version_repo(self):
        self.antwort = {"itineraries": []}
        quellen.plane(FF, HD, JETZT)
        ua = self.anfragen[0].headers["user-agent"]
        self.assertIn("heimweg/", ua)
        self.assertIn("github.com/yasuosmax/heimweg", ua)

    def test_zeitueberschreitung_gibt_quellewege(self):
        self.antwort = httpx.ReadTimeout("zu langsam")
        with self.assertRaises(quellen.QuelleWeg):
            quellen.plane(FF, HD, JETZT)
        self.assertEqual(len(self.anfragen), 2, "ein zweiter Versuch")

    def test_serverfehler_gibt_quellewege(self):
        self.antwort = httpx.Response(503)
        with self.assertRaises(quellen.QuelleWeg):
            quellen.plane(FF, HD, JETZT)

    def test_kaputte_antwort_gibt_quellewege(self):
        self.antwort = httpx.Response(200, text="<html>Wartung</html>")
        with self.assertRaises(quellen.QuelleWeg):
            quellen.plane(FF, HD, JETZT)

    def test_antwort_ohne_verbindungsliste_gibt_quellewege(self):
        self.antwort = {"fehler": "x"}
        with self.assertRaises(quellen.QuelleWeg):
            quellen.plane(FF, HD, JETZT)

    def test_tagesdeckel_600_plan_aufrufe(self):
        self.antwort = {"itineraries": []}
        for _ in range(quellen.PLAN_DECKEL):
            quellen.plane(FF, HD, JETZT)
        self.assertEqual(quellen.PLAN_DECKEL, 600)
        with self.assertRaises(quellen.QuelleWeg):
            quellen.plane(FF, HD, JETZT)

    def test_nur_get(self):
        self.antwort = probe("plan_ff_hd.json")
        quellen.plane(FF, HD, JETZT)
        self.assertEqual({a.method for a in self.anfragen}, {"GET"})


class Zug(Basis):
    def test_zug_stand_liefert_den_zug_mit_halten(self):
        self.antwort = probe("trip_s6.json")
        a = quellen.zug_stand("irgendeine-kennung")
        self.assertEqual(a.linie, "S6")
        self.assertGreater(len(a.halte), 2)
        self.assertEqual(a.halte[-1].name, a.nach)
        self.assertEqual(self.anfragen[0].url.params["tripId"], "irgendeine-kennung")

    def test_zug_stand_ohne_zug_gibt_quellewege(self):
        self.antwort = {"legs": []}
        with self.assertRaises(quellen.QuelleWeg):
            quellen.zug_stand("weg")


class Orte(Basis):
    def test_haltestelle_steht_vor_gebaeuden(self):
        self.antwort = probe("geocode.json")
        orte = quellen.finde_ort("Darmstadt Hbf")
        self.assertEqual(orte[0].name, "Darmstadt Hbf")
        self.assertEqual(orte[0].stop_id, "de-DELFI_de:08221:1160")

    def test_nur_gebaeude_ist_trotzdem_ein_ort(self):
        self.antwort = [{"type": "PLACE", "name": "Schloss", "lat": 49.4, "lon": 8.7, "id": "way/1"}]
        orte = quellen.finde_ort("Schloss")
        self.assertEqual(orte[0].name, "Schloss")
        self.assertIsNone(orte[0].stop_id)

    def test_abstand_km(self):
        self.assertAlmostEqual(quellen.abstand_km(FF, HD), 78, delta=2)
