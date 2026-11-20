"""Elenco pubblico della Centrale Acquisti della Regione Lazio (piattaforma STELLA).

La pagina "Bandi di gara in scadenza" di centraleacquisti.regione.lazio.it costruisce l'elenco
nel browser (web component <bandi-gara>) chiamando un servizio REST, senza login ne' cookie:

    POST <url>?nPag=0&numeElemPerPag=50&bscaduto=0&tipologiaRisultati=BANDO_CONSULTAZIONE
    corpo: {}

La risposta e' JSON: TotalRecords e ListaBandiAvvisi, con IdDoc, TipoDoc, CIG, Titolo,
Oggetto (HTML, con il prefisso "<strong>Bando Revocato - </strong>" o "Bando Rettificato - "),
Ente, BaseAsta ("140.000,00"), DataScadenzaBando ("04/12/2026 10:00:00"), Contratto.
Pagine numerate da 0. Mancano CPV e data di pubblicazione. Nessun dato personale.

Si usa per le gare (tipologia BANDO_GARA) e per le consultazioni preliminari di mercato
(BANDO_CONSULTAZIONE), che il feed RSS di STELLA non contiene. Per le gare il servizio e'
preferito al feed RSS perche' la scadenza e' quella aggiornata dopo rettifiche e proroghe:
nel feed DtScadenzaBandoTecnical resta la data originale (verificato il 20/11/2026 sui
dettagli: RIS-PACS ASL Roma 6 feed 03/12, dettaglio ed elenco 18/12; ISDN-VoIP feed 23/11,
dettaglio ed elenco 25/11), e il filtro del feed sulle voci aperte perde le gare prorogate.

Dettaglio (opzione "dettaglio", per le gare): il CPV, il tipo di procedura e la data di
pubblicazione stanno solo nella scheda di dettaglio,
    POST <url_dettaglio>  {"idDoc": "<IdDoc>", "typeDoc": "BANDO_GARA_PORTALE", "ml_xslt": "", "params": ""}
che contiene anche RUP e compilatore con codice fiscale, email e telefono: si leggono SOLO
CODICE_CPV e CODICE_CPV_SECONDARIO, PROCEDURAGARA (Aperta, Negoziata, Affidamento Diretto...),
TIPOBANDOGARA (Bando/Invito) e DATAINVIO, e solo questi finiscono nella cache
state/<sigla>_dettagli.json (che fa anche da campione). Il dettaglio si chiede solo per le voci
che il filtro potrebbe tenere (ente sanitario o aggregatore, oggetto sanitario o IT) e una volta
sola per voce: le voci gia' in cache non si richiedono. Senza dettaglio (errore) la voce passa
dal filtro testuale e la richiesta si ripete all'esecuzione successiva.

Opzioni per sito (sezione centrale_acquisti.siti di config.yaml):
  nome, nome_fonte, sigla, regione, url   - come le altre fonti.
  tipologia            - valore di tipologiaRisultati (BANDO_CONSULTAZIONE, BANDO_GARA, BANDO_SDA).
  tipo                 - tipo di avviso da mostrare quando il testo non ne indica uno piu' preciso.
  link                 - modello del link alla pagina di dettaglio, con {id} al posto di IdDoc.
  per_pagina, max_pagine - default 50 e 10.
  escludi_oggetti_regex  - voci da scartare in base al titolo o all'oggetto (es. revocate,
                           affidamenti diretti): finiscono in ESCLUSI e main.py le toglie anche
                           dallo storico.
  dettaglio            - true per leggere la scheda di dettaglio (vedi sopra), con url_dettaglio,
                         tipo_documento (default BANDO_GARA_PORTALE), pausa_secondi (default 0.5)
                         e max_dettagli (richieste al massimo per esecuzione, default 200).
  escludi_procedure    - valori di PROCEDURAGARA da scartare (es. Affidamento Diretto): anche
                         queste voci finiscono in ESCLUSI.
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
from datetime import date
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .common import Filtro, Opportunita, log, parse_data, parse_importo
from .portali_html import _tipo

CAMPIONI = Path("state/campioni")
STATO = Path("state")
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


def _valore(sezione: dict, campo: str) -> list[str]:
    """Valori di un campo della scheda: {"CAMPO": {"CAMPO_VALUE": {"content", "codext"}}} oppure
    {"CAMPO": {"content": ...}}; il _VALUE puo' essere una lista."""
    nodo = (sezione or {}).get(campo) or {}
    if not isinstance(nodo, dict):
        return []
    v = nodo.get(f"{campo}_VALUE")
    if v is None:
        c = nodo.get("content")
        return [str(c)] if c not in (None, "") else []
    elementi = v if isinstance(v, list) else [v]
    out = []
    for e in elementi:
        if isinstance(e, dict) and (e.get("codext") or e.get("content")):
            out.append(str(e.get("codext") or e.get("content")))
    return out


