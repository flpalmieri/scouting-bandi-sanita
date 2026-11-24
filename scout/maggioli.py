"""Piattaforme di e-procurement Maggioli "Appalti & Contratti" (PortaleAppalti).

Primo uso: Marche (23/11/2026). Tutte le piattaforme sanitarie marchigiane sono istanze di
PortaleAppalti, ciascuna con il suo indirizzo ma con gli stessi percorsi: GT-SUAM della Regione
(appaltisuam.regione.marche.it, multi-ente), le quattro AST con piattaforma propria
(appalti-ast2an/ast3mc/ast4fm/ast5ap.sanita.marche.it), GTAOMN (AST Pesaro Urbino, ex AO Marche
Nord), GTAOAN (AOU delle Marche) e GTMULTIE (INRCA e ARS, insieme a molti comuni).

Due elenchi pubblici per istanza, letti con un semplice GET (nessun login, nessun cookie, nessun
token: verificato dall'utente con lo User-Agent del progetto, che riceve 200 come quello di
browser):
    gare in corso:
        <host>/PortaleAppalti/it/ppgare_bandi_lista.wp?actionPath=/ExtStr2/do/FrontEnd/Bandi/
        listAllBandi.action&currentFrame=7&model.stato=1&model.iDisplayLength=100
    avvisi pubblici in corso (indagini di mercato, consultazioni preliminari, infungibilita'):
        <host>/PortaleAppalti/it/ppgare_avvisi_lista.wp?actionPath=/ExtStr2/do/FrontEnd/Avvisi/
        listAllAvvisi.action&currentFrame=7&model.stato=1&model.iDisplayLength=100
    con, facoltativo, &model.stazioneAppaltante=<codice> per le istanze multi-ente.

L'HTML e' generato dal server: ogni voce e' un <div class="list-item"> con righe
"<label>Campo : </label> valore" (Stazione appaltante, Titolo, Tipologia appalto / Tipologia,
Avviso per, Importo, Data pubblicazione, Data scadenza "gg/mm/aaaa entro le hh:mm", Riferimento
procedura Gxxxxx / Axxxxx, Stato) e un link "Visualizza scheda" con apici singoli. Il riepilogo
"La ricerca ha restituito N risultati." dice quante voci ci sono.

Le schede delle gare (Bandi/view.action) sono protette da un captcha (Friendly Captcha): non si
leggono e non si aggira il captcha. Dall'elenco ci sono gia' i campi necessari; il link della
dashboard apre la scheda nel browser di una persona. Niente CPV ne' CIG nell'elenco: filtro
testuale, come per le altre fonti senza CPV.

Controlli: se il server ignora un filtro della richiesta (stato "In corso", stazione appaltante)
la pagina non lo mostra selezionato e la lista si scarta con un errore, invece di leggere tutto
l'archivio; se il riepilogo dichiara piu' voci di quelle lette (oltre 100) si segnala "dati
parziali"; una pagina di verifica (captcha) al posto dell'elenco e' un errore riconoscibile.

Dati personali: l'elenco non ne contiene (solo nomi di enti e uffici). Il campione in
state/campioni/ contiene solo i campi letti; l'HTML grezzo si salva solo se la pagina non e'
leggibile.

Per un'altra istanza basta un blocco in config.yaml (sezione maggioli.siti) con l'host.
"""
from __future__ import annotations

import html
import json
import re
from datetime import date, timedelta
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .common import Filtro, Opportunita, log, parse_data, parse_importo, sessione_http

CAMPIONI = Path("state/campioni")
ERRORI: list[str] = []
ESCLUSI: set[str] = set()   # id scartati da una regola esplicita: main.py li toglie dallo storico

PERCORSI = {
    "bandi": "/PortaleAppalti/it/ppgare_bandi_lista.wp?actionPath=/ExtStr2/do/FrontEnd/Bandi/"
             "listAllBandi.action&currentFrame=7&model.stato=1&model.iDisplayLength=100",
    "avvisi": "/PortaleAppalti/it/ppgare_avvisi_lista.wp?actionPath=/ExtStr2/do/FrontEnd/Avvisi/"
              "listAllAvvisi.action&currentFrame=7&model.stato=1&model.iDisplayLength=100",
}

