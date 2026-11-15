# FactoryVPN

**[Guide operative](documentation/README.md)** ·
[Guida amministratore](documentation/ADMINISTRATOR_GUIDE.md) ·
[Guida tecnico](documentation/TECHNICIAN_GUIDE.md)

FactoryVPN permette l'assistenza remota ai gateway industriali, con accessi autorizzati
su richiesta e limitati nel tempo. La base software comprende API FastAPI/PostgreSQL,
server OpenVPN, worker di rete e agente Linux con attivazione SMS o controllo Wi-Fi.

Il [portale web](documentation/ADMINISTRATOR_GUIDE.md) gestisce utenti, gruppi, permessi, gateway e sessioni
VPN, con accesso locale e SSO in `/app/`. Il tecnico seleziona un gateway e segue
apertura, stato e chiusura; il client OpenVPN viene avviato manualmente.
Gli amministratori globali dispongono di [backup cifrati e ripristino guidato](documentation/BACKUP_RESTORE.md),
con un coordinatore separato dal portale per gestire anche il recupero dopo un guasto.
Impostare `PORTAL_PUBLIC_URL`
all’origin HTTPS dell’istanza per abilitare le sessioni browser.
Prima dell'uso operativo occorre collaudare l'installazione su hardware, modem e
provider reali. L'integrazione SSO comprende OIDC e un connettore **Amazon Cognito
sperimentale**, coperto da test locali ma non ancora validato su un User Pool reale.
La [configurazione SSO](documentation/DOCKER_GUIDE.md#sso-opzionale) descrive
i requisiti di installazione e i limiti di revoca.
La pagina **Client VPN** riunisce supervisione, collegamenti osservati, ricerca
dei tecnici e chiusura delle sessioni. Le schede gateway mostrano identificativi,
versioni dei componenti, log tecnici e storico delle metriche; **Stato server**
raccoglie componenti centrali, certificati, backup e verifica SMTP.

## Funzionamento

Ogni gateway ha un UUID stabile, una chiave HTTPS e un profilo VPN individuali.
L'amministratore assegna ogni gateway a un gruppo e gli utenti ai gruppi di
competenza. Il permesso `connect` abilita le sessioni; le altre capacità
consentono amministrazione delegata entro gli stessi gruppi. Un gateway non
assegnato non consente sessioni. La gestione comprende profilo, cambio password,
logout globale, revoca dei token e degli accessi VPN. Vedere i
[permessi amministrativi](documentation/ADMINISTRATOR_GUIDE.md#assegnare-i-permessi).
Il tecnico richiede una sessione fino a 3 ore per default selezionando il gateway;
il login al portale dura 12 ore per default. Le due scadenze non si rinnovano con l'uso.
Per le nuove sessioni dal portale, logout e scadenza dell'accesso chiudono anche
le VPN aperte da quell'accesso. L'API prenota la coppia tecnico/gateway per tutta
la sessione e la successiva pulizia. Sono disponibili due modalità, configurate per dispositivo:

- **SMS, principale:** il gateway attende un SMS firmato e comunica il comando
  ricevuto al server via HTTPS. In attesa non esegue heartbeat periodici.
- **Wi-Fi:** il gateway controlla il server via HTTPS ogni 30 secondi per default,
  senza SIM né modem. L'apertura della VPN dipende dalla richiesta autorizzata sul server.

Durante la sessione entrambi usano heartbeat normalmente ogni 5 secondi, configurabili.
Il tecnico avvia il proprio profilo OpenVPN quando il gateway è pronto. La sessione diventa `ACTIVE`
solo dopo la verifica di entrambi i client e la configurazione della rete.

Il modello **Gateway Only** permette al tecnico assegnato di raggiungere il solo
IP VPN fisso del gateway, sulle porte TCP **22 (SSH), 80 (HTTP), 443 (HTTPS)**.
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
I servizi SSH e web, gli account Linux e i certificati HTTPS del servizio web
devono essere predisposti sul dispositivo.

Una sessione chiusa/scaduta mantiene le proprie risorse riservate fino alla
conferma di pulizia del worker. Un secondo accesso sullo stesso utente o gateway
restituisce `409` durante la sessione e la pulizia.

## Prima installazione

Seguire la [guida di installazione](documentation/DOCKER_GUIDE.md) per server,
HTTPS, SMTP, primo amministratore e gateway. Prima dell'uso operativo predisporre
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

## Verifiche

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
pytest -q
ruff check api gateway-agent session-worker factoryvpn_common scripts tests migrations docker/openvpn/entrypoint.py
# URL di un database di test: vengono creati e rimossi schemi isolati.
FACTORYVPN_TEST_DATABASE_URL=postgresql+asyncpg://user:password@localhost/test_db pytest -q
```

Il test di rete richiede un container Linux temporaneo con privilegi per creare
namespace. Non eseguire `tests/network_smoke.py` direttamente sull'host:

```bash
docker build -f docker/Dockerfile.network --target test -t factoryvpn-network-test .
docker run --rm --privileged --network none -e FACTORYVPN_NETWORK_TEST=1 factoryvpn-network-test
```

Il test esercita SSH, HTTP e HTTPS reali, LAN diretta negata, split tunnel,
Internet locale simulato, chiusura indipendente e scadenza delle autorizzazioni
nel kernel anche sull'output SSH. Verifica inoltre che i guasti API temporanei
non prolunghino il lease già installato.
Il simulatore consente inoltre di provare portale, provisioning, agenti e OpenVPN
insieme, con due gateway e LAN simulate. I test locali non sostituiscono i
[controlli nell'ambiente di destinazione](documentation/DOCKER_GUIDE.md#verifica-prima-delluso-operativo).

## Documentazione

- [Guide operative essenziali](documentation/README.md): installazione, amministratore,
  tecnico, backup e risoluzione dei problemi.
- Per gli sviluppatori gateway: [prima inizializzazione](documentation/gateway/INITIAL_SETUP.md)
  e [rinnovo remoto dei certificati](documentation/gateway/CERTIFICATE_RENEWAL.md).
- [Sito statico e pubblicazione](website/README.md), [OpenAPI](website/dist/api/openapi.json).

## Licenza

MIT
