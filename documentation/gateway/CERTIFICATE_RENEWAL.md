# FactoryVPN — integrazione del rinnovo certificati nel gateway

Versione del protocollo API: **v1**.
Questa guida è destinata agli sviluppatori del firmware o dell'agente gateway
che integrano FactoryVPN con root offline e Sub-CA online. Descrive il contratto
HTTPS, le firme e il recupero delle operazioni interrotte necessari al rinnovo remoto.

## Prima inizializzazione e rinnovi successivi

La prima inizializzazione usa il [percorso di fine linea](FACTORY_ENROLLMENT.md)
con approvazione e consegna HTTPS, oppure la copia manuale HQ di `gateway.ovpn`
e `gateway.env` dallo ZIP preparato, scaricato e attivato nel portale. I percorsi standard
sono `/etc/factoryvpn/gateway.ovpn` e `/etc/factoryvpn/gateway.env`, con permessi
`0600`. La procedura operativa è in [installazione del gateway](../DOCKER_GUIDE.md#utenti-e-gateway).

Al primo avvio l'agente usa i dati già installati per confermare la registrazione
con `POST /api/v1/gateways/register`, come descritto nella
[guida alla prima inizializzazione](INITIAL_SETUP.md).
La copia manuale dei file non elimina questa comunicazione iniziale via HTTPS.

Le API `/api/v1/gateways/certificates` descritte qui servono **solo per i rinnovi
successivi**: non creano il gateway e non consegnano le sue credenziali iniziali.
Dopo il provisioning iniziale, il firmware deve implementare questo protocollo
per sostituire chiave e certificato da remoto, senza ricopiare i due file a bordo macchina.

## Obiettivo e prerequisiti

Rinnovare chiave e certificato OpenVPN senza accesso locale al gateway, conservando
UUID, credenziale HTTPS `GATEWAY_API_KEY` e segreto `GATEWAY_SMS_KEY`.
I certificati client durano **3650 giorni** per default; quello server OpenVPN
**1825 giorni**. Usare sempre le date effettive restituite/lette dal certificato.
Il rinnovo viene preparato nell'ultimo anno di validità; non attendere l'ultimo giorno.

Il gateway deve avere:

- HTTPS verso `SERVER_URL` anche senza tunnel VPN, DNS funzionante e orologio corretto;
- fiducia HTTPS indipendente dalla root VPN, con verifica del nome e della catena TLS;
- API key attiva, registrazione già completata e certificato corrente non revocato;
- accesso alla chiave privata del certificato corrente, anche se questo è scaduto;
- generazione e firma ECDSA P-256, SHA-256, lettura CSR/X.509 e memoria persistente;
- un solo processo responsabile dell'aggiornamento del profilo OpenVPN.

Una scadenza può essere recuperata via HTTPS. Revoca, cambio gruppo, perdita della
chiave privata o API key invalidata richiedono nuova autorizzazione/provisioning.
Il recupero di una Sub-CA compromessa non rientra in questo protocollo.

## Trasporto e autenticazione

Tutte le URL seguenti sono relative a `https://<server>`.
Tutte le richieste macchina usano:

```http
X-Gateway-Key: <credenziale individuale del gateway>
Content-Type: application/json
```

`Content-Type` serve per i POST con JSON. Non usare cookie browser, JWT del tecnico,
`X-Worker-Key`, SMS come autorizzazione al rinnovo o API di provisioning umane.
Il server ricava il gateway dall'API key: non si specifica un altro gateway nel body.
Non seguire redirect verso origini diverse; non disabilitare la verifica HTTPS.
Non registrare API key, chiavi private o profili `.ovpn` nei log.

Il rinnovo non richiede una VPN, una sessione tecnica o un download del pacchetto ZIP.
Non ruotare API key/SMS durante questo flusso.

## Endpoint

Prefisso comune `B = /api/v1/gateways/certificates`.
`renewal_id` è l'UUID della richiesta restituito dal server.

| Metodo e URL | Body/header specifico | Esito positivo |
|---|---|---|
| `GET B` | Nessuno | `200`: certificato corrente, necessità di rinnovo ed eventuale richiesta pendente |
| `POST B/renewals` | `Idempotency-Key: <UUID>`; `previous_certificate_id`, `csr_pem` | `201`: richiesta/challenge, oppure stessa richiesta in caso di retry |
| `GET B/renewals/{renewal_id}` | Nessuno | `200`: stato persistente e bundle pubblico quando disponibile |
| `POST B/renewals/{renewal_id}/submit` | `signature` con vecchia chiave | `200`: richiesta in coda; ripetibile |
| `POST B/renewals/{renewal_id}/activate` | `signature` con nuova chiave; `certificate_sha256` | `200`: nuovo certificato attivo, vecchio revocato; ripetibile |
| `POST B/renewals/{renewal_id}/confirm` | `signature` con nuova chiave | `200`: installazione confermata; ripetibile |
| `POST B/renewals/{renewal_id}/cancel` | Nessun body | `200`: annullamento prima dell'attivazione; dopo l'attivazione restituisce `409` |

Gli [schemi delle richieste](../../api/schemas/certificate_renewal.py) definiscono
la validazione degli input; queste API macchina non fanno parte dell'esportazione
OpenAPI destinata ai client umani del portale.

## Sequenza da implementare

### 1. Controllare il certificato e lo stato remoto

Leggere localmente `notAfter`. Se mancano al massimo 365 giorni, avviare il rinnovo
anche in modalità SMS inattiva. Se c'è già un journal locale, riprenderlo sempre.

Quando il gateway comunica normalmente via HTTPS, controllare anche `GET B` per
le richieste anticipate dell'amministratore. L'agente di riferimento lo fa al
risveglio e, se continuamente sveglio senza rinnovi, al massimo una volta al giorno.
In attesa SMS senza scadenza vicina e senza rinnovo pendente non esegue polling
HTTPS per questo scopo. Il rinnovo non deve attivare autonomamente una VPN.

Risposta schematica; UUID e date sono sostituiti da segnaposto:

```json
{
  "gateway_id": "<UUID gateway>",
  "certificate_id": "<UUID certificato corrente>",
  "serial": "<seriale decimale come stringa>",
  "expires_at": "<data ISO 8601 UTC>",
  "renewal_due": true,
  "renewal": null
}
```

Non convertire il seriale in un numero floating point. `certificate_id` è un UUID
del registro, distinto dal seriale X.509. `renewal` contiene una richiesta pendente
se esiste. Se manca il journal/nuova chiave per quella richiesta, non attivarla.

### 2. Generare e salvare chiave, CSR e idempotenza

Generare una **nuova** chiave ECDSA P-256 sul gateway. Non inviarla mai al server.
Creare una CSR PKCS#10 PEM firmata con tale chiave. Il server verifica firma e curva;
ignora identità/estensioni richieste e assegna `CN=gateway-<UUID autenticato>`,
`CA=false`, uso TLS client. Una CSR che chiede `CA=true` viene rifiutata.

Prima di qualsiasi POST salvare in modo durevole: nuova chiave privata, CSR PEM,
UUID `Idempotency-Key`, UUID del certificato precedente e fase locale. Usare file
`0600`, directory privata, scrittura temporanea, `fsync`, rename atomico e `fsync`
della directory. Se questa scrittura fallisce non inviare la richiesta.

```http
POST /api/v1/gateways/certificates/renewals
Idempotency-Key: <lo stesso UUID per tutti i retry di questa richiesta>
```

```json
{
  "previous_certificate_id": "<certificate_id ottenuto da GET B>",
  "csr_pem": "-----BEGIN CERTIFICATE REQUEST-----\n...\n-----END CERTIFICATE REQUEST-----\n"
}
```

La CSR deve essere P-256, lunga 100–16384 caratteri e al massimo 16384 byte.
I body JSON di questo protocollo rifiutano campi extra; il campo `signature`
richiede 8–512 caratteri base64. Generare un nuovo UUID solo per una nuova
operazione, mai per un timeout.
Stesso UUID con CSR o certificato precedente differenti produce `409`.
Il limite è **10 nuove richieste per gateway in 24 ore**. Una sola sostituzione può
essere pendente, condividendo il vincolo con il provisioning umano.

### 3. Provare il possesso della vecchia chiave

La richiesta restituisce questi campi:

```json
{
  "id": "<renewal_id>",
  "gateway_id": "<UUID gateway>",
  "previous_certificate_id": "<UUID precedente>",
  "certificate_id": null,
  "state": "challenge",
  "challenge": "<base64 standard>",
  "signature_algorithm": "ecdsa-sha256-der",
  "challenge_expires_at": "<UTC: circa 15 minuti dalla creazione>",
  "expires_at": "<UTC: circa 24 ore dalla creazione>",
  "maintenance_until": null,
  "bundle": null,
  "error_code": null
}
```

Salvare `id`. Decodificare `challenge` da base64 standard e firmare **esattamente
quei byte** con la vecchia chiave usando ECDSA/SHA-256. Codificare la firma DER
ASN.1 in base64 standard, non base64url e non concatenazione raw `r || s`.
Non firmare il testo base64 e non calcolare autonomamente una challenge alternativa.
La challenge include richiesta, gateway, credenziale, generazione precedente,
hash della CSR normalizzata e nonce. Il client deve trattarla come byte opachi.

```http
POST /api/v1/gateways/certificates/renewals/{renewal_id}/submit
```

```json
{"signature": "<base64 della firma DER con la vecchia chiave>"}
```

Il successo porta a `queued`; se l'emissione è già progredita un retry può restituire
`ready`, `active` o `confirmed`. La challenge non può essere usata per altri gateway.

### 4. Attendere e verificare il bundle pubblico

Interrogare `GET B/renewals/{renewal_id}`, con intervallo indicativo di 60 secondi.
Il worker firma la CSR e porta lo stato a `ready`. `bundle` contiene:

```json
{
  "certificate_pem": "<certificato client PEM>",
  "issuer_pem": "<Sub-CA emittente PEM>",
  "trust_pem": "<una o più root pubbliche PEM>",
  "issuer_id": "<SHA-256 DER della Sub-CA, esadecimale minuscolo>",
  "certificate_sha256": "<SHA-256 DER del client, esadecimale minuscolo>",
  "trust_generation": 2
}
```

La nuova chiave privata non compare nel bundle. Prima dell'attivazione verificare:

- corrispondenza della chiave pubblica client con la nuova chiave locale;
- CN esatto `gateway-<UUID proprio>`, `CA=false`, `digitalSignature`, EKU esclusivamente `clientAuth`;
- impronte SHA-256 calcolate sul DER del certificato, non sui byte PEM;
- da 1 a 32 root auto-firmate, firme root → Sub-CA → client; chiavi CA P-256,
  BasicConstraints e KeyUsage critiche, `CA=true`, `keyCertSign`, `cRLSign`,
  `pathLen=1` per root e `pathLen=0` per Sub-CA; una sola root deve corrispondere
  all'emettitore della Sub-CA;
- validità temporale della catena scelta e scadenza client non successiva a quella
  dell'emettitore, a sua volta non successiva alla root;
- generazione di fiducia intera e **non inferiore** a quella già installata.

Il bundle arriva sul canale HTTPS già autenticato: può introdurre una nuova root
VPN durante un ricambio ordinario. Non richiedere che sia già nota al profilo VPN,
altrimenti il recupero della fiducia non sarebbe possibile. Conservare nel profilo
il commento `# factoryvpn-trust-generation <numero>` o un equivalente dato durevole.
L'agente distribuito usa il commento.

Preparare un nuovo profilo completo sostituendo solo `<ca>`, `<extra-certs>`,
`<cert>`, `<key>` e generazione di fiducia. Mantenere endpoint, TLS-crypt e direttive
locali già configurate. Non interpretare dati remoti come direttive OpenVPN/shell.
Ri-serializzare gli oggetti PEM verificati, senza copiare testo aggiuntivo arbitrario.
Salvare il profilo candidato e sincronizzarlo su disco **prima** dell'attivazione.

### 5. Attivare quando la VPN è ferma

La preparazione può avvenire durante una VPN. L'attivazione deve attendere OpenVPN
locale fermo, nessuna sessione prenotata/attiva sul server e pulizia completata.
Non chiudere una sessione solo perché è arrivato un certificato candidato.

Il messaggio da firmare con la **nuova** chiave è UTF-8, con LF e LF finale:

```text
factoryvpn-certificate-activate-v1
<renewal_id>
<certificate_sha256 esadecimale minuscolo>
```

Equivalente preciso: `"factoryvpn-certificate-activate-v1\n" + renewal_id +
"\n" + certificate_sha256 + "\n"`.

Persistire nel journal l'intenzione di attivare, poi chiamare:

```http
POST /api/v1/gateways/certificates/renewals/{renewal_id}/activate
```

```json
{
  "signature": "<base64 ECDSA/SHA-256 DER con la nuova chiave>",
  "certificate_sha256": "<impronta del candidato verificato>"
}
```

Il server attiva il candidato e revoca il vecchio certificato nella stessa
transazione. Non cambia API key/SMS. Lo stato diventa `active`; per cinque minuti
`maintenance_until` impedisce nuove prenotazioni mentre il gateway installa i file.
Una sessione o pulizia pendente produce `409`, lasciando il vecchio certificato attivo.

Se la risposta si perde, **non presumere un fallimento e non tornare al vecchio
profilo**. Rileggere lo stato con lo stesso `renewal_id`. `active`/`confirmed`
impongono di completare l'installazione della nuova coppia; `ready` permette il retry.
Durante l'esito incerto non avviare localmente OpenVPN con il vecchio profilo.

### 6. Installare e confermare

Dopo avere osservato `active`, sostituire atomicamente il profilo con il candidato
già durevole. Sincronizzare il file e la directory, conservare permessi `0600`.
Usare una coppia certificato/chiave coerente; un unico `.ovpn` inline semplifica
l'atomicità. Solo dopo avere installato il profilo chiamare:

```http
POST /api/v1/gateways/certificates/renewals/{renewal_id}/confirm
```

```json
{"signature": "<firma con nuova chiave dello stesso messaggio di attivazione>"}
```

Il server restituisce `confirmed` e libera la manutenzione. Eliminare il journal
solo dopo il successo; una conferma persa viene ripetuta al riavvio con la nuova
chiave. Non rinnovare API key/SMS e non riscaricare il pacchetto iniziale.
Una vecchia richiesta `confirmed` riferita a un certificato poi revocato o sostituito
non può reinstallarlo: il server restituisce `409`.

## Codice delle firme

Questo estratto illustra i byte e la codifica; il journal e la macchina a stati
restano obbligatori. Dipendenze: Python e `cryptography`.

```python
import base64
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

new_key = ec.generate_private_key(ec.SECP256R1())
csr = (
    x509.CertificateSigningRequestBuilder()
    .subject_name(x509.Name([]))  # il server assegna l'identità
    .sign(new_key, hashes.SHA256())
)
csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode("ascii")
# Persistire new_key, csr_pem e request UUID prima del POST.

def signature(key, message: bytes) -> str:
    der = key.sign(message, ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(der).decode("ascii")

# old_key è la chiave del certificato attualmente installato.
# challenge_response è il JSON di POST /renewals.
challenge_bytes = base64.b64decode(challenge_response["challenge"], validate=True)
submit_body = {"signature": signature(old_key, challenge_bytes)}

# ready_response è il JSON verificato quando state == "ready".
renewal_id = ready_response["id"]
digest = ready_response["bundle"]["certificate_sha256"]
message = f"factoryvpn-certificate-activate-v1\n{renewal_id}\n{digest}\n".encode("utf-8")
activate_body = {
    "certificate_sha256": digest,
    "signature": signature(new_key, message),
}
confirm_body = {"signature": signature(new_key, message)}
```

Non usare `Prehashed` a meno di implementare esplicitamente la stessa operazione:
il protocollo richiede ECDSA/SHA-256 del messaggio, non un doppio hash.

## Retry, stati e guasti

| Situazione | Comportamento gateway |
|---|---|
| Timeout/errore di rete o `5xx` | Conservare journal e chiavi; backoff e stesso UUID. Dopo una possibile attivazione rileggere prima lo stato. |
| `401` | API key non valida/revocata: non aggirare il blocco con altri account. Segnalare necessità di provisioning. |
| `403` su `submit`/`activate`/`confirm` | Prova di possesso errata: controllare chiave, byte e DER; non avanzare. |
| `404` | Richiesta assente/non più accessibile, anche dopo cambio identità/gruppo: non installare il bundle conservato senza riconciliazione. |
| `409` su stato/creazione/`submit` | Certificato non idoneo, registrazione incompleta, sostituzione pendente, idempotenza diversa o challenge scaduta: rileggere lo stato e riconciliare il journal; non cambiare UUID alla cieca. |
| `409` su `activate` | Rileggere lo stato; se ancora `ready`, attendere fine sessione/pulizia. Un conflitto di generazione non autorizza un rollback. |
| `422` | JSON/UUID/CSR/firma fuori contratto: correggere il client; evitare tentativi ciechi. |
| `429` | Limite giornaliero raggiunto; rallentare i retry, senza generare nuove chiavi a ogni tentativo. |
| `cancelled`/`failed` prima di attivare | Conservare il vecchio profilo; scartare candidato/journal solo dopo avere accertato che non sono stati attivati. |
| Disco pieno prima di attivare | Nessun POST di attivazione; il vecchio profilo resta valido. |
| Riavvio dopo `active` | Recuperare nuova chiave e candidato dal journal, installare, confermare. Mai ripristinare automaticamente il vecchio certificato. |
| Journal/nuova chiave persi dopo `active` | Il certificato nuovo non è recuperabile dal server con la chiave privata: serve intervento di provisioning. |

La challenge dura 15 minuti; il lavoro non attivato dura 24 ore. `active` non viene
annullato automaticamente alla scadenza di `maintenance_until`: il retry della
conferma resta possibile se l'identità è ancora valida. Non interpretare `expires_at`
come scadenza del certificato; leggere il certificato X.509 restituito.

`error_code` può indicare `expired`, `identity_changed`, `cancelled`,
`installation_restored` o `issuer_unavailable`. La CRL server viene aggiornata
dal worker; le autorizzazioni nel database revocano subito l'uso del vecchio seriale.

Un restore annulla i candidati non attivati. Le conferme già attivate sono
recuperabili solo se il certificato è ancora autorizzato. Dopo perdita totale del
server, la politica di recupero revoca le credenziali recuperate: questo protocollo
non promette recupero autonomo senza nuova autorizzazione.

## Riferimento e collaudo firmware

L'implementazione completa è [gateway-agent/certificate_renewal.py](../../gateway-agent/certificate_renewal.py),
integrata in [gateway-agent/main.py](../../gateway-agent/main.py). I file locali usati
sono `gateway.ovpn`, `gateway.ovpn.renewal.json` e `gateway.ovpn.renewal.ovpn`.
La directory deve essere scrivibile dall'agente; evitare un bind mount in sola
lettura del singolo profilo quando si vuole abilitare il rinnovo.

Per verificare un'implementazione, includere CSR/firme reali, certificato prossimo
alla scadenza e già scaduto, SMS inattivo, risposte perse, riavvio, disco pieno,
cambio gruppo, revoca API, sessioni/pulizia pendenti, restore e bundle alterati.
Verificare con OpenVPN il profilo prodotto dal gateway, anche dopo il ricambio
della Sub-CA. Esempi di test nel repository:
[test_certificate_renewal_postgres.py](../../tests/test_certificate_renewal_postgres.py),
[test_gateway_certificates.py](../../tests/test_gateway_certificates.py) e
[openvpn_provisioning_smoke.py](../../tests/openvpn_provisioning_smoke.py).

Prima del rilascio firmware ripetere sul gateway effettivo: perdita di alimentazione
prima/dopo ogni scrittura e POST, spazio pieno, HTTPS indisponibile, RTC errato,
riavvio del client OpenVPN, rinnovo dopo lunga permanenza offline e protezione dei
file. Le prove Docker/Linux non attestano dieci anni di compatibilità del firmware,
modem, TLS o algoritmi crittografici.
