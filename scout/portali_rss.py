"""Raccolta da feed RSS di portali di e-procurement (EmPULIA e simili).

Modulo generico: ogni feed si configura in config.yaml (sezione portali_rss).
I feed non riportano i CPV, quindi il filtro lavora sul testo (titolo e
descrizione). Ente, scadenza, CIG e importo vengono cercati sia nei campi
dedicati dell'elemento RSS sia nel testo della descrizione.

Opzioni per feed oltre a "campi": "ente" (ente fisso, per i feed di un solo ente),
"escludi_titoli_regex" (titoli da scartare, es. aggiudicazioni), "escludi_stati",
"timeout_connessione" e "tentativi" (come per i portali HTML).
Altre opzioni (19/11/2026, START Sanita' Toscana):
  "escludi_campi": {NomeCampo: [valori]} scarta le voci con quel valore in un campo del feed
      (confronto esatto, senza maiuscole/minuscole), es. {TipoProcedura: [Affidamento Diretto]}
      per le procedure riservate agli invitati;
  "link_sostituisci": {"testo": "nuovo testo"} corregge i link del feed (es. un percorso
      che sul portale non esiste piu');
  "enti_rinomina": {"nome nel feed": "nome da usare"}, es. per usare lo stesso nome di TED
      e permettere a main.py di fondere la stessa gara vista sulle due fonti.
Opzione aggiunta il 20/11/2026 (STELLA, Lazio):
  "escludi_descrizioni_regex": come escludi_titoli_regex, ma sulla descrizione. Serve quando il
      titolo e' il nome breve della procedura e la formula che la esclude (es. "AFFIDAMENTO
      DIRETTO, AI SENSI DELL'ART. 50...") sta solo nell'oggetto completo.
Opzioni aggiunte il 20/11/2026 (eAppalti SAT, Friuli Venezia Giulia):
  "vuoto_ammesso": true: un feed ben formato senza voci (per i feed afs: totRecord 0, es. nessuna
      consultazione in corso) non e' un errore. Una risposta non RSS resta un errore.
  "link_fisso": "url": stesso link per tutte le voci del feed (es. l'elenco pubblico), al posto
      di quello del feed; vale anche per le voci gia' salvate (correggi_storico). Ha la
      precedenza su link_sostituisci.
Opzioni aggiunte il 23/11/2026 (sito dell'ASL 3 Pescara, feed senza date):
  "dettaglio": per le sole voci pertinenti si apre la scheda (il link della voce, gia' corretto) e
      se ne leggono pochi campi con espressioni regolari sul testo della pagina:
        scadenza_regex / pubblicazione_regex  - un gruppo con la data (usata se il feed non la da');
        descrizione_regex   - elenco di espressioni con un gruppo: i valori trovati diventano la
                              descrizione (es. il tipo di procedura). Nient'altro della scheda si
                              legge o si salva (le schede contengono il responsabile del procedimento);
        escludi_regex       - se il testo della scheda corrisponde, la voce si scarta e finisce in
                              ESCLUSI (es. le procedure che la scheda rimanda a GIADA, gia' lette da li');
        pausa_secondi       - attesa tra due schede (default 1).
      Se la scheda non si apre la voce resta, senza i dati della scheda, con un avviso nel log.
  "descrizione_aggiunta": testo fisso aggiunto alla descrizione di ogni voce del feed (es. la sezione
      del sito \"Bandi esplorativi beni infungibili\", che fa scattare l'etichetta della dashboard).
Opzioni aggiunte il 24/11/2026 (PlaCe-VdA, Valle d'Aosta):
  "escludi_combinazioni": elenco di regole {NomeCampo: [valori], AltroCampo: [valori]}; una voce si
      scarta se TUTTI i campi di una regola hanno uno dei valori indicati (confronto esatto, senza
      maiuscole/minuscole). Serve quando un valore da solo non basta: su PlaCe-VdA "Affidamento
      Diretto" vale sia per le richieste di offerta (Tipo Bando, a invito) sia per le indagini di
      mercato e le consultazioni aperte a tutti (Tipo Avviso), che vanno tenute.
  "campi: proponente: [EnteProponente]": ente per conto del quale la centrale di committenza svolge
      la gara (su PlaCe-VdA IN.VA per l'Azienda USL). Se e' diverso dall'ente, l'ente diventa
      "<ente> per conto di <proponente>" (es. "IN.VA. S.P.A. per conto di AZIENDA USL VALLE D'AOSTA"):
      il filtro la vede come gara di un ente sanitario, la dashboard mostra per chi si compra e la
      rivalutazione dello storico in main.py, che conosce solo l'ente salvato, da' lo stesso esito.
      Le regole sugli aggregatori misti scritte con l'inizio del nome (es. "^IN.VA") valgono ancora.
Le voci scartate da una regola esplicita (stato, campo, titolo) finiscono in ESCLUSI: main.py
le toglie anche dallo storico (es. un bando revocato dopo essere stato salvato).
correggi_storico() applica link_sostituisci ed enti_rinomina anche alle voci gia' salvate.
Con "campi: descrizione: []" la descrizione non viene letta (utile se contiene
dati personali, es. il nome del RUP).

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
ESCLUSI: set[str] = set()  # id delle voci scartate da una regola esplicita (vedi correggi_storico)
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


def _data_rss(valore: str | None) -> str | None:
    """Date italiane/ISO (parse_data) e formato RSS "Wed, 18 Nov 2026 14:07:25 +0000"."""
    if not valore:
        return None
    d = parse_data(valore)
    if d:
        return d
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(valore.strip()).date().isoformat()
    except (TypeError, ValueError, IndexError):
        return None


RE_CODICE_CPV = re.compile(r"\b(\d{8})(?:-\d)?\b")


def _id_voce(campi: dict[str, str], feed: dict, titolo: str) -> str:
    """Identificativo stabile della voce: campo \"id\" della mappa, altrimenti un hash di
    guid, link originale (prima di link_sostituisci) o titolo."""
    mappa = feed.get("campi") or {}
    chiave = _valore(campi, mappa.get("id"))
    if not chiave:
        link = (campi.get("link") or "").strip()
        link = requote_uri(link) if link else None
        chiave = (campi.get("guid") or link or titolo).strip()
        chiave = hashlib.sha1(chiave.encode("utf-8")).hexdigest()[:12]
    return f"{feed['sigla']}:{chiave}"


def _correggi_link(link: str | None, feed: dict) -> str | None:
    if feed.get("link_fisso"):
        # Stesso link per tutte le voci (es. l'elenco pubblico) quando la scheda di dettaglio non
        # e' utilizzabile: su eAppalti SAT FVG si apre in circa 40 secondi e senza il bando.
        return feed["link_fisso"]
    for vecchio, nuovo in (feed.get("link_sostituisci") or {}).items():
        if link:
            link = link.replace(vecchio, nuovo)
    return link


def _rinomina_ente(ente: str, feed: dict) -> str:
    mappa = {k.strip().lower(): v for k, v in (feed.get("enti_rinomina") or {}).items()}
    return mappa.get((ente or "").strip().lower(), ente)


def _chiave_ente(nome: str) -> str:
    """Solo lettere e cifre, minuscole: \"IN.VA. S.P.A.\" e \"IN.VA S.p.A.\" coincidono."""
    return re.sub(r"[^0-9a-z]+", "", (nome or "").lower())


