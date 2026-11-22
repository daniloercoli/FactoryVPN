# FactoryVPN — apertura VPN dal gateway

Protocollo API **v1**. Prima di attendere una sessione, caricare il pacchetto del
dispositivo e completare `POST /api/v1/gateways/register` come descritto nella
[prima inizializzazione](INITIAL_SETUP.md#registrazione-https). Il tecnico
richiede l'apertura dal portale. Il gateway risponde con la sequenza seguente.

## Modalità SMS

Prima di iniziare, completare la
[registrazione HTTPS](INITIAL_SETUP.md#registrazione-https) con
`activation_mode: "sms"` e il numero telefonico del pacchetto. Predisporre
modem, SIM, connessione dati, orologio sincronizzato e accesso HTTPS al server.
Tenere il traffico del tunnel bloccato e OpenVPN fermo.

Il percorso è: **ricevere l'SMS → verificarlo localmente → chiedere conferma
HTTPS → avviare OpenVPN → confermare IP e prontezza → eliminare l'SMS**.
La verifica HMAC e la conferma HTTPS sono entrambe necessarie: una firma valida
non dimostra che la sessione sia ancora autorizzata sul server.

### 1. Attendere e leggere il messaggio dal modem

Leggere il modem ogni `GATEWAY_POLL_INTERVAL` (default 5 s, da 1 a 10 s).
In attesa di un SMS valido, **non inviare heartbeat**. L'accettazione dell'invio
da parte del provider non prova che il gateway abbia ricevuto il messaggio.

Estrarre il solo body dell'SMS, separandolo da mittente, data, intestazioni AT
e risposta `OK` del modem. Nell'agente di riferimento la lettura usa
`AT+CMGL="ALL"`. Conservare anche l'indice del messaggio nel modem: servirà per
eliminarlo soltanto dopo la conferma finale. Leggere anche i messaggi già letti:
gli indici sono slot riutilizzabili, non contatori su cui basare la deduplicazione.

Esempio dimostrativo di body, generato con la chiave di prova `demo-sms-key`:

```text
FACTORYVPN: activate 11111111-1111-4111-8111-111111111111 1893456000 e52c4e871049904383132effa1754c30
```

La scadenza dell'esempio è il **1 gennaio 2030 alle 00:00:00 UTC**. Tutti gli
scambi di esempio presuppongono un istante precedente. La chiave di prova
serve soltanto al collaudo offline; sul dispositivo usare quella individuale
del pacchetto.

### 2. Verificare formato, scadenza e HMAC prima di contattare l'API

Eseguire nell'ordine i controlli di [verifica dell'SMS](#verificare-lsms):
parsing dei cinque campi, controllo della scadenza, ricostruzione del payload,
HMAC-SHA256 e confronto a tempo costante. Usare `GATEWAY_SMS_KEY`.

Se un controllo fallisce, scartare ed eliminare quel messaggio; non inviare
l'heartbeat di attivazione e non avviare OpenVPN. Se tutti i controlli passano,
conservare il testo ricevuto come `activation_token`, l'UUID canonico della
sessione e l'indice del messaggio. **Lasciare ancora l'SMS nel modem**.

L'UUID firmato è quello della **sessione**, non `GATEWAY_ID`. Il mittente
telefonico non sostituisce l'HMAC. Non generare una nuova firma sul gateway
per correggere un messaggio rifiutato.

### 3. Chiedere l'autorizzazione via HTTPS: heartbeat 1

Usare `SERVER_URL` e autenticare il gateway con la chiave HTTPS del pacchetto:

```http
POST /api/v1/gateways/heartbeat
Content-Type: application/json
X-Gateway-Key: <GATEWAY_API_KEY>
```

Inviare l'intero SMS già verificato in `activation_token`, non soltanto la firma:

```json
{
  "vpn_ip": null,
  "ready_session_id": null,
  "activation_token": "FACTORYVPN: activate 11111111-1111-4111-8111-111111111111 1893456000 e52c4e871049904383132effa1754c30"
}
```

Una possibile autorizzazione **HTTP 200** è:

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

Validare la risposta e procedere soltanto con `connect: true` e
`network.session_id` uguale all'UUID verificato nell'SMS. Conservare anche
la coppia di IP; questa conferma autorizza l'avvio del tunnel e non dichiara
il gateway già pronto.

Se la sessione non è più autorizzata, una possibile risposta **HTTP 200** è:

```json
{"connect": false, "network": null, "lease_seconds": 0}
```

In questo caso scartare l'attivazione e tornare in attesa. Se il token inviato
non è valido per la sessione richiesta sul server, è possibile ricevere
**HTTP 403**:

```json
{"detail": "SMS activation is invalid or belongs to another session"}
```

Fermare il tunnel e scartare l'attivazione rifiutata. Anche una risposta
positiva che indichi un UUID diverso da quello firmato deve essere rifiutata.

### 4. Avviare OpenVPN e confermare l'IP: heartbeat 2

Avviare il profilo OpenVPN del pacchetto mantenendo il traffico bloccato.
Attendere `Initialization Sequence Completed` e leggere l'IPv4 effettivo
del tunnel: deve coincidere con `network.gateway_ip`. Inviare quindi:

```json
{
  "vpn_ip": "10.100.0.2",
  "ready_session_id": null,
  "activation_token": "FACTORYVPN: activate 11111111-1111-4111-8111-111111111111 1893456000 e52c4e871049904383132effa1754c30"
}
```

La risposta positiva **HTTP 200** ha il formato mostrato al punto 3.
Controllare nuovamente `connect`, stesso UUID e stessa coppia di IP.
Solo dopo la conferma, applicare il lease locale con scadenza nel kernel,
sottraendo il tempo della richiesta. Regole e controlli sono descritti in
[avviare, confermare e mantenere il tunnel](#avviare-confermare-e-mantenere-il-tunnel).
Non eliminare ancora l'SMS.

### 5. Confermare la prontezza: heartbeat 3

Dopo aver verificato il tunnel e applicato le regole locali, inviare:

```json
{
  "vpn_ip": "10.100.0.2",
  "ready_session_id": "11111111-1111-4111-8111-111111111111",
  "activation_token": "FACTORYVPN: activate 11111111-1111-4111-8111-111111111111 1893456000 e52c4e871049904383132effa1754c30"
}
```

Attendere **HTTP 200** con `connect: true`, stesso UUID e stessa coppia di IP,
sempre nel formato del punto 3. Soltanto dopo questa conferma eliminare dal
modem il messaggio originale e verificare l'esito del comando di cancellazione
(`AT+CMGD=<indice>` nell'agente di riferimento). Conservare il token in RAM.
La conferma di prontezza viaggia via HTTPS: il gateway non deve inviare un SMS
di risposta. La raggiungibilità richiede anche il client del tecnico e
l'abilitazione della coppia sul server.

### 6. Mantenere la sessione, gestire i tentativi e tornare in attesa

Durante la sessione, continuare gli heartbeat ogni `GATEWAY_POLL_INTERVAL`
con IP, UUID pronto e **lo stesso token**, come al punto 5. L'agente di
riferimento esegue due heartbeat per ciclo e rinnova il lease dalla seconda
risposta. Riverificare anche la scadenza dell'SMS: quando è raggiunta,
bloccare il traffico, fermare OpenVPN e scartare il token.

| Situazione | Comportamento del gateway |
|---|---|
| Errore HTTPS temporaneo prima della conferma finale | Conservare il messaggio nel modem e ritentare finché è valido; non aprire o rinnovare traffico senza conferma API. |
| Errore HTTPS temporaneo durante una sessione pronta | Conservare soltanto il lease già installato e ritentare, senza prorogarlo offline. |
| Duplicato valido dello stesso UUID su un altro indice del modem | Si può eliminare il duplicato; conservare l'originale ancora in conferma o il token della sessione già pronta. |
| SMS valido per un'altra sessione mentre una è già in gestione | Non sostituire l'attivazione corrente; l'agente lascia l'altro messaggio nel modem per una successiva verifica. |
| `connect: false` o sessione/coppia di IP diversa durante la conferma | Bloccare e fermare il tunnel, scartare il token ed eliminare l'eventuale messaggio originale ancora nel modem. |
| `401`, `403`, `404`, `409` | Bloccare e fermare il tunnel, scartare l'attivazione; verificare credenziali, token, registrazione o IP secondo la [tabella dei rifiuti](#gestire-rifiuti-e-guasti). |
| `422`, risposta malformata o errore locale | Bloccare e fermare il tunnel, correggere il problema. L'agente non scarta automaticamente il token; conservarlo non autorizza traffico senza una nuova conferma API. |
| Riavvio prima della cancellazione dell'SMS | Ripetere registrazione, lettura e verifica completa del messaggio ancora presente, se non è scaduto. |
| Riavvio dopo la cancellazione dell'SMS | Il token in RAM è perso. Per una sessione ancora valida chiedere al tecnico il reinvio dell'SMS dal portale, poi rifare la sequenza. |

Il reinvio non prolunga la scadenza originaria. Alla chiusura ritirare prima
le regole, fermare il proprio client OpenVPN e scartare l'attivazione.
Tornare alla sola lettura del modem, senza heartbeat periodici in attesa.

## Modalità Wi-Fi

La connessione Wi-Fi a Internet deve essere già funzionante e la
[registrazione HTTPS](INITIAL_SETUP.md#registrazione-https) completata con
`activation_mode: "wifi"` e il MAC del pacchetto. Iniziare con il traffico del
tunnel bloccato e senza client OpenVPN dell'agente in esecuzione.

La sequenza seguente mostra uno scambio completo con **risposte ipotetiche,
coerenti con l'API**. UUID, indirizzi e scadenza sono dimostrativi: usare sempre
i valori effettivi ricevuti dal server. `activation_token` resta `null` in
tutte le richieste Wi-Fi; non servono SMS o modem.

Tutte le richieste seguenti sono POST allo stesso endpoint HTTPS:

```http
POST /api/v1/gateways/heartbeat
Content-Type: application/json
X-Gateway-Key: <GATEWAY_API_KEY>
```

I nomi heartbeat **1**, **2** e **3** indicano le tre fasi della
[chiamata heartbeat](#chiamata-heartbeat), non il numero totale di POST:
il controllo in attesa e gli heartbeat durante la sessione si ripetono.

### 1. Restare in attesa della richiesta del tecnico

Inviare l'heartbeat **1** ogni `GATEWAY_WIFI_CHECK_INTERVAL`
(default 30 s, da 5 a 300 s), con tutti e tre i campi a `null`:

```json
{
  "vpn_ip": null,
  "ready_session_id": null,
  "activation_token": null
}
```

Se nessun tecnico ha richiesto una sessione valida, la risposta è **HTTP 200**:

```json
{
  "connect": false,
  "network": null,
  "lease_seconds": 0
}
```

Lasciare OpenVPN fermo e il traffico bloccato. Attendere l'intervallo e
ripetere la stessa richiesta. `200` conferma che la chiamata è riuscita;
è `connect` a indicare se il gateway deve aprire la VPN.

### 2. Ricevere l'autorizzazione ad aprire la VPN

Il tecnico richiede la connessione dal portale. Al successivo controllo il
gateway invia **lo stesso body del punto 1** e può ricevere **HTTP 200**:

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

Validare e conservare l'UUID della sessione e la coppia di IP. Questa risposta
autorizza l'avvio di OpenVPN: non dichiara che il tunnel o il tecnico siano
già collegati. `lease_seconds` indica la durata dell'autorizzazione di rete;
`expires_at` è la scadenza complessiva della sessione e non si sposta con
gli heartbeat. Non aprire ancora il traffico locale.

### 3. Avviare OpenVPN e confermare l'IP del tunnel

Avviare OpenVPN con il profilo del pacchetto, attendere
`Initialization Sequence Completed` e leggere l'IPv4 realmente assegnato
all'interfaccia VPN. Deve coincidere con `network.gateway_ip`, nell'esempio
`10.100.0.2`. Se manca o è diverso, bloccare e fermare il tunnel.

Inviare l'heartbeat **2**, riportando l'IP effettivo e lasciando ancora
`ready_session_id` a `null`:

```json
{
  "vpn_ip": "10.100.0.2",
  "ready_session_id": null,
  "activation_token": null
}
```

Una possibile conferma **HTTP 200** è:

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

Procedere solo se `connect` è `true` e UUID e coppia di IP coincidono con
quelli del punto 2. Calcolare il lease residuo sottraendo il tempo della
richiesta e applicare le regole locali con scadenza nel kernel, come descritto
in [avviare, confermare e mantenere il tunnel](#avviare-confermare-e-mantenere-il-tunnel).
Le regole consentono soltanto la coppia tecnico↔gateway sul tunnel; l'inoltro
verso la LAN resta bloccato.

### 4. Confermare che il gateway è pronto

Soltanto dopo aver verificato il tunnel e applicato le regole locali, inviare
l'heartbeat **3** con IP e UUID della sessione:

```json
{
  "vpn_ip": "10.100.0.2",
  "ready_session_id": "11111111-1111-4111-8111-111111111111",
  "activation_token": null
}
```

Una possibile risposta **HTTP 200** è:

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

Verificare di nuovo `connect`, UUID e coppia di IP. La risposta ha lo stesso
formato delle precedenti: non contiene un campo `ready` o uno stato `ACTIVE`.
Il server registra la prontezza comunicata dal gateway; il tecnico può avviare
il proprio client quando il portale indica il gateway pronto. Il traffico
richiede anche il collegamento del tecnico e l'abilitazione della coppia sul
server: questa sola risposta non prova la raggiungibilità dei servizi.

### 5. Mantenere la sessione e rinnovare il lease

Durante la sessione usare `GATEWAY_POLL_INTERVAL` (default 5 s, da 1 a 10 s),
al posto dell'intervallo Wi-Fi di attesa. Continuare a inviare **il body del
punto 4**, finché IP e regole locali sono validi. Ogni risposta positiva ha
il formato del punto 4, con la stessa sessione e coppia di IP e il lease
aggiornato. Non rimettere `ready_session_id` a `null` durante una sessione
già pronta: il server annullerebbe la prontezza registrata. Il lease può essere
inferiore a 300 s per la durata residua della sessione o dell'accesso browser/SSO.

L'agente di riferimento esegue due heartbeat per ciclo: uno prima della
verifica del tunnel e uno dopo. Entrambi riportano IP e UUID della sessione
già pronta; il lease locale viene rinnovato dalla seconda risposta, sottraendo
il tempo trascorso nella richiesta. Non riavviare un tunnel già funzionante
e non ripetere da zero l'apertura a ogni controllo. Un keepalive OpenVPN
non rinnova il lease.

Se cambia l'UUID o la coppia di IP, chiudere la configurazione precedente
prima di preparare quella nuova. Se HTTPS non risponde, conservare soltanto
il lease già installato e ritentare: il guasto non autorizza a prorogarlo.
Alla sua scadenza il kernel deve bloccare il traffico anche se OpenVPN
è ancora in esecuzione. Per gli altri esiti seguire la
[gestione di rifiuti e guasti](#gestire-rifiuti-e-guasti).

### 6. Chiudere e tornare in attesa

Se il tecnico chiude la sessione, l'autorizzazione scade o viene revocata,
il successivo heartbeat, ancora con **il body del punto 4**, può ricevere
**HTTP 200**:

```json
{
  "connect": false,
  "network": null,
  "lease_seconds": 0
}
```

Ritirare subito le regole che consentono il traffico, poi fermare il client
OpenVPN dell'agente e dimenticare la sessione locale. Riprendere dal punto 1:
tre campi a `null` e intervallo Wi-Fi, senza attendere un altro comando.
La stessa risposta negativa va rispettata anche se arriva durante l'apertura,
prima della conferma di prontezza.

Un rifiuto HTTP richiede invece la gestione indicata nella tabella comune.
Ecco due possibili risposte di errore:

| Esito | Body della risposta | Azione |
|---|---|---|
| `401` | `{"detail": "Invalid gateway credentials"}` | Bloccare e fermare il tunnel; fare verificare la chiave e la sua attivazione. |
| `409` | `{"detail": "Tunnel address differs from assigned gateway address"}` | Bloccare e fermare il tunnel; verificare l'IPv4 letto dall'interfaccia e il profilo. |

Dopo un riavvio del gateway, ripetere la registrazione e ripartire dal punto 1
con traffico bloccato. Il polling può ritrovare una sessione ancora valida:
in quel caso rifare verifica del tunnel, conferma dell'IP e prontezza; non
ripristinare un vecchio lease dalla memoria persistente.

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

Questa verifica si svolge **localmente sul gateway, prima dell'heartbeat 1**.
Il server genera il messaggio con il segreto individuale dello stesso gateway
e ne verifica nuovamente l'autenticità quando riceve `activation_token`.

### Chiavi e dati da usare

| Dato | Provenienza e uso |
|---|---|
| `GATEWAY_SMS_KEY` | Segreto individuale in `gateway.env`, usato come chiave HMAC. Codificare il valore testuale direttamente in UTF-8, senza decodifica Base64 o esadecimale. |
| `GATEWAY_API_KEY` | Credenziale separata, inviata nell'header HTTPS `X-Gateway-Key`. Non usarla per calcolare l'HMAC. |
| `session_id` | UUID della sessione contenuto nell'SMS; non è l'UUID del dispositivo `GATEWAY_ID`. |
| `expires_unix` | Scadenza firmata, espressa in secondi Unix UTC; non millisecondi né durata relativa. |

Caricare le chiavi dal pacchetto attivato senza modificarne il contenuto.
L'agente richiede almeno 32 caratteri per `GATEWAY_SMS_KEY` nella configurazione
operativa. La chiave breve `demo-sms-key` usata sotto è soltanto un vettore di
prova offline. Non registrare chiavi o token completi nei log.

### Controlli da implementare, nell'ordine

Il formato emesso dal server è una riga con cinque campi separati da spazi:

```text
FACTORYVPN: activate <session_id> <expires_unix> <signature>
```

1. **Estrarre i cinque campi.** Il verificatore di riferimento rimuove il
   whitespace esterno e separa i campi sul whitespace. Richiedere esattamente
   cinque elementi: campi mancanti o aggiuntivi rendono il messaggio invalido.
   Controllare `FACTORYVPN:` e `activate` esattamente, rispettando le maiuscole.
2. **Leggere UUID e scadenza.** Validare l'UUID e convertirlo nella forma canonica
   minuscola con trattini. Convertire la scadenza in un intero decimale.
   Se uno dei due valori non è interpretabile, rifiutare il messaggio.
3. **Controllare l'ora.** Il messaggio è valido soltanto se `now_unix < expires_unix`.
   All'istante esatto della scadenza è già scaduto; non applicare una tolleranza
   che ne estenda la validità. Usare l'ora UTC del sistema sincronizzato.
4. **Ricostruire il testo firmato.** Concatenare UUID canonico, **un solo spazio
   ASCII `0x20`** e scadenza riscritta come intero decimale senza zeri iniziali.
   Codificare questo testo in UTF-8. Non firmare il body SMS grezzo: prefisso,
   verbo, firma, numero del mittente, UUID del gateway, newline e terminatore
   NUL non fanno parte del payload.
5. **Calcolare HMAC-SHA256.** Usare i byte UTF-8 di `GATEWAY_SMS_KEY` come chiave
   e i byte del payload come messaggio. Il digest completo è di 32 byte.
   Prendere i **primi 16 byte**, rappresentati come **32 caratteri esadecimali
   minuscoli**. Equivale a prendere i primi 32 caratteri dell'hexdigest completo;
   non inviare Base64 o tutti i 64 caratteri del digest.
6. **Confrontare la firma.** Confrontare la firma ricevuta con quella attesa
   mediante la funzione di confronto a tempo costante della libreria usata
   (`hmac.compare_digest` in Python). La firma ricevuta non va convertita in
   minuscolo o altrimenti corretta: una firma maiuscola o di lunghezza diversa
   viene rifiutata. Un confronto fallito non autorizza richieste di attivazione.
7. **Chiedere conferma al server.** Solo dopo il successo, conservare l'UUID
   canonico e il testo originale verificato. Inviare quest'ultimo integralmente
   in `activation_token`, mantenendolo uguale nelle chiamate successive.
   Verificare poi che la risposta HTTPS autorizzi proprio quell'UUID prima
   di avviare il tunnel, come nella [sequenza SMS](#modalità-sms).

La canonicalizzazione riguarda il payload dell'HMAC. Il parser di riferimento
tollera spazi aggiuntivi tra i campi e riconverte UUID e scadenza prima del
calcolo; il server emette già la forma canonica. Questa tolleranza non cambia
la stringa firmata e non consente di ignorare campi aggiuntivi o una firma errata.

### Vettore di prova riproducibile

Con la chiave testuale `demo-sms-key`, il payload UTF-8 esatto è di **47 byte**:

```text
11111111-1111-4111-8111-111111111111 1893456000
```

Il ritorno a capo del blocco qui sopra non fa parte dei byte da firmare.
Il digest HMAC-SHA256 completo atteso è:

```text
e52c4e871049904383132effa1754c3080eef83a3547dd846d767849dda12532
```

La firma troncata da confrontare e il body SMS completo sono rispettivamente:

```text
e52c4e871049904383132effa1754c30
```

```text
FACTORYVPN: activate 11111111-1111-4111-8111-111111111111 1893456000 e52c4e871049904383132effa1754c30
```

Prima di provare modem, HTTPS e OpenVPN, verificare questi risultati con la
libreria HMAC del firmware. Con `now_unix = 1893455999` il messaggio deve passare;
con `now_unix = 1893456000` deve essere rifiutato anche se la firma è corretta.

### Esempio Python della verifica locale

L'esempio seguente usa solo la libreria standard e restituisce l'UUID canonico
oppure `None`. Il parametro `now` serve a rendere ripetibili le prove; sul
gateway ometterlo per usare l'ora reale. La verifica della lunghezza della
chiave operativa avviene al caricamento della configurazione.

```python
import hashlib
import hmac
import time
from uuid import UUID


def verify_sms(message, sms_key, now=None):
    try:
        prefix, verb, raw_id, raw_expiry, received_signature = message.strip().split()
        if prefix != "FACTORYVPN:" or verb != "activate":
            return None
        session_id = str(UUID(raw_id))
        expiry = int(raw_expiry)
        if expiry <= (time.time() if now is None else now):
            return None
        payload = f"{session_id} {expiry}".encode("utf-8")
        expected_signature = hmac.new(
            sms_key.encode("utf-8"), payload, hashlib.sha256
        ).hexdigest()[:32]
        if not hmac.compare_digest(received_signature, expected_signature):
            return None
        return session_id
    except (ValueError, TypeError):
        return None


# Dati pubblici dimostrativi: non sono credenziali operative.
demo_sms = (
    "FACTORYVPN: activate 11111111-1111-4111-8111-111111111111 "
    "1893456000 e52c4e871049904383132effa1754c30"
)
assert verify_sms(demo_sms, "demo-sms-key", now=1893455999) == (
    "11111111-1111-4111-8111-111111111111"
)
assert verify_sms(demo_sms, "demo-sms-key", now=1893456000) is None
assert verify_sms(demo_sms, "chiave-diversa", now=1893455999) is None
assert verify_sms(
    demo_sms.replace("1893456000", "1893456001"), "demo-sms-key", now=1893455999
) is None
```

Nell'agente Python si può usare direttamente
[`verify_activation`](../../factoryvpn_common/protocol.py), condiviso con
l'API. Per passare dalla verifica all'heartbeat:

```python
sms = message_from_modem  # Solo body, conservato anche per i tentativi successivi.
session_id = verify_sms(sms, os.environ["GATEWAY_SMS_KEY"])
if session_id is not None:
    state = heartbeat(activation_token=sms)
    # Validare connect e network, incluso session_id, prima di avviare OpenVPN.
```

Quest'ultimo frammento usa `os` e la funzione `heartbeat` dell'
[esempio HTTPS per Yocto](#esempio-python-per-yocto); la lettura del modem e la
gestione completa degli errori restano nella sequenza precedente.

### Casi minimi da collaudare nel firmware

| Prova | Risultato atteso |
|---|---|
| Vettore corretto, ora precedente alla scadenza | UUID della sessione; è consentito tentare l'heartbeat 1. |
| Chiave di un altro gateway, UUID o scadenza alterati senza rifirmare | Rifiuto locale, nessuna attivazione HTTPS o OpenVPN. |
| Firma con un carattere cambiato, maiuscola, troncata o con caratteri non esadecimali | Rifiuto locale. |
| Prefisso/verbo errato, UUID invalido, scadenza non intera, campi mancanti o aggiuntivi | Rifiuto locale. |
| Ora uguale o successiva alla scadenza | Rifiuto locale anche con firma corretta. |
| HMAC locale valido, ma sessione chiusa o revocata sul server | Rispettare il rifiuto HTTPS e non aprire la VPN. |
| HMAC locale valido, ma risposta positiva con altro UUID | Bloccare e scartare l'attivazione; non avviare il tunnel. |

La firma consente tentativi ripetuti per la stessa sessione finché è valida;
non è un codice da consumare alla prima lettura. La verifica HTTPS impedisce
che un vecchio SMS riapra una sessione non più autorizzata.

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
