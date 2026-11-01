import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from heimweg import quellen, wache
from heimweg.suche import Einstellungen

BERLIN = ZoneInfo("Europe/Berlin")


def uhr(h, m):
    return dt.datetime(2026, 11, 1, h, m, tzinfo=BERLIN)


def zug(linie, ab, an, von="Frankfurt Hbf", nach="Mannheim Hbf", gleis_ab="17", gleis_an="7", ausfall=False, halte=()):
    return quellen.Abschnitt("nah", linie, von, nach, ab, ab, an, an, gleis_ab, gleis_an, True, ausfall,
                             f"{linie}-trip", halte=list(halte))


def umsteigen():
    return quellen.Verbindung([zug("RE70", uhr(5, 15), uhr(6, 20)),
                               zug("S1", uhr(6, 28), uhr(6, 45), von="Mannheim Hbf", nach="Darmstadt Hbf",
                                   gleis_ab="10", gleis_an="3")])


def geaendert(**felder_re70):
    v = umsteigen()
    for name, wert in felder_re70.items():
        setattr(v.abschnitte[0], name, wert)
    return v


class Melden(unittest.TestCase):
    def pruefe(self, neu, jetzt=uhr(5, 0), gemeldet=None, stumm_seit=None):
        return wache.pruefe(umsteigen(), neu, jetzt, gemeldet if gemeldet is not None else set(), stumm_seit)

    def arten(self, *a, **k):
        return [e.art for e in self.pruefe(*a, **k)]

    def test_alles_im_plan_bleibt_still(self):
        self.assertEqual(self.pruefe(umsteigen()), [])

    def test_verspaetung_ab_3_minuten(self):
        e = self.pruefe(geaendert(ab=uhr(5, 18)))
        self.assertEqual(e[0].art, "verspaetung")
        self.assertIn("RE70", e[0].text)
        self.assertIn("3 Minuten", e[0].text)

    def test_2_minuten_bleibt_still(self):
        self.assertEqual(self.arten(geaendert(ab=uhr(5, 17))), [])

    def test_gleiswechsel_meldet_alt_und_neu(self):
        e = self.pruefe(geaendert(gleis_ab="18"))[0]
        self.assertEqual(e.art, "gleis")
        self.assertIn("17", e.text)
        self.assertIn("18", e.text)

    def test_ausfall_braucht_plan_b(self):
        e = self.pruefe(geaendert(ausfall=True))[0]
        self.assertEqual(e.art, "ausfall")
        self.assertTrue(e.braucht_plan_b)

    def test_anschluss_unter_umstiegszeit_gefaehrdet(self):
        e = self.pruefe(geaendert(an=uhr(6, 26)))
        anschluss = next(x for x in e if x.art == "anschluss")
        self.assertTrue(anschluss.braucht_plan_b)
        self.assertIn("Mannheim", anschluss.text)
        self.assertIn("2 Minuten", anschluss.text)

    def test_anschluss_weg_sagt_das(self):
        e = self.pruefe(geaendert(an=uhr(6, 35)))
        self.assertIn("nicht", next(x for x in e if x.art == "anschluss").text)

    def test_jedes_ereignis_nur_einmal(self):
        gemeldet = set()
        erste = self.pruefe(geaendert(ausfall=True), gemeldet=gemeldet)
        gemeldet.update(x.schluessel for x in erste)
        self.assertEqual(self.pruefe(geaendert(ausfall=True), gemeldet=gemeldet), [])

    def test_groessere_verspaetung_meldet_erneut(self):
        gemeldet = {x.schluessel for x in self.pruefe(geaendert(ab=uhr(5, 19)))}
        self.assertEqual(self.pruefe(geaendert(ab=uhr(5, 19)), gemeldet=gemeldet), [])
        self.assertEqual(self.arten(geaendert(ab=uhr(5, 23)), gemeldet=gemeldet), ["verspaetung"])

    def test_vergangener_zug_meldet_keine_verspaetung_mehr(self):
        self.assertEqual(self.arten(geaendert(ab=uhr(5, 20)), jetzt=uhr(6, 21)), [])

    def test_lebenszeichen_vor_umstieg(self):
        e = self.pruefe(umsteigen(), jetzt=uhr(6, 16))
        self.assertEqual([x.art for x in e], ["lebenszeichen"])
        self.assertIn("Mannheim", e[0].text)
        self.assertIn("Gleis 10", e[0].text)

    def test_lebenszeichen_vor_abfahrt(self):
        e = self.pruefe(umsteigen(), jetzt=uhr(5, 7))
        self.assertEqual([x.art for x in e], ["lebenszeichen"])
        self.assertIn("Gleis 17", e[0].text)

    def test_lebenszeichen_ohne_live_daten_sagt_nur_fahrplan(self):
        v = umsteigen()
        for a in v.abschnitte:
            a.live = False
        e = self.pruefe(v, jetzt=uhr(5, 7))
        self.assertIn("Laut Fahrplan", e[0].text)
        self.assertNotIn("Noch im Plan", e[0].text)

    def test_fuenf_minuten_ohne_daten_meldet_quelle_stumm(self):
        self.assertEqual(self.pruefe(None, jetzt=uhr(5, 4), stumm_seit=uhr(5, 0)), [])
        e = self.pruefe(None, jetzt=uhr(5, 5), stumm_seit=uhr(5, 0))
        self.assertEqual([x.art for x in e], ["quelle_stumm"])
        self.assertIn("DB Navigator", e[0].text)

    def test_stumme_quelle_ist_kein_ausfall(self):
        self.assertNotIn("ausfall", self.arten(None, jetzt=uhr(5, 10), stumm_seit=uhr(5, 0)))

    def test_angekommen_nach_letzter_ankunft(self):
        self.assertEqual(self.arten(umsteigen(), jetzt=uhr(6, 46)), ["angekommen"])

    def test_spaete_ankunft_verschiebt_das_ende(self):
        v = umsteigen()
        v.abschnitte[1].an = uhr(7, 0)
        self.assertNotIn("angekommen", self.arten(v, jetzt=uhr(6, 46)))

    def test_harte_grenze_drei_stunden_nach_plan(self):
        self.assertEqual(self.arten(None, jetzt=uhr(9, 46), stumm_seit=uhr(9, 40)), ["angekommen"])


