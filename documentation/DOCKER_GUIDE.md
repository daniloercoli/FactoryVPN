# FactoryVPN: prima installazione e gestione operativa

Prima dell'uso operativo completare la
[verifica dell'installazione](#verifica-prima-delluso-operativo), con hardware,
modem e servizi esterni effettivamente utilizzati.

## Stack e confini di rete

`docker-compose.yml` avvia PostgreSQL, API, Nginx, OpenVPN, worker VPN ed emettitore. Solo Nginx
(`80/443`) e OpenVPN (`1194/udp` per default, configurabile con `OPENVPN_SERVER_PORT`)
pubblicano porte. Il worker condivide il namespace
di rete del server OpenVPN: può quindi gestire il traffico che attraversa `tun0`.
La management interface usa un socket Unix privato, mai una porta TCP pubblica.
Il worker richiede soltanto `NET_ADMIN` per gestire il firewall; il server richiede `NET_ADMIN` e `/dev/net/tun`.

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
# Proseguire con la cerimonia offline qui sotto, prima del bootstrap.
```

Sostituire `vpn.example.com` con il nome effettivo. Prima dell'avvio controllare
in `.env` l'origin HTTPS `PORTAL_PUBLIC_URL`, `OPENVPN_SERVER_HOST` e la porta
`OPENVPN_SERVER_PORT`. Configurare SMTP come descritto sotto.

### Root offline e Sub-CA del server

La chiave root viene creata su una macchina offline e non deve entrare nel server.
Questi comandi usano tre cartelle: `root-offline` soltanto sulla macchina offline,
`.runtime` sul server e una cartella di trasferimento contenente solo dati pubblici.
I nomi dei file CSR sono restituiti dal comando `prepare`.

```bash
# Macchina offline: inserire e confermare la passphrase richiesta.
python -m scripts.pki --directory /supporto/root-offline root-init
# Annotare l'impronta pubblica restituita e custodire due copie offline protette.

# Server: genera la chiave della Sub-CA e stampa il percorso della CSR pubblica.
python -m scripts.pki --directory .runtime prepare

# Macchina offline: dopo aver trasferito e verificato la CSR del server.
python -m scripts.pki --directory /supporto/root-offline sign \
  --csr /trasferimento/richiesta.csr --output /trasferimento/emettitore.json

# Server: importare il pacchetto pubblico, confrontando l'impronta della root.
python -m scripts.pki --directory .runtime import \
  --bundle /trasferimento/emettitore.json --root-fingerprint IMPRONTA_SHA256_ROOT
python -m scripts.bootstrap init --host vpn.example.com
python -m scripts.backup_coordinator init
```

`IMPRONTA_SHA256_ROOT` è l'impronta esadecimale di 64 caratteri verificata offline.
Il pacchetto contiene certificati e CRL, mai la chiave root. Il bootstrap rifiuta
una PKI non preparata e non sovrascrive un server già inizializzato. Solo i test e
il simulatore usano una cerimonia temporanea automatica.

| Percorso | Contenuto |
|---|---|
| `.env` | Segreti applicativi e configurazione |
| `.runtime/issuers` | Chiavi private delle Sub-CA, mai della root |
| `.runtime/pki-public` | Registro pubblico con root, intermedie e CRL root |
| `.runtime/crls` | Bundle CRL verificato e pubblicato dal worker |
| `.runtime/server` | Chiave/certificato server, catena e root pubbliche |
| `.runtime/transport` | TLS-crypt |
| `.runtime/https` | Identità HTTPS separata dalla PKI VPN |
| PostgreSQL | Registro certificati/emettitori, credenziali e pacchetti temporanei cifrati |

File privati `0600`, directory nuove `0700`. Non versionare `.env` o `.runtime`.
I certificati gateway e tecnici durano **3650 giorni**, il server OpenVPN **1825
giorni**. Le durate client sono configurabili con `VPN_GATEWAY_CERTIFICATE_DAYS`
e `VPN_TECHNICIAN_CERTIFICATE_DAYS`, da 1 a 3650 giorni, e non cambiano la durata
delle sessioni. L'emissione viene rifiutata se la catena non copre la durata richiesta
più un giorno: preparare l'emettitore successivo con anticipo.
La root dura 9125 giorni (circa 25 anni), ogni nuova Sub-CA 5475 giorni (circa
15 anni). Una Sub-CA di 15 anni può emettere client decennali soltanto nei primi
cinque anni circa: non attendere la scadenza della Sub-CA per sostituirla.
Anche la root deve coprire tutta la vita della nuova Sub-CA: con queste durate il
termine per firmare una nuova Sub-CA di 15 anni arriva circa dieci anni dopo la
creazione della root. Lo Stato server mostra anche questo termine; pianificare
la transizione della fiducia prima di raggiungerlo, senza aspettare il 25° anno.

In produzione installare un certificato HTTPS attendibile: quello iniziale è
un'identità locale autofirmata, indipendente dalla root VPN. Solo nel laboratorio
si può usare `curl --cacert .runtime/https/cert.pem https://vpn.example.com/health`;
non disattivare la verifica TLS. Conservare i backup descritti nella
[guida di recupero](BACKUP_RESTORE.md).

## Certificati nello Stato server

Il Compose monta nell'API soltanto i PEM pubblici `https/cert.pem`,
`server/server.crt` e `server/ca.crt` della directory runtime, in sola lettura.
Monta inoltre `pki-public/` e `crls/` in sola lettura, senza chiavi di firma.
Le chiavi private restano fuori da questi mount. L'API deve poter leggere i tre
file con l'UID configurato; non allargare i permessi delle chiavi per abilitarlo.
Per avvio nativo impostare `STATUS_HTTPS_CERTIFICATE_FILE`,
`STATUS_VPN_CERTIFICATE_FILE` e `STATUS_VPN_CA_FILE` ai rispettivi PEM pubblici.
Impostare anche `STATUS_PKI_ROOT` a una directory contenente soltanto i sottoalberi
pubblici `pki-public/` e `crls/`.

Il controllo verifica le date del primo certificato nel PEM e avvisa a 30 giorni
dalla scadenza. Non verifica catena di fiducia, corrispondenza del nome o il
certificato effettivamente servito da Nginx/OpenVPN.
La sezione **Autorità e liste di revoca** verifica firme e completezza del bundle
CRL, distingue root/Sub-CA emittente/Sub-CA precedenti e mostra il termine per
emettere client con l'intera durata configurata. Pianifica la nuova Sub-CA almeno
un anno prima di quel termine. La sezione non è un sistema di notifiche esterne.

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
PEM in sola lettura nel container API nel `docker-compose.yml` dell'installazione
e impostare `SMTP_CA_FILE` al percorso **interno** del file. Username e password sono
facoltativi se il relay autorizza l’API tramite la propria configurazione di rete.
Non configurare il relay come open relay.

`localhost` nel container API indica il container stesso. Usare il DNS/IP del
server di posta o un servizio nella rete Docker. Se il relay gira sull’host, usare
un indirizzo raggiungibile dai container; su Linux `host.docker.internal` richiede
un mapping `host-gateway` nello stesso file Compose. Conservare queste modifiche
nel file usato anche dal coordinatore backup: il recupero non carica override esterni.

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
è 12 ore (`SSO_SESSION_MINUTES=720`), ridotta dalle scadenze dei token del provider.
Per OIDC generico la revoca è limitata alla scadenza locale; abilitare
`revocation_check: "userinfo"`
solo dopo aver verificato che UserInfo rifiuti i token revocati. Cognito utilizza
già questo controllo. I valori predefiniti sono un controllo ogni 60 secondi e
un limite di 120 secondi senza verifica riuscita. Collaudare logout, revoca,
guasti del provider e chiusura delle VPN nell'installazione.

Il login al portale dura 12 ore per default (`PORTAL_SESSION_MINUTES=720`).
Entrambi i parametri accettano da 1 a 720 minuti. La VPN dura al massimo 3 ore
per default dalla richiesta, entro la scadenza del login; il logout continua a chiuderla.

## Durate, timeout e reti instabili

Il `.env.example` contiene i parametri commentati in italiano; il bootstrap li
copia anche nel `.env` privato generato, insieme ai segreti. Le unità sono nel nome
(`MINUTES` o `SECONDS`); i due intervalli di polling gateway e quello worker sono
in secondi. I valori predefiniti consentono assistenze di 3 ore e tollerano guasti
API di qualche minuto senza chiudere al primo errore.

| Parametro nel `.env` | Default | Intervallo | Effetto |
| --- | --- | --- | --- |
| `VPN_SESSION_MINUTES` | 180 min | 1–720 | Durata massima dalla richiesta, inclusa l'attesa iniziale. |
| `PORTAL_SESSION_MINUTES` | 720 min | 1–720 | Durata del login browser. Logout e revoche restano vincolanti. |
| `SSO_SESSION_MINUTES` | 720 min | 1–720 | Limite SSO, anche soggetto alla scadenza dei token provider. |
| `VPN_LEASE_SECONDS` | 300 s | 30–900 | Durata massima dell'autorizzazione del firewall dall'ultimo rinnovo. |
| `WORKER_POLL_INTERVAL` | 5 s | 1–10 | Pausa fra i cicli di controllo server. |
| `GATEWAY_POLL_INTERVAL` | 5 s | 1–10 | Pausa fra heartbeat attivi e controlli modem SMS. |
| `GATEWAY_WIFI_CHECK_INTERVAL` | 30 s | 5–300 | Controllo Wi-Fi in attesa, senza VPN attiva. |
| `CONTROL_API_TIMEOUT_SECONDS` | 10 s | 1–60 | Timeout HTTP per fase degli heartbeat e delle richieste worker. |
| `GATEWAY_CONNECT_TIMEOUT_SECONDS` | 30 s | 5–300 | Attesa dell'avvio/riconnessione OpenVPN sul gateway. |
| `OPENVPN_PING_SECONDS` | 10 s | 1–60 | Ping del tunnel in assenza di traffico. |
| `OPENVPN_RESTART_SECONDS` | 300 s | 30–900 | Assenza di pacchetti prima della riconnessione del client. |
| `SSO_CHECK_INTERVAL_SECONDS` | 60 s | 5–300 | Frequenza della verifica presso il provider, se abilitata. |
| `SSO_MAX_STALENESS_SECONDS` | 120 s | 10–600 | Massima età dell'ultima verifica SSO riuscita, se richiesta. |

**Come funziona il lease.** Con API disponibili, gateway e worker rinnovano
normalmente ogni 5 secondi, oltre al tempo necessario al ciclo. Un timeout, un
problema di trasporto, HTTP 408/429 o 5xx conserva le regole esistenti senza
rinnovarle. Per esempio: ultimo rinnovo alle 10:00:00, lease di 300 secondi,
API irraggiungibili dalle 10:00:20: restano al massimo 4 minuti e 40 secondi,
non altri 5 minuti. Un recupero prima delle 10:05 permette il rinnovo senza
interrompere apposta SSH; dopo la scadenza il firewall blocca anche i TCP già
aperti, in entrambi i versi. La successiva autorizzazione può riaprire il traffico
se la sessione resta valida, ma SSH potrebbe avere già terminato la connessione.
Non è possibile aprire una nuova VPN senza autorizzazione API.

**Logout e revoca.** Una risposta esplicita di chiusura o rifiuto blocca subito
il componente che la riceve; il normale polling aggiunge il proprio ritardo.
Se nessuno dei due componenti riesce a ricevere la revoca, il blocco può attendere
il lease residuo: è il compromesso della tolleranza ai guasti. La durata totale
VPN/login non viene prolungata e limita sempre il lease. Con la verifica revoca
SSO attiva, anche `SSO_MAX_STALENESS_SECONDS=120` può limitarlo a meno di 5 minuti;
questa politica del provider è distinta e non viene allargata automaticamente.

**Rete fisica e OpenVPN.** Il lease non trasporta pacchetti quando manca la rete.
`OPENVPN_RESTART_SECONDS` evita riconnessioni troppo precoci del tunnel; deve
essere almeno il doppio di `OPENVPN_PING_SECONDS`. OpenVPN distribuisce questi
valori ai client e usa sul server un'attesa doppia, quindi 600 secondi con il
default 300, come descritto nel [manuale OpenVPN](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual/).
Il firewall continua comunque a rispettare il proprio lease. I 30 secondi di
`GATEWAY_CONNECT_TIMEOUT_SECONDS` riguardano solo l'avvio/riconnessione, non una
VPN già funzionante. SSH, applicazioni e modem possono avere altri timeout:
questa configurazione non garantisce la sopravvivenza dei processi remoti a ogni
guasto. Il traffico Internet del tecnico resta sulla sua connessione abituale.

**Dove applicare i valori.** Preparare il `.env` prima del primo avvio Compose.
I servizi leggono l'ambiente alla partenza: una modifica successiva richiede la
ricreazione dei container interessati (`docker compose up -d --build`); durante
un'assistenza, riavviare OpenVPN o il worker può interrompere il collegamento.
La nuova durata VPN/login vale per le nuove sessioni, non modifica le scadenze
salvate. Il lease viene concesso dall'API e ricevuto da worker e gateway: non va
impostato separatamente sul dispositivo.

Il servizio di provisioning copia `GATEWAY_POLL_INTERVAL`,
`GATEWAY_WIFI_CHECK_INTERVAL`, `CONTROL_API_TIMEOUT_SECONDS` e
`GATEWAY_CONNECT_TIMEOUT_SECONDS` nei nuovi `gateway.env`. Un gateway già
preparato non legge il `.env` del server: aggiornare il suo
`/etc/factoryvpn/gateway.env` e riavviare `factoryvpn-agent.service` fuori da un
intervento. Il keepalive viene invece comunicato da OpenVPN alla connessione.
Mantenere il lease ben maggiore dei timeout HTTP e degli intervalli di polling;
non aumentare il polling per ottenere più tolleranza, perché ritarderebbe anche
l'apertura e la chiusura richieste dal tecnico.

I 30 secondi di freschezza della diagnostica rimangono un criterio di visualizzazione:
uno stato «non verificabile» non significa che la VPN sia stata chiusa.
Anche il token Bearer per le integrazioni API ha una durata distinta, 30 minuti
per default: non è il login browser di 12 ore. Per continuare a chiamare le API
con quel token occorre autenticarsi di nuovo alla sua scadenza; il solo rinnovo
del token non prolunga la sessione VPN. Vedere la
[guida sviluppatori](../website/dist/developers.html#authentication).

## Utenti e gateway

Il percorso manuale seguente resta disponibile per HQ e per i gateway di laboratorio,
senza passaggio in produzione. Per le postazioni di fine linea seguire la
[guida di arruolamento](gateway/FACTORY_ENROLLMENT.md), che descrive chiave dedicata,
allowlist IP, identità persistente e completamento quando il gateway avrà Internet.
Nell'installazione predefinita le API di fine linea sono chiuse; non occorre
abilitarle per usare il provisioning manuale.

1. Accedere al portale con l'amministratore iniziale. Creare gruppi e utenti.
2. Da **Le mie VPN → Aggiungi gateway**, inserire nome, gruppo, modalità SMS/Wi-Fi
   senza configurazione LAN; il MAC è richiesto per Wi-Fi e facoltativo per SMS. Solo amministratori
   globali o titolari di `gateways.provision`
   nei gruppi assegnati possono preparare le credenziali.
   Si possono indicare anche matricola, IMEI e ICCID: vedere
   [anagrafica e ricerca](ADMINISTRATOR_GUIDE.md#gestire-gateway-e-credenziali).
3. Scaricare il pacchetto ZIP dallo stesso accesso che l'ha richiesto, quindi
   attivarlo nel portale. Installare `gateway.env` e `gateway.ovpn` in
   `/etc/factoryvpn/`, con permessi `0600`, e avviare l'agente.
4. Verificare la registrazione nella scheda del gateway. Il server controlla che
   UUID e configurazione coincidano con il provisioning; il gruppo è già assegnato.
5. Ogni tecnico prepara, scarica e attiva il proprio `.ovpn` in **Il mio profilo**.
   Importarlo nel client OpenVPN e avviarlo manualmente dopo aver aperto una VPN
   nel portale. Lo stesso percorso vale per account locali e SSO.

Il servizio `provisioning-worker` emette certificati e pubblica la CRL. L'API monta
solo i certificati e le CRL pubblici, senza le chiavi private delle CA; nessun
riavvio API è necessario per aggiungere un gateway. I download
scadono entro 15 minuti con la configurazione predefinita e sono rimossi all'attivazione; perdendo il file
occorre preparare un nuovo profilo. I pacchetti includono chiavi private: conservarli
in modo protetto. Per integrare il provisioning in altri strumenti consultare il
[contratto OpenAPI](../website/dist/api/openapi.json).

Lo schema iniziale include il registro di arruolamento, IMEI e ICCID e ammette
MAC duplicati; la matricola rimane univoca anche fra prenotazioni di fine linea
e creazioni manuali. Per le nuove installazioni usare lo schema completo della stessa
versione di API e portale. Un laboratorio con lo schema precedente va ricreato
separatamente: non è previsto un aggiornamento incrementale dei dati esistenti.

## SSH sul gateway

Per usare il gateway da terminale, predisporre un server SSH nel sistema operativo
del dispositivo, un account autorizzato e la relativa chiave/password. Il servizio
deve ascoltare sull’IP VPN fisso (o su tutte le interfacce) e il firewall locale
deve consentire TCP 22 dalla VPN. Per il web predisporre HTTP 80 e/o HTTPS 443.
Il firewall FactoryVPN consente solo queste tre porte al tecnico assegnato. Non occorre pubblicare una porta SSH su Internet.

L'agente FactoryVPN non installa né avvia SSH e non gestisce gli account Linux.
Configurare il servizio secondo la distribuzione utilizzata e verificare
l'impronta della chiave host prima del primo accesso. Le credenziali del portale
FactoryVPN sono separate dalle credenziali SSH del gateway.

La VPN del tecnico riceve la rotta `/32` verso l’IP VPN fisso del gateway; nessuna
rotta LAN, Internet predefinita o impostazione DNS viene inviata. Le reti del cliente si possono
ispezionare dalla shell SSH sul gateway, usando le rotte già presenti sul dispositivo.
I passaggi operativi sono nella [guida del tecnico](TECHNICIAN_GUIDE.md#entrare-in-ssh-sul-gateway).

## Agente Linux

Installare il progetto sotto `/opt/factoryvpn`, le dipendenze Python nella
relativa `.venv` e i pacchetti Linux `openvpn iproute2 nftables procps`.
Nel percorso manuale installare il file generato dal provisioning in
`/etc/factoryvpn/gateway.env`. Nel percorso di fine linea predisporre invece
`enrollment.json` e le impostazioni locali come descritto nella
[guida di arruolamento](gateway/FACTORY_ENROLLMENT.md#dati-del-gateway-e-primo-avvio):
il bootstrap installa i file operativi dopo l'approvazione.
`gateway-agent/gateway.env.example` descrive le opzioni: UUID, chiave, modalità,
interfaccia VPN e URL HTTPS del server. La VPN non richiede dati della LAN.
Impostare permessi `0600`. La CA HTTPS locale va installata nel trust store del
sistema oppure indicata con `SSL_CERT_FILE`; in produzione usare TLS attendibile.

Installare `gateway-agent/factoryvpn-agent.service` con systemd e avviare il servizio.
L'unità usa privilegi root per modem, OpenVPN, rotte e firewall. I percorsi nell'unità
vanno adattati se l'installazione usa una directory diversa.
Le interfacce e rotte locali restano gestite dal sistema operativo. Cambiare LAN
non richiede rigenerazione dello ZIP VPN. Il pool `10.100.0.0/16` è riservato
alla VPN e non deve sovrapporsi alle reti locali dei peer. L’agente necessita di connettività HTTPS indipendente dal tunnel
VPN per registrazione, heartbeat e recupero. Mantenere sincronizzati gli orologi.

Impostare `GATEWAY_ACTIVATION_MODE=sms` (default) oppure `wifi`.

`MAC_ADDRESS` può essere vuoto per SMS; resta richiesto per Wi-Fi. Per rilevare
il MAC radio indicare anche `GATEWAY_WIFI_INTERFACE` (per esempio `wlan0`):
l'agente verifica che sia realmente wireless. Non usare l'interfaccia LAN come
sostituto. `GATEWAY_HARDWARE_MODEL` e `GATEWAY_HARDWARE_REVISION` sono descrizioni
facoltative configurate dall'operatore. IMEI, ICCID, modello e firmware del modem vengono
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

- Rinnovo remoto gateway: l'agente controlla localmente la scadenza e avvia il flusso
  HTTPS nell'ultimo anno di validità, anche se la VPN non è più utilizzabile. Genera
  la nuova chiave sul dispositivo, conserva API key/SMS, prepara i file e attiva il
  certificato soltanto senza sessioni né pulizie pendenti. Retry e riavvio riprendono
  il journal `gateway.ovpn.renewal.json`; non eliminare questi file durante il rinnovo.
  Il portale permette **Richiedi rinnovo remoto** anticipato. In SMS inattivo non c'è
  polling remoto: la richiesta anticipata viene rilevata al successivo risveglio;
  la scadenza locale invece avvia la manutenzione HTTPS anche in attesa SMS.
- Sostituzione manuale del pacchetto gateway o del profilo tecnico: preparare e scaricare il sostituto, chiudere le VPN e attendere
  la pulizia, quindi attivarlo dal portale. Il precedente viene revocato.
- Revoca d'emergenza: usare **Revoca credenziali VPN**. Nuove autorizzazioni sono
  bloccate nel database; il worker chiude le connessioni e l'emettitore aggiorna la CRL.
- Spostare un gateway di gruppo revoca le vecchie credenziali: preparare e installare
  un nuovo pacchetto nel gruppo di destinazione. Anche i pacchetti pendenti sono annullati.
- Ogni CRL delle Sub-CA dura sette giorni ed è rigenerata automaticamente quando
  resta meno di un giorno. La CRL root dura 365 giorni e richiede firma offline.
  Il server rifiuta nuove connessioni se una CRL necessaria manca, è alterata o scaduta;
  questo non disconnette istantaneamente ogni tunnel già stabilito.
- Conservare insieme database, Sub-CA, configurazione e `PROVISIONING_SECRET_KEY` nei
  backup protetti. Cambiare quest'ultima senza ricifrare i dati rende indisponibili
  i segreti SMS esistenti. Non rigenerarla a ogni riavvio.
- Rotazione chiave worker: impostare il nuovo valore in `.env` e ricreare API,
  OpenVPN, worker ed emettitore insieme.
- Se si cambia `POSTGRES_PASSWORD` dopo la prima installazione, impostare anche
  la password del ruolo PostgreSQL: l'immagine non la cambia in un volume esistente.

### Rinnovare la CRL root

Programmare la cerimonia almeno 30 giorni prima di `crl_next_update` mostrato nello
Stato server. Anche senza revoche occorre rinnovare la CRL. Sulla macchina offline:

```sh
python -m scripts.pki --directory /supporto/root-offline root-crl
```

Aggiornare le copie offline e trasferire **solo** `root.crl` al server:

```sh
python -m scripts.pki --directory .runtime import-crl --crl /trasferimento/root.crl
```

Il worker ripubblica il bundle completo al ciclo successivo; verificare lo Stato
server. Le CRL vecchie o firmate da root estranee vengono rifiutate. Il breve
intervallo fra importazione e pubblicazione può rifiutare nuove connessioni.
Non cancellare il bundle per azzerare le revoche o i contatori.

### Rinnovare server e Sub-CA

Per la Sub-CA ripetere `prepare`, firma `sign` offline e `import` della cerimonia
iniziale. La precedente passa a `retiring`: conservarne certificato, chiave e CRL
finché tutti i certificati emessi sono scaduti. Il worker mantiene le CRL di tutti
gli emettitori ancora validi. Non sovrascrivere chiavi e certificati a mano.

Il server OpenVPN si rinnova in manutenzione, prima dei suoi cinque anni:
chiudere le VPN dal portale, attendere la pulizia e fermare i servizi indicati.
Il comando genera una nuova chiave e un certificato di 1825 giorni.

```sh
docker compose stop openvpn session-worker provisioning-worker
python -m scripts.bootstrap renew-server --services-stopped
docker compose run --rm --no-deps provisioning-worker python -m scripts.backup_publish_crl
docker compose up -d --force-recreate openvpn session-worker provisioning-worker api
```

`--services-stopped` attesta la manutenzione eseguita dall'operatore: il comando
non ferma Docker. Se si interrompe una sostituzione, tenere i servizi fermi e
rieseguire il rinnovo completo prima di avviarli. Fare una copia server dopo ogni
importazione CA o rinnovo server. Il cambio Sub-CA sotto la stessa root non richiede
di reinstallare i vecchi profili client.

Il ricambio ordinario della root richiede una transizione: importare una Sub-CA
firmata dalla nuova root e, con OpenVPN fermo, eseguire
`python -m scripts.bootstrap refresh-server-trust --services-stopped` per accettare
entrambi i rami **mantenendo il certificato server precedente**. Rinnovare i gateway
via HTTPS e consegnare i profili aggiornati ai tecnici; verificare la distribuzione
prima di rinnovare il server sotto la nuova root. I gateway rimasti offline devono
recuperare via HTTPS. Non eliminare prematuramente il vecchio ramo.

HTTPS va mantenuto attendibile in modo indipendente dalla PKI VPN. Il recupero di
una Sub-CA compromessa e la distribuzione ai client delle revoche server/Sub-CA
non sono inclusi: i profili verificano catena, nome server e date, senza una CRL
statica di sette giorni che renderebbe inutilizzabile un profilo decennale.

## Controllo dei servizi

```bash
docker compose ps
docker compose logs --tail=100 api openvpn session-worker provisioning-worker
docker compose exec openvpn ip -4 address show dev tun0
docker compose exec session-worker nft list table inet factoryvpn_support
docker compose exec api alembic current
```

Gateway Only usa il tunnel condiviso e le autorizzazioni temporanee nel firewall:
non crea namespace di rete per le singole sessioni.

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
   SMS e Wi-Fi, client tecnico e SSH/HTTP/HTTPS all’IP VPN fisso; verificare
   il blocco della LAN diretta e degli altri gateway. Provare dalla shell del gateway
   l’accesso alle reti locali e Internet sulla connessione del tecnico.
5. Verificare chiusura, logout, scadenza e rilascio delle risorse. In una finestra
   di manutenzione, simulare guasti API più brevi e più lunghi del lease configurato:
   un errore temporaneo deve conservare solo il tempo già autorizzato. Senza nuovi
   rinnovi il traffico deve fermarsi alla scadenza, anche su SSH già aperto.
   Ripristinate le API, il traffico può riprendere solo con una nuova conferma per
   una sessione ancora valida; sessioni chiuse, revocate o scadute devono restare
   bloccate. Provare anche l'arresto separato di worker e agente: il loro arresto
   ordinato può chiudere subito la rete, mentre il lease limita i guasti senza pulizia.
6. Verificare copia esterna, chiave separata, prova isolata e recupero secondo la
   [guida backup](BACKUP_RESTORE.md).

Il simulatore e i test automatici non certificano modem, radio, provider SMS,
SMTP o identità dell'installazione reale.
