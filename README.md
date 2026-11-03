# FactoryVPN

**[Guide operative](documentation/README.md)** ·
[Guida amministratore](documentation/ADMINISTRATOR_GUIDE.md) ·
[Guida tecnico](documentation/TECHNICIAN_GUIDE.md)

FactoryVPN permette l'assistenza remota alle LAN industriali, con accessi autorizzati
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
Il tecnico richiede una sessione fino a 30 minuti selezionando il gateway.
Per le nuove sessioni dal portale, logout e scadenza dell'accesso chiudono anche
le VPN aperte da quell'accesso. L'API riserva
indirizzi e rete. Sono disponibili due modalità, configurate per dispositivo:

- **SMS, principale:** il gateway attende un SMS firmato e comunica il comando
  ricevuto al server via HTTPS. In attesa non esegue heartbeat periodici.
- **Wi-Fi:** il gateway controlla il server via HTTPS ogni 30 secondi, senza SIM
  né modem. L'apertura della VPN dipende dalla richiesta autorizzata sul server.

Durante la sessione entrambi usano heartbeat ogni 5 secondi. Il tecnico avvia
il proprio profilo OpenVPN quando il gateway è pronto. La sessione diventa `ACTIVE`
solo dopo la verifica di entrambi i client e la configurazione della rete.

Ogni sessione usa un namespace e un tunnel IPIP trasportato dentro OpenVPN.
Questo consente a siti diversi di usare la stessa LAN, per esempio `192.168.1.0/24`.
Il NAT sul gateway limita l'accesso al tecnico assegnato. L'implementazione prevede
che le autorizzazioni nel firewall scadano entro 30 secondi senza rinnovo, anche
se API, worker o agente si fermano. Verificare questo comportamento nell'installazione.
Sono supportate reti IPv4; `10.100.0.0/16` e `10.250.0.0/16` sono riservate al trasporto.

Una sessione chiusa/scaduta mantiene le proprie risorse riservate fino alla
conferma di pulizia del worker. Un secondo accesso sullo stesso utente o gateway
restituisce `409` durante la sessione e la pulizia.

## Prima installazione

Seguire la [guida di installazione](documentation/DOCKER_GUIDE.md) per server,
HTTPS, SMTP, primo amministratore e gateway. Prima dell'uso operativo predisporre
[backup e recupero](documentation/BACKUP_RESTORE.md) e completare i
[controlli nell'ambiente di destinazione](documentation/DOCKER_GUIDE.md#verifica-prima-delluso-operativo).

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

Il test esercita due LAN sovrapposte, raggiungibilità, rinnovo delle regole,
chiusura indipendente e scadenza delle autorizzazioni nel kernel.
Il simulatore consente inoltre di provare portale, provisioning, agenti e OpenVPN
insieme, con due gateway e LAN simulate. I test locali non sostituiscono i
[controlli nell'ambiente di destinazione](documentation/DOCKER_GUIDE.md#verifica-prima-delluso-operativo).

## Documentazione

- [Guide operative essenziali](documentation/README.md): installazione, amministratore,
  tecnico, backup e risoluzione dei problemi.
- [Sito statico e pubblicazione](website/README.md), [OpenAPI](website/dist/api/openapi.json).

## Licenza

MIT
