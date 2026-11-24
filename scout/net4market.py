"""Piattaforme Net4market "Albo fornitori / e-procurement" (app.albofornitori.it, Sicilia 23/11/2026).

La pagina pubblica dell'albo di un ente (es. https://app.albofornitori.it/alboeproc/albo_aspragusa)
e' un'applicazione GWT: l'elenco delle gare arriva con una chiamata GWT-RPC
    POST https://app.albofornitori.it/alboeproc/alboeproc/servizioGarePubblico
    Content-Type: text/x-gwt-rpc; charset=UTF-8, X-GWT-Permutation, X-GWT-Module-Base
metodo filtraListaBandiAlboFornitori. Il corpo cambia solo nel nome dell'albo e nell'ultimo booleano
(0 = in corso, 1 = scadute). Serve una sessione anonima: prima un GET della pagina dell'albo (cookie),
poi la POST (verificato dall'utente dal suo computer con lo User-Agent del progetto: senza cookie la
risposta e' "//EX ... configInvalide").

La risposta e' il formato di serializzazione di GWT (//OK[...]): una lista di numeri letta al
contrario e una tabella di stringhe. Non c'e' uno schema pubblico, quindi si legge per struttura,
con controlli:
  * ogni gara inizia con il tipo BandoAlboFornitoriDto seguito da AlboDto; il numero di inizi
    trovati deve essere uguale alla dimensione della lista dichiarata;
  * dentro ogni gara, la prima coppia di date consecutive (dopo l'anno 2000) PRIMA delle date della
    procedura (inserimento e termine, a tre posti di distanza) e' fine e inizio della
    pubblicazione (campi in ordine alfabetico: dataFineBando, dataInizioBando); la fine, alle 00:00
    ora italiana, e' la scadenza mostrata dall'albo. Se l'albo non ha la fine pubblicazione (AO
    Cannizzaro: solo l'inizio), la scadenza e' la seconda data della procedura, con la nota "da
    verificare" in descrizione;
  * l'oggetto e' il primo testo lungo negli ultimi campi della gara, il riferimento (es. "SP/337/2026")
    la prima stringa breve dopo l'oggetto; l'identificativo e' il Long che precede le date;
  * i CPV non si usano: nella lista numerica un intero qualsiasi puo' coincidere con l'indice di una
    stringa, e sulla risposta di Ragusa una gara di lampade LED "aveva" anche il CPV di un mezzo per
    interventi paramedici. Filtro testuale, come per Maggioli.
Se un controllo non torna la fonte da' errore, invece di inventare dati.

DATI PERSONALI: la risposta contiene molto piu' dell'elenco (nomi, email, impronte di password degli
utenti dell'ente; in quella di Catania anche quelle che sembrano password in chiaro). Si legge tutto
in memoria e non si salva mai la risposta: il campione contiene solo i campi letti (riferimento,
oggetto, date); in caso di errore solo i conteggi.

Niente link alla singola gara (il dettaglio e' un pannello, l'indirizzo non cambia): il link porta
all'albo dell'ente e la descrizione riporta il riferimento con cui ritrovarla.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .common import Filtro, Opportunita, log, sessione_http

CAMPIONI = Path("state/campioni")
ERRORI: list[str] = []
ESCLUSI: set[str] = set()

BASE = "https://app.albofornitori.it/alboeproc/"
SERVIZIO = BASE + "alboeproc/servizioGarePubblico"
ROMA = ZoneInfo("Europe/Rome")
ALFABETO = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789$_"
RE_CPV = re.compile(r"^\d{8}-\d$")
RE_CLASSE = re.compile(r"^(com|java|javax)\.")
ANNO_2000 = 946684800000   # millisecondi

# Corpo della richiesta, copiato dal browser (23/11/2026). {base} = BASE + "alboeproc/" (module base GWT; con il solo BASE il server risponde 500), {nome_forte}, {albo}, {scadute}.
# {limite} = quante gare per pagina (PagingLoadConfigBean: campi in ordine alfabetico limit, offset,
# sortInfo). Il browser chiede 10: la prima richiesta e' identica alla sua; solo se il totale
# dichiarato e' maggiore se ne fa una seconda con un limite piu' alto (AO Cannizzaro: 17 in corso).
PAYLOAD = ("7|2|14|{base}|{nome_forte}|com.csamed.devlibsgxt.shared.dto.ExtRpcToken/3539413198|{albo}|"
           "com.csamed.alboeproc.client.eproc.interfacciagare.servizi.ServizioGarePubblico|"
           "filtraListaBandiAlboFornitori|com.sencha.gxt.data.shared.loader.PagingLoadConfig|java.util.List|"
           "java.lang.Boolean/476441737|com.sencha.gxt.data.shared.loader.PagingLoadConfigBean/38458988|"
           "java.util.ArrayList/4159755760|com.sencha.gxt.data.shared.SortInfoBean/1820471044|"
           "com.sencha.gxt.data.shared.SortDir/317895740|dataInizioBando|1|2|3|4|5|6|3|7|8|9|10|{limite}|0|11|1|12|"
           "13|1|14|11|0|9|{scadute}|")


class StrutturaCambiata(RuntimeError):
    pass


def _long(s: str) -> int:
    v = 0
    for c in s:
        v = v * 64 + ALFABETO.index(c)
    return v - (1 << 64) if v >= 1 << 63 else v


def _data_roma(ms: int) -> date:
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=ms)).astimezone(ROMA).date()


def _testo_lungo(s: str) -> bool:
    s = s.strip()
    return (len(s) >= 25 and " " in s and "@" not in s and not s.startswith(("http", "com.", "java."))
            and not re.match(r"^\d{3} - ", s) and not s.lower().startswith("procedura negoziata (per procedure"))


def leggi_risposta(testo: str) -> tuple[list[dict], int | None, list[str]]:
    """Gare lette dalla risposta GWT-RPC, totale dichiarato e gare non riconosciute (\"gara N: motivo\").
    Solleva StrutturaCambiata se non torna il conteggio delle gare o se non se ne riconosce nessuna.
    Dal 24/11/2026 una gara con oggetto o date non riconosciuti non ferma piu' tutto l'albo (AO
    Cannizzaro, gara 7 al primo lancio): si leggono le altre e si segnala. Non restituisce altro della
    risposta."""
    testo = testo.strip()
    if testo.startswith("//EX"):
        raise RuntimeError(f"il servizio ha risposto con un'eccezione ({testo[4:200]})")
    if not testo.startswith("//OK"):
        raise StrutturaCambiata("risposta non GWT-RPC (pagina di errore o di verifica?)")
    try:
        dati = json.loads(testo[4:])
        tabella, r = dati[-3], list(reversed(dati[:-3]))
    except (ValueError, IndexError, TypeError) as e:
        raise StrutturaCambiata(f"risposta GWT-RPC non leggibile ({e})") from e
    if not isinstance(tabella, list) or len(r) < 5:
        raise StrutturaCambiata("risposta GWT-RPC senza tabella delle stringhe")

    def stringa(x):
        return tabella[x - 1] if isinstance(x, int) and not isinstance(x, bool) and 0 < x <= len(tabella) else None

    classi = {s.split("/")[0].rsplit(".", 1)[-1]: i + 1 for i, s in enumerate(tabella) if RE_CLASSE.match(s)}
    if "BandoAlboFornitoriDto" not in classi:
        if len(r) > 4 and r[2] == 0:
            return [], 0                      # nessuna gara: la tabella non ha la classe
        raise StrutturaCambiata("classe BandoAlboFornitoriDto assente dalla risposta")
    ib, ia = classi["BandoAlboFornitoriDto"], classi.get("AlboDto")
    totale = r[2] if isinstance(r[2], int) else None
    dimensione = r[4] if isinstance(r[4], int) else None
    if ia is None or "Date" not in classi:
        raise StrutturaCambiata("classi AlboDto o Date assenti dalla risposta")
    idata, ilong = classi["Date"], classi.get("Long")
    # Ogni gara inizia con il suo tipo seguito dall'AlboDto (sulle tre risposte viste anche dopo la
    # prima). Il numero di inizi deve tornare con la dimensione della lista.
    inizi = [p for p in range(len(r) - 1) if r[p] == ib and r[p + 1] == ia]
    if dimensione is not None and len(inizi) != dimensione:
        raise StrutturaCambiata(f"{len(inizi)} gare riconosciute, {dimensione} dichiarate nella lista")
    gare, non_lette = [], []
    for k, s in enumerate(inizi):
        e = inizi[k + 1] if k + 1 < len(inizi) else len(r)
        fine = inizio = id_gara = None
        nota = None
        valida = lambda q: isinstance(r[q + 1], str) and _long(r[q + 1]) > ANNO_2000

        def id_prima(q):
            lunghi = [x for x in range(max(s, q - 14), q) if r[x] == ilong and isinstance(r[x + 1], str)]
            return _long(r[lunghi[-1] + 1]) if lunghi else None
        # Date della procedura (GaraDto): inserimento e, tre posti dopo, il termine o l'apertura
        # (Ragusa 509/512, Cannizzaro 447/450 e 497/500). Le date della pubblicazione del bando vengono
        # prima; quelle dopo appartengono ad altri oggetti (Cannizzaro, gara 6: la coppia 14/05-08/05
        # era di un'altra procedura, "colonne Olympus").
        g = next((q for q in range(s, e - 4) if r[q] == idata and r[q + 3] == idata and valida(q) and valida(q + 3)
                  and _long(r[q + 4]) >= _long(r[q + 1])), None)
        limite = g if g is not None else e - 3
        for p in range(s, limite):
            if r[p] == idata and r[p + 2] == idata and valida(p) and valida(p + 2):
                fine, inizio = _data_roma(_long(r[p + 1])), _data_roma(_long(r[p + 3]))
                id_gara = id_prima(p)
                break
        if fine is None and g is not None:
            # Albo senza fine pubblicazione (AO Cannizzaro, 24/11/2026: dataFineBando vuota, solo l'inizio).
            # Scadenza dalla procedura; pubblicazione dalla prima data del bando (dopo quella dell'albo,
            # che sta nei primi token: Cannizzaro 211, Ragusa 226; la data dell'albo e' a 14).
            fine = _data_roma(_long(r[g + 4]))
            sole = [q for q in range(s + 30, g) if r[q] == idata and valida(q)]
            if sole:
                inizio = _data_roma(_long(r[sole[0] + 1]))
                id_gara = id_prima(sole[0])
            nota = "scadenza presa dalla procedura (l'albo non indica la fine pubblicazione): da verificare"
        coda = range(max(s, e - 18), e)
        titolo = riferimento = None
        for p in coda:
            t = stringa(r[p])
            if t and titolo is None and _testo_lungo(t):
                titolo = " ".join(t.split())
            elif t and titolo is not None and riferimento is None and not _testo_lungo(t) \
                    and not RE_CLASSE.match(t) and len(t.strip()) <= 40:
                riferimento = t.strip()
        if not titolo or not fine:
            mancano = ("oggetto e date non riconosciuti" if not titolo and not fine else
                       "oggetto non riconosciuto" if not titolo else "date non riconosciute")
            non_lette.append(f"gara {k + 1} di {len(inizi)}: {mancano}")
            continue
        gare.append({"id": id_gara, "riferimento": riferimento, "titolo": titolo, "scadenza": fine.isoformat(),
                     "pubblicazione": inizio.isoformat() if inizio else None, "nota": nota})
    if inizi and not gare:
        raise StrutturaCambiata("nessuna gara riconosciuta (" + "; ".join(non_lette) + ")")
    return gare, totale, non_lette


def _tipo(titolo: str) -> str:
    t = titolo.lower()
    if "consultazion" in t:
        return "consultazione di mercato"
    if re.search(r"manifestazion|indagine\s+(conoscitiva\s+)?di\s+mercato|avviso\s+esplorativo", t):
        return "avviso esplorativo"
    return "bando"


def _salva_campione(sito: dict, contenuto: dict) -> None:
    CAMPIONI.mkdir(parents=True, exist_ok=True)
    (CAMPIONI / f"{sito['sigla']}.json").write_text(json.dumps(contenuto, ensure_ascii=False, indent=1),
                                                     encoding="utf-8")


def _leggi_sito(sito: dict, filtro: Filtro) -> list[Opportunita]:
    sess = sessione_http()
    sess.mount("https://", HTTPAdapter(max_retries=Retry(
        total=int(sito.get("tentativi", 1)), backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET", "POST"}))))
    attesa = (float(sito.get("timeout_connessione", 20)), 60)
    pagina_albo = BASE + sito["albo"]
    r = sess.get(pagina_albo, timeout=attesa)          # sessione anonima (cookie)
    r.raise_for_status()
    def chiedi(limite: int) -> str:
        corpo = PAYLOAD.format(base=BASE + "alboeproc/", nome_forte=sito["nome_forte"], albo=sito["albo"],
                               scadute=0, limite=limite)
        risp = sess.post(SERVIZIO, data=corpo.encode("utf-8"), timeout=attesa, headers={
            "Content-Type": "text/x-gwt-rpc; charset=UTF-8",
            "X-GWT-Permutation": sito["permutazione"],
            "X-GWT-Module-Base": BASE + "alboeproc/",
            "Referer": pagina_albo, "Origin": "https://app.albofornitori.it"})
        risp.raise_for_status()
        return risp.content.decode("utf-8", errors="replace")

    testo = chiedi(10)
    try:
        gare, totale, non_lette = leggi_risposta(testo)
    except Exception as e:
        # Mai la risposta grezza (dati personali): solo cosa non torna.
        _salva_campione(sito, {"errore": str(e), "lunghezza_risposta": len(testo),
                               "inizio": testo[:4]})
        if "IncompatibleRemoteService" in str(e) or "configInvalide" in str(e):
            raise RuntimeError(f"{sito['nome']}: {e}. Se Net4market ha aggiornato l'applicazione, vanno "
                               "ricopiati da F12 'permutazione' (X-GWT-Permutation) e 'nome_forte' "
                               "(seconda voce del corpo della richiesta)") from e
        raise RuntimeError(f"{sito['nome']}: {e}") from e
    if totale is not None and totale > len(gare) + len(non_lette):
        # Seconda richiesta con piu' gare per pagina; se il server non la accetta si tiene la prima.
        try:
            g2, t2, n2 = leggi_risposta(chiedi(min(int(totale), 100)))
            if len(g2) + len(n2) > len(gare) + len(non_lette):
                gare, totale, non_lette = g2, t2, n2
        except Exception as e:
            log.warning("Net4market %s: richiesta con %s gare per pagina non riuscita (%s), tengo le prime 10",
                        sito["nome"], min(int(totale), 100), str(e)[:120])
    _salva_campione(sito, {"totale_dichiarato": totale, "gare": gare, "non_lette": non_lette})
    if non_lette:
        ERRORI.append(f"{sito['nome']}: " + "; ".join(non_lette) + f" (lette le altre {len(gare)}: dati parziali)")
    elif totale is not None and totale > len(gare) + len(non_lette):
        ERRORI.append(f"{sito['nome']}: lette {len(gare)} gare su {totale} in corso dichiarate (dati parziali)")

    escl = re.compile("|".join(f"(?:{p})" for p in sito.get("escludi_titoli_regex") or []) or r"(?!)", re.IGNORECASE)
    limite_vecchie = (date.today() - timedelta(days=int(sito.get("scadute_max_giorni", 30)))).isoformat()
    risultati, escluse, vecchie = [], 0, 0
    for g in gare:
        if g["id"] is not None:
            ident = f"{sito['sigla']}:{g['id']}"
        else:   # identificativo non trovato: impronta di oggetto e date (stabile tra un'esecuzione e l'altra)
            impronta = hashlib.sha1(f"{g['titolo']}|{g['pubblicazione']}|{g['scadenza']}".encode()).hexdigest()[:12]
            ident = f"{sito['sigla']}:{impronta}"
        if escl.search(g["titolo"]):
            escluse += 1
            ESCLUSI.add(ident)
            continue
        if g["scadenza"] < limite_vecchie:
            # "In corso" con la fine pubblicazione passata da tempo (Catania: gare del 2023 ancora "Indetta").
            vecchie += 1
            continue
        motivo = filtro.valuta_testo(g["titolo"], sito["ente"], "")
        if not motivo:
            continue
        risultati.append(Opportunita(
            id=ident, fonti=[sito["nome_fonte"]], titolo=g["titolo"], ente=sito["ente"],
            scadenza=g["scadenza"], pubblicazione=g["pubblicazione"], tipo=_tipo(g["titolo"]),
            link=sito.get("link") or pagina_albo, regione=sito.get("regione"), motivo_match=motivo,
            descrizione="; ".join(x for x in (
                f"Riferimento sull'albo Net4market: {g['riferimento']}" if g["riferimento"] else "",
                g.get("nota") or "") if x) or None))
    log.info("Net4market %s: %d gare lette (su %s dichiarate), %d escluse da regole esplicite, "
             "%d con la pubblicazione chiusa da oltre 30 giorni, %d pertinenti",
             sito["nome"], len(gare), totale, escluse, vecchie, len(risultati))
    return risultati


def raccogli(cfg: dict, filtro: Filtro, sess=None) -> list[Opportunita]:
    risultati: list[Opportunita] = []
    errori: list[str] = []
    siti = [s for s in (cfg.get("net4market") or {}).get("siti", []) if s.get("abilitato", True)]
    for sito in siti:
        try:
            risultati.extend(_leggi_sito(sito, filtro))
        except requests.ConnectionError as e:
            log.warning("Net4market %s: connessione non riuscita (%s)", sito["nome"], type(e).__name__)
            errori.append(f"{sito['nome']}: connessione non riuscita ({type(e).__name__}), il sito non "
                          "risponde ai server di GitHub")
        except Exception as e:
            log.exception("Net4market %s: errore", sito["nome"])
            errori.append(str(e) if str(e).startswith(sito["nome"]) else f"{sito['nome']}: {e}")
    if errori and len(errori) == len(siti):
        raise RuntimeError("; ".join(errori))
    ERRORI.extend(errori)
    return risultati
