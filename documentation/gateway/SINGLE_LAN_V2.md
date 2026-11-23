# FactoryVPN — Single LAN dalla versione 2.0

**Stato del prodotto:** la versione corrente è **0.9.0**; stiamo completando
la **1.0**, basata su **Gateway Only**. La funzionalità **Single LAN** è prevista
**a partire dalla versione 2.0**; **la versione 2.0 non è attualmente in fase di progettazione**.
Questo documento descrive il comportamento atteso e i punti di integrazione
per gli sviluppatori. Architettura definitiva, contratti API e data di rilascio
saranno definiti quando inizierà la progettazione.

## Che cosa significa implementare Single LAN

In FactoryVPN, **Single LAN significa consentire al PC del tecnico di raggiungere
direttamente gli indirizzi IPv4 della singola LAN associata al gateway scelto**,
per la durata di una sessione autorizzata. Il gateway diventa anche il punto di
inoltro verso PLC, HMI e altri dispositivi della sottorete del sito.

Per esempio, con una LAN `192.168.1.0/24` e un PLC `192.168.1.50`, il software di
programmazione installato sul PC potrebbe collegarsi direttamente a quell'IP.
Il tecnico non dovrebbe eseguire il software nella shell del gateway né usare
il gateway come postazione intermedia per ogni operazione.

La modalità **Gateway Only corrente**, invece, autorizza il traffico IPv4 fra
il PC e **il solo IP VPN fisso del gateway**, per esempio `10.100.0.2`.
Non applica filtri VPN di porta o protocollo alla coppia autorizzata: sono
ammessi TCP, UDP e ICMP in entrambe le direzioni, nei limiti dei servizi e dei
firewall locali. L'inoltro tra VPN e LAN è bloccato. Il tecnico può entrare in
SSH sul gateway e usare da lì le interfacce e le rotte locali, ma il PC non
riceve una rotta verso la LAN del cliente.

La differenza riguarda quindi **le destinazioni raggiungibili dal PC**.
Implementare Single LAN richiede instradamento, isolamento e autorizzazione
del traffico verso il sito; non basta aprire altre porte sul gateway.

Single LAN indica qui una modalità **Layer 3 IPv4**. Non estende al PC il
dominio Ethernet del sito, ARP, DHCP o discovery broadcast. Un protocollo
industriale basato su connessioni IP unicast può essere compatibile; l'uso di
UDP non implica che la relativa discovery broadcast o multicast attraversi
la VPN. Layer 2, IPv6 e collegamenti permanenti fra più siti richiederebbero
specifiche distinte.

## Differenze rispetto a Gateway Only

| Aspetto | Gateway Only, versione 0.9.0 verso 1.0 | Single LAN, comportamento atteso dalla versione 2.0 |
| --- | --- | --- |
| Destinazioni dal PC | IP VPN fisso del gateway | Gateway e dispositivi nella LAN autorizzata del sito |
| Accesso al PLC | Dalla shell del gateway, con gli strumenti disponibili sul dispositivo | Dal software sul PC, usando l'IP del PLC |
| Rotta distribuita al tecnico | Host `/32` del gateway | Sottorete LAN del sito selezionato |
| Forwarding sul gateway | Tra VPN e LAN bloccato | Consentito soltanto per la sessione e la LAN autorizzate |
| Configurazione LAN in FactoryVPN | Non richiesta | Necessaria per identificare la rete da raggiungere e l'uscita del gateway |
| Cambio della LAN cliente | Nessuna rigenerazione del pacchetto VPN | Riallineamento fra configurazione locale e configurazione autorizzata dal server |
| Siti con indirizzi LAN uguali | La LAN non viene instradata | Separazione delle destinazioni in base alla sessione |
| Conflitto con la rete locale del PC | Nessuna rotta verso la LAN remota | Rilevamento e gestione espliciti della sovrapposizione |

Il percorso continuerà a passare dal server VPN centrale. Il comportamento
atteso è uno split tunnel verso il sito autorizzato: Internet e DNS del tecnico
restano sulla sua connessione abituale.

## Riferimento di partenza

Una prima implementazione di Single LAN si trova nel branch
**`codex/single-lan-reference-2026-11-15`**, che conserva la base da cui è stato
avviato lo sviluppo della modalità Gateway Only. È un riferimento tecnico
da riesaminare, non una specifica già approvata per la versione 2.0.

## Percorso del traffico e responsabilità

Il percorso funzionale atteso è:

```text
Software sul PC -> VPN -> server -> gateway selezionato -> dispositivo LAN
```

Il PC dovrà ricevere la rotta per la sola sottorete autorizzata. Il server dovrà
associare il traffico all'identità del tecnico e alla sessione attiva, quindi
inoltrarlo al gateway corretto. Il gateway dovrà verificare l'autorizzazione e
instradare i pacchetti verso la propria interfaccia LAN. Anche il percorso di
ritorno dovrà essere gestito senza consentire accessi ad altre sessioni o reti.

