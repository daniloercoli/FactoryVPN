# FactoryVPN — prima inizializzazione del gateway

Versione del protocollo API: **v1**.
Questa guida è destinata agli sviluppatori del firmware o dell'agente gateway.
Riassume i due percorsi di inizializzazione e la conferma iniziale via HTTPS. Per rinnovare
successivamente il certificato senza accesso locale, implementare anche il
[protocollo di rinnovo](CERTIFICATE_RENEWAL.md).

## Due percorsi, la stessa identità operativa

| Percorso | Preparazione | Completamento |
|---|---|---|
| Fine linea | La postazione registra il gateway e scrive UUID, URL e credenziale iniziale individuale sul dispositivo. | L'amministratore approva e assegna un gruppo; quando ha Internet, il gateway completa i dati, scarica e installa il pacchetto. |
| Manuale HQ / laboratorio | L'operatore crea il gateway dal portale, prepara, scarica e attiva lo ZIP. | Copia `gateway.env` e `gateway.ovpn` sul dispositivo e avvia l'agente. Non occorre una postazione di produzione. |

Nel percorso di fine linea il dispositivo può restare offline, anche con SIM
inattiva, durante magazzino e trasporto. L'approvazione può precedere il primo
contatto. IMEI, ICCID e telefono possono essere completati successivamente;
la modalità SMS richiede il numero prima di preparare il pacchetto operativo.
Il download usa **HTTPS sulla normale connessione Internet, prima della VPN**.
Non servono un IP pubblico del gateway o un codice di abbinamento.

UUID e matricola restano i riferimenti dell'identità. MAC, modem, SIM, LAN e IP
Internet possono cambiare senza creare un altro gateway. La matricola è univoca;
MAC, IMEI e ICCID sono dati ricercabili, non credenziali. L'IP VPN fisso viene
assegnato dal server alla prima registrazione operativa riuscita.

La procedura completa, i file di fabbrica e le API dedicate sono nel
[contratto di arruolamento a fine linea](FACTORY_ENROLLMENT.md).
Questa è la prima modalità automatizzata implementata, scelta per contenere la
complessità. **L'autoarruolamento senza passaggio a fine linea è un'evoluzione
futura**: oggi il gateway deve avere un'identità preparata dalla postazione oppure
un pacchetto manuale HQ.

## Consegna manuale sempre disponibile

