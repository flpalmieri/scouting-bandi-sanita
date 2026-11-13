"""Piattaforma di e-procurement Sintel (ARIA S.p.A., Regione Lombardia).

Sintel e' usata da tutte le aziende sanitarie lombarde (ASST, ATS, IRCCS, AREU),
da ARIA come soggetto aggregatore e anche da enti di altre regioni (es. aziende
sanitarie del Veneto e del Piemonte). La ricerca pubblica e' la pagina JSF
    https://www.sintel.regione.lombardia.it/eprocdata/sintelSearch.xhtml
che mostra i risultati solo dopo l'invio del modulo ("Applica"). Il modulo:
  1. apre la pagina e legge il modulo di ricerca (campi e codice ViewState);
  2. lo invia con i filtri "Sede legale" (una regione per volta, cosi' ogni gara
     ha la regione giusta) e "Stato" (pubblicata, aperta pre-qualifica);
  3. porta gli elementi per pagina a 200 e segue il pulsante ">" fino all'ultima pagina.

I nomi dei campi (es. "j_idt28:j_idt31:auctionStatus") sono generati da JSF e
possono cambiare: si leggono ogni volta dalla pagina, cercando le parti stabili.

Colonne della tabella (campione del 13/11/2026): ID SINTEL, STAZIONE APPALTANTE,
NOME PROCEDURA (link auctionDetail.xhtml?id=...), CODICE GARA, TIPO, STATO,
AMBITO DELLA PROCEDURA, VALORE ECONOMICO, DATA INIZIO, DATA FINE.
Non ci sono dati personali nell'elenco.
"""
from __future__ import annotations

import html
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .common import Filtro, Opportunita, log, parse_data, parse_importo, sessione_http

CAMPIONI = Path("state/campioni")
ERRORI: list[str] = []

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9",
}

# Nomi delle regioni come li vuole il filtro "Sede legale" -> nome in dashboard
NOMI_REGIONI = {"Emilia Romagna": "Emilia-Romagna"}

RE_FORM = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.DOTALL | re.IGNORECASE)
RE_INPUT = re.compile(r"<input\b([^>]*?)/?>", re.IGNORECASE)
RE_SELECT = re.compile(r"<select\b([^>]*)>(.*?)</select>", re.DOTALL | re.IGNORECASE)
RE_OPTION = re.compile(r"<option\b([^>]*)>", re.IGNORECASE)
RE_ATTR = re.compile(r'([\w:\-]+)\s*=\s*"([^"]*)"')
RE_COMMENTO = re.compile(r"<!--.*?-->", re.DOTALL)
RE_SCRIPT = re.compile(r"<script\b.*?</script>", re.DOTALL | re.IGNORECASE)
RE_TAG = re.compile(r"<[^>]+>")
RE_CIG = re.compile(r"\b(?=[0-9A-Z]{10}\b)(?=[0-9A-Z]*\d)[0-9A-Z]{10}\b")
RE_CIG_TITOLO = re.compile(r"\bCIG\b\W{0,5}([0-9A-Z]{10})\b", re.IGNORECASE)


def _attributi(s: str) -> dict:
    return {k.lower(): html.unescape(v) for k, v in RE_ATTR.findall(s)}


def _testo(frammento: str) -> str:
    t = RE_SCRIPT.sub(" ", RE_COMMENTO.sub(" ", frammento or ""))
    t = html.unescape(RE_TAG.sub(" ", t))
    return " ".join(t.split())


def _moduli(pagina: str) -> list[tuple[dict, str]]:
    return [(_attributi(a), corpo) for a, corpo in RE_FORM.findall(pagina)]


def _campi_modulo(corpo: str) -> list[tuple[str, str]]:
    """Campi che il browser invierebbe: hidden e text sempre, checkbox solo se spuntate.
    I pulsanti di invio sono esclusi (si aggiungono a parte)."""
    campi = []
    for a in (_attributi(x) for x in RE_INPUT.findall(corpo)):
        nome, tipo = a.get("name"), (a.get("type") or "text").lower()
        if not nome or tipo in ("submit", "button", "reset", "image"):
            continue
        if tipo in ("checkbox", "radio"):
            if "checked" in a:
                campi.append((nome, a.get("value", "on")))
        else:
            campi.append((nome, a.get("value", "")))
    for attr, opzioni in RE_SELECT.findall(corpo):
        a = _attributi(attr)
        scelta = [_attributi(o).get("value", "") for o in RE_OPTION.findall(opzioni) if "selected" in o]
        if a.get("name") and scelta:
            campi.append((a["name"], scelta[0]))
    return campi


