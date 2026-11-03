import type { components } from "./schema";

export type User = components["schemas"]["UserResponse"];
export type CurrentUser = components["schemas"]["CurrentUserResponse"];
export type Group = components["schemas"]["GroupResponse"];
export type Gateway = components["schemas"]["AdminGatewayCatalogResponse"];
export type VPNGateway = components["schemas"]["GatewayCatalogResponse"];
export type GatewayDetail = components["schemas"]["GatewayDetailResponse"];
export type Device = components["schemas"]["DeviceResponse"];
export type DeviceInput = components["schemas"]["DeviceInput"];
export type VPNRow = components["schemas"]["SessionListResponse"];
export type VPNClient = components["schemas"]["AdminSessionListResponse"];
export type VPNDetail = components["schemas"]["SessionDetailResponse"];
export type VPNOpened = components["schemas"]["SessionCreateResponse"];
export type Diagnostics = components["schemas"]["DiagnosticsResponse"];
export type SystemInfo = components["schemas"]["SystemView"];
export type TechnicalLog = components["schemas"]["TechnicalLogView"];
export type Telemetry = components["schemas"]["TelemetryView"];
export type TelemetryHistory = components["schemas"]["TelemetryHistory"];
export type ServerStatus = components["schemas"]["ServerStatus"];
export type SmtpResult = components["schemas"]["SmtpResult"];
export type SessionEvent = components["schemas"]["SessionEventResponse"];
export type Identity = components["schemas"]["IdentityResponse"];
export type Provider = components["schemas"]["SSOProviderResponse"];
export type EmailChallenge = components["schemas"]["EmailChallengeResponse"];
export type Session = components["schemas"]["WebSessionResponse"];
export type Audit = components["schemas"]["AuditLogResponse"];
export type Stats = components["schemas"]["StatsResponse"];
export type UserInput = components["schemas"]["UserCreateRequest"];
export type UserUpdate = components["schemas"]["UserUpdateRequest"];
export type ProvisioningState = components["schemas"]["ProvisioningState"];
export type ProvisioningJob = components["schemas"]["JobResponse"];
export type Permission = components["schemas"]["Permission"];
export type BackupStatus = components["schemas"]["BackupStatus"];
export type BackupEntry = components["schemas"]["BackupEntry"];
export type Page<T> = { items: T[]; total: number };

let csrf = "";
let generation = 0;
// Discard late responses from a previous login, including uncancellable mutations.
export function invalidateRequests() {
  generation += 1;
}
function checkGeneration(started: number) {
  if (started !== generation)
    throw new DOMException("Session changed", "AbortError");
}
export function setCsrf(value: string) {
  csrf = value;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(detail);
  }
}

type Options = {
  method?: string;
  body?: unknown;
  revision?: string;
  idempotencyKey?: string;
  signal?: AbortSignal;
  rawBody?: File;
};
async function transport(path: string, options: Options = {}) {
  const started = generation;
  const headers: Record<string, string> = {
    "X-FactoryVPN-Portal": "1",
    Accept: "application/json",
  };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.rawBody) headers["Content-Type"] = "application/octet-stream";
  if (csrf) headers["X-CSRF-Token"] = csrf;
  if (options.revision) headers["If-Match"] = `"${options.revision}"`;
  if (options.idempotencyKey)
    headers["Idempotency-Key"] = options.idempotencyKey;
  const response = await fetch("/api/v1" + path, {
    method: options.method || "GET",
    body:
      options.rawBody ??
      (options.body === undefined ? undefined : JSON.stringify(options.body)),
    headers,
    signal: options.signal,
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
  });
  checkGeneration(started);
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    checkGeneration(started);
    const confirmationFailed =
      path === "/backups/reauthenticate" &&
      data.detail === "Invalid confirmation password";
    if (response.status === 401 && path !== "/web/login" && !confirmationFailed)
      window.dispatchEvent(new Event("factoryvpn:unauthorized"));
    throw new ApiError(
      response.status,
      typeof data.detail === "string" ? data.detail : "",
    );
  }
  return response;
}

