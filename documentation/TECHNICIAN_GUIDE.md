# Guida tecnico

## Prima connessione

1. Aprire `https://<server>/app/`. Accedere con username/password e l'eventuale
   codice email, oppure con il provider SSO indicato dall'amministratore.
2. In **Il mio profilo → Il mio profilo VPN**, preparare e scaricare il proprio
   profilo, quindi confermarne l'attivazione. Importarlo nel client OpenVPN.
   Il download contiene una chiave privata: non condividerlo.
3. In **Le mie VPN**, scegliere il gateway. Se non compare, chiedere la verifica
   dei gruppi e del permesso di connessione. Aprire la scheda per IP VPN e inventario.
4. Premere **Apri VPN**, confermare e seguire lo stato. Per SMS l'accettazione
   del provider è distinta dalla conferma del gateway; per Wi-Fi il gateway
   acquisisce la richiesta al successivo controllo periodico.
5. Quando il gateway è pronto, avviare manualmente il profilo OpenVPN. Il portale
   attende entrambi i client e la configurazione della rete di assistenza.
6. Collegarsi in SSH o web all’IP VPN fisso del gateway. Dalla sua shell verificare
   la raggiungibilità del PLC con gli strumenti disponibili sul dispositivo.
   Il solo stato VPN non prova che il PLC risponda né registra le modifiche al PLC.
7. Al termine premere **Chiudi sessione**, confermare e attendere il rilascio.
   Disconnettere anche il client OpenVPN.

Ogni tecnico e gateway può occupare una sola sessione alla volta, compresa la
pulizia finale. La scadenza indicata nel dettaglio limita la durata dell'accesso.

La VPN dura al massimo **3 ore dalla richiesta per impostazione predefinita**,
compresa l'attesa di connessione. L'amministratore può configurare una durata diversa.
Il login al portale dura **12 ore per impostazione predefinita**. La VPN termina
prima se scade il login che l'ha aperta o se si fa logout; con SSO possono valere
scadenze più brevi del provider. L'attività in SSH non prolunga questi tempi.
Prima di un intervento lungo controllare la scadenza indicata e il tempo residuo
del login: non c'è un rinnovo automatico della durata della VPN.

## Entrare in SSH sul gateway

FactoryVPN usa **Gateway Only**: la VPN autorizza soltanto l’IP VPN fisso del gateway
sulle porte TCP 22 (SSH), 80 (HTTP) e 443 (HTTPS).
Non cambia la rotta predefinita Internet, i DNS o la configurazione IPv6 del
computer del tecnico. La navigazione e le altre applicazioni continuano a usare
la connessione abituale. La LAN del cliente non viene instradata sul PC.

Dopo aver aperto la sessione e collegato OpenVPN, leggere **Indirizzo VPN fisso**
nella sua scheda. Se è `10.100.0.2`, dal terminale del proprio PC:

```bash
ssh nomeutente@10.100.0.2
```

