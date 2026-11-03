# Backup e ripristino di FactoryVPN

Implementazione per **nuove installazioni**, con schema iniziale unico. Non sono
previsti aggiornamenti o conversioni di database da versioni precedenti.

## Perimetro

Il portale offre backup manuali, elenco, download cifrato, conservazione,
importazione verificata, prova isolata e ripristino completo. Ogni operazione è
riservata agli amministratori **globali**, anche tramite API. I delegati di gruppo
non possono vedere metadati, richiedere copie o scaricare archivi.

Il backup comprende PostgreSQL (utenti, gruppi, permessi, inventario, storico,
audit, certificati e credenziali), configurazione applicativa effettiva, CA,
certificati/chiavi OpenVPN e HTTPS, TLS-crypt e lista delle revoche. Non salva il
sistema operativo del server, dei gateway, i programmi PLC o le chiavi private dei
client che non sono conservate sul server. Il codice si reinstalla dalla **stessa
versione**. La firma dello schema è derivata dal DDL iniziale, non dal solo nome
`factoryvpn_initial`.

Il primo rilascio usa un archivio locale e file scaricabili per una copia esterna.
Pianificazione, destinazioni remote (ad esempio S3), notifiche e recupero a un
istante arbitrario tramite WAL non sono inclusi.

## Installazione

Il coordinatore gira sull'host Linux con Docker Compose e Python 3.12, utilizzando
le dipendenze di `requirements.txt`. L'API **non** riceve socket Docker, chiave di
recupero o accesso alla CA. L'account che esegue il coordinatore controlla lo stack
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
docker compose up -d --build
.venv/bin/python -m scripts.backup_coordinator serve
```

Il modello `deploy/factoryvpn-backup.service` permette la gestione con systemd;
adattare percorso, account e gruppo Docker prima di installarlo. Il coordinatore
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
chiavi né modificare i file del runtime.

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

Se un passaggio fallisce dopo la sostituzione, il coordinatore tenta il ripristino
della copia di sicurezza. In caso di perdita del processo, il giornale viene
recuperato al riavvio. Se anche il recupero fallisce, la manutenzione rimane attiva;
non viene dichiarato successo. Eventi e job restano nel catalogo esterno al database.

## Recupero da console

Per riprendere un recupero interrotto quando il portale è indisponibile:

```sh
.venv/bin/python -m scripts.backup_coordinator recover
```

In caso di perdita dell'intero server, predisporre una nuova installazione della
stessa versione, il suo `.env` e runtime, poi inizializzare con la chiave custodita
separatamente. Avviare database e stack iniziale prima del comando di ripristino:

```sh
.venv/bin/python -m scripts.backup_coordinator init --recovery-key-file /percorso/protetto/recovery.key
docker compose up -d --build
.venv/bin/python -m scripts.backup_coordinator restore-file --archive /percorso/protetto/copia.fvbackup
```

Il comando richiede conferma e un **nuovo amministratore locale** con password
ed email raggiungibile. La password viene inserita senza eco. Non conoscendo le revoche successive alla copia, disabilita i
vecchi account e revoca le credenziali VPN/gateway recuperate. L'amministratore deve
riconvalidare gli accessi e ripetere il provisioning. Il recupero da console non
presuppone che un vecchio backup rappresenti autorizzazioni ancora valide.

## Verifica dopo il recupero

Accedere con un codice email nuovo, verificare gruppi e assegnazioni, preparare
nuovi profili quando richiesto e provare una VPN completa verso un bersaglio LAN.
Conservare separatamente chiave di recupero e copia esterna. Dalla pagina
**Backup e ripristino**, eseguire una prova isolata della copia più recente
e controllarne l'esito prima di considerarla recuperabile.

Configurare il [relay SMTP](DOCKER_GUIDE.md#server-smtp-locale) prima del login.
Il ripristino conserva email, verifica della casella e politica corrente, elimina
le richieste di codice e mantiene SMTP/EMAIL_CODE_KEY del server di destinazione.
Il recupero disastro richiede anche l’email del nuovo amministratore; configurare
il relay SMTP prima di accedere al portale.