def _esclusa(campi: dict[str, str], feed: dict, titolo: str, descrizione: str = "") -> bool:
    """Regole esplicite di esclusione del feed: stato, valori di campo, titolo, descrizione."""
    mappa = feed.get("campi") or {}
    stato = _valore(campi, mappa.get("stato"))
    if stato and stato.lower() in {s.lower() for s in feed.get("escludi_stati", [])}:
        return True
    for nome, valori in (feed.get("escludi_campi") or {}).items():
        v = _valore(campi, [nome]).strip().lower()
        if v and v in {str(x).strip().lower() for x in valori}:
            return True
    for regola in feed.get("escludi_combinazioni") or []:
        # Tutte le condizioni della regola devono valere (un campo vuoto non corrisponde).
        if regola and all(
                _valore(campi, [nome]).strip().lower() in {str(x).strip().lower() for x in valori}
                and _valore(campi, [nome]).strip()
                for nome, valori in regola.items()):
            return True
    escl = feed.get("escludi_titoli_regex")
    if escl and re.search("|".join(f"(?:{e})" for e in escl), titolo, re.IGNORECASE):
        return True
    escl = feed.get("escludi_descrizioni_regex")
    if escl and re.search("|".join(f"(?:{e})" for e in escl), descrizione, re.IGNORECASE):
        return True
    return False


