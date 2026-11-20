"""Elenco pubblico della Centrale Acquisti della Regione Lazio (piattaforma STELLA).

La pagina "Bandi di gara in scadenza" di centraleacquisti.regione.lazio.it costruisce l'elenco
nel browser (web component <bandi-gara>) chiamando un servizio REST, senza login ne' cookie:

    POST <url>?nPag=0&numeElemPerPag=50&bscaduto=0&tipologiaRisultati=BANDO_CONSULTAZIONE
    corpo: {}

La risposta e' JSON: TotalRecords e ListaBandiAvvisi, con IdDoc, TipoDoc, CIG, Titolo,
Oggetto (HTML, con il prefisso "<strong>Bando Revocato - </strong>" o "Bando Rettificato - "),
Ente, BaseAsta ("140.000,00"), DataScadenzaBando ("04/12/2026 10:00:00"), Contratto.
Pagine numerate da 0. Mancano CPV e data di pubblicazione. Nessun dato personale.

Si usa per le **consultazioni preliminari di mercato**, che il feed RSS di STELLA (letto da
portali_rss) non contiene. Le gare si leggono dal feed, che ha il CPV.

Opzioni per sito (sezione centrale_acquisti.siti di config.yaml):
  nome, nome_fonte, sigla, regione, url   - come le altre fonti.
  tipologia            - valore di tipologiaRisultati (BANDO_CONSULTAZIONE, BANDO_GARA, BANDO_SDA).
  tipo                 - tipo di avviso da mostrare quando il testo non ne indica uno piu' preciso.
  link                 - modello del link alla pagina di dettaglio, con {id} al posto di IdDoc.
  per_pagina, max_pagine - default 50 e 10.
  escludi_oggetti_regex  - voci da scartare in base all'oggetto (es. revocate, annullate):
                           finiscono in ESCLUSI e main.py le toglie anche dallo storico.
  enti_rinomina        - {"nome sul portale": "nome da usare"}, come per i feed RSS.
  timeout_connessione, tentativi - come per le altre fonti (default 20 e 2).
A ogni esecuzione la prima pagina viene salvata in state/campioni/<sigla>.json, prima di
interpretarla e anche se e' un errore.
"""
from __future__ import annotations

import html
import json
import re
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .common import Filtro, Opportunita, log, parse_data, parse_importo
from .portali_html import _tipo

CAMPIONI = Path("state/campioni")
ERRORI: list[str] = []
ESCLUSI: set[str] = set()   # id delle voci scartate da una regola esplicita (es. revocate)
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Accept-Language": "it-IT,it;q=0.9",
}
RE_TAG = re.compile(r"<[^>]+>")
RE_SPAZI = re.compile(r"\s+")
# Prefisso che il portale aggiunge all'oggetto: "<strong>Bando Rettificato - </strong> ..."
RE_PREFISSO = re.compile(r"^\s*bando\s+(rettificato|revocato|prorogato|sospeso|annullato)\s*-\s*",
                         re.IGNORECASE)


def _testo(valore) -> str:
    t = html.unescape(str(valore or ""))
    t = RE_TAG.sub(" ", t)
    return RE_SPAZI.sub(" ", t).strip()


