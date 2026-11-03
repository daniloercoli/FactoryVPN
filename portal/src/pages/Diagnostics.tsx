import { useState, type ChangeEvent } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router";
import {
  api,
  page,
  downloadCsv,
  type Diagnostics,
  type SessionEvent,
} from "../api/client";
import {
  Badge,
  Empty,
  ErrorBox,
  Loading,
  Notice,
  PageTitle,
  Pager,
} from "../ui";
import { useGroups } from "../hooks";

const statuses = {
  ok: "Confermato",
  waiting: "In attesa",
  error: "Problema segnalato",
  unknown: "Non verificabile",
  not_applicable: "Non previsto",
};
const origins: Record<string, string> = {
  api: "Server",
  worker: "Worker VPN",
  gateway: "Gateway",
};
const events: Record<string, string> = {
  session_requested: "Sessione richiesta",
  session_active: "Rete di assistenza attiva",
  session_waiting: "Collegamento in attesa",
  session_closed: "Sessione chiusa",
  session_expired: "Sessione scaduta",
  session_failed: "Sessione interrotta",
  resources_released: "Risorse rilasciate",
  gateway_acknowledged: "Gateway attivato",
  sms_attempt: "Tentativo SMS",
  sms_accepted: "SMS accettato dal provider",
  sms_rejected: "SMS rifiutato dal provider",
  sms_unconfirmed: "Esito SMS non confermato",
  sms_not_configured: "Servizio SMS non configurato",
  worker_stale: "Conferma del worker scaduta",
  worker_changed: "Osservazione del server VPN",
  gateway_tunnel_changed: "Osservazione del tunnel gateway",
  gateway_network_changed: "Osservazione della rete gateway",
};
const reasons: Record<string, string> = {
  portal_logout: "Uscita dall’accesso che ha aperto la VPN.",
  account_logout: "Disconnessione globale dell’account.",
  expired: "Durata della VPN esaurita.",
  password_changed: "Password modificata.",
  access_revoked: "Autorizzazione revocata.",
  portal_access_invalid: "Accesso al portale scaduto o revocato.",
  sso_access_invalid: "Accesso SSO scaduto o revocato.",
  legacy_network_missing:
    "Sessione precedente senza configurazione di rete utilizzabile.",
  manual_close: "Chiusura richiesta dal tecnico.",
  administrative_close: "Chiusura amministrativa.",
  sms_unconfirmed: "Il provider non ha confermato l’SMS.",
  sms_rejected: "Il provider ha rifiutato l’SMS.",
  sms_not_configured: "Servizio SMS non configurato.",
  tunnel_timeout: "Il tunnel non si è aperto entro il tempo previsto.",
  tunnel_failed: "Apertura del tunnel non riuscita.",
  address_mismatch: "Indirizzo VPN diverso da quello assegnato.",
  network_failed: "Configurazione della rete non riuscita.",
  firewall_failed: "Configurazione del firewall non riuscita.",
  cleanup_failed: "Pulizia della rete non riuscita.",
  peer_disconnect_pending: "Un client risulta ancora collegato.",
  vpn_unavailable: "Impossibile verificare i collegamenti sul server OpenVPN.",
};
const values: Record<string, string> = {
  ready: "pronta",
  waiting: "in attesa",
  error: "errore",
  cleaned: "rilasciata",
  unknown: "non verificabile",
  starting: "in preparazione",
  stopped: "ferma",
};
function timestamp(value?: string | null) {
  return value
    ? new Intl.DateTimeFormat("it-IT", {
        dateStyle: "short",
        timeStyle: "medium",
      }).format(new Date(value))
    : "Non disponibile";
}
function description(row: SessionEvent) {
  const data = row.details;
  const parts: string[] = [];
  for (const [key, name] of [
    ["gateway_connected", "Gateway"],
    ["technician_connected", "Tecnico"],
  ]) {
    if (key in data)
      parts.push(
        `${name}: ${data[key] === true ? "collegato" : data[key] === false ? "non collegato" : "non verificabile"}.`,
      );
  }
  if (typeof data.network === "string")
    parts.push(`Rete: ${values[data.network] || "non verificabile"}.`);
  if (typeof data.state === "string")
    parts.push(`Stato: ${values[data.state] || "non verificabile"}.`);
  for (const key of ["reason", "code"]) {
    if (typeof data[key] === "string" && reasons[data[key]])
      parts.push(reasons[data[key]]);
  }
  return parts.join(" ");
}