RE_TAG = re.compile(r"<[^>]+>")
RE_VOCE = re.compile(r'<div\s+class="list-item"\s*>')
RE_RIGA = re.compile(r'<div\s+class="list-item-row"\s*>\s*<label>(.*?)</label>(.*?)</div>', re.S | re.I)
RE_LINK = re.compile(r"""href\s*=\s*(['"])([^'"]*view\.action[^'"]*)\1""", re.I)
RE_CODICE = re.compile(r"[?&]codice=([A-Za-z0-9]+)")
RE_TOTALE = re.compile(r'class="list-summary"\s*>\s*La\s+ricerca\s+ha\s+restituito\s+([\d.]+)\s+risultat', re.I)
class SitoIrraggiungibile(RuntimeError):
    """Connessione al sito non riuscita (timeout o rifiuto): niente traceback nel log."""


RE_CAPTCHA = re.compile(r"frc-captcha|allowCaptcha|friendlycaptcha", re.I)

# Etichette dell'elenco (minuscole, senza ":") -> nome interno.
CAMPI = {
    "stazione appaltante": "ente",
    "titolo": "titolo",
    "tipologia appalto": "tipologia",
    "tipologia": "tipologia",
    "avviso per": "avviso_per",
    "importo": "importo",
    "data pubblicazione": "pubblicazione",
    "data scadenza": "scadenza",
    "riferimento procedura": "codice",
    "stato": "stato",
}


def _testo(frammento: str) -> str:
    return " ".join(html.unescape(RE_TAG.sub(" ", frammento)).split())


def _selezionato(pagina: str, nome: str) -> str | None:
    """Valore dell'opzione selezionata nella <select name="nome"> del modulo di ricerca."""
    m = re.search(r'<select\b[^>]*name="' + re.escape(nome) + r'"[^>]*>(.*?)</select>', pagina, re.S | re.I)
    if not m:
        return None
    s = re.search(r'<option\b[^>]*value="([^"]*)"[^>]*selected', m.group(1), re.I)
    return s.group(1) if s else ""


def leggi_elenco(pagina: str) -> tuple[list[dict], int | None]:
    """Voci dell'elenco (solo i campi noti) e totale dichiarato dal riepilogo."""
    mt = RE_TOTALE.search(pagina)
    totale = int(mt.group(1).replace(".", "")) if mt else None
    voci = []
    for blocco in RE_VOCE.split(pagina)[1:]:
        voce: dict[str, str] = {}
        for etichetta, valore in RE_RIGA.findall(blocco):
            nome = _testo(etichetta).rstrip(":").strip().lower()
            if nome in CAMPI:
                voce[CAMPI[nome]] = _testo(valore)
        ml = RE_LINK.search(blocco)
        if ml:
            voce["link"] = html.unescape(ml.group(2)).strip()
            mc = RE_CODICE.search(voce["link"])
            if mc and not voce.get("codice"):
                voce["codice"] = mc.group(1)
        if voce.get("titolo"):
            voci.append(voce)
    return voci, totale


def _data(valore: str) -> str | None:
    d = parse_data(valore or "")
    if d and not ("1971" <= d[:4] <= "2100"):
        return None
    return d


def _tipo(lista: str, titolo: str) -> str:
    t = titolo.lower()
    if "consultazion" in t:
        return "consultazione di mercato"
    if lista == "avvisi" or re.search(r"\bavviso\b|indagine\s+(conoscitiva\s+)?di\s+mercato|manifestazion", t):
        # La tipologia "Avviso di preinformazione" di PortaleAppalti e' usata anche per le
        # indagini di mercato (AST Ascoli): non diventa "Preinformazione".
        return "avviso esplorativo"
    return "bando"


def _regex(lista) -> re.Pattern | None:
    return re.compile("|".join(f"(?:{e})" for e in lista), re.I) if lista else None


