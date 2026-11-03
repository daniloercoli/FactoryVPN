# Guida amministratore

Accedere a `/app/` con le credenziali assegnate. Il login locale amministrativo
richiede password e codice email; per SSO usare il pulsante del provider
configurato. Il provider deve attestare MFA per gli amministratori.

## Preparare l'accesso all'assistenza

1. In **Gruppi**, creare un gruppo per ogni ambito di risorse da separare.
2. In **Utenti**, creare gli account e assegnare gruppi e permessi. Un utente può
   appartenere a più gruppi. Controllare l'email: serve per il codice di accesso.
3. In **Gateway → Aggiungi gateway** (oppure **Le mie VPN**), indicare gruppo,
   modalità SMS/Wi-Fi e rete LAN. Preparare il pacchetto, scaricarlo e attivarlo.
   Far installare le credenziali sul gateway e verificarne la registrazione.
4. Nella scheda gateway, compilare l'inventario PLC/HMI e controllare la rete.
   L'inventario descrive i dispositivi: non ne certifica la raggiungibilità.
5. Ogni tecnico prepara, scarica e attiva il proprio profilo VPN da **Il mio
   profilo**. L'amministratore non scarica le chiavi private degli altri utenti.
6. Eseguire un intervento di prova e verificarne chiusura e rilascio delle risorse.

