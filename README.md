# heimweg

Findet mit Live-Daten die beste Bahnverbindung nach Hause, samt Plan B, und begleitet sie auf Wunsch. Kein Modell,
keine geratenen Zeiten: Fehlen Daten, sagt das Programm das.

- `heimweg.quellen`: Verbindungen über [Transitous](https://transitous.org), Gegenprobe über die DB-API Timetables
- `heimweg.suche`: beste Verbindung, Plan B, Warnungen für die Nacht und für knappe Umstiege
- `heimweg.wache`: vergleicht einen neuen Stand mit dem gemerkten und meldet, was sich geändert hat
- `heimweg.notfall`: kleine Web-Seite hinter einer PIN, nützlich auf einem fremden Handy

Widersprechen sich die Quellen, gilt der schlechtere Wert. Preise liefert keine der Quellen.

## Notfall-Seite betreiben

```bash
python -m heimweg.pin
```

Die ausgegebene Zeile zusammen mit den Werten aus `.env.example` in die Umgebung des Dienstes legen, Vorlage:
`deploy/heimweg-notfall.service`. Der Dienst hört nur auf den Unix-Socket `/run/heimweg/notfall.sock`, davor gehört ein Webserver mit HTTPS (Caddy: `reverse_proxy unix//run/heimweg/notfall.sock`). Ohne gültige PIN-Einstellung bleibt die Seite
gesperrt.

## Prüfen

```bash
python -m unittest discover -s tests -t .
```

Die Tests gehen nie ins Netz. Die DB-Proben sind nach der API-Beschreibung von Hand geschrieben.

## Datenquellen

Fahrplandaten von Transitous, siehe [Quellen](https://transitous.org/sources/). Kartendaten © OpenStreetMap-Mitwirkende.
Transitous ist ehrenamtlich und nicht für kommerzielle Nutzung gedacht, siehe [Nutzungsregeln](https://transitous.org/api/).
Dieses Programm kauft nichts und bucht nichts, es liest nur.

Lizenz: MIT
