"""Avvisi speciali del Sistema informativo contratti pubblici della Provincia di Bolzano
(bandi-altoadige.it = ausschreibungen-suedtirol.it, software Pleiade di i-Faber), dal 24/11/2026.

La pagina pubblica /notice/special-notice/list (app Angular) legge l'elenco da un servizio JSON
pubblico, senza login ne' cookie:

    POST <host>/notice-api/special-notice/search
    corpo: {"status": "generic", "page": 0, "limit": <limite>}

Risposta: {"totalRows": N, "specialNoticeList": [...]}, una voce per avviso con protocolId
("S001783/2026"), name {it_IT, de_DE}, type, status ("published" o "expired"), publishedAt,
expiredAt (UTC: "2026-12-09T22:59:00.000+00:00" = 09/12 alle 23:59 italiane, verificato
dall'utente dal browser), cig, organizationName e organizationUnitName {it_IT, de_DE} e
"publisher", il nome di chi ha pubblicato: un dato personale che NON si legge e non si salva.
Il servizio restituisce tutti gli avvisi dal 12/2021 (1.623 al 24/11/2026, circa 2 MB) in una
richiesta; le voci in corso arrivano fino alla posizione 300 circa, quindi si chiede tutto e si
filtra qui. Se totalRows supera le voci ricevute l'elenco e' parziale (errore \"dati parziali\").

Tipi (etichette del filtro della pagina): market-consultation \"Consultazione di mercato\",
contract-award \"Affidamento contratti\", professional-list \"Elenco professionisti\", other-notice
\"Altri avvisi\", tender-notices-nspr \"Bandi e avvisi non soggetti alla normativa degli appalti\".
Si leggono i tipi in `tipi` (default: solo le consultazioni di mercato: sull'intero storico gli
altri tipi non portano voci IT sanitarie). Infungibilita' e trasparenza preventiva non hanno un
tipo proprio: compaiono, se ci sono, nel titolo delle consultazioni.

Si tengono le voci \"published\"; le \"expired\" si ignorano (quelle gia' nello storico restano
fino alla potatura); uno stato diverso (es. una voce annullata) mette la voce in ESCLUSI e main.py
la toglie dallo storico. Le voci senza scadenza pubblicate da piu' di `pubblicazione_max_giorni`
(default 120) si ignorano: sul portale restano \"published\" per anni.

Ente: organizationName in italiano, seguito dall'unita' organizzativa se diversa
(\"Azienda Sanitaria dell'Alto Adige - Comprensorio sanitario di Merano-…\"), come nei nomi TED.
L'unita' sta nell'ente e non nella descrizione, perche' il suo nome non deve contare come parola
IT (\"Ripartizione 9 - Informatica\" della Provincia). Niente CPV: filtro testuale.

Link: <host>/notice/special-notice/<protocollo con - al posto di />/resume (verificato
dall'utente: in finestra anonima apre il dettaglio).

Opzioni (sezione bandi_altoadige.siti di config.yaml):
  nome, nome_fonte, sigla, regione, host  - come le altre fonti.
  tipi                    - valori di type da leggere (default [market-consultation]).
  limite                  - voci chieste (default 3000).
  escludi_titoli_regex    - voci da scartare in base al titolo (finiscono in ESCLUSI).
  enti_rinomina           - {\"nome sul portale\": \"nome da usare\"}.
  pubblicazione_max_giorni - vedi sopra (default 120).
  timeout_connessione, tentativi - come per le altre fonti (default 20 e 1).
Campione: state/campioni/<sigla>.json con i soli campi letti delle voci dei tipi letti e in corso
(nessun dato personale); la risposta non JSON si salva in <sigla>_errore.html (primi 20 kB).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .common import Filtro, Opportunita, log, sessione_http

CAMPIONI = Path("state/campioni")
ERRORI: list[str] = []
ESCLUSI: set[str] = set()   # id delle voci scartate da una regola esplicita o non piu' pubblicate
ROMA = ZoneInfo("Europe/Rome")
RE_CIG = re.compile(r"\b[0-9A-Z]{10}\b")
STATI_IGNORATI = {"expired"}   # conclusi: non si leggono, ma restano nello storico fino alla potatura
TIPI_ETICHETTA = {
    "market-consultation": "Consultazione di mercato",
    "contract-award": "Affidamento contratti",
    "professional-list": "Elenco professionisti",
    "other-notice": "Altri avvisi",
    "tender-notices-nspr": "Bandi e avvisi non soggetti alla normativa degli appalti",
}


def _lingua(valore, lingua: str = "it_IT") -> str:
    """Testo di un campo bilingue {it_IT, de_DE}: l'italiano, altrimenti il tedesco."""
    if isinstance(valore, dict):
        for k in (lingua, "it_IT", "de_DE"):
            t = valore.get(k)
            if t and str(t).strip():
                return " ".join(str(t).split())
        return ""
    return " ".join(str(valore or "").split())


