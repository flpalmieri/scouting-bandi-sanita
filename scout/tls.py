"""Completamento della catena dei certificati TLS.

Alcuni server (es. bandi.aslto5.piemonte.it, 19/11/2026) inviano solo il proprio certificato
senza l'"intermedio" dell'autorita' che l'ha emesso. I browser lo scaricano da soli dall'indirizzo
scritto nel certificato (estensione "Authority Information Access", CA Issuers); Python/requests no,
e la connessione fallisce con "unable to get local issuer certificate".

Questo modulo fa come il browser, con le stesse garanzie:
  1. legge il certificato del sito e scarica gli intermedi indicati (al massimo 3 livelli);
  2. scarta i certificati autofirmati (un'autorita' radice non si scarica mai da un sito);
  3. VERIFICA la catena sito -> intermedi -> autorita' radice nota (certifi) con la libreria
     cryptography, compreso il nome del sito: se non torna, si ferma con un errore;
  4. solo allora prepara il file certifi + intermedi da passare a requests come verify=.
La verifica TLS di requests resta attiva: non si usa mai verify=False.
"""
from __future__ import annotations

import socket
import ssl
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import certifi
import requests
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID
from cryptography.x509.verification import PolicyBuilder, Store

from .common import log

_CACHE: dict[str, str] = {}


def _indirizzi_emittente(cert: x509.Certificate) -> list[str]:
    try:
        aia = cert.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
    except x509.ExtensionNotFound:
        return []
    return [d.access_location.value for d in aia
            if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS
            and isinstance(d.access_location, x509.UniformResourceIdentifier)
            and d.access_location.value.startswith(("http://", "https://"))]


def _certificato_server(host: str, porta: int, timeout: float) -> x509.Certificate:
    """Certificato presentato dal server. Qui non lo si verifica: serve solo a leggere
    l'indirizzo dell'intermedio; la verifica vera avviene dopo, sulla catena completa."""
    contesto = ssl.create_default_context()
    contesto.check_hostname = False
    contesto.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, porta), timeout=timeout) as s:
        with contesto.wrap_socket(s, server_hostname=host) as t:
            return x509.load_der_x509_certificate(t.getpeercert(binary_form=True))


def _scarica(url: str, timeout: float) -> x509.Certificate:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    dati = r.content
    if dati.lstrip().startswith(b"-----BEGIN"):
        return x509.load_pem_x509_certificate(dati)
    return x509.load_der_x509_certificate(dati)


def completa_catena(url: str, base: str | None = None, timeout: float = 20,
                    cartella: Path | None = None) -> str:
    """Percorso del file di autorita' (base + intermedi verificati) da usare come verify=.
    `base` e' l'elenco delle autorita' radice (default certifi; diverso solo nei test)."""
    parti = urlsplit(url)
    host, porta = parti.hostname, parti.port or 443
    chiave = f"{host}:{porta}"
    if chiave in _CACHE:
        return _CACHE[chiave]

    sito = _certificato_server(host, porta, timeout)
    intermedi: list[x509.Certificate] = []
    corrente = sito
    for _ in range(3):
        indirizzi = _indirizzi_emittente(corrente)
        if not indirizzi:
            break
        cert = _scarica(indirizzi[0], timeout)
        if cert.issuer == cert.subject:
            break                    # radice autofirmata: non la si aggiunge mai
        intermedi.append(cert)
        corrente = cert
    if not intermedi:
        raise RuntimeError(f"{host}: il certificato non indica un intermedio scaricabile")

    percorso_base = base or certifi.where()
    radici = x509.load_pem_x509_certificates(Path(percorso_base).read_bytes())
    verificatore = (PolicyBuilder().store(Store(radici)).time(datetime.now(timezone.utc))
                    .build_server_verifier(x509.DNSName(host)))
    try:
        verificatore.verify(sito, intermedi)
    except Exception as e:
        raise RuntimeError(f"{host}: la catena con l'intermedio scaricato non e' valida ({e})") from e

    cartella = cartella or Path(tempfile.gettempdir())
    cartella.mkdir(parents=True, exist_ok=True)
    destinazione = cartella / f"catena_{host}_{porta}.pem"
    destinazione.write_bytes(Path(percorso_base).read_bytes() + b"\n"
                             + b"".join(c.public_bytes(Encoding.PEM) for c in intermedi))
    log.info("TLS %s: catena completata e verificata con %d intermedi", host, len(intermedi))
    _CACHE[chiave] = str(destinazione)
    return str(destinazione)
