import datetime as dt
import json
import pathlib
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import httpx

from heimweg import quellen, suche

BERLIN = ZoneInfo("Europe/Berlin")
FF = quellen.Ort("Frankfurt Hbf", 50.1071, 8.6638)
HD = quellen.Ort("Darmstadt Hbf", 49.4036, 8.6757, "de-DELFI_de:08221:1160")
PROBE = json.loads((pathlib.Path(__file__).parent / "proben" / "plan_ff_hd.json").read_text(encoding="utf-8"))


def uhr(h, m, tag=1):
    return dt.datetime(2026, 11, tag, h, m, tzinfo=BERLIN)


def zug(linie, ab, an, von="Frankfurt Hbf", nach="Darmstadt Hbf", art="nah", gleis="12", live=True, ausfall=False,
        trip=None):
    return quellen.Abschnitt(art, linie, von, nach, ab, ab, an, an, gleis, "3", live, ausfall,
                             trip or f"{linie}-{ab:%H%M}", geprueft="bestaetigt")


def fuss(ab, an, nach="Frankfurt Hbf"):
    return quellen.Abschnitt("fuss", "", "START", nach, ab, ab, an, an, None, None, False, False, None)


def v(*abschnitte):
    return quellen.Verbindung(list(abschnitte))


class Basis(unittest.TestCase):
    def suche(self, regio, alle=None, jetzt=uhr(4, 0), **kw):
        def plane(von, nach, wann, nur_regio=False):
            if isinstance(regio, Exception):
                raise regio
            return list(regio) if nur_regio else list((alle if alle is not None else regio))

        with mock.patch.object(quellen, "plane", plane), \
                mock.patch.object(quellen, "gegenprobe", lambda vb, j: vb):
            return suche.finde_weg(FF, HD, jetzt, **kw)


class Echt(unittest.TestCase):
    def test_frankfurt_sonntag_drei_uhr(self):
        def handler(anfrage):
            daten = json.loads(json.dumps(PROBE))
            if "HIGHSPEED_RAIL" not in anfrage.url.params["transitModes"]:
                daten["itineraries"] = [i for i in daten["itineraries"]
                                        if not any(l["mode"] in ("LONG_DISTANCE", "COACH") for l in i["legs"])]
            return httpx.Response(200, json=daten)

        quellen.client_setzen(httpx.Client(transport=httpx.MockTransport(handler)))
        self.addCleanup(quellen.client_setzen, None)
        quellen.zaehler_leeren()
        e = suche.finde_weg(FF, HD, uhr(4, 13))
        self.assertEqual(e.status, "ok")
        self.assertEqual(e.haupt.zuege[0].linie, "RB68")
        self.assertEqual(e.plan_b.zuege[0].linie, "RE70")
        self.assertIn("Darmstadt Hbf", e.satz)
        self.assertIn("05:06", e.satz)
        self.assertIsNotNone(e.fern)
        self.assertIn("Fernzug", e.satz)


