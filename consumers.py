"""Verbraucher aus openHAB (nur lesen): aktuelle Leistung der eingetragenen Geräte.

Pro Lauf drei REST-Abrufe (Item-Zustände, Thing-Status, Kanal-Links; die Links werden 10 min gemerkt).
Ein Wert gilt als **veraltet**, wenn das zugehörige Thing nicht ONLINE ist, das Item keinem Thing
zugeordnet ist oder der Zustand keine Leistung ist (NULL/UNDEF). Veraltete Werte werden angezeigt, aber
nie in Summe oder Rest eingerechnet. openHAB nicht erreichbar → None (nur die Card fehlt, die Regelung
ist nicht betroffen).

Summe/Rest (``evaluate``): ein Gerät zählt, wenn es aktuell ist, ``in_sum`` hat und kein vorgeschalteter
Zähler (``parent``-Kette) bereits zählt. Rest = Hausverbrauch − Heizstab/Wallbox/Whirlpool − Summe.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

from clients.openhab_client import OpenHabClient

log = logging.getLogger("pv-controller.consumers")

LINKS_TTL_S = 600.0
_links: dict[str, Any] = {"ts": -1e12, "url": "", "map": {}}

_NUM = re.compile(r"^\s*(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*(\S*)\s*$")
_UNIT = {"": 1.0, "W": 1.0, "kW": 1000.0, "mW": 0.001}


def parse_power(state: Any) -> Optional[float]:
    """openHAB-Zustand („27.11 W“, „1.2 kW“, „40“) → Watt; NULL/UNDEF/andere Einheit → None."""
    if state is None:
        return None
    m = _NUM.match(str(state))
    if not m or m.group(2) not in _UNIT:
        return None
    return float(m.group(1)) * _UNIT[m.group(2)]


def _item_things(client: OpenHabClient, url: str) -> dict[str, str]:
    now = time.monotonic()
    if _links["url"] != url or now - _links["ts"] > LINKS_TTL_S:
        _links.update(ts=now, url=url, map=client.get_item_things())
    return _links["map"]


def read(cfg, client: Optional[OpenHabClient] = None) -> Optional[list[dict[str, Any]]]:
    """Alle eingetragenen Geräte mit Leistung und Status; None, wenn openHAB nicht antwortet."""
    client = client or OpenHabClient(cfg.openhab_url, timeout=cfg.openhab_timeout_s, token=cfg.openhab_token)
    try:
        states = client.get_states()
        things = client.get_thing_statuses()
        thing_of = _item_things(client, cfg.openhab_url)
    except Exception as e:
        log.warning("Verbraucher: openHAB nicht lesbar (%s)", e)
        return None
    out = []
    for d in cfg.devices:
        w = parse_power(states.get(d.item))
        uid = thing_of.get(d.item)
        status = "NO_THING" if uid is None else things.get(uid, "MISSING")
        out.append({
            "item": d.item, "name": d.name, "parent": d.parent, "in_sum": d.in_sum,
            "power_w": None if w is None else round(w, 1),
            "stale": w is None or status != "ONLINE",
            "thing_status": status,
        })
    return out


def evaluate(devices: list[dict[str, Any]], house_w: Optional[float],
             loads_w: float = 0.0) -> dict[str, Any]:
    """Summe der gezählten Geräte und Rest; markiert je Gerät ``counted``/``covered``.

    ``covered``: ein vorgeschalteter Zähler zählt schon (Gerät steckt in dessen Wert).
    ``own_w`` bei Zählern mit Geräten dahinter: Zähler − aktuelle Geräte dahinter (nicht einzeln gemessen).
    """
    by = {d["item"]: d for d in devices}
    memo: dict[str, bool] = {}

    def counted(item: str) -> bool:
        if item not in memo:
            d = by[item]
            memo[item] = (not d["stale"] and d["in_sum"] and d["power_w"] is not None
                          and not covered(item))
        return memo[item]

    def covered(item: str) -> bool:
        p = by[item]["parent"]
        while p and p in by:
            if counted(p):
                return True
            p = by[p]["parent"]
        return False

    total = 0.0
    out = []
    for d in devices:
        c = counted(d["item"])
        e = dict(d, counted=c, covered=covered(d["item"]))
        kids = [k for k in devices if k["parent"] == d["item"]]
        if kids:
            e["children"] = [k["item"] for k in kids]
            if not d["stale"] and d["power_w"] is not None:
                e["own_w"] = round(d["power_w"] - sum(k["power_w"] for k in kids
                                                      if not k["stale"] and k["power_w"] is not None), 1)
        if c:
            total += d["power_w"]
        out.append(e)
    rest = None if house_w is None else round(house_w - loads_w - total)
    return {"devices": out, "sum_w": round(total), "rest_w": rest}


def leaves(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Geräte ohne eigene Geräte dahinter (für „Top-Verbraucher“ und Spitzen-Zuordnung)."""
    parents = {d["parent"] for d in devices if d["parent"]}
    return [d for d in devices if d["item"] not in parents]


def top(devices: list[dict[str, Any]], n: int = 5) -> list[dict[str, Any]]:
    """Die ``n`` stärksten aktuellen Einzelgeräte (> 0 W)."""
    fresh = [d for d in leaves(devices) if not d["stale"] and (d["power_w"] or 0) > 0]
    return sorted(fresh, key=lambda d: -d["power_w"])[:n]
