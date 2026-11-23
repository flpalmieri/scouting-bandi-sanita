"""Raccolta da siti che pubblicano i bandi solo come pagine HTML.

Ogni sito si configura in config.yaml (sezione portali_html) indicando quale
"lettore" usare. I lettori sono funzioni che ricevono l'HTML di una pagina e
restituiscono un elenco di dizionari {titolo, link, testo}.

Lettori disponibili:
  scheda_sito    - elenchi WordPress / Bootstrap Italia con schede "scheda-sito"
                   (es. USL Umbria 1, AO Terni): titolo nel link dell'h6, date nel
                   blocco "data" (DATA PUBBLICAZIONE, DATA SCADENZA o SCADENZA).
  tabella        - tabelle con intestazioni oggetto (o "bando")/scadenza/tipologia/importo/
                   CIG/data pubblicazione (USL Umbria 2, portali PAT di ISWEB come l'IZSUM,
                   AreaCom Abruzzo su Drupal).
  box_etichette  - riquadri C2I MediaCenter con etichette (AO Perugia).
  teaser_drupal  - schede Drupal <article class="node-teaser"> con stato (field-workflow),
                   "Pubblicato:" e "Scade:" (portale bandi ASL TO5). Il referente (nome,
                   telefono, email) non viene letto.
  publiccompetitions - elenchi del componente Joomla com_publiccompetitions (siti sanitari
                   liguri: ATS Liguria, ASL 3, IRCCS AOM, Gaslini): voci div.pc_latest_item
                   con link "bando_link", "Data pubblicazione" e "Data chiusura"; l'area che
                   pubblica (ATS Liguria) si usa con l'opzione enti_per_area. Si legge solo
                   la prima pagina (la paginazione del sito funziona solo in POST): gli elenchi
                   sono ordinati dal piu' recente e bastano per un'esecuzione al giorno.
  it_card        - schede Bootstrap Italia <article class="it-card"> con titolo nell'h3,
                   "Codice", "Inizio", "Scadenza" ed etichette di stato e tipo (ULSS 6 Euganea).
  albo_ap        - albo pretorio WordPress + Bootstrap Italia delle aziende sanitarie sarde
                   (ARES, ASL 1-8, AOU Sassari, AREUS: stesso tema, tipo di contenuto "ap"),
                   sezione /albo-pretorio/bandi-e-gare/: schede con titolo e link nell'h5
                   "card-title" e coppie <strong>Etichetta</strong> valore (Stato, Data di
                   Pubblicazione, Data e ora di scadenza, Esito pubblicato). Si legge la prima pagina
                   (6-9 schede, dalla piu' recente): basta per un'esecuzione al giorno.

Opzioni per sito (config.yaml), oltre a nome, sigla, ente, regione, lettore, url:
  escludi_titoli_regex  - elenco di espressioni: le schede il cui titolo corrisponde
                          vengono scartate prima dei filtri (es. delibere e determine
                          di aggiudicazione, che non sono opportunita').
  pagina_iniziale       - numero della prima pagina in {pagina} (default 1; 0 per Drupal).
  url_alternativo       - indirizzo da usare se il principale risponde con un errore
                          (es. 403 dai server di GitHub); stesso formato di url.
  timeout_connessione   - secondi di attesa per la connessione (default 60). Utile per i siti
                          che scartano in silenzio le connessioni dai server di GitHub.
  escludi_stati         - stati da scartare (per i lettori che leggono lo stato: teaser_drupal,
                          it_card), es. [Scaduto, Aggiudicato, Archiviato].
  completa_catena_tls   - true per i siti che non inviano il certificato intermedio (errore
                          "unable to get local issuer certificate"): vedi scout/tls.py.
  tentativi             - nuovi tentativi in caso di errore (default: quelli della sessione
                          comune, 5). Con un sito bloccato, pochi tentativi = poco tempo perso.
  enti_per_area         - (lettore publiccompetitions) per gli elenchi con voci di piu' enti
                          o aree (ATS Liguria): tabella "area come scritta sul sito" -> nome
                          dell'ente. Usare per un'area lo stesso nome dell'ente di un altro
                          sito che pubblica gli stessi avvisi, cosi' la deduplica li fonde.
                          Un'area assente dalla tabella diventa "<ente> - <area>".
  scadenza_max_giorni   - se la "scadenza" e' piu' lontana di cosi' dalla pubblicazione
                          (default 365) non e' un termine di gara ma la fine della
                          pubblicazione sul sito: viene ignorata.
  pubblicazione_max_giorni - le voci SENZA scadenza pubblicate da piu' di cosi' si ignorano
                          (default 120 per il lettore albo_ap, nessun limite per gli altri). Senza
                          scadenza una voce resta in dashboard 120 giorni dal primo rilevamento: un
                          avviso dell'agosto 2025 trovato oggi sarebbe rimasto fino a marzo 2027
                          (ASL 3 Nuoro, telemonitoraggio BPCO, run 44738107918).
  user_agent            - (23/11/2026) "progetto": il sito riceve lo User-Agent dichiarato del
                          progetto (scouting-bandi-sanita/1.0, quello di common.sessione_http) invece
                          di quello di un browser. I portali PAT di ISWEB rispondono 403 a ogni
                          User-Agent che inizia con "Mozilla/5.0" e 200 a quello del progetto
                          (ricognizione del 23/11/2026, ASL 1 Abruzzo e IZSUM). Non si accettano altri
                          valori: ci si presenta per quello che si e', senza fingersi un altro programma.
  Una "scadenza" anteriore alla pubblicazione e' un errore di battitura del sito (ARES,
  20/11/2026: pubblicato 11.11.2026, scadenza 11.01.2026) e viene ignorata.
"""
from __future__ import annotations