La scelta dei meccanismi di isolamento e dell'eventuale incapsulamento resta
aperta. Non è sufficiente inserire una rotta globale sul server: due sessioni
possono avere la stessa sottorete di destinazione e devono rimanere separate.
La soluzione andrà valutata anche rispetto ai privilegi richiesti al worker,
al supporto del firmware e al costo in risorse.

### Risposte dai dispositivi e possibile NAT

Il progetto dovrà stabilire come i dispositivi LAN restituiscono il traffico
al tecnico. Una soluzione da valutare è il NAT sul gateway, per evitare di
richiedere rotte VPN sui PLC o sul router del cliente. In quel caso il
dispositivo vedrebbe come sorgente l'indirizzo LAN del gateway, con conseguenze
per log, regole locali e protocolli che includono indirizzi nei payload.

Dovranno essere definite anche le direzioni delle connessioni: l'accesso
iniziato dal tecnico con relative risposte non equivale alla possibilità per
un PLC di aprire una nuova connessione verso il PC. I protocolli con callback
o canali separati richiederanno requisiti e prove espliciti. La bidirezionalità
di Gateway Only non determina automaticamente la politica della futura Single LAN.

## Configurazione LAN e contratti da definire

L'integrazione dovrà rappresentare almeno queste informazioni, con nomi di
campi e formati da stabilire durante la progettazione:

| Informazione | Utilizzo |
| --- | --- |
| Sottorete IPv4 del sito | Delimitare le destinazioni autorizzate e la rotta del tecnico |
| Indirizzo LAN del gateway | Verificare coerenza e raggiungibilità del gateway nella rete del sito |
| Interfaccia o percorso di uscita LAN | Applicare forwarding e, se previsto, NAT sull'uscita corretta |
| Configurazione associata alla sessione | Evitare che una variazione successiva cambi implicitamente le destinazioni autorizzate |
| Capacità del firmware | Rifiutare configurazioni non supportate dal gateway prima di abilitare traffico |

I dati dichiarati al server dovranno essere coerenti con interfacce, indirizzi
e rotte reali. La configurazione della rete del sistema operativo dovrà avere
una responsabilità esplicita: distribuire un profilo VPN non configura da solo
la LAN, il PLC o la connettività Internet del dispositivo.

I contratti dovranno rifiutare reti che si sovrappongono ai pool riservati alla
VPN o al transito, dati incoerenti e richieste che ampliano l'accesso oltre
la LAN approvata. Il client del tecnico non dovrà poter scegliere liberamente
un'altra rete di destinazione. Una rotta predefinita `0.0.0.0/0` non rappresenta
l'autorizzazione alla singola LAN.

### Cambi della LAN in campo

Dovrà esistere una procedura completa per modificare la rete del sito: permessi
di modifica, configurazione del gateway, aggiornamento dei dati server e
conferma dell'allineamento. Andrà definito anche il comportamento delle sessioni
già aperte. Nessun cambio dovrà abilitare nuove destinazioni prima della
corrispondente autorizzazione.

I dati LAN dovranno essere acquisiti in una fase definita del provisioning
manuale o dell'arruolamento automatico. Il momento e la fonte autorevole di
questi dati restano da scegliere, soprattutto quando la rete del cliente è
nota soltanto dopo l'installazione sul campo.

## Isolamento e sovrapposizioni

**LAN uguali in siti diversi.** Due gateway possono servire entrambi
`192.168.1.0/24`, con un PLC `192.168.1.50` per sito. Due tecnici collegati
contemporaneamente dovranno raggiungere ciascuno il proprio PLC. Le decisioni
di routing e firewall dovranno usare il contesto della sessione, impedendo
traffico incrociato anche quando il client forza rotte o indirizzi sorgente.

**LAN del tecnico uguale alla LAN remota.** Il server non può correggere da
solo la scelta di rotta già fatta dal PC. La progettazione dovrà definire come
rilevare il conflitto, segnalarlo e risolverlo con una modalità supportata.
Eventuali alias o traduzioni di indirizzi richiederanno una specifica propria;
non si può assumere raggiungibilità trasparente con qualunque rete locale.

**Altre reti conosciute dal gateway.** L'autorizzazione riguarderà la LAN
selezionata, non tutte le rotte presenti sul dispositivo. Un gateway con più
interfacce non dovrà diventare un punto di accesso indiscriminato alle reti
adiacenti. I servizi locali del gateway e il forwarding verso i dispositivi
richiederanno controlli coerenti, pur attraversando percorsi firewall diversi.

## Sessioni, lease e chiusura dell'accesso

La funzionalità dovrà integrarsi con identità, gruppi, permessi, attivazione
SMS/Wi-Fi, PKI e rinnovo dei certificati della versione aggiornata.
**Un certificato valido autentica il peer, ma da solo non autorizza una LAN.**
Il modello di partenza resta una sessione per tecnico e per gateway, con risorse
riservate fino al completamento della pulizia.

Il ciclo di vita dovrà comprendere:

