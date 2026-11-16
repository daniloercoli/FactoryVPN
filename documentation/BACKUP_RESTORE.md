# Backup e ripristino di FactoryVPN

Implementazione per **nuove installazioni**, con schema iniziale unico. Non sono
previsti aggiornamenti o conversioni di database da versioni precedenti.

## Perimetro

Il portale offre backup manuali, elenco, download cifrato, conservazione,
importazione verificata, prova isolata e ripristino completo. Ogni operazione è
riservata agli amministratori **globali**, anche tramite API. I delegati di gruppo
non possono vedere metadati, richiedere copie o scaricare archivi.

Il backup comprende PostgreSQL (utenti, gruppi, permessi, inventario, storico,
audit, certificati, rinnovi e credenziali), i parametri applicativi previsti dall'archivio,
registro pubblico root/Sub-CA, chiavi delle Sub-CA, certificati/chiavi server OpenVPN
e HTTPS, TLS-crypt e CRL. **Non contiene la chiave privata root né la sua passphrase.**
Non salva il sistema operativo del server, dei gateway o i programmi PLC. Le chiavi
generate dal gateway durante i rinnovi restano sul gateway; possono invece essere
presenti pacchetti iniziali cifrati ancora in attesa di consegna. Il codice si reinstalla dalla **stessa
versione**. La firma dello schema è derivata dal DDL iniziale, non dal solo nome
`factoryvpn_initial`.

