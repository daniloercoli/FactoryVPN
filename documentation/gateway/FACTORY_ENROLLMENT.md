# FactoryVPN — arruolamento del gateway a fine linea

Protocollo API **v1**. La postazione prepara l'identità del gateway. Il
dispositivo usa quell'identità per scaricare e attivare il pacchetto via HTTPS.
Per la consegna manuale vedere il [primo avvio](INITIAL_SETUP.md).

## Processo

Il flusso coinvolge quattro ruoli. La stessa persona può coprirne più di uno,
e per ciascuna operazione è indicato dove eseguirla:

| Chi | Dove opera | Responsabilità |
|---|---|---|
| Sviluppatori del firmware | Immagine Linux/Yocto del gateway | Includono agente, dipendenze e servizio; predispongono memoria persistente, rete e fiducia HTTPS. |
| Amministratore del server | Terminale del server FactoryVPN | Abilita una volta la postazione di fine linea: chiave e IP di uscita autorizzato. |
| Operatore di fine linea | PC di produzione, detto **postazione** | Per ogni dispositivo verifica la matricola, esegue la CLI e scrive l'identità individuale nella memoria del gateway. |
| HQ, amministratore globale | Portale FactoryVPN | Verifica la pratica in **Arruolamenti**, sceglie il gruppo, approva e completa o corregge i dati. |

Il gateway esegue poi automaticamente il bootstrap previsto dal firmware.
La sequenza complessiva è:

1. La postazione prepara UUID e credenziale casuale individuale, li salva in un
   registro locale protetto e scrive l'identità nella memoria persistente del gateway.
2. Dalla rete fidata registra UUID, matricola e hash della credenziale su FactoryVPN.
   Il gateway è visibile in **Arruolamenti**, anche senza SIM attiva o dati operativi.
3. Un amministratore globale approva l'identità e la assegna a un gruppo.
4. Quando dispone di Internet, il gateway contatta l'URL HTTPS predisposto,
   invia i dati disponibili e attende approvazione e configurazione completa.
5. Richiede il pacchetto, installa i file con un journal persistente e conferma
   quel candidato. Il server attiva le credenziali operative e consuma quella iniziale.
6. L'agente esegue la [registrazione ordinaria](INITIAL_SETUP.md#registrazione-https).
   Il server assegna l'IP VPN se manca; il gateway attende una sessione autorizzata.

Approvazione e primo contatto possono avvenire in qualsiasi ordine, anche a mesi
di distanza. Una volta ottenuto l'accesso a Internet, usare HTTPS sulla connessione
mobile o Wi-Fi del gateway. Per aprire una sessione dopo la registrazione seguire
la [sequenza SMS o Wi-Fi](RUNTIME_PROTOCOL.md).