def data_roma(valore) -> str | None:
    """"2026-12-09T22:59:00.000+00:00" (UTC) -> "2026-12-09" (data italiana)."""
    if not valore:
        return None
    s = str(valore).strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        return d.date().isoformat()
    return d.astimezone(ROMA).date().isoformat()


def leggi_voce(v: dict) -> dict:
    """Solo i campi usati (mai \"publisher\")."""
    org = _lingua(v.get("organizationName"))
    unita = _lingua(v.get("organizationUnitName"))
    return {
        "protocollo": str(v.get("protocolId") or "").strip(),
        "tipo": str(v.get("type") or "").strip(),
        "stato": str(v.get("status") or "").strip(),
        "titolo": _lingua(v.get("name")),
        "ente": org,
        "unita": unita,
        "pubblicazione": data_roma(v.get("publishedAt")),
        "scadenza": data_roma(v.get("expiredAt")),
        "cig": sorted(set(RE_CIG.findall(str(v.get("cig") or "").upper()))),
    }


def _ente(voce: dict, sito: dict) -> str:
    org, unita = voce["ente"], voce["unita"]
    nome = org if not unita or unita.strip().lower() == org.strip().lower() else f"{org} - {unita}"
    nome = nome or unita
    mappa = {k.strip().lower(): v for k, v in (sito.get("enti_rinomina") or {}).items()}
    return mappa.get(nome.strip().lower(), nome)


def _tipo(voce: dict) -> str:
    if voce["tipo"] == "market-consultation":
        return "consultazione di mercato"
    t = voce["titolo"].lower()
    if "consultazion" in t:
        return "consultazione di mercato"
    return "avviso esplorativo"


def _salva(nome: str, contenuto) -> None:
    CAMPIONI.mkdir(parents=True, exist_ok=True)
    (CAMPIONI / f"{nome}.json").write_text(json.dumps(contenuto, ensure_ascii=False, indent=1),
                                           encoding="utf-8")


def _sessione(sito: dict) -> requests.Session:
    sess = sessione_http()   # User-Agent del progetto
    sess.mount("https://", HTTPAdapter(max_retries=Retry(
        total=int(sito.get("tentativi", 1)), backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"POST"}))))
    return sess


def interpreta(dati, sito: dict, filtro: Filtro, oggi: date | None = None) -> tuple[list[Opportunita], dict]:
    """Dalla risposta JSON alle opportunita'. Restituisce anche i conteggi per il log."""
    oggi = oggi or date.today()
    if not isinstance(dati, dict) or not isinstance(dati.get("specialNoticeList"), list):
        raise RuntimeError("risposta senza specialNoticeList (struttura cambiata?)")
    elenco = dati["specialNoticeList"]
    totale = dati.get("totalRows")
    if not elenco:
        raise RuntimeError("nessun avviso nella risposta (attesi tutti gli avvisi dal 2021)")
    if isinstance(totale, int) and totale > len(elenco):
        ERRORI.append(f"{sito['nome']}: ricevuti {len(elenco)} avvisi su {totale} "
                      f"(alzare 'limite': dati parziali)")
    tipi = set(sito.get("tipi") or ["market-consultation"])
    escl = re.compile("|".join(f"(?:{p})" for p in sito.get("escludi_titoli_regex") or []) or r"(?!)",
                      re.IGNORECASE)
    max_giorni = int(sito.get("pubblicazione_max_giorni", 120))
    host = sito["host"].rstrip("/")
    conti = {"ricevuti": len(elenco), "totale": totale, "in_corso": 0, "esclusi": 0,
             "vecchi_senza_scadenza": 0, "stati_inattesi": {}}
    campione, trovate = [], []
    for grezza in elenco:
        if not isinstance(grezza, dict):
            continue
        v = leggi_voce(grezza)
        if v["tipo"] not in tipi or not v["protocollo"]:
            continue
        ident = f"{sito['sigla']}:{v['protocollo'].replace('/', '-')}"
        if v["stato"] in STATI_IGNORATI:
            continue
        if v["stato"] != "published":
            conti["stati_inattesi"][v["stato"]] = conti["stati_inattesi"].get(v["stato"], 0) + 1
            ESCLUSI.add(ident)
            continue
        conti["in_corso"] += 1
        campione.append(v)
        if escl.search(v["titolo"]):
            conti["esclusi"] += 1
            ESCLUSI.add(ident)
            continue
        if not v["scadenza"] and v["pubblicazione"] and \
                date.fromisoformat(v["pubblicazione"]) < oggi - timedelta(days=max_giorni):
            conti["vecchi_senza_scadenza"] += 1
            ESCLUSI.add(ident)
            continue
        ente = _ente(v, sito)
        descr = f"Avviso speciale: {TIPI_ETICHETTA.get(v['tipo'], v['tipo'])}"
        motivo = filtro.valuta_testo(v["titolo"], ente, descr)
        if not motivo:
            continue
        trovate.append(Opportunita(
            id=ident, fonti=[sito["nome_fonte"]], titolo=v["titolo"], ente=ente,
            scadenza=v["scadenza"], pubblicazione=v["pubblicazione"], cig=v["cig"], tipo=_tipo(v),
            link=f"{host}/notice/special-notice/{v['protocollo'].replace('/', '-')}/resume",
            regione=sito.get("regione"), motivo_match=motivo, descrizione=descr))
    conti["campione"] = campione
    return trovate, conti


