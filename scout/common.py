"""Modello dati, filtri e funzioni di supporto condivise dalle fonti."""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("scout")


@dataclass
class Opportunita:
    id: str
    fonti: list[str]
    titolo: str
    ente: str
    scadenza: str | None = None          # YYYY-MM-DD
    pubblicazione: str | None = None     # YYYY-MM-DD
    importo: float | None = None         # EUR, stimato / a base d'asta
    cpv: list[str] = field(default_factory=list)
    cig: list[str] = field(default_factory=list)
    tipo: str | None = None
    link: str | None = None
    regione: str | None = None
    motivo_match: str = ""
    primo_rilevamento: str | None = None
    ultimo_rilevamento: str | None = None


# ---------------------------------------------------------------- HTTP

def sessione_http() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=5, backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = "scouting-bandi-sanita/1.0 (+GitHub Actions)"
    return s


# ---------------------------------------------------------------- parsing

_RE_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_RE_IT = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_RE_COMPATTA = re.compile(r"^(\d{4})(\d{2})(\d{2})")


def parse_data(valore) -> str | None:
    """Restituisce la data in formato ISO (YYYY-MM-DD) o None."""
    if valore is None:
        return None
    s = str(valore).strip()
    for rx, ordine in ((_RE_ISO, (1, 2, 3)), (_RE_IT, (3, 2, 1)), (_RE_COMPATTA, (1, 2, 3))):
        m = rx.search(s)
        if m:
            try:
                a, me, g = (int(m.group(i)) for i in ordine)
                return date(a, me, g).isoformat()
            except ValueError:
                continue
    return None


def parse_importo(valore) -> float | None:
    if valore is None:
        return None
    if isinstance(valore, (int, float)):
        return float(valore)
    s = str(valore).strip().replace("€", "").replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:            # 1.234.567,89 oppure 1,234,567.89
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def appiattisci(valore) -> list[str]:
    """Raccoglie tutti i valori scalari (come stringhe) da liste/dizionari annidati."""
    out: list[str] = []
    if valore is None:
        return out
    if isinstance(valore, dict):
        for v in valore.values():
            out.extend(appiattisci(v))
    elif isinstance(valore, (list, tuple, set)):
        for v in valore:
            out.extend(appiattisci(v))
    else:
        s = str(valore).strip()
        if s:
            out.append(s)
    return out


_LINGUE_PREFERITE = ("ita", "ITA", "it", "IT", "eng", "ENG", "en", "EN")


def testo(valore, separatore: str = " / ") -> str:
    """Estrae un testo da campi multilingua TED (preferendo l'italiano)."""
    if valore is None:
        return ""
    if isinstance(valore, dict):
        for k in _LINGUE_PREFERITE:
            if k in valore:
                return testo(valore[k], separatore)
        for v in valore.values():
            t = testo(v, separatore)
            if t:
                return t
        return ""
    if isinstance(valore, (list, tuple)):
        visti, parti = set(), []
        for v in valore:
            t = testo(v, separatore)
            if t and t not in visti:
                visti.add(t)
                parti.append(t)
        return separatore.join(parti)
    return str(valore).strip()


def normalizza_cpv(codice: str) -> str:
    return re.sub(r"\D", "", str(codice))[:8]


_RE_PREFISSO_TED = re.compile(r"^\s*Italia\s*[–—-]\s*.+?\s+[–—]\s+")


def pulisci_titolo(titolo: str) -> str:
    """Toglie il prefisso standard TED "Italia – <categoria CPV> – "."""
    return _RE_PREFISSO_TED.sub("", titolo or "", count=1).strip()


def chiave_testo(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())[:90]


# ---------------------------------------------------------------- filtro

class Filtro:
    def __init__(self, cfg: dict):
        self.prefissi = [normalizza_cpv(c) for c in cfg.get("cpv_prefissi", [])]
        self.sempre = {normalizza_cpv(c) for c in cfg.get("cpv_sempre_inclusi", [])}
        self.re_ente = self._compila(cfg.get("enti_sanitari_regex"))
        self.re_sanita = self._compila(cfg.get("parole_chiave_sanita_regex"))
        self.re_escl = self._compila(cfg.get("esclusioni_regex"))
        self.re_aggr = self._compila(cfg.get("aggregatori_misti_regex"))

    @staticmethod
    def _compila(lista):
        if not lista:
            return None
        return re.compile("|".join(f"(?:{p})" for p in lista), re.IGNORECASE)

    def valuta(self, titolo: str, ente: str, cpv: list[str]) -> str | None:
        """Restituisce il motivo dell'inclusione, oppure None se l'avviso va scartato."""
        titolo, ente = titolo or "", ente or ""
        if self.re_escl and self.re_escl.search(titolo):
            return None
        codici = [normalizza_cpv(c) for c in cpv if c]
        speciali = [c for c in codici if c in self.sempre]
        if speciali:
            return f"CPV software sanitario ({', '.join(speciali)})"
        it = [c for c in codici if any(c.startswith(p) for p in self.prefissi)]
        if not it:
            return None
        if self.re_ente and self.re_ente.search(ente):
            return f"Ente sanitario + CPV IT ({it[0]})"
        if self.re_sanita:
            m = self.re_sanita.search(titolo)
            if m:
                return f"Oggetto sanitario ('{m.group(0)}') + CPV IT ({it[0]})"
        if self.re_aggr and self.re_aggr.search(ente):
            return f"Soggetto aggregatore + CPV IT ({it[0]}) - da verificare"
        return None
