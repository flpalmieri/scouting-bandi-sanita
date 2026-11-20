"""Piattaforme di e-procurement JAGGAER (ex BravoSolution), indirizzi "/esop/...".

Primo uso: eAppaltiFVG (Friuli Venezia Giulia, 20/11/2026), piattaforma regionale di ARCS,
ASUGI, ASUFC, ASFO, CRO, Burlo, Insiel, Regione e comuni.

L'elenco pubblico "Avvisi in Area Pubblica" (gare e avvisi di indagine attivi) e' nell'HTML
statico della pagina, senza servizi JSON:
    GET <host>/esop/guest/go/public/opportunity/current
    -> 302 a /esop/toolkit/opportunity/current/list.si?reset=true&resetstored=true
Serve uno User-Agent di browser completo (con "Mozilla/5.0" e basta il portale risponde con la
pagina "Aggiornare il browser") e un cookie di sessione: la sessione e' propria di ogni sito.
Al 20/11/2026: 76 voci in una sola pagina ("Visualizzazione risultati 1 - 76 di 76").

Colonne lette per nome di intestazione: Processi di Procurement (tipo di procedura),
Stazione Appaltante, Iniziativa (titolo, con l'id nel link goToDetail('<id>', ...)),
Tipologia Prestazione, Termine scadenza (corrente). La colonna "Protocollo Cartella di Gara"
e' sempre vuota. Niente CPV, CIG, importo, data di pubblicazione (stanno solo negli allegati
PDF): si usa il filtro testuale. La pagina di dettaglio non aggiunge nulla di utile al filtro
e non si chiede.

Avvisi, manifestazioni di interesse e preinformazioni stanno nella stessa lista: il tipo si
ricava da "Processi di Procurement" e dal titolo.

Dati personali: l'elenco non ne contiene (solo nomi di enti). Il campione in state/campioni/
contiene solo le colonne lette; l'HTML grezzo si salva solo se la pagina non e' leggibile.

Per un altro portale Jaggaer basta un blocco in config.yaml (sezione jaggaer.siti).
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .common import Filtro, Opportunita, log, parse_data

CAMPIONI = Path("state/campioni")
ERRORI: list[str] = []
ESCLUSI: set[str] = set()   # id scartati da una regola esplicita: main.py li toglie dallo storico

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}

# Intestazioni attese (minuscole, inizio del testo) -> nome interno.
COLONNE = {
    "processi di procurement": "procedura",
    "stazione appaltante": "ente",
    "protocollo": "protocollo",
    "iniziativa": "titolo",
    "tipologia prestazione": "tipologia",
    "termine scadenza": "scadenza",
}
OBBLIGATORIE = ("procedura", "ente", "titolo", "scadenza")

RE_RIGA = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
RE_CELLA = re.compile(r"<(t[hd])\b[^>]*>(.*?)</\1>", re.S | re.I)
RE_TAG = re.compile(r"<(?:[^>\"']|\"[^\"]*\"|'[^']*')*>")   # anche con '<br>' dentro gli attributi
RE_ID = re.compile(r"goToDetail\(\s*'(\d+)'")
RE_TOTALE = re.compile(r"js-fullsize-cnt[^>]*>\s*([\d.]+)\s*<")
RE_TABELLA = re.compile(r"<table\b[^>]*class=\"[^\"]*list-table[^\"]*\"[^>]*>(.*?)</table>", re.S | re.I)


def _testo(frammento: str) -> str:
    return " ".join(html.unescape(RE_TAG.sub(" ", frammento)).split())


def leggi_elenco(pagina: str) -> tuple[list[dict], int | None]:
    """Righe della tabella dell'elenco (solo le colonne note) e totale dichiarato dal portale."""
    m = RE_TABELLA.search(pagina)
    if not m:
        return [], None
    mt = RE_TOTALE.search(pagina)
    totale = int(mt.group(1).replace(".", "")) if mt else None
    indici: dict[int, str] = {}
    righe: list[dict] = []
    for tr in RE_RIGA.findall(m.group(1)):
        celle = RE_CELLA.findall(tr)
        if not celle:
            continue
        if not indici and all(tipo.lower() == "th" for tipo, _ in celle):
            for i, (_, contenuto) in enumerate(celle):
                nome = _testo(contenuto).lower()
                for inizio, interno in COLONNE.items():
                    if nome.startswith(inizio):
                        indici[i] = interno
            mancanti = [c for c in OBBLIGATORIE if c not in indici.values()]
            if mancanti:
                raise RuntimeError(f"intestazioni della tabella cambiate: mancano {', '.join(mancanti)}")
            continue
        if not indici:
            continue
        riga = {interno: _testo(celle[i][1]) for i, interno in indici.items() if i < len(celle)}
        mid = RE_ID.search(html.unescape(tr))
        riga["id"] = mid.group(1) if mid else ""
        if riga.get("titolo"):
            righe.append(riga)
    return righe, totale


def _scadenza(valore: str) -> str | None:
    d = parse_data(valore)
    if d and not ("1971" <= d[:4] <= "2100"):
        return None
    return d


def _tipo(procedura: str, titolo: str) -> str:
    p, t = procedura.lower(), titolo.lower()
    if "preinformazione" in p:
        return "pin (avviso di preinformazione)"       # la dashboard lo mostra come Preinformazione
    if "consultazion" in t or "consultazion" in p:
        return "consultazione di mercato"
    if "manifestazione" in p or re.search(r"\bavviso\b|indagine\s+di\s+mercato|manifestazion|\bRdI\b", titolo, re.I):
        return "avviso esplorativo"
    return "bando"


