import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router";
import { Radio, RefreshCw, ShieldCheck } from "lucide-react";
import {
  api,
  page,
  type VPNGateway,
  type VPNRow,
  type VPNDetail as Detail,
  type VPNOpened,
} from "../api/client";
import { useAuth } from "../auth";
import { DiagnosticPanel, SessionHistory, useDiagnostics } from "./Diagnostics";
import { useGroups } from "../hooks";
import {
  Badge,
  Confirm,
  Empty,
  ErrorBox,
  Loading,
  Notice,
  PageTitle,
  Pager,
  date,
} from "../ui";

export const statusNames: Record<string, string> = {
  REQUESTED: "In attesa",
  ACTIVE: "Attiva",
  CLOSED: "Chiusa",
  EXPIRED: "Scaduta",
  FAILED: "Non riuscita",
};
export const availabilityNames = {
  available: "Disponibile",
  busy: "Occupato",
  cleanup_pending: "Pulizia in corso",
  unconfigured: "Da configurare",
};
export const blockNames = {
  personal_profile_required:
    "Prepara e attiva il tuo profilo VPN dalla pagina Il mio profilo.",
  permission_required: "Serve il permesso di connessione",
  gateway_unconfigured: "Configurazione incompleta",
  gateway_busy: "Gateway già in uso",
  gateway_cleanup_pending: "Attendi il rilascio delle risorse",
  technician_busy: "Hai già una sessione aperta",
  technician_cleanup_pending: "La tua sessione precedente è in pulizia",
};
const live = (status: string) => status === "REQUESTED" || status === "ACTIVE";
const pending = (session: Detail) =>
  !live(session.status) && session.network_slot != null && !session.cleaned_at;
