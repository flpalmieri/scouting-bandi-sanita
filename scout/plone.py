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

Opzioni per i portali grandi (aggiunte per Intercent-ER, 20/11/2026):
  percorsi        - piu' sezioni dello stesso sito in un solo blocco, al posto di percorso:
                    elenco di indirizzi oppure di {percorso, tipo}; tipo (es. "consultazione")
                    aiuta a riconoscere il tipo di avviso. max_risultati vale per ogni sezione.
  solo_aperte     - true: si leggono solo le voci con scadenza futura. Le voci arrivano
                    ordinate per scadenza decrescente (Intercent-ER ha 38.000 voci storiche) e la
                    lettura si ferma alla prima pagina fatta SOLO di voci scadute: su Intercent-ER
                    l'ordine non e' perfetto (al primo lancio, 20/11/2026, alcune voci aperte
                    comparivano dopo voci gia' scadute), quindi la prima voce scaduta non basta.
                    Le inversioni si contano e si scrivono nel log, con la piu' ampia; tra gli
                    errori finisce solo il caso con possibile perdita di dati (max_risultati
                    raggiunto senza una pagina di sole scadute).
  dettaglio       - nome di un servizio di dettaglio da chiamare per ogni voce candidata
                    (es. intercenter-data: GET <host>/++api++<percorso della voce>/@intercenter-data).
                    Si chiama solo per le voci che passano la parte del filtro che non dipende
                    dall'ente (parole IT nel titolo o nella descrizione): poche decine di
                    richieste invece di centinaia. Dal dettaglio si leggono SOLO ente_appaltante,
                    stato_procedura, cig, importo_appalto, type, procedura_gara e tipo_bando_gara:
                    niente referenti, RUP, contatti o allegati. Le voci si leggono una alla volta,
                    con pausa_secondi tra una richiesta e l'altra (default 0.5). I campi
                    "Evidenza Pubblica" e CPV dell'XML di SATER non si leggono: al primo lancio
                    valevano "si" e vuoto per tutte le 18 voci lette.
  ente_se_vuoto   - nome dell'ente da usare quando il dettaglio non lo indica o non si legge.
  escludi_stati   - valori di stato_procedura da scartare (es. Revocato).
  escludi_campi   - {campo del dettaglio: [valori]} da scartare, es. {procedura_gara:
                    [Affidamento Diretto]} (procedure a invito rivolte a un fornitore scelto).
                    Le voci scartate da escludi_stati ed escludi_campi vengono tolte anche dallo
                    storico (ESCLUSI, vedi main.py).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .common import Filtro, Opportunita, log, parse_data, parse_importo
from .portali_html import RE_CIG, _tipo

CAMPIONI = Path("state/campioni")
ERRORI: list[str] = []
ESCLUSI: set[str] = set()   # id delle voci scartate da una regola esplicita (stato revocato)
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
# Campi del dettaglio che si leggono (e che finiscono nel campione): nessun dato personale.
CAMPI_DETTAGLIO = ("title", "type", "ente_appaltante", "stato_procedura", "importo_appalto",
                   "procedura_gara", "tipo_bando_gara", "criterio", "scadenza", "pubdate",
                   "sospeso", "in_sospensione")

try:
    from zoneinfo import ZoneInfo
    _ROMA = ZoneInfo("Europe/Rome")
except Exception:          # senza dati dei fusi si usa la data UTC
    _ROMA = None


def _data(valore) -> str | None:
    """Data ISO di Plone ("2026-11-16T08:00:00+00:00") come data italiana. Gli anni prima
    del 1971 valgono "non indicata": Plone scrive 1100-01-01 (Galliera) o 1960-01-01
    (Intercent-ER) per i campi vuoti."""
    if not valore or not isinstance(valore, str):
        return None
    try:
        d = datetime.fromisoformat(valore.replace("Z", "+00:00"))
    except ValueError:
        return parse_data(valore)
    if d.year < 1971:
        return None
    if d.tzinfo and _ROMA:
        d = d.astimezone(_ROMA)
    return d.date().isoformat()


def _istante(valore) -> datetime | None:
    """Data e ora di Plone in UTC (per confrontare le scadenze con l'ora attuale)."""
    if not valore or not isinstance(valore, str):
        return None
    try:
        d = datetime.fromisoformat(valore.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.year < 1971:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _sezioni(sito: dict) -> list[dict]:
    """Le sezioni da leggere: percorso (una) oppure percorsi (piu' di una)."""
    if sito.get("percorsi"):
        return [p if isinstance(p, dict) else {"percorso": p} for p in sito["percorsi"]]
    return [{"percorso": sito["percorso"]}]


def corpo_ricerca(sito: dict, b_start: int, b_size: int, percorso: str | None = None) -> dict:
    return {
        "metadata_fields": CAMPI,
        "b_start": b_start,
        "b_size": b_size,
        "query": [
            {"i": "path", "o": "plone.app.querystring.operation.string.relativePath",
             "v": percorso or sito["percorso"]},
            {"i": "portal_type", "o": "plone.app.querystring.operation.selection.any",
             "v": sito.get("tipi_contenuto", ["Bando"])},
        ],
        "sort_on": "scadenza_bando",
        "sort_order": "descending",
    }


def _regex(lista) -> re.Pattern | None:
    return re.compile("|".join(f"(?:{p})" for p in lista), re.IGNORECASE) if lista else None


def _testi(voce: dict) -> tuple[str, str]:
    titolo = " ".join((voce.get("title") or voce.get("Title") or "").split())
    descrizione = " ".join((voce.get("description") or voce.get("Description") or "").split())
    if descrizione.lower() == titolo.lower():   # Intercent-ER ripete spesso il titolo
        descrizione = ""
    return titolo, descrizione


def _id_voce(voce: dict, sito: dict) -> str:
    chiave = voce.get("@id") or _testi(voce)[0]
    return f"{sito['sigla']}:{hashlib.sha1(chiave.encode('utf-8')).hexdigest()[:12]}"


def _scartata(voce: dict, sito: dict) -> bool:
    """Regole del sito che non dipendono dal dettaglio (titolo e descrizione)."""
    titolo, descrizione = _testi(voce)
    if not titolo:
        return True
    escl = _regex(sito.get("escludi_titoli_regex"))
    if escl and escl.search(titolo):
        return True
    escl = _regex(sito.get("escludi_descrizioni_regex"))
    return bool(escl and escl.search(descrizione))


def _testo_campo(valore) -> str:
    """Valore leggibile di un campo del dettaglio (solo testo semplice)."""
    if isinstance(valore, str):
        return " ".join(valore.split())
    if isinstance(valore, dict):   # vocabolari Plone: {"token": .., "title": ..}
        return _testo_campo(valore.get("title") or valore.get("token"))
    return ""


def converti(voce: dict, sito: dict, filtro: Filtro, dettaglio: dict | None = None,
             tipo_sezione: str = "") -> Opportunita | None:
    if _scartata(voce, sito):
        return None
    titolo, descrizione = _testi(voce)
    dettaglio = dettaglio or {}
    ente = _testo_campo(dettaglio.get("ente_appaltante")) or sito.get("ente_se_vuoto") or sito.get("ente", "")
    # Descrizioni dei lotti (dettaglio): solo quelle diverse dal titolo, per non ripeterlo.
    cig_dettaglio = [c for c in dettaglio.get("cig") or [] if isinstance(c, dict)]
    lotti = []
    for c in cig_dettaglio:
        d = _testo_campo(c.get("descrizione"))
        if d and d.lower() not in titolo.lower() and d not in lotti:
            lotti.append(d)
    procedura = " ".join(x for x in (_testo_campo(dettaglio.get("procedura_gara")),
                                     f"({_testo_campo(dettaglio.get('tipo_bando_gara'))})"
                                     if _testo_campo(dettaglio.get("tipo_bando_gara")) else "") if x)
    tipologia = voce.get("tipologia_bando") or ""
    testo = "; ".join(x for x in (
        descrizione,
        f"Lotti: {' | '.join(lotti)}" if lotti else "",
        f"Procedura: {procedura}" if procedura else "",
        f"Tipologia: {tipologia}" if isinstance(tipologia, str) and tipologia else "",
    ) if x)
    motivo = filtro.valuta_testo(titolo, ente, testo)
    if not motivo:
        return None
    link = voce.get("@id") or None
    cig = {c.upper() for c in RE_CIG.findall(f"{titolo} {descrizione}") if c.strip("0")}
    cig |= {str(c.get("codice")).upper() for c in cig_dettaglio
            if re.fullmatch(r"[0-9A-Za-z]{10}", str(c.get("codice") or "")) and str(c.get("codice")).strip("0")}
    return Opportunita(
        id=_id_voce(voce, sito),
        fonti=[sito.get("nome_fonte", sito["nome"])],
        titolo=titolo,
        ente=ente,
        scadenza=_data(voce.get("scadenza_bando")),
        pubblicazione=_data(voce.get("apertura_bando")) or _data(voce.get("effective")),
        importo=parse_importo(dettaglio.get("importo_appalto")) or None,
        cig=sorted(cig),
        tipo=_tipo(f"{titolo} {descrizione}", tipo_sezione),
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
        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET", "POST"}))))
    return propria


def _nome_campione(sito: dict, i: int, n: int) -> str:
    return f"{sito['sigla']}.json" if n == 1 else f"{sito['sigla']}_{i + 1}.json"


def _leggi_sezione(sito: dict, sezione: dict, campione: str, sess, avvisi: list[str]) -> tuple[list[dict], object]:
    """Voci di una sezione. Con solo_aperte si tengono le voci con scadenza futura (o senza
    scadenza) e ci si ferma alla prima pagina che contiene voci scadute."""
    percorso = sezione["percorso"].rstrip("/")
    url = f"{sito['host'].rstrip('/')}/++api++{percorso}/@querystring-search"
    attesa = (float(sito.get("timeout_connessione", 20)), 60)
    massimo = int(sito.get("max_risultati", 100))
    solo_aperte = bool(sito.get("solo_aperte"))
    adesso = datetime.now(timezone.utc)
    voci: list[dict] = []
    lette = 0
    totale = None
    inversioni = 0           # voci con scadenza piu' lontana della precedente
    salto_max = None         # inversione piu' ampia (timedelta)
    ultima = None            # ultima scadenza vista, per controllare l'ordinamento
    finite = False           # letta una pagina fatta solo di voci scadute (con solo_aperte)
    aperte_dopo = 0          # voci aperte trovate dopo la prima scaduta: misura il disordine utile
    vista_scaduta = False
    while lette < massimo:
        r = sess.post(url, headers=HEADERS, timeout=attesa,
                      data=json.dumps(corpo_ricerca(sito, lette, min(B_SIZE, massimo - lette), percorso)))
        if not lette:   # il campione si salva prima di interpretare la risposta, anche se e' un errore
            CAMPIONI.mkdir(parents=True, exist_ok=True)
            (CAMPIONI / campione).write_bytes(r.content[:60000])
        r.raise_for_status()
        try:
            dati = r.json()
        except ValueError as e:
            raise RuntimeError(f"{percorso}: la risposta non e' JSON (vedi state/campioni)") from e
        pagina = dati.get("items") if isinstance(dati, dict) else None
        if not isinstance(pagina, list):
            raise RuntimeError(f"{percorso}: manca l'elenco 'items' (vedi state/campioni)")
        totale = dati.get("items_total", len(pagina))
        lette += len(pagina)
        if not solo_aperte:
            voci.extend(pagina)
        else:
            aperte_pagina = 0
            for v in pagina:
                scad = _istante(v.get("scadenza_bando"))
                if scad and ultima and scad > ultima:
                    inversioni += 1
                    salto_max = max(salto_max or (scad - ultima), scad - ultima)
                if scad:
                    ultima = scad
                if scad and scad < adesso:
                    vista_scaduta = True
                else:
                    voci.append(v)
                    aperte_pagina += 1
                    if vista_scaduta:
                        aperte_dopo += 1
            if pagina and aperte_pagina == 0:
                finite = True
                break
        if not pagina or lette >= totale:
            break
    if lette == 0:
        # La ricerca comprende anche le voci scadute: zero voci vuol dire percorso o tipo sbagliati.
        raise RuntimeError(f"{percorso}: nessuna voce trovata (percorso o tipo di contenuto cambiati?)")
    if solo_aperte and not finite and lette >= massimo and lette < (totale or 0):
        avvisi.append(f"{percorso}: lette {lette} voci senza arrivare a una pagina di sole scadute, "
                      f"possibile troncamento (dati parziali: alzare max_risultati)")
    log.info("Plone %s %s: %d voci lette (su %s), %d tenute", sito["nome"], percorso, lette, totale, len(voci))
    if solo_aperte and inversioni:
        log.info("Plone %s %s: ordinamento per scadenza imperfetto: %d inversioni (la piu' ampia %s), "
                 "%d voci aperte trovate dopo la prima scaduta", sito["nome"], percorso, inversioni,
                 salto_max, aperte_dopo)
    return voci, totale


def _url_dettaglio(sito: dict, voce: dict) -> str | None:
    link = voce.get("@id") or ""
    parti = urlsplit(link)
    if not parti.path or urlsplit(sito["host"]).netloc != parti.netloc:
        return None
    return f"{sito['host'].rstrip('/')}/++api++{parti.path.rstrip('/')}/@{sito['dettaglio']}"


def _leggi_dettaglio(sito: dict, voce: dict, sess, campioni: list) -> dict:
    """Solo i campi di CAMPI_DETTAGLIO (e i codici e le descrizioni dei lotti): il dettaglio
    contiene anche referenti, RUP con codice fiscale e allegati, che non si leggono."""
    url = _url_dettaglio(sito, voce)
    if not url:
        raise RuntimeError(f"indirizzo della voce non valido: {voce.get('@id')}")
    attesa = (float(sito.get("timeout_connessione", 20)), 60)
    r = sess.get(url, headers={k: v for k, v in HEADERS.items() if k != "Content-Type"}, timeout=attesa)
    if r.status_code >= 400 or "json" not in r.headers.get("Content-Type", "json"):
        campioni.append({"@id": voce.get("@id"), "http": r.status_code, "inizio": r.text[:2000]})
    r.raise_for_status()
    grezzo = r.json()
    dati = grezzo.get("data") if isinstance(grezzo, dict) and isinstance(grezzo.get("data"), dict) else grezzo
    if not isinstance(dati, dict):
        raise RuntimeError("risposta del dettaglio senza dati")
    ridotto = {k: dati.get(k) for k in CAMPI_DETTAGLIO if k in dati}
    ridotto["cig"] = [{"codice": c.get("codice"), "lotto": c.get("lotto"), "descrizione": c.get("descrizione")}
                      for c in dati.get("cig") or [] if isinstance(c, dict)]
    campioni.append({"@id": voce.get("@id"), **ridotto})
    return ridotto


def _leggi_sito(sito: dict, filtro: Filtro, sess) -> list[Opportunita]:
    sess = _sessione(sito, sess)
    sezioni = _sezioni(sito)
    avvisi: list[str] = []
    candidate: list[tuple[dict, str]] = []
    lette_tot = sezioni_fallite = 0
    for i, sezione in enumerate(sezioni):
        try:
            voci, _ = _leggi_sezione(sito, sezione, _nome_campione(sito, i, len(sezioni)), sess, avvisi)
        except Exception as e:
            if len(sezioni) == 1:
                raise
            log.exception("Plone %s: errore nella sezione %s", sito["nome"], sezione["percorso"])
            avvisi.append(f"{e} (dati parziali: le altre sezioni sono state lette)")
            sezioni_fallite += 1
            continue
        lette_tot += len(voci)
        candidate.extend((v, sezione.get("tipo", "")) for v in voci)
    if sezioni_fallite == len(sezioni):
        raise RuntimeError("; ".join(avvisi))

    trovate: dict[str, Opportunita] = {}
    dettagli_letti = dettagli_falliti = 0
    campioni_dettaglio: list = []
    pausa = float(sito.get("pausa_secondi", 0.5))
    stati_esclusi = {s.lower() for s in sito.get("escludi_stati") or []}
    campi_esclusi = {k: {str(v).strip().lower() for v in (vals or [])}
                     for k, vals in (sito.get("escludi_campi") or {}).items()}
    scartate = 0
    for voce, tipo_sezione in candidate:
        if _scartata(voce, sito):
            continue
        dettaglio = None
        if sito.get("dettaglio"):
            titolo, descrizione = _testi(voce)
            # Prefiltro: senza parole IT la voce non puo' passare, qualunque sia l'ente.
            if not filtro.parola_it(titolo, descrizione):
                continue
            try:
                if dettagli_letti or dettagli_falliti:
                    time.sleep(pausa)
                dettaglio = _leggi_dettaglio(sito, voce, sess, campioni_dettaglio)
                dettagli_letti += 1
            except Exception as e:
                dettagli_falliti += 1
                log.warning("Plone %s: dettaglio non letto per %s: %s", sito["nome"], voce.get("@id"), e)
            if dettaglio and (
                    str(dettaglio.get("stato_procedura") or "").strip().lower() in stati_esclusi
                    or any(_testo_campo(dettaglio.get(k)).lower() in vals for k, vals in campi_esclusi.items())):
                ESCLUSI.add(_id_voce(voce, sito))
                scartate += 1
                continue
        o = converti(voce, sito, filtro, dettaglio, tipo_sezione)
        if o:
            trovate[o.id] = o
    if sito.get("dettaglio"):
        CAMPIONI.mkdir(parents=True, exist_ok=True)
        (CAMPIONI / f"{sito['sigla']}_dettagli.json").write_text(
            json.dumps(campioni_dettaglio, indent=1, ensure_ascii=False)[:200000], encoding="utf-8")
        if dettagli_falliti:
            avvisi.append(f"{dettagli_falliti} dettagli su {dettagli_letti + dettagli_falliti} non letti: "
                          f"quelle voci usano l'ente \"{sito.get('ente_se_vuoto') or sito.get('ente', '')}\" "
                          f"(vedi state/campioni/{sito['sigla']}_dettagli.json)")
    log.info("Plone %s: %d voci tenute, %d dettagli letti (%d falliti), %d scartate per stato o "
             "tipo di procedura, %d pertinenti", sito["nome"], lette_tot, dettagli_letti,
             dettagli_falliti, scartate, len(trovate))
    ERRORI.extend(f"{sito['nome']}: {a}" for a in avvisi)
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
