# Scouting bandi software e IT in sanità

Raccoglie ogni giorno, tramite GitHub Actions, i bandi pubblici per software e
servizi informatici emessi da ASL, aziende ospedaliere, IRCCS, Regioni e
soggetti aggregatori, e li salva in un unico elenco ordinato per scadenza.

## Fonti attive

| Fonte | Cosa copre | Frequenza dati |
|---|---|---|
| **TED** (API v3) | Gare italiane sopra soglia UE, bandi e preinformazioni | Quotidiana |
| **ANAC** (open data CIG) | Tutte le procedure con CIG, anche sotto soglia | Mensile, con ritardo |

Prossime fonti: piattaforme dei soggetti aggregatori regionali.

## Messa in funzione

1. Crea un repository su GitHub (va bene anche privato) e carica tutti i file
   di questa cartella, compresa `.github/workflows/scouting.yml`.
2. Vai in **Settings → Actions → General → Workflow permissions** e verifica
   che sia selezionato **Read and write permissions** (serve se la tua
   organizzazione imposta di default la sola lettura).
3. Vai nella scheda **Actions**, scegli *Scouting bandi sanità* e premi
   **Run workflow** per il primo avvio.
4. Al termine trovi i risultati in `data/`:
   - `opportunita.json` – elenco completo (verrà letto dalla dashboard)
   - `opportunita.csv` – stesso elenco, apribile in Excel
   - `ultima_esecuzione.json` – riepilogo ed eventuali errori

Da quel momento lo script gira da solo ogni mattina.

## Dashboard

`index.html` è la dashboard: legge `data/opportunita.json` e mostra le
opportunità raggruppate per scadenza. Per pubblicarla con GitHub Pages:
**Settings → Pages → Build and deployment → Source: Deploy from a branch**,
branch `main`, cartella `/ (root)`. L'indirizzo sarà
`https://<utente>.github.io/scouting-bandi-sanita/`.

Lo stato di lavorazione (interessante, in preparazione, scartata…) viene
salvato nel browser di chi lo imposta e non è condiviso.

## Personalizzare i filtri

Tutto è in `config.yaml`. Un avviso viene tenuto se:

- ha un CPV che inizia con uno dei `cpv_prefissi` (di default 48 software, 72 servizi IT)
- **e** l'ente risulta sanitario (`enti_sanitari_regex`) **oppure** l'oggetto
  contiene parole del mondo sanitario (`parole_chiave_sanita_regex`);
- oppure ha un CPV in `cpv_sempre_inclusi` (software medico);
- e il titolo non contiene nessuna `esclusioni_regex`.

Il campo `motivo_match` di ogni opportunità spiega quale regola l'ha fatta
entrare: è utile per correggere falsi positivi e falsi negativi.

## Cose da sapere

- **Primo avvio**: controlla il log in Actions. Se TED rifiuta qualche nome di
  campo, lo script riprova con un insieme minimo e lo scrive nel log. Se ANAC
  ha cambiato i nomi delle colonne, il log mostra l'intestazione trovata, così
  basta aggiornare il dizionario `COLONNE` in `scout/anac.py`.
- **Ritardo ANAC**: i file mensili arrivano con qualche settimana di ritardo,
  quindi molte gare sono già scadute. Lo script tiene solo quelle ancora aperte.
- **Duplicati**: la stessa gara vista su TED e ANAC viene fusa se ente e
  oggetto coincidono. Varianti di testo possono sfuggire.
- **Inattività**: GitHub sospende i workflow schedulati dopo 60 giorni senza
  attività nel repository. Se ricevi l'avviso via email, basta riattivarlo.

## Esecuzione in locale

```bash
pip install -r requirements.txt
python -m scout.main
```
