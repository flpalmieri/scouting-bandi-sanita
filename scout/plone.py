"""Siti Plone + Volto (tema design-italia di RedTurtle, es. E.O. Ospedali Galliera).

Le pagine "Bandi di gara" di questi siti costruiscono l'elenco nel browser chiamando la
REST API standard di Plone, senza login ne' cookie:

    POST <host>/++api++<percorso>/@querystring-search
    {"query": [path relativo + portal_type "Bando"], "sort_on": "scadenza_bando",
     "metadata_fields": [...], "b_size": .., "b_start": ..}

La risposta e' JSON: items_total e items, con @id (indirizzo della pagina, usato come
link), title, description, scadenza_bando (ISO in UTC), apertura_bando (1100-01-01 =
non valorizzata), effective, bando_state, tipologia_bando. Si leggono tutte le voci,
comprese le scadute (le pota main.py): "bando_state" vale "In corso" anche per voci
scadute, quindi conta solo scadenza_bando. Allegati e ufficio responsabile non si leggono.

Per un altro ente con lo stesso software basta un blocco in config.yaml (sezione
plone.siti) con host e percorso della sezione bandi.

Opzioni per sito, oltre a nome, nome_fonte, sigla, ente, regione, host, percorso:
  tipi_contenuto            - portal_type da cercare (default [Bando]).
  max_risultati             - voci lette al massimo, dalla scadenza piu' lontana (default 100).
  escludi_titoli_regex      - voci da scartare per titolo (es. aggiudicazioni, esiti).
  escludi_descrizioni_regex - voci da scartare per descrizione (es. procedure svolte su
                              Sintel, gia' lette dal modulo sintel).
  timeout_connessione       - secondi di attesa per la connessione (default 20).
  tentativi                 - nuovi tentativi in caso di errore 5xx (default 2).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .common import Filtro, Opportunita, log, parse_data
from .portali_html import RE_CIG, _tipo

CAMPIONI = Path("state/campioni")
ERRORI: list[str] = []
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Accept-Language": "it-IT,it;q=0.9",
}
CAMPI = ["Title", "Description", "scadenza_bando", "apertura_bando", "tipologia_bando",
         "bando_state", "effective"]
B_SIZE = 50

try:
    from zoneinfo import ZoneInfo
    _ROMA = ZoneInfo("Europe/Rome")
except Exception:          # senza dati dei fusi si usa la data UTC
    _ROMA = None


def _data(valore) -> str | None:
    """Data ISO di Plone ("2026-11-16T08:00:00+00:00") come data italiana. Gli anni prima
    del 1900 (Plone scrive 1100-01-01 per i campi vuoti) valgono "non indicata"."""
    if not valore or not isinstance(valore, str):
        return None
    try:
        d = datetime.fromisoformat(valore.replace("Z", "+00:00"))
    except ValueError:
        return parse_data(valore)
    if d.year < 1900:
        return None
    if d.tzinfo and _ROMA:
        d = d.astimezone(_ROMA)
    return d.date().isoformat()


def corpo_ricerca(sito: dict, b_start: int, b_size: int) -> dict:
    return {
        "metadata_fields": CAMPI,
        "b_start": b_start,
        "b_size": b_size,
        "query": [
            {"i": "path", "o": "plone.app.querystring.operation.string.relativePath",
             "v": sito["percorso"]},
            {"i": "portal_type", "o": "plone.app.querystring.operation.selection.any",
             "v": sito.get("tipi_contenuto", ["Bando"])},
        ],
        "sort_on": "scadenza_bando",
        "sort_order": "descending",
    }


def _regex(lista) -> re.Pattern | None:
    return re.compile("|".join(f"(?:{p})" for p in lista), re.IGNORECASE) if lista else None


def converti(voce: dict, sito: dict, filtro: Filtro) -> Opportunita | None:
    titolo = " ".join((voce.get("title") or voce.get("Title") or "").split())
    if not titolo:
        return None
    descrizione = " ".join((voce.get("description") or voce.get("Description") or "").split())
    escl = _regex(sito.get("escludi_titoli_regex"))
    if escl and escl.search(titolo):
        return None
    escl = _regex(sito.get("escludi_descrizioni_regex"))
    if escl and escl.search(descrizione):
        return None
    ente = sito.get("ente", "")
    motivo = filtro.valuta_testo(titolo, ente, descrizione)
    if not motivo:
        return None
    link = voce.get("@id") or None
    tipologia = voce.get("tipologia_bando") or ""
    testo = "; ".join(x for x in (descrizione, f"Tipologia: {tipologia}" if tipologia else "") if x)
    return Opportunita(
        id=f"{sito['sigla']}:{hashlib.sha1((link or titolo).encode('utf-8')).hexdigest()[:12]}",
        fonti=[sito.get("nome_fonte", sito["nome"])],
        titolo=titolo,
        ente=ente,
        scadenza=_data(voce.get("scadenza_bando")),
        pubblicazione=_data(voce.get("apertura_bando")) or _data(voce.get("effective")),
        cig=sorted({c.upper() for c in RE_CIG.findall(f"{titolo} {descrizione}") if c.strip("0")}),
        tipo=_tipo(f"{titolo} {descrizione}"),
        link=link,
        regione=sito.get("regione"),
        motivo_match=motivo,
        descrizione=testo[:600] or None,
    )


def _sessione(sito: dict, sess) -> requests.Session:
    propria = requests.Session()
    propria.headers.update(sess.headers)
    propria.mount("https://", HTTPAdapter(max_retries=Retry(
        total=int(sito.get("tentativi", 2)), backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"POST"}))))
    return propria


def _leggi_sito(sito: dict, filtro: Filtro, sess) -> list[Opportunita]:
    url = f"{sito['host'].rstrip('/')}/++api++{sito['percorso'].rstrip('/')}/@querystring-search"
    sess = _sessione(sito, sess)
    attesa = (float(sito.get("timeout_connessione", 20)), 60)
    massimo = int(sito.get("max_risultati", 100))
    voci: list[dict] = []
    totale = None
    while len(voci) < massimo:
        r = sess.post(url, headers=HEADERS, timeout=attesa,
                      data=json.dumps(corpo_ricerca(sito, len(voci), min(B_SIZE, massimo - len(voci)))))
        if not voci:   # il campione si salva prima di interpretare la risposta, anche se e' un errore
            CAMPIONI.mkdir(parents=True, exist_ok=True)
            (CAMPIONI / f"{sito['sigla']}.json").write_bytes(r.content[:60000])
        r.raise_for_status()
        try:
            dati = r.json()
        except ValueError as e:
            raise RuntimeError(f"{sito['nome']}: la risposta non e' JSON (vedi state/campioni)") from e
        pagina = dati.get("items") if isinstance(dati, dict) else None
        if not isinstance(pagina, list):
            raise RuntimeError(f"{sito['nome']}: manca l'elenco 'items' (vedi state/campioni)")
        totale = dati.get("items_total", len(pagina))
        voci.extend(pagina)
        if not pagina or len(voci) >= totale:
            break
    if not voci:
        # La ricerca comprende anche le voci scadute: zero voci vuol dire percorso o tipo sbagliati.
        raise RuntimeError(f"{sito['nome']}: nessuna voce trovata (percorso o tipo di contenuto cambiati?)")
    trovate = {o.id: o for o in (converti(v, sito, filtro) for v in voci) if o}
    log.info("Plone %s: %d voci lette (su %s), %d pertinenti", sito["nome"], len(voci), totale, len(trovate))
    return list(trovate.values())


def raccogli(cfg: dict, filtro: Filtro, sess) -> list[Opportunita]:
    risultati: list[Opportunita] = []
    errori: list[str] = []
    siti = [s for s in cfg["plone"].get("siti", []) if s.get("abilitato", True)]
    for sito in siti:
        try:
            risultati.extend(_leggi_sito(sito, filtro, sess))
        except Exception as e:
            log.exception("Plone %s: errore", sito["nome"])
            errori.append(f"{sito['nome']}: {e}")
    if errori and len(errori) == len(siti):
        raise RuntimeError("; ".join(errori))
    ERRORI.extend(errori)
    return risultati
