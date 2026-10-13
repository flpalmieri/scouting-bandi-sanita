"""Raccolta avvisi italiani da TED (Tenders Electronic Daily), API v3 di ricerca.

L'API di ricerca è pubblica e non richiede chiave. Si interrogano gli avvisi
italiani pubblicati negli ultimi N giorni e si filtrano localmente per CPV,
tipo di avviso ed ente, così da non dipendere da come TED gestisce i codici
CPV "figli" nella query.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

from .common import (Filtro, Opportunita, appiattisci, log, normalizza_cpv,
                     parse_data, parse_importo, pulisci_titolo, testo)

CAMPI_MINIMI = [
    "publication-number", "publication-date", "notice-title", "buyer-name",
    "classification-cpv", "deadline-receipt-tender-date-lot", "notice-type",
]
LIMITE_PAGINA = 250


def _richiesta(sess, url, query, campi, pagina):
    body = {
        "query": query,
        "fields": campi,
        "page": pagina,
        "limit": LIMITE_PAGINA,
        "scope": "ALL",
        "paginationMode": "PAGE_NUMBER",
    }
    return sess.post(url, json=body, timeout=90)


def _link(avviso: dict, numero: str) -> str:
    links = avviso.get("links") or {}
    html = links.get("html") if isinstance(links, dict) else None
    if isinstance(html, dict):
        for k in ("ITA", "ita", "ENG", "eng"):
            if html.get(k):
                return html[k]
        for v in html.values():
            if v:
                return v
    return f"https://ted.europa.eu/it/notice/-/detail/{numero}"


def _converti(avviso: dict, filtro: Filtro, tipi: list[str]) -> Opportunita | None:
    numero = testo(avviso.get("publication-number"))
    if not numero:
        return None

    tipo = testo(avviso.get("notice-type"))
    if tipi and tipo and not any(tipo.lower().startswith(p) for p in tipi):
        return None

    titolo = pulisci_titolo(testo(avviso.get("notice-title"), separatore=" | "))
    ente = testo(avviso.get("buyer-name"), separatore="; ")
    cpv = sorted({normalizza_cpv(c) for c in appiattisci(avviso.get("classification-cpv"))} - {""})

    motivo = filtro.valuta(titolo, ente, cpv)
    if not motivo:
        return None

    scadenze = sorted(filter(None, (parse_data(d) for d in
                                    appiattisci(avviso.get("deadline-receipt-tender-date-lot")))))
    valori = [parse_importo(v) for v in appiattisci(avviso.get("estimated-value-lot"))]
    valori = [v for v in valori if v is not None]

    return Opportunita(
        id=f"ted:{numero}",
        fonti=["TED"],
        titolo=titolo,
        ente=ente,
        scadenza=scadenze[0] if scadenze else None,
        pubblicazione=parse_data(testo(avviso.get("publication-date"))),
        importo=sum(valori) if valori else None,
        cpv=cpv,
        tipo=tipo or None,
        link=_link(avviso, numero),
        motivo_match=motivo,
    )


def raccogli(cfg: dict, filtro: Filtro, sess) -> list[Opportunita]:
    tcfg = cfg["ted"]
    da = (date.today() - timedelta(days=int(tcfg.get("finestra_giorni", 10)))).strftime("%Y%m%d")
    query = f"buyer-country=ITA AND publication-date>={da}"
    campi = list(tcfg.get("campi") or CAMPI_MINIMI)
    tipi = [t.lower() for t in tcfg.get("tipi_avviso_prefissi", [])]
    url = tcfg["url"]

    log.info("TED: query '%s'", query)
    risultati: dict[str, Opportunita] = {}
    letti, pagina, totale = 0, 1, None

    while pagina <= int(tcfg.get("max_pagine", 40)):
        r = _richiesta(sess, url, query, campi, pagina)
        if r.status_code == 400 and pagina == 1 and campi != CAMPI_MINIMI:
            log.warning("TED: richiesta rifiutata (%s). Riprovo con i campi minimi.", r.text[:500])
            campi = CAMPI_MINIMI
            continue
        r.raise_for_status()
        dati = r.json()
        avvisi = dati.get("notices") or []
        totale = dati.get("totalNoticeCount", totale)
        letti += len(avvisi)

        for a in avvisi:
            opp = _converti(a, filtro, tipi)
            if opp:
                risultati[opp.id] = opp

        log.info("TED: pagina %d, letti %d/%s, pertinenti finora %d",
                 pagina, letti, totale, len(risultati))
        if not avvisi or (totale is not None and letti >= totale):
            break
        pagina += 1
        time.sleep(1)

    return list(risultati.values())
