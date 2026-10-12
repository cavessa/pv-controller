#!/usr/bin/env python3
"""Smoke-Test nach dem Deploy: Gab es nach START einen vollständigen Cron-Lauf ohne Fehler?

Aufruf: smoke_check.py <logdatei> "<YYYY-MM-DD HH:MM:SS>"
Exit 0 = ein Lauf (Start ... Ende) nach START ohne Fehlermarker, 1 = noch kein vollständiger
Lauf, 2 = Lauf mit Fehler. Fehlermarker: der Catch-all aus main.py („Unerwarteter Fehler")
und Python-Importfehler. Einzelne Warnungen mit Traceback (z. B. ein offline Gerät) zählen nicht.
"""
import re
import sys

ERR = re.compile(r"Unerwarteter Fehler|ImportError|ModuleNotFoundError|SyntaxError|NameError")
START_RX = re.compile(r"PV-Controller Start \((\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)")


def main(path: str, since: str) -> int:
    since_iso = since.replace(" ", "T")
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return 1
    run, state = [], None            # state: None | "inside"
    for line in lines[-4000:]:
        m = START_RX.search(line)
        if m and m.group(1) >= since_iso:
            run, state = [line], "inside"
            continue
        if state == "inside":
            run.append(line)
            if ERR.search(line):
                return 2
            if "PV-Controller Ende" in line:
                return 0
    if state == "inside" and any(ERR.search(l) for l in run):
        return 2
    # Lauf ohne „Start" im Log (Absturz vor dem Logging) oder noch laufend
    errs = [l for l in lines[-400:] if ERR.search(l) and l[:19].replace(" ", "T") >= since_iso]
    return 2 if errs else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
