# Guide FactoryVPN

Questa cartella contiene le istruzioni operative per chi installa, amministra e
usa FactoryVPN e le guide di integrazione per gli sviluppatori dei gateway.

| Cosa devi fare | Guida |
| --- | --- |
| Installare server e gateway, configurare HTTPS e SMTP | [Installazione](DOCKER_GUIDE.md) |
| Impostare il nome visualizzato prima della build | [Nome dell'applicazione](DOCKER_GUIDE.md#nome-visualizzato-dellapplicazione) |
| Rinnovare CA, server, CRL e certificati gateway | [Manutenzione PKI](DOCKER_GUIDE.md#rotazione-e-manutenzione) |
| Applicare modifiche al server o riavviare i servizi | [Manutenzione del server](DOCKER_GUIDE.md#applicare-modifiche-al-server) |
| Configurare durata VPN, login e tolleranza alle interruzioni | [Durate e timeout nel .env](DOCKER_GUIDE.md#durate-timeout-e-reti-instabili) |
| Gestire utenti, gruppi, gateway e sessioni | [Amministratore](ADMINISTRATOR_GUIDE.md) |
| Collegarti a un impianto e chiudere l'intervento | [Tecnico](TECHNICIAN_GUIDE.md) |
| Raggiungere i servizi sull’IP VPN fisso del gateway | [Gateway Only](TECHNICIAN_GUIDE.md#entrare-in-ssh-sul-gateway) |
| Creare copie e recuperare il servizio | [Backup e ripristino](BACKUP_RESTORE.md) |
| Risolvere un problema o recuperare l'accesso | [Risoluzione dei problemi](TROUBLESHOOTING.md) |
| Integrare il primo avvio del gateway e la registrazione HTTPS | [Prima inizializzazione gateway](gateway/INITIAL_SETUP.md) |
| Avviare un singolo gateway Wi-Fi simulato con Docker | [Mini guida Docker gateway](gateway/DOCKER_SIMULATOR.md) |
| Aprire una VPN dal gateway via SMS o Wi-Fi | [Sequenza di chiamate ed esempio Python](gateway/RUNTIME_PROTOCOL.md) |
| Registrare a fine linea e completare l'arruolamento via HTTPS | [Arruolamento gateway](gateway/FACTORY_ENROLLMENT.md) |
| Implementare i rinnovi successivi dei certificati sul gateway | [Rinnovo remoto gateway](gateway/CERTIFICATE_RENEWAL.md) |

Per integrare applicazioni con le API utente e amministratore sono disponibili la
[guida per sviluppatori](../website/dist/developers.html) e il
[contratto OpenAPI](../website/dist/api/openapi.json).
Le API macchina per registrazione e rinnovo sono descritte nelle guide sopra,
separatamente dal contratto OpenAPI dei client del portale.

Per gli sviluppatori è disponibile anche il riferimento
[Single LAN dalla versione 2.0](gateway/SINGLE_LAN_V2.md): significato, differenze
da Gateway Only e requisiti della futura implementazione. La versione corrente
è la 0.9.0, con la 1.0 in completamento; la 2.0 non è attualmente in fase di
progettazione. Il documento non descrive
una funzionalità disponibile nella versione corrente.

L'evoluzione successiva è descritta in
[MQTT dalla versione 2.1](gateway/MQTT_V2_1.md): benefici attesi del canale di
messaggistica gateway–piattaforma, distinto dalla VPN. Anche questa funzionalità
è futura e non disponibile nella 0.9.0.
