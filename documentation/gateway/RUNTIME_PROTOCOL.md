# FactoryVPN — protocollo operativo del gateway

Protocollo **v1**, modello **Gateway Only**. Questo contratto serve a implementare
un agente firmware proprio dopo la [prima inizializzazione](INITIAL_SETUP.md).
L'agente Linux distribuito applica lo stesso percorso in
[main.py](../../gateway-agent/main.py). Il [rinnovo PKI](CERTIFICATE_RENEWAL.md)
è un flusso separato: un certificato valido da solo non autorizza una VPN.

## Credenziali e confini

Le richieste del gateway usano `SERVER_URL` come origin HTTPS e
`X-Gateway-Key: <GATEWAY_API_KEY>`. Verificare TLS, nome del server e orologio;
non seguire redirect né inviare segreti in query o log. La connessione HTTPS
deve funzionare prima e indipendentemente dal tunnel. La CA HTTPS è distinta
dalla PKI VPN. Il profilo `.ovpn` installato contiene endpoint UDP e fiducia VPN.

Il firmware non usa JWT del tecnico, `X-Worker-Key` o chiavi di fabbrica durante
le sessioni. Non chiama `/api/v1/internal/vpn/authorize`: è un hook del server
OpenVPN che verifica certificato e sessione. Neppure crea una sessione utente:
la richiesta parte dal portale o dalle API del tecnico autorizzato.

Un tecnico e un gateway possono partecipare ciascuno a una sola sessione,
anche durante la pulizia. Il server concede una coppia di IP VPN; non concede
LAN, rotte arbitrarie, accesso a tutti i peer o traffico Layer 2.

## Attesa e risveglio

All'avvio bloccare il traffico sull'interfaccia VPN, fermare eventuali processi
OpenVPN posseduti dall'agente e completare la registrazione HTTPS. Lo stato
gateway `ACTIVE` della registrazione non dimostra la presenza di un tunnel.

| Modalità | Comportamento senza sessione |
|---|---|
| `wifi` | Chiamare l'heartbeat ogni `GATEWAY_WIFI_CHECK_INTERVAL`: 30 s predefiniti, 5–300 s ammessi. Non occorre un modem. |
| `sms` | Leggere il modem ogni `GATEWAY_POLL_INTERVAL`: 5 s predefiniti, 1–10 s ammessi. Nessun heartbeat periodico prima di un SMS valido. |

In SMS restano possibili la registrazione e un rapporto iniziale d'inventario,
oltre al rinnovo PKI quando necessario o già pendente. Non trasformare questi
percorsi in un polling HTTPS continuo durante l'attesa SMS.

### SMS firmato

Il messaggio è testo ASCII con questo formato:

```text
FACTORYVPN: activate <session_id> <expires_unix> <signature>
```

`session_id` è l'UUID canonico minuscolo della sessione; `expires_unix` è la
scadenza Unix in secondi interi, **non** la durata del lease. La firma è costituita
dai primi **32 caratteri esadecimali minuscoli** dell'HMAC-SHA256 calcolato sui
byte UTF-8 di `"<UUID canonico> <scadenza intera>"`. La chiave è il testo UTF-8
di `GATEWAY_SMS_KEY`, senza decodificarlo da base64 o esadecimale. Non includere
prefisso, verbo, newline o spazi aggiuntivi nei byte firmati.

```python
import hashlib
import hmac
from uuid import UUID

def sms_signature(session_id, expires_unix, sms_key):
    payload = f"{UUID(session_id)} {int(expires_unix)}".encode("utf-8")
    return hmac.new(sms_key.encode("utf-8"), payload, hashlib.sha256).hexdigest()[:32]
```

Verificare formato, firma con confronto a tempo costante e scadenza strettamente
futura rispetto all'orologio UTC. Il numero mittente non sostituisce la firma.
Inviare il messaggio ricevuto come `activation_token` nell'heartbeat: anche il
server verifica la firma e che l'UUID sia quello della sessione ancora autorizzata.
Un SMS valido non autorizza il traffico senza questa conferma HTTPS.

Conservare il messaggio sul modem finché tunnel, rete e conferma di prontezza
non sono riusciti; un errore HTTPS deve permettere di riprovare lo stesso SMS.
Poi eliminarlo dal modem, mantenendo il token in memoria per gli heartbeat della
sessione. I duplicati non creano nuove sessioni né prolungano la scadenza.
Scartare messaggi alterati/scaduti; una risposta API negativa chiude il tentativo.
Dopo un riavvio, se l'SMS è già stato eliminato serve un nuovo invio dal portale
(`POST /api/v1/sessions/{id}/wake`, con autorizzazione del tecnico, non del gateway).
Il reinvio non prolunga la sessione. Wi-Fi recupera invece una sessione valida
al polling successivo.

## Heartbeat e preparazione del tunnel

```http
POST /api/v1/gateways/heartbeat
Content-Type: application/json
X-Gateway-Key: <GATEWAY_API_KEY>
```

