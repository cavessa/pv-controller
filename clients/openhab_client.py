"""Schmaler OpenHAB-REST-Client.

- get_pv_power_w: aktuelle PV-Leistung als float (Altbestand, ohne Token).
- Heizung: Items lesen (einzeln oder alle in einem Request), Kommandos senden,
  Verlauf aus der Persistence. Authentifizierung per Bearer-Token (API-Token aus
  openHAB → Profil → API-Tokens).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable, Optional

import requests

log = logging.getLogger(__name__)


class OpenHabError(Exception):
    pass


class OpenHabClient:
    def __init__(self, base_url: str, timeout: float = 5, token: str = ""):
        self.base_url = base_url.rstrip("/")
        # base_url darf mit oder ohne /rest angegeben werden
        self.rest_url = self.base_url if self.base_url.endswith("/rest") else self.base_url + "/rest"
        self.timeout = timeout
        self.session = requests.Session()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.session.headers["Accept"] = "application/json"

    def get_pv_power_w(self, item_name: str) -> Optional[float]:
        url = f"{self.base_url}/items/{item_name}/state"
        try:
            r = requests.get(url, timeout=self.timeout)
            r.raise_for_status()
            raw = r.text.strip()
        except requests.RequestException as e:
            log.error("OpenHAB read failed for %s: %s", item_name, e)
            return None

        if raw in ("", "NULL", "UNDEF"):
            log.error("OpenHAB item %s has no usable state (got %r)", item_name, raw)
            return None
        try:
            return float(raw.split(" ")[0])
        except ValueError:
            log.error("OpenHAB item %s state %r is not numeric", item_name, raw)
            return None

    # ── Heizung ─────────────────────────────────────────────────────────────

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        try:
            r = self.session.get(self.rest_url + path, params=params, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            raise OpenHabError(f"GET {path}: {e}") from e

    def get_item(self, name: str) -> dict:
        """Ein Item (name, type, state, label, …)."""
        return self._get(f"/items/{name}")

    def list_items(self) -> list[dict]:
        """Alle Items (ohne Gruppenmitglieder-Rekursion), schlanke Felder."""
        return self._get("/items", {"recursive": "false", "fields": "name,state,type,label"})

    def get_items(self, names: Iterable[str]) -> dict[str, dict]:
        """Mehrere Items in einem Request (alle Items holen, filtern).

        Fehlende Items fehlen im Ergebnis; ein Verbindungsfehler wirft OpenHabError.
        """
        wanted = set(n for n in names if n)
        if not wanted:
            return {}
        return {i["name"]: i for i in self.list_items() if i.get("name") in wanted}

    def send_command(self, name: str, value: Any) -> None:
        """POST /rest/items/{name} mit text/plain (wie ein Klick in der UI)."""
        try:
            r = self.session.post(
                f"{self.rest_url}/items/{name}",
                data=str(value).encode("utf-8"),
                headers={"Content-Type": "text/plain", "Accept": "*/*"},
                timeout=self.timeout,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            raise OpenHabError(f"command {name}={value}: {e}") from e

    def get_history(self, name: str, start: datetime, end: Optional[datetime] = None,
                    service_id: Optional[str] = None) -> list[tuple[int, float]]:
        """Persistierte Werte als [(epoch_ms, wert)]; nicht-numerische Werte werden übersprungen."""
        # Mit Zeitzone senden: ohne Offset ignoriert openHAB starttime und liefert den Default (1 Tag).
        params = {"starttime": start.astimezone().isoformat(timespec="seconds")}
        if end is not None:
            params["endtime"] = end.astimezone().isoformat(timespec="seconds")
        if service_id:
            params["serviceId"] = service_id
        data = self._get(f"/persistence/items/{name}", params)
        out: list[tuple[int, float]] = []
        for p in data.get("data") or []:
            try:
                out.append((int(p["time"]), float(str(p["state"]).split(" ")[0])))
            except (KeyError, TypeError, ValueError):
                continue
        return out
