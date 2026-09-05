# AutoPrint

Stampa periodica automatica di un JPG da una **Synology NAS** a una stampante di
rete, via **IPP**. Nato per un problema concreto: una stampante a getto d'inchiostro
che resta ferma troppo a lungo si secca le testine, quindi una volta al mese le si
manda una stampa fotografica a colori a tutta pagina per tenerla in esercizio.

- **Zero dipendenze**: solo la libreria standard di Python. Niente `pip install`.
- **Niente CUPS**, niente Docker, niente daemon sempre attivo.
- Un solo eseguibile da pianificare con l'Utilità di pianificazione di DSM.
- Parla IPP direttamente: encoder e parser del protocollo sono inclusi (~100 righe).

Testato su DSM 7.3.2 (Python 3.8.15, aarch64) con una **Epson EcoTank ET-8550**.

## Perché IPP e non altro

Le EcoTank recenti espongono IPP sulla 631 e accettano `image/jpeg` **direttamente**,
insieme agli attributi per carta, scaling, qualità e margini. Non serve rasterizzare
niente, non serve CUPS, non serve un driver. Su una ET-8550 le porte 9100 (RAW) e
515 (LPD) sono chiuse: IPP non è solo la scelta migliore, è l'unica.

Nota: quella stampante risponde `426 Upgrade Required` in chiaro e vuole TLS.
Lo script lo gestisce da solo — indichi `ipp://` e passa a TLS quando serve, senza
verificare il certificato (le stampanti usano certificati self-signed).

## Installazione

```bash
git clone https://github.com/giuliomaffei90/synology-autoprint.git
cd synology-autoprint
cp config.example.json config.json
```

Copia la cartella sulla NAS, per esempio in `/volume1/Contents/AutoPrint/`.
Se `scp` viene rifiutato dalla NAS (capita, DSM spesso non abilita il sottosistema
sftp), passa da tar:

```bash
tar cf - *.py config.json | ssh utente@NAS 'mkdir -p /volume1/Contents/AutoPrint && tar xf - -C /volume1/Contents/AutoPrint'
```

## Trovare la stampante

```bash
python3 printer_probe.py 192.168.1.100
```

Riporta le porte aperte (631/9100/515), l'endpoint IPP valido e gli attributi che
servono per configurare il resto: formati carta supportati, tipi MIME accettati,
risoluzioni, modalità colore, qualità, stato e livelli di inchiostro.

Se non conosci l'IP della stampante, cercalo con `dns-sd -B _ipp._tcp` (macOS),
`avahi-browse -rt _ipp._tcp` (Linux), o dal pannello della stampante stessa.

## Uso

```bash
python3 monthly_print.py --dry-run   # tutti i controlli, nessuna stampa
python3 monthly_print.py             # stampa
```

Exit code: `0` job inviato, `1` errore, `2` esito ignoto (timeout durante l'invio:
il job potrebbe essere passato, controlla la stampante prima di rilanciare).

Prima di inviare, lo script verifica che il file esista, sia leggibile e sia un JPEG
strutturalmente valido; che la stampante risponda, accetti job, non sia in errore, e
supporti il formato carta, lo scaling e il borderless richiesti. Un solo tentativo di
invio, nessun retry automatico: meglio fallire che ritrovarsi dieci copie dello stesso
foglio.

## Pianificazione su DSM

DSM 7 non ha un comando per creare attività pianificate (`synoschedtask` espone solo
`--get/--del/--run`), quindi va fatto dall'interfaccia:

**Pannello di controllo > Utilità di pianificazione > Crea > Attività pianificata >
Script definito dall'utente**

| Campo | Valore |
|---|---|
| Utente | il proprietario del file, root non serve |
| Pianificazione | mensile |
| Notifica e-mail | *"solo se lo script termina in modo anomalo"* |
| Script | `/usr/bin/python3 /volume1/Contents/AutoPrint/monthly_print.py` |

Il percorso assoluto dell'interprete è necessario: il PATH dello scheduler DSM è
minimale e `python3` da solo non viene risolto.

## config.json

| chiave | valore |
|---|---|
| `printer_uri` | `ipp://IP/ipp/print` |
| `image_path` | percorso assoluto del JPG |
| `paper_size` | nome PWG (`iso_a4_210x297mm`) o alias `a4`/`a3`/`a5`/`letter`/`10x15`/`13x18` |
| `borderless` | `true` → margini a 0 via `media-col` |
| `scaling` | `fit` / `fill` / `auto` / `auto-fit` / `none` |
| `color` | `true` = colore |
| `print_quality` | `draft` / `normal` / `high` |
| `log_file` | relativo alla cartella dello script |

Sostituendo il JPG con un altro file dallo stesso nome, la stampa successiva usa
automaticamente la nuova immagine. L'originale non viene mai modificato.

### fit contro fill

`fit` adatta l'immagine alla pagina mantenendo le proporzioni: nessun ritaglio, ma se
la foto non ha le proporzioni del foglio restano bande bianche sui lati. `fill` riempie
davvero tutto il foglio, al prezzo di un ritaglio ai bordi. Per un borderless
edge-to-edge vero serve `fill`.

I valori realmente accettati variano da stampante a stampante: `printer_probe.py` li
elenca sotto `print-scaling-supported`.

## Log

Append-only in `logs/autoprint.log`, con rotazione a 1 MB × 3 file.

```text
2026-09-05 02:03:57 - INFO - Starting monthly print
2026-09-05 02:03:57 - INFO - Image: /volume1/Contents/AutoPrint/PRINT.jpg (1189x2000 px, 0.3 MB)
2026-09-05 02:03:57 - INFO - Printer reachable at 192.168.1.100:631
2026-09-05 02:03:57 - INFO - Printer: EPSON ET-8550 Series - stato idle
2026-09-05 02:03:58 - INFO - Print job submitted successfully
2026-09-05 02:03:58 - INFO - Job ID: 8
```

## Test

```bash
python3 test_autoprint.py
```

Verifica il parsing dei formati carta PWG, la validazione JPEG (compresi file
troncati, senza SOF e non-JPEG) e il round-trip di codifica/decodifica IPP, incluso
il `media-col` del borderless. Non serve una stampante.

## Struttura

```text
monthly_print.py    controlli, invio del job, logging
printer_probe.py    discovery + encoder/parser IPP condiviso
test_autoprint.py   self-check senza stampante
config.example.json
```
