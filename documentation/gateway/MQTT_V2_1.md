# FactoryVPN — MQTT dalla versione 2.1

L'introduzione di **MQTT è prevista dalla versione 2.1**, dopo la
[modalità Single LAN della 2.0](SINGLE_LAN_V2.md). La versione corrente resta
**0.9.0**, con la 1.0 in completamento. MQTT non è ancora implementato;
progettazione, contratti e data di rilascio restano da definire.

## Che cosa aggiungerà

MQTT offrirà un canale di messaggistica fra gateway e piattaforma. Nel modello
publish/subscribe, i componenti pubblicano messaggi su topic e un **broker** li
distribuisce ai destinatari autorizzati. Il gateway aprirà una connessione
uscente protetta con TLS per ricevere notifiche e inviare eventi, stato e dati
di telemetria, anche senza una sessione di assistenza aperta.

Single LAN riguarda l'accesso IP ai dispositivi del sito; MQTT riguarda la
comunicazione con il gateway. **Il traffico di assistenza continuerà a usare
la VPN**: la connessione al broker non autorizzerà da sola una sessione VPN.

## Benefici attesi

| Beneficio | Effetto per FactoryVPN |
| --- | --- |
| Notifiche più tempestive | Un gateway collegato potrà ricevere una richiesta senza attendere il successivo controllo HTTPS periodico. |
| Meno polling a vuoto | Gli aggiornamenti potranno essere inviati quando accadono; la riduzione del traffico andrà misurata includendo keepalive e riconnessioni. |
| Stato più leggibile | Eventi e conferme permetteranno di distinguere messaggio ricevuto, comando eseguito e VPN effettivamente pronta. |
| Integrazioni disaccoppiate | Servizi autorizzati potranno utilizzare gli stessi eventi senza aggiungere un collegamento dedicato da ogni gateway a ciascun consumatore. |

Una connessione persistente richiede connettività dati: MQTT non risveglia un
gateway privo di Internet. La convivenza con SMS e HTTPS sarà definita durante
la progettazione.

## Requisiti da mantenere

- **Identità e isolamento:** credenziali individuali, verifica TLS e permessi
  sui topic per impedire accessi ai messaggi di altri gateway o gruppi.
- **Comandi verificabili:** identificativo, scadenza e conferma applicativa.
  Le ritrasmissioni possono produrre duplicati: l'esecuzione dovrà essere
  idempotente. Un messaggio di attivazione obsoleto, accodato o retained,
  non dovrà riaprire una VPN.
- **Autorizzazioni a tempo:** permessi, revoche e lease continueranno a valere.
  Un guasto del broker non dovrà prorogare l'accesso; la presenza MQTT e le
  notifiche di disconnessione non dimostreranno la raggiungibilità VPN.

Broker, versione MQTT, topic, payload, QoS e gestione della riconnessione
restano scelte aperte. Il collaudo dovrà verificare isolamento, duplicati,
messaggi scaduti, guasti del broker e comportamento sulle reti mobili,
misurando latenza, traffico e risorse senza anticipare risultati prestazionali.

Il [protocollo gateway corrente](RUNTIME_PROTOCOL.md) resta il riferimento
per l'implementazione disponibile. Le caratteristiche del protocollo sono
descritte nella [specifica ufficiale OASIS MQTT 5.0](https://docs.oasis-open.org/mqtt/mqtt/v5.0/os/mqtt-v5.0-os.html).
