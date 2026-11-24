"""SICOPAT, la sezione trasparenza dell'Osservatorio dei contratti pubblici della Provincia
autonoma di Trento (dal 24/11/2026): manifestazioni di interesse, indagini di mercato e \"altri
avvisi a scadenza\" (le consultazioni preliminari di mercato) di tutte le stazioni appaltanti
trentine, ASUIT compresa (la piu' attiva: 43 avvisi dal 01/06/2026).

La pagina pubblica sicopat2.provincia.tn.it/trasparenza-fe/ (app Angular) legge i dati da
un'API REST pubblica, senza login (BACKEND_URL in assets/env.js):

    GET <base>/getListaManifestazioneInteresse?dataScadenzaDa=YYYY-MM-DD
        -> gli avvisi con scadenza da quella data in poi (65 al 24/11/2026). Campi: codein,
           stazioneAppaltante, descri (oggetto), tipologia / tipologiaDesc, dataPrimaPubb, idAtto.
           Niente scadenza, niente dati personali. Il parametro stato=1 non filtra (4.132 voci
           dal 2020): si usa dataScadenzaDa con la data di oggi. Date solo nel formato YYYY-MM-DD.
    GET <base>/getDettaglioAttiGenerali?idAtto=<id>
        -> dataScadenza (aggiornata dopo le proroghe: 7139, proroga del 17/11, scadenza 30/11),
           dataCanc (annullamento), ma anche nome e codice fiscale del RUP, l'utente che ha
           pubblicato e gli allegati in base64.

Il dettaglio si chiede solo per le voci che passano il filtro testuale (descri + ente) e a ogni
esecuzione, perche' la scadenza puo' essere prorogata. Del dettaglio si leggono SOLO dataScadenza,
dataCanc e dataPrimaPubb, in memoria: RUP, utenti e allegati non si leggono e non si salvano.
Un dettaglio che non si legge rimanda la voce all'esecuzione successiva (senza scadenza resterebbe
120 giorni); le voci gia' nello storico restano. Un avviso annullato (dataCanc) va in ESCLUSI e
main.py lo toglie dallo storico.

La risposta e' JSON: una lista, oppure un oggetto con la lista in \"data\" (nei file della
ricognizione i dati stavano sotto \"data\"; si accettano entrambe le forme). Per il dettaglio vale
lo stesso (oggetto diretto o sotto \"data\").

Link per voce (verificato dall'utente in finestra anonima):
    https://sicopat2.provincia.tn.it/trasparenza-fe/#/page/dettaglio-manifestazione-interesse-page?idAtto=<id>

Opzioni (sezione sicopat.siti di config.yaml):
  nome, nome_fonte, sigla, regione - come le altre fonti.
  base                    - indirizzo dell'API pubblica (fino a /public).
  link                    - modello del link con {id}.
  enti_rinomina           - {\"nome su SICOPAT\": \"nome da usare\"} (es. ASUIT come su TED e Contracta).
  escludi_titoli_regex    - voci da scartare in base all'oggetto (finiscono in ESCLUSI).
  pausa_secondi           - attesa tra due dettagli (default 1).
  max_dettagli            - dettagli al massimo per esecuzione (default 30; le voci in piu' si
                            rimandano, con avviso).
  timeout_connessione, tentativi - come per le altre fonti (default 20 e 1).
Campione: state/campioni/<sigla>.json con i campi della lista (nessun dato personale) e, per le
voci di cui si e' letto il dettaglio, solo idAtto, scadenza e annullamento.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .common import Filtro, Opportunita, log, sessione_http

CAMPIONI = Path("state/campioni")
ERRORI: list[str] = []
ESCLUSI: set[str] = set()   # id delle voci annullate o scartate da una regola esplicita
ROMA = ZoneInfo("Europe/Rome")
CAMPI_LISTA = ("idAtto", "codein", "stazioneAppaltante", "tipologia", "tipologiaDesc", "dataPrimaPubb", "descri")


def data_roma(valore) -> str | None:
    """"2026-11-30T00:00:00.000Z" -> "2026-11-30" (data italiana)."""
    if not valore:
        return None
    s = str(valore).strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        return d.date().isoformat()
    return d.astimezone(ROMA).date().isoformat()


def _lista(dati) -> list:
    if isinstance(dati, list):
        return dati
    if isinstance(dati, dict) and isinstance(dati.get("data"), list):
        return dati["data"]
    raise RuntimeError("elenco non riconosciuto (attesa una lista, anche sotto \"data\")")


def riduci_dettaglio(dati) -> dict:
    """Dal dettaglio solo scadenza, annullamento e pubblicazione. Nessun dato personale."""
    d = dati.get("data") if isinstance(dati, dict) and isinstance(dati.get("data"), dict) else dati
    if not isinstance(d, dict) or "idAtto" not in d:
        raise RuntimeError("dettaglio non riconosciuto (manca idAtto)")
    return {"idAtto": d.get("idAtto"), "scadenza": data_roma(d.get("dataScadenza")),
            "annullato": bool(d.get("dataCanc")), "pubblicazione": data_roma(d.get("dataPrimaPubb"))}


def _tipo(voce: dict) -> str:
    t = f"{voce.get('tipologiaDesc') or ''} {voce.get('descri') or ''}".lower()
    if "consultazion" in t:
        return "consultazione di mercato"
    return "avviso esplorativo"


def _ente(voce: dict, sito: dict) -> str:
    nome = " ".join(str(voce.get("stazioneAppaltante") or "").split())
    mappa = {" ".join(k.split()).lower(): v for k, v in (sito.get("enti_rinomina") or {}).items()}
    return mappa.get(nome.lower(), nome)


def _sessione(sito: dict) -> requests.Session:
    sess = sessione_http()   # User-Agent del progetto
    sess.mount("https://", HTTPAdapter(max_retries=Retry(
        total=int(sito.get("tentativi", 1)), backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET"}))))
    return sess


def _salva(nome: str, contenuto) -> None:
    CAMPIONI.mkdir(parents=True, exist_ok=True)
    (CAMPIONI / f"{nome}.json").write_text(json.dumps(contenuto, ensure_ascii=False, indent=1),
                                           encoding="utf-8")


def _leggi_sito(sito: dict, filtro: Filtro, oggi: date | None = None) -> list[Opportunita]:
    oggi = oggi or date.today()
    sess = _sessione(sito)
    base = sito["base"].rstrip("/")
    attesa = (float(sito.get("timeout_connessione", 20)), 90)
    intest = {"Accept": "application/json, text/plain, */*"}
    r = sess.get(f"{base}/getListaManifestazioneInteresse", params={"dataScadenzaDa": oggi.isoformat()},
                 headers=intest, timeout=attesa)
    r.raise_for_status()
    try:
        voci = [v for v in _lista(r.json()) if isinstance(v, dict)]
    except ValueError as e:
        CAMPIONI.mkdir(parents=True, exist_ok=True)
        (CAMPIONI / f"{sito['sigla']}_errore.html").write_bytes(r.content[:20000])
        raise RuntimeError(f"risposta non JSON (vedi state/campioni/{sito['sigla']}_errore.html)") from e
    campione = {"letti": len(voci), "voci": [{k: v.get(k) for k in CAMPI_LISTA} for v in voci], "dettagli": []}

    escl = re.compile("|".join(f"(?:{p})" for p in sito.get("escludi_titoli_regex") or []) or r"(?!)",
                      re.IGNORECASE)
    max_dettagli = int(sito.get("max_dettagli", 30))
    pausa = float(sito.get("pausa_secondi", 1))
    trovate, escluse, rimandate, falliti, annullate, chiesti = [], 0, 0, 0, 0, 0
    for v in voci:
        id_atto = str(v.get("idAtto") or "").strip()
        titolo = " ".join(str(v.get("descri") or "").split())
        if not id_atto or not titolo:
            continue
        ident = f"{sito['sigla']}:{id_atto}"
        if escl.search(titolo):
            escluse += 1
            ESCLUSI.add(ident)
            continue
        ente = _ente(v, sito)
        descr = f"Tipologia SICOPAT: {' '.join(str(v.get('tipologiaDesc') or '').split())}"
        motivo = filtro.valuta_testo(titolo, ente, descr)
        if not motivo:
            continue
        if chiesti >= max_dettagli:
            rimandate += 1
            continue
        if chiesti:
            time.sleep(pausa)
        chiesti += 1
        try:
            rd = sess.get(f"{base}/getDettaglioAttiGenerali", params={"idAtto": id_atto},
                          headers=intest, timeout=attesa)
            rd.raise_for_status()
            det = riduci_dettaglio(rd.json())
        except Exception as e:   # la voce si rimanda: senza scadenza resterebbe 120 giorni
            falliti += 1
            rimandate += 1
            log.warning("SICOPAT %s: dettaglio %s non letto (%s), voce rimandata", sito["nome"], id_atto,
                        type(e).__name__)
            continue
        campione["dettagli"].append(det)
        if det["annullato"]:
            annullate += 1
            ESCLUSI.add(ident)
            continue
        trovate.append(Opportunita(
            id=ident, fonti=[sito["nome_fonte"]], titolo=titolo, ente=ente, scadenza=det["scadenza"],
            pubblicazione=data_roma(v.get("dataPrimaPubb")) or det["pubblicazione"], tipo=_tipo(v),
            link=sito["link"].format(id=id_atto), regione=sito.get("regione"),
            motivo_match=motivo, descrizione=descr))
    _salva(sito["sigla"], campione)
    if rimandate:
        ERRORI.append(f"{sito['nome']}: {falliti} dettagli non letti, {rimandate} voci rimandate "
                      f"all'esecuzione successiva")
    log.info("SICOPAT %s: %d avvisi aperti letti, %d esclusi da regole, %d dettagli letti, %d annullati, "
             "%d rimandati, %d pertinenti", sito["nome"], len(voci), escluse, chiesti - falliti, annullate,
             rimandate, len(trovate))
    return trovate


def raccogli(cfg: dict, filtro: Filtro, sess=None) -> list[Opportunita]:
    risultati: list[Opportunita] = []
    errori: list[str] = []
    siti = [s for s in (cfg.get("sicopat") or {}).get("siti", []) if s.get("abilitato", True)]
    for sito in siti:
        try:
            risultati.extend(_leggi_sito(sito, filtro))
        except requests.ConnectionError as e:
            log.warning("SICOPAT %s: connessione non riuscita (%s)", sito["nome"], type(e).__name__)
            errori.append(f"{sito['nome']}: connessione non riuscita ({type(e).__name__}), il sito non "
                          "risponde ai server di GitHub")
        except Exception as e:
            log.exception("SICOPAT %s: errore", sito["nome"])
            errori.append(f"{sito['nome']}: {e}")
    if errori and len(errori) == len(siti):
        raise RuntimeError("; ".join(errori))
    ERRORI.extend(errori)
    return risultati
