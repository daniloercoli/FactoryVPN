# Risoluzione dei problemi

| Sintomo | Verifica e azione |
| --- | --- |
| Il codice email non arriva | Verificare la casella indicata, spam e scadenza. Attendere il tempo di reinvio. L'amministratore verifica relay, TLS e raggiungibilità SMTP dal container API. Non disattivare la verifica per aggirare il problema. |
| Codice errato o scaduto | Usare l'ultimo codice della richiesta ancora valida. Cinque tentativi esauriscono la richiesta; dieci errori cumulativi bloccano la verifica per 15 minuti. |
| Login SSO amministrativo rifiutato | Verificare associazione esplicita al `sub`, account attivo e claim MFA firmati accettati dalla policy. Il semplice successo sul provider non garantisce accesso FactoryVPN. |
| Gateway non visibile | Verificare gruppo assegnato e appartenenza dell'utente. I gateway senza gruppo sono visibili solo globalmente. |
| Gateway disponibile ma nessun collegamento | Disponibilità significa risorse libere/configurazione presente. Controllare conferma del gateway, tunnel e client nella diagnostica. |
| Gateway in attesa dopo SMS | Accettazione del provider non significa consegna. Verificare numero, SIM/modem, alimentazione e HTTPS del gateway. Reinvio dopo almeno 60 secondi. |
| Client del tecnico non collegato | Avviare OpenVPN manualmente con il profilo attivato e valido. Verificare endpoint raggiungibile e orologio del computer. |
| Rete pronta ma PLC irraggiungibile | Controllare indirizzo del PLC, LAN/interfaccia del gateway, cablaggio e configurazione del dispositivo. L'inventario non è un controllo di raggiungibilità. |
| Dati non verificabili/scaduti | Controllare momento dell'ultimo campione, connettività API/worker/agente e servizi in Telemetria. Per SMS inattivo l'assenza di aggiornamenti periodici è prevista. |
| IMEI o MAC Wi-Fi non rilevati | Controllare supporto modem e `GATEWAY_WIFI_INTERFACE`; un MAC registrato non dimostra la presenza della radio. Il fallimento della raccolta non impedisce la VPN. |
| Log tecnici assenti o incompleti | Verificare `logs.read`, gruppo e sessione selezionata. Attesa SMS, riavvii e limiti di conservazione possono lasciare lacune. Consultare [uso e limiti](ADMINISTRATOR_GUIDE.md#componenti-e-log-del-gateway). |
| API rifiuta lo schema all'avvio | Per una nuova installazione usare un database dedicato vuoto, inizializzato con il DDL completo della versione corrente. Verificare che `DATABASE_URL` punti al database corretto; non cancellare un database non identificato. Vedere [primo avvio](DOCKER_GUIDE.md#avvio-e-primo-amministratore). |
| Versione server assente o stato non verificabile | Aprire **Stato server** come amministratore globale e controllare le date. Worker e provisioning devono pubblicare osservazioni recenti; una versione nota non dimostra operatività. |
| Certificato assente, non leggibile o con vecchia scadenza | Controllare i PEM pubblici e i permessi dell'API; dopo sostituzione atomica ricreare il container API seguendo la [procedura certificati](DOCKER_GUIDE.md#certificati-nello-stato-server). Non montare chiavi private nell'API. |
| Ultima prova backup non riuscita | Aprire **Backup e ripristino**, verificare l'operazione e ripetere la prova dopo aver risolto la causa. Un precedente successo non annulla il nuovo fallimento; seguire la [procedura di recupero](BACKUP_RESTORE.md). |
| Grafico vuoto o interrotto | Provare un altro periodo e controllare l'ultimo campione. Lo storico parte dalla ricezione dei dati, non ricostruisce il passato. Un gateway SMS in attesa può non inviare misure; controllare anche il permesso `telemetry.read` e i gruppi assegnati. |
| Sessione chiusa ma gateway occupato | Attendere la conferma del worker. Se persiste, verificare servizio OpenVPN, pulizia e diagnostica; non cancellare righe del database per liberare risorse. |
| Salvataggio rifiutato per dati cambiati | Ricaricare la scheda, confrontare i valori e ripetere. Un'altra operazione ha aggiornato la revisione. |
| Download profilo non disponibile | Controllare scadenza e medesimo accesso/browser. Dopo attivazione o revoca occorre una nuova emissione. |
| CSV troppo grande | Restringere date, gateway o sessione. Il massimo è 10.000 righe autorizzate. |
| Backup/coordinatore indisponibile | L'amministratore del server verifica processo, catalogo e spazio disco; seguire la guida di recupero senza rimuovere il blocco di manutenzione a mano. |

Per un intervento raccogliere UUID di gateway/sessione, orari con fuso, passaggio
che fallisce e CSV filtrato della cronologia. L'audit amministrativo è separato
dagli eventi VPN. I log grezzi dei servizi restano sul server e possono contenere
dati sensibili: condividerne soltanto le parti necessarie dopo averle controllate.

## Recuperare l'accesso email

Un altro amministratore globale può usare il
[recupero dalla scheda utente](ADMINISTRATOR_GUIDE.md#uscita-e-recupero).
Per il proprio account o quando nessun amministratore può accedere, l'amministratore
del server esegue dalla directory dell'installazione:

```sh
docker compose exec api python -m scripts.recover_email admin --email nuova@azienda.example
```

Sostituire account ed email con quelli effettivi. La console chiede conferma;
revoca accessi e VPN e mantiene la password locale, da usare insieme al codice
sulla nuova casella. Non disattiva la verifica email e non genera codici permanenti.
L'operazione è registrata nell'audit. Configurare e verificare il
[relay SMTP](DOCKER_GUIDE.md#server-smtp-locale) prima del nuovo accesso.

Se il problema è la perdita o il guasto del server, seguire invece il
[recupero dei backup](BACKUP_RESTORE.md#recupero-da-console).