def _converti(campi: dict[str, str], feed: dict, filtro: Filtro) -> Opportunita | None:
    mappa = feed.get("campi") or {}

    titolo = _valore(campi, mappa.get("titolo", ["title"])).replace("\n", " ")
    descrizione = _valore(campi, mappa.get("descrizione", ["description", "summary", "content"]))
    if _esclusa(campi, feed, titolo, descrizione):
        ESCLUSI.add(_id_voce(campi, feed, titolo))
        return None
    if not titolo:
        return None
    tutto = f"{titolo}\n{descrizione}"

    # "ente" fisso in config.yaml: per i feed di un solo ente, dove l'autore (dc:creator)
    # e' una persona e non va ne' usato come ente ne' mostrato (es. ASL CN2, WordPress).
    ente = feed.get("ente") or (_valore(campi, mappa.get("ente"))
            or _cerca_campo(campi, ("ente", "amministrazione", "stazione", "author", "creator")))
    if not ente:
        m = RE_ENTE.search(descrizione)
        ente = m.group(1).strip() if m else ""
    ente = _rinomina_ente(ente, feed)

    # Gara svolta da una centrale di committenza per conto di un altro ente (PlaCe-VdA: IN.VA per
    # l'Azienda USL): l'ente salvato dice anche per chi, cosi' filtro e rivalutazione lo vedono.
    proponente = _rinomina_ente(_valore(campi, mappa.get("proponente")), feed) if mappa.get("proponente") else ""
    if proponente and ente and _chiave_ente(proponente) != _chiave_ente(ente):
        ente = f"{ente} per conto di {proponente}"

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
    link = _correggi_link(requote_uri(link) if link else None, feed)

    return Opportunita(
        id=_id_voce(campi, feed, titolo),
        fonti=[feed["nome_fonte"]],
        titolo=titolo,
        ente=ente,
        scadenza=scadenza,
        pubblicazione=_data_rss(_valore(campi, mappa.get("pubblicazione"))
                                or campi.get("pubdate") or campi.get("published")),
        importo=importo,
        cpv=cpv,
        cig=sorted(cig),
        tipo=feed.get("tipo") or _valore(campi, mappa.get("tipo")) or None,
        link=link,
        regione=feed.get("regione"),
        motivo_match=motivo,
        descrizione=_unisci_descrizione(descrizione, feed.get("descrizione_aggiunta"))[:600] or None,
    )


def _unisci_descrizione(*parti) -> str:
    return "\n".join(p.strip() for p in parti if p and p.strip())