import hashlib
import html
import re
import time
from datetime import date

import requests
from pathlib import Path
from urllib.parse import urljoin

from .common import Filtro, Opportunita, estrai_importo, log, parse_data

CAMPIONI = Path("state/campioni")
ERRORI: list[str] = []
# Id delle voci ignorate da una regola che lo storico da solo non conosce (voci vecchie senza
# scadenza; dal 23/11/2026 anche i titoli esclusi da escludi_titoli_regex): main.py le toglie
# anche dallo storico.
ESCLUSI: set[str] = set()
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9",
}

RE_TAG = re.compile(r"<[^>]+>")
RE_SPAZI = re.compile(r"[ \t\r\f\v\xa0]+")
RE_DATA_TESTO = r"(\d{1,2}[./-]\d{1,2}[./-]\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}\s+[a-zA-Z]+\s+\d{4})"
RE_PUBBLICAZIONE = re.compile(r"data\s+pubblicazione\s*:?\s*" + RE_DATA_TESTO, re.IGNORECASE)
RE_SCADENZA_ETICHETTA = re.compile(r"data\s+scadenza\s*:?\s*(.{0,160})", re.IGNORECASE)
RE_SCADENZA_TESTO = re.compile(
    r"(?:scadenz\w*|termine\s+(?:ultimo|di\s+presentazione)\w*)[^0-9\n]{0,80}" + RE_DATA_TESTO,
    re.IGNORECASE)
RE_CIG = re.compile(r"\bCIG\b\W{0,5}([0-9A-Z]{10})\b", re.IGNORECASE)


def _testo(frammento: str) -> str:
    t = re.sub(r"<\s*br\s*/?>|</p>|</div>|</li>", "\n", frammento or "", flags=re.IGNORECASE)
    t = html.unescape(RE_TAG.sub(" ", t))
    righe = [RE_SPAZI.sub(" ", r).strip() for r in t.split("\n")]
    return "\n".join(r for r in righe if r)


# ------------------------------------------------------------------ lettori

def lettore_scheda_sito(pagina: str, url_base: str) -> list[dict]:
    voci = []
    blocchi = re.split(r'<div[^>]+class="[^"]*\bscheda-sito\b[^"]*"', pagina)[1:]
    for b in blocchi:
        m = re.search(r"<h\d[^>]*>\s*<a([^>]*)>(.*?)</a>", b, re.DOTALL | re.IGNORECASE)
        if not m:
            continue
        attributi, interno = m.group(1), m.group(2)
        href = re.search(r'href="([^"]+)"', attributi)
        titolo_attr = re.search(r'title="([^"]*)"', attributi)
        titolo = html.unescape(titolo_attr.group(1)) if titolo_attr else _testo(interno)
        d = re.search(r'<div[^>]+class="[^"]*\bdata\b[^"]*"[^>]*>(.*?)</div>', b, re.DOTALL | re.IGNORECASE)
        voci.append({
            "titolo": RE_SPAZI.sub(" ", titolo).strip(),
            "link": urljoin(url_base, href.group(1)) if href else None,
            "testo": _testo(d.group(1)) if d else "",
        })
    return voci


