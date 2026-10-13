"""Raccolta da siti che pubblicano i bandi solo come pagine HTML.

Ogni sito si configura in config.yaml (sezione portali_html) indicando quale
"lettore" usare. I lettori sono funzioni che ricevono l'HTML di una pagina e
restituiscono un elenco di dizionari {titolo, link, testo}.

Lettori disponibili:
  scheda_sito  - elenchi WordPress / Bootstrap Italia con schede "scheda-sito"
                 (es. USL Umbria 1): titolo nel link dell'h6, date nel blocco
                 "data small" con le diciture DATA PUBBLICAZIONE / DATA SCADENZA.
"""
from __future__ import annotations

import hashlib
import html
import re
import time
from pathlib import Path
from urllib.parse import urljoin

from .common import Filtro, Opportunita, estrai_importo, log, parse_data

CAMPIONI = Path("state/campioni")
ERRORI: list[str] = []
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
        if not any("oggetto" in h for h in intestazioni):
            continue

        def colonna(*parole):
            for i, h in enumerate(intestazioni):
                if any(p in h for p in parole):
                    return i
            return None

        c_ogg, c_scad = colonna("oggetto"), colonna("scadenza")
        c_tipo, c_imp = colonna("tipolog", "tipo"), colonna("importo")
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
                "testo": f"DATA SCADENZA: {cella(c_scad)}\nTIPOLOGIA: {cella(c_tipo)}\nIMPORTO: {cella(c_imp)}",
                "scadenza_grezza": cella(c_scad),
                "tipo_grezzo": cella(c_tipo),
                "importo_grezzo": cella(c_imp),
            })
    return voci


LETTORI = {"scheda_sito": lettore_scheda_sito, "tabella": lettore_tabella}
RE_PROGRAMMAZIONE = re.compile(r"programmazione\s+(triennale|biennale)|programma\s+(triennale|biennale)", re.IGNORECASE)


# ------------------------------------------------------------------ conversione

def _tipo(titolo: str, tipo_grezzo: str = "") -> str:
    t = f"{titolo} {tipo_grezzo}".lower()
    if RE_PROGRAMMAZIONE.search(t):
        return "programmazione acquisti"
    if "consultazione preliminare" in t or "consultazione di mercato" in t:
        return "consultazione di mercato"
    if "manifestazion" in t or "avviso esplorativo" in t or "indagine di mercato" in t:
        return "avviso esplorativo"
    if "avviso d'asta" in t or "avviso d’asta" in t or "alienazione" in t:
        return "asta"
    return "bando"


def _converti(voce: dict, sito: dict, filtro: Filtro) -> Opportunita | None:
    titolo, testo = voce["titolo"], voce["testo"]
    if not titolo:
        return None
    ente = sito.get("ente", "")
    motivo = filtro.valuta_testo(titolo, ente, "")
    if not motivo and RE_PROGRAMMAZIONE.search(titolo) and filtro.re_ente and filtro.re_ente.search(ente):
        motivo = "Programmazione acquisti di un ente sanitario: consultala per le gare IT in arrivo"
    if not motivo:
        return None

    m = RE_PUBBLICAZIONE.search(testo)
    pubblicazione = parse_data(m.group(1)) if m else None

    scadenza = parse_data(voce.get("scadenza_grezza"))
    m = RE_SCADENZA_ETICHETTA.search(testo)
    if m and not scadenza:
        scadenza = parse_data(m.group(1))
    if not scadenza:
        m = RE_SCADENZA_TESTO.search(f"{titolo}\n{testo}")
        scadenza = parse_data(m.group(1)) if m else None

    chiave = voce.get("link") or titolo
    return Opportunita(
        id=f"{sito['sigla']}:{hashlib.sha1(chiave.encode('utf-8')).hexdigest()[:12]}",
        fonti=[sito.get("nome_fonte", sito["nome"])],
        titolo=titolo,
        ente=ente,
        scadenza=scadenza,
        pubblicazione=pubblicazione,
        cig=sorted({c.upper() for c in RE_CIG.findall(f"{titolo} {testo}")}),
        tipo=_tipo(titolo, voce.get("tipo_grezzo", "")),
        importo=estrai_importo(voce.get("importo_grezzo")),
        link=voce.get("link"),
        regione=sito.get("regione"),
        motivo_match=motivo,
        descrizione=testo[:600] or None,
    )


# ------------------------------------------------------------------ raccolta

def _salva_campione(sito: dict, contenuto: bytes) -> None:
    CAMPIONI.mkdir(parents=True, exist_ok=True)
    nome = re.sub(r"[^a-z0-9]+", "_", sito["nome"].lower()).strip("_")
    (CAMPIONI / f"{nome}.html").write_bytes(contenuto[:60000])


def _leggi_sito(sito: dict, filtro: Filtro, sess) -> list[Opportunita]:
    lettore = LETTORI[sito["lettore"]]
    url_modello = sito["url"]
    paginato = "{pagina}" in url_modello
    max_pagine = int(sito.get("max_pagine", 5)) if paginato else 1
    risultati: dict[str, Opportunita] = {}
    visti: set[str] = set()
    letti = 0

    for pagina in range(1, max_pagine + 1):
        url = url_modello.replace("{pagina}", str(pagina))
        r = sess.get(url, headers=HEADERS, timeout=60)
        r.raise_for_status()
        if pagina == 1:
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
