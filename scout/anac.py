"""Raccolta gare dagli open data ANAC (dataset CIG, pubblicati mensilmente).

Il portale dati.anticorruzione.it espone i dataset tramite API CKAN. Per ogni
anno esiste un dataset (es. "cig-2026") con un file per mese. Lo script:
  1. scopre i file mensili disponibili,
  2. elabora solo quelli nuovi o modificati (stato in state/anac.json),
  3. legge il CSV in streaming, filtra le righe pertinenti e le raggruppa per gara.

Nota: i dati ANAC arrivano con qualche settimana di ritardo, quindi sono più
utili per intercettare gare con scadenze lunghe e per lo storico che come
"allerta" immediata (per quella c'è TED, e in seguito le piattaforme regionali).
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import tempfile
import zipfile
from datetime import date
from pathlib import Path

from .common import Filtro, Opportunita, log, normalizza_cpv, parse_data, parse_importo

STATO = Path("state/anac.json")

COLONNE = {
    "cig": ["cig"],
    "numero_gara": ["numero_gara", "id_gara"],
    "oggetto_gara": ["oggetto_gara"],
    "oggetto_lotto": ["oggetto_lotto"],
    "importo_gara": ["importo_complessivo_gara"],
    "importo_lotto": ["importo_lotto"],
    "ente": ["denominazione_amministrazione_appaltante", "denominazione_sa"],
    "scadenza": ["data_scadenza_offerta"],
    "pubblicazione": ["data_pubblicazione"],
    "cpv": ["cod_cpv", "cpv"],
    "regione": ["sezione_regionale"],
    "tipo": ["tipo_scelta_contraente"],
}
ESSENZIALI = ("cig", "ente", "cpv")
ERRORI: list[str] = []  # errori non bloccanti dell'ultima esecuzione

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def _carica_stato() -> dict:
    try:
        return json.loads(STATO.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"elaborati": {}}


def _salva_stato(stato: dict) -> None:
    STATO.parent.mkdir(parents=True, exist_ok=True)
    STATO.write_text(json.dumps(stato, indent=2, ensure_ascii=False), encoding="utf-8")


def _scopri_risorse(acfg: dict, sess) -> list[dict]:
    oggi = date.today()
    anni = [oggi.year] + ([oggi.year - 1] if oggi.month <= 3 else [])
    risorse = []
    for anno in anni:
        dataset = acfg["dataset_pattern"].format(anno=anno)
        url = f"{acfg['base_url'].rstrip('/')}/api/3/action/package_show"
        r = sess.get(url, params={"id": dataset}, timeout=60)
        if r.status_code == 404:
            log.warning("ANAC: dataset %s non trovato", dataset)
            continue
        r.raise_for_status()
        for res in r.json().get("result", {}).get("resources", []):
            nome = f"{res.get('name') or ''} {res.get('url') or ''}".lower()
            formato = (res.get("format") or "").lower()
            if "csv" in nome or formato == "csv" or ("zip" in formato and "csv" in nome):
                risorse.append({
                    "url": res["url"],
                    "nome": res.get("name") or res["url"].rsplit("/", 1)[-1],
                    "versione": res.get("last_modified") or res.get("created") or "",
                })
    risorse.sort(key=lambda x: x["url"], reverse=True)
    return risorse


def _scarica(sess, url: str) -> str:
    fd, percorso = tempfile.mkstemp(suffix=".dat")
    with os.fdopen(fd, "wb") as f, sess.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        for blocco in r.iter_content(1 << 20):
            f.write(blocco)
    return percorso


def _apri_csv(percorso: str):
    """Restituisce un file di testo sul CSV, gestendo sia zip sia CSV diretti."""
    if zipfile.is_zipfile(percorso):
        z = zipfile.ZipFile(percorso)
        nomi = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not nomi:
            raise ValueError("lo zip non contiene CSV")
        grezzo = z.open(nomi[0])
    else:
        grezzo = open(percorso, "rb")
    return io.TextIOWrapper(grezzo, encoding="utf-8-sig", errors="replace", newline="")


def _mappa_colonne(intestazione: list[str]) -> dict[str, str]:
    norm = {h.strip().lower(): h for h in intestazione}
    mappa = {}
    for chiave, candidati in COLONNE.items():
        for c in candidati:
            if c in norm:
                mappa[chiave] = norm[c]
                break
    mancanti = [k for k in ESSENZIALI if k not in mappa]
    if mancanti:
        raise ValueError(f"colonne essenziali mancanti {mancanti}; intestazione: {intestazione[:40]}")
    return mappa


def _elabora_file(percorso: str, filtro: Filtro, acfg: dict) -> list[Opportunita]:
    oggi = date.today().isoformat()
    gare: dict[str, Opportunita] = {}
    with _apri_csv(percorso) as f:
        prima = f.readline()
        sep = ";" if prima.count(";") >= prima.count(",") else ","
        intestazione = next(csv.reader([prima], delimiter=sep))
        m = _mappa_colonne(intestazione)
        lettore = csv.DictReader(f, fieldnames=intestazione, delimiter=sep)

        def v(riga, k):
            col = m.get(k)
            return (riga.get(col) or "").strip() if col else ""

        righe = 0
        for riga in lettore:
            righe += 1
            titolo = v(riga, "oggetto_gara") or v(riga, "oggetto_lotto")
            ente = v(riga, "ente")
            cpv = v(riga, "cpv")
            motivo = filtro.valuta(f"{titolo} {v(riga, 'oggetto_lotto')}", ente, [cpv])
            if not motivo:
                continue

            scadenza = parse_data(v(riga, "scadenza"))
            if acfg.get("solo_aperte", True):
                if scadenza is None and not acfg.get("includi_senza_scadenza", False):
                    continue
                if scadenza is not None and scadenza < oggi:
                    continue

            cig = v(riga, "cig")
            chiave = v(riga, "numero_gara") or cig
            opp = gare.get(chiave)
            if opp is None:
                opp = gare[chiave] = Opportunita(
                    id=f"anac:{chiave}",
                    fonti=["ANAC"],
                    titolo=titolo,
                    ente=ente,
                    scadenza=scadenza,
                    pubblicazione=parse_data(v(riga, "pubblicazione")),
                    importo=parse_importo(v(riga, "importo_gara")),
                    regione=v(riga, "regione") or None,
                    tipo=v(riga, "tipo") or None,
                    motivo_match=motivo,
                )
                opp._somma_lotti = 0.0  # type: ignore[attr-defined]
            if cig and cig not in opp.cig:
                opp.cig.append(cig)
            cpv_n = normalizza_cpv(cpv)
            if cpv_n and cpv_n not in opp.cpv:
                opp.cpv.append(cpv_n)
            if scadenza and (opp.scadenza is None or scadenza < opp.scadenza):
                opp.scadenza = scadenza
            il = parse_importo(v(riga, "importo_lotto"))
            if il:
                opp._somma_lotti += il  # type: ignore[attr-defined]

    for opp in gare.values():
        somma = getattr(opp, "_somma_lotti", 0.0)
        if opp.importo is None and somma:
            opp.importo = somma
        if hasattr(opp, "_somma_lotti"):
            del opp._somma_lotti
    log.info("ANAC: %d righe lette, %d gare pertinenti", righe, len(gare))
    return list(gare.values())


def raccogli(cfg: dict, filtro: Filtro, sess) -> list[Opportunita]:
    acfg = cfg["anac"]
    stato = _carica_stato()
    elaborati: dict = stato.setdefault("elaborati", {})

    risorse = _scopri_risorse(acfg, sess)
    log.info("ANAC: %d file mensili disponibili", len(risorse))
    if not elaborati:  # primo avvio: solo gli ultimi N mesi
        risorse = risorse[: int(acfg.get("mesi_da_elaborare", 2))]
    da_fare = [r for r in risorse if elaborati.get(r["url"]) != r["versione"]]
    # Non riprocessare file molto vecchi rimasti fuori dalla finestra iniziale
    da_fare = da_fare[: max(int(acfg.get("mesi_da_elaborare", 2)), 3)]

    risultati: list[Opportunita] = []
    for res in da_fare:
        log.info("ANAC: elaboro %s", res["nome"])
        percorso = None
        try:
            percorso = _scarica(sess, res["url"])
            risultati.extend(_elabora_file(percorso, filtro, acfg))
            elaborati[res["url"]] = res["versione"]
        except Exception as e:  # un file difettoso non blocca gli altri
            log.exception("ANAC: errore su %s", res["nome"])
            ERRORI.append(f"{res['nome']}: {e}")
        finally:
            if percorso and os.path.exists(percorso):
                os.remove(percorso)

    _salva_stato(stato)
    return risultati