def _titolo_da_link(href: str) -> str:
    slug = href.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[-_]+", " ", slug).strip().capitalize()


def lettore_tabella(pagina: str, url_base: str) -> list[dict]:
    """Elenchi in forma di tabella (es. USL Umbria 2): le colonne vengono
    riconosciute dalle intestazioni (oggetto, scadenza, tipologia, importo)."""
    voci = []
    for tabella in re.findall(r"<table[^>]*>(.*?)</table>", pagina, re.DOTALL | re.IGNORECASE):
        righe = re.findall(r"<tr[^>]*>(.*?)</tr>", tabella, re.DOTALL | re.IGNORECASE)
        if not righe:
            continue
        intestazioni = [_testo(h).lower() for h in re.findall(r"<th[^>]*>(.*?)</th>", righe[0], re.DOTALL | re.IGNORECASE)]
        # colonna del titolo: "Oggetto" (USL Umbria 2, PAT) oppure "Bando" (AreaCom, Drupal)
        if not any("oggetto" in h or h in ("bando", "titolo") for h in intestazioni):
            continue

        def colonna(*parole):
            for i, h in enumerate(intestazioni):
                if any(p in h for p in parole):
                    return i
            return None

        c_ogg, c_scad = colonna("oggetto"), colonna("scadenza")
        if c_ogg is None:
            c_ogg = next(i for i, h in enumerate(intestazioni) if h in ("bando", "titolo"))
        c_cig = colonna("cig")
        c_tipo, c_imp = colonna("tipolog", "tipo"), colonna("importo")
        c_pubb = colonna("pubblicazion", "data avviso")   # es. portali PAT/ISWEB
        for riga in righe[1:]:
            celle = re.findall(r"<td[^>]*>(.*?)</td>", riga, re.DOTALL | re.IGNORECASE)
            if not celle:
                continue
            cella = lambda i: _testo(celle[i]).replace("\n", " ") if i is not None and i < len(celle) else ""
            href = re.search(r'<a[^>]+href="([^"]+)"', riga)
            link = urljoin(url_base, html.unescape(href.group(1))) if href else None
            titolo = cella(c_ogg) or (_titolo_da_link(link) if link else "")
            voci.append({
                "titolo": RE_SPAZI.sub(" ", titolo).strip(),
                "link": link,
                "testo": (f"DATA PUBBLICAZIONE: {cella(c_pubb)}\n" if c_pubb is not None else "")
                         + f"DATA SCADENZA: {cella(c_scad)}\nTIPOLOGIA: {cella(c_tipo)}\nIMPORTO: {cella(c_imp)}"
                         + (f"\nCIG: {cella(c_cig)}" if c_cig is not None else ""),
                "scadenza_grezza": cella(c_scad),
                "tipo_grezzo": cella(c_tipo),
                "importo_grezzo": cella(c_imp),
            })
    return voci


def lettore_box_etichette(pagina: str, url_base: str) -> list[dict]:
    """Elenchi a riquadri con etichette (C2I MediaCenter, es. AO Perugia):
    titolo in span.title > a, coppie "Etichetta / valore" in span.label-master."""
    voci = []
    for b in re.split(r'<div[^>]+class="[^"]*\bbox-content\b[^"]*"', pagina)[1:]:
        m = re.search(r'<span[^>]+class="[^"]*\btitle\b[^"]*"[^>]*>\s*<a([^>]*)>(.*?)</a>',
                      b, re.DOTALL | re.IGNORECASE)
        if not m:
            continue
        href = re.search(r'href="([^"]+)"', m.group(1))
        coppie = re.findall(
            r'<span[^>]+class="[^"]*label-master[^"]*"[^>]*>\s*<strong>(.*?)</strong>\s*</span>(.*?)</span>',
            b, re.DOTALL | re.IGNORECASE)
        campi = {_testo(k).lower(): _testo(v) for k, v in coppie}
        scad = next((v for k, v in campi.items() if "scadenz" in k), "")
        pubb = next((v for k, v in campi.items() if "pubblicazion" in k), "")
        voci.append({
            "titolo": RE_SPAZI.sub(" ", _testo(m.group(2)).replace("\n", " ")).strip(),
            "link": urljoin(url_base, html.unescape(href.group(1))) if href else None,
            "testo": f"DATA PUBBLICAZIONE: {pubb}\nDATA SCADENZA: {scad}",
            "scadenza_grezza": scad,
        })
    return voci