def _leggi_sito(sito: dict, filtro: Filtro) -> list[Opportunita]:
    sess = _sessione(sito)
    host = sito["host"].rstrip("/")
    r = sess.post(f"{host}/notice-api/special-notice/search",
                  json={"status": "generic", "page": 0, "limit": int(sito.get("limite", 3000))},
                  headers={"Accept": "application/json, text/plain, */*",
                           "Content-Type": "application/json",
                           "Referer": f"{host}/notice/special-notice/list"},
                  timeout=(float(sito.get("timeout_connessione", 20)), 120))
    r.raise_for_status()
    try:
        dati = r.json()
    except ValueError as e:
        CAMPIONI.mkdir(parents=True, exist_ok=True)
        (CAMPIONI / f"{sito['sigla']}_errore.html").write_bytes(r.content[:20000])
        raise RuntimeError(f"risposta non JSON (vedi state/campioni/{sito['sigla']}_errore.html)") from e
    trovate, conti = interpreta(dati, sito, filtro)
    _salva(sito["sigla"], {"totalRows": conti["totale"], "ricevuti": conti["ricevuti"],
                           "tipi": sorted(sito.get("tipi") or ["market-consultation"]),
                           "voci_in_corso": conti["campione"]})
    if conti["stati_inattesi"]:
        log.info("bandi-altoadige %s: stati non previsti (voci escluse): %s", sito["nome"], conti["stati_inattesi"])
    log.info("bandi-altoadige %s: %d avvisi ricevuti (su %s), %d in corso dei tipi letti, %d esclusi da "
             "regole, %d senza scadenza troppo vecchi, %d pertinenti", sito["nome"], conti["ricevuti"],
             conti["totale"], conti["in_corso"], conti["esclusi"], conti["vecchi_senza_scadenza"], len(trovate))
    return trovate


def raccogli(cfg: dict, filtro: Filtro, sess=None) -> list[Opportunita]:
    risultati: list[Opportunita] = []
    errori: list[str] = []
    siti = [s for s in (cfg.get("bandi_altoadige") or {}).get("siti", []) if s.get("abilitato", True)]
    for sito in siti:
        try:
            risultati.extend(_leggi_sito(sito, filtro))
        except requests.ConnectionError as e:
            log.warning("bandi-altoadige %s: connessione non riuscita (%s)", sito["nome"], type(e).__name__)
            errori.append(f"{sito['nome']}: connessione non riuscita ({type(e).__name__}), il sito non "
                          "risponde ai server di GitHub")
        except Exception as e:
            log.exception("bandi-altoadige %s: errore", sito["nome"])
            errori.append(f"{sito['nome']}: {e}")
    if errori and len(errori) == len(siti):
        raise RuntimeError("; ".join(errori))
    ERRORI.extend(errori)
    return risultati