def _contenuto(sezione: dict, campo: str) -> str:
    nodo = (sezione or {}).get(campo) or {}
    v = nodo.get(f"{campo}_VALUE") if isinstance(nodo, dict) else None
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        return str(v.get("content") or "")
    return str(nodo.get("content") or "") if isinstance(nodo, dict) else ""


def _riduci_dettaglio(dati) -> dict | None:
    """Dalla scheda tiene solo CPV, procedura, tipo di bando e data di invio. Nessun dato
    personale (RUP, compilatore, riferimenti) viene letto o salvato."""
    try:
        resp = dati["xmlresp"]
        if str(resp.get("returncode", "0")) != "0":
            return None
        doc = next(iter(resp["output"].values()))
        sezioni = doc["SECTIONS"]
    except (KeyError, TypeError, AttributeError, StopIteration):
        return None
    pcp = sezioni.get("INTEROP_PCP") or {}
    testata = sezioni.get("TESTATA") or {}
    documento = sezioni.get("DOCUMENT") or {}
    cpv = []
    for campo in ("CODICE_CPV", "CODICE_CPV_SECONDARIO"):
        for v in _valore(pcp, campo):
            m = re.match(r"(\d{8})", v)
            if m and m.group(1) not in cpv:
                cpv.append(m.group(1))
    invio = _valore(documento, "DATAINVIO")
    return {
        "cpv": cpv,
        "procedura": _contenuto(testata, "PROCEDURAGARA"),
        "tipo_bando": _contenuto(testata, "TIPOBANDOGARA"),
        "pubblicazione": parse_data(invio[0]) if invio else None,
    }


def _candidata(titolo: str, oggetto: str, ente: str, filtro: Filtro) -> bool:
    """La voce potrebbe passare il filtro con un CPV IT (ente sanitario o aggregatore, oggetto
    sanitario) o ha parole IT: solo per queste si chiede il dettaglio."""
    testo = f"{titolo} {oggetto}"
    return bool(filtro.ente_sanitario(ente)
                or (filtro.re_aggr and filtro.re_aggr.search(ente or ""))
                or (filtro.re_sanita and filtro.re_sanita.search(testo))
                or filtro.parola_it(titolo, oggetto))


def _converti(voce: dict, sito: dict, filtro: Filtro, escl: re.Pattern | None,
              dettaglio: dict | None = None) -> Opportunita | None:
    id_doc = str(voce.get("IdDoc") or "").strip()
    if not id_doc:
        return None
    oid = f"{sito['sigla']}:{id_doc}"
    oggetto_grezzo = _testo(voce.get("Oggetto"))
    titolo_grezzo = _testo(voce.get("Titolo"))
    if escl and (escl.search(oggetto_grezzo) or escl.search(titolo_grezzo)):
        ESCLUSI.add(oid)
        return None
    dettaglio = dettaglio or {}
    procedura = dettaglio.get("procedura") or ""
    if procedura and procedura.strip().lower() in {str(p).strip().lower()
                                                    for p in sito.get("escludi_procedure") or []}:
        ESCLUSI.add(oid)
        return None
    titolo = titolo_grezzo or RE_PREFISSO.sub("", oggetto_grezzo)
    descrizione = RE_PREFISSO.sub("", oggetto_grezzo)
    if not titolo:
        return None
    ente = _rinomina(_testo(voce.get("Ente")), sito)
    cpv = list(dettaglio.get("cpv") or [])

    # Con il CPV si usa il filtro preciso; il filtro testuale recupera i casi con CPV non
    # informatico ma oggetto chiaramente IT (es. RIS-PACS con CPV di diagnostica radiologica).
    motivo = (filtro.valuta(titolo, ente, cpv) if cpv else None) \
        or filtro.valuta_testo(titolo, ente, descrizione)
    if not motivo:
        return None

    tipo = _tipo(f"{titolo} {descrizione}")
    if tipo == "bando" and sito.get("tipo"):
        tipo = sito["tipo"]
    if procedura:
        tipo_bando = dettaglio.get("tipo_bando")
        descrizione = f"{descrizione}\nProcedura: {procedura}" + (f" ({tipo_bando})" if tipo_bando else "")
    cig = _testo(voce.get("CIG")).upper()
    return Opportunita(
        id=oid,
        fonti=[sito["nome_fonte"]],
        titolo=titolo,
        ente=ente,
        scadenza=_scadenza(voce.get("DataScadenzaBando")),
        pubblicazione=dettaglio.get("pubblicazione"),
        importo=parse_importo(_testo(voce.get("BaseAsta"))) or None,   # 0,00 = non indicato
        cpv=cpv,
        cig=[cig] if re.fullmatch(r"[0-9A-Z]{10}", cig) else [],
        tipo=tipo,
        link=sito["link"].replace("{id}", id_doc) if sito.get("link") else None,
        regione=sito.get("regione"),
        motivo_match=motivo,
        descrizione=(descrizione[:600] if descrizione and descrizione != titolo else None),
    )