class Auswahl(Basis):
    def test_nur_regio_ohne_fern(self):
        regio = [v(zug("RE1", uhr(5, 0), uhr(7, 0)))]
        fern = [v(zug("ICE 1", uhr(5, 0), uhr(5, 45), art="fern"))]
        e = self.suche(regio, regio + fern, nur_regio=True)
        self.assertIsNone(e.fern)
        self.assertEqual(e.haupt.zuege[0].linie, "RE1")

    def test_fern_erst_ab_30_minuten(self):
        regio = [v(zug("RE1", uhr(5, 0), uhr(7, 0)))]
        knapp_drunter = [v(zug("ICE 1", uhr(5, 0), uhr(6, 31), art="fern"))]
        genau = [v(zug("ICE 1", uhr(5, 0), uhr(6, 30), art="fern"))]
        self.assertIsNone(self.suche(regio, regio + knapp_drunter).fern)
        self.assertIsNotNone(self.suche(regio, regio + genau).fern)

    def test_schwelle_ist_einstellbar(self):
        regio = [v(zug("RE1", uhr(5, 0), uhr(7, 0)))]
        fern = [v(zug("ICE 1", uhr(5, 0), uhr(6, 45), art="fern"))]
        e = self.suche(regio, regio + fern, einst=suche.Einstellungen(fern_schwelle_min=10))
        self.assertIsNotNone(e.fern)

    def test_regio_leer_fern_wird_haupt(self):
        fern = [v(zug("ICE 1", uhr(5, 0), uhr(5, 45), art="fern"))]
        e = self.suche([], fern)
        self.assertEqual(e.status, "ok")
        self.assertEqual(e.haupt.zuege[0].linie, "ICE 1")
        self.assertIn("Fernverkehr", e.satz)

    def test_ausgefallene_verbindung_wird_nie_empfohlen(self):
        kaputt = v(zug("RB68", uhr(5, 0), uhr(6, 0), ausfall=True))
        heil = v(zug("RE70", uhr(5, 30), uhr(6, 30)))
        e = self.suche([kaputt, heil], [kaputt, heil])
        self.assertEqual(e.haupt.zuege[0].linie, "RE70")

    def test_alles_ausgefallen_heisst_keine(self):
        kaputt = v(zug("RB68", uhr(5, 0), uhr(6, 0), ausfall=True))
        e = self.suche([kaputt], [kaputt])
        self.assertEqual(e.status, "keine")
        self.assertIsNone(e.haupt)

    def test_kurz_spaeter_mit_weniger_umstiegen_gewinnt(self):
        zwei = v(zug("RE1", uhr(5, 0), uhr(5, 30), nach="Mannheim"),
                 zug("S1", uhr(5, 40), uhr(6, 0), von="Mannheim"))
        direkt = v(zug("RE2", uhr(5, 5), uhr(6, 8)))
        e = self.suche([zwei, direkt], [zwei, direkt])
        self.assertEqual(e.haupt.zuege[0].linie, "RE2")

    def test_plan_b_nimmt_anderen_ersten_zug(self):
        a = v(zug("RB68", uhr(5, 0), uhr(6, 0), trip="eins"))
        gleicher = v(zug("RB68", uhr(5, 0), uhr(6, 5), trip="eins"))
        b = v(zug("RE70", uhr(5, 30), uhr(6, 30), trip="zwei"))
        e = self.suche([a, gleicher, b], [a, gleicher, b])
        self.assertEqual(e.plan_b.zuege[0].trip_id, "zwei")

    def test_plan_b_meidet_den_gleichen_umstieg(self):
        a = v(zug("RE1", uhr(5, 0), uhr(5, 30), nach="Mannheim"), zug("S1", uhr(5, 40), uhr(6, 0), von="Mannheim"))
        ueber_mannheim = v(zug("RE3", uhr(5, 10), uhr(5, 40), nach="Mannheim"),
                           zug("S2", uhr(5, 50), uhr(6, 10), von="Mannheim"))
        ueber_darmstadt = v(zug("RE4", uhr(5, 12), uhr(5, 40), nach="Darmstadt"),
                            zug("RB5", uhr(5, 50), uhr(6, 20), von="Darmstadt"))
        e = self.suche([a, ueber_mannheim, ueber_darmstadt], [a, ueber_mannheim, ueber_darmstadt])
        self.assertEqual(e.plan_b.zuege[0].linie, "RE4")

    def test_ohne_plan_b_steht_das_im_satz(self):
        e = self.suche([v(zug("RB68", uhr(5, 0), uhr(6, 0)))])
        self.assertIsNone(e.plan_b)
        self.assertIn("keinen Plan B", e.satz)

    def test_knapper_umstieg_4_min_ja_5_min_nein(self):
        knapp = v(zug("RE1", uhr(5, 0), uhr(5, 30), nach="Mannheim"), zug("S1", uhr(5, 34), uhr(6, 0), von="Mannheim"))
        locker = v(zug("RE1", uhr(5, 0), uhr(5, 30), nach="Mannheim"), zug("S1", uhr(5, 35), uhr(6, 0), von="Mannheim"))
        self.assertEqual(suche.knappe_umstiege(knapp, suche.Einstellungen()), [4])
        self.assertEqual(suche.knappe_umstiege(locker, suche.Einstellungen()), [])

    def test_knapper_umstieg_steht_in_der_warnung(self):
        knapp = v(zug("RE1", uhr(5, 0), uhr(5, 30), nach="Mannheim"), zug("S1", uhr(5, 34), uhr(6, 0), von="Mannheim"))
        e = self.suche([knapp])
        self.assertTrue(any("Mannheim" in w and "4 Minuten" in w for w in e.warnungen))
        self.assertTrue(e.satz.startswith("Knapper"))