1. **Prenotazione:** verifica dei permessi e acquisizione della configurazione
   LAN autorizzata per la sessione; allocazione esclusiva delle risorse necessarie.
2. **Preparazione:** attivazione del gateway, verifica dei peer e costruzione
   della rete con traffico ancora bloccato.
3. **Abilitazione:** conferma API della sessione attiva prima di aprire il lease
   sul server; autorizzazione locale coerente anche sul gateway.
4. **Mantenimento:** rinnovo del lease solo dopo una risposta valida del server,
   entro la scadenza complessiva della sessione e dell'accesso che l'ha originata.
5. **Chiusura:** blocco del traffico, rimozione delle risorse di rete e rilascio
   soltanto dopo la pulizia, ripetibile anche in seguito a un errore o riavvio.

Gli errori API temporanei non dovranno prorogare il lease offline. Il controllo
di scadenza dovrà valere anche per connessioni già aperte e relativo traffico
di risposta: una regola generale per connessioni `established/related` non dovrà
permettere di oltrepassare l'autorizzazione. Stato di routing, firewall o NAT
residuo non dovrà essere attribuito alla sessione successiva.

La perdita di API, worker o agente dovrà lasciare al kernel la possibilità di
bloccare il traffico alla scadenza del lease. La chiusura HTTP non dimostra da
sola l'arresto fisico dei pacchetti: nella versione corrente il lease predefinito
è di 300 secondi; limiti e tempi della futura modalità andranno dichiarati e verificati.

## Componenti da integrare

| Componente | Lavoro necessario |
| --- | --- |
| Schema e API | Rappresentare configurazione LAN, compatibilità del gateway e dati della sessione; validare permessi e coerenza |
| Provisioning e arruolamento | Acquisire e distribuire i dati LAN nella fase concordata, gestendo gli aggiornamenti |
| Agente gateway | Verificare configurazione reale, predisporre forwarding e ritorno, applicare lease e pulizia |
| Server VPN e worker | Isolare le sessioni, instradare verso il sito corretto e gestire eventuali risorse di rete aggiuntive |
| Portale | Mostrare LAN raggiungibile, conflitti, stato di allineamento e diagnostica utile al tecnico |
| Backup e ripristino | Conservare una configurazione coerente senza riattivare autorizzazioni di runtime scadute |
| Documentazione e contratti gateway | Specificare formati, errori, capacità richieste e procedure operative della nuova modalità |

Schema iniziale completo, modelli, contratti, pacchetti e guide dovranno essere
aggiornati insieme per **nuove installazioni**. Non viene introdotta una
migrazione dalla versione corrente come prerequisito. La versione di prodotto
2.0 non determina automaticamente il prefisso di versione degli endpoint API.

Resta da decidere se Single LAN sostituirà Gateway Only oppure sarà una modalità
selezionabile. Nel secondo caso serviranno regole esplicite di selezione e
compatibilità fra server e firmware, oltre alla verifica di entrambe le modalità.

## Validazione della futura implementazione

Il collaudo dovrà coprire almeno:

- accesso dal software sul PC a gateway, PLC e HMI, con TCP, UDP e ICMP secondo
  le direzioni supportate e i limiti dei protocolli applicativi;
- due sessioni contemporanee con LAN e indirizzi dei PLC identici, verificando
  destinazione corretta e rifiuto del traffico incrociato;
- tentativi verso reti, peer e destinazioni non autorizzati, anche forzando rotte;
- conflitti con la rete locale del tecnico e modifiche della LAN durante l'uso;
- chiusura, logout, revoca e scadenza con connessioni persistenti già stabilite;
- guasti separati di API, worker, agente e OpenVPN, senza rinnovi offline;
- riavvii e pulizia parziale, con riapertura e riuso delle risorse soltanto
  dopo il rilascio corretto;
- MTU, pacchetti grandi, trasferimenti prolungati e protocolli con callback;
- provisioning, rinnovo certificati, backup e ripristino con dati LAN coerenti;
- consumo di risorse e prestazioni su gateway, client e percorsi Internet reali.

L'esito di un handshake OpenVPN o di un ping non basta a dichiarare compatibile
un software industriale. Capacità, prestazioni e compatibilità dovranno essere
misurate sulla futura implementazione, senza dedurle dalla prima versione
sperimentale. La priorità attuale resta completare e validare la versione 1.0.

## Documentazione della versione corrente

- [Guida tecnico: Gateway Only e accesso SSH](../TECHNICIAN_GUIDE.md#entrare-in-ssh-sul-gateway).
- [Prima inizializzazione del gateway](INITIAL_SETUP.md).
- [Arruolamento di fine linea](FACTORY_ENROLLMENT.md).
- [Protocollo gateway SMS/Wi-Fi, heartbeat e lease](RUNTIME_PROTOCOL.md).
- [Rinnovo remoto dei certificati](CERTIFICATE_RENEWAL.md).
- [Backup e ripristino](../BACKUP_RESTORE.md).