Body iniziale Wi-Fi; per SMS sostituire `activation_token` con il messaggio valido:

```json
{"vpn_ip": null, "ready_session_id": null, "activation_token": null}
```

`vpn_ip` è l'IPv4 realmente assegnato al tunnel, mai l'IP LAN o mobile.
`ready_session_id` rimane `null` finché la rete locale della sessione non è pronta;
poi contiene l'UUID della sessione. `activation_token` è `null` in Wi-Fi e testo
lungo al massimo 160 caratteri in SMS. Non inviare l'UUID del gateway nel body:
l'identità deriva dalla credenziale. Le chiamate sono ripetibili senza
`Idempotency-Key`.

Risposta senza autorizzazione:

```json
{"connect": false, "network": null, "lease_seconds": 0}
```

Esempio di risposta autorizzata:

```json
{
  "connect": true,
  "network": {
    "session_id": "11111111-1111-4111-8111-111111111111",
    "technician_ip": "10.100.128.2",
    "gateway_ip": "10.100.0.2"
  },
  "lease_seconds": 300,
  "expires_at": "2030-01-01T15:00:00+00:00"
}
```

Validare tipi e campi prima di modificare la rete. Gli indirizzi sono IPv4:
gateway nel pool `10.100.0.0/17`, tecnico in `10.100.128.0/17`; `10.100.0.0`
e `10.100.0.1` non sono indirizzi gateway. In SMS la sessione deve coincidere
con quella firmata. Una risposta incoerente non rinnova l'autorizzazione.

1. Con `connect: false`, bloccare subito il traffico, fermare il proprio client
   OpenVPN, eliminare l'attivazione SMS e tornare all'attesa.
2. Con una nuova sessione autorizzata, mantenere il firewall bloccato e avviare
   OpenVPN con il profilo installato. Attendere un handshake riuscito e verificare
   che l'IP del tunnel coincida con `network.gateway_ip`: la sola presenza di
   `tun0`, eventualmente rimasta dopo una riconnessione, non prova la prontezza.
3. Riconfermare via heartbeat, con `vpn_ip` reale e `ready_session_id: null`.
   Installare le regole solo se `connect` resta vero e la coppia/sessione è
   invariata; usare esclusivamente il lease residuo di questa risposta.
4. Quando le regole sono installate, inviare un heartbeat con `vpn_ip` e
   `ready_session_id` valorizzati. Una risposta negativa o una coppia diversa
   richiede blocco e arresto. Il worker centrale osserva entrambi i peer, prepara
   la rete server bloccata e comunica la prontezza all'API, che imposta `ACTIVE`.
   Abilita il lease server solo dopo la conferma API riuscita.
5. Durante la sessione, ripetere controllo tunnel e heartbeat con il relativo
   `ready_session_id` ogni `GATEWAY_POLL_INTERVAL`. Se cambia la coppia/sessione,
   pulire prima la precedente e ripetere la preparazione.

La risposta heartbeat non contiene lo stato della sessione del portale:
`connect: true` è un'autorizzazione limitata nel tempo, non la prova che il
tecnico sia già collegato o che il percorso completo sia pronto.
Sul server `REQUESTED` indica la preparazione, `ACTIVE` la prontezza comunicata dal worker;
`CLOSED`, `EXPIRED` e `FAILED` sono terminali. Il firmware usa l'heartbeat,
non le API del tecnico, per conoscere il comportamento richiesto.

## Lease e regole di rete

`lease_seconds` è un intero positivo, massimo 900; può essere inferiore a 30
vicino alla scadenza. Il default server è 300, ulteriormente limitato da VPN,
login e SSO. Non impostare cinque minuti fissi sul gateway e non usare
`expires_at` come durata del firewall.

Misurare l'inizio della richiesta con orologio monotono. Prima di installare o
rinnovare calcolare `floor(lease_seconds - tempo_trascorso_dall_invio)`: il tempo
HTTP e di preparazione consuma il lease. Se il residuo è zero o negativo,
bloccare; una risposta lenta non crea altro tempo autorizzato. Ogni nuovo rinnovo
richiede una risposta HTTPS valida per la stessa coppia. Il keepalive OpenVPN
non rinnova il lease e il traffico non prolunga la durata totale della sessione.

Sul gateway le regole devono:

- consentire sull'interfaccia VPN soltanto pacchetti IPv4 fra
  `technician_ip` e `gateway_ip`, in entrambe le direzioni, durante il lease;
- applicare il limite a ogni pacchetto, comprese le risposte e le connessioni
  già stabilite: un generico `established,related accept` non deve aggirarlo;
- bloccare altro traffico in ingresso/uscita dal tunnel e tutto il forwarding
  da e verso l'interfaccia VPN: nessun inoltro VPN↔LAN, NAT LAN o bridge;
- far scadere le regole nel kernel anche se il processo agente si blocca.