def _carica_cache(sito: dict) -> dict:
    try:
        return json.loads((STATO / f"{sito['sigla']}_dettagli.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _salva_cache(sito: dict, cache: dict) -> None:
    STATO.mkdir(parents=True, exist_ok=True)
    (STATO / f"{sito['sigla']}_dettagli.json").write_text(
        json.dumps(cache, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _leggi_dettagli(sito: dict, voci: list[dict], filtro: Filtro, sess) -> dict:
    """Dettagli (ridotti) delle voci candidate, dalla cache o dal servizio."""
    cache = _carica_cache(sito)
    presenti = {str(v.get("IdDoc")) for v in voci}
    cache = {k: v for k, v in cache.items() if k in presenti}     # via le voci uscite dall'elenco
    attesa = (float(sito.get("timeout_connessione", 20)), 60)
    pausa = float(sito.get("pausa_secondi", 0.5))
    massimo = int(sito.get("max_dettagli", 200))
    tipo_doc = sito.get("tipo_documento", "BANDO_GARA_PORTALE")
    chiesti = falliti = rimandati = 0
    for v in voci:
        id_doc = str(v.get("IdDoc") or "")
        if not id_doc or id_doc in cache:
            continue
        titolo, oggetto = _testo(v.get("Titolo")), _testo(v.get("Oggetto"))
        if not _candidata(titolo, oggetto, _rinomina(_testo(v.get("Ente")), sito), filtro):
            continue
        if chiesti >= massimo:
            rimandati += 1
            continue
        chiesti += 1
        try:
            r = sess.post(sito["url_dettaglio"], headers=HEADERS, timeout=attesa,
                          data=json.dumps({"idDoc": id_doc, "typeDoc": tipo_doc, "ml_xslt": "", "params": ""}))
            r.raise_for_status()
            ridotto = _riduci_dettaglio(r.json())
        except Exception as e:
            log.warning("%s: dettaglio %s non letto (%s)", sito["nome"], id_doc, e)
            ridotto = None
        if ridotto is None:
            falliti += 1
        else:
            ridotto["letto_il"] = date.today().isoformat()
            cache[id_doc] = ridotto
        time.sleep(pausa)
    _salva_cache(sito, cache)
    log.info("%s: dettagli chiesti %d (falliti %d, rimandati %d), in cache %d",
             sito["nome"], chiesti, falliti, rimandati, len(cache))
    if chiesti and falliti == chiesti:
        ERRORI.append(f"{sito['nome']}: nessun dettaglio letto su {chiesti} (servizio di dettaglio "
                      f"non disponibile? le voci passano dal solo filtro testuale)")
    if rimandati:
        ERRORI.append(f"{sito['nome']}: {rimandati} dettagli rimandati alla prossima esecuzione "
                      f"(max_dettagli {massimo})")
    return cache


def _leggi_sito(sito: dict, filtro: Filtro, sess) -> list[Opportunita]:
    sess = _sessione(sito, sess)
    attesa = (float(sito.get("timeout_connessione", 20)), 60)
    per_pagina = int(sito.get("per_pagina", 50))
    max_pagine = int(sito.get("max_pagine", 10))
    lista_escl = sito.get("escludi_oggetti_regex") or []
    escl = re.compile("|".join(f"(?:{p})" for p in lista_escl), re.IGNORECASE) if lista_escl else None
    risultati: dict[str, Opportunita] = {}
    tutte: list[dict] = []
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
        tutte.extend(nuove)
        if len(voci) < per_pagina or (isinstance(totale, int) and lette >= totale):
            break
        time.sleep(1)
    else:
        if isinstance(totale, int) and lette < totale:
            ERRORI.append(f"{sito['nome']}: lette {lette} voci su {totale} (max_pagine raggiunto, "
                          f"dati parziali)")

    dettagli = _leggi_dettagli(sito, tutte, filtro, sess) if sito.get("dettaglio") else {}
    for v in tutte:
        o = _converti(v, sito, filtro, escl, dettagli.get(str(v.get("IdDoc"))))
        if o:
            risultati[o.id] = o

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