def lettore_it_card(pagina: str, url_base: str) -> list[dict]:
    """Schede <article class="it-card"> (es. www.aulss6.veneto.it/bandi-di-gara): titolo e link
    nell'h3 "it-card-title", poi "Codice:", "Inizio:", "Scadenza:" ed etichette (badge) con
    stato (Pubblicato, Aggiudicato...) e tipo (Avviso, Bando, Asta, Adesione gara centralizzata)."""
    voci = []
    blocchi = re.split(r'<article\b[^>]*class="[^"]*\bit-card\b[^"]*"', pagina)[1:]
    for b in blocchi:
        b = b.split("</article>", 1)[0]
        m = re.search(r'<h\d[^>]*it-card-title[^>]*>\s*<a([^>]*)>(.*?)</a>', b, re.DOTALL | re.IGNORECASE)
        if not m:
            continue
        href = re.search(r'href="([^"]+)"', m.group(1))
        testo = _testo(b[m.end():])
        etichette = [_testo(x) for x in re.findall(r'<span[^>]*class="[^"]*\bbadge\b[^"]*"[^>]*>(.*?)</span>', b, re.DOTALL)]
        inizio = re.search(r"Inizio\s*:\s*" + RE_DATA_TESTO, testo, re.IGNORECASE)
        fine = re.search(r"Scadenza\s*:\s*" + RE_DATA_TESTO, testo, re.IGNORECASE)
        voci.append({
            "titolo": RE_SPAZI.sub(" ", _testo(m.group(2))).strip(),
            "link": urljoin(url_base, html.unescape(href.group(1))) if href else None,
            "testo": testo,
            "pubblicazione_grezza": inizio.group(1) if inizio else None,
            "scadenza_grezza": fine.group(1) if fine else None,
            "tipo_grezzo": etichette[-1] if etichette else "",
            "stato_grezzo": etichette[0] if len(etichette) > 1 else "",
        })
    return voci


def lettore_teaser_drupal(pagina: str, url_base: str) -> list[dict]:
    """Schede <article class="... node-teaser ..."> di Drupal (es. bandi.aslto5.piemonte.it):
    titolo e link nell'h2, stato nel blocco "field-workflow", date nel blocco "field-pubblicazione"
    ("Pubblicato: 16/11/2026" / "Scade: 11/12/2026 - 18:00"). Il blocco "field-referente"
    contiene dati personali e viene ignorato."""
    voci = []
    for b in re.split(r'<article\b(?=[^>]*\bnode-teaser\b)', pagina)[1:]:
        b = b.split("</article>", 1)[0]
        m = re.search(r'<h2\b.*?<a([^>]*)>(.*?)</a>', b, re.DOTALL | re.IGNORECASE)
        if not m:
            continue
        href = re.search(r'href="([^"]+)"', m.group(1))
        stato = re.search(r'field-workflow[^"]*"[^>]*>(.*?)(?:<span|</div>)', b, re.DOTALL)
        date_ = re.search(r'field-pubblicazione[^"]*"[^>]*>(.*?)</div>', b, re.DOTALL)
        stato_t = _testo(stato.group(1)) if stato else ""
        date_t = _testo(date_.group(1)) if date_ else ""
        pubbl = re.search(r"Pubblicato\s*:?\s*" + RE_DATA_TESTO, date_t, re.IGNORECASE)
        scade = re.search(r"Scade\w*\s*:?\s*" + RE_DATA_TESTO, date_t, re.IGNORECASE)
        voci.append({
            "titolo": RE_SPAZI.sub(" ", _testo(m.group(2))).strip(),
            "link": urljoin(url_base, html.unescape(href.group(1))) if href else None,
            "testo": f"Stato: {stato_t}. {date_t}".strip(),     # niente referente
            "pubblicazione_grezza": pubbl.group(1) if pubbl else None,
            "scadenza_grezza": scade.group(1) if scade else None,
            "tipo_grezzo": "",
            "stato_grezzo": stato_t,
        })
    return voci


