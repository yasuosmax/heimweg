'use strict';
const $ = (id) => document.getElementById(id);
const anmelden = $('anmelden'), sucheFeld = $('suche'), ergebnis = $('ergebnis');
let bitteWarten = false;

async function senden(pfad, daten) {
    const antwort = await fetch(pfad, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(daten),
    });
    let inhalt = {};
    try { inhalt = await antwort.json(); } catch { /* keine lesbare Antwort */ }
    return { status: antwort.status, inhalt };
}

function feld(tag, klasse, text) {
    const e = document.createElement(tag);
    if (klasse) e.className = klasse;
    if (text !== undefined) e.textContent = text;
    return e;
}

function zeigeZiele(ziele) {
    const gruppe = $('ziele');
    ziele.forEach((name, i) => {
        const zeile = feld('label', 'schalter');
        const wahl = document.createElement('input');
        Object.assign(wahl, { type: 'radio', name: 'ziel', value: name, checked: i === 0 });
        zeile.append(wahl, ' ' + name);
        gruppe.append(zeile);
    });
}

$('pin-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const hinweis = $('pin-hinweis');
    hinweis.textContent = '';
    let a;
    try {
        a = await senden('/pin', { pin: $('pin').value });
    } catch {
        hinweis.textContent = 'Keine Verbindung. Versuch es gleich nochmal.';
        return;
    }
    $('pin').value = '';
    if (a.status === 200) {
        zeigeZiele(a.inhalt.ziele);
        anmelden.hidden = true;
        sucheFeld.hidden = false;
    } else if (a.status === 429) {
        hinweis.textContent = 'Zu viele Versuche. In 15 Minuten geht es wieder.';
    } else {
        hinweis.textContent = 'PIN falsch.';
    }
});

function fahrtZeigen(titel, v, gross) {
    const block = feld('article', gross ? 'fahrt haupt' : 'fahrt');
    block.append(feld('h2', '', titel));
    if (gross) block.append(feld('p', 'uhrzeit', v.ab));
    const zuege = v.abschnitte.filter((a) => a.art !== 'fuss');
    if (v.los !== v.ab) block.append(feld('p', 'zeile', `Los zu Fuß um ${v.los}`));
    zuege.forEach((a, i) => {
        const gleis = a.gleis_ab ? `, Gleis ${a.gleis_ab}` : '';
        const spaeter = a.verspaetung > 0 ? ` (+${a.verspaetung})` : '';
        block.append(feld('p', 'zeile', `${a.linie} um ${a.ab}${spaeter}${gleis}, ${a.von}`));
        const wechsel = v.wechsel[i];
        if (wechsel) {
            block.append(feld('p', wechsel.knapp ? 'zeile knapp' : 'zeile dimm',
                `Umstieg in ${wechsel.ort}: ${wechsel.minuten} Minuten`));
        }
    });
    block.append(feld('p', 'zeile', `An ${v.an}`));
    return block;
}

function ergebnisZeigen(k) {
    ergebnis.replaceChildren();
    (k.warnungen || []).forEach((w) => ergebnis.append(feld('p', 'warnung', w)));
    if (k.haupt) {
        ergebnis.append(fahrtZeigen(`Nach ${k.ziel}`, k.haupt, true));
        ergebnis.append(k.plan_b ? fahrtZeigen('Plan B', k.plan_b, false) : feld('p', 'zeile dimm', 'Kein Plan B gefunden.'));
        if (k.fern) ergebnis.append(fahrtZeigen('Schneller mit Fernverkehr, kostet extra', k.fern, false));
    } else {
        ergebnis.append(feld('p', 'warnung', k.satz));
    }
    if (k.stand) ergebnis.append(feld('p', 'zeile dimm', `Stand ${k.stand} Uhr`));
    if (k.link) {
        const link = feld('a', 'knopf zweitrangig', 'Bei der Bahn prüfen');
        link.href = k.link;
        link.rel = 'noreferrer';
        ergebnis.append(link);
    }
    ergebnis.hidden = false;
    ergebnis.scrollIntoView({ block: 'start' });
}

async function suchen(teil) {
    if (bitteWarten) return;
    bitteWarten = true;
    const hinweis = $('such-hinweis');
    hinweis.textContent = '';
    ergebnis.replaceChildren(feld('p', 'arbeitet', 'Suche läuft'));
    ergebnis.hidden = false;
    const ziel = document.querySelector('input[name=ziel]:checked');
    try {
        const a = await senden('/suche', { ...teil, ziel: ziel && ziel.value, nur_regio: $('regio').checked });
        if (a.status === 200) {
            ergebnisZeigen(a.inhalt);
        } else {
            ergebnis.hidden = true;
            if (a.status === 403) {
                sucheFeld.hidden = true;
                anmelden.hidden = false;
                $('pin-hinweis').textContent = 'Die Sitzung ist abgelaufen. Bitte die PIN nochmal eingeben.';
            } else {
                hinweis.textContent = a.status === 429 ? 'Zu viele Suchen. Warte ein paar Minuten.' : 'Das kann ich nicht suchen.';
            }
        }
    } catch {
        ergebnis.hidden = true;
        hinweis.textContent = 'Keine Verbindung zum Server. Nimm den DB Navigator.';
    } finally {
        bitteWarten = false;
    }
}

$('standort').addEventListener('click', () => {
    if (!navigator.geolocation) {
        $('such-hinweis').textContent = 'Dieses Handy gibt keinen Standort. Gib einen Bahnhof ein.';
        return;
    }
    navigator.geolocation.getCurrentPosition(
        (p) => {
            if (p.coords.accuracy > 1000) {
                $('such-hinweis').textContent = 'Standort zu ungenau. Gib einen Bahnhof ein.';
                $('ort').focus();
                return;
            }
            suchen({ lat: p.coords.latitude, lon: p.coords.longitude });
        },
        () => {
            $('such-hinweis').textContent = 'Kein Standort. Gib einen Bahnhof ein.';
            $('ort').focus();
        },
        { enableHighAccuracy: true, timeout: 8000, maximumAge: 60000 });
});

$('ort-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const text = $('ort').value.trim();
    if (text) suchen({ ort: text });
});
