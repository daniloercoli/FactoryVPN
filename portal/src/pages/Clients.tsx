import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router";
import { api, page, type VPNClient } from "../api/client";
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
import { statusNames } from "./VPN";

const clock = () => ({ monotonic: performance.now(), wall: Date.now() });

function duration(session: VPNClient, now: number) {
  if (!session.requested_at) return "—";
  const end = session.ended_at ? Date.parse(session.ended_at) : now;
  const minutes = Math.max(
    0,
    Math.floor((end - Date.parse(session.requested_at)) / 60000),
  );
  return minutes < 60
    ? `${minutes} min`
    : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

function Connection({
  session,
  elapsed,
}: {
  session: VPNClient;
  elapsed: number;
}) {
  if (!["REQUESTED", "ACTIVE"].includes(session.status))
    return <span className="muted">Sessione terminata</span>;
  const fresh =
    !!session.observation_valid_until &&
    elapsed <
      Date.parse(session.observation_valid_until) -
        Date.parse(session.diagnostics.generated_at);
  return (
    <div className="client-connection">
      {[
        ["technician", "Client"],
        ["tunnel", "Gateway"],
        ["network", "Rete"],
      ].map(([id, label]) => {
        const step = session.diagnostics.steps.find((s) => s.id === id);
        const state = fresh ? step?.status : "unknown";
        const message =
          state === "ok"
            ? id === "network"
              ? "Pronta"
              : "Connesso"
            : state === "waiting"
              ? id === "network"
                ? "In preparazione"
                : "Non collegato"
              : state === "error"
                ? "Errore"
                : "Non verificabile";
        return (
          <div
            key={id}
            title={
              fresh
                ? step?.message
                : "Nessuna osservazione recente del collegamento."
            }
          >
            <span>{label}</span>{" "}
            <Badge
              tone={state === "ok" ? "green" : state === "error" ? "red" : ""}
            >
              {message}
            </Badge>
          </div>
        );
      })}
    </div>
  );
}

export default function Clients() {
  const groups = useGroups();
  const client = useQueryClient();
  const [now, setNow] = useState(clock);
  useEffect(() => {
    const update = () => setNow(clock());
    const timer = setInterval(update, 1000);
    window.addEventListener("focus", update);
    document.addEventListener("visibilitychange", update);
    return () => {
      clearInterval(timer);
      window.removeEventListener("focus", update);
      document.removeEventListener("visibilitychange", update);
    };
  }, []);
  const [params, setParams] = useSearchParams();
  const q = params.get("q") || "";
  const group = params.get("group") || "";
  const status = params.get("status") || "";
  const view = params.get("view") || "reserved";
  const offset = Math.max(0, Number(params.get("offset")) || 0);
  const [selected, setSelected] = useState<VPNClient | null>(null);
  const [closed, setClosed] = useState(false);
  function filter(key: string, value: string) {
    setParams(
      (previous) => {
        const next = new URLSearchParams(previous);
        if (value) next.set(key, value);
        else next.delete(key);
        if (key !== "offset") next.delete("offset");
        return next;
      },
      { replace: true },
    );
  }
  const query = useQuery({
    queryKey: ["vpn", "clients", q, group, status, view, offset],
    queryFn: async ({ signal }) => {
      const requestedAt = clock();
      const result = await page<VPNClient>(
        "/admin/sessions",
        {
          q,
          group_id: group,
          status_filter: status,
          reserved: view === "reserved" ? true : undefined,
          cleanup_pending: view === "cleanup" ? true : undefined,
          limit: 20,
          offset,
        },
        signal,
      );
      return { ...result, requestedAt };
    },
    refetchInterval: 5000,
    refetchOnWindowFocus: true,
  });
  // Count request time too. Monotonic time survives backward wall-clock changes;
  // wall time covers sleep on browsers that pause the monotonic clock.
  const elapsed = query.data
    ? Math.max(
        0,
        now.monotonic - query.data.requestedAt.monotonic,
        now.wall - query.data.requestedAt.wall,
      )
    : 0;
  const close = useMutation({
    mutationFn: (session: VPNClient) =>
      api(`/admin/sessions/${session.id}`, { method: "DELETE" }),
    onSuccess: async () => {
      setSelected(null);
      setClosed(true);
      await Promise.all(
        ["vpn", "stats"].map((key) =>
          client.invalidateQueries({ queryKey: [key] }),
        ),
      );
    },
  });
  return (
    <>
      <PageTitle
        title="Client VPN"
        description="Supervisiona le sessioni di assistenza nei tuoi gruppi e verifica i collegamenti osservati dal server."
        action={
          <Link className="button secondary" to="/vpn-history">
            Cronologia ed esportazione
          </Link>
        }
      />
      {closed && (
        <Notice>
          Chiusura richiesta. Le risorse restano riservate finché il server
          conferma il rilascio.
        </Notice>
      )}
      <section className="card">
        <div className="card-heading">
          <div>
            <h2>Sessioni dei tecnici</h2>
            <p className="muted">
              Aggiornamento ogni 5 secondi. L’accesso al portale non indica una
              connessione VPN.
            </p>
            {!!query.dataUpdatedAt && !query.error && (
              <small className="muted">
                Ultimo aggiornamento:{" "}
                {new Date(query.dataUpdatedAt).toLocaleTimeString("it-IT")}
              </small>
            )}
          </div>
          <button
            className="button secondary small"
            onClick={() => void query.refetch()}
            disabled={query.isFetching}
          >
            Aggiorna client
          </button>
        </div>
        <div className="filters">
          <label className="search-field">
            Cerca client
            <input
              value={q}
              maxLength={100}
              placeholder="Tecnico, gateway o UUID sessione"
              onChange={(e) => filter("q", e.target.value)}
            />
          </label>
          <label>
            Gruppo sessione
            <select
              value={group}
              aria-label="Gruppo sessione"
              onChange={(e) => filter("group", e.target.value)}
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
            Mostra
            <select
              value={view}
              aria-label="Mostra"
              onChange={(e) => filter("view", e.target.value)}
            >
              <option value="reserved">In corso e in pulizia</option>
              <option value="cleanup">Risorse in pulizia</option>
              <option value="all">Tutte le sessioni</option>
            </select>
          </label>
          <label>
            Stato sessione
            <select
              value={status}
              aria-label="Stato sessione"
              onChange={(e) => filter("status", e.target.value)}
            >
              <option value="">Tutti</option>
              {Object.entries(statusNames).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
        </div>
        <ErrorBox
          error={query.error || groups.error}
          reload={() => {
            void query.refetch();
            void groups.refetch();
          }}
        />
        {query.isPending ? (
          <Loading />
        ) : (
          !query.error &&
          query.data && (
            <>
              {query.data.items.length ? (
                <div className="table-scroll">
                  <table className="clients-table">
                    <thead>
                      <tr>
                        <th>Tecnico</th>
                        <th>Gateway / gruppo</th>
                        <th>Collegamento osservato</th>
                        <th>Sessione</th>
                        <th>Durata e scadenza</th>
                        <th>Azioni</th>
                      </tr>
                    </thead>
                    <tbody>
                      {query.data.items.map((s) => {
                        const name =
                          s.technician_full_name ||
                          s.technician_username ||
                          (s.technician_id
                            ? "Identità fuori ambito"
                            : "Account rimosso");
                        const gateway =
                          s.gateway_display_name || s.gateway_mac_address;
                        return (
                          <tr key={s.id}>
                            <td>
                              {s.technician_manageable ? (
                                <Link to={`/users/${s.technician_id}`}>
                                  <strong>{name}</strong>
                                </Link>
                              ) : (
                                <strong>{name}</strong>
                              )}
                              {s.technician_username && (
                                <small className="cell-sub">
                                  {s.technician_username}
                                </small>
                              )}
                              <small className="cell-sub mono">
                                IP assegnato: {s.technician_vpn_ip || "—"}
                              </small>
                            </td>
                            <td>
                              {gateway ? (
                                <Link to={`/gateways/${s.gateway_id}`}>
                                  <strong>{gateway}</strong>
                                </Link>
                              ) : (
                                <strong>Gateway non disponibile</strong>
                              )}
                              <small className="cell-sub">
                                {groups.data?.find((g) => g.id === s.group_id)
                                  ?.name || "Gruppo non disponibile"}
                              </small>
                              <small className="cell-sub mono">
                                IP assegnato: {s.gateway_vpn_ip || "—"}
                              </small>
                            </td>
                            <td>
                              <Connection session={s} elapsed={elapsed} />
                            </td>
                            <td>
                              <Badge>{statusNames[s.status]}</Badge>
                              {s.cleanup_pending && (
                                <small className="cell-sub">
                                  Rilascio delle risorse
                                </small>
                              )}
                              <small className="cell-sub mono">{s.id}</small>
                              <small className="cell-sub">
                                {s.activation_mode === "wifi" ? "Wi-Fi" : "SMS"}
                              </small>
                            </td>
                            <td>
                              <strong>
                                {duration(
                                  s,
                                  Date.parse(s.diagnostics.generated_at) +
                                    elapsed,
                                )}
                              </strong>
                              <small className="cell-sub">
                                Dalla richiesta: {date(s.requested_at)}
                              </small>
                              <small className="cell-sub">
                                Scadenza:{" "}
                                {s.expires_at ? date(s.expires_at) : "—"}
                              </small>
                            </td>
                            <td>
                              <div className="client-actions">
                                <Link
                                  className="button secondary small"
                                  aria-label={`Dettaglio sessione ${s.id}`}
                                  to={`/vpn/${s.id}`}
                                >
                                  Diagnostica
                                </Link>
                                {["REQUESTED", "ACTIVE"].includes(s.status) && (
                                  <button
                                    className="button danger small"
                                    aria-label={`Chiudi sessione ${s.id}`}
                                    onClick={() => {
                                      close.reset();
                                      setClosed(false);
                                      setSelected(s);
                                    }}
                                  >
                                    Chiudi sessione
                                  </button>
                                )}
                              </div>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty title="Nessuna sessione con questi filtri" />
              )}
              <Pager
                total={query.data.total}
                offset={offset}
                onChange={(value) => filter("offset", String(value))}
              />
            </>
          )
        )}
      </section>
      <p className="page-note">
        Lo stato del collegamento scade dopo 30 secondi senza osservazioni. Gli
        IP sono quelli assegnati alla sessione; la raggiungibilità dei PLC va
        verificata nell’intervento. Le identità fuori dal tuo ambito sono
        mascherate.
      </p>
      {selected && (
        <Confirm
          title="Chiudere la sessione VPN?"
          label="Conferma chiusura"
          busy={close.isPending}
          error={close.error}
          onCancel={() => setSelected(null)}
          onConfirm={() => close.mutate(selected)}
        >
          <p>
            L’operazione interrompe l’assistenza del tecnico su{" "}
            <strong>
              {selected.gateway_display_name ||
                selected.gateway_mac_address ||
                "questo gateway"}
            </strong>
            .
          </p>
          <p className="mono">{selected.id}</p>
          <p>
            Il server chiuderà i collegamenti e rilascerà le risorse. La
            chiusura viene registrata nella cronologia.
          </p>
        </Confirm>
      )}
    </>
  );
}