def lettore_publiccompetitions(pagina: str, url_base: str) -> list[dict]:
    """Voci <div class='row pc_latest_item ...'> di com_publiccompetitions: titolo nel link
    a.bando_link dentro l'h3, date negli attributi title dei blocchi pc_latest_item_apertura
    ("Data pubblicazione: 26 Ottobre 2026") e pc_latest_item_chiusura ("Data chiusura: ...")."""
    voci = []
    for b in re.split(r"<div\s+class=['\"](?=[^'\"]*\bpc_latest_item\b(?![_-]))[^'\"]*['\"]", pagina)[1:]:
        m = re.search(r"<a\b(?=[^>]*\bbando_link\b)([^>]*)>(.*?)</a>", b, re.DOTALL | re.IGNORECASE)
        if not m:
            continue
        href = re.search(r'href=["\']([^"\']+)["\']', m.group(1))
        ap = re.search(r"pc_latest_item_apertura[^>]*title=['\"]([^'\"]*)['\"]", b)
        ch = re.search(r"pc_latest_item_chiusura[^>]*title=['\"]([^'\"]*)['\"]", b)
        apertura, chiusura = (ap.group(1) if ap else ""), (ch.group(1) if ch else "")
        # Negli elenchi multi-ente (ATS Liguria) un secondo blocco pc_latest_item_bando_titolo,
        # senza h3, riporta l'area che pubblica ("Area sociosanitaria locale 3"). Lo si usa
        # solo se il sito ha enti_per_area in config (altrove quel blocco contiene altro).
        area = ""
        for blocco in re.findall(r"<div\s+class=['\"][^'\"]*\bpc_latest_item_bando_titolo\b[^'\"]*['\"][^>]*>"
                                 r"((?:(?!</div>).)*)</div>", b, re.DOTALL | re.IGNORECASE):
            if "<h3" not in blocco.lower() and "bando_link" not in blocco:
                area = RE_SPAZI.sub(" ", _testo(blocco)).strip()
                break
        voci.append({
            "titolo": RE_SPAZI.sub(" ", _testo(m.group(2))).strip(),
            "link": urljoin(url_base, html.unescape(href.group(1))) if href else None,
            "testo": " ".join(x for x in (apertura, chiusura) if x),
            "pubblicazione_grezza": apertura.split(":", 1)[-1].strip() or None,
            "scadenza_grezza": chiusura.split(":", 1)[-1].strip() or None,
            "tipo_grezzo": "",
            "stato_grezzo": "",
            "area": area,
        })
    return voci


def lettore_albo_ap(pagina: str, url_base: str) -> list[dict]:
    """Albo pretorio WordPress + Bootstrap Italia (aziende sanitarie sarde, tipo di contenuto "ap"):
    ogni scheda ha il titolo nel link dell'h5 "card-title" e righe "<strong>Etichetta </strong> valore"
    (Stato, Sezione, Data di Pubblicazione, Data e ora di scadenza, Data ultimo aggiornamento,
    Esito pubblicato). Si leggono solo stato e date: nessun dato personale nella scheda."""
    voci = []
    blocchi = re.split(r'<h\d[^>]*class="[^"]*\bcard-title\b[^"]*"[^>]*>', pagina)[1:]
    for b in blocchi:
        m = re.search(r"<a([^>]*)>(.*?)</a>", b, re.DOTALL | re.IGNORECASE)
        if not m:
            continue
        href = re.search(r'href="([^"]+)"', m.group(1))
        # la scheda finisce dove inizia la successiva (split) o dove finisce il contenitore
        corpo = b[m.end():]
        coppie = re.findall(r"<strong>\s*([^<]+?)\s*</strong>(.*?)</span>", corpo, re.DOTALL | re.IGNORECASE)
        campi = {k.strip().lower(): RE_SPAZI.sub(" ", _testo(v).replace("\n", " ")).strip() for k, v in coppie}
        stato = campi.get("stato", "")
        pubb = campi.get("data di pubblicazione", "")
        scad = next((v for k, v in campi.items() if "scadenz" in k), "")
        # NB: la riga "Esito pubblicato <data>" non dice che l'avviso e' chiuso: sull'albo dell'ASL 1
        # Sassari compare con la data di pubblicazione anche su avvisi aperti (20/11/2026). Conta
        # solo il campo "Stato" (su ARES "Esito pubblicato" per le procedure concluse).
        voci.append({
            "titolo": RE_SPAZI.sub(" ", _testo(m.group(2)).replace("\n", " ")).strip(),
            "link": urljoin(url_base, html.unescape(href.group(1))) if href else None,
            "testo": "; ".join(x for x in (f"Stato: {stato}" if stato else "",
                                         f"Pubblicazione: {pubb}" if pubb else "",
                                         f"Scadenza: {scad}" if scad else "") if x),
            "pubblicazione_grezza": pubb or None,
            "scadenza_grezza": scad or None,
            "tipo_grezzo": "",
            "stato_grezzo": stato,
        })
    return voci