export function useDiagnostics(id: string, poll: boolean, enabled = true) {
  return useQuery({
    queryKey: ["vpn", "diagnostics", id],
    queryFn: ({ signal }) =>
      api<Diagnostics>(`/sessions/${id}/diagnostics`, { signal }),
    refetchInterval: poll ? 3000 : false,
    refetchOnWindowFocus: true,
    enabled,
  });
}

export function DiagnosticPanel({
  query,
}: {
  query: ReturnType<typeof useDiagnostics>;
}) {
  return (
    <section className="card padded vpn-diagnostics">
      <div className="card-heading">
        <h2>Diagnostica del collegamento</h2>
        <button
          className="button secondary small"
          disabled={query.isFetching}
          onClick={() => void query.refetch()}
        >
          Aggiorna diagnostica
        </button>
      </div>
      <ErrorBox error={query.error} reload={() => void query.refetch()} />
      {query.isPending ? (
        <Loading />
      ) : (
        !query.error &&
        query.data && (
          <>
            {!query.data.history_complete && (
              <Notice>
                Sessione precedente alla nuova cronologia: lo storico
                disponibile è parziale.
              </Notice>
            )}
            <div className="diagnostic-grid">
              {query.data.steps.map((step) => (
                <article
                  key={step.id}
                  className={`diagnostic-step ${step.status}`}
                >
                  <div>
                    <h3>{step.title}</h3>
                    <Badge
                      tone={
                        step.status === "ok"
                          ? "green"
                          : step.status === "error"
                            ? "red"
                            : ""
                      }
                    >
                      {statuses[step.status]}
                    </Badge>
                  </div>
                  <p>{step.message}</p>
                  {step.action && (
                    <p className="diagnostic-action">{step.action}</p>
                  )}
                  {step.observed_at && (
                    <small className="muted">
                      {origins[step.source || ""] || "Sistema"} ·{" "}
                      {timestamp(step.observed_at)}
                    </small>
                  )}
                </article>
              ))}
            </div>
            <p className="page-note">
              Le osservazioni di collegamento sono considerate recenti per 30
              secondi. L’assenza di una conferma non prova un guasto. I segnali
              descrivono la VPN, senza verificare le modifiche sul PLC.
            </p>
          </>
        )
      )}
    </section>
  );
}

type Filters = {
  gateway_id: string;
  session_id: string;
  group_id: string;
  event_type: string;
  since: string;
  until: string;
};
const blank: Filters = {
  gateway_id: "",
  session_id: "",
  group_id: "",
  event_type: "",
  since: "",
  until: "",
};
const uuidPattern =
  "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}";

