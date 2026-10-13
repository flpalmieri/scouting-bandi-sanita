"""Raccolta da feed RSS di portali di e-procurement (EmPULIA e simili).

Modulo generico: ogni feed si configura in config.yaml (sezione portali_rss).
I feed non riportano i CPV, quindi il filtro lavora sul testo (titolo e
descrizione). Ente, scadenza, CIG e importo vengono cercati sia nei campi
dedicati dell'elemento RSS sia nel testo della descrizione.

A ogni esecuzione viene salvato in state/campioni/ un estratto grezzo della
prima pagina di ogni feed, utile per verificare la struttura dei dati.
"""
from __future__ import annotations

import hashlib
import html
import re
import time
import xml.etree.ElementTree as ET

from requests.utils import requote_uri
from pathlib import Path

from .common import Filtro, Opportunita, log, parse_data, parse_importo

CAMPIONI = Path("state/campioni")
ERRORI: list[str] = []  # errori non bloccanti (un feed fallito su più feed)
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9",
}

RE_TAG = re.compile(r"<[^>]+>")
RE_SPAZI = re.compile(r"[ \t\r\f\v]+")
RE_CIG = re.compile(r"\bCIG\b\W{0,5}([0-9A-Z]{10})\b", re.IGNORECASE)
RE_ENTE = re.compile(
    r"(?:ente(?:\s+appaltante)?|stazione\s+appaltante|amministrazione(?:\s+aggiudicatrice)?)"
    r"\s*[:\-]\s*([^\n|;]{3,200}?)"
    r"(?=\s+[-–]\s+|\s*\b(?:CIG|CUP|scadenz\w*|importo|data)\b|\s*$)",
    re.IGNORECASE | re.MULTILINE)
RE_SCADENZA = re.compile(
    r"(?:scadenz\w*|termine\s+(?:di\s+)?presentazione|presentazione\s+offert\w*)"
    r"[^0-9\n]{0,60}(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})", re.IGNORECASE)
RE_IMPORTO = re.compile(r"importo[^0-9\n]{0,40}([\d.]+(?:,\d{1,2})?)", re.IGNORECASE)


def _pulisci(testo: str | None) -> str:
    if not testo:
        return ""
    t = html.unescape(testo)
    t = re.sub(r"<\s*br\s*/?>|</p>|</div>|</tr>|</li>", "\n", t, flags=re.IGNORECASE)
    t = RE_TAG.sub(" ", t)
    t = html.unescape(t)
    righe = [RE_SPAZI.sub(" ", r).strip() for r in t.split("\n")]
    return "\n".join(r for r in righe if r)


def _nome_locale(tag: str) -> str:
    """'{ns}Oggetto' oppure 'afs:Oggetto' -> 'oggetto'."""
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower()


def _elementi(contenuto: bytes) -> list[dict[str, str]]:
    """Estrae gli <item> come dizionari {nome_campo: testo}. Tollerante agli XML difettosi."""
    try:
        radice = ET.fromstring(contenuto)
        voci = []
        for item in radice.iter():
            if _nome_locale(item.tag) not in ("item", "entry"):
                continue
            campi = {}
            for figlio in item:
                nome = _nome_locale(figlio.tag)
                valore = figlio.text or figlio.attrib.get("href") or ""
                if nome in campi:
                    campi[nome] += "\n" + valore
                else:
                    campi[nome] = valore
            voci.append(campi)
        return voci
    except ET.ParseError as e:
        log.warning("RSS: XML non valido (%s), uso il parser di riserva", e)
    testo = contenuto.decode("utf-8", errors="replace")
    voci = []
    for blocco in re.findall(r"<item\b[^>]*>(.*?)</item>", testo, re.DOTALL | re.IGNORECASE):
        campi = {}
        for nome, valore in re.findall(r"<([\w:]+)[^>]*>(.*?)</\1>", blocco, re.DOTALL):
            valore = re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", valore.strip(), flags=re.DOTALL)
            campi[_nome_locale(nome)] = valore
        voci.append(campi)
    return voci


def _valore(campi: dict[str, str], nomi) -> str:
    """Primo campo non vuoto tra quelli indicati (nomi senza prefisso, minuscoli)."""
    for n in nomi or []:
        v = _pulisci(campi.get(n.lower()))
        if v:
            return v
    return ""


def _cerca_campo(campi: dict[str, str], parole: tuple[str, ...]) -> str:
    """Ricerca euristica per nome di campo, usata quando il feed non ha una mappa."""
    for nome, valore in campi.items():
        if nome in ("title", "description", "link", "guid"):
            continue
        if any(p in nome for p in parole) and valore and valore.strip():
            return _pulisci(valore)
    return ""


RE_CODICE_CPV = re.compile(r"\b(\d{8})(?:-\d)?\b")