class PlanBSatz(Basis):
    def test_nimmt_die_beste_verbindung_mit_anderem_zug(self):
        e = self.suche([v(zug("RB68", uhr(5, 0), uhr(6, 0), trip="eins")), v(zug("RE70", uhr(5, 30), uhr(6, 30), trip="zwei"))])
        self.assertEqual(suche.plan_b_satz(e, {"zwei"}), "Plan B: RB68 um 05:00 von Gleis 12, an 06:00.")

    def test_meidet_den_eigenen_zug(self):
        e = self.suche([v(zug("RB68", uhr(5, 0), uhr(6, 0), trip="eins")), v(zug("RE70", uhr(5, 30), uhr(6, 30), trip="zwei"))])
        self.assertEqual(suche.plan_b_satz(e, {"eins"}), "Plan B: RE70 um 05:30 von Gleis 12, an 06:30.")

    def test_ohne_quelle_sagt_das(self):
        e = self.suche(quellen.QuelleWeg("weg"))
        self.assertEqual(suche.plan_b_satz(e, set()), "Einen Plan B finde ich gerade nicht, schau im DB Navigator nach.")

    def test_ohne_andere_verbindung_sagt_das(self):
        e = self.suche([v(zug("RB68", uhr(5, 0), uhr(6, 0), trip="eins"))])
        self.assertIn("Einen Plan B finde ich gerade nicht", suche.plan_b_satz(e, {"eins"}))


class Nacht(Basis):
    def test_um_2350_ist_0010_noch_heute(self):
        e = self.suche([v(zug("RB68", uhr(0, 10, 2), uhr(1, 30, 2)))], jetzt=uhr(23, 50))
        self.assertEqual(e.status, "ok")

    def test_nachts_lange_luecke_warnt_und_nennt_erste_am_morgen(self):
        e = self.suche([v(zug("RB68", uhr(5, 30), uhr(6, 50)))], jetzt=uhr(1, 0))
        self.assertEqual(e.status, "heute_nichts")
        self.assertIn("Heute fährt nichts mehr", e.satz)
        self.assertIn("05:30", e.satz)

    def test_tagsueber_keine_nachtwarnung(self):
        e = self.suche([v(zug("RB68", uhr(14, 30), uhr(15, 50)))], jetzt=uhr(11, 0))
        self.assertEqual(e.status, "ok")

    def test_letzte_verbindung_wird_gesagt(self):
        letzte = v(zug("RB68", uhr(23, 40), uhr(0, 50, 2)))
        morgen = v(zug("RE70", uhr(5, 0, 2), uhr(6, 0, 2)))
        e = self.suche([letzte, morgen], jetzt=uhr(23, 0))
        self.assertTrue(any("letzte" in w for w in e.warnungen))
        self.assertIn("05:00", " ".join(e.warnungen))


class Unsicher(Basis):
    def test_quelle_weg_ohne_jede_uhrzeit_im_satz_und_mit_link(self):
        e = self.suche(quellen.QuelleWeg("weg"))
        self.assertEqual(e.status, "quelle_weg")
        self.assertNotRegex(e.satz, r"\d\d:\d\d")
        self.assertIn("DB Navigator", e.satz)
        self.assertTrue(e.link.startswith("https://www.bahn.de/"))

    def test_start_am_ziel_heisst_schon_da(self):
        with mock.patch.object(quellen, "plane", side_effect=AssertionError("keine Suche")):
            e = suche.finde_weg(HD, HD, uhr(4, 0))
        self.assertEqual(e.status, "schon_da")
        self.assertIn("Darmstadt Hbf", e.satz)

    def test_ohne_live_daten_sagt_nach_fahrplan(self):
        e = self.suche([v(zug("RB68", uhr(5, 0), uhr(6, 0), live=False))])
        self.assertIn("nach Fahrplan", e.satz)

    def test_abweichung_steht_im_satz(self):
        z = zug("RB68", uhr(5, 0), uhr(6, 0))
        z.geprueft = "abweichung"
        e = self.suche([v(z)])
        self.assertIn("widersprechen sich", e.satz)

    def test_ohne_gegenprobe_steht_im_satz(self):
        z = zug("RB68", uhr(5, 0), uhr(6, 0))
        z.geprueft = "ohne_gegenprobe"
        self.assertIn("ohne Gegenprobe", self.suche([v(z)]).satz)

    def test_fussweg_steht_im_satz(self):
        e = self.suche([v(fuss(uhr(4, 50), uhr(5, 0)), zug("RB68", uhr(5, 0), uhr(6, 0)))])
        self.assertIn("10 Minuten zu Fuß", e.satz)

    def test_erster_zug_zaehlt_fuer_die_nachtluecke_nicht_der_fussweg(self):
        e = self.suche([v(fuss(uhr(2, 50), uhr(3, 5)), zug("RB68", uhr(3, 5), uhr(4, 0)))], jetzt=uhr(1, 0))
        self.assertEqual(e.status, "heute_nichts")

    def test_zug_ist_kein_reiner_fussweg(self):
        e = self.suche([v(fuss(uhr(4, 50), uhr(5, 40)))])
        self.assertEqual(e.status, "keine")
