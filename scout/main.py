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

from . import (acquistitelematici, anac, centrale_acquisti, jaggaer, maggioli, plone, portali_html,
               portali_rss, sintel, ted)
from .common import (Filtro, Opportunita, chiave_testo, log, pulisci_titolo,
                     sessione_http)

DATA = Path("data")
FONTI = {"ted": ted.raccogli, "anac": anac.raccogli, "portali_rss": portali_rss.raccogli,
         "portali_html": portali_html.raccogli, "acquistitelematici": acquistitelematici.raccogli,
         "sintel": sintel.raccogli, "plone": plone.raccogli,
         "centrale_acquisti": centrale_acquisti.raccogli, "jaggaer": jaggaer.raccogli,
         "maggioli": maggioli.raccogli}
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


def _fondibili(a: Opportunita, b: Opportunita) -> bool:
    """Due record con stessa chiave sono la stessa gara solo se vengono da fonti diverse
    e non hanno CIG diversi. La chiave guarda i primi 90 caratteri del titolo, che nei
    titoli lunghi (es. Sintel: "RICHIESTA DI OFFERTA (RDO) AI SENSI DELL'ART. 50...")
    sono solo formule fisse: due procedure diverse dello stesso ente si confonderebbero.
    Eccezione (20/11/2026): due avvisi TED dello stesso ente con titolo identico per intero
    sono l'avviso originale e la sua modifica (es. proroga del termine: ORMS del San Camillo,
    722510-2026 e 789687-2026) e si fondono."""
    if a.cig and b.cig and not set(a.cig) & set(b.cig):
        return False
    if a.id.split(":", 1)[0] == b.id.split(":", 1)[0]:
        return (a.id.startswith("ted:") and b.id.startswith("ted:")
                and _titolo_intero(a.titolo) == _titolo_intero(b.titolo))
    if not _titoli_compatibili(a.titolo, b.titolo):
        log.info("Deduplica: %s e %s non fusi (stesso inizio, titoli diversi dopo i primi %d caratteri)",
                 a.id, b.id, LUNGHEZZA_CHIAVE)
        return False
    return True


LUNGHEZZA_CHIAVE = 90   # caratteri del titolo normalizzato nella chiave (common.chiave_testo)