def _converti(campi: dict[str, str], feed: dict, filtro: Filtro) -> Opportunita | None:
    mappa = feed.get("campi") or {}

    stato = _valore(campi, mappa.get("stato"))
    if stato and stato.lower() in {s.lower() for s in feed.get("escludi_stati", [])}:
        return None

    titolo = _valore(campi, mappa.get("titolo", ["title"])).replace("\n", " ")
    descrizione = _valore(campi, mappa.get("descrizione", ["description", "summary", "content"]))
    if not titolo:
        return None
    tutto = f"{titolo}\n{descrizione}"

    ente = (_valore(campi, mappa.get("ente"))
            or _cerca_campo(campi, ("ente", "amministrazione", "stazione", "author", "creator")))
    if not ente:
        m = RE_ENTE.search(descrizione)
        ente = m.group(1).strip() if m else ""

    scadenza = parse_data(_valore(campi, mappa.get("scadenza"))
                          or _cerca_campo(campi, ("scadenz", "deadline", "termine")))
    if not scadenza:
        m = RE_SCADENZA.search(tutto)
        scadenza = parse_data(m.group(1)) if m else None

    cpv = sorted(set(RE_CODICE_CPV.findall(_valore(campi, mappa.get("cpv")))))

    # Con il CPV si usa il filtro preciso; il filtro testuale recupera i casi
    # con CPV non informatico ma oggetto chiaramente IT (es. telemedicina).
    motivo = (filtro.valuta(titolo, ente, cpv) if cpv else None) \
        or filtro.valuta_testo(titolo, ente, descrizione)
    if not motivo:
        return None

    importo = parse_importo(_valore(campi, mappa.get("importo"))
                            or _cerca_campo(campi, ("importo", "valore", "amount")))
    if importo is None:
        m = RE_IMPORTO.search(tutto)
        importo = parse_importo(m.group(1)) if m else None

    cig = set(c.upper() for c in RE_CIG.findall(tutto))
    cig |= set(re.findall(r"\b[0-9A-Z]{10}\b", _valore(campi, mappa.get("cig")).upper()))

    link = (campi.get("link") or "").strip()
    link = requote_uri(link) if link else None
    chiave = _valore(campi, mappa.get("id")) or (campi.get("guid") or link or titolo).strip()
    if not _valore(campi, mappa.get("id")):
        chiave = hashlib.sha1(chiave.encode("utf-8")).hexdigest()[:12]

    return Opportunita(
        id=f"{feed['sigla']}:{chiave}",
        fonti=[feed["nome_fonte"]],
        titolo=titolo,
        ente=ente,
        scadenza=scadenza,
        pubblicazione=parse_data(_valore(campi, mappa.get("pubblicazione"))
                                 or campi.get("pubdate") or campi.get("published")),
        importo=importo,
        cpv=cpv,
        cig=sorted(cig),
        tipo=feed.get("tipo") or _valore(campi, mappa.get("tipo")) or None,
        link=link,
        regione=feed.get("regione"),
        motivo_match=motivo,
        descrizione=descrizione[:600] or None,
    )


def _salva_campione(feed: dict, contenuto: bytes) -> None:
    CAMPIONI.mkdir(parents=True, exist_ok=True)
    nome = re.sub(r"[^a-z0-9]+", "_", feed["nome"].lower()).strip("_")
    (CAMPIONI / f"{nome}.xml").write_bytes(contenuto[:30000])


def _leggi_feed(feed: dict, filtro: Filtro, sess) -> list[Opportunita]:
    url_modello = feed["url"]
    paginato = "{pagina}" in url_modello
    max_pagine = int(feed.get("max_pagine", 30)) if paginato else 1
    risultati: dict[str, Opportunita] = {}
    visti: set[str] = set()
    letti = 0

    for pagina in range(1, max_pagine + 1):
        url = url_modello.replace("{pagina}", str(pagina))
        r = sess.get(url, headers=HEADERS, timeout=90)
        r.raise_for_status()
        if pagina == 1:
            _salva_campione(feed, r.content)
        voci = _elementi(r.content)
        firme = {(v.get("guid") or v.get("link") or v.get("title") or "") for v in voci}
        if not voci or firme <= visti:   # pagina vuota o ripetuta: fine
            break
        visti |= firme
        letti += len(voci)
        for v in voci:
            o = _converti(v, feed, filtro)
            if o:
                risultati[o.id] = o
        if paginato:
            time.sleep(1)

    log.info("RSS %s: %d elementi letti, %d pertinenti", feed["nome"], letti, len(risultati))
    if letti == 0:
        raise RuntimeError(f"feed {feed['nome']} vuoto o non leggibile (vedi state/campioni)")
    return list(risultati.values())


def raccogli(cfg: dict, filtro: Filtro, sess) -> list[Opportunita]:
    risultati: list[Opportunita] = []
    errori: list[str] = []
    feeds = [f for f in cfg["portali_rss"].get("feed", []) if f.get("abilitato", True)]
    for feed in feeds:
        try:
            risultati.extend(_leggi_feed(feed, filtro, sess))
        except Exception as e:
            log.exception("RSS %s: errore", feed["nome"])
            errori.append(f"{feed['nome']}: {e}")
    if errori and len(errori) == len(feeds):
        raise RuntimeError("; ".join(errori))
    ERRORI.extend(errori)
    return risultati
