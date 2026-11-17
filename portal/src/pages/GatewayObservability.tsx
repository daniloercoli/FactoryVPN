import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import {
  api,
  page,
  downloadCsv,
  type SystemInfo,
  type TechnicalLog,
} from "../api/client";
import { useAuth } from "../auth";
import { APP_NAME } from "../branding";
import { Badge, ErrorBox, Loading, Pager, date } from "../ui";

const names: Record<string, string> = {
  agent: `Agente ${APP_NAME}`,
  openvpn: "OpenVPN",
  system: "Sistema operativo",
  hardware: "Hardware",
  modem: "Modem / SIM",
  wifi: "Interfaccia Wi-Fi",
  network: "Rete",
  api: "API",
  collector: "Raccolta",
  sms: "SMS",
};
const states: Record<string, string> = {
  not_applicable: "Non applicabile",
  unsupported: "Rilevamento non supportato",
  collection_failed: "Rilevamento non riuscito",
  reported: "Rilevato",
};
const fields: Record<string, string> = {
  version: "Versione",
  build: "Build",
  model: "Modello",
  manufacturer: "Produttore",
  revision: "Revisione",
  kernel: "Kernel",
  architecture: "Architettura",
  imei: "IMEI modem",
  iccid: "ICCID SIM",
  interface: "Interfaccia",
  mac_address: "MAC Wi-Fi",
};

export function GatewayComponents({ id }: { id: string }) {
  const query = useQuery({
    queryKey: ["gateways", "system-info", id],
    queryFn: ({ signal }) =>
      api<SystemInfo>(`/gateways/${id}/system-info`, { signal }),
    refetchInterval: 60000,
  });
  const info = query.data;
  const components = info?.components || {};
  return (
    <section
      className="card padded gateway-section"
      aria-label="Componenti e identificativi"
    >
      <h2>Componenti e identificativi</h2>
      <p className="muted">
        Ultimi dati rilevati sul gateway. Le versioni non attestano la presenza
        online o la disponibilità di aggiornamenti.
      </p>
      <ErrorBox error={query.error} reload={() => void query.refetch()} />
      {query.isPending ? (
        <Loading />
      ) : (
        !query.error &&
        info && (
          <>
            {!Object.keys(components).length && (
              <p>
                Non ancora rilevato. Un agente precedente o un gateway in attesa
                SMS può non avere inviato informazioni.
              </p>
            )}
            <div className="component-grid">
              {["modem", "wifi", "agent", "openvpn", "system", "hardware"].map(
                (name) => {
                  const item = components[name];
                  if (!item) return null;
                  const value =
                    item.latest.state === "reported"
                      ? item.latest
                      : item.last_success;
                  return (
                    <article key={name} className="component-item">
                      <h3>{names[name]}</h3>
                      <Badge
                        tone={
                          item.latest.state === "collection_failed" ? "red" : ""
                        }
                      >
                        {states[item.latest.state]}
                      </Badge>
                      {value && (
                        <dl className="vpn-details">
                          {Object.entries(fields).map(([field, label]) => {
                            const text = value[field as keyof typeof value];
                            return text ? (
                              <div key={field}>
                                <dt>{label}</dt>
                                <dd>{String(text)}</dd>
                              </div>
                            ) : null;
                          })}
                        </dl>
                      )}
                      {value?.origin === "configured" && (
                        <p className="muted">
                          Informazione configurata dall’operatore.
                        </p>
                      )}
                      <p className="page-note">
                        Ultimo tentativo: {date(item.observed_at)}
                        {item.last_success_at && (
                          <>
                            {" "}
                            · Ultimo dato rilevato: {date(item.last_success_at)}
                          </>
                        )}
                      </p>
                    </article>
                  );
                },
              )}
            </div>
            {components.system && (
              <p className="page-note">
                Sistema e kernel descrivono l’ambiente Linux visibile
                all’agente, anche quando viene eseguito in un container.
              </p>
            )}
          </>
        )
      )}
    </section>
  );
}

