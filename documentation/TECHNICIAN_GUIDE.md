# Guida tecnico

## Prima connessione

1. Aprire `https://<server>/app/`. Accedere con username/password e l'eventuale
   codice email, oppure con il provider SSO indicato dall'amministratore.
2. In **Il mio profilo → Il mio profilo VPN**, preparare e scaricare il proprio
   profilo, quindi confermarne l'attivazione. Importarlo nel client OpenVPN.
   Il download contiene una chiave privata: non condividerlo.
3. In **Le mie VPN**, scegliere il gateway. Se non compare, chiedere la verifica
   dei gruppi e del permesso di connessione. Aprire la scheda per LAN e inventario.
4. Premere **Apri VPN**, confermare e seguire lo stato. Per SMS l'accettazione
   del provider è distinta dalla conferma del gateway; per Wi-Fi il gateway
   acquisisce la richiesta al successivo controllo periodico.
5. Quando il gateway è pronto, avviare manualmente il profilo OpenVPN. Il portale
   attende entrambi i client e la configurazione della rete di assistenza.
6. Verificare la raggiungibilità del PLC con il software di assistenza appropriato.
   Il solo stato VPN non prova che il PLC risponda né registra le modifiche al PLC.
7. Al termine premere **Chiudi sessione**, confermare e attendere il rilascio.
   Disconnettere anche il client OpenVPN.

Ogni tecnico e gateway può occupare una sola sessione alla volta, compresa la
pulizia finale. La scadenza indicata nel dettaglio limita la durata dell'accesso.

## Quando il collegamento si ferma

Aprire la **Diagnostica del collegamento**. Mostra provider SMS, conferma gateway,
tunnel, client, rete e pulizia con il momento dell'osservazione. **Non verificabile**
significa che manca un dato recente: non conferma né connessione né disconnessione.

Nella scheda gateway, **Componenti e identificativi** riporta le versioni e,
quando rilevati, IMEI del modem o MAC Wi-Fi. Con il permesso `logs.read`, **Log
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

Il logout o la scadenza del login chiudono le VPN avviate da quell'accesso.
Chiudere soltanto la finestra non termina l'accesso. Per revocare tutti gli accessi
FactoryVPN usare **Disconnetti tutti gli accessi** nel profilo. Per sospetto furto
del profilo VPN, revocare il certificato e avvisare l'amministratore.

Vedere anche [risoluzione dei problemi](TROUBLESHOOTING.md).
