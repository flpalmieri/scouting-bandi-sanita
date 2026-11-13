"""Piattaforme di e-procurement DigitalPA ("<ente>.acquistitelematici.it").

La pagina pubblica carica l'elenco gare via JavaScript dal servizio JSON
    /ws/tender/fe?$limit=..&$offset=0&$sort[]=publish_date+DESC&query={"type":[{"$or":"<tipo>"}]}
con tipo "procedure-gara" o "manifestazioni-interesse". Ogni gara ha titolo,
date di pubblicazione e scadenza, stato, CPV (in cat_prevalente), CIG e link.

Per aggiungere un ente che usa la stessa piattaforma basta un blocco in config.yaml
(sezione acquistitelematici.siti) con il suo indirizzo.

Dati personali: i campi rup, contatti_per_info e simili non vengono letti.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .common import Filtro, Opportunita, log, parse_data

CAMPIONI = Path("state/campioni")
ERRORI: list[str] = []

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "it-IT,it;q=0.9",
}

RE_CPV = re.compile(r"\b(\d{8})(?:-\d)?\b")
RE_CIG_VERO = re.compile(r"^[0-9A-Z]{10}$")   # i "CIG" delle manifestazioni sono codici interni (M-00000102)


def url_elenco(host: str, tipo: str, limite: int) -> str:
    # Stesso formato della richiesta fatta dal browser (verificato il 13/11/2026).
    query = "%7B%22type%22:[%7B%22$or%22:%22" + tipo + "%22%7D]%7D"
    return (f"{host.rstrip('/')}/ws/tender/fe?$limit={limite}&$offset=0"
            f"&$sort[]=publish_date+DESC&query={query}")


def _importo(gara: dict) -> float | None:
    for campo in ("importo_complessivo_gara", "importo_gara"):
        v = gara.get(campo)
        if v in (None, ""):
            continue
        try:
            return float(str(v).replace(",", "."))
        except ValueError:
            continue
    return None


def _tipo(gara: dict, titolo: str) -> str:
    t = titolo.lower()
    rec = (gara.get("tipologia_rec") or gara.get("tipologia") or "").lower()
    if "consultazion" in t or "richiesta di informazioni" in rec or "richiesta_informazioni" in rec:
        return "consultazione di mercato"
    if "manifestazion" in rec or "manifestazion" in t or "avviso esplorativo" in t:
        return "avviso esplorativo"
    return "bando"


def converti(gara: dict, sito: dict, filtro: Filtro) -> Opportunita | None:
    if gara.get("is_public") is False:
        return None
    stato = (gara.get("stato") or "").lower()
    if stato in {s.lower() for s in sito.get("escludi_stati", ["annullata"])}:
        return None
    titolo = " ".join((gara.get("oggetto") or gara.get("title") or "").split())
    if not titolo:
        return None
    descrizione = " ".join((gara.get("description") or "").split())
    if descrizione.lower() == titolo.lower():
        descrizione = ""
    ente = (gara.get("nome_ente") or sito.get("ente") or "").replace("dell ", "dell'")

    cpv = sorted(set(RE_CPV.findall(f"{gara.get('cat_prevalente') or ''} {gara.get('cat_secondaria') or ''}")))
    motivo = (filtro.valuta(titolo, ente, cpv) if cpv else None) \
        or filtro.valuta_testo(titolo, ente, descrizione)
    if not motivo:
        return None

    cig = [c for c in {(gara.get("cig_number") or gara.get("cig") or "").strip().upper()} if RE_CIG_VERO.match(c)]
    url = gara.get("url_tender") or ""
    link = f"{sito['host'].rstrip('/')}{url}" if url.startswith("/") else (url or sito["host"])
    return Opportunita(
        id=f"{sito['sigla']}:{gara.get('id') or gara.get('uuid')}",
        fonti=[sito["nome_fonte"]],
        titolo=titolo,
        ente=ente,
        scadenza=parse_data((gara.get("expiring_date") or "")[:10]) or parse_data(gara.get("data_scadenza") or ""),
        pubblicazione=parse_data((gara.get("publish_date") or "")[:10]) or parse_data(gara.get("data_pubblicazione") or ""),
        importo=_importo(gara),
        cpv=cpv,
        cig=cig,
        tipo=_tipo(gara, titolo),
        link=link,
        regione=sito.get("regione"),
        motivo_match=motivo,
        descrizione=descrizione[:600] or None,
    )


def _salva_campione(sito: dict, tipo: str, contenuto: bytes) -> None:
    CAMPIONI.mkdir(parents=True, exist_ok=True)
    (CAMPIONI / f"{sito['sigla']}_{tipo}.json").write_bytes(contenuto[:60000])


def _leggi_tipo(sito: dict, tipo: str, filtro: Filtro, sess) -> list[Opportunita]:
    r = sess.get(url_elenco(sito["host"], tipo, int(sito.get("max_risultati", 50))), headers=HEADERS, timeout=60)
    r.raise_for_status()
    _salva_campione(sito, tipo, r.content)
    try:
        dati = json.loads(r.content.decode("utf-8"))
    except ValueError as e:
        raise RuntimeError(f"{tipo}: la risposta non è JSON (vedi state/campioni)") from e
    gare = dati.get("data") if isinstance(dati, dict) else None
    if not isinstance(gare, list):
        raise RuntimeError(f"{tipo}: manca l'elenco 'data' (vedi state/campioni)")
    trovate = [o for o in (converti(g, sito, filtro) for g in gare) if o]
    log.info("DigitalPA %s %s: %d gare lette (su %s), %d pertinenti", sito["nome"], tipo,
             len(gare), (dati.get("meta") or {}).get("nb_total", "?"), len(trovate))
    return trovate


def _leggi_sito(sito: dict, filtro: Filtro, sess) -> list[Opportunita]:
    """Legge ogni tipo (gare, manifestazioni) separatamente: se uno fallisce,
    gli altri non vanno persi."""
    risultati: dict[str, Opportunita] = {}
    tipi = sito.get("tipi", ["procedure-gara", "manifestazioni-interesse"])
    errori = []
    for tipo in tipi:
        try:
            for o in _leggi_tipo(sito, tipo, filtro, sess):
                risultati[o.id] = o
        except Exception as e:
            log.exception("DigitalPA %s %s: errore", sito["nome"], tipo)
            errori.append(str(e))
    if errori and len(errori) == len(tipi):
        raise RuntimeError("; ".join(errori))
    ERRORI.extend(f"{sito['nome']}: {e}" for e in errori)
    return list(risultati.values())


def raccogli(cfg: dict, filtro: Filtro, sess) -> list[Opportunita]:
    risultati: list[Opportunita] = []
    errori: list[str] = []
    siti = [s for s in cfg["acquistitelematici"].get("siti", []) if s.get("abilitato", True)]
    for sito in siti:
        try:
            risultati.extend(_leggi_sito(sito, filtro, sess))
        except Exception as e:
            log.exception("DigitalPA %s: errore", sito["nome"])
            errori.append(f"{sito['nome']}: {e}")
    if errori and len(errori) == len(siti):
        raise RuntimeError("; ".join(errori))
    ERRORI.extend(errori)
    return risultati