def _completa_da_dettaglio(o: Opportunita, feed: dict, sess, attesa) -> Opportunita | None:
    """Legge dalla scheda della voce (solo per le voci pertinenti) scadenza, pubblicazione e i
    campi indicati in dettaglio.descrizione_regex. None se la scheda corrisponde a escludi_regex.
    Della pagina non si salva nulla: contiene il nome del responsabile del procedimento."""
    det = feed["dettaglio"]
    if not o.link:
        return o
    try:
        r = sess.get(o.link, headers=HEADERS, timeout=attesa)
        r.raise_for_status()
        testo = _pulisci(r.content.decode(r.encoding or "utf-8", errors="replace"))
    except Exception as e:
        log.warning("RSS %s: scheda non letta per %s (%s), la voce resta senza i suoi dati",
                    feed["nome"], o.id, e)
        return o
    escl = det.get("escludi_regex")
    if escl:
        m = re.search("|".join(f"(?:{x})" for x in escl), testo, re.IGNORECASE)
        if m:
            log.info("RSS %s: %s scartata dalla scheda (\"%s\"): %s", feed["nome"], o.id,
                     m.group(0), o.titolo[:80])
            ESCLUSI.add(o.id)
            return None

    def cerca(espressione):
        m = re.search(espressione, testo, re.IGNORECASE) if espressione else None
        return m.group(1).strip() if m else ""

    o.scadenza = o.scadenza or parse_data(cerca(det.get("scadenza_regex")))
    o.pubblicazione = o.pubblicazione or parse_data(cerca(det.get("pubblicazione_regex")))
    valori = [v for v in (cerca(x) for x in det.get("descrizione_regex") or []) if v]
    if valori:
        o.descrizione = _unisci_descrizione("; ".join(valori), o.descrizione)[:600]
    if not o.scadenza:
        log.info("RSS %s: nessuna scadenza nella scheda di %s", feed["nome"], o.id)
    return o


# Autore del post nei feed WordPress (dc:creator, author): e' il nome di chi pubblica, una persona.
# Non si legge (ente fisso in config) e dal 25/11/2026 non si salva nemmeno nel campione, che sta nel
# repository pubblico (lezione 53): prima il campione dell'ASL CN2 lo conteneva; il feed dell'IZS di
# Puglia e Basilicata ha lo stesso campo.
RE_AUTORE_CAMPIONE = re.compile(rb"(<(dc:creator|author)\b[^>]*>).*?(</\2>)", re.IGNORECASE | re.DOTALL)


def _salva_campione(feed: dict, contenuto: bytes) -> None:
    CAMPIONI.mkdir(parents=True, exist_ok=True)
    nome = re.sub(r"[^a-z0-9]+", "_", feed["nome"].lower()).strip("_")
    contenuto = RE_AUTORE_CAMPIONE.sub(rb"\1[non salvato]\3", contenuto)
    (CAMPIONI / f"{nome}.xml").write_bytes(contenuto[:30000])


RE_TOT_RECORD = re.compile(rb"<(?:[\w-]+:)?totRecord>\s*(\d+)", re.IGNORECASE)
RE_RIGHE_PAGINA = re.compile(r"numRowForPag=(\d+)", re.IGNORECASE)