export function GatewayLogs({ id, sms }: { id: string; sms: boolean }) {
  const auth = useAuth();
  const [open, setOpen] = useState(true);
  const [visible, setVisible] = useState(!document.hidden);
  const [paused, setPaused] = useState(false);
  const [filters, setFilters] = useState("");
  const [offset, setOffset] = useState(0);
  useEffect(() => {
    const change = () => setVisible(!document.hidden);
    document.addEventListener("visibilitychange", change);
    return () => document.removeEventListener("visibilitychange", change);
  }, []);
  const base = `/gateways/${id}/technical-logs`;
  const query = useQuery({
    queryKey: ["gateways", "technical-logs", id, filters, offset],
    queryFn: ({ signal }) =>
      page<TechnicalLog>(
        base,
        {
          ...Object.fromEntries(new URLSearchParams(filters)),
          limit: 50,
          offset,
        },
        signal,
      ),
    enabled: auth.can("logs.read") && open && visible,
    refetchInterval: paused || !open || !visible ? false : 15000,
  });
  const download = useMutation({
    mutationFn: () =>
      downloadCsv(
        `${base}.csv?${filters}`,
        "factoryvpn-gateway-technical-logs.csv",
      ),
  });
  if (!auth.can("logs.read")) return null;
  return (
    <details
      className="card padded gateway-section"
      open={open}
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary>
        <strong>Log tecnici</strong>
      </summary>
      <p className="muted">
        Eventi disponibili fino a 30 giorni, con limiti di volume. Lo storico
        può essere parziale; un’interruzione o un riavvio può perdere eventi non
        consegnati.
      </p>
      {sms && (
        <p className="page-note">
          Durante l’attesa SMS non vengono inviati log periodici. Gli eventi in
          coda arriveranno alla prossima attivazione, entro i limiti di
          conservazione.
        </p>
      )}
      <form
        className="filters technical-log-filters"
        onSubmit={(e) => {
          e.preventDefault();
          const form = new FormData(e.currentTarget);
          const params = new URLSearchParams();
          for (const [key, raw] of form) {
            const value = String(raw).trim();
            if (value)
              params.set(
                key,
                ["since", "until"].includes(key)
                  ? new Date(value).toISOString()
                  : value,
              );
          }
          setOffset(0);
          setFilters(params.toString());
          download.reset();
        }}
      >
        <label>
          Ricerca
          <input name="q" maxLength={100} placeholder="Messaggio o codice" />
        </label>
        <label>
          Gravità
          <select name="severity">
            <option value="">Tutte</option>
            <option value="info">Informazione</option>
            <option value="warning">Avviso</option>
            <option value="error">Errore</option>
          </select>
        </label>
        <label>
          Origine
          <select name="source">
            <option value="">Tutte</option>
            <option value="agent">Agente</option>
            <option value="worker">Worker VPN</option>
            <option value="api">API</option>
          </select>
        </label>
        <label>
          Componente
          <select name="component">
            <option value="">Tutti</option>
            {[
              "agent",
              "openvpn",
              "network",
              "api",
              "sms",
              "modem",
              "collector",
            ].map((c) => (
              <option key={c} value={c}>
                {names[c] || c}
              </option>
            ))}
          </select>
        </label>
        <label>
          Sessione
          <input name="session_id" placeholder="UUID sessione" />
        </label>
        <label>
          Codice
          <input name="code" maxLength={40} />
        </label>
        <label>
          Ricevuti dal
          <input name="since" type="datetime-local" />
        </label>
        <label>
          Ricevuti fino al
          <input name="until" type="datetime-local" />
        </label>
        <button className="button" type="submit">
          Filtra log
        </button>
      </form>
      <div className="vpn-actions">
        <button className="button secondary" onClick={() => setPaused(!paused)}>
          {paused ? "Riprendi aggiornamento" : "Sospendi aggiornamento"}
        </button>
        <button
          className="button secondary"
          onClick={() => void query.refetch()}
        >
          Aggiorna log
        </button>
        <button
          className="button secondary"
          disabled={download.isPending || !!query.error || !query.data?.total}
          onClick={() => download.mutate()}
        >
          Esporta CSV
        </button>
      </div>
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
            {!query.data.total ? (
              <p>
                Nessun evento nell’intervallo selezionato. L’assenza di eventi
                non certifica il funzionamento del gateway.
              </p>
            ) : (
              <div className="table-scroll">
                <table className="technical-log-table">
                  <thead>
                    <tr>
                      <th>Ricezione</th>
                      <th>Origine</th>
                      <th>Gravità</th>
                      <th>Evento</th>
                      <th>Sessione</th>
                    </tr>
                  </thead>
                  <tbody>
                    {query.data.items.map((row) => (
                      <tr key={row.id}>
                        <td>
                          {date(row.received_at)}
                          <small className="muted">
                            Rilevato: {date(row.observed_at)}
                          </small>
                        </td>
                        <td>
                          {
                            {
                              agent: "Agente",
                              worker: "Worker VPN",
                              api: "API",
                            }[row.source]
                          }
                          <small className="muted">
                            {names[row.component] || row.component}
                          </small>
                        </td>
                        <td>
                          <Badge tone={row.severity === "error" ? "red" : ""}>
                            {{
                              info: "Informazione",
                              warning: "Avviso",
                              error: "Errore",
                            }[row.severity as "info" | "warning" | "error"] ||
                              row.severity}
                          </Badge>
                        </td>
                        <td>
                          {row.message}
                          <small className="mono">{row.code}</small>
                          {row.occurrences > 1 && (
                            <small>
                              {row.occurrences} occorrenze dal{" "}
                              {date(row.first_observed_at)}
                            </small>
                          )}
                          {Object.entries(row.parameters)
                            .filter(([, v]) => v != null)
                            .map(([key, value]) => (
                              <small key={key}>
                                {key}: {value}
                              </small>
                            ))}
                        </td>
                        <td>
                          {row.session_id ? (
                            <Link to={`/vpn/${row.session_id}`}>
                              Apri sessione
                            </Link>
                          ) : (
                            "—"
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <Pager
              offset={offset}
              size={50}
              total={query.data.total}
              onChange={setOffset}
            />
          </>
        )
      )}
    </details>
  );
}