L'implementazione Linux è [network.py](../../gateway-agent/network.py), con set
nftables a timeout. Il server applica anche il proprio controllo della coppia.
OpenVPN assegna gli IP e fornisce una rotta `/32` al peer; il firmware non deve
aggiungere rotte verso la LAN, default route o sostituzioni DNS. Il pool
`10.100.0.0/16` deve essere libero da sovrapposizioni locali. Servizi SSH/web,
account e firewall dell'applicazione restano responsabilità del firmware.

## Errori, arresto e ripresa

| Evento | Azione |
|---|---|
| Timeout/errore di trasporto, HTTP `408`, `429`, `5xx` | Solo se esistono già rete autorizzata e tunnel in esecuzione: conservarli senza rinnovare il lease. L'agente riprova al normale intervallo del ciclo. Nessuna nuova autorizzazione offline. |
| `401` | Credenziale operativa errata/revocata: bloccare, fermare il tunnel e richiedere verifica del provisioning. |
| `403` | SMS non valido o di un'altra sessione: bloccare, fermare il tunnel e scartare l'attivazione. |
| `404` | Identità non disponibile: bloccare e verificare la registrazione/provisioning. |
| `409` | IP tunnel diverso da quello assegnato: bloccare e verificare profilo/IP. |
| `422`, risposta malformata, errore locale di tunnel/firewall | Bloccare e fermare il tunnel; correggere la causa, senza conservare un'autorizzazione sulla base di dati non validi. |
| `connect: false` o arresto dell'agente | Ritirare le regole prima di arrestare il client; conservare identità e profili persistenti. |
| Lease scaduto durante un guasto HTTPS | Il kernel blocca il traffico autonomamente. Il processo OpenVPN può restare in esecuzione: la scadenza del lease non equivale all'arresto immediato del tunnel. |

Una revoca ricevuta non beneficia della tolleranza ai guasti. Se HTTPS è
irraggiungibile, il limite rimane il lease residuo; la scadenza kernel deve
bloccare anche traffico già aperto. Dopo il recupero HTTPS una sessione ancora
valida può riprendere, ma applicazioni e SSH potrebbero essersi già disconnessi.
L'agente deve gestire solo il processo OpenVPN che possiede, con arresto a tempo
limitato, senza terminare indiscriminatamente altri client del dispositivo.

## Osservabilità e collaudo firmware

Diagnostica, inventario, telemetria e log sono facoltativi per il controllo VPN:
non autorizzano traffico e non rinnovano lease. Tutti usano `X-Gateway-Key`;
la tabella riporta i riferimenti pubblici per payload, risposte e validazioni.
Il contratto OpenAPI destinato ai client del portale non comprende queste API macchina.

| Metodo e percorso | Schemi | Gestione server |
|---|---|---|
| `POST /api/v1/gateways/sessions/{id}/diagnostics` | [GatewayObservation, ObservationResponse](../../api/schemas/diagnostics.py) | [Router gateways](../../api/routers/gateways.py), [controlli temporali e stato sessione](../../api/services/diagnostics.py) |
| `POST /api/v1/gateways/system-info` | [SystemReport](../../factoryvpn_common/observability.py), [ObservationResponse](../../api/schemas/diagnostics.py) | [Router gateway_observability](../../api/routers/gateway_observability.py), [record_system](../../api/services/gateway_observability.py) |
| `POST /api/v1/gateways/telemetry` | [GatewayTelemetry](../../api/schemas/telemetry.py), [ObservationResponse](../../api/schemas/diagnostics.py) | [Router telemetry](../../api/routers/telemetry.py), [controlli temporali](../../api/services/telemetry.py) |
| `POST /api/v1/gateways/technical-logs` | [EventBatch, TechnicalEvent, BatchResult](../../factoryvpn_common/observability.py) | [Router gateway_observability](../../api/routers/gateway_observability.py), [record_logs e limiti](../../api/services/gateway_observability.py) |

I codici degli eventi tecnici sono il catalogo `LOG_CODES` nel modulo
`observability`; non accettano testo libero. Non inviare log grezzi o segreti.
`recorded: false` indica un'osservazione non acquisita, non un rifiuto della VPN;
la risposta dei log riporta invece `accepted`, `duplicates` e `discarded`.

Prima della consegna verificare su hardware: SMS duplicato/alterato/scaduto,
HTTPS indisponibile e risposte lente, riavvio e riconnessione OpenVPN, chiusura
durante traffico TCP/UDP/ICMP, due coppie isolate, accesso LAN negato, agente
bloccato fino alla scadenza kernel, rinnovo PKI e persistenza dopo perdita di
alimentazione. Le prove del repository in
[test_gateway_modes.py](../../tests/test_gateway_modes.py),
[test_network_contract.py](../../tests/test_network_contract.py) e
[network_smoke.py](../../tests/network_smoke.py) aiutano a verificare il contratto,
ma non sostituiscono il collaudo del firmware e della rete effettivi.
