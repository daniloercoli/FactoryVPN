# FactoryVPN — apertura VPN dal gateway

Protocollo API **v1**. Prima di attendere una sessione, caricare il pacchetto del
dispositivo e completare `POST /api/v1/gateways/register` come descritto nella
[prima inizializzazione](INITIAL_SETUP.md#registrazione-https). Il tecnico
richiede l'apertura dal portale. Il gateway risponde con la sequenza seguente.

## Modalità SMS

1. Leggere il modem ogni `GATEWAY_POLL_INTERVAL` (default 5 s, da 1 a 10 s).
   In attesa di un [SMS valido](#verificare-lsms), il gateway non invia heartbeat.
2. Inviare l'heartbeat **1**, con `vpn_ip` e `ready_session_id` a `null` e il
   testo dell'SMS in `activation_token`. Procedere solo con `connect: true` e
   `network.session_id` uguale all'UUID firmato nell'SMS.
3. Avviare OpenVPN e inviare l'heartbeat **2** con l'IPv4 del tunnel. Se l'API
   conferma la stessa sessione e coppia di IP, applicare il lease locale.
4. Inviare l'heartbeat **3** con IPv4 e UUID della sessione pronta. Dopo la
   conferma, eliminare l'SMS dal modem e conservare il token in memoria.
5. Durante la sessione continuare gli heartbeat con IPv4, UUID e lo stesso
   token. Se HTTPS cade prima della conferma, conservare l'SMS per ritentare
   finché è valido. Se il gateway si riavvia dopo averlo eliminato, per una
   sessione ancora valida chiedere al tecnico di reinviare l'SMS dal portale.

## Modalità Wi-Fi

1. In attesa, inviare l'heartbeat **1** ogni `GATEWAY_WIFI_CHECK_INTERVAL`
   (default 30 s, da 5 a 300 s). Impostare tutti e tre i campi a `null`.
   Con `connect: false`, attendere il prossimo controllo.
2. Con `connect: true`, avviare OpenVPN e inviare l'heartbeat **2** con l'IPv4
   del tunnel. Se l'API conferma la stessa sessione e coppia di IP, applicare
   il lease locale.
3. Inviare l'heartbeat **3** con IPv4 e UUID della sessione pronta.
4. Durante la sessione continuare gli heartbeat con IPv4 e UUID.
   `activation_token` resta sempre `null`. Dopo un riavvio, il polling
   può ritrovare una sessione ancora valida.

## Chiamata heartbeat

Usare l'origin HTTPS `SERVER_URL` del pacchetto (esempio
`https://vpn.example.com`). Verificare nome host e catena TLS, usare un orologio
sincronizzato ed evitare redirect. HTTPS deve funzionare prima del tunnel.

```http
POST /api/v1/gateways/heartbeat
Content-Type: application/json
X-Gateway-Key: <GATEWAY_API_KEY>
```

I tre POST hanno lo stesso formato. I valori qui sotto sono dimostrativi.

| Chiamata | `vpn_ip` | `ready_session_id` | `activation_token` |
|---|---|---|---|
| **1. Chiedere l'autorizzazione** | `null` | `null` | SMS ricevuto oppure `null` per Wi-Fi |
| **2. Confermare l'IP del tunnel** | `"10.100.0.2"` | `null` | Come nella chiamata 1 |
| **3. Confermare la prontezza** | `"10.100.0.2"` | `"11111111-1111-4111-8111-111111111111"` | Come nella chiamata 1 |

Esempio di richiesta **1** per Wi-Fi:

```json
{"vpn_ip": null, "ready_session_id": null, "activation_token": null}
```

L'identità deriva dall'header. I POST sono ripetibili senza `Idempotency-Key`.
`vpn_ip` è l'IPv4 realmente assegnato al tunnel. `activation_token` accetta al
massimo 160 caratteri.

Risposta **HTTP 200** in assenza di autorizzazione:

```json
{"connect": false, "network": null, "lease_seconds": 0}
```

Risposta **HTTP 200** con autorizzazione:

```json
{
  "connect": true,
  "network": {
    "session_id": "11111111-1111-4111-8111-111111111111",
    "technician_ip": "10.100.128.2",
    "gateway_ip": "10.100.0.2"
  },
  "lease_seconds": 300,
  "expires_at": "2030-01-01T00:00:00+00:00"
}
```

Verificare tipi, UUID e indirizzi di `network` prima di usare la risposta.

## Avviare, confermare e mantenere il tunnel

1. Dopo la chiamata **1**, tenere bloccato il traffico e avviare
   `openvpn --config "$OPENVPN_CONFIG" --dev "$VPN_INTERFACE"`. Attendere
   `Initialization Sequence Completed` e leggere l'IPv4 assegnato con
   `ip -j addr show dev "$VPN_INTERFACE"`. Deve coincidere con `network.gateway_ip`.
2. Con la chiamata **2**, inviare l'IPv4 assegnato e verificare `connect`, UUID
   e coppia di indirizzi. Accettare un lease intero tra 1 e 900 secondi.
   Detrarre il tempo trascorso dall'inizio della richiesta con un orologio
   monotono e arrotondare per difetto. Se il residuo non è positivo, lasciare
   bloccato il traffico. Usare `lease_seconds` per il firewall e `expires_at` per la
   scadenza complessiva indicata dall'API.
3. Consentire sull'interfaccia VPN solo traffico IPv4 tra `technician_ip` e
   `gateway_ip` in entrambe le direzioni. Applicare il lease a ogni pacchetto,
   anche su connessioni esistenti, con scadenza nel kernel. Bloccare il resto
   del traffico del tunnel e l'inoltro da e verso la LAN. Conservare la rotta
   al peer fornita da OpenVPN. Il pool VPN è `10.100.0.0/16`, con il gateway
   in `10.100.0.0/17` (esclusi `10.100.0.0` e
   `10.100.0.1`), tecnico in `10.100.128.0/17`.
4. Dopo aver applicato le regole locali inviare la chiamata **3**. Proseguire
   soltanto con `connect: true` e la stessa sessione e coppia. Questa chiamata
   segnala che il gateway è pronto; il tecnico si collega con il proprio client.
5. Ripetere il controllo ogni `GATEWAY_POLL_INTERVAL` durante la sessione.
   L'agente di riferimento invia un heartbeat prima di verificare il tunnel
   e un secondo dopo la verifica, da cui calcola il lease da rinnovare. Inviare
   `ready_session_id` quando la rete locale è configurata e l'IP è presente.
   Se l'API indica una nuova sessione, chiudere la precedente prima di
   preparare quella nuova. Un keepalive OpenVPN non rinnova il lease.

## Verificare l'SMS

Formato del messaggio:

```text
FACTORYVPN: activate <session_id> <expires_unix> <signature>
```

- Verificare prefisso e verbo, leggere l'UUID e la scadenza Unix in secondi.
  Scartare messaggi alterati o con scadenza già raggiunta.
- Calcolare HMAC-SHA256 con la chiave `GATEWAY_SMS_KEY` in UTF-8 e il testo
  `<UUID canonico minuscolo> <scadenza intera>` in UTF-8, con un solo spazio.
  Confrontare i primi 32 caratteri esadecimali minuscoli con la firma ricevuta,
  a tempo costante. Il numero del mittente non sostituisce il controllo HMAC.
- Inviare l'intero messaggio verificato in `activation_token`. Un duplicato
  può essere scartato quando la stessa attivazione è già in gestione.
  L'SMS serve a chiedere l'autorizzazione via HTTPS.

Per provare il calcolo offline, con chiave dimostrativa `demo-sms-key` e testo
`11111111-1111-4111-8111-111111111111 1893456000`, la firma attesa è
`e52c4e871049904383132effa1754c30`. Vedi il
[verificatore Python](../../factoryvpn_common/protocol.py).

## Gestire rifiuti e guasti

| Esito | Azione sul gateway |
|---|---|
| `connect: false` | Ritirare subito le regole, fermare il proprio client OpenVPN e tornare in attesa. In SMS, scartare l'attivazione. |
| Timeout, errore di trasporto, `408`, `429`, `5xx` | Ritentare. Un tunnel già in esecuzione può usare soltanto il lease già installato, fino alla scadenza nel kernel. Mai avviare o rinnovare offline. |
| `401` o `404` | Bloccare e fermare il tunnel. Verificare credenziali e registrazione con l'operatore. |
| `403` | Bloccare, fermare e scartare l'attivazione SMS rifiutata. |
| `409` | Bloccare e fermare il tunnel. Verificare che l'IPv4 del tunnel sia quello assegnato. |
| `422`, risposta incoerente, errore locale | Bloccare e fermare il tunnel. Controllare i payload, il profilo o il firewall secondo l'errore. |

Un guasto HTTPS non proroga il lease. Alla scadenza il kernel blocca il traffico
anche se OpenVPN è ancora in esecuzione. All'arresto dell'agente, rimuovere
prima l'accesso e poi fermare il client posseduto dall'agente.

## Esempio Python per Yocto

Questa funzione esegue un POST. Caricare prima le variabili dal pacchetto e
usare i valori della tabella per le tre fasi. Per SMS passare il testo già
verificato dal modem. Per validazione, gestione del tunnel e firewall usare
la sequenza sopra o l'[agente di riferimento](../../gateway-agent/main.py).

```python
import os

import httpx


def heartbeat(vpn_ip=None, ready_session_id=None, activation_token=None):
    url = os.environ["SERVER_URL"].rstrip("/") + "/api/v1/gateways/heartbeat"
    body = {
        "vpn_ip": vpn_ip,
        "ready_session_id": ready_session_id,
        "activation_token": activation_token,
    }
    timeout = float(os.getenv("CONTROL_API_TIMEOUT_SECONDS", "10"))
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        response = client.post(
            url, headers={"X-Gateway-Key": os.environ["GATEWAY_API_KEY"]}, json=body
        )
        response.raise_for_status()
        return response.json()
```

`heartbeat()` esegue il primo controllo Wi-Fi. `heartbeat(activation_token=sms)`
esegue il primo controllo SMS. Inviare l'heartbeat di prontezza soltanto dopo
aver verificato OpenVPN e applicato le regole locali. Non registrare token,
chiavi o profili nei log.

## Collaudo sul dispositivo

Provare entrambe le modalità, poi chiusura durante il traffico, SMS duplicato
o scaduto, riavvio e perdita di HTTPS oltre il lease. Verificare che il tecnico
raggiunga i servizi sull'IP VPN del gateway e che l'inoltro verso la LAN resti
bloccato.

Le seguenti richieste sono facoltative e usano lo stesso `X-Gateway-Key`:

| POST | Schema del body |
|---|---|
| `/api/v1/gateways/sessions/{id}/diagnostics` | [`GatewayObservation`](../../api/schemas/diagnostics.py) |
| `/api/v1/gateways/system-info` | [`SystemReport`](../../factoryvpn_common/observability.py) |
| `/api/v1/gateways/telemetry` | [`GatewayTelemetry`](../../api/schemas/telemetry.py) |
| `/api/v1/gateways/technical-logs` | [`EventBatch`](../../factoryvpn_common/observability.py) |

Il [rinnovo del certificato](CERTIFICATE_RENEWAL.md) usa un flusso HTTPS dedicato.