def converti(voce: dict, lista: str, sito: dict, filtro: Filtro,
             oggi: date | None = None) -> tuple[Opportunita | None, bool]:
    """(opportunita o None, esclusa da una regola esplicita)."""
    oggi = oggi or date.today()
    titolo = voce.get("titolo", "")
    tipologia = voce.get("tipologia", "")
    if tipologia.lower() in {t.lower() for t in sito.get("escludi_tipologie", [])}:
        return None, True
    escl = _regex(sito.get("escludi_titoli_regex"))
    if escl and escl.search(titolo):
        return None, True
    stazione = voce.get("ente", "").strip()
    escl_staz = _regex(sito.get("escludi_stazioni_regex"))
    if escl_staz and escl_staz.search(stazione):
        return None, True
    scadenza = _data(voce.get("scadenza", ""))
    pubblicazione = _data(voce.get("pubblicazione", ""))
    # Gli avvisi senza scadenza restano "In corso" per anni (GT-SUAM: avvisi del 2021): senza
    # scadenza si tengono solo se pubblicati da poco, come negli albi sardi.
    massimo = int(sito.get("pubblicazione_max_giorni", 120))
    if not scadenza and pubblicazione and pubblicazione < (oggi - timedelta(days=massimo)).isoformat():
        return None, True
    if scadenza and pubblicazione and scadenza < pubblicazione:
        log.warning("Maggioli %s: %s ha la scadenza (%s) prima della pubblicazione (%s): scadenza ignorata",
                    sito["nome"], voce.get("codice"), scadenza, pubblicazione)
        scadenza = None
    mappa = {k.strip().lower(): v for k, v in (sito.get("enti_rinomina") or {}).items()}
    ente = mappa.get(stazione.lower(), stazione) or sito.get("ente_se_vuoto", "")
    parti = [f"Tipologia: {tipologia}" if tipologia else "",
             f"Avviso per: {voce['avviso_per']}" if voce.get("avviso_per") else "",
             f"Riferimento: {voce['codice']}" if voce.get("codice") else ""]
    descrizione = "; ".join(p for p in parti if p)
    motivo = filtro.valuta_testo(titolo, ente, descrizione)
    if not motivo:
        return None, False
    codice = voce.get("codice") or re.sub(r"[^a-z0-9]+", "-", titolo.lower())[:80]
    return Opportunita(
        id=f"{sito['sigla']}:{codice}",
        fonti=[sito["nome_fonte"]],
        titolo=titolo,
        ente=ente,
        scadenza=scadenza,
        pubblicazione=pubblicazione,
        importo=parse_importo(voce.get("importo")) or None,
        tipo=_tipo(lista, titolo),
        link=voce.get("link") or sito["host"],
        regione=sito.get("regione"),
        motivo_match=motivo,
        descrizione=descrizione or None,
    ), False


def _sessione(sito: dict) -> requests.Session:
    """Sessione con lo User-Agent dichiarato del progetto e pochi tentativi."""
    s = sessione_http()
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=int(sito.get("tentativi", 2)), backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET"}))))
    return s


def _nome_file(sito: dict, lista: str, stazione: str | None) -> str:
    return f"{sito['sigla']}_{lista}" + (f"_{stazione}" if stazione else "")


def _salva_campione(nome: str, voci: list[dict], totale: int | None) -> None:
    CAMPIONI.mkdir(parents=True, exist_ok=True)
    (CAMPIONI / f"{nome}.json").write_text(
        json.dumps({"totale_dichiarato": totale, "voci": voci}, ensure_ascii=False, indent=1),
        encoding="utf-8")


def _salva_errore(nome: str, contenuto: str) -> None:
    CAMPIONI.mkdir(parents=True, exist_ok=True)
    i = contenuto.find("portgare-list")
    (CAMPIONI / f"{nome}_errore.html").write_text(contenuto[max(0, i - 500):][:60000], encoding="utf-8")


