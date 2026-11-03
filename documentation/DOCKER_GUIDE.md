# FactoryVPN: prima installazione e gestione operativa

Prima dell'uso operativo completare la
[verifica dell'installazione](#verifica-prima-delluso-operativo), con hardware,
modem e servizi esterni effettivamente utilizzati.

## Stack e confini di rete

`docker-compose.yml` avvia PostgreSQL, API, Nginx, OpenVPN, worker VPN ed emettitore. Solo Nginx
(`80/443`) e OpenVPN (`1194/udp`) pubblicano porte. Il worker condivide il namespace
di rete del server OpenVPN: può quindi gestire il traffico che attraversa `tun0`.
La management interface usa un socket Unix privato, mai una porta TCP pubblica.
Il worker richiede `NET_ADMIN`, `SYS_ADMIN` e il profilo AppArmor non confinato per
creare namespace di rete; il server richiede `NET_ADMIN` e `/dev/net/tun`.

I sorgenti vengono copiati nelle immagini costruite con
`docker compose up --build -d`.
Il worker deve restare una singola istanza per server OpenVPN.

## Preparazione

Servono un server Linux con Docker Compose, Python 3.12, DNS e orologio
sincronizzato. Dalla cartella del repository:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m scripts.bootstrap init --host vpn.example.com
python -m scripts.backup_coordinator init
```

Sostituire `vpn.example.com` con il nome effettivo. Prima dell'avvio controllare
in `.env` l'origin HTTPS `PORTAL_PUBLIC_URL`, `OPENVPN_SERVER_HOST` e la porta
`OPENVPN_SERVER_PORT`. Configurare SMTP come descritto sotto.

File creati:

| Percorso | Contenuto |
|---|---|
| `.env` | Password DB, segreto JWT, chiave worker, chiave di cifratura provisioning e configurazione |
| `.runtime/ca` | CA privata, montata in sola lettura nel solo emettitore |
| `.runtime/server` | Certificato/chiave server, CA pubblica, TLS-crypt, CRL |
| `.runtime/https` | Certificato HTTPS iniziale e chiave |
| PostgreSQL | Registro certificati, hash delle credenziali HTTPS, segreti SMS e pacchetti temporanei cifrati |

File privati scritti con permessi `0600`, directory nuove `0700`. Conservare backup
della CA e del database; non versionare `.env` o `.runtime`. Il comando `init`
serve soltanto alla prima installazione e rifiuta file già presenti. Per produzione
sostituire il certificato HTTPS locale con uno attendibile. Per il collaudo si può usare `curl --cacert
.runtime/ca/ca.crt https://vpn.example.com/health`; non disattivare la verifica TLS.

## Certificati nello Stato server

Il Compose monta nell'API soltanto i PEM pubblici `https/cert.pem`,
`server/server.crt` e `server/ca.crt` della directory runtime, in sola lettura.
Le chiavi private restano fuori da questi mount. L'API deve poter leggere i tre
file con l'UID configurato; non allargare i permessi delle chiavi per abilitarlo.
Per avvio nativo impostare `STATUS_HTTPS_CERTIFICATE_FILE`,
`STATUS_VPN_CERTIFICATE_FILE` e `STATUS_VPN_CA_FILE` ai rispettivi PEM pubblici.

Il controllo verifica le date del primo certificato nel PEM e avvisa a 30 giorni
dalla scadenza. Non verifica catena di fiducia, corrispondenza del nome o il
certificato effettivamente servito da Nginx/OpenVPN.

Dopo un rinnovo, applicare il certificato al servizio interessato. Se il PEM è
stato sostituito atomicamente, ricreare anche l'API perché Docker aggiorni i
bind mount dei singoli file:

```bash
docker compose up -d --no-deps --force-recreate api
```

La ricreazione interrompe brevemente le richieste API. Aggiornare **Stato server**
e verificare nuova scadenza e impronta; collaudare separatamente HTTPS/OpenVPN.
Non rigenerare la CA come semplice rinnovo del certificato HTTPS.

## Server SMTP locale

Dopo il bootstrap, configurare `.env` prima di avviare lo stack:

```dotenv
SMTP_HOST=mail.interno.example
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_FROM=factoryvpn@azienda.example
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_CA_FILE=
```

`EMAIL_CODE_KEY` è generata dal bootstrap, indipendente da JWT e dalle chiavi di
backup. Non copiarla in documenti o repository. Un’installazione senza questa
chiave (almeno 32 caratteri) o senza mittente/host non può inviare i codici.

Modalità disponibili: `starttls` (predefinita, TLS richiesto), `tls` (TLS implicito,
solitamente porta 465), `plain` (solo per relay fidato su rete protetta).
La verifica dei certificati rimane attiva. Per una CA aziendale, montare il file
PEM in sola lettura nel container API con un override Compose e impostare
`SMTP_CA_FILE` al percorso **interno** del file. Username e password sono
facoltativi se il relay autorizza l’API tramite la propria configurazione di rete.
Non configurare il relay come open relay.

`localhost` nel container API indica il container stesso. Usare il DNS/IP del
server di posta o un servizio nella rete Docker. Se il relay gira sull’host, usare
un indirizzo raggiungibile dai container; su Linux `host.docker.internal` richiede
un mapping `host-gateway` nell’override Compose.

Il server SMTP reale e la consegna alle caselle aziendali vanno collaudati
nell’installazione: i test automatici usano un sink che non inoltra messaggi.

## Avvio e primo amministratore

Dopo aver configurato HTTPS e SMTP:

```sh
docker compose config --quiet
docker compose up --build -d
docker compose exec api python -m scripts.create_admin admin --email admin@azienda.example
```

L'API inizializza lo schema PostgreSQL prima dell'avvio. L'amministratore viene
creato con password richiesta da terminale (12–128 caratteri); il comando rifiuta
utenti già esistenti. Per automazione usare `--password-stdin` e un secret manager,
senza inserire password negli argomenti del processo.

Custodire una copia esterna di `.backup-private/recovery.key` e avviare
`python -m scripts.backup_coordinator serve` con lo stesso account del bootstrap,
oppure configurare l'unità systemd descritta nella guida backup.

Aprire `https://<server>/app/`. Il login amministrativo richiede password e codice
email; verificare la consegna alla casella reale prima di proseguire.

## SSO opzionale

SSO è disabilitato con `SSO_PROVIDERS={}`. Per abilitarlo, predisporre un client
OIDC confidenziale con Authorization Code, PKCE S256 e scope `openid profile email`.
La compatibilità va verificata sul provider scelto; il connettore Cognito è
sperimentale e non è ancora stato validato su un User Pool reale.

Per un provider chiamato `company`, registrare la callback
`https://vpn.example.com/api/v1/auth/sso/company/callback`. Sostituire il dominio
con quello dell'installazione. Nel `.env` privato, sostituire tutti i segnaposto:

```dotenv
SSO_PUBLIC_URL=https://vpn.example.com
SSO_TOKEN_KEY=<chiave-Fernet-casuale>
SSO_PROVIDERS={"company":{"kind":"oidc","label":"SSO aziendale","issuer":"https://idp.example.com","client_id":"<client-id>","client_secret":"<client-secret>"}}
```

`SSO_PUBLIC_URL` deve coincidere con `PORTAL_PUBLIC_URL`: origin HTTPS senza path
o slash finale. Generare una sola volta una chiave Fernet con
`cryptography.fernet.Fernet.generate_key()` nell'ambiente Python del progetto.
Custodirla con il segreto del client nel `.env` protetto (`0600`) o in un secret
store; non riutilizzare `JWT_SECRET` e non rigenerarla a ogni avvio. La sua
sostituzione rende illeggibili i token provider precedenti.

Se discovery o altri endpoint usano origin diversi dall'issuer, indicarli
esplicitamente nell'elenco `endpoint_origins` del provider. Gli endpoint devono
usare HTTPS. L'autenticazione del client usa `client_secret_basic`; configurare
`token_endpoint_auth_method: "client_secret_post"` solo se richiesto dal provider.
Il metodo vale sia per lo scambio del codice sia per la revoca.

Per Cognito usare `kind: "cognito"`, l'issuer del pool e l'origin del managed login
in `endpoint_origins`. Aggiungere `logout_endpoint` con il relativo URL `/logout`
e registrare come sign-out URL
`https://vpn.example.com/api/v1/auth/sso/signed-out`. Issuer e dominio di login
sono distinti; sostituire anche questi valori con quelli effettivi.

Gli amministratori SSO devono presentare attestazioni MFA firmate: il default
accetta `amr` contenente `mfa`. Configurare `mfa_amr_values`, `mfa_acr_values` e,
se necessario, `mfa_request_acr` soltanto con valori verificati sul provider.
Il semplice successo del login non attesta MFA. Le operazioni sensibili richiedono
anche un'autenticazione recente attestata dal provider.

Ricreare API e worker dopo aver aggiornato la configurazione. Dal portale un
amministratore globale collega esplicitamente il provider e il `sub` all'account
FactoryVPN e ne imposta il metodo di accesso SSO. Email e gruppi esterni non
creano account né assegnano permessi. Conservare un amministratore locale attivo
per il recupero e verificare prima un singolo account.

Le sessioni SSO non si rinnovano automaticamente. La durata massima predefinita
è 30 minuti, ridotta dalle scadenze dei token del provider. Per OIDC generico la
revoca è limitata alla scadenza locale; abilitare `revocation_check: "userinfo"`
solo dopo aver verificato che UserInfo rifiuti i token revocati. Cognito utilizza
già questo controllo. I valori predefiniti sono un controllo ogni 60 secondi e
un limite di 120 secondi senza verifica riuscita. Collaudare logout, revoca,
guasti del provider e chiusura delle VPN nell'installazione.

## Utenti e gateway

1. Accedere al portale con l'amministratore iniziale. Creare gruppi e utenti.
2. Da **Le mie VPN → Aggiungi gateway**, inserire nome, gruppo, modalità SMS/Wi-Fi
   e LAN; il MAC è richiesto per Wi-Fi e facoltativo per SMS. Solo amministratori
   globali o titolari di `gateways.provision`
   nei gruppi assegnati possono preparare le credenziali.
3. Scaricare il pacchetto ZIP dallo stesso accesso che l'ha richiesto, quindi
   attivarlo nel portale. Installare `gateway.env` e `gateway.ovpn` in
   `/etc/factoryvpn/`, con permessi `0600`, e avviare l'agente.
4. Verificare la registrazione nella scheda del gateway. Il server controlla che
   UUID e configurazione coincidano con il provisioning; il gruppo è già assegnato.
5. Ogni tecnico prepara, scarica e attiva il proprio `.ovpn` in **Il mio profilo**.
   Importarlo nel client OpenVPN e avviarlo manualmente dopo aver aperto una VPN
   nel portale. Lo stesso percorso vale per account locali e SSO.

Il servizio `provisioning-worker` emette certificati e pubblica la CRL. L'API non
monta la CA; nessun riavvio API è necessario per aggiungere un gateway. I download
scadono entro 15 minuti con la configurazione predefinita e sono rimossi all'attivazione; perdendo il file
occorre preparare un nuovo profilo. I pacchetti includono chiavi private: conservarli
in modo protetto. Per integrare il provisioning in altri strumenti consultare il
[contratto OpenAPI](../website/dist/api/openapi.json).

## Agente Linux

Installare il progetto sotto `/opt/factoryvpn`, le dipendenze Python nella
relativa `.venv` e i pacchetti Linux `openvpn iproute2 nftables procps`.
Installare il file generato dal provisioning in `/etc/factoryvpn/gateway.env`.
`gateway-agent/gateway.env.example` descrive le opzioni: UUID, chiave, modalità,
interfaccia LAN, indirizzi effettivi e URL HTTPS del server.
Impostare permessi `0600`. La CA HTTPS locale va installata nel trust store del
sistema oppure indicata con `SSL_CERT_FILE`; in produzione usare TLS attendibile.

Installare `gateway-agent/factoryvpn-agent.service` con systemd e avviare il servizio.
L'unità usa privilegi root per modem, OpenVPN, rotte e firewall. I percorsi nell'unità
vanno adattati se l'installazione usa una directory diversa.
L'interfaccia LAN deve essere già configurata dal sistema operativo e appartenere
al CIDR registrato. L'agent necessita di connettività HTTPS indipendente dal tunnel
VPN per registrazione, heartbeat e recupero. Mantenere sincronizzati gli orologi.

Impostare `GATEWAY_ACTIVATION_MODE=sms` (default) oppure `wifi`.

`MAC_ADDRESS` può essere vuoto per SMS; resta richiesto per Wi-Fi. Per rilevare
il MAC radio indicare anche `GATEWAY_WIFI_INTERFACE` (per esempio `wlan0`):
l'agente verifica che sia realmente wireless. Non usare l'interfaccia LAN come
sostituto. `GATEWAY_HARDWARE_MODEL` e `GATEWAY_HARDWARE_REVISION` sono descrizioni
facoltative configurate dall'operatore. IMEI, modello e firmware del modem vengono
letti automaticamente quando supportati. Le versioni sono tratte dall'installazione
locale; conservare `pyproject.toml` quando si installa copiando i sorgenti.

- SMS richiede `GATEWAY_PHONE_NUMBER` e `MODEM_PATH`. Dopo la registrazione iniziale,
  l'agente legge il modem ogni `GATEWAY_POLL_INTERVAL` secondi (default 5) e attende
  un messaggio firmato. Inattivo non esegue heartbeat HTTPS. Il comando viene
  mantenuto sul modem fino alla rete pronta, per poter ritentare dopo un errore.
- Wi-Fi non richiede SIM né modem e non apre la porta seriale. L'agente controlla
  HTTPS ogni `GATEWAY_WIFI_CHECK_INTERVAL` secondi (default 30, intervallo 5–300).
- Durante una sessione entrambe rinnovano HTTPS e lease normalmente ogni 5 secondi
  (`GATEWAY_POLL_INTERVAL`, intervallo 1–10). Chiusura/scadenza riportano all'attesa.

Dopo un riavvio, SMS richiede un comando valido ancora nel modem o un nuovo invio
`POST /api/v1/sessions/{id}/wake`; Wi-Fi recupera una richiesta valida al controllo
successivo. Messaggi scaduti, alterati o di altre sessioni non concedono accesso.
La connettività Internet deve essere disponibile per HTTPS e VPN: Wi-Fi e dati
cellulari, APN/PPP/WWAN sono configurati dal sistema operativo. L'agente non apre
né chiude il collegamento dati del modem. Il gateway deve essere alimentato.

## Uso e chiusura

Il tecnico apre la VPN dal portale e avvia il proprio client quando il gateway
è pronto. Seguire la [guida tecnico](TECHNICIAN_GUIDE.md). Dopo la chiusura attendere
il rilascio delle risorse; se il gateway resta occupato consultare la
[risoluzione dei problemi](TROUBLESHOOTING.md).

## SMS

`SMS_PROVIDER=generic` invia JSON `{"to":"+...","message":"..."}` con header
`Authorization: Bearer <SMS_PROVIDER_API_KEY>` a `SMS_PROVIDER_URL`.
Qualunque risposta 2xx significa accettazione secondo questo contratto; errori,
redirect e timeout producono `FAILED` salvo ricezione già confermata durante
l'invio. Il provider deve implementare tale contratto. Wi-Fi non usa il provider.

`SMS_PROVIDER=twilio` usa le credenziali `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
`TWILIO_FROM`, con autenticazione Basic e form `To`, `From`, `Body`. L'API controlla
anche lo stato restituito. I test usano risposte simulate, senza inviare SMS reali.
L'accettazione non certifica la consegna: il gateway restituisce il comando firmato
via HTTPS per confermarla. La connessione VPN e la rete pronta determinano la
disponibilità operativa. Riferimento:
[Twilio Message resource](https://www.twilio.com/docs/messaging/api/message-resource).

## Prima configurazione dei gruppi

Creare gruppi e utenti, assegnare i gateway e concedere i soli permessi necessari
secondo la [guida amministratore](ADMINISTRATOR_GUIDE.md#assegnare-i-permessi).
Verificare gli accessi con due tecnici di gruppi distinti. Un gateway senza gruppo
non consente VPN.

## Backup e ripristino

Il portale comprende backup manuali cifrati, importazione, prove isolate e ripristino
guidato per amministratori globali. Prima del primo avvio dello stack inizializzare
il coordinatore con `python -m scripts.backup_coordinator init`, usando l'account
che lo eseguirà; custodire la chiave fuori dal server e avviare il relativo servizio.
Il flusso completo e il recupero in caso di guasto sono descritti in
[Backup e ripristino](BACKUP_RESTORE.md).

## Rotazione e manutenzione

- Rinnovo profilo: preparare e scaricare il sostituto, chiudere le VPN e attendere
  la pulizia, quindi attivarlo dal portale. Il precedente viene revocato.
- Revoca d'emergenza: usare **Revoca credenziali VPN**. Nuove autorizzazioni sono
  bloccate nel database; il worker chiude le connessioni e l'emettitore aggiorna la CRL.
- Spostare un gateway di gruppo revoca le vecchie credenziali: preparare e installare
  un nuovo pacchetto nel gruppo di destinazione. Anche i pacchetti pendenti sono annullati.
- La CRL viene rinnovata automaticamente per sette giorni, con rinnovo quando resta
  meno di un giorno. I certificati client durano un anno. La CA, il certificato del
  server VPN e HTTPS restano manutenzione dell'installazione.
- Conservare insieme database, CA, configurazione e `PROVISIONING_SECRET_KEY` nei
  backup protetti. Cambiare quest'ultima senza ricifrare i dati rende indisponibili
  i segreti SMS esistenti. Non rigenerarla a ogni riavvio.
- Rotazione chiave worker: impostare il nuovo valore in `.env` e ricreare API,
  OpenVPN, worker ed emettitore insieme.
- Se si cambia `POSTGRES_PASSWORD` dopo la prima installazione, impostare anche
  la password del ruolo PostgreSQL: l'immagine non la cambia in un volume esistente.

## Controllo dei servizi

```bash
docker compose ps
docker compose logs --tail=100 api openvpn session-worker provisioning-worker
docker compose exec session-worker ip netns list
docker compose exec session-worker nft list table inet factoryvpn_support
docker compose exec api alembic current
```

`/health` sull'API verifica il processo, `/health/ready` verifica anche PostgreSQL;
`https://<host>/health` su Nginx inoltra il controllo di readiness dell'API.
Per le anomalie seguire [risoluzione dei problemi](TROUBLESHOOTING.md).

## Verifica prima dell'uso operativo

Registrare versione, data ed esito dei controlli nell'ambiente di destinazione:

1. Verificare HTTPS attendibile, DNS, orologi e porte esposte; controllare stato
   e freschezza dei servizi in **Stato server**.
2. Provare il login amministrativo con email reale, il recupero dell'accesso e,
   se abilitato, SSO con MFA e revoca sul provider configurato.
3. Con due gruppi distinti, verificare che utenti e delegati vedano soltanto
   gateway, sessioni, log ed esportazioni consentiti.
4. Eseguire provisioning, attivazione, rinnovo e revoca su gateway reali. Provare
   SMS e Wi-Fi, client tecnico e accesso alla LAN; verificare l'isolamento di
   siti con indirizzi LAN sovrapposti.
5. Verificare chiusura, logout, scadenza e rilascio delle risorse. In una finestra
   di manutenzione, interrompere API, worker e agente separatamente: il traffico
   deve interrompersi alla scadenza delle autorizzazioni, senza riaprirsi da solo.
6. Verificare copia esterna, chiave separata, prova isolata e recupero secondo la
   [guida backup](BACKUP_RESTORE.md).

Il simulatore e i test automatici non certificano modem, radio, provider SMS,
SMTP o identità dell'installazione reale.