def _pagine_previste(contenuto: bytes, url_modello: str) -> int | None:
    """Numero di pagine dichiarato dal feed (schema afs: totRecord + numRowForPag)."""
    m_tot = RE_TOT_RECORD.search(contenuto[:5000])
    m_righe = RE_RIGHE_PAGINA.search(url_modello)
    if not (m_tot and m_righe) or int(m_righe.group(1)) <= 0:
        return None
    return max(1, -(-int(m_tot.group(1)) // int(m_righe.group(1))))   # arrotonda per eccesso


def _feed_vuoto_valido(contenuto: bytes) -> bool:
    """True se la risposta e' un RSS ben formato con un <channel> e nessun <item>
    (per i feed afs: anche totRecord 0). Una pagina HTML di errore non lo e'."""
    try:
        radice = ET.fromstring(contenuto)
    except ET.ParseError:
        return False
    canale = next((e for e in radice.iter() if _nome_locale(e.tag) == "channel"), None)
    if canale is None or any(_nome_locale(e.tag) in ("item", "entry") for e in radice.iter()):
        return False
    tot = next((e.text for e in radice.iter() if _nome_locale(e.tag) == "totrecord"), None)
    return tot is None or tot.strip() in ("", "0")


def _leggi_feed(feed: dict, filtro: Filtro, sess) -> list[Opportunita]:
    # Attesa e tentativi per feed (come per i portali HTML): utili per i siti nuovi,
    # che potrebbero non rispondere ai server di GitHub.
    from .portali_html import _sessione_sito
    sess = _sessione_sito(feed, sess)
    attesa = (float(feed.get("timeout_connessione", 90)), 90)
    url_modello = feed["url"]
    paginato = "{pagina}" in url_modello
    max_pagine = int(feed.get("max_pagine", 30)) if paginato else 1
    risultati: dict[str, Opportunita] = {}
    visti: set[str] = set()
    letti = 0
    prima_pagina = b""

    pagina = 0
    while pagina < max_pagine:
        pagina += 1
        url = url_modello.replace("{pagina}", str(pagina))
        try:
            r = sess.get(url, headers=HEADERS, timeout=attesa)
            r.raise_for_status()
        except Exception as e:
            if pagina == 1:
                raise
            # Una pagina successiva in errore non deve far perdere quelle gia' lette.
            log.warning("RSS %s: pagina %d non letta (%s), tengo le %d precedenti",
                        feed["nome"], pagina, e, pagina - 1)
            ERRORI.append(f"{feed['nome']}: lette {pagina - 1} pagine, la {pagina} ha dato errore "
                          f"(dati parziali, non persi)")
            break
        if pagina == 1:
            _salva_campione(feed, r.content)
            prima_pagina = r.content
            if paginato:
                previste = _pagine_previste(r.content, url_modello)
                if previste is not None:   # il feed dice quante pagine ha: non chiedere oltre
                    if previste > max_pagine:
                        # Per alcuni feed e' voluto (consultazioni ordinate dalla piu' recente),
                        # ma va detto: il 20/11/2026 STELLA si e' fermato a 500 voci su 1.460
                        # senza che nulla lo segnalasse.
                        log.warning("RSS %s: il feed dichiara %d pagine, se ne leggono %d (max_pagine)",
                                    feed["nome"], previste, max_pagine)
                    max_pagine = min(max_pagine, previste)
        voci = _elementi(r.content)
        firme = {(v.get("guid") or v.get("link") or v.get("title") or "") for v in voci}
        if not voci or firme <= visti:   # pagina vuota o ripetuta: fine
            break
        visti |= firme
        letti += len(voci)
        for v in voci:
            o = _converti(v, feed, filtro)
            if o and feed.get("dettaglio"):
                time.sleep(float(feed["dettaglio"].get("pausa_secondi", 1)))
                o = _completa_da_dettaglio(o, feed, sess, attesa)
            if o:
                risultati[o.id] = o
        if paginato and pagina < max_pagine:
            time.sleep(1)

    log.info("RSS %s: %d elementi letti, %d pertinenti", feed["nome"], letti, len(risultati))
    if letti == 0 and feed.get("vuoto_ammesso") and _feed_vuoto_valido(prima_pagina):
        # Feed valido senza voci (es. nessuna consultazione in corso su eAppalti SAT FVG):
        # non e' un errore, ma resta nel log.
        log.info("RSS %s: feed valido senza voci in corso", feed["nome"])
        return []
    if letti == 0:
        raise RuntimeError(f"feed {feed['nome']} vuoto o non leggibile (vedi state/campioni)")
    return list(risultati.values())


def correggi_storico(storico: dict, cfg: dict) -> dict:
    """Applica allo storico le regole dei feed che lo storico da solo non conosce:
    toglie le voci ora escluse da una regola esplicita (ESCLUSI, es. procedure a invito,
    bandi revocati) e corregge link e nomi di ente delle voci gia' salvate."""
    feeds = {f["sigla"]: f for f in (cfg.get("portali_rss") or {}).get("feed", [])}
    tolte = [k for k in storico if k in ESCLUSI]
    for k in tolte:
        del storico[k]
    if tolte:
        log.info("RSS: tolte dallo storico %d voci ora escluse: %s", len(tolte), ", ".join(sorted(tolte)))
    for o in storico.values():
        feed = feeds.get(o.id.split(":", 1)[0])
        if feed:
            o.link = _correggi_link(o.link, feed)
            o.ente = _rinomina_ente(o.ente, feed)
    return storico


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