export function HistoryFilters({
  value,
  set,
  apply,
  fixedSession = false,
  audit = false,
}: {
  value: Filters;
  set: (value: Filters) => void;
  apply: () => void;
  fixedSession?: boolean;
  audit?: boolean;
}) {
  const groups = useGroups();
  const input = (key: keyof Filters) => ({
    value: value[key],
    onChange: (event: ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
      set({ ...value, [key]: event.target.value }),
  });
  return (
    <form
      className="filters history-filters"
      onSubmit={(event) => {
        event.preventDefault();
        apply();
      }}
    >
      {!fixedSession && (
        <>
          <label>
            Gateway (UUID)
            <input
              {...input("gateway_id")}
              pattern={uuidPattern}
              placeholder="Tutti i gateway visibili"
            />
          </label>
          <label>
            Sessione (UUID)
            <input
              {...input("session_id")}
              pattern={uuidPattern}
              placeholder="Tutte le sessioni visibili"
            />
          </label>
          <label>
            Gruppo
            <select {...input("group_id")}>
              <option value="">Tutti i gruppi visibili</option>
              {groups.data?.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
          </label>
        </>
      )}
      <label>
        Tipo di evento
        {audit ? (
          <input
            {...input("event_type")}
            maxLength={50}
            placeholder="es. user_updated"
          />
        ) : (
          <select {...input("event_type")}>
            <option value="">Tutti gli eventi</option>
            {Object.entries(events).map(([key, title]) => (
              <option key={key} value={key}>
                {title}
              </option>
            ))}
          </select>
        )}
      </label>
      <label>
        Dal
        <input type="datetime-local" {...input("since")} />
      </label>
      <label>
        Al
        <input
          type="datetime-local"
          {...input("until")}
          min={value.since || undefined}
        />
      </label>
      <button className="button secondary">Filtra eventi</button>
      {groups.error && <ErrorBox error={groups.error} />}
    </form>
  );
}

export function historyParams(value: Filters) {
  const result = new URLSearchParams();
  Object.entries(value).forEach(([key, value]) => {
    if (value)
      result.set(
        key,
        key === "since" || key === "until"
          ? new Date(value).toISOString()
          : value,
      );
  });
  return result;
}
export { blank as emptyHistoryFilters };

export function SessionHistory({
  sessionId,
  poll = true,
}: {
  sessionId?: string;
  poll?: boolean;
}) {
  const [params] = useSearchParams();
  const [draft, setDraft] = useState<Filters>({
    ...blank,
    session_id: sessionId || params.get("session_id") || "",
    gateway_id: sessionId ? "" : params.get("gateway_id") || "",
  });
  const [filter, setFilter] = useState(draft);
  const [offset, setOffset] = useState(0);
  const qs = historyParams(filter);
  const query = useQuery({
    queryKey: ["vpn", "history", qs.toString(), offset],
    queryFn: ({ signal }) =>
      page<SessionEvent>(
        "/session-events",
        { ...Object.fromEntries(qs), limit: 20, offset },
        signal,
      ),
    refetchInterval: poll ? 5000 : false,
    refetchOnWindowFocus: true,
  });
  const download = useMutation({
    mutationFn: () =>
      downloadCsv(`/session-events.csv?${qs}`, "factoryvpn-session-events.csv"),
  });
  return (
    <section className="card vpn-history">
      <div className="card-heading">
        <h2>Cronologia dell’assistenza</h2>
        <button
          className="button secondary small"
          disabled={download.isPending || !!query.error || !query.data?.total}
          onClick={() => download.mutate()}
        >
          Esporta CSV
        </button>
      </div>
      <HistoryFilters
        value={draft}
        set={setDraft}
        fixedSession={!!sessionId}
        apply={() => {
          setFilter(draft);
          setOffset(0);
          download.reset();
        }}
      />
      <ErrorBox
        error={query.error || download.error}
        reload={() => void query.refetch()}
      />
      {query.isPending ? (
        <Loading />
      ) : (
        !query.error &&
        query.data && (
          <>
            {query.data.items.length ? (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Data e ora</th>
                      <th>Evento</th>
                      <th>Origine e dettagli</th>
                      {!sessionId && <th>Sessione</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {query.data.items.map((row) => (
                      <tr key={row.id}>
                        <td className="nowrap">{timestamp(row.timestamp)}</td>
                        <td>
                          <strong>
                            {events[row.event_type] || "Evento di assistenza"}
                          </strong>
                          {row.actor_id && (
                            <small className="cell-sub mono">
                              Operatore: {row.actor_id}
                            </small>
                          )}
                        </td>
                        <td>
                          {origins[row.source] || "Sistema"}
                          <small className="cell-sub">{description(row)}</small>
                        </td>
                        {!sessionId && (
                          <td>
                            <Link
                              to={`/vpn/${row.session_id}`}
                              className="mono"
                            >
                              {row.session_id}
                            </Link>
                          </td>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty title="Nessun evento con questi filtri" />
            )}
            <Pager
              total={query.data.total}
              offset={offset}
              onChange={setOffset}
            />
          </>
        )
      )}
      <p className="page-note">
        Il CSV include tutti gli eventi accessibili con i filtri applicati, fino
        a 10.000 righe, con orari UTC. La cronologia registra il collegamento
        VPN; non le operazioni eseguite sul PLC.
      </p>
    </section>
  );
}

export default function HistoryPage() {
  const [params] = useSearchParams();
  return (
    <>
      <PageTitle
        title="Cronologia VPN"
        description="Cerca gli eventi delle sessioni di assistenza nel tuo ambito."
      />
      <SessionHistory key={params.toString()} />
    </>
  );
}
