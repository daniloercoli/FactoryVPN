# FactoryVPN — primo avvio del gateway

Protocollo API **v1**. Servono un'identità individuale, un profilo OpenVPN e una
connessione Internet funzionante. Per le richieste che aprono una sessione,
vedere [apertura VPN dal gateway](RUNTIME_PROTOCOL.md).

Per provare l'agente contro un server già disponibile, vedere la
[mini guida del singolo gateway Docker](DOCKER_SIMULATOR.md).

## Preparare il dispositivo

- **Consegna manuale, anche per i test.** Chiedere all'amministratore lo ZIP
   scaricato e attivato nel portale. Copiare entrambi i file del pacchetto:
   - `gateway.env` in `/etc/factoryvpn/gateway.env` per URL, identità,
     credenziali, modalità e impostazioni del tunnel.
   - `gateway.ovpn` in `/etc/factoryvpn/gateway.ovpn` per profilo, certificato
     e chiave privata OpenVPN.
- **Dispositivo predisposto in fabbrica.** Usare il
   [contratto di arruolamento](FACTORY_ENROLLMENT.md) per completare la consegna
   dei due file via HTTPS. Al termine, la registrazione è la stessa del percorso
   manuale.

Conservare file individuali e journal su memoria persistente scrivibile, con
permessi `0600` sui file sensibili. Caricare `gateway.env` nell'ambiente
dell'agente. Configurare Internet, DNS, orologio e trust store HTTPS. Se serve
una CA HTTPS dedicata, indicare il bundle con `SSL_CERT_FILE`. Consentire anche
l'uscita UDP verso host e porta del profilo OpenVPN. In modalità SMS predisporre
modem, SIM e accesso dati. In modalità Wi-Fi predisporre la rete e usare il MAC
del pacchetto. Evitare sovrapposizioni con il pool `10.100.0.0/16`.

## Registrazione HTTPS

Prima di iniziare l'attesa, bloccare il traffico del tunnel e fermare eventuali
client OpenVPN posseduti dall'agente. Inviare la richiesta a `SERVER_URL`,
l'origin HTTPS del pacchetto senza `/api/v1`.

```http
POST /api/v1/gateways/register
Content-Type: application/json
X-Gateway-Key: <GATEWAY_API_KEY>
```

| Campo JSON | Valore da `gateway.env` |
|---|---|
| `gateway_id` | `GATEWAY_ID` (UUID) |
| `activation_mode` | `GATEWAY_ACTIVATION_MODE` (`sms` o `wifi`) |
| `phone_number` | `GATEWAY_PHONE_NUMBER` (numero internazionale con `+` per SMS, `null` se assente) |
| `mac_address` | `MAC_ADDRESS` (obbligatorio per Wi-Fi, `null` se assente) |

Esempio SMS:

```json
{
  "gateway_id": "22222222-2222-4222-8222-222222222222",
  "activation_mode": "sms",
  "phone_number": "+15550100001",
  "mac_address": null
}
```

Esempio Wi-Fi:

```json
{
  "gateway_id": "33333333-3333-4333-8333-333333333333",
  "activation_mode": "wifi",
  "phone_number": null,
  "mac_address": "02:00:00:00:00:01"
}
```

Usare i valori del pacchetto, anche per i campi facoltativi. Inviare solo i
quattro campi indicati. Verificare TLS e nome del server, evitare redirect e
tenere segreti e profilo fuori dai log.

Con **HTTP 200**, controllare che `id` e `activation_mode` coincidano con i valori
inviati. `vpn_ip` è l'indirizzo VPN assegnato al gateway. `status: ACTIVE` in
questa risposta conferma la registrazione. La registrazione è ripetibile dopo
un riavvio o una risposta persa e conserva l'IP già assegnato.

| Esito | Azione |
|---|---|
| Timeout, errore di rete, `5xx` | Ritentare con attesa crescente e gli stessi dati. |
| `401` o `404` | Verificare con l'operatore provisioning, attivazione e chiave. |
| `403` | L'UUID non corrisponde alla credenziale. Usare entrambi i file dello stesso pacchetto. |
| `409` | Verificare con l'operatore i dati provisionati per il dispositivo. |
| `422` | Correggere formato o campi del body. |

## Invio facoltativo di IMEI e ICCID

Dopo il `200` di `register`, se sono disponibili letture del modem, inviare un
report allo stesso `SERVER_URL` con la stessa `GATEWAY_API_KEY`.

```http
POST /api/v1/gateways/system-info
Content-Type: application/json
X-Gateway-Key: <GATEWAY_API_KEY>
```

Generare un nuovo UUID per `report_id` e usare la data e l'ora effettive della
lettura in UTC per `observed_at`. I valori seguenti sono dimostrativi.

```json
{
  "report_id": "44444444-4444-4444-8444-444444444444",
  "observed_at": "2026-11-17T10:30:00Z",
  "components": {
    "modem": {
      "state": "reported",
      "imei": "012345678901234",
      "iccid": "89000000000000000001"
    }
  }
}
```

Inviare entrambi come stringhe. L'IMEI ha 15 cifre, l'ICCID da 18 a 22. Omettere
i valori mancanti o non validi per conservare quelli già registrati. Una risposta
`200` con `{"recorded": true}` conferma l'acquisizione. L'agente Linux tenta
l'invio all'avvio, anche in modalità SMS.

Proseguire con [l'attesa SMS o Wi-Fi](RUNTIME_PROTOCOL.md) anche se il modem non
fornisce dati o il report non viene acquisito.

## Usare l'agente su Yocto

[`gateway-agent/`](../../gateway-agent/) contiene l'agente Python di riferimento
per Linux. [`main.py`](../../gateway-agent/main.py) avvia il ciclo,
[`registration.py`](../../gateway-agent/registration.py) e
[`heartbeat.py`](../../gateway-agent/heartbeat.py) eseguono le richieste HTTPS.

Per integrarlo nella propria immagine Yocto:

1. Includere i moduli `gateway-agent`, `factoryvpn_common` e lo
   [schema di registrazione](../../api/schemas/gateway.py) nel package `api`.
   L'agente usa Python 3.11 o successivo, `httpx`,
   `pydantic` 2, `cryptography`, `python-dotenv` e `pyserial`.
2. Includere OpenVPN, iproute2, nftables, il dispositivo kernel `/dev/net/tun`,
   il supporto nftables per set a scadenza e le CA HTTPS. Dare al servizio i
   permessi per gestire tunnel e firewall; per SMS anche accesso alla porta AT
   del modem indicata in `MODEM_PATH`.
3. Adattare il comando `python3 -m gateway-agent.main` e i percorsi al layout
   dell'immagine. Con systemd usare come base
   [l'unità fornita](../../gateway-agent/factoryvpn-agent.service), che carica
   `gateway.env`. Verificare che gli aggiornamenti del firmware preservino la
   directory delle credenziali, il profilo e i journal.
4. Collaudare [l'apertura e la chiusura](RUNTIME_PROTOCOL.md#collaudo-sul-dispositivo)
   sull'immagine e sul modem effettivi. Per la sostituzione del certificato usare
   la [guida al rinnovo](CERTIFICATE_RENEWAL.md).

L'integrazione nell'immagine e il collaudo su hardware Yocto sono da eseguire
sulla piattaforma di destinazione.
