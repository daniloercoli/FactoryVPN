# FactoryVPN

**[Guide operative](documentation/README.md)** ·
[Guida amministratore](documentation/ADMINISTRATOR_GUIDE.md) ·
[Guida tecnico](documentation/TECHNICIAN_GUIDE.md)

FactoryVPN permette l'assistenza remota ai gateway industriali, con accessi autorizzati
su richiesta e limitati nel tempo. La base software comprende API FastAPI/PostgreSQL,
portale React/TypeScript servito da Nginx, server OpenVPN, worker di rete e agente
Linux con attivazione SMS o controllo Wi-Fi.

La versione corrente di FactoryVPN e delle API è **0.9.0**, con modello
**Gateway Only**. È in corso il completamento del rilascio **1.0**.
La funzionalità [Single LAN](documentation/gateway/SINGLE_LAN_V2.md) è prevista
**a partire dalla versione 2.0, attualmente non in fase di progettazione**.
L'accesso diretto dal PC alla LAN del gateway non fa parte della versione corrente.

Il [portale web](documentation/ADMINISTRATOR_GUIDE.md) gestisce utenti, gruppi, permessi, gateway e sessioni
VPN, con accesso locale e SSO in `/app/`. Il tecnico seleziona un gateway e segue
apertura, stato e chiusura; il client OpenVPN viene avviato manualmente.
Il login locale amministrativo richiede password e codice email; per gli
amministratori SSO il provider deve attestare MFA. Durante un guasto della posta,
gli account locali possono usare un [codice di emergenza da console](documentation/TROUBLESHOOTING.md#accesso-di-emergenza-durante-un-guasto-della-posta)
insieme alla password, senza modificare le normali durate di accesso e VPN.
Gli amministratori globali dispongono di [backup manuali cifrati e ripristino guidato](documentation/BACKUP_RESTORE.md),
con un coordinatore separato dal portale per gestire anche il recupero dopo un guasto.
Impostare `PORTAL_PUBLIC_URL`
all’origin HTTPS dell’istanza per abilitare le sessioni browser.
Il nome visualizzato si imposta con `APP_NAME` nel `.env` prima della build;
vedere [personalizzazione del nome](documentation/DOCKER_GUIDE.md#nome-visualizzato-dellapplicazione).
Anche il [sito statico](website/README.md#build) usa `APP_NAME`, incorporato nei
file pubblicabili dalla sua build Python.
Prima dell'uso operativo occorre collaudare l'installazione su hardware, modem e
provider reali. L'integrazione SSO comprende OIDC e un connettore **Amazon Cognito
sperimentale**, coperto da test locali ma non ancora validato su un User Pool reale.
La [configurazione SSO](documentation/DOCKER_GUIDE.md#sso-opzionale) descrive
i requisiti di installazione e i limiti di revoca.
La pagina **Client VPN** riunisce supervisione, collegamenti osservati, ricerca
dei tecnici e chiusura delle sessioni. Le schede gateway mostrano identificativi,
versioni dei componenti, log tecnici e storico delle metriche; **Stato server**
raccoglie componenti centrali, certificati, backup e verifica SMTP.
Cronologia e audit descrivono accessi e operazioni amministrative, non le modifiche
eseguite sui PLC. Telemetria e backup non includono allarmi automatici,
pianificazione dei backup o copia remota automatica.

Le credenziali VPN sono individuali. Il [rinnovo remoto dei certificati gateway](documentation/gateway/CERTIFICATE_RENEWAL.md)
usa una CSR via HTTPS con chiave generata sul dispositivo; il rinnovo non aggira
revoca delle credenziali o cambio del gruppo assegnato.

## Funzionamento

Ogni gateway ha un UUID stabile, una chiave HTTPS e un profilo VPN individuali.
L'amministratore assegna ogni gateway a un gruppo e gli utenti ai gruppi di
competenza. Il permesso `connect` abilita le sessioni; le altre capacità
consentono amministrazione delegata entro gli stessi gruppi. Un gateway non
assegnato non consente sessioni. La gestione comprende profilo, cambio password,
logout globale, revoca dei token e degli accessi VPN. Vedere i
[permessi amministrativi](documentation/ADMINISTRATOR_GUIDE.md#assegnare-i-permessi).
Il tecnico richiede una sessione fino a 3 ore dalla richiesta per default selezionando il gateway;
il login al portale dura 12 ore per default. Le due scadenze non si rinnovano con l'uso.
Per le sessioni dal portale, logout e scadenza dell'accesso chiudono anche
le VPN aperte da quell'accesso. L'API prenota la coppia tecnico/gateway per tutta
la sessione e la successiva pulizia. Sono disponibili due modalità, configurate per dispositivo:

- **SMS, principale:** il gateway attende un SMS firmato e comunica il comando
  ricevuto al server via HTTPS. In attesa non esegue heartbeat periodici.
- **Wi-Fi:** il gateway controlla il server via HTTPS ogni 30 secondi per default,
  senza SIM né modem. L'apertura della VPN dipende dalla richiesta autorizzata sul server.

Durante la sessione entrambi usano heartbeat normalmente ogni 5 secondi, configurabili.
Il tecnico avvia il proprio profilo OpenVPN quando il gateway è pronto. Il worker
verifica entrambi i peer, prepara la rete bloccata e ne comunica la prontezza
all'API, che registra `ACTIVE`. Solo dopo la conferma API abilita il lease del
firewall server. Lo stato registrato non sostituisce la verifica del traffico
effettivo e delle osservazioni recenti nella diagnostica.

Il modello **Gateway Only** permette al tecnico assegnato di raggiungere il solo
IP VPN fisso del gateway, senza filtri VPN su porte o protocolli IPv4: per esempio
SSH, web anche su porte personalizzate, UDP e ping ICMP. Il traffico è bidirezionale
solo fra i due peer della sessione autorizzata.
La LAN non viene instradata: niente IPIP, namespace di sessione o NAT nel gateway.
Le autorizzazioni nel firewall scadono entro 5 minuti senza rinnovo per default,
anche se API, worker o agente si fermano. Durate e timeout sono configurabili nel
`.env`: vedere [reti instabili](documentation/DOCKER_GUIDE.md#durate-timeout-e-reti-instabili).
Gli errori API temporanei conservano il lease residuo senza prolungarlo. Logout e
revoca bloccano appena ricevuti; durante un guasto possono attendere quel termine.
Le scadenze VPN, browser e SSO possono ridurre ulteriormente il tempo disponibile.
Sono supportate reti IPv4; `10.100.0.0/16` è riservato alla VPN.

Il tecnico usa uno **split tunnel verso il gateway**: Internet, DNS e IPv6
restano configurati sul suo computer. Per operare sulle reti raggiungibili
dal gateway, entra in [SSH sul suo IP VPN fisso](documentation/TECHNICIAN_GUIDE.md#entrare-in-ssh-sul-gateway)
con un account Linux del dispositivo. La shell usa le rotte correnti del gateway,
comprese quelle aggiunte durante l'intervento.
I servizi devono essere predisposti sul dispositivo e ascoltare sull'IP VPN
(o su tutte le interfacce); il firewall locale può limitarli. Per SSH servono
account Linux del gateway, per HTTPS certificati attendibili del servizio web.
È un accesso IP al gateway: non estende Ethernet o la LAN del cliente al PC
e non trasporta discovery broadcast.

Una sessione chiusa/scaduta mantiene le proprie risorse riservate fino alla
conferma di pulizia del worker. Un secondo accesso sullo stesso utente o gateway
restituisce `409` durante la sessione e la pulizia.

## API e integrazione

La [guida per sviluppatori](website/dist/developers.html) e il
[contratto OpenAPI](website/dist/api/openapi.json) descrivono le API HTTP per
autenticazione, gateway, sessioni e amministrazione. I connettori verso portali,
service desk o altre applicazioni vanno sviluppati per la specifica integrazione;
le API non avviano automaticamente il client OpenVPN sul PC del tecnico.

L'apertura di sessione supporta `Idempotency-Key`: in caso di risposta persa,
riutilizzare la stessa chiave e lo stesso body per recuperare la richiesta.
Il contratto pubblico del sito esclude le API riservate a gateway e worker;
i protocolli gateway sono descritti nelle [guide di integrazione](documentation/README.md).
Lo schema completo dell'istanza FastAPI resta disponibile in `/openapi.json`.
La versione del software è distinta da quella del protocollo: il prefisso
delle API resta `/api/v1`.

## Prima installazione

Seguire la [guida di installazione](documentation/DOCKER_GUIDE.md) per server,
HTTPS, SMTP, primo amministratore e gateway, usando un database nuovo.
Il modello previsto è una **nuova installazione**, senza migrazioni da versioni
precedenti. Prima dell'uso operativo predisporre
[backup e recupero](documentation/BACKUP_RESTORE.md) e completare i
[controlli nell'ambiente di destinazione](documentation/DOCKER_GUIDE.md#verifica-prima-delluso-operativo).

Il gateway può essere registrato a fine linea e completare l'inizializzazione via
HTTPS dopo approvazione e assegnazione al gruppo. Il percorso manuale HQ resta
disponibile anche per il laboratorio: creazione, download e attivazione dello ZIP
dal portale, poi copia di `gateway.env` e `gateway.ovpn` sul dispositivo.
Entrambi sono descritti nella [prima inizializzazione](documentation/gateway/INITIAL_SETUP.md).
L'autoarruolamento senza passaggio a fine linea resta un'evoluzione futura.
Anche il tecnico deve preparare, scaricare e attivare il proprio profilo OpenVPN
prima di richiedere una sessione.

Il repository include un simulatore gateway su Alpine Linux, con un gateway
SMS/IMEI e uno Wi-Fi/MAC, agente e connessioni OpenVPN reali. Richiede Docker e
si controlla da terminale; la grafica per sessioni e diagnostica è il portale
FactoryVPN. Consultare `.venv/bin/python -m scripts.gateway_simulator --help`
per i comandi disponibili. Il modem e le reti radio sono simulati: nessun SMS
viene inviato a una rete telefonica.

Per eseguire **un solo gateway Wi-Fi/polling** contro un server già installato,
senza creare il laboratorio, seguire la
[mini guida Docker gateway](documentation/gateway/DOCKER_SIMULATOR.md).

## Verifiche

Preparare l'ambiente Python della [guida di installazione](documentation/DOCKER_GUIDE.md#preparazione)
e aggiungere le dipendenze di sviluppo dalla radice dei sorgenti:

```bash
.venv/bin/python -m pip install -e '.[dev]'
```

Per eseguire tutta la suite, compresi i test PostgreSQL, con Docker attivo:

```bash
.venv/bin/python -m scripts.test_with_postgres
```

Il comando crea un PostgreSQL 15 temporaneo, attende che sia pronto, esegue pytest
e rimuove il container anche se i test falliscono. Usa una porta casuale su
`127.0.0.1`, credenziali casuali e dati in memoria, separati dal database applicativo.
Accetta anche argomenti pytest, per esempio
`.venv/bin/python -m scripts.test_with_postgres -q tests/test_initial_schema.py`.

Per la suite senza database oppure con un database di test già disponibile:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check api gateway-agent session-worker factoryvpn_common scripts tests migrations docker/openvpn/entrypoint.py
# URL di un database di test: vengono creati e rimossi schemi isolati.
FACTORYVPN_TEST_DATABASE_URL=postgresql+asyncpg://user:password@localhost/test_db .venv/bin/pytest -q
```

Il test di rete richiede un container Linux temporaneo con privilegi per creare
namespace. Non eseguire `tests/network_smoke.py` direttamente sull'host:

```bash
docker build -f docker/Dockerfile.network --target test -t factoryvpn-network-test .
docker run --rm --privileged --network none -e FACTORYVPN_NETWORK_TEST=1 factoryvpn-network-test
```

Il test esercita SSH, HTTP anche su porta 8080, HTTPS, UDP e ping ICMP,
traffico bidirezionale fra i peer autorizzati, LAN diretta negata, split tunnel,
Internet locale simulato, chiusura indipendente e scadenza delle autorizzazioni
nel kernel anche sull'output SSH. Verifica inoltre che i guasti API temporanei
non prolunghino il lease già installato.
Il simulatore consente inoltre di provare portale, provisioning, agenti e OpenVPN
insieme, con due gateway e LAN simulate. I test locali non sostituiscono i
[controlli nell'ambiente di destinazione](documentation/DOCKER_GUIDE.md#verifica-prima-delluso-operativo).

## Documentazione

- [Guide operative essenziali](documentation/README.md): installazione, amministratore,
  tecnico, backup e risoluzione dei problemi.
- Per gli sviluppatori gateway: [prima inizializzazione](documentation/gateway/INITIAL_SETUP.md),
  [arruolamento di fine linea](documentation/gateway/FACTORY_ENROLLMENT.md),
  [protocollo operativo](documentation/gateway/RUNTIME_PROTOCOL.md) e
  [rinnovo remoto dei certificati](documentation/gateway/CERTIFICATE_RENEWAL.md).
- [Guida alle API](website/dist/developers.html), [OpenAPI](website/dist/api/openapi.json),
  [sito statico e pubblicazione](website/README.md).
- [Single LAN dalla versione 2.0](documentation/gateway/SINGLE_LAN_V2.md):
  comportamento futuro e requisiti da definire, non funzionalità della versione corrente.
- [MQTT dalla versione 2.1](documentation/gateway/MQTT_V2_1.md):
  futura messaggistica gateway–piattaforma, benefici e vincoli d'integrazione.

## Licenza

[MIT](LICENSE)