def _prefisso_comune(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _titoli_compatibili(a: str, b: str) -> bool:
    """Due titoli con la stessa chiave (primi 90 caratteri) descrivono la stessa procedura? (23/11/2026)
    Se il piu' corto non supera la chiave, si': e' il caso dei titoli brevi di TED e ANAC. Se entrambi
    sono piu' lunghi, il piu' corto deve coincidere con l'inizio dell'altro almeno per il 90% della sua
    lunghezza (un titolo TED puo' essere troncato o avere una coda diversa). Caso reale: le consultazioni
    di infungibilita' della Regione Marche (screening prenatale, GT-SUAM A00251 e TED 795909; NuCDR,
    A00253 e TED 807322) hanno 160 caratteri identici di formula prima dell'oggetto: con la sola chiave
    la NuCDR di GT-SUAM finiva fusa nello screening di TED."""
    ta, tb = _titolo_intero(a), _titolo_intero(b)
    corto = min(len(ta), len(tb))
    if corto <= LUNGHEZZA_CHIAVE:
        return True
    return _prefisso_comune(ta, tb) >= 0.9 * corto


def _titolo_intero(titolo: str) -> str:
    """Titolo normalizzato senza il taglio a 90 caratteri della chiave di deduplica."""
    import re
    import unicodedata
    t = unicodedata.normalize("NFKD", titolo or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", t.lower())


def _freschezza(o: Opportunita) -> tuple[str, str]:
    """Quanto e' recente l'informazione di un record: ultimo rilevamento, poi pubblicazione."""
    return (o.ultimo_rilevamento or "", o.pubblicazione or "")


def deduplica(tutte: dict[str, Opportunita]) -> dict[str, Opportunita]:
    """Fonde la stessa gara vista su fonti diverse (stesso ente + stesso oggetto).
    Scadenza e link vengono dal record rilevato piu' di recente (20/11/2026): la piattaforma
    di gara, letta ogni giorno, riporta proroghe e rettifiche, mentre un avviso TED uscito
    dalla finestra di lettura resta fermo alla data originale. A parita' resta il record TED.
    Poi una seconda passata (_stessa_procedura) fonde i record dello stesso ente con titoli
    diversi ma dati di procedura identici (rettifiche TED con il titolo riscritto)."""
    per_chiave: dict[str, list[Opportunita]] = {}
    tenute: dict[str, Opportunita] = {}
    fonte_dati: dict[str, tuple[str, str]] = {}   # freschezza dei dati di scadenza e link
    ordine = sorted(tutte.values(), key=lambda o: (0 if "TED" in o.fonti else 1, o.id))
    for o in ordine:
        k = chiave_testo(o.ente)[:40] + "|" + chiave_testo(o.titolo)
        candidati = [p for p in per_chiave.get(k, []) if p.id == o.id or _fondibili(p, o)]
        # Con piu' candidati si sceglie quello col titolo piu' simile (prefisso comune piu' lungo).
        principale = max(candidati, key=lambda p: (p.id == o.id, _prefisso_comune(
            _titolo_intero(p.titolo), _titolo_intero(o.titolo))), default=None)
        if principale is None or principale.id == o.id:
            per_chiave.setdefault(k, []).append(o)
            tenute[o.id] = o
            fonte_dati[o.id] = _freschezza(o)
            continue
        _fondi(principale, o, fonte_dati, _freschezza(o))
    return _stessa_procedura(tenute, fonte_dati)


def _fondi(principale: Opportunita, o: Opportunita, fonte_dati: dict, freschezza_o: tuple) -> None:
    """Aggiunge a principale i dati di o. Scadenza e link passano a o se i suoi dati sono
    stati rilevati piu' di recente."""
    if freschezza_o > fonte_dati[principale.id] and (o.scadenza or o.link):
        principale.scadenza = o.scadenza or principale.scadenza
        principale.link = o.link or principale.link
        fonte_dati[principale.id] = freschezza_o
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
    if (o.ultimo_rilevamento or "") > (principale.ultimo_rilevamento or ""):
        principale.ultimo_rilevamento = o.ultimo_rilevamento


def _parole(titolo: str) -> set[str]:
    import re
    import unicodedata
    t = unicodedata.normalize("NFKD", titolo or "").encode("ascii", "ignore").decode().lower()
    return set(re.findall(r"[a-z0-9]{4,}", t))


def _lotti(titolo: str) -> set[str]:
    import re
    return set(re.findall(r"\blott[oi]\W*(?:n\W*)?(\d+)", (titolo or "").lower()))


def _stessi_dati(a: Opportunita, b: Opportunita) -> bool:
    """Stessa procedura con un titolo riscritto (20/11/2026). Caso reale: Fortinet di ASUFC,
    TED 768150-2026 ("PR26INF073 | Procedura aperta...") e 783479-2026 ("PR26INF073 | Lotto 1
    Procedura aperta ... indetta su piattaforma telematica eAppalti SAT FVG..."), il secondo
    fuso con eAppalti SAT: chiavi diverse, quindi due righe in dashboard. Si fondono due record
    dello stesso ente solo se TUTTO coincide: scadenza, importo (al centesimo), almeno un CPV,
    nessun CIG in conflitto, nessun numero di lotto diverso nei titoli, e almeno l'80% delle
    parole del titolo piu' corto presenti nell'altro."""
    if not (a.scadenza and a.scadenza == b.scadenza):
        return False
    if not (a.importo and b.importo and abs(a.importo - b.importo) < 0.005):
        return False
    if not (set(a.cpv) & set(b.cpv)):
        return False
    if a.cig and b.cig and not set(a.cig) & set(b.cig):
        return False
    la, lb = _lotti(a.titolo), _lotti(b.titolo)
    if la and lb and la != lb:
        return False
    pa, pb = _parole(a.titolo), _parole(b.titolo)
    if not pa or not pb:
        return False
    return len(pa & pb) / min(len(pa), len(pb)) >= 0.8


def _stesso_cig(a: Opportunita, b: Opportunita) -> bool:
    """Stessa procedura vista su due fonti con titoli diversi (23/11/2026, Abruzzo): l'elenco di
    areacom.eu ("Intervento di bonifica ... ex Conceria") e GIADA ("PROCEDURA APERTA AI SENSI
    DELL'ART. 71 ... BONIFICA ...") hanno lo stesso CIG (BDAB08FCF9). Il CIG identifica un lotto
    di una sola procedura: due record dello stesso ente (lo controlla il chiamante) da fonti
    diverse con almeno un CIG in comune sono la stessa gara. Dalla stessa fonte non si fondono."""
    if a.id.split(":", 1)[0] == b.id.split(":", 1)[0]:
        return False
    return bool(a.cig and b.cig and set(a.cig) & set(b.cig))


def _stessa_procedura(tenute: dict[str, Opportunita], fonte_dati: dict) -> dict[str, Opportunita]:
    """Seconda passata: fonde i record dello stesso ente rimasti separati perche' il titolo e'
    stato riscritto (rettifiche TED), se i dati della procedura coincidono (_stessi_dati), o
    perche' due fonti scrivono il titolo in modo diverso ma riportano lo stesso CIG (_stesso_cig).
    Resta il record rilevato per primo (stesso id in dashboard); scadenza e link vengono dal
    piu' recente, come nella prima passata. Ogni fusione finisce nel log."""
    per_ente: dict[str, list[Opportunita]] = {}
    ordine = sorted(tenute.values(), key=lambda o: (o.primo_rilevamento or "9999", o.id))
    for o in ordine:
        gruppo = per_ente.setdefault(chiave_testo(o.ente)[:40], [])
        principale = next((p for p in gruppo if _stessi_dati(p, o)), None)
        motivo = "stesso ente, scadenza, importo e CPV; titoli simili"
        if principale is None:
            principale = next((p for p in gruppo if _stesso_cig(p, o)), None)
            motivo = "stesso ente e CIG, fonti diverse"
        if principale is None:
            gruppo.append(o)
            continue
        log.info("Deduplica: %s fuso in %s (%s)", o.id, principale.id, motivo)
        _fondi(principale, o, fonte_dati, fonte_dati.get(o.id, _freschezza(o)))
        del tenute[o.id]
    return tenute


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
    riepilogo["errori"].extend(f"portali_html: {e}" for e in portali_html.ERRORI)
    riepilogo["errori"].extend(f"acquistitelematici: {e}" for e in acquistitelematici.ERRORI)
    riepilogo["errori"].extend(f"sintel: {e}" for e in sintel.ERRORI)
    riepilogo["errori"].extend(f"plone: {e}" for e in plone.ERRORI)
    riepilogo["errori"].extend(f"centrale_acquisti: {e}" for e in centrale_acquisti.ERRORI)
    riepilogo["errori"].extend(f"jaggaer: {e}" for e in jaggaer.ERRORI)
    riepilogo["errori"].extend(f"maggioli: {e}" for e in maggioli.ERRORI)

    storico = carica_storico()
    prima = set(storico)
    # Regole dei feed che lo storico non conosce (procedure a invito, revoche, link corretti).
    storico = portali_rss.correggi_storico(storico, cfg)
    # Voci Plone, Centrale Acquisti, Jaggaer e HTML scartate da una regola esplicita (es. bando revocato
    # su Intercent-ER, consultazione revocata su STELLA, asta di vendita su eAppaltiFVG, voce
    # dell'albo dell'ASL 3 Nuoro del 2025 senza scadenza).
    for nome, esclusi in (("Plone", plone.ESCLUSI), ("Centrale Acquisti", centrale_acquisti.ESCLUSI),
                          ("Jaggaer", jaggaer.ESCLUSI), ("HTML", portali_html.ESCLUSI),
                          ("Maggioli", maggioli.ESCLUSI)):
        tolte = sorted(k for k in storico if k in esclusi)
        for k in tolte:
            del storico[k]
        if tolte:
            log.info("%s: tolte dallo storico %d voci ora escluse: %s", nome, len(tolte), ", ".join(tolte))
    tutte = pota(rivaluta(deduplica(unisci(storico, nuove, oggi)), filtro), cfg)
    riepilogo["nuove_opportunita"] = len(set(tutte) - prima)
    riepilogo["totale"] = len(tutte)

    salva(tutte, riepilogo)
    log.info("Fatto: %d opportunità totali, %d nuove", len(tutte), riepilogo["nuove_opportunita"])
    return 1 if abilitate and fallite == abilitate else 0


if __name__ == "__main__":
    sys.exit(main())