def _sessione(sito: dict, sess) -> requests.Session:
    propria = requests.Session()
    propria.headers.update(sess.headers)
    propria.mount("https://", HTTPAdapter(max_retries=Retry(
        total=int(sito.get("tentativi", 2)), backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET", "POST"}))))
    return propria


def _rinomina(ente: str, sito: dict) -> str:
    mappa = {k.strip().lower(): v for k, v in (sito.get("enti_rinomina") or {}).items()}
    return mappa.get((ente or "").strip().lower(), ente)


def _scadenza(valore) -> str | None:
    """"04/12/2026 10:00:00" -> 2026-12-04. Le date fittizie (anno 5024 sulle voci revocate)
    valgono "non indicata"."""
    d = parse_data(str(valore or ""))
    if d and not ("1971" <= d[:4] <= "2100"):
        return None
    return d


def _converti(voce: dict, sito: dict, filtro: Filtro, escl: re.Pattern | None) -> Opportunita | None:
    id_doc = str(voce.get("IdDoc") or "").strip()
    if not id_doc:
        return None
    oid = f"{sito['sigla']}:{id_doc}"
    oggetto_grezzo = _testo(voce.get("Oggetto"))
    if escl and escl.search(oggetto_grezzo):
        ESCLUSI.add(oid)
        return None
    titolo = _testo(voce.get("Titolo")) or RE_PREFISSO.sub("", oggetto_grezzo)
    descrizione = RE_PREFISSO.sub("", oggetto_grezzo)
    if not titolo:
        return None
    ente = _rinomina(_testo(voce.get("Ente")), sito)

    motivo = filtro.valuta_testo(titolo, ente, descrizione)
    if not motivo:
        return None

    tipo = _tipo(f"{titolo} {descrizione}")
    if tipo == "bando" and sito.get("tipo"):
        tipo = sito["tipo"]
    cig = _testo(voce.get("CIG")).upper()
    return Opportunita(
        id=oid,
        fonti=[sito["nome_fonte"]],
        titolo=titolo,
        ente=ente,
        scadenza=_scadenza(voce.get("DataScadenzaBando")),
        importo=parse_importo(_testo(voce.get("BaseAsta"))) or None,   # 0,00 = non indicato
        cig=[cig] if re.fullmatch(r"[0-9A-Z]{10}", cig) else [],
        tipo=tipo,
        link=sito["link"].replace("{id}", id_doc) if sito.get("link") else None,
        regione=sito.get("regione"),
        motivo_match=motivo,
        descrizione=(descrizione[:600] if descrizione and descrizione != titolo else None),
    )


def _leggi_sito(sito: dict, filtro: Filtro, sess) -> list[Opportunita]:
    sess = _sessione(sito, sess)
    attesa = (float(sito.get("timeout_connessione", 20)), 60)
    per_pagina = int(sito.get("per_pagina", 50))
    max_pagine = int(sito.get("max_pagine", 10))
    lista_escl = sito.get("escludi_oggetti_regex") or []
    escl = re.compile("|".join(f"(?:{p})" for p in lista_escl), re.IGNORECASE) if lista_escl else None
    risultati: dict[str, Opportunita] = {}
    visti: set[str] = set()
    totale = None
    lette = 0

    for pagina in range(max_pagine):
        parametri = {"nPag": pagina, "numeElemPerPag": per_pagina, "bscaduto": 0}
        if sito.get("tipologia"):
            parametri["tipologiaRisultati"] = sito["tipologia"]
        try:
            r = sess.post(sito["url"], params=parametri, data="{}", headers=HEADERS, timeout=attesa)
            if pagina == 0:   # il campione si salva prima di interpretare la risposta
                CAMPIONI.mkdir(parents=True, exist_ok=True)
                (CAMPIONI / f"{sito['sigla']}.json").write_bytes(r.content[:60000])
            r.raise_for_status()
            dati = r.json()
        except ValueError as e:
            raise RuntimeError(f"{sito['nome']}: la risposta non e' JSON (vedi state/campioni)") from e
        except Exception as e:
            if pagina == 0:
                raise
            log.warning("%s: pagina %d non letta (%s), tengo le precedenti", sito["nome"], pagina, e)
            ERRORI.append(f"{sito['nome']}: lette {pagina} pagine, la {pagina + 1} ha dato errore "
                          f"(dati parziali, non persi)")
            break
        voci = dati.get("ListaBandiAvvisi") if isinstance(dati, dict) else None
        if not isinstance(voci, list):
            raise RuntimeError(f"{sito['nome']}: manca l'elenco ListaBandiAvvisi (vedi state/campioni)")
        totale = dati.get("TotalRecords", totale)
        nuove = [v for v in voci if str(v.get("IdDoc")) not in visti]
        if not nuove:   # pagina vuota o ripetuta: fine
            break
        visti |= {str(v.get("IdDoc")) for v in nuove}
        lette += len(nuove)
        for v in nuove:
            o = _converti(v, sito, filtro, escl)
            if o:
                risultati[o.id] = o
        if len(voci) < per_pagina or (isinstance(totale, int) and lette >= totale):
            break
        time.sleep(1)
    else:
        if isinstance(totale, int) and lette < totale:
            ERRORI.append(f"{sito['nome']}: lette {lette} voci su {totale} (max_pagine raggiunto, "
                          f"dati parziali)")

    log.info("%s: %d voci lette (su %s), %d pertinenti, %d escluse da regole esplicite",
             sito["nome"], lette, totale, len(risultati),
             sum(1 for k in ESCLUSI if k.startswith(sito["sigla"] + ":")))
    if lette == 0:
        # Non si blocca l'esecuzione (potrebbero davvero non esserci voci aperte), ma lo si
        # segnala: di solito e' un cambiamento del servizio.
        ERRORI.append(f"{sito['nome']}: 0 voci lette, verificare (vedi state/campioni)")
    return list(risultati.values())


def raccogli(cfg: dict, filtro: Filtro, sess) -> list[Opportunita]:
    risultati: list[Opportunita] = []
    errori: list[str] = []
    siti = [s for s in cfg["centrale_acquisti"].get("siti", []) if s.get("abilitato", True)]
    for sito in siti:
        try:
            risultati.extend(_leggi_sito(sito, filtro, sess))
        except Exception as e:
            log.exception("%s: errore", sito["nome"])
            errori.append(f"{sito['nome']}: {e}")
    if errori and len(errori) == len(siti):
        raise RuntimeError("; ".join(errori))
    ERRORI.extend(errori)
    return risultati