def _modulo_ricerca(pagina: str) -> tuple[str, list[tuple[str, str]], str, str, dict]:
    """Trova il modulo di ricerca: restituisce (action, campi, nome campo stato,
    nome campo sede legale, nome pulsante)."""
    for attr, corpo in _moduli(pagina):
        if "auctionStatus" not in corpo or "auctionLegalSite" not in corpo:
            continue
        nomi = [_attributi(x).get("name", "") for x in RE_INPUT.findall(corpo)]
        stato = next((n for n in nomi if n.endswith(":auctionStatus")), None)
        sede = next((n for n in nomi if n.endswith(":auctionLegalSite")), None)
        pulsante = next((_attributi(x) for x in RE_INPUT.findall(corpo)
                         if (_attributi(x).get("type") or "").lower() == "submit"), None)
        if stato and sede and pulsante:
            return attr.get("action", ""), _campi_modulo(corpo), stato, sede, pulsante
    raise RuntimeError("modulo di ricerca non trovato nella pagina (vedi state/campioni)")


def _modulo_paginazione(pagina: str) -> tuple[str, list[tuple[str, str]], str | None, str | None, dict]:
    """Modulo con "Elementi per pagina": (action, campi, nome select, valore attuale,
    {testo del pulsante: parametro da aggiungere})."""
    for attr, corpo in _moduli(pagina):
        if "paginationCustom" not in corpo:
            continue
        campi = _campi_modulo(corpo)
        sel = RE_SELECT.search(corpo)
        nome_sel = _attributi(sel.group(1)).get("name") if sel else None
        attuale = next((v for n, v in campi if n == nome_sel), None)
        pulsanti = {}
        for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", corpo, re.DOTALL | re.IGNORECASE):
            clic = html.unescape(_attributi(m.group(1)).get("onclick", ""))
            p = re.search(r"\{'([^']+)':'([^']+)'\}", clic)
            if p:
                pulsanti[_testo(m.group(2))] = (p.group(1), p.group(2))
        return attr.get("action", ""), campi, nome_sel, attuale, pulsanti
    return "", [], None, None, {}


def leggi_righe(pagina: str) -> list[dict]:
    """Righe della tabella dei risultati come dizionari {intestazione: testo}
    (piu' "_link" con l'indirizzo del dettaglio)."""
    m = re.search(r'<table[^>]*id="result"[^>]*>(.*?)</table>', pagina, re.DOTALL | re.IGNORECASE)
    if not m:
        return []
    tabella = m.group(1)
    testa = re.search(r"<thead>(.*?)</thead>", tabella, re.DOTALL | re.IGNORECASE)
    intestazioni = [_testo(h).upper() for h in
                    re.findall(r"<th\b[^>]*>(.*?)</th>", testa.group(1) if testa else "", re.DOTALL | re.IGNORECASE)]
    corpo = re.search(r"<tbody>(.*?)</tbody>", tabella, re.DOTALL | re.IGNORECASE)
    righe = []
    for tr in re.findall(r"<tr\b[^>]*>(.*?)</tr>", corpo.group(1) if corpo else "", re.DOTALL | re.IGNORECASE):
        celle = re.findall(r"<td\b[^>]*>(.*?)</td>", tr, re.DOTALL | re.IGNORECASE)
        if not celle:
            continue
        riga = {intestazioni[i] if i < len(intestazioni) and intestazioni[i] else f"_{i}": _testo(c)
                for i, c in enumerate(celle)}
        link = re.search(r'href="([^"]*auctionDetail[^"]*)"', tr)
        riga["_link"] = html.unescape(link.group(1)) if link else ""
        righe.append(riga)
    return righe


def _tipo(tipo_sintel: str, titolo: str) -> str:
    t, ts = titolo.lower(), tipo_sintel.lower()
    if "consultazion" in t:
        return "consultazione di mercato"
    if ("indagine di mercato" in ts or "manifestazione di interesse" in ts
            or "avviso esplorativo" in t or "manifestazion" in t):
        return "avviso esplorativo"
    return "bando"


def _descrizione(tipo_sintel: str, ambito: str) -> str:
    # "Procedura negoziata senza pubblicazione di un bando" e' una tipologia di Sintel
    # usata anche per le normali negoziate sotto soglia (art. 50): la si accorcia perche'
    # la dashboard non la scambi per un avviso di infungibilita' (art. 76).
    tipo = re.sub(r"senza\s+pubblicazione\s+di\s+un\s+bando", "senza bando", tipo_sintel, flags=re.IGNORECASE)
    ambito = re.sub(r"^procedur[ae]\s+per\s+", "", ambito.rstrip(". "), flags=re.IGNORECASE)
    return "; ".join(p for p in (f"Tipo Sintel: {tipo}" if tipo else "", f"Ambito: {ambito}" if ambito else "") if p)


