# FactoryVPN — un gateway simulato con Docker

Il container esegue un solo agente gateway reale, con OpenVPN e firewall. Si
collega a un server FactoryVPN già installato: basta il pacchetto individuale
scaricato dal portale. Include un piccolo servizio HTTP per provare il tunnel.
Non avvia server, client tecnico, dispositivi LAN o radio Wi-Fi; non include SSH.
Questa versione supporta **Wi-Fi/polling**; il modem SMS non è implementato.

## 1. Preparare il pacchetto

Servono Docker Engine con Compose su Linux, oppure Docker Desktop con container
Linux e WSL2 su Windows. Il kernel deve fornire `/dev/net/tun` e nftables. I
comandi seguenti vanno eseguiti dalla radice del repository in una shell Linux
(su Windows, il terminale WSL2).

1. Sul portale, predisporre un gateway **Wi-Fi**, con il MAC di inventario e il
   gruppo appropriato. Scaricare e **attivare** lo ZIP secondo la
   [prima inizializzazione](INITIAL_SETUP.md).
2. Estrarre `gateway.env` e `gateway.ovpn` nella directory locale seguente,
   sostituendo `/percorso/gateway.zip` con il file scaricato:

   ```sh
   mkdir -p .factoryvpn-simulator/standalone-gateway
   chmod 700 .factoryvpn-simulator/standalone-gateway
   unzip /percorso/gateway.zip gateway.env gateway.ovpn -d .factoryvpn-simulator/standalone-gateway
   chmod 600 .factoryvpn-simulator/standalone-gateway/gateway.env .factoryvpn-simulator/standalone-gateway/gateway.ovpn
   ```

Usare i due file dello stesso pacchetto senza modificarne identità, MAC o
modalità. Non è necessario che il MAC registrato coincida con quello della scheda
virtuale Docker. Wi-Fi indica il polling HTTPS dell'agente: il PC può usare Ethernet.

Il dominio/IP HTTPS e l'endpoint OpenVPN del pacchetto devono essere raggiungibili
dal container, con DNS funzionante e uscita HTTPS e UDP sulla porta VPN prevista.
Non usare `localhost` o nomi interni allo stack server come `nginx` e `openvpn`.
Evitare sovrapposizioni con il pool VPN `10.100.0.0/16`.

Per HTTPS con CA pubblica non occorre altro. Per una CA HTTPS privata, copiare il
bundle pubblico in `.factoryvpn-simulator/standalone-gateway/https-ca.pem`: verrà
usato per verificare il server. Non copiare chiavi private delle CA e non
disabilitare la verifica TLS.

## 2. Avviare il gateway

```sh
docker compose -f gateway-agent/compose.yaml -p factoryvpn-gateway up -d --build
docker compose -f gateway-agent/compose.yaml -p factoryvpn-gateway ps
docker compose -f gateway-agent/compose.yaml -p factoryvpn-gateway logs --tail 80
```

La build usa `docker/Dockerfile.gateway` e riutilizza `gateway-agent/`,
`factoryvpn_common/` e gli schemi necessari. Il container riceve il dispositivo
TUN e le capacità `NET_ADMIN` e `NET_RAW`; `DAC_OVERRIDE` consente di usare la
directory protetta anche se appartiene all'utente Linux del PC. Mantiene una rete
Docker separata, senza modalità `privileged`, rete host o porte pubblicate sul PC.

La directory delle credenziali è montata in `/etc/factoryvpn` **scrivibile**:
il rinnovo sostituisce atomicamente il profilo e conserva lì i journal. Non
montare i due file singolarmente e non cancellare i journal durante un rinnovo.
Il percorso OpenVPN viene adattato automaticamente al container. La directory
predefinita è esclusa da Git e dai contesti Docker: contiene segreti individuali,
da proteggere anche sul PC e nelle eventuali copie di backup.

Lo stato `healthy` verifica soltanto che il processo e il servizio dimostrativo
funzionino; non prova registrazione, sessione attiva o traffico VPN. Il container
si riavvia automaticamente se termina, salvo arresto esplicito.

## 3. Provare la VPN

Con un tecnico autorizzato per il gruppo del gateway:

1. Chiedere la connessione dal portale e attendere che il gateway sia pronto.
2. Avviare manualmente il profilo OpenVPN del tecnico sul proprio PC, seguendo
   la [guida tecnico](../TECHNICIAN_GUIDE.md).
3. Aprire `http://IP_VPN_GATEWAY:8080/`, usando l'IP VPN indicato nel portale:
   la risposta attesa è `FactoryVPN gateway simulator`.
4. Chiudere la sessione dal portale e verificare che l'accesso HTTP si interrompa.

Il servizio HTTP restituisce solo un testo fisso, senza file o credenziali. Per
disabilitarlo, impostare `export FACTORYVPN_SIMULATOR_HTTP_PORT=0` e rieseguire
il comando di avvio. In questo caso il controllo di salute verifica il processo.

## 4. Arresto e altre istanze

```sh
docker compose -f gateway-agent/compose.yaml -p factoryvpn-gateway down
```

L'arresto concede fino a 30 secondi alla pulizia dell'agente. I file locali
restano disponibili per il prossimo avvio.

Per un'altra istanza, preparare un'altra directory con un **diverso pacchetto
gateway**, poi scegliere un nome progetto distinto e un percorso assoluto:

```sh
export FACTORYVPN_GATEWAY_DIR="$PWD/.factoryvpn-simulator/gateway-2"
docker compose -f gateway-agent/compose.yaml -p factoryvpn-gateway-2 up -d --build
```

Conservare lo stesso nome progetto e la stessa variabile per i comandi di
gestione di quella istanza. Non avviare due container con la stessa identità.
Se l'avvio fallisce, controllare i log, l'attivazione del pacchetto, la CA HTTPS
e la raggiungibilità degli endpoint dal container. Un pacchetto SMS viene
rifiutato: predisporre dal portale un'identità Wi-Fi per questo simulatore.