Per usare gli strumenti forniti, seguire nell'ordine
[abilitazione della postazione](#abilitare-una-postazione-sul-server),
[preparazione di ogni dispositivo](#preparazione-a-fine-linea) e
[configurazione del primo avvio](#dati-del-gateway-e-primo-avvio).
Il [contratto HTTP](#contratto-http-per-firmware-o-postazioni-propri) descrive
le API usate da questi strumenti e serve anche a chi realizza una postazione
o un firmware propri. Le chiamate HTTP non sono operazioni da ripetere a mano
dopo avere eseguito con successo la CLI e il bootstrap.

## Abilitare una postazione sul server

La **postazione** è il PC o servizio che registra i gateway a fine linea.
Per autorizzarla servono entrambi i controlli: Nginx deve consentire il suo IP
di uscita e l'API deve riconoscere la sua chiave. Questa preparazione si esegue
una volta per postazione; non va ripetuta per ogni gateway.

### 1. Individuare il `.env` del server

Eseguire i comandi nel terminale **Linux del server**, con lo stesso account del
bootstrap, dalla directory dell'installazione Docker standard già inizializzata.
Su Windows/WSL usare il terminale Ubuntu in cui gira il server. Gli esempi
seguenti assumono il progetto in `$HOME/factoryvpn` e il runtime in `.runtime`:

```bash
cd "$HOME/factoryvpn"
```

Questi comandi riguardano lo **stack Docker standard**, anche quando gira in
WSL. Il laboratorio avviato con `scripts.gateway_simulator` usa invece un
`docker-compose.yml` generato con valori già incorporati: modificare soltanto
il suo `.env` non aggiorna le chiavi riconosciute dall'API. Non applicare i
comandi Compose sotto dalla radice dei sorgenti per gestire quel laboratorio.

Il `.env` da usare è quello **esistente del server**, accanto a
`docker-compose.yml`, generato durante la [prima installazione](../DOCKER_GUIDE.md#root-offline-e-sub-ca-del-server).
Prima di aggiungere la prima postazione, la parte pertinente ha questo aspetto:

```dotenv
# Estratto: mantenere anche tutte le altre impostazioni e i segreti del server.
FACTORYVPN_RUNTIME_DIR=./.runtime
FACTORY_STATIONS={}
FACTORY_REQUESTS_PER_MINUTE=60
```

`FACTORY_STATIONS={}` significa che nessuna postazione è abilitata; il limite
predefinito è di 60 richieste al minuto per postazione. L'estratto non è un
`.env` completo e non deve sostituire il file dell'installazione. Se ci sono
già postazioni configurate, mantenere il loro contenuto: il comando successivo
aggiunge la nuova voce automaticamente.

`--env-file .env` indica il file da aggiornare. `--root .runtime` indica il
runtime effettivo e deve corrispondere a `FACTORYVPN_RUNTIME_DIR`: se nel proprio
`.env` vale, per esempio, `/srv/factoryvpn/runtime`, usare quel percorso in
**tutti** gli argomenti `--root` e nei percorsi dei file sotto. Il comando non
ricava automaticamente `--root` dal `.env`.

### 2. Generare la chiave della postazione

Scegliere un nome riconoscibile, per esempio `line-1`, ed eseguire sul server:

```bash
.venv/bin/python -m scripts.factory_access --root .runtime --env-file .env \
  add-station --station line-1 --key-file .runtime/factory/line-1.key
```

Il comando genera una chiave casuale, la salva in
`.runtime/factory/line-1.key` con permessi `0600` e aggiorna il `.env` conservando
le altre impostazioni. La riga risultante sarà simile a questa:

```dotenv
FACTORY_STATIONS='{"line-1": "18c07fbdef188d3ff1c506ca17aa4c751ae437c7642eeda2711a008aa5a44a6a"}'
```

L'hash mostrato è **solo dimostrativo**: il comando scrive quello della chiave
appena generata. Lasciare il JSON su una sola riga, con virgolette singole esterne
e doppie per nome e hash. Non occorre calcolare l'hash o compilare questa riga a
mano. `line-1` identifica la postazione; il valore è l'hash SHA-256 della sua
chiave, composto da 64 caratteri esadecimali minuscoli. La chiave in chiaro resta
nel file `.key` e non viene stampata né inserita nel `.env`.

### 3. Autorizzare l'IP di uscita

Eseguire sul server, sostituendo l'indirizzo dimostrativo con quello effettivo:

```bash
.venv/bin/python -m scripts.factory_access --root .runtime --env-file .env \
  allow-ips 192.0.2.10/32
```

Conta l'IP sorgente **visto da Nginx**: nella stessa LAN può essere l'IP del PC;
se la postazione arriva da Internet attraverso un router NAT, normalmente è
l'IP pubblico di uscita della fabbrica. Per un singolo IPv4 usare `/32`, per
un singolo IPv6 `/128`. Non è l'IP del gateway da arruolare né l'indirizzo del
server FactoryVPN.

Gli IP **non si scrivono nel `.env`**. Il comando crea o sostituisce
`.runtime/factory/allow.conf`; nell'esempio il contenuto utile è:

```nginx
allow 192.0.2.10/32;
deny all;
```

La lista è comune a tutte le postazioni, mentre ogni postazione ha la propria
chiave. `allow-ips` **sostituisce l'intera lista**: se ci sono più IP da mantenere,
passarli tutti nella stessa chiamata, per esempio
`allow-ips 192.0.2.10/32 192.0.2.11/32`. Senza indirizzi torna a negare tutti.

Con un proxy a monte, concordare il filtro con chi gestisce l'ingresso: il
controllo fornito usa l'IP del peer TCP e non considera attendibili gli header
`X-Forwarded-For` o `X-Real-IP` inviati dal chiamante. L'allowlist riguarda le
chiamate della postazione a `/api/v1/factory/`; il gateway usa invece la propria
credenziale HTTPS individuale, anche da reti diverse.

### 4. Applicare la configurazione e consegnare la chiave

Sul server, in una finestra senza VPN di prova attive, eseguire un comando alla
volta e proseguire solo se riesce:

```bash
docker compose --env-file .env config --quiet
docker compose --env-file .env up -d --force-recreate --wait api nginx
docker compose --env-file .env ps api nginx
```

La ricreazione passa i nuovi hash all'API e monta la nuova allowlist in Nginx.
Per queste sole modifiche di configurazione non serve ricostruire le immagini.
Se l'installazione usa opzioni Compose aggiuntive, mantenere lo stesso progetto,
file e opzioni usati per avviarla.

Trasferire **il solo file `line-1.key`** alla postazione con un canale protetto,
per esempio nel percorso `/secure/factoryvpn/line-1.key` usato nel capitolo
seguente. Limitare l'accesso al file all'operatore o servizio di fine linea.
La postazione lo userà con `scripts.factory_station --key-file`; non occorre
copiarle il `.env` del server. Non inserire la chiave della postazione nei gateway
o nell'immagine firmware. Dopo il trasferimento verificato, rimuovere la copia
temporanea `.runtime/factory/line-1.key` dal server: le API usano l'hash nel `.env`.

Alla prima registrazione riuscita, il gateway compare in **Arruolamenti**.
Un `403` di Nginx indica di verificare l'IP sorgente e la allowlist; un `401`
dell'API indica di verificare la chiave della postazione, il suo hash e la
ricreazione dell'API. Questi controlli non richiedono di generare un'altra
identità del gateway.

### Rotazione e revoca

Per rotazione usare `add-station` con lo stesso nome, `--rotate` e un **nuovo**
percorso `--key-file`; per revoca usare `revoke-station --station line-1`.
Usare gli stessi `--root` e `--env-file`, poi applicare la configurazione come
al punto 4 e, in caso di rotazione, distribuire la nuova chiave alla postazione.
Una rotazione non modifica le credenziali individuali dei dispositivi già registrati.

## Preparazione a fine linea

Questa procedura si ripete **per ogni gateway**. Nell'esempio la matricola è
`GW-000123`, la postazione è `line-1` e il server è `https://vpn.example.com`.
Sostituire questi valori con quelli dell'installazione.

### 1. Predisporre firmware e postazione

**Sviluppatori del firmware, prima di avviare la produzione:** installare
l'agente e il servizio come nella
[guida al primo avvio su Yocto](INITIAL_SETUP.md#usare-lagente-su-yocto)
o nella [guida Linux](../DOCKER_GUIDE.md#agente-linux). Predisporre una directory
persistente scrivibile `/etc/factoryvpn` che sopravviva agli aggiornamenti.
L'immagine comune non deve contenere identità o segreti di altri dispositivi.
L'avvio del bootstrap va subordinato al completamento del punto 4.

**Amministratore del server, una volta per postazione:** completare la
[procedura di abilitazione](#abilitare-una-postazione-sul-server) e consegnare
il file `line-1.key` al PC di fine linea. Sul server devono essere applicati
sia l'hash della chiave sia la allowlist dell'IP di uscita.

**Responsabile della postazione:** predisporre i sorgenti FactoryVPN e un
ambiente Python con le dipendenze del progetto. I comandi sotto assumono una
postazione Linux, un ambiente `.venv` pronto e l'esecuzione dalla radice dei
sorgenti. L'account deve poter scrivere nella memoria del gateway collegato.

### 2. Collegare il dispositivo corretto e identificare i file

**Operatore di fine linea, per ogni dispositivo:** leggere la matricola fisica
o dal gestionale e collegare il gateway alla postazione. Nell'esempio il suo
filesystem persistente è montato sul PC in `/mnt/gateway`. Verificare che il
mount corrisponda proprio al dispositivo con matricola `GW-000123`.

| Percorso dell'esempio | Dove si trova | A cosa serve |
|---|---|---|
| `/secure/factoryvpn/line-1.key` | PC di fine linea | Chiave della postazione, riutilizzata per i dispositivi di quella linea; non va copiata sul gateway. |
| `.factoryvpn-factory/GW-000123.json` | PC di fine linea | Registro della singola operazione, da conservare per riprendere un tentativo interrotto. |
| `/mnt/gateway/etc/factoryvpn/enrollment.json` | Memoria persistente del gateway, vista dal PC | Identità e segreto iniziale individuale del dispositivo. All'avvio il gateway lo vedrà come `/etc/factoryvpn/enrollment.json`. |

`/mnt/gateway` è soltanto il punto di montaggio sul PC. Non è una directory da
creare nel gateway per ospitare una seconda copia dei file.
Se la linea usa un programmatore o un trasferimento diverso, l'integrazione
deve garantire scrittura e rilettura nella memoria del dispositivo: un export
sul solo disco del PC non prova che il gateway abbia ricevuto l'identità.

### 3. Eseguire la registrazione di fine linea dal PC

**Operatore di fine linea:** eseguire il comando seguente **sul PC di produzione**,
con il dispositivo corretto ancora montato:

```bash
.venv/bin/python -m scripts.factory_station \
  --server-url https://vpn.example.com \
  --serial-number GW-000123 \
  --display-name "Macchina 123" \
  --key-file /secure/factoryvpn/line-1.key \
  --record .factoryvpn-factory/GW-000123.json \
  --identity-file /mnt/gateway/etc/factoryvpn/enrollment.json
```

`--external-reference` è un riferimento gestionale facoltativo, privo di autorità
sulle autorizzazioni VPN. La chiave può anche arrivare dall'ambiente
`FACTORYVPN_FACTORY_KEY`; non passarla in chiaro nella riga di comando.

**La CLI esegue automaticamente queste operazioni, nell'ordine:**

1. Genera UUID del dispositivo, segreto iniziale individuale da 32 byte casuali
   codificati come testo e identificatore idempotente dell'operazione.
2. Salva il registro sul PC **prima** di scrivere sul dispositivo o inviare
   richieste di rete.
3. Scrive `enrollment.json` nella memoria persistente del gateway e lo rilegge
   per verificarne identità, URL e segreto.
4. Chiama `POST /api/v1/factory/gateways` con la chiave della postazione,
   l'UUID, la matricola e l'hash del segreto individuale. Il segreto individuale
   in chiaro resta sul dispositivo e, fino al successo, nel registro locale.
5. Controlla che il server confermi lo stesso UUID, marca il registro come
   confermato e rimuove da quel registro il segreto individuale.

Con gli strumenti forniti non occorre preparare a mano JSON, hash o richieste
HTTP. In questa fase la CLI non genera `gateway.env` o `gateway.ovpn` e non
approva la pratica: queste operazioni appartengono alle fasi successive.

### 4. Verificare la conclusione del passaggio di fine linea

**Operatore di fine linea:** il comando deve terminare con esito positivo e
stampare l'UUID, per esempio:

```text
FactoryVPN gateway registered: 11111111-1111-4111-8111-111111111111
```

L'UUID è generato dalla CLI: quello mostrato è dimostrativo. A questo punto
il server conosce la pratica e HQ può trovarla in **Arruolamenti**. La frase
`gateway registered` stampata dalla CLI indica questa registrazione di fabbrica;
la registrazione operativa e l'assegnazione dell'IP VPN avverranno al punto 6.

In caso di timeout o errore, conservare registro e file identità e ripetere
lo stesso comando, con gli stessi argomenti e percorsi. Non cancellare il
registro o generare un altro UUID per aggirare l'errore. Lasciare il bootstrap
fermo finché la CLI non conclude con successo. Se l'agente era già partito
e si è fermato per un rifiuto di autenticazione, riavviarlo dopo il retry riuscito.

Registro e file identità sono privati (`0600`); il registro rimane sul PC.
Il percorso `.factoryvpn-factory/` è escluso da Git e dalle immagini Docker.

### 5. Verificare e approvare la pratica nel portale

**HQ, con un account amministratore globale:** aprire **Arruolamenti**, cercare
la matricola o l'UUID appena confermati, verificare i dati, scegliere il gruppo
e usare **Approva e assegna**. Non creare un secondo gateway con **Aggiungi gateway**
per completare questa pratica: l'identità da conservare è quella di fine linea.

HQ può approvare anche con dispositivo offline, SIM inattiva e dati operativi
ancora incompleti. Prima di emettere il pacchetto devono però essere noti la
modalità e il numero telefonico per SMS oppure la modalità e il MAC per Wi-Fi.
IMEI e ICCID restano facoltativi. I dati possono arrivare dal gateway tramite
`gateway.local.env` o essere completati da HQ, come nella sezione successiva.

### 6. Avviare il gateway e lasciare completare il bootstrap

**Operatore o installatore del gateway:** predisporre la connessione Internet,
la fiducia HTTPS e gli eventuali dati in
[`gateway.local.env`](#dati-del-gateway-e-primo-avvio). Avviare quindi il servizio
sul gateway. Se è installata l'unità systemd fornita:

```bash
sudo systemctl start factoryvpn-agent.service
```

Questo comando si esegue **nel terminale del gateway**. Con un altro gestore
di servizi usare il comando di avvio previsto dal firmware.

**Agente del gateway, automaticamente:** legge `enrollment.json`, contatta il
server via HTTPS e presenta i dati disponibili. Attende approvazione e dati
completi; poi richiede il pacchetto, lo scarica, verifica e installa
`gateway.env` e `gateway.ovpn` con journal persistente e conferma l'installazione.
Il server attiva le credenziali operative e consuma il segreto iniziale.
L'agente esegue infine `POST /api/v1/gateways/register`: il server assegna
l'IP VPN fisso se manca.

**Servizi del server, automaticamente:** le API e il worker di provisioning
gestiscono preparazione ed emissione dei file dopo le verifiche previste.
L'amministratore non deve eseguire un comando per ogni pacchetto.

Approvazione HQ e primo contatto del gateway possono avvenire in ordine inverso:
il gateway può avviarsi e attendere HQ. Il requisito per avviarlo è il successo
della registrazione della postazione al punto 4.

**Risultato finale da verificare:** pratica completata, registrazione ordinaria
riuscita e IP VPN presente nella scheda del gateway. Il dispositivo è ora pronto
ad attendere una richiesta SMS o Wi-Fi; la VPN si apre soltanto con una sessione
autorizzata, come nel [protocollo operativo](RUNTIME_PROTOCOL.md).

Il file `/etc/factoryvpn/enrollment.json` scritto dalla CLI al punto 3 ha questo
**formato iniziale**, prima che il bootstrap consumi il segreto e lo trasformi
in un marcatore di completamento (valori dimostrativi):

```json
{
  "format": 1,
  "gateway_id": "11111111-1111-4111-8111-111111111111",
  "server_url": "https://vpn.example.com",
  "enrollment_key": "<segreto individuale casuale>"
}
```

La matricola è obbligatoria a fine linea e univoca anche rispetto ai gateway
manuali. L'UUID non cambia al riavvio; MAC, IMEI e ICCID non sono univoci e non
valgono come prova d'identità. HQ può correggere la matricola senza cambiare UUID.

## Dati del gateway e primo avvio

Questa sezione dettaglia la configurazione locale del punto 6. L'operatore o
l'integrazione di produzione può predisporre
`/etc/factoryvpn/gateway.local.env`, protetto (`0600`), con i dati già noti:

```dotenv
GATEWAY_ACTIVATION_MODE=sms
GATEWAY_PHONE_NUMBER=+15550100001
MODEM_PATH=/dev/ttyUSB0
GATEWAY_HARDWARE_MODEL=Modello gateway
GATEWAY_HARDWARE_REVISION=1
```

Il numero è dimostrativo. Modalità e numero possono essere aggiunti quando noti,
prima del completamento, oppure da HQ nel portale. La modalità `sms` richiede il
numero internazionale; `wifi` richiede `MAC_ADDRESS`, e per la telemetria radio
può specificare `GATEWAY_WIFI_INTERFACE`. Il numero telefonico **non viene dedotto
dalla SIM**. L'agente prova la lettura di IMEI/ICCID da un modem disponibile;
l'assenza di questi dati facoltativi non blocca l'arruolamento. Sono accettati
anche `GATEWAY_IMEI` e `GATEWAY_ICCID` locali.

La presentazione legge i dati operativi da `gateway.local.env`, non dal
`gateway.env` di un candidato precedente né dai suoi valori ereditati da systemd.
Invia soltanto valori locali non vuoti nuovi o cambiati: un valore assente/vuoto
non cancella il dato sul server, e il polling non ripristina continuamente valori
già corretti da HQ. Dopo il completamento restano applicabili dal file locale
soltanto le impostazioni hardware elencate sotto.

Il servizio avvia il bootstrap quando esiste `enrollment.json`, anche senza
`gateway.env`. Controlla lo stato circa ogni 60 secondi con variazione casuale;
gli errori temporanei aumentano l'attesa fino a circa 15 minuti. HTTPS richiede
nome host e catena attendibili: non disabilitare la verifica TLS né seguire redirect.
La fiducia HTTPS può essere nel trust store del sistema oppure indicata da
`SSL_CERT_FILE=/percorso/ca-https.pem` in `gateway.local.env`: viene caricata
prima della prima richiesta, senza usare la CA VPN come sostituto automatico.
Un errore definitivo ferma il servizio con stato 78 e richiede intervento HQ.

I percorsi mostrati sono quelli standard. `FACTORYVPN_ENROLLMENT_FILE` può indicare
un altro file identità; file locali, journal e coppia `gateway.env`/`gateway.ovpn`
vengono cercati o scritti nella sua stessa directory.

Il pacchetto è lo stesso ZIP operativo del percorso manuale. Il bootstrap valida
UUID, URL, configurazione/versione e profilo, quindi salva `enrollment-install.json`
prima di installare `gateway.env` e `gateway.ovpn`. Non eliminare il journal
durante un'installazione: consente di recuperare un'interruzione fra le scritture.
UUID, server, credenziali e dati operativi concordati non vengono sovrascritti da
un'unione indiscriminata con le impostazioni locali. Si conservano soltanto
`MODEM_PATH`, `VPN_INTERFACE`, `GATEWAY_WIFI_INTERFACE`, modello/revisione hardware
e `SSL_CERT_FILE`; il percorso del profilo è quello installato dal bootstrap.

Dopo conferma e registrazione riuscite, `enrollment.json` diventa un marcatore
di completamento senza segreto e il journal viene eliminato. Al riavvio si carica
la configurazione operativa: nessun polling aggiuntivo permanente di arruolamento.
Il caricamento locale controlla UUID, identità del certificato e corrispondenza
della chiave privata. HQ può installare un nuovo ZIP per lo stesso UUID anche
con un diverso origin HTTPS valido; prima del completamento l'origin resta
vincolato a quello predisposto in fabbrica. Il controllo locale non blocca un
certificato soltanto perché scaduto: registrazione e rinnovo HTTPS gestiscono
lo stato operativo e il recupero previsto dal [protocollo di rinnovo](CERTIFICATE_RENEWAL.md).
Non riportare il dispositivo allo stato iniziale cancellando il marcatore.

## Contratto HTTP per firmware o postazioni propri

Questa è la **specifica delle API chiamate dagli strumenti descritti sopra**:

| Componente fornito | Dove gira | Chiamate eseguite |
|---|---|---|
| `scripts.factory_station` | PC di fine linea | Crea la pratica con `POST /api/v1/factory/gateways`. |
| Bootstrap dell'agente | Gateway | Consulta e aggiorna l'arruolamento, richiede, scarica e conferma il pacchetto tramite `/api/v1/gateway-enrollment/…`. |
| Agente dopo l'attivazione | Gateway | Esegue la [registrazione ordinaria](INITIAL_SETUP.md#registrazione-https) con le nuove credenziali operative. |

Chi usa CLI e agente forniti segue la procedura operativa; questa sezione serve
a comprenderne le chiamate o a implementare componenti equivalenti.
Per software propri, conservare la stessa ripartizione: la postazione crea
l'identità, HQ approva dal portale, il gateway completa la consegna.

Tutte le chiamate usano l'origin HTTPS configurata, senza query con credenziali.
Per la CLI e il bootstrap usare soltanto schema, host ed eventuale porta,
senza credenziali nell'URL, percorso `/api/v1`, query o frammento.
La chiave della postazione è distinta dal segreto iniziale e da `X-Gateway-Key`,
che serve soltanto dopo l'attivazione del pacchetto.

| Chiamante / header | Metodo e percorso | Contenuto |
|---|---|---|
| Postazione, `X-Factory-Key` | `POST /api/v1/factory/gateways` | `Idempotency-Key` UUID; JSON con `gateway_id`, `serial_number`, `secret_hash` SHA-256 esadecimale minuscolo e dati facoltativi. Risposta 201. |
| Postazione, `X-Factory-Key` | `GET /api/v1/factory/gateways/{id}` | Consulta solo le identità create dalla stessa postazione. |
| Gateway, `X-Enrollment-Key` | `GET /api/v1/gateway-enrollment/{id}` | Stato corrente, dati mancanti, versione e candidato. |
| Gateway, `X-Enrollment-Key` | `PUT /api/v1/gateway-enrollment/{id}` | Aggiornamento parziale di nome, modalità, telefono, MAC, IMEI, ICCID; mai UUID, matricola o gruppo. |
| Gateway, `X-Enrollment-Key` | `POST /api/v1/gateway-enrollment/{id}/package` | JSON `{"configuration_version": 1}` con la versione corrente. Risposta 202 con stato; può essere ancora in preparazione. |
| Gateway, `X-Enrollment-Key` | `POST /api/v1/gateway-enrollment/{id}/package/{job_id}/download` | Nessun body; risposta ZIP, ripetibile sullo stesso candidato valido. |
| Gateway, `X-Enrollment-Key` | `POST /api/v1/gateway-enrollment/{id}/package/{job_id}/confirm` | JSON con `configuration_version`; attiva il candidato già scaricato. |

Campi facoltativi della creazione: `display_name`, `external_reference`,
`activation_mode` (`sms`/`wifi`), `phone_number`, `mac_address`, `imei`, `iccid`.
Telefono in formato internazionale `+` e 10–15 cifre; MAC a sei coppie esadecimali
separate da `:` o `-`; IMEI di 15 cifre; ICCID di 18–22 cifre, conservando gli zeri.
Campi extra sono rifiutati. Inviare solo dati realmente disponibili.

`secret_hash` è SHA-256 dei byte UTF-8 del **testo esatto** che il gateway invierà
in `X-Enrollment-Key`, non dei byte casuali prima della codifica. La CLI genera
quel testo con `secrets.token_urlsafe(32)`; l'API accetta chiavi di 32–256 caratteri.
Un retry della postazione riusa `Idempotency-Key` e body originali; una chiave
idempotente già usata con un body diverso restituisce `409`.

Nel `PUT` un campo omesso rimane invariato; `null` è invece una richiesta di
cancellazione. Per nome/telefono/MAC/IMEI/ICCID anche la stringa vuota viene
normalizzata a `null`. Una configurazione operativa già materializzata deve
continuare a rispettare i requisiti SMS/Wi-Fi: non inviare valori vuoti al posto
di letture hardware fallite. `configuration_version` può cambiare dopo modifiche
operative o assegnazione del gruppo: rileggere lo stato prima di chiedere il pacchetto.

La risposta di stato contiene `id`, eventuale `gateway_id` operativo, `vpn_ip`,
anagrafica, `group_id`, `state`, `delivery_mode`, `configuration_version`,
`revision`, date, `blocked_reason`, `missing_fields`, `can_resume` e `job`.
`job` è nullo oppure contiene `id`, `state`, `expires_at`,
`configuration_version`, `can_download`, `error_code`.
Gli stati sono `prepared`, `awaiting_approval`, `awaiting_data`, `approved`,
`preparing`, `ready`, `completed`, `blocked`, `manual`.
`completed` indica attivazione delle credenziali: non prova un tunnel attivo.

| Stato osservato | Azione del firmware |
|---|---|
| `prepared`, `awaiting_approval` | Presentare i dati disponibili e attendere HQ, senza creare altre identità. |
| `awaiting_data` | Completare i campi in `missing_fields`, poi rileggere lo stato. |
| `approved` | Richiedere `/package` con la versione corrente. |
| `preparing` | Attendere lo stesso job; ripetere `/package` non ne crea un altro mentre è valido. |
| `ready` | Scaricare solo se `job.can_download` è vero e le versioni coincidono; installare e confermare quel job. |
| `manual` | Lasciare la consegna a HQ; verificare un'eventuale coppia manuale già installata con la registrazione ordinaria. |
| `blocked`, `completed` | Sono visibili a HQ/postazione; la credenziale iniziale bloccata o consumata riceve normalmente `401`, anche sul GET di stato. |

Per un installatore proprio, limitare il ZIP e ciascun file a 256 KiB e accettare
solo `gateway.env`, `gateway.ovpn`, `LEGGIMI.txt`, senza estrarre percorsi arbitrari.
Verificare UUID/origin, dati operativi e versione contro lo stato ricevuto;
verificare certificato, chiave e catena del profilo come nel
[contratto PKI](CERTIFICATE_RENEWAL.md#4-attendere-e-verificare-il-bundle-pubblico).
Il file `.env` è configurazione, non uno script da eseguire. Salvare journal e
nuova coppia con `0600`, scritture atomiche e `fsync` prima di `/confirm`.
Non avviare OpenVPN con un candidato non ancora attivato o superato da HQ.

Alla conferma riuscita il server risponde con `activated: true`, `gateway_id`
e `configuration_version`. Attivazione e consumo del segreto iniziale sono
atomici. Se la risposta si perde, provare la registrazione ordinaria con i file
già salvati: un successo conclude il flusso. Solo un `401` operativo consente di
ritentare la conferma iniziale; un `409` operativo richiede verifica HQ.

| Esito | Gestione |
|---|---|
| Timeout, errore di rete, `408`, `5xx` | Conservare identità e journal; ritentare con attesa crescente. |
| `401`/`403` | Credenziale errata, consumata/revocata o accesso non consentito. Non generare un'identità nuova. |
| `404` | Identità o job assente/non accessibile; verificare UUID e candidato con HQ, senza ricreare l'identità. |
| `409` | Conflitto di identità/idempotenza oppure candidato/configurazione/stato superati. Rileggere lo stato; non attivare una versione diversa. |
| `422` | Correggere dati o formato della richiesta. |
| `429` | Rispettare `Retry-After`; evitare cicli serrati. |

Un solo candidato pendente per gateway; la finestra è
`PROVISIONING_DOWNLOAD_MINUTES` (default 15 minuti dalla richiesta).
Il job nasce quando il dispositivo è pronto, non mentre è offline in magazzino.
Alla scadenza si può richiedere un nuovo candidato dopo rivalidazione.
Se `/confirm` restituisce `409`, i file del candidato non autorizzano la VPN:
rileggere lo stato e riprendere la consegna della versione valida. Conservare
sempre la stessa identità; il journal del candidato superato non deve continuare
a reinstallarlo. Per una conferma dalla risposta persa usare prima la verifica
operativa descritta sopra, senza presumere che sia fallita.
Limiti predefiniti: 60 richieste/minuto per postazione, 30 per gateway,
6 nuovi pacchetti/ora per gateway e almeno 60 secondi tra nuovi candidati.

## Intervento HQ e recupero

In **Arruolamenti** HQ può approvare, correggere dati, bloccare o passare alla
consegna manuale sulla stessa identità. Il passaggio annulla il candidato automatico.
Scaricare e attivare lo ZIP dal medesimo accesso al portale, fermare l'agente,
copiare entrambi i file e riavviarlo. L'attivazione manuale consuma il segreto
iniziale. Prima dell'attivazione HQ può riprendere esplicitamente l'automatismo,
se la credenziale iniziale non è stata revocata.

Per dispositivi di test senza fine linea, usare direttamente **Aggiungi gateway**:
non occorrono chiavi della postazione, registro di fabbrica o `enrollment.json`.
Le correzioni operative dopo l'attivazione richiedono un nuovo pacchetto manuale;
IMEI/ICCID e LAN non richiedono un nuovo certificato. Per dettagli sulle revoche
e sui cambi di configurazione vedere la [guida amministratore](../ADMINISTRATOR_GUIDE.md#gestire-gateway-e-credenziali).

Un reset che perde le credenziali richiede recupero HQ. Un restore del server
invalida i segreti iniziali recuperati e blocca gli arruolamenti incompleti:
non riattivarli copiando vecchi segreti. Le chiavi delle postazioni e la allowlist
restano configurazione dell'installazione corrente; vedere
[backup e ripristino](../BACKUP_RESTORE.md).

Dopo il completamento, seguire la [sequenza di apertura](RUNTIME_PROTOCOL.md)
e il [rinnovo dei certificati](CERTIFICATE_RENEWAL.md).