export async function api<T>(path: string, options: Options = {}): Promise<T> {
  const started = generation;
  const response = await transport(path, options);
  const value =
    response.status === 204 ? (undefined as T) : await response.json();
  checkGeneration(started);
  return value;
}
export async function downloadCsv(path: string, filename: string) {
  return saveDownload(path, filename, "GET");
}
export async function downloadFile(path: string, filename: string) {
  return saveDownload(path, filename, "POST");
}
async function saveDownload(path: string, filename: string, method: string) {
  const started = generation;
  const response = await transport(path, { method });
  const blob = await response.blob();
  checkGeneration(started);
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export async function page<T>(
  path: string,
  parameters: Record<string, string | number | boolean | undefined>,
  signal?: AbortSignal,
): Promise<Page<T>> {
  const started = generation;
  const params = new URLSearchParams();
  Object.entries(parameters).forEach(([key, value]) => {
    if (value !== undefined && value !== "") params.set(key, String(value));
  });
  const response = await transport(path + "?" + params, { signal });
  const items: T[] = await response.json();
  checkGeneration(started);
  return {
    items,
    total: Number(response.headers.get("X-Total-Count") ?? items.length),
  };
}
export async function allGroups(signal?: AbortSignal): Promise<Group[]> {
  const items: Group[] = [];
  for (;;) {
    const batch = await page<Group>(
      "/groups/",
      { limit: 1000, offset: items.length },
      signal,
    );
    items.push(...batch.items);
    if (items.length >= batch.total || batch.items.length === 0) return items;
  }
}

export function errorMessage(error: unknown): string {
  if (!(error instanceof ApiError))
    return "Impossibile raggiungere il servizio. Controlla la connessione e riprova.";
  if (error.detail === "Recent email verification required")
    return "Conferma la tua identità con un codice email nella pagina Il mio profilo, poi ripeti l’operazione.";
  if (
    [
      "Verification expired or invalid",
      "Incorrect or expired verification code",
    ].includes(error.detail)
  )
    return "Codice errato, già utilizzato o scaduto. Riprova oppure torna indietro per richiederne uno nuovo.";
  if (error.detail === "Email verification temporarily locked")
    return "Troppi tentativi errati. Attendi 15 minuti prima di riprovare.";
  if (error.detail === "Wait before requesting another email")
    return "Attendi almeno un minuto prima di richiedere un altro codice.";
  if (error.detail === "Email hourly limit reached")
    return "Hai raggiunto il limite di invii. Riprova tra un’ora.";
  if (
    error.detail.includes("Email delivery unavailable") ||
    error.detail === "Email verification is not configured"
  )
    return "Invio email non disponibile. Contatta l’amministratore del server.";
  if (error.detail.includes("valid account email is required"))
    return "L’account richiede un indirizzo email valido. Contatta un amministratore per il recupero.";
  if (error.detail === "Email address unavailable")
    return "L’indirizzo email non è disponibile.";
  if (error.detail === "Invalid confirmation password")
    return "Password di conferma non corretta. Riprova.";
  if (error.detail.includes("Backup: insufficient_disk"))
    return "Spazio su disco insufficiente per caricare il backup.";
  if (error.detail.includes("recent authentication"))
    return "Conferma nuovamente la tua identità per gestire i backup.";
  if (error.detail.includes("maintenance in progress"))
    return "Ripristino in corso. Il servizio tornerà disponibile al termine delle verifiche.";
  if (error.detail.includes("Backup: operation_in_progress"))
    return "Un’altra operazione di backup è già in corso. Attendi il suo completamento.";
  if (error.status === 413)
    return "Il file supera la dimensione massima consentita.";
  if (error.status === 412 || error.status === 428)
    return "Questi dati sono cambiati. Ricarica la scheda prima di salvare: le tue modifiche non sono state applicate.";
  if (error.status === 401)
    return "Accesso non valido o sessione scaduta. Accedi nuovamente.";
  if (error.detail.includes("Group contains accounts outside"))
    return "Questo gruppo contiene account che non puoi amministrare. La sua eliminazione richiede un amministratore autorizzato a gestire tutti i membri.";
  if (error.status === 403)
    return "Non hai il permesso di eseguire questa operazione.";
  if (error.status === 404) return "Risorsa non disponibile nel tuo ambito.";
  if (error.status === 422)
    if (error.detail.includes("Export exceeds"))
      return "L’esportazione supera 10.000 righe. Riduci l’intervallo o aggiungi filtri.";
    else
      return "Controlla i campi: alcuni valori non rispettano il formato richiesto.";
  if (error.detail.includes("personal VPN profile"))
    return "Prepara e attiva il tuo profilo VPN dalla pagina Il mio profilo.";
  if (error.detail.includes("Certificate changed"))
    return "Il certificato è cambiato. Aggiorna la pagina prima di continuare.";
  if (error.detail.includes("already being prepared"))
    return "Esiste già una preparazione in corso. Aggiorna la pagina per seguirla.";
  if (error.detail.includes("Provisioning request limit"))
    return "Hai raggiunto il limite orario di preparazione dei profili. Riprova più tardi.";
  if (
    error.detail.includes("Provisioning") ||
    error.detail.includes("provisioning")
  )
    return "Il pacchetto non è disponibile per questo accesso oppure il servizio di emissione non è configurato. Aggiorna la pagina e verifica lo stato.";
  if (error.status === 429)
    return "È già stato richiesto un SMS. Attendi almeno un minuto dall’ultimo tentativo e aggiorna lo stato.";
  if (error.detail.includes("invalid or overlapping LAN"))
    return "La rete LAN del gateway non è compatibile. Contatta l’amministratore.";
  if (error.detail.includes("credentials are not provisioned"))
    return "Il gateway deve essere configurato dall’amministratore prima dell’avvio.";
  if (
    error.detail.includes("Only a live SMS") ||
    error.detail.includes("no longer authorized")
  )
    return "La sessione non consente più questa operazione. Aggiorna lo stato.";
  if (error.detail.includes("Idempotency key"))
    return "Questa richiesta si riferisce a un altro gateway. Chiudi la finestra e ripeti la selezione.";
  if (error.detail.includes("last active"))
    return "Deve rimanere almeno un amministratore globale locale attivo.";
  if (
    error.detail.includes("cleanup") ||
    error.detail.includes("Close sessions")
  )
    return "Sono presenti sessioni VPN o risorse in pulizia. Attendi la chiusura e riprova.";
  if (error.detail.includes("Reassign the group's gateways"))
    return "Sposta i gateway in un altro gruppo prima di eliminare questo gruppo.";
  if (error.detail.includes("Link an external identity"))
    return "Collega prima un’identità SSO all’account.";
  if (error.detail.includes("current account"))
    return "Non puoi eliminare l’account con cui hai effettuato l’accesso.";
  if (error.detail.includes("Current password"))
    return "La password attuale non è corretta.";
  if (error.status === 409)
    return "Operazione in conflitto: verifica username, email e collegamenti già esistenti.";
  return "Il servizio non ha completato l’operazione. Riprova tra poco.";
}