def _regex(lista) -> re.Pattern | None:
    return re.compile("|".join(f"(?:{e})" for e in lista), re.I) if lista else None


def converti(riga: dict, sito: dict, filtro: Filtro) -> tuple[Opportunita | None, bool]:
    """(opportunita o None, esclusa da una regola esplicita)."""
    titolo = riga.get("titolo", "")
    procedura = riga.get("procedura", "")
    oid = f"{sito['sigla']}:{riga.get('id') or re.sub(r'[^a-z0-9]+', '-', titolo.lower())[:80]}"
    if procedura.lower() in {p.lower() for p in sito.get("escludi_procedure", [])}:
        return None, True
    escl = _regex(sito.get("escludi_titoli_regex"))
    if escl and escl.search(titolo):
        return None, True
    mappa = {k.strip().lower(): v for k, v in (sito.get("enti_rinomina") or {}).items()}
    ente = mappa.get(riga.get("ente", "").strip().lower(), riga.get("ente", "").strip())
    parti = [f"Procedura: {procedura}" if procedura else "",
             f"Tipologia: {riga['tipologia']}" if riga.get("tipologia") else ""]
    descrizione = "; ".join(p for p in parti if p)
    motivo = filtro.valuta_testo(titolo, ente, descrizione)
    if not motivo:
        return None, False
    link = sito.get("link", "").replace("{id}", riga["id"]) if riga.get("id") else sito["url"]
    return Opportunita(
        id=oid,
        fonti=[sito["nome_fonte"]],
        titolo=titolo,
        ente=ente,
        scadenza=_scadenza(riga.get("scadenza", "")),
        tipo=_tipo(procedura, titolo),
        link=link,
        regione=sito.get("regione"),
        motivo_match=motivo,
        descrizione=descrizione or None,
    ), False


def _sessione(sito: dict) -> requests.Session:
    """Sessione propria del sito: cookie separati e User-Agent di browser."""
    s = requests.Session()
    s.headers.update(HEADERS)
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=int(sito.get("tentativi", 2)), backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET"}))))
    return s


def _salva_campione(sito: dict, righe: list[dict], totale: int | None) -> None:
    CAMPIONI.mkdir(parents=True, exist_ok=True)
    (CAMPIONI / f"{sito['sigla']}.json").write_text(
        json.dumps({"totale_dichiarato": totale, "righe": righe}, ensure_ascii=False, indent=1),
        encoding="utf-8")


def _salva_errore(sito: dict, contenuto: str) -> None:
    CAMPIONI.mkdir(parents=True, exist_ok=True)
    (CAMPIONI / f"{sito['sigla']}_errore.html").write_text(contenuto[:60000], encoding="utf-8")


def _leggi_sito(sito: dict, filtro: Filtro) -> list[Opportunita]:
    sess = _sessione(sito)
    attesa = (float(sito.get("timeout_connessione", 60)), 90)
    r = sess.get(sito["url"], timeout=attesa, allow_redirects=True)
    pagina = r.content.decode(r.encoding or "utf-8", errors="replace")
    if r.status_code >= 400:
        _salva_errore(sito, pagina)
        r.raise_for_status()
    try:
        righe, totale = leggi_elenco(pagina)
    except RuntimeError:
        _salva_errore(sito, pagina)
        raise
    if not righe:
        if totale == 0:
            _salva_campione(sito, [], 0)
            log.info("Jaggaer %s: nessuna voce in corso", sito["nome"])
            return []
        _salva_errore(sito, pagina)
        if re.search(r"browserNotSupported|aggiornare\s+il\s+browser", pagina, re.I):
            raise RuntimeError("il portale ha risposto con la pagina 'browser non supportato' "
                               f"(vedi state/campioni/{sito['sigla']}_errore.html)")
        raise RuntimeError(f"nessuna riga nell'elenco (vedi state/campioni/{sito['sigla']}_errore.html)")
    _salva_campione(sito, righe, totale)
    if totale is not None and len(righe) < totale:
        # L'elenco e' oggi in una sola pagina; se il portale iniziasse a paginare, va detto.
        ERRORI.append(f"{sito['nome']}: lette {len(righe)} righe su {totale} dichiarate "
                      f"(paginazione non gestita: dati parziali)")
    trovate: dict[str, Opportunita] = {}
    escluse = 0
    for riga in righe:
        o, esclusa = converti(riga, sito, filtro)
        if esclusa:
            escluse += 1
            if riga.get("id"):
                ESCLUSI.add(f"{sito['sigla']}:{riga['id']}")
        elif o:
            trovate[o.id] = o
    log.info("Jaggaer %s: %d voci lette (su %s dichiarate), %d escluse da regole esplicite, %d pertinenti",
             sito["nome"], len(righe), totale if totale is not None else "?", escluse, len(trovate))
    return list(trovate.values())


def raccogli(cfg: dict, filtro: Filtro, sess=None) -> list[Opportunita]:
    risultati: list[Opportunita] = []
    errori: list[str] = []
    siti = [s for s in (cfg.get("jaggaer") or {}).get("siti", []) if s.get("abilitato", True)]
    for sito in siti:
        try:
            risultati.extend(_leggi_sito(sito, filtro))
        except Exception as e:
            log.exception("Jaggaer %s: errore", sito["nome"])
            errori.append(f"{sito['nome']}: {e}")
    if errori and len(errori) == len(siti):
        raise RuntimeError("; ".join(errori))
    ERRORI.extend(errori)
    return risultati