function useNow() {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  return now;
}
function remaining(value: string | null | undefined, now: number) {
  const seconds = Math.max(
    0,
    Math.ceil(((value ? Date.parse(value) : now) - now) / 1000),
  );
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
function fresh(value: string | null | undefined, now: number) {
  return !!value && now - Date.parse(value) < 30000;
}
function phase(session: Detail, now: number) {
  if (!live(session.status))
    return pending(session)
      ? "Rilascio delle risorse"
      : statusNames[session.status];
  if (session.expires_at && Date.parse(session.expires_at) <= now)
    return "Verifica della scadenza";
  if (
    session.status === "ACTIVE" &&
    fresh(session.worker_seen_at, now) &&
    fresh(session.gateway_ready_at, now)
  )
    return "VPN attiva";
  if (!session.activation_received_at)
    return session.activation_mode === "sms"
      ? "Attesa ricezione SMS"
      : "Attesa del gateway Wi-Fi";
  if (!fresh(session.gateway_ready_at, now)) return "Preparazione del gateway";
  return "Verifica del collegamento VPN";
}
async function refreshVPN(client: ReturnType<typeof useQueryClient>) {
  await Promise.all(
    ["vpn", "gateways", "stats", "audit"].map((key) =>
      client.invalidateQueries({ queryKey: [key] }),
    ),
  );
}

export default function VPN() {
  const auth = useAuth();
  const groups = useGroups();
  const [q, setQ] = useState("");
  const [group, setGroup] = useState("");
  const [mode, setMode] = useState("");
  const [availability, setAvailability] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<{
    gateway: VPNGateway;
    key: string;
  } | null>(null);
  const query = useQuery({
    queryKey: ["vpn", "gateways", q, group, mode, availability, offset],
    queryFn: ({ signal }) =>
      page<VPNGateway>(
        "/gateways/",
        {
          q,
          group_id: group,
          activation_mode: mode,
          availability,
          limit: 20,
          offset,
        },
        signal,
      ),
    refetchInterval: 10000,
    refetchOnWindowFocus: true,
  });
  return (
    <>
      <PageTitle
        action={
          auth.can("gateways.provision") && (
            <Link className="button" to="/gateways/new">
              Aggiungi gateway
            </Link>
          )
        }
        title="Le mie VPN"
        description="Scegli un gateway del tuo ambito e segui la sessione di assistenza."
      />
      <Notice>
        Apri la sessione qui, poi collega manualmente il client OpenVPN con il
        profilo già configurato sul tuo computer.{" "}
        <Link to="/profile">Gestisci il tuo profilo VPN</Link>.
      </Notice>
      <section className="card">
        <div className="card-heading">
          <h2>
            <Radio size={20} /> Scegli il gateway
          </h2>
          <button
            className="button secondary small"
            onClick={() => void query.refetch()}
            disabled={query.isFetching}
          >
            <RefreshCw size={15} /> Aggiorna gateway
          </button>
        </div>
        <div className="filters">
          <label className="search-field">
            Cerca gateway
            <input
              value={q}
              maxLength={100}
              placeholder="Nome, MAC, telefono o UUID"
              onChange={(e) => {
                setQ(e.target.value);
                setOffset(0);
              }}
            />
          </label>
          <label>
            Gruppo
            <select
              value={group}
              onChange={(e) => {
                setGroup(e.target.value);
                setOffset(0);
              }}
            >
              <option value="">Tutti i gruppi visibili</option>
              {groups.data?.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Modalità
            <select
              value={mode}
              onChange={(e) => {
                setMode(e.target.value);
                setOffset(0);
              }}
            >
              <option value="">Tutte</option>
              <option value="wifi">Wi-Fi</option>
              <option value="sms">SMS</option>
            </select>
          </label>
          <label>
            Disponibilità
            <select
              value={availability}
              onChange={(e) => {
                setAvailability(e.target.value);
                setOffset(0);
              }}
            >
              <option value="">Tutte</option>
              {Object.entries(availabilityNames).map(([value, name]) => (
                <option key={value} value={value}>
                  {name}
                </option>
              ))}
            </select>
          </label>
        </div>
        <ErrorBox
          error={query.error || groups.error}
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
                        <th>Gateway</th>
                        <th>Gruppo</th>
                        <th>Modalità</th>
                        <th>Disponibilità</th>
                        <th>Sessione</th>
                      </tr>
                    </thead>
                    <tbody>
                      {query.data.items.map((g) => (
                        <tr key={g.id}>
                          <td>
                            <Link to={`/gateways/${g.id}`}>
                              <strong>
                                {g.display_name || g.mac_address || g.id}
                              </strong>
                            </Link>
                            <small className="cell-sub mono">
                              {g.display_name ? g.mac_address || g.id : g.id}
                            </small>
                          </td>
                          <td>
                            {groups.data?.find((item) => item.id === g.group_id)
                              ?.name ||
                              (g.group_id
                                ? "Gruppo non disponibile"
                                : "Non assegnato")}
                          </td>
                          <td>
                            {g.activation_mode === "sms" ? "SMS" : "Wi-Fi"}
                          </td>
                          <td>
                            <Badge
                              tone={
                                g.availability === "available" ? "blue" : ""
                              }
                            >
                              {availabilityNames[g.availability]}
                            </Badge>
                          </td>
                          <td>
                            {auth.can("connect") && (
                              <button
                                className="button small"
                                disabled={!g.can_start || query.isFetching}
                                aria-label={`Apri VPN ${g.display_name || g.mac_address || g.id}`}
                                onClick={() => {
                                  setSelected({
                                    gateway: g,
                                    key: crypto.randomUUID(),
                                  });
                                }}
                              >
                                Apri sessione
                              </button>
                            )}
                            {g.start_block_reason && (
                              <small className="cell-sub">
                                {blockNames[g.start_block_reason]}
                              </small>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty title="Nessun gateway disponibile con questi filtri" />
              )}
              <Pager
                total={query.data.total}
                offset={offset}
                onChange={setOffset}
              />
            </>
          )
        )}
      </section>
      <p className="page-note vpn-note">
        La disponibilità indica se il gateway può ricevere una richiesta; la
        connessione viene verificata durante la sessione.
      </p>
      <SessionList />
      {selected && (
        <VPNStartDialog
          selection={selected}
          onClose={() => setSelected(null)}
        />
      )}
    </>
  );
}

export function VPNStartDialog({
  selection,
  onClose,
}: {
  selection: { gateway: VPNGateway; key: string };
  onClose: () => void;
}) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const open = useMutation({
    mutationFn: (selection: { gateway: VPNGateway; key: string }) =>
      api<VPNOpened>("/sessions/", {
        method: "POST",
        body: { gateway_id: selection.gateway.id },
        idempotencyKey: selection.key,
      }),
    onSuccess: async (result) => {
      onClose();
      await refreshVPN(client);
      navigate(`/vpn/${result.session_id}`);
    },
  });
  return (
    <Confirm
      title="Aprire la sessione VPN?"
      label="Apri sessione"
      onCancel={() => onClose()}
      onConfirm={() => open.mutate(selection)}
      busy={open.isPending}
      error={open.error}
    >
      <p>
        <strong>
          {selection.gateway.display_name ||
            selection.gateway.mac_address ||
            selection.gateway.id}
        </strong>
      </p>
      <p>
        {selection.gateway.activation_mode === "sms"
          ? "Verrà richiesto un SMS di attivazione al gateway."
          : "Il gateway prenderà in carico la richiesta al prossimo controllo Wi-Fi."}
      </p>
      <p>
        La sessione dura al massimo 30 minuti e termina anche al logout o alla
        scadenza di questo accesso. Quando il gateway è pronto, avvia
        manualmente il client OpenVPN.
      </p>
      {open.error && (
        <p>
          Puoi riprovare qui senza duplicare la richiesta, oppure consultare le
          tue sessioni.
        </p>
      )}
    </Confirm>
  );
}

function SessionList() {
  const groups = useGroups();
  const [q, setQ] = useState("");
  const [group, setGroup] = useState("");
  const [status, setStatus] = useState("");
  const [reservation, setReservation] = useState("");
  const [offset, setOffset] = useState(0);
  const query = useQuery({
    queryKey: ["vpn", "sessions", q, group, status, reservation, offset],
    queryFn: ({ signal }) =>
      page<VPNRow>(
        "/sessions/",
        {
          q,
          group_id: group,
          status_filter: status,
          reserved: reservation === "reserved" ? true : undefined,
          cleanup_pending: reservation === "cleanup" ? true : undefined,
          limit: 20,
          offset,
        },
        signal,
      ),
    refetchInterval: 5000,
    refetchOnWindowFocus: true,
  });
  return (
    <section className="card vpn-history">
      <div className="card-heading">
        <h2>Le tue sessioni</h2>
        <button
          className="button secondary small"
          onClick={() => void query.refetch()}
          disabled={query.isFetching}
        >
          Aggiorna sessioni
        </button>
      </div>
      <div className="filters">
        <label className="search-field">
          Cerca sessioni
          <input
            value={q}
            maxLength={100}
            placeholder="Gateway o UUID sessione"
            onChange={(e) => {
              setQ(e.target.value);
              setOffset(0);
            }}
          />
        </label>
        <label>
          Gruppo sessione
          <select
            value={group}
            onChange={(e) => {
              setGroup(e.target.value);
              setOffset(0);
            }}
          >
            <option value="">Tutti i gruppi visibili</option>
            {groups.data?.map((g) => (
              <option key={g.id} value={g.id}>
                {g.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Stato sessione
          <select
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              setOffset(0);
            }}
          >
            <option value="">Tutti</option>
            {Object.entries(statusNames).map(([value, name]) => (
              <option key={value} value={value}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Risorse
          <select
            value={reservation}
            onChange={(e) => {
              setReservation(e.target.value);
              setOffset(0);
            }}
          >
            <option value="">Tutte le sessioni</option>
            <option value="reserved">Con risorse riservate</option>
            <option value="cleanup">In pulizia</option>
          </select>
        </label>
      </div>
      <ErrorBox error={query.error} reload={() => void query.refetch()} />
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
                      <th>Gateway / sessione</th>
                      <th>Esito registrato</th>
                      <th>Apertura</th>
                      <th>Dettaglio</th>
                    </tr>
                  </thead>
                  <tbody>
                    {query.data.items.map((s) => (
                      <tr key={s.id}>
                        <td>
                          <strong>
                            {s.gateway_display_name ||
                              s.gateway_mac_address ||
                              "Gateway non disponibile"}
                          </strong>
                          <small className="cell-sub mono">{s.id}</small>
                        </td>
                        <td>
                          <Badge>{statusNames[s.status]}</Badge>
                          {s.cleanup_pending && (
                            <small className="cell-sub">
                              Rilascio delle risorse
                            </small>
                          )}
                        </td>
                        <td className="nowrap">{date(s.requested_at)}</td>
                        <td>
                          <Link
                            className="button secondary small"
                            to={`/vpn/${s.id}`}
                            aria-label={`Dettaglio sessione ${s.id}`}
                          >
                            Dettaglio
                          </Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty title="Nessuna sessione" />
            )}
            <Pager
              total={query.data.total}
              offset={offset}
              onChange={setOffset}
            />
          </>
        )
      )}
    </section>
  );
}

export function VPNDetail() {
  const { id } = useParams();
  return <VPNDetailView key={id} id={id!} />;
}

function VPNDetailView({ id }: { id: string }) {
  const client = useQueryClient();
  const auth = useAuth();
  const now = useNow();
  const [action, setAction] = useState<"close" | "wake" | null>(null);
  const [sent, setSent] = useState<string | null>(null);
  const query = useQuery({
    queryKey: ["vpn", "detail", id],
    queryFn: ({ signal }) =>
      api<Detail>(`/sessions/${encodeURIComponent(id!)}`, { signal }),
    refetchInterval: (query) =>
      query.state.data &&
      !live(query.state.data.status) &&
      !pending(query.state.data)
        ? false
        : 3000,
    refetchOnWindowFocus: true,
  });
  const s = query.data;
  const diagnostics = useDiagnostics(
    id,
    !!s && (live(s.status) || pending(s)),
    !!s && !query.error,
  );
  const gateway = useQuery({
    queryKey: ["vpn", "gateway", s?.gateway_id],
    queryFn: ({ signal }) =>
      api<VPNGateway>(`/gateways/${s!.gateway_id}`, { signal }),
    enabled: !!s?.gateway_id && !query.error,
  });
  const mutation = useMutation({
    mutationFn: (operation: "close" | "wake") =>
      operation === "close"
        ? api<{ message: string }>(`/sessions/${id}`, { method: "DELETE" })
        : api<VPNOpened>(`/sessions/${id}/wake`, { method: "POST" }),
    onSuccess: async (result, operation) => {
      setAction(null);
      setSent(
        operation === "wake"
          ? result.message.includes("resend failed")
            ? "Il provider non ha confermato il reinvio. La sessione resta valida; potrai riprovare dopo l’attesa."
            : "Richiesta di reinvio elaborata. Controlla lo stato di ricezione del gateway."
          : null,
      );
      await refreshVPN(client);
    },
    onError: () => {
      void query.refetch();
    },
  });
  if (query.isPending) return <Loading />;
  if (query.error || !s)
    return (
      <>
        <PageTitle
          title="Stato VPN non disponibile"
          description="Non è possibile verificare lo stato corrente della sessione."
          back="/vpn"
        />
        <ErrorBox error={query.error} reload={() => void query.refetch()} />
      </>
    );
  const connectionProblem = diagnostics.data?.steps.some(
    (step) =>
      ["worker", "tunnel", "technician", "network"].includes(step.id) &&
      step.status === "error",
  );
  const state =
    live(s.status) && diagnostics.error
      ? "Diagnostica non disponibile"
      : live(s.status) &&
          diagnostics.data?.status !== undefined &&
          diagnostics.data.status !== s.status
        ? "Aggiornamento dello stato"
        : live(s.status) && connectionProblem
          ? "Problema di collegamento segnalato"
          : phase(s, now);
  const isOwner = s.technician_id === auth.user?.id;
  const cooldown = s.sms_last_attempt_at
    ? Math.max(
        0,
        Math.ceil((Date.parse(s.sms_last_attempt_at) + 60000 - now) / 1000),
      )
    : 0;
  const canWake =
    live(s.status) &&
    s.activation_mode === "sms" &&
    !cooldown &&
    (!s.expires_at || Date.parse(s.expires_at) > now);
  const ready =
    live(s.status) &&
    fresh(s.gateway_ready_at, now) &&
    !!s.expires_at &&
    Date.parse(s.expires_at) > now;
  const gatewayData = gateway.error ? undefined : gateway.data;
  return (
    <>
      <PageTitle
        title="Sessione VPN"
        description={
          gatewayData?.display_name ||
          gatewayData?.mac_address ||
          "Sessione di assistenza"
        }
        back={isOwner ? "/vpn" : "/clients"}
        action={
          <button
            className="button secondary"
            onClick={() => void query.refetch()}
            disabled={query.isFetching}
          >
            <RefreshCw size={16} /> Aggiorna stato
          </button>
        }
      />
      {gatewayData && (
        <p className="page-note">
          <Link to={`/gateways/${gatewayData.id}`}>Scheda gateway</Link>
        </p>
      )}
      {sent && <Notice>{sent}</Notice>}
      <section className="card padded vpn-state">
        <div className="vpn-state-heading">
          <ShieldCheck size={30} />
          <div>
            <p className="eyebrow">STATO CORRENTE</p>
            <h2>{state}</h2>
          </div>
          <Badge tone={state === "VPN attiva" ? "green" : "blue"}>
            {statusNames[s.status]}
          </Badge>
        </div>
        {live(s.status) && (
          <p className="vpn-countdown">
            Tempo rimanente <strong>{remaining(s.expires_at, now)}</strong>
          </p>
        )}
        {live(s.status) && !ready && (
          <p>
            Attendi che il gateway confermi la ricezione e prepari la
            connessione. Lo stato si aggiorna automaticamente.
          </p>
        )}
        {ready && state !== "VPN attiva" && (
          <Notice>
            {isOwner
              ? "Il gateway è pronto. Apri manualmente il client OpenVPN se non è già collegato; consulta la diagnostica per verificare i due lati del collegamento."
              : "Il gateway è pronto. La diagnostica mostra se il client del tecnico risulta collegato."}
          </Notice>
        )}
        {state === "VPN attiva" && (
          <p>
            Gateway e tecnico sono collegati; la rete di assistenza è attiva.
          </p>
        )}
        {pending(s) && (
          <p>
            La sessione è terminata. Attendi che il server rilasci le risorse
            prima di aprirne un’altra.
          </p>
        )}
        {!live(s.status) && s.cleaned_at && (
          <p>Risorse rilasciate. Puoi aprire una nuova sessione.</p>
        )}
        {s.error_reason && (
          <div className="notice amber">
            <p>{failureText(s.error_reason)}</p>
          </div>
        )}
        <div className="vpn-actions">
          {live(s.status) && (
            <button
              className="button danger"
              onClick={() => {
                mutation.reset();
                setAction("close");
              }}
            >
              Chiudi sessione
            </button>
          )}
          {live(s.status) && s.activation_mode === "sms" && (
            <button
              className="button secondary"
              disabled={!canWake}
              onClick={() => {
                mutation.reset();
                setAction("wake");
              }}
            >
              {cooldown ? `Reinvia SMS tra ${cooldown}s` : "Reinvia SMS"}
            </button>
          )}
          {!live(s.status) && (
            <Link className="button secondary" to="/vpn">
              Torna alle mie VPN
            </Link>
          )}
        </div>
      </section>
      <DiagnosticPanel query={diagnostics} />
      <SessionHistory sessionId={id} poll={live(s.status) || pending(s)} />
      <p className="page-note">
        {s.gateway_id && (
          <Link to={`/vpn-history?gateway_id=${s.gateway_id}`}>
            Cronologia di questo gateway
          </Link>
        )}
        {auth.can("audit.read") && (
          <>
            {" "}
            ·{" "}
            <Link to={`/audit?session_id=${id}`}>
              Audit amministrativo della sessione
            </Link>
          </>
        )}
      </p>
      <section className="card padded vpn-history">
        <h2>Dettagli della sessione</h2>
        <dl className="vpn-details">
          <div>
            <dt>Sessione</dt>
            <dd className="mono">{s.id}</dd>
          </div>
          <div>
            <dt>Modalità</dt>
            <dd>{s.activation_mode === "sms" ? "SMS" : "Wi-Fi"}</dd>
          </div>
          <div>
            <dt>Richiesta</dt>
            <dd>{date(s.requested_at)}</dd>
          </div>
          <div>
            <dt>Scadenza</dt>
            <dd>{date(s.expires_at)}</dd>
          </div>
          <div>
            <dt>SMS accettato dal provider</dt>
            <dd>
              {s.activation_mode === "sms"
                ? s.sms_sent_at
                  ? date(s.sms_sent_at)
                  : "Non confermato"
                : "Non previsto"}
            </dd>
          </div>
          <div>
            <dt>Presa in carico del gateway</dt>
            <dd>
              {s.activation_received_at
                ? date(s.activation_received_at)
                : "In attesa"}
            </dd>
          </div>
          <div>
            <dt>Ultima conferma gateway pronto</dt>
            <dd>
              {s.gateway_ready_at ? date(s.gateway_ready_at) : "Non confermata"}
            </dd>
          </div>
          <div>
            <dt>Fine sessione</dt>
            <dd>{s.ended_at ? date(s.ended_at) : "Non terminata"}</dd>
          </div>
          <div>
            <dt>Pulizia completata</dt>
            <dd>{s.cleaned_at ? date(s.cleaned_at) : "Non completata"}</dd>
          </div>
          <div>
            <dt>Rete LAN</dt>
            <dd>{s.lan_cidr || "Non disponibile"}</dd>
          </div>
        </dl>
        <p className="muted">
          Ultimo aggiornamento:{" "}
          {date(new Date(query.dataUpdatedAt).toISOString())}. I dettagli
          restano consultabili dopo la chiusura.
        </p>
      </section>
      {action && (
        <Confirm
          title={
            action === "close"
              ? "Chiudere la sessione VPN?"
              : "Reinviare l’SMS di attivazione?"
          }
          label={action === "close" ? "Conferma chiusura" : "Conferma reinvio"}
          danger={action === "close"}
          busy={mutation.isPending}
          error={mutation.error}
          onCancel={() => setAction(null)}
          onConfirm={() => mutation.mutate(action)}
        >
          <p>
            {action === "close"
              ? "L’assistenza verrà interrotta e il server rilascerà le risorse della sessione."
              : "Verrà richiesto un nuovo SMS al gateway. Il reinvio non prolunga la scadenza della VPN."}
          </p>
          {!isOwner && (
            <p>
              Stai intervenendo sulla sessione di un altro tecnico nel tuo
              ambito.
            </p>
          )}
        </Confirm>
      )}
    </>
  );
}

function failureText(reason: string) {
  if (reason.includes("SMS provider"))
    return "Il provider non ha confermato l’accettazione dell’SMS.";
  if (reason.includes("expired") || reason.includes("signed out"))
    return "L’accesso è scaduto o è stato disconnesso.";
  if (
    reason.includes("revoked") ||
    reason.includes("disabled") ||
    reason.includes("permission")
  )
    return "L’accesso alla VPN è stato revocato. Controlla account e autorizzazioni.";
  return "La sessione non è più utilizzabile. Contatta l’amministratore se il problema persiste.";
}