class NaechsterHalt(unittest.TestCase):
    def test_vor_abfahrt_ist_es_der_start(self):
        self.assertEqual(wache.naechster_erreichbarer_halt(umsteigen(), uhr(5, 0)), ("Frankfurt Hbf", uhr(5, 0)))

    def test_auf_der_fahrt_ist_es_der_naechste_halt(self):
        h = [quellen.Halt("Frankfurt Hbf", uhr(5, 15), uhr(5, 15), "17", False),
             quellen.Halt("Darmstadt Hbf", uhr(5, 40), uhr(5, 40), "2", False, uhr(5, 38), uhr(5, 39)),
             quellen.Halt("Mannheim Hbf", None, None, "7", False)]
        v = umsteigen()
        v.abschnitte[0].halte = h
        self.assertEqual(wache.naechster_erreichbarer_halt(v, uhr(5, 20)), ("Darmstadt Hbf", uhr(5, 42)))

    def test_ohne_haltedaten_ist_es_das_abschnittsende(self):
        self.assertEqual(wache.naechster_erreichbarer_halt(umsteigen(), uhr(5, 20)), ("Mannheim Hbf", uhr(6, 23)))

    def test_beim_umstieg_ist_es_der_umstiegsbahnhof(self):
        self.assertEqual(wache.naechster_erreichbarer_halt(umsteigen(), uhr(6, 22)), ("Mannheim Hbf", uhr(6, 22)))