Per account SSO un amministratore globale deve collegare esplicitamente provider
e subject (`sub`) nella scheda dell'utente. La corrispondenza dell'email non crea
automaticamente il collegamento. Vedere [configurazione SSO](DOCKER_GUIDE.md#sso-opzionale).

## Assegnare i permessi

Gli amministratori globali hanno tutte le capacità. I delegati ricevono i soli
permessi necessari; questi valgono per tutti i gruppi loro assegnati.

| Permesso | Capacità |
| --- | --- |
| `connect` | Aprire VPN sui gateway dei propri gruppi |
| `users.manage` | Gestire utenti interamente nel proprio ambito |
| `groups.manage` | Gestire i gruppi assegnati |
| `gateways.manage` | Gestire anagrafica, assegnazione e inventario |
| `gateways.provision` | Preparare, attivare e revocare credenziali gateway |
| `sessions.manage` | Supervisionare e chiudere le sessioni degli altri tecnici |
| `audit.read` | Consultare ed esportare audit e statistiche del proprio ambito |
| `telemetry.read` | Leggere la telemetria dei gateway assegnati |
| `logs.read` | Consultare ed esportare log tecnici; per sessioni altrui serve anche `sessions.manage` |

Un delegato non può concedere capacità che non possiede. Un utente presente anche
in gruppi esterni alla delega richiede un amministratore con ambito più ampio.
Backup, telemetria server, identità SSO, recupero email e certificati di altri
utenti sono riservati agli amministratori globali. L'ultimo amministratore globale
locale attivo è protetto da rimozione, disabilitazione e conversione a SSO.

## Supervisionare i client

La **Panoramica** mostra account abilitati, gateway e, con `sessions.manage`,
richieste in attesa, sessioni attive e risorse in pulizia. I conteggi operativi
portano alla relativa vista **Client VPN**.

**Client VPN** mostra per default le sessioni in corso e quelle con risorse ancora
riservate. Cercare tecnico, nome/MAC del gateway o UUID della sessione; filtrare
gruppo e stato. Selezionare **Tutte le sessioni** per consultare quelle terminate.
I filtri rimangono nell'URL e l'elenco si aggiorna ogni cinque secondi.

Ogni riga distingue:

- **Tecnico e gateway:** nomi visibili nel proprio ambito e IP assegnati alla
  sessione. I nomi degli account condivisi con gruppi esterni sono mascherati;
  la sessione del gruppo autorizzato rimane supervisionabile.
- **Client / gateway / rete:** presenza osservata sul server e configurazione
  della rete di assistenza. Dopo 30 secondi senza evidenza recente lo stato è
  **Non verificabile**. Un errore di lettura nasconde la lista precedente.
- **Sessione:** stato amministrativo registrato, distinto dal collegamento.
  **Attiva** non prova che il client sia ancora connesso in questo momento.
- **Durata:** tempo dalla richiesta, comprensivo dell'attesa, e scadenza prevista.

**Diagnostica** apre i dettagli e la cronologia dell'intervento. **Chiudi sessione**
richiede conferma e registra l'operazione; attendere il rilascio delle risorse.
La chiusura non disabilita l'account e non revoca il suo certificato.

La vista riguarda le sessioni di assistenza, non i computer su cui è installato
OpenVPN. Un utente autenticato nel portale può non avere una VPN aperta.

## Gestire gateway e credenziali

In **Gateway** cercare nome, MAC, telefono o UUID e filtrare gruppo, modalità e
disponibilità. **Disponibile** significa libero e configurato, non necessariamente
online. Un gateway SMS inattivo non invia heartbeat periodici.

La scheda raccoglie rete, inventario, sessione corrente, telemetria e provisioning.
Cambiare gruppo richiede VPN e pulizia concluse; revoca le credenziali esistenti.
Preparare e installare un nuovo pacchetto nel gruppo di destinazione.
Il cambio del solo nome non interrompe le connessioni.

Disabilitare/eliminare un utente revoca accessi e certificati personali. Riabilitarlo
richiede un nuovo profilo VPN. Per un incidente su un certificato usare **Revoca**
nella relativa scheda e verificare la chiusura e il rilascio delle risorse.
Per un rinnovo, preparare e scaricare il sostituto dallo stesso accesso; chiudere
le VPN e attendere la pulizia prima di attivarlo. L'attivazione revoca il profilo
precedente. I download scadono e non sono più disponibili dopo l'attivazione:
se il file è perso, prepararne uno nuovo.

## Componenti e log del gateway

Nella scheda, **Componenti e identificativi** mostra versioni dell'agente e di
OpenVPN, sistema operativo e dati hardware disponibili. Per i gateway SIM l'IMEI
identifica il modem; per quelli Wi-Fi viene mostrato il MAC dell'interfaccia
verificata. **MAC registrato** conserva il dato del provisioning, il cui ruolo
può essere diverso. Per SMS il MAC è facoltativo; per Wi-Fi resta obbligatorio.

Controllare sempre le date: un errore di raccolta conserva l'ultimo valore riuscito.
Le versioni non attestano presenza online né disponibilità di aggiornamenti.
**Non ancora rilevato** può indicare un agente precedente o un report non ricevuto.

**Log tecnici** richiede `logs.read`, da assegnare esplicitamente agli utenti che
ne hanno bisogno. Si possono filtrare ricezione, gravità, origine, componente,
codice e sessione, sospendere l'aggiornamento ed esportare il risultato in CSV.
Senza `sessions.manage` sono visibili i soli eventi generici e quelli delle proprie
sessioni. Il trasferimento a un altro gruppo non trasferisce i log precedenti.

Lo storico è limitato a 30 giorni e a un volume massimo; può essere incompleto.
Un gateway in attesa SMS invia gli eventi accodati alla successiva attivazione;
un riavvio può perderli. Aggiornare la scheda non risveglia il gateway.
Per la configurazione della raccolta vedere
[Agente Linux](DOCKER_GUIDE.md#agente-linux).

## Cronologia, telemetria e copie di sicurezza

**Cronologia VPN** filtra gateway, sessione, gruppo e date ed esporta gli eventi
autorizzati. **Registro attività** conserva le operazioni amministrative.
I CSV sono limitati a 10.000 righe: restringere i filtri se il limite è superato.
La cronologia non registra le modifiche effettuate sui programmi PLC.

**Telemetria** mostra l'ultimo campione con freschezza e disponibilità dei servizi.
La scheda gateway aggiunge **Storico delle metriche**, accessibile con
`telemetry.read`: CPU, RAM e disco per ultima ora, 24 ore, 7 o 30 giorni. Le curve
mostrano media e picco dei campioni ricevuti; il cursore da tastiera e **Valori in
tabella** permettono di leggere i valori. Un intervallo vuoto non equivale a zero:
per i gateway SMS può corrispondere a un periodo di attesa. Lo storico parte dalle
misure ricevute e viene conservato fino a 30 giorni, entro i limiti di volume.
Gli aggiornamenti della pagina non risvegliano il gateway. Non ci sono allarmi automatici.

## Stato server e verifica SMTP

**Stato server**, nel menu dell'amministratore globale, mostra versioni e stato
dei componenti centrali, ultima rilevazione, telemetria corrente e grafici storici
del server. **Non disponibile** indica una versione non rilevata; **Non verificabile**
indica uno stato assente o scaduto. In Docker le misure riguardano l'ambiente Linux
visibile al worker e il suo filesystem, non necessariamente i volumi del database.

**Certificati configurati** mostra le scadenze di HTTPS, server OpenVPN e CA VPN,
con avviso entro 30 giorni. Aprire i dettagli per data di inizio validità e
impronta SHA-256. **Da aggiornare** significa che l'osservazione è scaduta.
Si controllano i file pubblici dell'installazione: il dato non garantisce che il
browser consideri attendibile il certificato o che il servizio lo abbia caricato.
Seguire la [procedura certificati](DOCKER_GUIDE.md#certificati-nello-stato-server)
per configurazione e aggiornamento dopo un rinnovo.

**Backup e recupero** riassume il servizio, le copie a catalogo, l'ultima copia e
l'ultima prova di ripristino, compresi i fallimenti successivi a un successo.
Un vecchio test riuscito non prova la copia più recente: viene segnalato quando
questa non ha una prova riuscita registrata. **Gestisci backup** apre la pagina
dedicata, mantenendo i suoi requisiti di autenticazione per le operazioni.
Il riepilogo non apre gli archivi e non conferma che esista una copia esterna.

Nella sezione SMTP controllare host, porta, sicurezza e mittente. **Verifica
connessione** verifica anche TLS e autenticazione configurati senza spedire email.
**Invia email di prova** chiede conferma e usa solo l'indirizzo verificato del tuo
account. Il successo indica che il server SMTP ha accettato il messaggio: controlla
la casella, compreso lo spam, per confermarne la consegna. Se scade il tempo di
attesa, l'esito può essere incerto: verifica la casella prima di ripetere.

Le prove sono limitate a una al minuto e sei all'ora per l'intera installazione;
la pagina mostra l'attesa e il registro attività conserva richiesta ed esito.
Seguire la [configurazione SMTP](DOCKER_GUIDE.md#server-smtp-locale) per correggere
i parametri. Per accesso e limiti dello storico vedere
[Cronologia e telemetria](#cronologia-telemetria-e-copie-di-sicurezza).

## Copie di sicurezza

**Backup e ripristino** richiede un amministratore globale e una conferma recente
dell'identità. Eseguire copie regolari, custodirle fuori dal server insieme a una
chiave di recupero conservata separatamente e provare il ripristino isolato.
Seguire la [procedura completa](BACKUP_RESTORE.md), soprattutto prima di un recupero.

## Uscita e recupero

**Esci dal portale** chiude le VPN avviate da quell'accesso; gli altri accessi
restano indipendenti. **Disconnetti tutti gli accessi**, nel profilo, revoca gli
accessi FactoryVPN dell'account. Chiudere la scheda del browser non esegue logout.

Per una casella email non più raggiungibile, verificare la persona attraverso
un canale fidato. Un altro amministratore globale, dopo aver riconfermato la
propria identità, può usare **Recupero email dell'account** nella scheda utente.
La procedura revoca gli accessi e le VPN e mantiene obbligatoria la verifica della
nuova casella. Non è disponibile ai delegati né per recuperare il proprio account
dallo stesso accesso.

Se nessun amministratore può accedere, seguire il
[recupero da console](TROUBLESHOOTING.md#recuperare-laccesso-email). Non è presente
un reset password pubblico tramite link email. Per account SSO rivolgersi
all'amministratore del provider.
