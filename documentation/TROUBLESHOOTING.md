# Risoluzione dei problemi

| Sintomo | Verifica e azione |
| --- | --- |
| Il codice email non arriva | Verificare la casella indicata, spam e scadenza. Attendere il tempo di reinvio. L'amministratore verifica relay, TLS e raggiungibilità SMTP dal container API. Per un guasto della posta usare l'[accesso di emergenza da console](#accesso-di-emergenza-durante-un-guasto-della-posta). |
| Codice errato o scaduto | Usare l'ultimo codice della richiesta ancora valida. Cinque tentativi esauriscono la richiesta; dieci errori cumulativi bloccano la verifica per 15 minuti. |
| Login SSO amministrativo rifiutato | Verificare associazione esplicita al `sub`, account attivo e claim MFA firmati accettati dalla policy. Il semplice successo sul provider non garantisce accesso FactoryVPN. |
| Gateway non visibile | Verificare gruppo assegnato e appartenenza dell'utente. I gateway senza gruppo sono visibili solo globalmente. |
| Gateway disponibile ma nessun collegamento | Disponibilità significa risorse libere/configurazione presente. Controllare conferma del gateway, tunnel e client nella diagnostica. |
| Gateway in attesa dopo SMS | Accettazione del provider non significa consegna. Verificare numero, SIM/modem, alimentazione e HTTPS del gateway. Reinvio dopo almeno 60 secondi. |
| Client del tecnico non collegato | Avviare OpenVPN manualmente con il profilo attivato e valido. Verificare endpoint raggiungibile e orologio del computer. |
| IP VPN non risponde al ping o a un servizio | Con OpenVPN collegato e sessione autorizzata, usare l'IP VPN indicato nella scheda. Gateway Only non filtra porte o protocolli IPv4: verificare che il servizio ascolti su quell'IP o su tutte le interfacce, la porta effettiva e i firewall locali. Le risposte ICMP possono essere disabilitate sul dispositivo. |
| Rete pronta ma PLC irraggiungibile | Accedere prima in SSH al gateway: la LAN non è esposta direttamente nella VPN. Dalla shell controllare indirizzo del PLC, LAN/interfaccia del gateway, cablaggio e configurazione del dispositivo. L'inventario non è un controllo di raggiungibilità. |
| Errore API durante una VPN | Controllare raggiungibilità HTTPS e servizi del server. Un guasto temporaneo conserva il lease esistente senza prolungarlo; senza rinnovi il firewall blocca il traffico entro la scadenza (massimo 5 minuti per default, eventualmente ridotti da login/SSO). Vedere [continuità del collegamento](TECHNICIAN_GUIDE.md#quando-il-collegamento-si-ferma). |
| VPN terminata durante un intervento lungo | Controllare `expires_at`, scadenza del login e limiti SSO: i default sono 3 ore per VPN e 12 per il portale, senza rinnovo con l'attività. Il logout chiude le VPN associate. Per i valori configurabili vedere [durate e timeout](DOCKER_GUIDE.md#durate-timeout-e-reti-instabili). |
| SSH al gateway non risponde | Usare il suo IP VPN fisso, con OpenVPN collegato e sessione autorizzata. Verificare servizio SSH, porta configurata (normalmente TCP 22), firewall locale e credenziali Linux del dispositivo; seguire [SSH sul gateway](TECHNICIAN_GUIDE.md#entrare-in-ssh-sul-gateway). |
| Un'altra rete cliente non è raggiungibile direttamente dal PC | Gateway Only espone i servizi del solo gateway, senza instradare la LAN. Entrare in SSH sul gateway e operare da lì usando le sue rotte correnti. Internet e DNS del tecnico restano locali; riservare `10.100.0.0/16` alla VPN. |
| La ricerca automatica non trova i dispositivi LAN | Gateway Only offre accesso IP al gateway, non un ponte Ethernet o un'estensione della LAN. La discovery broadcast non attraversa la VPN; usare l'IP VPN fisso per i servizi del gateway e operare da lì sui dispositivi locali. |
| Dati non verificabili/scaduti | Controllare momento dell'ultimo campione, connettività API/worker/agente e servizi in Telemetria. Per SMS inattivo l'assenza di aggiornamenti periodici è prevista. |
| IMEI o MAC Wi-Fi non rilevati | Controllare supporto modem e `GATEWAY_WIFI_INTERFACE`; un MAC registrato non dimostra la presenza della radio. Il fallimento della raccolta non impedisce la VPN. |
| Log tecnici assenti o incompleti | Verificare `logs.read`, gruppo e sessione selezionata. Attesa SMS, riavvii e limiti di conservazione possono lasciare lacune. Consultare [uso e limiti](ADMINISTRATOR_GUIDE.md#componenti-e-log-del-gateway). |
| API rifiuta lo schema all'avvio | Per una nuova installazione usare un database dedicato vuoto, inizializzato con il DDL completo della versione corrente. Verificare che `DATABASE_URL` punti al database corretto; non cancellare un database non identificato. Vedere [primo avvio](DOCKER_GUIDE.md#avvio-e-primo-amministratore). |
| Versione server assente o stato non verificabile | Aprire **Stato server** come amministratore globale e controllare le date. Worker e provisioning devono pubblicare osservazioni recenti; una versione nota non dimostra operatività. |
| Certificato assente, non leggibile o con vecchia scadenza | Controllare i PEM pubblici e i permessi dell'API; dopo sostituzione atomica ricreare il container API seguendo la [procedura certificati](DOCKER_GUIDE.md#certificati-nello-stato-server). Non montare chiavi private nell'API. |
| Nuove VPN rifiutate per CRL | Controllare completezza e scadenze in Stato server. Le CRL online si rigenerano con `provisioning-worker`; la CRL root richiede [firma e importazione offline](DOCKER_GUIDE.md#rinnovare-la-crl-root). Non rimuovere `crl-verify` o cancellare le revoche. |
| Sub-CA valida ma emissione impossibile | La catena deve coprire tutta la durata del nuovo client più un giorno. Sostituire la Sub-CA prima del termine di emissione mostrato nel portale. |
| Rinnovo remoto in attesa | Verificare HTTPS indipendente dalla VPN, agente compatibile, spazio libero e orologio; terminare sessioni e pulizie pendenti. In SMS una richiesta anticipata attende il prossimo risveglio. Conservare journal e chiave locale, poi riavviare l'agente per il retry. |
| Certificato gateway scaduto | Il rinnovo HTTPS può funzionare ancora con API key e vecchia chiave privata valide. Revoche, cambio gruppo o perdita della chiave richiedono nuovo provisioning e non vengono aggirati dal rinnovo. |
| Ultima prova backup non riuscita | Aprire **Backup e ripristino**, verificare l'operazione e ripetere la prova dopo aver risolto la causa. Un precedente successo non annulla il nuovo fallimento; seguire la [procedura di recupero](BACKUP_RESTORE.md). |
| Grafico vuoto o interrotto | Provare un altro periodo e controllare l'ultimo campione. Lo storico parte dalla ricezione dei dati, non ricostruisce il passato. Un gateway SMS in attesa può non inviare misure; controllare anche il permesso `telemetry.read` e i gruppi assegnati. |
| Sessione chiusa ma gateway occupato | Attendere la conferma del worker. Se persiste, verificare servizio OpenVPN, pulizia e diagnostica; non cancellare righe del database per liberare risorse. |
| Salvataggio rifiutato per dati cambiati | Ricaricare la scheda, confrontare i valori e ripetere. Un'altra operazione ha aggiornato la revisione. |
| Download profilo non disponibile | Controllare scadenza e medesimo accesso/browser. Dopo attivazione o revoca occorre una nuova emissione. |
| CSV troppo grande | Restringere date, gateway o sessione. Il massimo è 10.000 righe autorizzate. |
| Backup/coordinatore indisponibile | L'amministratore del server verifica processo, catalogo e spazio disco; seguire la guida di recupero senza rimuovere il blocco di manutenzione a mano. |
| Portale in errore dopo la ricreazione dell'API | Controllare prima che l'API sia healthy, poi ricreare Nginx perché risolva il nuovo indirizzo dell'API. Seguire la [manutenzione del server](DOCKER_GUIDE.md#applicare-modifiche-al-server). |
| Nuovo `.env` apparentemente ignorato | `restart` non rilegge l'ambiente: applicare la [ricreazione coordinata](DOCKER_GUIDE.md#applicare-modifiche-al-server). Non ricreare soltanto OpenVPN lasciando il worker collegato al vecchio namespace. |

Per un intervento raccogliere UUID di gateway/sessione, orari con fuso, passaggio
che fallisce e CSV filtrato della cronologia. L'audit amministrativo è separato
dagli eventi VPN. I log grezzi dei servizi restano sul server e possono contenere
dati sensibili: condividerne soltanto le parti necessarie dopo averle controllate.

## Accesso di emergenza durante un guasto della posta

L'amministratore del server può emettere un codice per un account locale attivo,
compreso quello di un tecnico. L'operatore deve avere accesso amministrativo alla
console o SSH del server; il destinatario usa la propria password insieme al codice.
La generazione è disponibile soltanto da console, senza dipendere dal relay SMTP.

Dalla directory dell'installazione, con l'account che gestisce Docker:

```sh
docker compose exec api python -m scripts.issue_console_code tecnico \
  --operator operatore --reason "Posta indisponibile durante assistenza"
```

Sostituire `tecnico` con l'username dell'account e `operatore` con l'identità di chi
emette il codice. Dopo avere verificato l'identità del destinatario, confermare
con `EMETTI` e consegnare il codice mostrato attraverso un canale fidato disponibile.
Nel login del portale il destinatario sceglie **Usa codice di emergenza** e inserisce
username, password e codice.

Se è già nella schermata del codice email, può premere **Non ricevi l’email? Usa
un codice di emergenza**. Il login conserva l'username e seleziona il percorso
di emergenza: occorre reinserire password e codice da console. Il pulsante è
disponibile subito e anche dopo la scadenza del codice email, indipendentemente
dall'esito SMTP: il relay può accettare un messaggio senza che arrivi alla casella.

Il codice consente un solo uso entro **10 minuti**, con al massimo **5 tentativi**.
Una nuova emissione sostituisce il precedente codice ancora pendente per lo
stesso account e scopo. Scadenza del codice e durata dell'accesso sono distinte:
dopo il login valgono le durate ordinarie configurate del portale e della VPN
(predefinite: 12 ore e fino a 3 ore). Permessi, gruppi, revoche e lease restano
quelli dell'account. L'emissione non chiude gli accessi o le VPN già attivi.
I login successivi richiedono nuovamente il metodo ordinario oppure un nuovo
codice emesso dalla console.

Per riconfermare un'operazione sensibile durante il guasto, emettere un nuovo
codice destinato alla conferma:

```sh
docker compose exec api python -m scripts.issue_console_code admin \
  --operator operatore --reason "Riconferma durante guasto posta" --purpose confirm
```

Nella richiesta di riconferma del portale scegliere il codice di emergenza e
inserire password e nuovo codice. La conferma mantiene il browser corrente e
la scadenza dell'accesso, senza chiudere la VPN. Resta valida per i normali
cinque minuti delle operazioni sensibili. Un codice per il login non può essere
usato per la conferma e viceversa.

L'audit registra l'emissione con account, operatore e motivazione, insieme agli
eventi di uso ed errore; il codice non compare nell'audit e nel database ne è conservato
soltanto l'hash. L'uso non attesta la proprietà della casella email e non ne cambia
lo stato di verifica. Gli account SSO continuano a usare il recupero del provider.
Il ripristino di un backup elimina i codici di emergenza pendenti.

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