LETTORI = {"scheda_sito": lettore_scheda_sito, "tabella": lettore_tabella,
           "box_etichette": lettore_box_etichette, "it_card": lettore_it_card,
           "teaser_drupal": lettore_teaser_drupal, "publiccompetitions": lettore_publiccompetitions,
           "albo_ap": lettore_albo_ap}
RE_PROGRAMMAZIONE = re.compile(r"programmazione\s+(triennale|biennale)|programma\s+(triennale|biennale)", re.IGNORECASE)


# ------------------------------------------------------------------ conversione

def _tipo(titolo: str, tipo_grezzo: str = "") -> str:
    t = f"{titolo} {tipo_grezzo}".lower()
    if RE_PROGRAMMAZIONE.search(t):
        return "programmazione acquisti"
    if "consultazion" in t:   # anche con refusi ("consultazione prelimnare")
        return "consultazione di mercato"
    if "manifestazion" in t or "avviso esplorativo" in t or re.search(r"indagine\s+(conoscitiva\s+)?di\s+mercato", t):
        return "avviso esplorativo"
    if "avviso d'asta" in t or "avviso d’asta" in t or "alienazione" in t:
        return "asta"
    return "bando"


_CACHE_ESCLUSIONI: dict[str, re.Pattern | None] = {}


def _esclusioni_sito(sito: dict) -> re.Pattern | None:
    nome = sito.get("sigla") or sito.get("nome", "")
    if nome not in _CACHE_ESCLUSIONI:
        lista = sito.get("escludi_titoli_regex") or []
        _CACHE_ESCLUSIONI[nome] = (re.compile("|".join(f"(?:{p})" for p in lista), re.IGNORECASE)
                                   if lista else None)
    return _CACHE_ESCLUSIONI[nome]


def _ente_voce(voce: dict, sito: dict) -> str:
    """Ente della voce. Con enti_per_area (elenchi che mescolano piu' enti o aree, es. ATS
    Liguria) l'area letta nella voce si traduce nel nome scritto in config: se coincide con
    l'ente di un altro sito che pubblica lo stesso avviso, main.py fonde i due record.
    Un'area non prevista diventa "<ente> - <area>"."""
    ente = sito.get("ente", "")
    mappa = sito.get("enti_per_area")
    area = RE_SPAZI.sub(" ", voce.get("area") or "").strip()
    if not mappa or not area:
        return ente
    norm = {RE_SPAZI.sub(" ", k).strip().lower(): v for k, v in mappa.items()}
    return norm.get(area.lower()) or f"{ente} - {area}"


def _id_voce(voce: dict, sito: dict) -> str:
    chiave = voce.get("link") or voce["titolo"]
    return f"{sito['sigla']}:{hashlib.sha1(chiave.encode('utf-8')).hexdigest()[:12]}"


