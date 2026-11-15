# FactoryVPN — arruolamento del gateway a fine linea

Protocollo **v1**, modello di rete **Gateway Only**. Questa guida descrive il
flusso disponibile per nuove installazioni. Il riepilogo dei due percorsi,
compreso quello manuale HQ, è nella [prima inizializzazione](INITIAL_SETUP.md).

## Processo

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
di distanza. La prima connessione e il download non richiedono un tunnel VPN:
sono richieste HTTPS in uscita sulla connessione mobile/Wi-Fi del dispositivo.
L'IP della SIM e la LAN del cliente non vanno comunicati al server.

La VPN permette TCP 22/80/443 sul solo IP VPN del gateway. SSH e servizi web,
account del sistema operativo, APN, DNS, orologio e connettività sono predisposti
nel firmware/sistema operativo. L'arruolamento non li installa né li collauda.

## Abilitare una postazione sul server

Il bootstrap del server crea `factory/allow.conf` nel runtime con `deny all;`.
La configurazione `FACTORY_STATIONS={}` non accetta alcuna postazione. Servono
**sia IP fidato sia chiave API**. Con l'ambiente Python dell'installazione:

```bash
.venv/bin/python -m scripts.factory_access --root .runtime --env-file .env \
  add-station --station line-1 --key-file .runtime/factory/line-1.key
.venv/bin/python -m scripts.factory_access --root .runtime --env-file .env \
  allow-ips 192.0.2.10/32 2001:db8:1234::/64
docker compose up -d --force-recreate api nginx
```

Gli indirizzi sono esempi: usare gli IP di uscita effettivi delle postazioni.
`allow-ips` sostituisce l'intera lista; senza indirizzi torna a negare tutti.
Adeguare `--root` a `FACTORYVPN_RUNTIME_DIR`. Trasferire il file `.key` alla
postazione con un canale protetto e limitarne l'accesso all'operatore/servizio.
Il comando non stampa la chiave; `.env` conserva soltanto il suo hash SHA-256.
Non inserire questa chiave nei gateway o nell'immagine firmware.
Il file `.key` sul server serve alla consegna iniziale: dopo il trasferimento
verificato alla postazione, rimuovere quella copia temporanea. Le API non la usano.

Per rotazione usare `add-station` con lo stesso nome, `--rotate` e un **nuovo**
percorso `--key-file`; per revoca usare `revoke-station --station line-1`.
Ricreare `api` dopo la modifica e distribuire la nuova chiave alla postazione.
Una rotazione non modifica le credenziali individuali dei dispositivi già registrati.

Nginx usa l'indirizzo del peer TCP, non `X-Forwarded-For` o `X-Real-IP` inviati dal
chiamante. Il Compose non espone la porta API. Non pubblicarla separatamente.
Con un reverse proxy ulteriore, verificare la topologia e configurare esplicitamente
la fiducia nel proxy oppure applicare il filtro al bordo corretto: consentire
semplicemente l'IP di un proxy aperto annullerebbe la restrizione per postazione.
Le API del gateway non hanno questa allowlist, perché l'IP del cliente può cambiare.

## Preparazione a fine linea