def converti(riga: dict, cfg: dict, regione: str | None, filtro: Filtro) -> Opportunita | None:
    id_sintel = re.sub(r"\D", "", riga.get("ID SINTEL", ""))
    titolo = riga.get("NOME PROCEDURA", "").strip()
    ente = riga.get("STAZIONE APPALTANTE", "").strip()
    if not id_sintel or not titolo:
        return None
    tipo_sintel = riga.get("TIPO", "").strip()
    if tipo_sintel.lower() in {t.lower() for t in cfg.get("escludi_tipi", [])}:
        return None
    descrizione = _descrizione(tipo_sintel, riga.get("AMBITO DELLA PROCEDURA", ""))
    motivo = filtro.valuta_testo(titolo, ente, descrizione)
    if not motivo:
        return None

    cig = set()
    codice = riga.get("CODICE GARA", "").strip().upper()
    if RE_CIG.fullmatch(codice) and codice != "0000000000":
        cig.add(codice)
    cig.update(c.upper() for c in RE_CIG_TITOLO.findall(titolo) if c != "0000000000")
    importo = parse_importo(riga.get("VALORE ECONOMICO", ""))
    href = riga.get("_link") or cfg.get("url_dettaglio", "").format(id=id_sintel)
    return Opportunita(
        id=f"{cfg.get('sigla', 'sintel')}:{id_sintel}",
        fonti=[cfg.get("nome_fonte", "Sintel")],
        titolo=titolo,
        ente=ente,
        scadenza=parse_data(riga.get("DATA FINE", "")),
        pubblicazione=parse_data(riga.get("DATA INIZIO", "")),
        importo=importo if importo else None,          # 0,00 € = importo non indicato
        cig=sorted(cig),
        tipo=_tipo(tipo_sintel, titolo),
        link=urljoin(cfg["url"], href) if href else None,
        regione=NOMI_REGIONI.get(regione, regione) if regione else cfg.get("regione_default"),
        motivo_match=motivo,
        descrizione=descrizione or None,
    )


def _salva_campione(nome: str, pagina: str) -> None:
    try:
        CAMPIONI.mkdir(parents=True, exist_ok=True)
        (CAMPIONI / f"sintel_{nome}.html").write_text(pagina[:300000], encoding="utf-8")
    except OSError:
        log.exception("Sintel: impossibile salvare il campione %s", nome)


class SintelNonRaggiungibile(RuntimeError):
    """Il sito non risponde o rifiuta l'accesso: inutile provare le altre regioni."""


def _controlla(r, nome: str) -> str:
    """Salva sempre le risposte in errore, cosi' si vede cosa ha restituito il server."""
    if r.status_code >= 400:
        _salva_campione(f"errore_{nome}", f"<!-- HTTP {r.status_code} -->\n" + (r.text or ""))
        if r.status_code in (401, 403, 407, 429):
            raise SintelNonRaggiungibile(f"HTTP {r.status_code} (accesso rifiutato), "
                                         f"vedi state/campioni/sintel_errore_{nome}.html")
        raise RuntimeError(f"HTTP {r.status_code}, vedi state/campioni/sintel_errore_{nome}.html")
    return r.text


def _richiesta(sess, metodo: str, url: str, nome: str, **kw) -> str:
    try:
        r = sess.request(metodo, url, timeout=(20, 90), **kw)
    except requests.exceptions.RequestException as e:
        raise SintelNonRaggiungibile(f"{type(e).__name__}: {e}") from e
    return _controlla(r, nome)


def _invia(sess, url: str, action: str, campi: list[tuple[str, str]], nome: str = "ricerca"):
    return _richiesta(sess, "POST", urljoin(url, action) if action else url, nome,
                      data=campi, headers={**HEADERS, "Referer": url})