def _leggi_lista(sito: dict, lista: str, stazione: str | None, sess, filtro: Filtro) -> list[Opportunita]:
    url = sito["host"].rstrip("/") + PERCORSI[lista]
    if stazione:
        url += f"&model.stazioneAppaltante={stazione}"
    nome = _nome_file(sito, lista, stazione)
    etichetta = f"{sito['nome']} {lista}" + (f" (stazione {stazione})" if stazione else "")
    attesa = (float(sito.get("timeout_connessione", 60)), 90)
    r = sess.get(url, timeout=attesa)
    pagina = r.content.decode(r.encoding or "utf-8", errors="replace")
    if r.status_code >= 400:
        _salva_errore(nome, pagina)
        r.raise_for_status()
    if "portgare-list" not in pagina:
        _salva_errore(nome, pagina)
        if RE_CAPTCHA.search(pagina):
            raise RuntimeError(f"{etichetta}: al posto dell'elenco una pagina di verifica (captcha), "
                               f"vedi state/campioni/{nome}_errore.html")
        raise RuntimeError(f"{etichetta}: la pagina non contiene l'elenco, vedi state/campioni/{nome}_errore.html")
    # Filtri della richiesta: se il server li ignorasse, si leggerebbe l'archivio intero.
    if _selezionato(pagina, "model.stato") != "1":
        _salva_errore(nome, pagina)
        raise RuntimeError(f"{etichetta}: il filtro 'In corso' non risulta applicato, lista scartata")
    if stazione and _selezionato(pagina, "model.stazioneAppaltante") != stazione:
        _salva_errore(nome, pagina)
        raise RuntimeError(f"{etichetta}: il filtro sulla stazione appaltante non risulta applicato, "
                           "lista scartata (togliere 'stazioni' dal blocco per leggere tutta la piattaforma)")
    voci, totale = leggi_elenco(pagina)
    if not voci and totale not in (None, 0):
        _salva_errore(nome, pagina)
        raise RuntimeError(f"{etichetta}: dichiarate {totale} voci ma nessuna letta (struttura cambiata?)")
    _salva_campione(nome, voci, totale)
    if totale is not None and len(voci) < totale:
        ERRORI.append(f"{etichetta}: lette {len(voci)} voci su {totale} dichiarate "
                      "(oltre 100 per pagina, paginazione non gestita: dati parziali)")
    trovate: dict[str, Opportunita] = {}
    escluse = 0
    for voce in voci:
        if not voce.get("stato", "In corso").lower().startswith("in corso"):
            continue
        o, esclusa = converti(voce, lista, sito, filtro)
        if esclusa:
            escluse += 1
            if voce.get("codice"):
                ESCLUSI.add(f"{sito['sigla']}:{voce['codice']}")
        elif o:
            trovate[o.id] = o
    log.info("Maggioli %s: %d voci lette (su %s dichiarate), %d escluse da regole esplicite, %d pertinenti",
             etichetta, len(voci), totale if totale is not None else "?", escluse, len(trovate))
    return list(trovate.values())


def _leggi_sito(sito: dict, filtro: Filtro) -> list[Opportunita]:
    sess = _sessione(sito)
    # Siti che non mandano il certificato intermedio (Sicilia, 23/11/2026: ARNAS Civico, AO Papardo):
    # lo si scarica o lo si prende da un file del repository e si verifica la catena (scout/tls.py).
    # La verifica TLS resta sempre attiva.
    if sito.get("completa_catena_tls"):
        from .tls import completa_catena
        sess.verify = completa_catena(sito["host"], intermedi_extra=sito.get("intermedi_extra"))
    risultati: list[Opportunita] = []
    errori: list[str] = []
    richieste = [(lista, staz) for lista in sito.get("liste", ["bandi", "avvisi"])
                 for staz in (sito.get("stazioni") or [None])]
    for lista, staz in richieste:
        try:
            risultati.extend(_leggi_lista(sito, lista, staz, sess, filtro))
        except requests.ConnectionError as e:
            # Connessione non riuscita (primo lancio del 23/11/2026, run 45189892369: ConnectTimeout su
            # tutte le 8 piattaforme da GitHub, mentre dal computer dell'utente rispondono). Inutile
            # provare le altre liste dello stesso host: si perde solo tempo. Un timeout di connessione
            # avviene prima che il server veda lo User-Agent: non dipende da come ci si presenta.
            log.warning("Maggioli %s: connessione non riuscita (%s): altre liste del sito non provate",
                        sito["nome"], type(e).__name__)
            raise SitoIrraggiungibile(f"{sito['nome']}: connessione non riuscita ({type(e).__name__}), il sito non "
                               "risponde ai server di GitHub; altre liste del sito non provate") from e
        except Exception as e:
            # Una lista in errore non fa perdere le altre dello stesso sito.
            log.exception("Maggioli %s %s: errore", sito["nome"], lista)
            testo = str(e)
            errori.append(testo if testo.startswith(sito["nome"]) else f"{sito['nome']} {lista}: {testo}")
    if errori and len(errori) == len(richieste):
        raise RuntimeError("; ".join(errori))
    ERRORI.extend(f"dati parziali: {e}" for e in errori)
    return risultati


def raccogli(cfg: dict, filtro: Filtro, sess=None) -> list[Opportunita]:
    risultati: list[Opportunita] = []
    errori: list[str] = []
    siti = [s for s in (cfg.get("maggioli") or {}).get("siti", []) if s.get("abilitato", True)]
    for sito in siti:
        try:
            risultati.extend(_leggi_sito(sito, filtro))
        except SitoIrraggiungibile as e:
            errori.append(str(e))
        except Exception as e:
            log.exception("Maggioli %s: errore", sito["nome"])
            errori.append(f"{sito['nome']}: {e}")
    if errori and len(errori) == len(siti):
        raise RuntimeError("; ".join(errori))
    ERRORI.extend(errori)
    return risultati
