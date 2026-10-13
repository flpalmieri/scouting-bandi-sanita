"""Esegue tutte le fonti abilitate, unisce i risultati con lo storico e salva.

Uso:  python -m scout.main [--config config.yaml]
Output:
  data/opportunita.json   elenco completo (letto dalla dashboard)
  data/opportunita.csv    stesso elenco, per Excel
  data/ultima_esecuzione.json   riepilogo e eventuali errori
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from dataclasses import asdict, fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import anac, portali_rss, ted
from .common import (Filtro, Opportunita, chiave_testo, log, pulisci_titolo,
                     sessione_http)

DATA = Path("data")
FONTI = {"ted": ted.raccogli, "anac": anac.raccogli, "portali_rss": portali_rss.raccogli}
NOMI_CAMPI = {f.name for f in fields(Opportunita)}


def carica_storico() -> dict[str, Opportunita]:
    try:
        grezzo = json.loads((DATA / "opportunita.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    out = {}
    for d in grezzo.get("opportunita", []):
        d = {k: v for k, v in d.items() if k in NOMI_CAMPI}
        out[d["id"]] = Opportunita(**d)
    return out


def unisci(storico: dict[str, Opportunita], nuove: list[Opportunita], oggi: str) -> dict[str, Opportunita]:
    for o in nuove:
        vecchia = storico.get(o.id)
        o.primo_rilevamento = vecchia.primo_rilevamento if vecchia and vecchia.primo_rilevamento else oggi
        o.ultimo_rilevamento = oggi
        if vecchia:  # conserva i dati arrivati da altre fonti in esecuzioni precedenti
            o.fonti = sorted(set(o.fonti) | set(vecchia.fonti))
            o.cig = sorted(set(o.cig) | set(vecchia.cig))
            o.cpv = sorted(set(o.cpv) | set(vecchia.cpv))
            o.regione = o.regione or vecchia.regione
            o.importo = o.importo or vecchia.importo
            o.scadenza = o.scadenza or vecchia.scadenza
        storico[o.id] = o
    return storico


def deduplica(tutte: dict[str, Opportunita]) -> dict[str, Opportunita]:
    """Fonde la stessa gara vista su fonti diverse (stesso ente + stesso oggetto)."""
    per_chiave: dict[str, Opportunita] = {}
    ordine = sorted(tutte.values(), key=lambda o: (0 if "TED" in o.fonti else 1, o.id))
    for o in ordine:
        k = chiave_testo(o.ente)[:40] + "|" + chiave_testo(o.titolo)
        principale = per_chiave.get(k)
        if principale is None or principale.id == o.id:
            per_chiave[k] = o
            continue
        # fusione: si tiene il record TED (ha il link) e si aggiungono i dati ANAC
        principale.fonti = sorted(set(principale.fonti) | set(o.fonti))
        principale.cig = sorted(set(principale.cig) | set(o.cig))
        principale.cpv = sorted(set(principale.cpv) | set(o.cpv))
        principale.scadenza = principale.scadenza or o.scadenza
        principale.importo = principale.importo or o.importo
        principale.regione = principale.regione or o.regione
        principale.link = principale.link or o.link
        if o.primo_rilevamento and (not principale.primo_rilevamento
                                    or o.primo_rilevamento < principale.primo_rilevamento):
            principale.primo_rilevamento = o.primo_rilevamento
    return {o.id: o for o in per_chiave.values()}


def rivaluta(tutte: dict[str, Opportunita], filtro: Filtro) -> dict[str, Opportunita]:
    """Riapplica i filtri attuali a tutto lo storico, così una modifica a
    config.yaml ripulisce anche le opportunità raccolte in passato."""
    tenute = {}
    for k, o in tutte.items():
        o.titolo = pulisci_titolo(o.titolo)
        motivo = filtro.valuta_opportunita(o)
        if motivo:
            o.motivo_match = motivo
            tenute[k] = o
    return tenute


def pota(tutte: dict[str, Opportunita], cfg: dict) -> dict[str, Opportunita]:
    oggi = date.today()
    lim_scad = (oggi - timedelta(days=int(cfg.get("giorni_conservazione_scadute", 30)))).isoformat()
    lim_senza = (oggi - timedelta(days=int(cfg.get("giorni_conservazione_senza_scadenza", 120)))).isoformat()
    tenute = {}
    for k, o in tutte.items():
        if o.scadenza and o.scadenza < lim_scad:
            continue
        if not o.scadenza and (o.primo_rilevamento or "9999") < lim_senza:
            continue
        tenute[k] = o
    return tenute


def salva(tutte: dict[str, Opportunita], riepilogo: dict) -> None:
    DATA.mkdir(exist_ok=True)
    elenco = sorted(tutte.values(), key=lambda o: (o.scadenza or "9999-12-31", o.ente))
    payload = {
        "aggiornato_il": riepilogo["eseguito_il"],
        "totale": len(elenco),
        "opportunita": [asdict(o) for o in elenco],
    }
    (DATA / "opportunita.json").write_text(
        json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")

    with open(DATA / "opportunita.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        colonne = [f.name for f in fields(Opportunita)]
        w.writerow(colonne)
        for o in elenco:
            r = asdict(o)
            w.writerow([", ".join(r[c]) if isinstance(r[c], list) else ("" if r[c] is None else r[c])
                        for c in colonne])

    (DATA / "ultima_esecuzione.json").write_text(
        json.dumps(riepilogo, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    filtro = Filtro(cfg)
    sess = sessione_http()
    oggi = date.today().isoformat()

    riepilogo = {"eseguito_il": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "fonti": {}, "errori": []}
    nuove: list[Opportunita] = []
    fallite = abilitate = 0

    for nome, funzione in FONTI.items():
        if not cfg.get(nome, {}).get("abilitato", False):
            continue
        abilitate += 1
        try:
            trovate = funzione(cfg, filtro, sess)
            nuove.extend(trovate)
            riepilogo["fonti"][nome] = {"esito": "ok", "pertinenti": len(trovate)}
        except Exception as e:
            fallite += 1
            log.exception("Fonte %s fallita", nome)
            riepilogo["fonti"][nome] = {"esito": "errore", "pertinenti": 0}
            riepilogo["errori"].append(f"{nome}: {e}")
    riepilogo["errori"].extend(f"anac: {e}" for e in anac.ERRORI)
    riepilogo["errori"].extend(f"portali_rss: {e}" for e in portali_rss.ERRORI)

    storico = carica_storico()
    prima = set(storico)
    tutte = pota(rivaluta(deduplica(unisci(storico, nuove, oggi)), filtro), cfg)
    riepilogo["nuove_opportunita"] = len(set(tutte) - prima)
    riepilogo["totale"] = len(tutte)

    salva(tutte, riepilogo)
    log.info("Fatto: %d opportunità totali, %d nuove", len(tutte), riepilogo["nuove_opportunita"])
    return 1 if abilitate and fallite == abilitate else 0


if __name__ == "__main__":
    sys.exit(main())
