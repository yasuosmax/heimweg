"""PIN der Notfall-Seite: nur als scrypt-Hash, nie im Klartext.

Setzen: python -m heimweg.pin
Die ausgegebene Zeile gehoert in die Umgebung des Dienstes, nicht ins Repo.
"""
import getpass
import hashlib
import hmac
import os

MINDESTLAENGE = 6


def _rechnen(pin: str, salz: bytes) -> bytes:
    return hashlib.scrypt(pin.encode("utf-8"), salt=salz, n=2**14, r=8, p=1, dklen=32)


def pin_hash(pin: str) -> str:
    if len(pin) < MINDESTLAENGE:
        raise ValueError(f"Die PIN braucht mindestens {MINDESTLAENGE} Stellen.")
    salz = os.urandom(16)
    return f"scrypt:{salz.hex()}:{_rechnen(pin, salz).hex()}"


def _teile(wert: str) -> tuple[bytes, bytes] | None:
    teile = str(wert or "").split(":")
    if len(teile) != 3 or teile[0] != "scrypt":
        return None
    try:
        salz, ergebnis = bytes.fromhex(teile[1]), bytes.fromhex(teile[2])
    except ValueError:
        return None
    return (salz, ergebnis) if len(salz) >= 16 and len(ergebnis) == 32 else None


def hash_gueltig(wert: str) -> bool:
    return _teile(wert) is not None


def pin_passt(pin: str, wert: str) -> bool:
    teile = _teile(wert)
    return bool(teile) and hmac.compare_digest(_rechnen(pin, teile[0]), teile[1])


if __name__ == "__main__":
    erste = getpass.getpass("Neue PIN: ")
    if erste != getpass.getpass("Nochmal: "):
        raise SystemExit("Die beiden Eingaben sind verschieden.")
    print("HEIM_PIN_HASH=" + pin_hash(erste))
