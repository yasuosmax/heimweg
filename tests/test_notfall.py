import asyncio
import datetime as dt
import json
import pathlib
import types
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import httpx
from fastapi.testclient import TestClient

from heimweg import notfall, pin, quellen, suche

BERLIN = ZoneInfo("Europe/Berlin")
HASH = pin.pin_hash("482913")
ZIELE = "Darmstadt Hbf;Kassel Hbf"
HBF = quellen.Ort("Darmstadt Hbf", 49.8723, 8.6297, "x")
FF = quellen.Ort("Frankfurt Hbf", 50.1071, 8.6638, "y")


def ergebnis(*a, **k):
    jetzt = dt.datetime(2026, 11, 1, 4, 0, tzinfo=BERLIN)
    return suche.Ergebnis("quelle_weg", FF, HBF, None, None, None, [], suche.SATZ_KEINE_DATEN, "https://www.bahn.de/", jetzt)


def client(umgebung=None, adresse="testclient"):
    app = notfall.erstellen({"HEIM_PIN_HASH": HASH, "HEIM_ZIELE": ZIELE} if umgebung is None else umgebung)
    return TestClient(app, base_url="https://testserver", client=(adresse, 50000))


class Gesperrt(unittest.TestCase):
    def test_ohne_pin_hash_alles_gesperrt(self):
        c = client({"HEIM_ZIELE": ZIELE})
        for methode, pfad in (("get", "/"), ("post", "/pin"), ("post", "/suche")):
            self.assertEqual(getattr(c, methode)(pfad).status_code, 503, pfad)

    def test_leerer_pin_hash_gesperrt(self):
        self.assertEqual(client({"HEIM_PIN_HASH": "", "HEIM_ZIELE": ZIELE}).get("/").status_code, 503)

    def test_kaputter_pin_hash_gesperrt(self):
        for kaputt in ("482913", "scrypt:zz:zz", "scrypt::", "md5:aa:bb"):
            self.assertEqual(client({"HEIM_PIN_HASH": kaputt, "HEIM_ZIELE": ZIELE}).get("/").status_code, 503, kaputt)

    def test_ohne_ziele_gesperrt(self):
        self.assertEqual(client({"HEIM_PIN_HASH": HASH}).get("/").status_code, 503)


class Pin(unittest.TestCase):
    def test_richtige_pin_setzt_cookie(self):
        c = client()
        a = c.post("/pin", json={"pin": "482913"})
        self.assertEqual(a.status_code, 200)
        kopf = a.headers["set-cookie"].lower()
        for merkmal in ("httponly", "secure", "samesite=strict", "max-age=900"):
            self.assertIn(merkmal, kopf)
        self.assertEqual(a.json()["ziele"], ["Darmstadt Hbf", "Kassel Hbf"])

    def test_falsche_pin_403(self):
        a = client().post("/pin", json={"pin": "000000"})
        self.assertEqual(a.status_code, 403)
        self.assertNotIn("set-cookie", a.headers)
        self.assertNotIn("ziele", a.json())

    def test_leere_pin_eingabe_zaehlt_als_fehlversuch(self):
        c = client()
        for _ in range(5):
            self.assertEqual(c.post("/pin", json={"pin": ""}).status_code, 403)
        self.assertEqual(c.post("/pin", json={"pin": "482913"}).status_code, 429)

    def test_pin_kein_text_ist_fehlversuch(self):
        self.assertEqual(client().post("/pin", json={"pin": ["482913"]}).status_code, 403)

    def test_fuenf_fehlversuche_sperren_die_adresse(self):
        c = client()
        for _ in range(5):
            c.post("/pin", json={"pin": "000000"})
        a = c.post("/pin", json={"pin": "482913"})
        self.assertEqual(a.status_code, 429)
        self.assertIn("retry-after", a.headers)

    def test_dreissig_fehlversuche_sperren_alles(self):
        c = client(adresse="127.0.0.1")
        for i in range(30):
            c.post("/pin", json={"pin": "000000"}, headers={"X-Forwarded-For": f"10.0.{i // 200}.{i}"})
        a = c.post("/pin", json={"pin": "482913"}, headers={"X-Forwarded-For": "10.9.9.9"})
        self.assertEqual(a.status_code, 429)

    def test_fremde_kopfzeile_umgeht_die_bremse_nicht(self):
        c = client(adresse="127.0.0.1")
        for i in range(5):
            c.post("/pin", json={"pin": "000000"}, headers={"X-Forwarded-For": f"1.1.1.{i}, 8.8.8.8"})
        a = c.post("/pin", json={"pin": "482913"}, headers={"X-Forwarded-For": "1.1.1.77, 8.8.8.8"})
        self.assertEqual(a.status_code, 429)

    def test_direkte_verbindung_kann_keine_adresse_vortaeuschen(self):
        c = client()
        for i in range(5):
            c.post("/pin", json={"pin": "000000"}, headers={"X-Forwarded-For": f"1.1.1.{i}"})
        self.assertEqual(c.post("/pin", json={"pin": "482913"}).status_code, 429)

    def test_grosser_koerper_wird_abgelehnt(self):
        self.assertEqual(client().post("/pin", content=b"x" * 5000).status_code, 413)