def _converti(voce: dict, sito: dict, filtro: Filtro) -> Opportunita | None:
    titolo, testo = voce["titolo"], voce["testo"]
    if not titolo:
        return None
    stato = (voce.get("stato_grezzo") or "").strip().lower()
    if stato and stato in {x.lower() for x in sito.get("escludi_stati", [])}:
        return None
    escl = _esclusioni_sito(sito)
    if escl and escl.search(titolo):
        # Dal 23/11/2026 la voce esce anche dallo storico (main.py): una regola di esclusione
        # aggiunta dopo il primo rilevamento (es. adesione Consip ED.3 PAL sul sito AreaCom)
        # altrimenti lascerebbe la voce senza scadenza in dashboard per 120 giorni.
        ESCLUSI.add(_id_voce(voce, sito))
        return None
    ente = _ente_voce(voce, sito)
    if sito.get("enti_per_area") and voce.get("area"):
        testo = f"Area: {voce['area']}. {testo}".strip()
    motivo = filtro.valuta_testo(titolo, ente, "")
    if not motivo and RE_PROGRAMMAZIONE.search(titolo) and filtro.re_ente and filtro.re_ente.search(ente):
        motivo = "Programmazione acquisti di un ente sanitario: consultala per le gare IT in arrivo"
    if not motivo:
        return None

    pubblicazione = parse_data(voce.get("pubblicazione_grezza"))
    if not pubblicazione:
        m = RE_PUBBLICAZIONE.search(testo)
        pubblicazione = parse_data(m.group(1)) if m else None

    scadenza = parse_data(voce.get("scadenza_grezza"))
    m = RE_SCADENZA_ETICHETTA.search(testo)
    if m and not scadenza:
        scadenza = parse_data(m.group(1))
    if not scadenza:
        m = RE_SCADENZA_TESTO.search(f"{titolo}\n{testo}")
        scadenza = parse_data(m.group(1)) if m else None
    # Alcuni siti chiamano "scadenza" la fine della pubblicazione (es. 5 anni dopo):
    # non e' un termine per partecipare, quindi si ignora.
    if scadenza and pubblicazione:
        limite = int(sito.get("scadenza_max_giorni", 365))
        giorni = (date.fromisoformat(scadenza) - date.fromisoformat(pubblicazione)).days
        if giorni > limite:
            scadenza = None
        elif giorni < 0:
            # Scadenza anteriore alla pubblicazione: errore di battitura del sito (ARES, 20/11/2026:
            # pubblicato 11.11.2026, scadenza 11.01.2026). Meglio "non indicata" che "scaduta".
            log.warning("HTML %s: scadenza %s anteriore alla pubblicazione %s, ignorata (%s)",
                        sito["nome"], scadenza, pubblicazione, titolo[:80])
            scadenza = None

    if not scadenza and pubblicazione:
        default = 120 if sito.get("lettore") == "albo_ap" else None
        limite_pub = sito.get("pubblicazione_max_giorni", default)
        if limite_pub is not None and (date.today() - date.fromisoformat(pubblicazione)).days > int(limite_pub):
            log.info("HTML %s: voce senza scadenza pubblicata il %s, ignorata (%s)",
                     sito["nome"], pubblicazione, titolo[:80])
            ESCLUSI.add(_id_voce(voce, sito))
            return None

    return Opportunita(
        id=_id_voce(voce, sito),
        fonti=[sito.get("nome_fonte", sito["nome"])],
        titolo=titolo,
        ente=ente,
        scadenza=scadenza,
        pubblicazione=pubblicazione,
        cig=sorted({c.upper() for c in RE_CIG.findall(f"{titolo} {testo}")
                    if c.strip("0")}),   # "0000000000" = CIG non disponibile
        tipo=_tipo(titolo, voce.get("tipo_grezzo", "")),
        importo=estrai_importo(voce.get("importo_grezzo")),
        link=voce.get("link"),
        regione=sito.get("regione"),
        motivo_match=motivo,
        descrizione=testo[:600] or None,
    )


# ------------------------------------------------------------------ raccolta

# Dove iniziano i dati per ogni lettore: il campione parte da qui (500 byte prima), cosi' contiene
# le voci lette e non solo stili e menu. Negli albi WordPress (run 44738107918) e nei portali PAT di
# ISWEB (run 45180967473 e 45182582775: 60 kB di CSS, nessuna riga della tabella) i primi 60 kB
# della pagina non contengono nessuna voce. Se il marcatore manca si salva l'inizio della pagina.
MARCATORI_CAMPIONE = {
    "albo_ap": b"card-title",
    "tabella": b"<table",
    "scheda_sito": b"scheda-sito",
    "box_etichette": b"box-content",
    "it_card": b"it-card",
    "teaser_drupal": b"node-teaser",
    "publiccompetitions": b"pc_latest_item",
}


def _salva_campione(sito: dict, contenuto: bytes) -> None:
    CAMPIONI.mkdir(parents=True, exist_ok=True)
    nome = re.sub(r"[^a-z0-9]+", "_", sito["nome"].lower()).strip("_")
    marcatore = MARCATORI_CAMPIONE.get(sito.get("lettore"))
    if marcatore:
        i = contenuto.lower().find(marcatore.lower())
        if i > 0:
            contenuto = contenuto[max(0, i - 500):]
    (CAMPIONI / f"{nome}.html").write_bytes(contenuto[:60000])