Usare un account e una chiave/password del sistema Linux del gateway, forniti
dall'amministratore del dispositivo. FactoryVPN non crea account SSH: il servizio
deve essere abilitato come descritto nei [prerequisiti](DOCKER_GUIDE.md#ssh-sul-gateway).
Per il web aprire `http://10.100.0.2` o `https://10.100.0.2` (sostituire l’IP
con quello della scheda). Servizi e credenziali devono essere predisposti sul
gateway. Per HTTPS usare un certificato attendibile e coerente con il nome/IP
del servizio; la VPN non sostituisce la verifica del certificato web.
Il ping e le altre porte, comprese le porte web diverse da 80/443, non sono
consentiti attraverso la VPN. Per verificare l'accesso provare SSH o il servizio
web predisposto sul gateway.

Una volta dentro SSH, questi comandi vengono eseguiti **sul gateway**:

```bash
ip -br address
ip route
ping 10.0.0.5
ssh altro_utente@10.0.0.5
```

La shell vede le interfacce e le rotte correnti del gateway, comprese le reti
aggiunte durante l'intervento. Le connessioni avviate da lì usano la rete del
gateway e i permessi dell'account remoto. Non occorre instradare quelle reti dal
PC del tecnico. Tutti i dispositivi LAN si raggiungono dalla shell del gateway,
non direttamente attraverso la VPN dal PC. Il cambio di LAN non richiede
rigenerazione del pacchetto VPN; configurare le nuove interfacce/rotte sul gateway.

La chiusura o scadenza della VPN interrompe anche il traffico SSH, entro il lease
di rete. Non è una garanzia che un comando già avviato sul gateway venga terminato:
chiudere correttamente i programmi remoti prima di uscire. Il portale non contiene
un terminale SSH integrato.

Il pool VPN `10.100.0.0/16` deve restare distinto dalle reti locali dei peer.
LAN uguali presso clienti diversi non richiedono alcuna configurazione VPN speciale.

## Quando il collegamento si ferma

Aprire la **Diagnostica del collegamento**. Mostra provider SMS, conferma gateway,
tunnel, client, rete e pulizia con il momento dell'osservazione. **Non verificabile**
significa che manca un dato recente: non conferma né connessione né disconnessione.

Un errore temporaneo delle API non interrompe subito una VPN già autorizzata.
Il traffico può continuare fino alla scadenza del lease di rete già ricevuto,
**fino a 5 minuti dall'ultimo rinnovo riuscito per default**, senza aggiungere tempo
per ogni errore. Se il server torna disponibile in tempo, l'assistenza può proseguire.
Logout e revoca bloccano appena ricevuti; se le API non rispondono, il blocco può
attendere il lease residuo. Scadenze di VPN, login o SSO possono ridurlo.

Se manca la connettività fisica, il traffico si ferma comunque. OpenVPN attende
5 minuti senza pacchetti prima di tentare la riconnessione, per default: non è una
chiusura della sessione di assistenza. SSH e gli strumenti sul PLC possono avere
propri timeout più brevi. La ripresa della stessa connessione SSH non è garantita.
L'amministratore trova tutti i parametri nella
[configurazione delle durate](DOCKER_GUIDE.md#durate-timeout-e-reti-instabili).

Nella scheda gateway, **Componenti e identificativi** riporta le versioni e,
quando rilevati, IMEI del modem, ICCID della SIM o MAC Wi-Fi. Matricola, IMEI e
ICCID sono visibili anche nell'anagrafica e ricercabili in **Le mie VPN**:
[ricerca e significato degli identificativi](ADMINISTRATOR_GUIDE.md#gestire-gateway-e-credenziali).
Con il permesso `logs.read`, **Log
tecnici** mostra anche gli errori generici e quelli delle proprie sessioni, con
filtri e CSV. Controllare la data dell'ultimo rilevamento; lo storico può essere
parziale. [Uso e limiti](ADMINISTRATOR_GUIDE.md#componenti-e-log-del-gateway).

Con `telemetry.read`, nella stessa scheda trovi **Storico delle metriche** di CPU,
RAM e disco: scegli ultima ora, 24 ore, 7 o 30 giorni. Puoi esplorare i valori
con il cursore da tastiera o aprire la tabella. Un tratto vuoto indica campioni
assenti; per i gateway SMS può corrispondere all'attesa di attivazione.

Il reinvio SMS è disponibile solo nella sessione valida, dopo almeno 60 secondi
dall'ultimo tentativo. Se una richiesta di apertura perde la risposta, ripeterla
nella stessa finestra; dopo un ricaricamento controllare prima **Le tue sessioni**.
Comunicare all'assistenza gateway, UUID della sessione, orario e punto fermo della
diagnostica. Non inviare password, codici email o file `.ovpn`.

## Profilo e sicurezza

Il profilo consente aggiornamento anagrafico, cambio password locale, verifica
email e gestione del certificato personale. Per gli account senza obbligo
amministrativo è possibile attivare il codice email dal profilo. Le credenziali
SSO si gestiscono sul provider aziendale.

Scaricare il profilo nello stesso accesso che lo ha richiesto, entro il tempo
indicato. Dopo l'attivazione non è più scaricabile: se il file è perso, preparare
un sostituto. L'attivazione revoca il certificato precedente; vanno prima concluse
le VPN e la pulizia.
Il certificato personale dura 3650 giorni (circa dieci anni) per default; il suo
rinnovo dal portale resta distinto dal login e dalla durata di ogni VPN. La lunga
validità non evita la revoca quando il profilo viene perso o l'accesso viene tolto.

Il logout o la scadenza del login chiudono le VPN avviate da quell'accesso.
Chiudere soltanto la finestra non termina l'accesso. Per revocare tutti gli accessi
FactoryVPN usare **Disconnetti tutti gli accessi** nel profilo. Per sospetto furto
del profilo VPN, revocare il certificato e avvisare l'amministratore.

Vedere anche [risoluzione dei problemi](TROUBLESHOOTING.md).