Installare l'agente e il servizio Linux come nella
[guida di installazione](../DOCKER_GUIDE.md#agente-linux). L'identità individuale
deve stare fuori dall'immagine comune e sopravvivere agli aggiornamenti firmware.
Il comando fornito può scriverla direttamente su un filesystem del dispositivo
montato dalla postazione:

```bash
.venv/bin/python -m scripts.factory_station \
  --server-url https://vpn.example.com \
  --serial-number GW-000123 \
  --display-name "Macchina 123" \
  --key-file /secure/factoryvpn/line-1.key \
  --record .factoryvpn-factory/GW-000123.json \
  --identity-file /mnt/gateway/etc/factoryvpn/enrollment.json
```

`--identity-file` deve puntare alla memoria persistente del dispositivo corretto.
Un adattatore per una linea diversa deve garantire scrittura e rilettura sul
dispositivo prima di considerare terminata l'operazione; un semplice export su
disco della postazione non prova la consegna al gateway.
`--external-reference` è un riferimento gestionale facoltativo, privo di autorità
sulle autorizzazioni VPN. La chiave può anche arrivare dall'ambiente
`FACTORYVPN_FACTORY_KEY`; non passarla in chiaro nella riga di comando.

Il comando genera UUID, segreto casuale di 32 byte e identificatore idempotente.
Salva il registro **prima** della scrittura e della richiesta di rete. Un retry
deve riusare gli stessi argomenti e lo stesso registro. Non cancellarlo per
aggirare un errore: si creerebbe una seconda identità. Dopo conferma del server
e verifica locale, il registro non conserva più il segreto individuale; questo
rimane soltanto sul dispositivo. Registro e file identità sono privati (`0600`).
Il percorso `.factoryvpn-factory/` è escluso da Git e dalle immagini Docker.
Avviare il servizio sul gateway soltanto dopo il successo della registrazione
della postazione: prima di quel momento l'identità non è ancora riconosciuta
dal server. Se la linea ha già avviato l'agente e questo si è fermato per un
rifiuto di autenticazione, riavviarlo dopo avere completato il retry di fine linea.

Formato di `/etc/factoryvpn/enrollment.json` (valori dimostrativi):

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

Preparare facoltativamente `/etc/factoryvpn/gateway.local.env`, protetto (`0600`):

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

La risposta di stato contiene `id`, eventuale `gateway_id` operativo, `vpn_ip`,
anagrafica, `group_id`, `state`, `delivery_mode`, `configuration_version`,
`revision`, date, `blocked_reason`, `missing_fields`, `can_resume` e `job`.
`job` è nullo oppure contiene `id`, `state`, `expires_at`,
`configuration_version`, `can_download`, `error_code`.
Gli stati sono `prepared`, `awaiting_approval`, `awaiting_data`, `approved`,
`preparing`, `ready`, `completed`, `blocked`, `manual`.
`completed` indica attivazione delle credenziali: non prova un tunnel attivo.

Alla conferma riuscita il server risponde con `activated: true`, `gateway_id`
e `configuration_version`. Attivazione e consumo del segreto iniziale sono
atomici. Se la risposta si perde, provare la registrazione ordinaria con i file
già salvati: un successo conclude il flusso. Solo un `401` operativo consente di
ritentare la conferma iniziale; un `409` operativo richiede verifica HQ.

| Esito | Gestione |
|---|---|
| Timeout, errore di rete, `5xx` | Conservare identità e journal; ritentare con attesa crescente. |
| `401`/`403` | Credenziale errata, consumata/revocata o accesso non consentito. Non generare un'identità nuova. |
| `409` | Conflitto di identità/idempotenza oppure candidato/configurazione/stato superati. Rileggere lo stato; non attivare una versione diversa. |
| `422` | Correggere dati o formato della richiesta. |
| `429` | Rispettare `Retry-After`; evitare cicli serrati. |

Un solo candidato pendente per gateway; la finestra è
`PROVISIONING_DOWNLOAD_MINUTES` (default 15 minuti dalla richiesta).
Il job nasce quando il dispositivo è pronto, non mentre è offline in magazzino.
Alla scadenza si può richiedere un nuovo candidato dopo rivalidazione.
Limiti predefiniti: 60 richieste/minuto per postazione, 30 per gateway,
6 nuovi pacchetti/ora per gateway e almeno 60 secondi tra nuovi candidati.

## Intervento HQ, recupero ed evoluzioni

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

L'autoarruolamento senza registrazione a fine linea resta un'evoluzione futura.
Questa prima implementazione usa il percorso semplice di identità predisposta,
approvazione amministrativa e consegna individuale; non prevede codici di
abbinamento, attestazione hardware o creazione autonoma di identità sconosciute.