def _sessione_sito(sito: dict, sess):
    """Sessione con tentativi ridotti per i siti che lo chiedono in config.yaml."""
    if "tentativi" not in sito:
        return sess
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    propria = requests.Session()
    propria.headers.update(sess.headers)
    propria.mount("https://", HTTPAdapter(max_retries=Retry(
        total=int(sito["tentativi"]), backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET"}))))
    return propria


def _intestazioni(sito: dict) -> dict:
    """Intestazioni HTTP del sito: quelle di un browser, oppure (user_agent: progetto) con lo
    User-Agent dichiarato del progetto."""
    scelta = sito.get("user_agent")
    if not scelta:
        return HEADERS
    if scelta != "progetto":
        raise ValueError(f"{sito['nome']}: user_agent ammette solo il valore 'progetto' (trovato {scelta!r})")
    from .common import sessione_http
    intestazioni = dict(HEADERS)
    intestazioni["User-Agent"] = sessione_http().headers["User-Agent"]
    return intestazioni


def _leggi_sito(sito: dict, filtro: Filtro, sess) -> list[Opportunita]:
    lettore = LETTORI[sito["lettore"]]
    intestazioni = _intestazioni(sito)
    sess = _sessione_sito(sito, sess)
    attesa = (float(sito.get("timeout_connessione", 60)), 60)   # (connessione, lettura)
    # Siti che non inviano il certificato intermedio: lo si scarica e si verifica la catena
    # (vedi scout/tls.py). La verifica TLS resta sempre attiva.
    verifica = True
    if sito.get("completa_catena_tls"):
        from .tls import completa_catena
        verifica = completa_catena(sito["url"].replace("{pagina}", str(sito.get("pagina_iniziale", 1))))
    url_modello = sito["url"]
    alternativo = sito.get("url_alternativo")
    paginato = "{pagina}" in url_modello
    max_pagine = int(sito.get("max_pagine", 5)) if paginato else 1
    risultati: dict[str, Opportunita] = {}
    visti: set[str] = set()
    letti = 0

    prima = int(sito.get("pagina_iniziale", 1))   # alcuni siti contano da 0 (Drupal: ?page=0)
    for pagina in range(prima, prima + max_pagine):
        url = url_modello.replace("{pagina}", str(pagina))
        try:
            r = sess.get(url, headers=intestazioni, timeout=attesa, verify=verifica)
            r.raise_for_status()
        except requests.RequestException as e:
            # Alcuni portali bloccano i server cloud (es. 403 da GitHub Actions):
            # si riprova sull'indirizzo alternativo indicato in config.yaml.
            if not alternativo or url_modello == alternativo:
                raise
            log.warning("HTML %s: %s, provo l'indirizzo alternativo", sito["nome"], e)
            url_modello = alternativo
            url = url_modello.replace("{pagina}", str(pagina))
            try:
                r = sess.get(url, headers=intestazioni, timeout=attesa, verify=verifica)
                r.raise_for_status()
            except requests.RequestException as e2:
                raise RuntimeError(f"{sito['nome']}: indirizzo principale ({e}) e alternativo ({e2}) "
                                   f"entrambi non raggiungibili") from e2
        if pagina == prima:
            _salva_campione(sito, r.content)
        voci = lettore(r.content.decode(r.encoding or "utf-8", errors="replace"), url)
        firme = {v.get("link") or v["titolo"] for v in voci}
        if not voci or firme <= visti:
            break
        visti |= firme
        letti += len(voci)
        for v in voci:
            o = _converti(v, sito, filtro)
            if o:
                risultati[o.id] = o
        if paginato:
            time.sleep(1)

    log.info("HTML %s: %d schede lette, %d pertinenti", sito["nome"], letti, len(risultati))
    if letti == 0:
        raise RuntimeError(f"{sito['nome']}: nessuna scheda trovata (vedi state/campioni)")
    return list(risultati.values())


def raccogli(cfg: dict, filtro: Filtro, sess) -> list[Opportunita]:
    risultati: list[Opportunita] = []
    errori: list[str] = []
    siti = [s for s in cfg["portali_html"].get("siti", []) if s.get("abilitato", True)]
    for sito in siti:
        try:
            risultati.extend(_leggi_sito(sito, filtro, sess))
        except Exception as e:
            log.exception("HTML %s: errore", sito["nome"])
            errori.append(f"{sito['nome']}: {e}")
    if errori and len(errori) == len(siti):
        raise RuntimeError("; ".join(errori))
    ERRORI.extend(errori)
    return risultati