class Suche(unittest.TestCase):
    def setUp(self):
        self.c = client()
        self.c.post("/pin", json={"pin": "482913"})
        p1 = mock.patch.object(quellen, "finde_ort", lambda t: [HBF] if "Darmstadt" in t else [FF])
        p2 = mock.patch.object(suche, "finde_weg", ergebnis)
        p1.start(), p2.start()
        self.addCleanup(mock.patch.stopall)

    def test_suche_ohne_cookie_403(self):
        self.assertEqual(client().post("/suche", json={"lat": 50.1, "lon": 8.6, "ziel": "Darmstadt Hbf"}).status_code, 403)

    def test_cookie_gefaelscht_403(self):
        a = client().post("/suche", json={"lat": 50.1, "lon": 8.6, "ziel": "Darmstadt Hbf"},
                          headers={"Cookie": "heim=9999999999.falsch"})
        self.assertEqual(a.status_code, 403)

    def test_cookie_abgelaufen_403(self):
        with mock.patch.object(notfall.time, "time", lambda: 9_999_999_999):
            a = self.c.post("/suche", json={"lat": 50.1, "lon": 8.6, "ziel": "Darmstadt Hbf"})
        self.assertEqual(a.status_code, 403)

    def test_suche_mit_standort(self):
        a = self.c.post("/suche", json={"lat": 50.1, "lon": 8.6, "ziel": "Darmstadt Hbf", "nur_regio": True})
        self.assertEqual(a.status_code, 200)
        self.assertEqual(a.json()["status"], "quelle_weg")
        self.assertIn("DB Navigator", a.json()["satz"])

    def test_suche_mit_bahnhof(self):
        self.assertEqual(self.c.post("/suche", json={"ort": "Frankfurt", "ziel": "Darmstadt Hbf"}).status_code, 200)

    def test_nur_gewaehlte_ziele_erlaubt(self):
        self.assertEqual(self.c.post("/suche", json={"ort": "Frankfurt", "ziel": "Berlin Hbf"}).status_code, 400)

    def test_koordinaten_ausser_bereich_400(self):
        for lat, lon in ((91, 8), (50, 181), ("x", 8), (None, None), (True, 8)):
            a = self.c.post("/suche", json={"lat": lat, "lon": lon, "ziel": "Darmstadt Hbf"})
            self.assertEqual(a.status_code, 400, (lat, lon))

    def test_text_ueber_80_zeichen_400(self):
        self.assertEqual(self.c.post("/suche", json={"ort": "x" * 81, "ziel": "Darmstadt Hbf"}).status_code, 400)

    def test_unbekannter_ort(self):
        with mock.patch.object(quellen, "finde_ort", lambda t: []):
            a = self.c.post("/suche", json={"ort": "Xyzzy", "ziel": "Darmstadt Hbf"})
        self.assertEqual(a.json()["status"], "ort_unbekannt")

    def test_quelle_weg_bei_der_ortssuche(self):
        def weg(t):
            raise quellen.QuelleWeg("x")

        with mock.patch.object(quellen, "finde_ort", weg):
            a = self.c.post("/suche", json={"ort": "Frankfurt", "ziel": "Darmstadt Hbf"})
        self.assertEqual(a.json()["status"], "quelle_weg")

    def test_zehn_suchen_dann_bremse(self):
        for _ in range(10):
            self.assertEqual(self.c.post("/suche", json={"ort": "Frankfurt", "ziel": "Darmstadt Hbf"}).status_code, 200)
        self.assertEqual(self.c.post("/suche", json={"ort": "Frankfurt", "ziel": "Darmstadt Hbf"}).status_code, 429)

    def test_nichts_im_log(self):
        with self.assertNoLogs("heimweg", level="INFO"):
            self.c.post("/suche", json={"lat": 50.123456, "lon": 8.654321, "ziel": "Darmstadt Hbf"})