Copiare `gateway.ovpn` e `gateway.env` dal pacchetto ZIP preparato, scaricato e
attivato nel portale FactoryVPN.
La [procedura per l'operatore](../DOCKER_GUIDE.md#utenti-e-gateway) descrive
creazione del gateway, download e attivazione. Lo ZIP contiene anche `LEGGIMI.txt`.

| File | Contenuto | Percorso standard |
|---|---|---|
| `gateway.ovpn` | Profilo OpenVPN, certificato client, chiave privata e materiale di fiducia VPN | `/etc/factoryvpn/gateway.ovpn` |
| `gateway.env` | UUID, credenziali HTTPS/SMS, URL server, modalità e interfaccia VPN | `/etc/factoryvpn/gateway.env` |

I due file devono appartenere allo stesso pacchetto. Conservarli con permessi
`0600`, accessibili soltanto all'account che esegue l'agente. Sono individuali:
non distribuirli come parte di un'immagine firmware comune a più dispositivi.
Non inserire file o credenziali nei log.

Il firmware deve caricare le variabili di `gateway.env`; nell'agente Linux di
riferimento lo fa [l'unità systemd](../../gateway-agent/factoryvpn-agent.service)
tramite `EnvironmentFile`, oppure il bootstrap dopo l'installazione automatica.
Copiare il file senza caricarne i valori non configura
automaticamente un processo. `OPENVPN_CONFIG` indica il percorso del profilo VPN.
Per installare l'agente di riferimento seguire la [guida Linux](../DOCKER_GUIDE.md#agente-linux).

Le API di rinnovo non creano il gateway e non consegnano queste credenziali
iniziali. Dopo la copia manuale o l'installazione automatica, l'agente deve confermare la propria
registrazione via HTTPS come descritto sotto.

## Prerequisiti del dispositivo

- Configurare nel sistema operativo accesso Internet, DNS e sincronizzazione
  dell’orologio. Configurare la LAN solo se usata dall'applicazione: è indipendente
  dalla VPN e non viene registrata nel portale.
  Riservare `10.100.0.0/16` alla VPN, senza sovrapposizioni con le reti locali.
- Rendere `SERVER_URL` raggiungibile via HTTPS anche senza tunnel VPN.
  Verificare nome host e catena TLS; la fiducia HTTPS è distinta da quella VPN.
- Per la registrazione operativa caricare UUID e credenziali dal pacchetto attivato;
  il primo contatto di fine linea usa invece il file identità e il protocollo dedicato.
  Non generare una nuova
  identità al riavvio e non usare IMEI o MAC come sostituti dell'UUID.
- Configurare modem e accesso dati per la modalità SMS, oppure accesso di rete
  per la modalità Wi-Fi. L'agente non configura APN, PPP/WWAN o la rete wireless.

## Registrazione HTTPS

All'avvio inviare un POST a `{SERVER_URL}/api/v1/gateways/register`.
`SERVER_URL` è l'origin HTTPS, senza il suffisso `/api/v1`.

```http
POST /api/v1/gateways/register
Content-Type: application/json
X-Gateway-Key: <valore di GATEWAY_API_KEY>
```

Non usare cookie del portale o JWT di un tecnico. Non seguire redirect verso
origini diverse e non disabilitare la verifica HTTPS.

| Variabile in `gateway.env` | Campo JSON |
|---|---|
| `GATEWAY_ID` | `gateway_id` |
| `GATEWAY_ACTIVATION_MODE` | `activation_mode` |
| `GATEWAY_PHONE_NUMBER` | `phone_number` (vuoto → `null`) |
| `MAC_ADDRESS` | `mac_address` (vuoto → `null`) |

Esempio con dati dimostrativi; sostituire tutti i valori con quelli del pacchetto:

```json
{
  "gateway_id": "11111111-1111-4111-8111-111111111111",
  "activation_mode": "sms",
  "phone_number": "+15550100001",
  "mac_address": null
}
```

`activation_mode` è `sms` oppure `wifi`. SMS richiede il numero internazionale
con prefisso `+`; Wi-Fi richiede il MAC. I campi facoltativi vanno comunque
trasmessi con il valore provisionato, oppure `null` se assenti. Non inviare
`lan_cidr`, `lan_address` o `lan_interface`: Gateway Only non li usa.

Non inviare `GATEWAY_SMS_KEY`, certificati o chiavi private nel body. Non inviare
`group_id`, `display_name`, `serial_number`, `imei` o `iccid`: gruppo e anagrafica
sono gestiti separatamente dal contratto di registrazione. I campi extra
sono rifiutati. Lo [schema della richiesta](../../api/schemas/gateway.py) definisce
la validazione completa.

### Risposta e ripetizione della richiesta

Una risposta `200` contiene `id`, `display_name`, `group_id`, `activation_mode`,
`phone_number`, `mac_address`, `serial_number`, `imei`, `iccid`, `status`, `vpn_ip`
e `last_seen`.
Verificare che `id` coincida con `GATEWAY_ID` e che `activation_mode` coincida
con la modalità configurata prima di proseguire.

La chiamata conferma una configurazione già assegnata: non crea un'identità né
modifica la LAN. Il server assegna un IP VPN se manca e conserva quello già
associato al gateway, indipendentemente dall'indirizzo della connessione Internet
mobile o Wi-Fi. Riceverlo non avvia il client OpenVPN. Lo stato `ACTIVE` nella risposta conferma la registrazione e
**non indica che il tunnel sia attivo**.

La stessa richiesta è ripetibile, anche dopo un riavvio o una risposta persa,
senza `Idempotency-Key`. Per timeout, errori di rete o `5xx`, conservare i dati
installati e ritentare con attesa crescente. L'agente di riferimento usa un
timeout HTTP di 30 secondi per la registrazione.

| Risposta | Significato e gestione |
|---|---|
| `401` | Credenziale assente, errata, non attivata o revocata. Verificare pacchetto e attivazione con l'operatore. |
| `403` | UUID diverso da quello della credenziale. Non mescolare file di dispositivi diversi. |
| `404` | Gateway non presente. Richiedere la verifica del provisioning. |
| `409` | Configurazione diversa da quella assegnata, oppure conflitto di identità/numero. Verificare i valori con l'operatore. |
| `422` | Body non valido o campi extra. Correggere la richiesta secondo lo schema. |

Il controllo della configurazione vale anche senza sessioni VPN attive.
Non aggirare un rifiuto generando UUID o credenziali nuovi sul dispositivo.

## Dopo la registrazione

La registrazione non apre né collauda il tunnel. Il gateway attende la richiesta
di una sessione autorizzata: SMS firmato in modalità SMS, controllo HTTPS in
modalità Wi-Fi. Durante la sessione l'agente gestisce OpenVPN, heartbeat e regole
di rete. I comportamenti operativi sono nella [guida dell'agente](../DOCKER_GUIDE.md#agente-linux);
l'implementazione è in [main.py](../../gateway-agent/main.py).

Completare una sessione di prova dal portale e verificare la raggiungibilità
di SSH e web sull’IP VPN fisso (TCP 22/80/443), con servizi predisposti nel firmware.
La LAN è accessibile solo operando dalla shell del gateway. Il cambio di LAN non
richiede un nuovo pacchetto VPN. Per i rinnovi successivi usare esclusivamente il
[contratto dei certificati](CERTIFICATE_RENEWAL.md): il rinnovo mantiene UUID,
API key e segreto SMS e aggiorna da remoto il profilo OpenVPN.

## Implementazione di riferimento e simulatore

La richiesta iniziale è implementata in [registration.py](../../gateway-agent/registration.py)
e verificata da [test_gateway_registration.py](../../tests/test_gateway_registration.py).
L'agente di riferimento può essere usato direttamente su Linux oppure come
riferimento per un firmware diverso.

Il simulatore FactoryVPN esegue lo stesso agente con API, certificati, OpenVPN e
traffico HTTP/HTTPS al gateway reali. Il codice di preparazione in [local_lab.py](../../scripts/local_lab.py)
automatizza download, estrazione dei due file e attivazione per il laboratorio.
Il simulatore usa il percorso di provisioning manuale e non richiede API
amministrative nel firmware. Il bootstrap di fine linea è implementato in
[enrollment.py](../../gateway-agent/enrollment.py); entrambi convergono sulla
stessa registrazione operativa.

I comandi disponibili si consultano dalla radice del repository con
`.venv/bin/python -m scripts.gateway_simulator --help`; requisiti e contesto sono
nel [README](../../README.md#prima-installazione). Modem e reti radio sono
simulati: collaudare separatamente hardware, modem/provider, riavvii, indisponibilità
HTTPS e persistenza della configurazione sul dispositivo di destinazione.
