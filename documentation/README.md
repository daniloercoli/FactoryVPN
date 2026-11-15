# Guide FactoryVPN

Questa cartella contiene le istruzioni operative per chi installa, amministra e
usa FactoryVPN e le guide di integrazione per gli sviluppatori dei gateway.

| Cosa devi fare | Guida |
| --- | --- |
| Installare server e gateway, configurare HTTPS e SMTP | [Installazione](DOCKER_GUIDE.md) |
| Rinnovare CA, server, CRL e certificati gateway | [Manutenzione PKI](DOCKER_GUIDE.md#rotazione-e-manutenzione) |
| Configurare durata VPN, login e tolleranza alle interruzioni | [Durate e timeout nel .env](DOCKER_GUIDE.md#durate-timeout-e-reti-instabili) |
| Gestire utenti, gruppi, gateway e sessioni | [Amministratore](ADMINISTRATOR_GUIDE.md) |
| Collegarti a un impianto e chiudere l'intervento | [Tecnico](TECHNICIAN_GUIDE.md) |
| Usare SSH e web sull’IP VPN fisso del gateway | [Gateway Only](TECHNICIAN_GUIDE.md#entrare-in-ssh-sul-gateway) |
| Creare copie e recuperare il servizio | [Backup e ripristino](BACKUP_RESTORE.md) |
| Risolvere un problema o recuperare l'accesso | [Risoluzione dei problemi](TROUBLESHOOTING.md) |
| Integrare il primo avvio del gateway e la registrazione HTTPS | [Prima inizializzazione gateway](gateway/INITIAL_SETUP.md) |
| Registrare a fine linea e completare l'arruolamento via HTTPS | [Arruolamento gateway](gateway/FACTORY_ENROLLMENT.md) |
| Implementare i rinnovi successivi dei certificati sul gateway | [Rinnovo remoto gateway](gateway/CERTIFICATE_RENEWAL.md) |

Per integrare applicazioni con le API utente e amministratore sono disponibili la
[guida per sviluppatori](../website/dist/developers.html) e il
[contratto OpenAPI](../website/dist/api/openapi.json).
Le API macchina per registrazione e rinnovo sono descritte nelle guide sopra,
separatamente dal contratto OpenAPI dei client del portale.