Tra i parametri salvati ci sono durata VPN, lease, durata portale/SSO e tempi
usati per generare i pacchetti gateway, oltre alle durate dei certificati client.
Il formato è la versione 2: non importa archivi della vecchia CA singola.
L'archivio non è una copia completa del
`.env`: `WORKER_POLL_INTERVAL`, `OPENVPN_PING_SECONDS` e `OPENVPN_RESTART_SECONDS`
restano quelli del server di destinazione, come i parametri SMTP. Conservare la
configurazione dell'installazione in modo protetto e ricontrollare
[durate e timeout](DOCKER_GUIDE.md#durate-timeout-e-reti-instabili) dopo il recupero.
Non sono inclusi eventuali bundle CA SMTP, personalizzazioni di `docker-compose.yml`
o unità systemd. Conservarli separatamente e predisporli sul nuovo server prima
del recupero: un percorso CA presente nel `.env` non ricrea il file o il suo mount.
I `gateway.env` già installati sui dispositivi non vengono aggiornati dal ripristino.

Il primo rilascio usa un archivio locale e file scaricabili per una copia esterna.
Pianificazione, destinazioni remote (ad esempio S3), notifiche e recupero a un
istante arbitrario tramite WAL non sono inclusi.

## Installazione

Il coordinatore gira sull'host Linux con Docker Compose e Python 3.12, utilizzando
le dipendenze di `requirements.txt`. L'API **non** riceve socket Docker, chiave di
recupero o chiavi private delle CA. L'account che esegue il coordinatore controlla lo stack
Docker e va trattato come amministratore dell'installazione.

Nella directory dell'installazione, dopo il bootstrap di `.env` e `.runtime` e
**prima** del primo `docker compose up`:

```sh
.venv/bin/python -m scripts.backup_coordinator init
```

L'inizializzazione crea:

| Percorso | Uso | Accesso API |
| --- | --- | --- |
| `.backup-control/` | Catalogo SQLite, coda, eventi e barriera di manutenzione | Lettura/scrittura |
| `.backups/` | Archivi cifrati completati | Sola lettura |
| `.backup-private/` | Chiave, temporanei privati e giornale cifrato del ripristino | Nessuno |

File privati `0600`, directory `0700`. `init` scrive in `.env` gli UID/GID
dell'account corrente; API e issuer usano questi identificativi anche nei container.
Eseguire bootstrap, inizializzazione e coordinatore con lo stesso account. Il
catalogo richiede un filesystem locale con locking POSIX affidabile, non NFS/SMB.

Custodire una copia di `.backup-private/recovery.key` **fuori dal server**, separata
dagli archivi. Senza questa chiave i backup cifrati non sono recuperabili. Non
inserirla nell'archivio, nel repository o nella configurazione dell'API.

Avviare lo stack e il coordinatore:

```sh
docker compose up -d --build --wait
.venv/bin/python -m scripts.backup_coordinator serve
```

`serve` rimane in primo piano: il terminale va lasciato aperto. Per gestire il
servizio anche dopo la chiusura della sessione o il riavvio dell'host usare systemd.
Il modello `deploy/factoryvpn-backup.service` permette la gestione con systemd;
adattare `User`, `Group`, `SupplementaryGroups`, `WorkingDirectory`, `ExecStart`
e `ReadWritePaths` ai percorsi e all'account del bootstrap. Dopo avere interrotto
l'eventuale `serve` in primo piano, un amministratore dell'host lo installa dalla
cartella dei sorgenti:

```sh
sudo install -m 0644 deploy/factoryvpn-backup.service /etc/systemd/system/factoryvpn-backup.service
sudo systemctl daemon-reload
sudo systemctl enable --now factoryvpn-backup
sudo systemctl status factoryvpn-backup --no-pager
```

Lo stato deve essere `active (running)`; in caso contrario leggere
`sudo journalctl -u factoryvpn-backup -n 80 --no-pager`.
Il coordinatore
controlla sempre il progetto indicato da `COMPOSE_PROJECT_NAME` in `.env`, oppure
`factoryvpn`. Non usare override Compose esterni al file dell'installazione: i
comandi di recupero utilizzano esplicitamente quel file e quella configurazione.

## Utilizzo dal portale

La pagina **Backup e ripristino** appare agli amministratori globali. Mostra
disponibilità del coordinatore, copie e operazioni recenti. Integrità verificata e
prova di ripristino riuscita sono due risultati distinti.

Le operazioni richiedono un'identità riconfermata da meno di cinque minuti. Il
login locale con codice email vale come conferma; successivamente si reinserisce
la password e si verifica un nuovo codice email. Per
SSO viene richiesto `prompt=login`, `max_age=0`, verificando `auth_time` nel token
firmato, l'utente e la sessione browser originaria. Un provider che non conferma
l'autenticazione recente non abilita queste operazioni. La conferma SSO conserva
la sessione corrente e le sue VPN. Il connettore Cognito è sperimentale:
verificare questo flusso con il provider effettivamente configurato.

Creazione, prova, importazione, eliminazione e ripristino sono serializzati e hanno
chiavi di idempotenza. Il coordinatore ricontrolla l'account prima dell'esecuzione;
un'operazione distruttiva già accettata e avviata completa il recupero anche se il
browser viene chiuso. Durante il download si ricontrolla il login a ogni blocco:
logout, revoca o perdita del ruolo interrompono il trasferimento.

Il limite iniziale è **512 MiB** per archivio e payload decifrato; può essere
ridotto con `BACKUP_MAX_BYTES`. L'upload ha anche il limite Nginx e non viene
bufferizzato integralmente dal proxy. Il server trasferisce i download a blocchi;
il browser conserva il file in memoria prima di salvarlo. La prova PostgreSQL usa
un container isolato con 1 GiB di RAM e 2 GiB di spazio database: un archivio che
richiede più risorse viene rifiutato, non dichiarato verificato.

Prima dell'upload si richiede spazio libero pari ad almeno quattro volte il limite
configurato. Il trasferimento deve terminare entro cinque minuti; gli upload rimasti
in sospeso dopo la perdita del processo API vengono eliminati dopo dieci minuti,
liberando la coda delle operazioni.
Anche il dump PostgreSQL viene fermato durante la scrittura al raggiungimento del
limite, senza aspettare che un database troppo grande esaurisca lo spazio del server.

La conservazione predefinita è di 10 copie, modificabile da 1 a 100. La rimozione
automatica avviene dopo la creazione di un nuovo backup. L'ultima copia di sicurezza
di un ripristino completato resta protetta. Gli archivi importati mantengono la data
della copia originale. Conservare una copia cifrata fuori dalla macchina protegge
anche dalla perdita del disco locale.

Il backup non interrompe le VPN. Durante l'acquisizione non ruotare manualmente
chiavi né modificare i file del runtime. Gli strumenti PKI distribuiti condividono
un lock sui file con il coordinatore; il backup acquisisce prima il lock database.
Fare una copia dopo ogni modifica di CA/server e conservarla anche fuori dal server.

## Ripristino gestito

1. Autenticazione recente, scelta della copia e conferma esplicita.
2. Verifica crittografica, compatibilità, PKI e ripristino in PostgreSQL temporaneo,
   senza rete o mount dell'host. Confronto di colonne, vincoli e indici con lo schema
   iniziale della stessa versione.
3. Barriera di manutenzione persistente; blocco di nuovi accessi e delle VPN.
4. Chiusura delle VPN esistenti e attesa della conferma di pulizia, fino a 90 secondi.
   Se la pulizia non riesce, il ripristino non sostituisce i dati.
5. Arresto dei servizi, distruzione del vecchio namespace OpenVPN, copia di sicurezza
   dello stato corrente e giornale di recupero cifrato, sincronizzato su disco.
6. Creazione del database con il DDL distribuito nel codice; caricamento dei soli
   dati del dump e nuova validazione delle chiavi esterne. Le definizioni SQL
   caricate dall'archivio non vengono installate nel database operativo.
7. Ripristino delle chiavi e dei segreti applicativi; rotazione di JWT e chiave dei
   worker. Password PostgreSQL, percorsi e origini pubbliche restano quelli del
   server di destinazione. Gli endpoint SSO devono essere compatibili con tale origine.
   Il primo rilascio richiede gli stessi endpoint pubblici della copia; un cambio di
   dominio o porta è una riconfigurazione distinta, non una conversione automatica.
8. Invalidazione delle sessioni, bonifica dello stato operativo e applicazione delle
   restrizioni di sicurezza più recenti. Pubblicazione delle revoche prima di OpenVPN.
9. Riavvio e verifica di API, database, management OpenVPN e accesso del worker.
   Solo dopo questi controlli viene tolta la manutenzione.

Durante l'arresto dell'API il portale può mostrare il servizio indisponibile; il
coordinatore prosegue indipendentemente. Al termine occorre accedere nuovamente.
Non si riprendono le VPN o i pacchetti di provisioning pendenti. Telemetria e
presenza gateway vengono aggiornate solo da nuove comunicazioni.

Il registro di arruolamento viene recuperato, ma tutti i segreti iniziali
provenienti dal backup sono invalidati e le pratiche incomplete sono bloccate,
senza approvazioni o candidati riutilizzabili. HQ può prenderle in carico
manualmente sulla stessa identità e consegnare un nuovo ZIP; non può riattivare
il vecchio segreto. I gateway già completati seguono i controlli delle credenziali
operative descritti sotto. `FACTORY_STATIONS` e `factory/allow.conf` non vengono
reintrodotti dal backup: restano quelli dell'installazione corrente. Un nuovo
`recovery-init` parte con postazioni disabilitate e allowlist chiusa.

Gli account eliminati/disabilitati non tornano utilizzabili. Permessi e gruppi
recuperati sono intersecati con quelli correnti; password e associazioni d'identità
correnti prevalgono. Certificati e credenziali gateway devono risultare attivi sia
nella copia sia nello stato corrente; un cambio di gruppo invalida le vecchie
credenziali. L'assegnazione attuale dei gateway prevale su quella della copia; se il
gateway è stato eliminato o il gruppo attuale non esiste nella copia, viene
recuperato senza gruppo e resta visibile soltanto agli amministratori globali fino
a una nuova assegnazione. Anche il suo inventario resta fuori dalla portata dei
delegati del vecchio gruppo.
L'amministratore che richiede il ripristino deve restare amministratore
anche nei dati recuperati, altrimenti il ripristino fallisce e viene recuperata la
copia di sicurezza.

Le generazioni PKI più recenti presenti sul server e i loro contatori CRL non
vengono sostituiti da quelli più vecchi dell'archivio. I candidati di rinnovo non
attivati sono annullati; una conferma già attivata resta ripetibile solo se il
certificato è tuttora autorizzato. Un rinnovo completato dopo la copia può richiedere
nuovo provisioning: il restore non ricrea identità assenti dall'archivio.
Una CRL online scaduta viene rigenerata prima del listener; un certificato server
scaduto viene rinnovato se la catena ha vita sufficiente. Una CRL root scaduta
richiede invece un nuovo file pubblico firmato offline. Prima del recupero
aggiornare la CRL root nel runtime preparato seguendo la
[procedura PKI](DOCKER_GUIDE.md#rinnovare-la-crl-root).

Se un passaggio fallisce dopo la sostituzione, il coordinatore tenta il ripristino
della copia di sicurezza. In caso di perdita del processo, il giornale viene
recuperato al riavvio. Se anche il recupero fallisce, la manutenzione rimane attiva;
non viene dichiarato successo. Eventi e job restano nel catalogo esterno al database.

## Recupero da console

Per riprendere un recupero interrotto quando il portale è indisponibile, fermare
prima l'eventuale coordinatore già in esecuzione: un amministratore dell'host usa
`sudo systemctl stop factoryvpn-backup` se è gestito da systemd. Poi, con lo
stesso account del bootstrap e dalla directory dell'installazione:

```sh
.venv/bin/python -m scripts.backup_coordinator recover
```

Il comando usa lo stesso lock esclusivo del servizio: non eseguirli insieme.
Dopo il recupero riuscito riavviare il coordinatore con il gestore configurato;
se fallisce, conservare catalogo e journal e leggere l'errore senza rimuovere
la manutenzione a mano.

In caso di perdita dell'intero server, predisporre una nuova installazione della
stessa versione. Preparare `.env` e runtime dall'archivio autenticato, senza
generare una nuova root. Usare percorsi vuoti e avviare inizialmente **solo**
database/API, mantenendo OpenVPN e worker spenti:

```sh
.venv/bin/python -m scripts.bootstrap recovery-init --host vpn.example.com \
  --archive /percorso/protetto/copia.fvbackup \
  --recovery-key-file /percorso/protetto/recovery.key
# Configurare .env (origini/porte originali e SMTP), verificare HTTPS e CRL root.
.venv/bin/python -m scripts.backup_coordinator init --recovery-key-file /percorso/protetto/recovery.key
docker compose build
docker compose up -d --no-deps --wait db
docker compose up -d --no-deps --wait api
.venv/bin/python -m scripts.backup_coordinator restore-file --archive /percorso/protetto/copia.fvbackup
```

Il comando richiede conferma e un **nuovo amministratore locale** con password
ed email raggiungibile. La password viene inserita senza eco. Non conoscendo le revoche successive alla copia, disabilita i
vecchi account e revoca le credenziali VPN/gateway recuperate. L'amministratore deve
riconvalidare gli accessi e ripetere il provisioning. Il recupero da console non
presuppone che un vecchio backup rappresenti autorizzazioni ancora valide.
Il rinnovo automatico dei certificati non aggira questa revoca di API key e identità.
Dopo il successo completare i controlli sotto e avviare il coordinatore come
descritto nell'[installazione](#installazione); `restore-file` termina al termine
del recupero e non lascia un servizio backup in esecuzione.

## Copie della root offline

Il backup server e quello della root sono due copie diverse. Conservare almeno due
copie offline cifrate, su supporti separati, dell'intera cartella root: `root.key`
(già cifrata con passphrase), `root.crt`, `root.crl`, `state.json` e gli eventuali
certificati esportati. Custodire la passphrase separatamente e aggiornare entrambe
le copie dopo ogni firma o rinnovo CRL. Il registro e il contatore CRL servono quanto
la chiave: non ripristinare una vecchia copia operativa senza confrontarli.

Provare periodicamente il recupero su una macchina isolata: aprire una copia di
prova, confrontare l'impronta con quella registrata e generare una CRL di prova con
`root-crl`. Il comando verifica firma root e corrispondenza chiave/certificato prima
di firmare. Scartare l'ambiente e il risultato di prova; non sostituire con essi lo
stato autorevole. Per trasferire dati al server usare un supporto contenente solo
certificati/CRL pubblici, mai quello che custodisce la chiave root.

## Verifica dopo il recupero

Accedere con un codice email nuovo, verificare gruppi e assegnazioni, preparare
nuovi profili quando richiesto e provare una VPN completa con SSH/web al gateway e accesso alla LAN dalla sua shell.
Conservare separatamente chiave di recupero e copia esterna. Dalla pagina
**Backup e ripristino**, eseguire una prova isolata della copia più recente
e controllarne l'esito prima di considerarla recuperabile.

Configurare il [relay SMTP](DOCKER_GUIDE.md#server-smtp-locale) prima del login.
Il ripristino conserva email, verifica della casella e politica corrente, elimina
le richieste di codice e mantiene SMTP/EMAIL_CODE_KEY del server di destinazione.
Il recupero disastro richiede anche l’email del nuovo amministratore; configurare
il relay SMTP prima di accedere al portale.
