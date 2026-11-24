"""Piattaforme TuttoGare di Studio Amica (Sicilia, 23/11/2026: IZS Sicilia, istanze SITAS della
Regione e delle ASP di Agrigento ed Enna).

La pagina pubblica /gare/ costruisce l'elenco con DataTables in modalita' serverSide: un GET a
    <host>/gare/table.php?draw=1&columns[0..10][...]&order[0][column]=2&order[0][dir]=desc
        &start=0&length=100&search[value]=&stato=<codice>&procedura=&tipologia=&criterio=&ente=<codice>
        &somma_urgenza=0&finanza_progetto=0&pari_opportunita=0
con l'intestazione X-Requested-With: XMLHttpRequest, senza cookie (ricostruito dall'utente dal
JavaScript della pagina; il server accetta 100 righe anche se il menu arriva a 50).

Risposta JSON {recordsTotal, recordsFiltered, data: [[flag, stato, id, cig, cup, oggetto_html,
valore, pubblicazione, scadenza, ente, bottone], ...]}; nell'oggetto HTML i riquadri \"Procedura:\",
\"Fase:\", \"Tipologia:\", il titolo in <strong> e il link dettaglio.php?codice=N (il codice del link
e' diverso dall'\"ID\" della colonna 2: si usa il codice).

Si chiedono solo le voci con stato 20 (\"Pubblicato\"): se il server ignorasse il filtro arriverebbe
tutto l'archivio (lezione 81), quindi ogni riga deve avere stato \"Pubblicato\", altrimenti errore.
Sulle istanze con molte stazioni (SITAS: 89) si leggono solo quelle indicate in `enti` (una richiesta
per ente). Niente CPV: filtro testuale. Il dettaglio (con il RUP) non si legge.

Esclusioni per procedura (`escludi_procedure`): affidamenti diretti / RDO (a invito, art. 50 c.1 b),
adesioni a convenzioni e accordi quadro, RDO su mercato elettronico e SDA. Restano aperte, negoziate,
ristrette, \"Affidamento con indagine di mercato\" (manifestazioni di interesse e consultazioni aperte
a tutti: verificato sulle schede 176 e 114 dell'IZS), consultazioni preliminari, richieste di
preventivo aperte.

Campione: state/campioni/<sigla>[_<ente>].json con i soli campi letti.
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

from .common import Filtro, Opportunita, log, parse_data, parse_importo, sessione_http

CAMPIONI = Path("state/campioni")
ERRORI: list[str] = []
ESCLUSI: set[str] = set()

ORDINABILI = (False, False, False, True, False, True, True, True, True, False, False)
RE_TAG = re.compile(r"<[^>]+>")
RE_CODICE = re.compile(r"dettaglio\.php\?codice=(\d+)")
RE_CIG = re.compile(r"\b[0-9A-Z]{10}\b")


def _testo(s: str) -> str:
    return " ".join(html.unescape(RE_TAG.sub(" ", s or "")).split())


def parametri(start: int, lunghezza: int, stato: str = "", ente: str = "") -> list[tuple[str, str]]:
    p = [("draw", "1")]
    for i, ordinabile in enumerate(ORDINABILI):
        p += [(f"columns[{i}][data]", str(i)), (f"columns[{i}][name]", ""), (f"columns[{i}][searchable]", "true"),
              (f"columns[{i}][orderable]", "true" if ordinabile else "false"),
              (f"columns[{i}][search][value]", ""), (f"columns[{i}][search][regex]", "false")]
    p += [("order[0][column]", "2"), ("order[0][dir]", "desc"), ("start", str(start)), ("length", str(lunghezza)),
          ("search[value]", ""), ("search[regex]", "false"), ("stato", stato), ("procedura", ""),
          ("tipologia", ""), ("criterio", ""), ("ente", ente), ("somma_urgenza", "0"),
          ("finanza_progetto", "0"), ("pari_opportunita", "0"), ("_", str(int(time.time() * 1000)))]
    return p


def leggi_riga(riga: list) -> dict:
    ogg = riga[5]
    badge = {k.strip().lower(): v.strip() for k, v in re.findall(r"badge[^>]*>\s*([^:<]+):\s*([^<]*)<", ogg)}
    titolo = re.search(r"<strong>(.*?)</strong>", ogg, re.DOTALL | re.IGNORECASE)
    codice = RE_CODICE.search(ogg) or RE_CODICE.search(riga[10] if len(riga) > 10 else "")
    return {
        "codice": codice.group(1) if codice else str(riga[2]),
        "stato": _testo(riga[1]),
        "cig": sorted(set(RE_CIG.findall(_testo(riga[3]).upper()))),
        "titolo": _testo(titolo.group(1) if titolo else ogg),
        "procedura": badge.get("procedura", ""),
        "fase": badge.get("fase", ""),
        "tipologia": badge.get("tipologia", ""),
        "importo": _testo(riga[6]),
        "pubblicazione": _testo(riga[7]),
        "scadenza": _testo(riga[8]),
        "ente": _testo(riga[9]),
    }


def _tipo(voce: dict) -> str:
    t = f"{voce['procedura']} {voce['fase']} {voce['titolo']}".lower()
    if "consultazion" in t:
        return "consultazione di mercato"
    if "indagine di mercato" in t or "manifestazion" in t or "avviso esplorativo" in t:
        return "avviso esplorativo"
    return "bando"


def _salva_campione(nome: str, contenuto: dict) -> None:
    CAMPIONI.mkdir(parents=True, exist_ok=True)
    (CAMPIONI / f"{nome}.json").write_text(json.dumps(contenuto, ensure_ascii=False, indent=1), encoding="utf-8")


def _leggi_ente(sito: dict, ente: str, sess, filtro: Filtro) -> list[Opportunita]:
    host = sito["host"].rstrip("/")
    nome = sito["sigla"] + (f"_{ente}" if ente else "")
    etichetta = sito["nome"] + (f" (ente {ente})" if ente else "")
    stato = str(sito.get("stato", "20"))
    lunghezza = int(sito.get("per_pagina", 100))
    r = sess.get(f"{host}/gare/table.php", params=parametri(0, lunghezza, stato, ente),
                 timeout=(float(sito.get("timeout_connessione", 20)), 60),
                 headers={"Accept": "application/json, text/javascript, */*; q=0.01",
                          "X-Requested-With": "XMLHttpRequest", "Referer": f"{host}/gare/"})
    r.raise_for_status()
    try:
        dati = r.json()
        righe = dati["data"]
    except (ValueError, KeyError, TypeError) as e:
        CAMPIONI.mkdir(parents=True, exist_ok=True)
        (CAMPIONI / f"{nome}_errore.html").write_bytes(r.content[:20000])
        raise RuntimeError(f"{etichetta}: risposta non JSON (vedi state/campioni/{nome}_errore.html)") from e
    voci = [leggi_riga(x) for x in righe]
    _salva_campione(nome, {"recordsTotal": dati.get("recordsTotal"), "recordsFiltered": dati.get("recordsFiltered"),
                           "voci": voci})
    if stato and any(v["stato"].lower() != "pubblicato" for v in voci):
        altri = sorted({v["stato"] for v in voci if v["stato"].lower() != "pubblicato"})
        raise RuntimeError(f"{etichetta}: il filtro sullo stato non risulta applicato (anche {', '.join(altri)}), "
                           "lista scartata")
    totale = dati.get("recordsFiltered")
    if isinstance(totale, int) and totale > len(voci):
        ERRORI.append(f"{etichetta}: lette {len(voci)} voci su {totale} (paginazione non gestita: dati parziali)")

    escl_proc = {p.lower() for p in sito.get("escludi_procedure") or []}
    escl_tit = re.compile("|".join(f"(?:{p})" for p in sito.get("escludi_titoli_regex") or []) or r"(?!)", re.IGNORECASE)
    rinomina = {k.lower(): v for k, v in (sito.get("enti_rinomina") or {}).items()}
    trovate, escluse = [], 0
    for v in voci:
        ident = f"{sito['sigla']}:{v['codice']}"
        if v["procedura"].lower() in escl_proc or escl_tit.search(v["titolo"]):
            escluse += 1
            ESCLUSI.add(ident)
            continue
        ente_voce = rinomina.get(v["ente"].lower(), v["ente"]) or sito.get("ente_se_vuoto", "")
        descr = f"Procedura: {v['procedura']}; Fase: {v['fase']}; Tipologia: {v['tipologia']}"
        motivo = filtro.valuta_testo(v["titolo"], ente_voce, descr)
        if not motivo:
            continue
        trovate.append(Opportunita(
            id=ident, fonti=[sito["nome_fonte"]], titolo=v["titolo"], ente=ente_voce,
            scadenza=parse_data(v["scadenza"]), pubblicazione=parse_data(v["pubblicazione"]),
            importo=parse_importo(v["importo"]) or None, cig=v["cig"], tipo=_tipo(v),
            link=f"{host}/gare/dettaglio.php?codice={v['codice']}", regione=sito.get("regione"),
            motivo_match=motivo, descrizione=descr))
    log.info("TuttoGare %s: %d voci pubblicate lette (su %s), %d escluse da regole esplicite, %d pertinenti",
             etichetta, len(voci), totale, escluse, len(trovate))
    return trovate


def _leggi_sito(sito: dict, filtro: Filtro) -> list[Opportunita]:
    sess = sessione_http()
    sess.mount("https://", HTTPAdapter(max_retries=Retry(
        total=int(sito.get("tentativi", 1)), backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET"}))))
    risultati, errori = [], []
    enti = [str(e) for e in sito.get("enti") or [""]]
    for ente in enti:
        try:
            risultati.extend(_leggi_ente(sito, ente, sess, filtro))
        except requests.ConnectionError:
            raise
        except Exception as e:
            log.exception("TuttoGare %s ente %s: errore", sito["nome"], ente)
            errori.append(str(e))
    if errori and len(errori) == len(enti):
        raise RuntimeError("; ".join(errori))
    ERRORI.extend(f"dati parziali: {e}" for e in errori)
    return risultati


def raccogli(cfg: dict, filtro: Filtro, sess=None) -> list[Opportunita]:
    risultati: list[Opportunita] = []
    errori: list[str] = []
    siti = [s for s in (cfg.get("tuttogare") or {}).get("siti", []) if s.get("abilitato", True)]
    for sito in siti:
        try:
            risultati.extend(_leggi_sito(sito, filtro))
        except requests.ConnectionError as e:
            log.warning("TuttoGare %s: connessione non riuscita (%s)", sito["nome"], type(e).__name__)
            errori.append(f"{sito['nome']}: connessione non riuscita ({type(e).__name__}), il sito non "
                          "risponde ai server di GitHub")
        except Exception as e:
            log.exception("TuttoGare %s: errore", sito["nome"])
            errori.append(str(e) if str(e).startswith(sito["nome"]) else f"{sito['nome']}: {e}")
    if errori and len(errori) == len(siti):
        raise RuntimeError("; ".join(errori))
    ERRORI.extend(errori)
    return risultati