class Rand(unittest.TestCase):
    def test_jeder_andere_pfad_404(self):
        c = client()
        for pfad in ("/api/frage", "/ws", "/api/health", "/mail", "/docs", "/openapi.json", "/redoc", "/kalender"):
            self.assertEqual(c.get(pfad).status_code, 404, pfad)
            self.assertEqual(c.post(pfad).status_code, 404, pfad)

    def test_kopfzeilen_csp_noindex_no_store(self):
        k = client().get("/").headers
        self.assertIn("default-src 'none'", k["content-security-policy"])
        self.assertIn("frame-ancestors 'none'", k["content-security-policy"])
        self.assertNotIn("unsafe", k["content-security-policy"])
        self.assertEqual(k["cache-control"], "no-store")
        self.assertEqual(k["x-robots-tag"], "noindex")
        self.assertEqual(k["referrer-policy"], "no-referrer")

    def test_seite_nennt_vor_der_pin_keine_ziele(self):
        c = client()
        for pfad in ("/", "/notfall.js", "/notfall.css"):
            text = c.get(pfad).text.lower()
            for wort in ("darmstadt", "kassel"):
                self.assertNotIn(wort, text, (pfad, wort))

    def test_seite_nennt_die_quellen(self):
        self.assertIn("transitous.org/sources", client().get("/").text)
        self.assertIn("openstreetmap", client().get("/").text.lower())


class PinWerkzeug(unittest.TestCase):
    def test_hash_prueft_richtig_und_falsch(self):
        self.assertTrue(pin.pin_passt("482913", HASH))
        self.assertFalse(pin.pin_passt("482914", HASH))

    def test_gleiche_pin_gibt_anderen_hash(self):
        self.assertNotEqual(pin.pin_hash("482913"), pin.pin_hash("482913"))

    def test_hash_enthaelt_kein_dollarzeichen(self):
        self.assertNotIn("$", HASH)

    def test_kurze_pin_wird_nicht_angenommen(self):
        with self.assertRaises(ValueError):
            pin.pin_hash("12345")


class Karte(unittest.TestCase):
    def test_karte_ist_json_mit_umstiegen(self):
        def uhr(h, m):
            return dt.datetime(2026, 11, 1, h, m, tzinfo=BERLIN)

        a = quellen.Abschnitt("nah", "RE70", "Frankfurt", "Mannheim", uhr(5, 15), uhr(5, 20), uhr(6, 20), uhr(6, 24),
                              "17", "7", True, False, "t1", geprueft="bestaetigt")
        b = quellen.Abschnitt("nah", "S1", "Mannheim", "Darmstadt", uhr(6, 28), uhr(6, 28), uhr(6, 45), uhr(6, 45),
                              "10", "3", True, False, "t2", geprueft="bestaetigt")
        e = suche.Ergebnis("ok", FF, HBF, quellen.Verbindung([a, b]), None, None, ["w"], "s", "l", uhr(5, 0))
        k = suche.karte(e, suche.Einstellungen())
        json.dumps(k)
        self.assertEqual(k["ziel"], "Darmstadt Hbf")
        self.assertEqual(k["stand"], "05:00")
        h = k["haupt"]
        self.assertEqual((h["ab"], h["an"], h["umstiege"]), ("05:20", "06:45", 1))
        self.assertEqual(h["abschnitte"][0]["verspaetung"], 5)
        self.assertEqual(h["wechsel"], [{"ort": "Mannheim", "minuten": 4, "knapp": True}])
        self.assertIsNone(k["plan_b"])


class Salve(unittest.TestCase):
    def test_parallele_fehlversuche_ueberholen_die_bremse_nicht(self):
        app = notfall.erstellen({"HEIM_PIN_HASH": HASH, "HEIM_ZIELE": ZIELE})

        async def salve():
            leitung = httpx.ASGITransport(app=app, client=("203.0.113.9", 1))
            async with httpx.AsyncClient(transport=leitung, base_url="https://testserver") as c:
                return await asyncio.gather(*[c.post("/pin", json={"pin": "000000"}) for _ in range(40)])

        codes = [a.status_code for a in asyncio.run(salve())]
        self.assertEqual((codes.count(403), codes.count(429)), (notfall.SPERRE_ADRESSE[0], 35))


class Betrieb(unittest.TestCase):
    def test_hinter_dem_socket_zaehlt_der_weitergeleitete_eintrag(self):
        anfrage = types.SimpleNamespace(client=None, headers={"x-forwarded-for": "198.51.100.1, 203.0.113.7"})
        self.assertEqual(notfall._adresse(anfrage), "203.0.113.7")

    def test_dienst_kommt_nicht_an_die_eigene_maschine(self):
        unit = (pathlib.Path(__file__).parent.parent / "deploy" / "heimweg-notfall.service").read_text(encoding="utf-8")
        self.assertIn("--uds /run/heimweg/notfall.sock", unit)
        self.assertNotIn("--port", unit)
        self.assertIn("IPAddressDeny=localhost", unit)
        self.assertIn("IPAddressAllow=127.0.0.53", unit)        # Namensaufloesung ueber systemd-resolved

    def test_ungenauer_standort_fragt_nach_dem_bahnhof(self):
        js = (pathlib.Path(notfall.__file__).parent / "notfall.js").read_text(encoding="utf-8")
        self.assertIn("p.coords.accuracy > 1000", js)