def _cerca(cfg: dict, sess, regione: str | None) -> list[dict]:
    """Esegue una ricerca e scorre tutte le pagine. Restituisce le righe lette."""
    url = cfg["url"]
    pausa = float(cfg.get("pausa_secondi", 1))
    etichetta = (regione or "tutte").lower().replace(" ", "_")
    modulo = _richiesta(sess, "GET", url, f"apertura_{etichetta}", headers=HEADERS)
    if not CAMPIONI.joinpath("sintel_modulo.html").exists():
        _salva_campione("modulo", modulo)          # la pagina iniziale, una volta sola
    try:
        action, campi, campo_stato, campo_sede, pulsante = _modulo_ricerca(modulo)
    except RuntimeError:
        _salva_campione("modulo", modulo)
        raise RuntimeError("modulo di ricerca non trovato: vedi state/campioni/sintel_modulo.html")

    campi = [(n, v) for n, v in campi if n not in (campo_stato, campo_sede)]
    campi += [(campo_stato, s) for s in cfg.get("stati", ["Open", "PublishedAcceptingRequest"])]
    if regione:
        campi.append((campo_sede, regione))
    else:  # tutte le sedi, come il browser con "Tutte" spuntato
        campi += [(campo_sede, v) for v in ["Tutte"] + list(cfg.get("regioni", []))]
    campi.append((pulsante["name"], pulsante.get("value", "Applica")))
    time.sleep(pausa)
    pagina = _invia(sess, url, action, campi, f"ricerca_{etichetta}")
    _salva_campione(etichetta, pagina)

    # Elementi per pagina: si porta al massimo per fare meno richieste
    per_pagina = str(cfg.get("elementi_per_pagina", 200))
    p_action, p_campi, p_select, attuale, _ = _modulo_paginazione(pagina)
    if p_select and attuale != per_pagina and leggi_righe(pagina):
        p_campi = [(n, per_pagina if n == p_select else v) for n, v in p_campi]
        time.sleep(pausa)
        pagina = _invia(sess, url, p_action, p_campi, f"paginazione_{etichetta}")

    righe, visti = [], set()
    for n_pagina in range(1, int(cfg.get("max_pagine", 10)) + 1):
        nuove = [r for r in leggi_righe(pagina) if r.get("ID SINTEL") not in visti]
        if not nuove:
            break
        righe.extend(nuove)
        visti.update(r.get("ID SINTEL") for r in nuove)
        p_action, p_campi, _, _, pulsanti = _modulo_paginazione(pagina)
        avanti = pulsanti.get(">")
        if not avanti:
            break
        if n_pagina == int(cfg.get("max_pagine", 10)):
            ERRORI.append(f"Sintel {regione or 'tutte'}: raggiunto max_pagine ({n_pagina}), dati parziali")
            break
        time.sleep(pausa)
        try:
            pagina = _invia(sess, url, p_action, p_campi + [avanti], f"pagina_{etichetta}")
        except Exception as e:  # le pagine gia' lette restano valide
            ERRORI.append(f"Sintel {regione or 'tutte'}: pagina {n_pagina + 1} in errore ({e}), dati parziali")
            break
    log.info("Sintel %s: %d procedure lette", regione or "tutte le sedi", len(righe))
    return righe


def raccogli(cfg_tutto: dict, filtro: Filtro, sess=None) -> list[Opportunita]:
    cfg = cfg_tutto["sintel"]
    # Sessione propria (i cookie JSF restano separati) con pochi tentativi: se Sintel non
    # risponde non deve consumare il tempo del workflow riprovando ogni regione.
    sess_sintel = sessione_http()
    sess_sintel.mount("https://", HTTPAdapter(max_retries=Retry(
        total=int(cfg.get("tentativi", 2)), backoff_factor=2,
        status_forcelist=(500, 502, 503, 504), allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False)))     # dopo l'ultimo tentativo si tiene la risposta, per salvarla
    regioni = list(cfg.get("regioni", [])) if cfg.get("per_regione", True) else [None]
    risultati: dict[str, Opportunita] = {}
    regione_di: dict[str, str] = {}
    errori, lette, doppie = [], 0, []
    for regione in regioni:
        try:
            righe = _cerca(cfg, sess_sintel, regione)
        except SintelNonRaggiungibile as e:
            log.error("Sintel %s: sito non raggiungibile (%s), salto le altre regioni", regione, e)
            raise RuntimeError(f"sito non raggiungibile: {e}") from e
        except Exception as e:
            log.exception("Sintel %s: errore", regione)
            errori.append(f"{regione or 'tutte'}: {e}")
            continue
        lette += len(righe)
        for riga in righe:
            ids = riga.get("ID SINTEL", "")
            if regione and ids in regione_di and regione_di[ids] != regione:
                doppie.append(ids)   # il filtro "Sede legale" non e' stato applicato dal server
                continue
            regione_di.setdefault(ids, regione)
            o = converti(riga, cfg, regione, filtro)
            if o:
                risultati[o.id] = o
    if doppie:
        ERRORI.append(f"Sintel: {len(doppie)} procedure compaiono in piu' regioni (es. {doppie[0]}): "
                      "il filtro Sede legale non sembra funzionare, valutare per_regione: false")
    if errori and len(errori) == len(regioni):
        raise RuntimeError("; ".join(errori))
    if lette == 0:
        raise RuntimeError("nessuna procedura letta: controllare state/campioni/sintel_*.html")
    ERRORI.extend(f"Sintel {e}" for e in errori)
    log.info("Sintel: %d procedure lette, %d pertinenti", lette, len(risultati))
    return list(risultati.values())
