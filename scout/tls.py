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
Dal 23/11/2026 (Sicilia): legge anche i PKCS#7 (.p7c), smette di salire quando l'emittente e'
gia' una radice nota, e accetta intermedi presi da un file del repository quando l'indirizzo
dell'emittente non risponde (sempre verificati come quelli scaricati).
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
from cryptography.hazmat.primitives.serialization import Encoding, pkcs7
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


def _carica_certificati(dati: bytes) -> list[x509.Certificate]:
    """Certificati da un file scaricato o dal repository: PEM (uno o piu'), DER, oppure PKCS#7
    (.p7c/.p7b, DER o PEM). Sectigo pubblica la radice R46 come .p7c con 4 certificati (ARNAS
    Civico, 23/11/2026): prima tls.py si fermava con \"error parsing asn1\"."""
    testo = dati.lstrip()
    if testo.startswith(b"-----BEGIN PKCS7") or testo.startswith(b"-----BEGIN CMS"):
        return list(pkcs7.load_pem_pkcs7_certificates(dati))
    if testo.startswith(b"-----BEGIN"):
        return x509.load_pem_x509_certificates(dati)
    try:
        return [x509.load_der_x509_certificate(dati)]
    except ValueError:
        return list(pkcs7.load_der_pkcs7_certificates(dati))


def _scarica(url: str, timeout: float) -> list[x509.Certificate]:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return _carica_certificati(r.content)


def _emittente(cert: x509.Certificate, candidati: list[x509.Certificate],
               nomi_radici: set | None = None) -> x509.Certificate | None:
    """Tra i candidati, l'autorita' che ha emesso cert (stesso nome; se c'e', stessa chiave).
    Se sono piu' d'uno (firme incrociate nel .p7c di Sectigo) si preferisce quello emesso da una
    radice nota; la verifica riceve comunque tutti i candidati e sceglie il percorso valido."""
    adatti = []
    try:
        aki = cert.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_KEY_IDENTIFIER).value.key_identifier
    except x509.ExtensionNotFound:
        aki = None
    for c in candidati:
        if c.subject != cert.issuer or c.subject == c.issuer:
            continue
        if aki:
            try:
                ski = c.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_KEY_IDENTIFIER).value.digest
                if ski != aki:
                    continue
            except x509.ExtensionNotFound:
                pass
        adatti.append(c)
    if not adatti:
        return None
    noti = [c for c in adatti if nomi_radici and c.issuer in nomi_radici]
    return (noti or adatti)[0]


def completa_catena(url: str, base: str | None = None, timeout: float = 20,
                    cartella: Path | None = None, intermedi_extra: list[str] | None = None,
                    adesso: datetime | None = None) -> str:
    """Percorso del file di autorita' (base + intermedi verificati) da usare come verify=.
    `base` e' l'elenco delle autorita' radice (default certifi; diverso solo nei test).
    `intermedi_extra`: file PEM del repository con intermedi che il sito non manda e che non si
    possono scaricare (AO Papardo, 23/11/2026: l'indirizzo dell'intermedio Actalis risponde 500).
    Sono trattati come quelli scaricati: non sono mai \"fidati\", la catena va verificata fino a una
    radice di certifi. `adesso` serve solo ai test."""
    parti = urlsplit(url)
    host, porta = parti.hostname, parti.port or 443
    chiave = f"{host}:{porta}"
    if chiave in _CACHE:
        return _CACHE[chiave]

    percorso_base = base or certifi.where()
    radici = x509.load_pem_x509_certificates(Path(percorso_base).read_bytes())
    nomi_radici = {r.subject for r in radici}

    riserva: list[x509.Certificate] = []
    for f in intermedi_extra or []:
        riserva.extend(_carica_certificati(Path(f).read_bytes()))

    sito = _certificato_server(host, porta, timeout)
    intermedi: list[x509.Certificate] = []
    altri: list[x509.Certificate] = []      # altri candidati scaricati (firme incrociate)
    corrente = sito
    for _ in range(4):
        if corrente.issuer in nomi_radici:
            break                    # l'emittente e' gia' una radice nota: non serve salire
        trovato = _emittente(corrente, riserva + intermedi, nomi_radici)
        if trovato is None:
            indirizzi = _indirizzi_emittente(corrente)
            if not indirizzi:
                break
            try:
                scaricati = _scarica(indirizzi[0], timeout)
            except Exception as e:
                if not intermedi:
                    raise RuntimeError(f"{host}: intermedio non scaricabile da {indirizzi[0]} ({e})") from e
                # Si ha gia' almeno un intermedio: decide la verifica qui sotto.
                log.warning("TLS %s: %s non scaricabile (%s), verifico con quello che ho", host, indirizzi[0], e)
                break
            altri.extend(c for c in scaricati if c.subject != c.issuer)
            trovato = _emittente(corrente, scaricati, nomi_radici)
            if trovato is None:
                break                # solo radici autofirmate: non si aggiungono mai
        intermedi.append(trovato)
        corrente = trovato
    if not intermedi:
        raise RuntimeError(f"{host}: il certificato non indica un intermedio scaricabile")

    verificatore = (PolicyBuilder().store(Store(radici)).time(adesso or datetime.now(timezone.utc))
                    .build_server_verifier(x509.DNSName(host)))
    try:
        catena = verificatore.verify(sito, intermedi + [c for c in riserva + altri if c not in intermedi])
    except Exception as e:
        raise RuntimeError(f"{host}: la catena con l'intermedio scaricato non e' valida ({e})") from e
    # Si scrivono solo gli intermedi della catena verificata (non il sito, non la radice).
    verificati = [c for c in catena[1:] if c.subject != c.issuer and c not in radici]

    cartella = cartella or Path(tempfile.gettempdir())
    cartella.mkdir(parents=True, exist_ok=True)
    destinazione = cartella / f"catena_{host}_{porta}.pem"
    destinazione.write_bytes(Path(percorso_base).read_bytes() + b"\n"
                             + b"".join(c.public_bytes(Encoding.PEM) for c in verificati))
    log.info("TLS %s: catena completata e verificata con %d intermedi", host, len(verificati))
    _CACHE[chiave] = str(destinazione)
    return str(destinazione)
