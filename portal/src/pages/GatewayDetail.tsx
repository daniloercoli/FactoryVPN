import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router";
import {
  api,
  ApiError,
  page,
  type Device,
  type DeviceInput,
  type GatewayDetail as Detail,
} from "../api/client";
import { useAuth } from "../auth";
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
import { availabilityNames, blockNames, VPNStartDialog } from "./VPN";
import { VPNProfile } from "./Provisioning";
import { GatewayTelemetry } from "./Telemetry";
import { GatewayComponents, GatewayLogs } from "./GatewayObservability";

async function refreshGateway(client: ReturnType<typeof useQueryClient>) {
  await Promise.all(
    [
      "gateways",
      "vpn",
      "groups",
      "group-list",
      "group",
      "audit",
      "stats",
      "telemetry",
    ].map((key) => client.invalidateQueries({ queryKey: [key] })),
  );
}

export default function GatewayDetail() {
  const { id } = useParams();
  return <GatewayCard key={id} id={id!} />;
}

function GatewayCard({ id }: { id: string }) {
  const auth = useAuth();
  const groups = useGroups();
  const [opening, setOpening] = useState<{
    gateway: Detail;
    key: string;
  } | null>(null);
  const query = useQuery({
    queryKey: ["gateways", "detail", id],
    queryFn: ({ signal }) =>
      api<Detail>(`/gateways/${encodeURIComponent(id)}`, { signal }),
    refetchInterval: 10000,
    refetchOnWindowFocus: true,
  });
  if (query.isPending) return <Loading />;
  if (query.error || !query.data)
    return (
      <>
        <PageTitle
          title="Gateway non disponibile"
          description="La scheda non è disponibile o non è più nel tuo ambito."
          back="/vpn"
        />
        <ErrorBox error={query.error} reload={() => void query.refetch()} />
      </>
    );
  const g = query.data;
  const current = g.current_session;
  const sessionState = current?.cleanup_pending
    ? "Rilascio delle risorse"
    : current?.expires_at && Date.parse(current.expires_at) <= Date.now()
      ? "Verifica della scadenza"
      : current && !current.activation_received_at
        ? current.activation_mode === "sms"
          ? "Attesa ricezione SMS"
          : "Attesa del gateway Wi-Fi"
        : current?.status === "ACTIVE"
          ? "Sessione attiva"
          : "Preparazione della sessione";
  const fields = [
    [
      "Gruppo",
      groups.data?.find((group) => group.id === g.group_id)?.name ||
        (g.group_id ? "Gruppo non disponibile" : "Non assegnato"),
    ],
    ["Modalità di attivazione", g.activation_mode === "sms" ? "SMS" : "Wi-Fi"],
    ["MAC registrato", g.mac_address || "Non registrato"],
    ["Numero di serie", g.serial_number || "Non registrato"],
    ["Telefono", g.phone_number || "Non configurato"],
    ["Rete LAN", g.lan_cidr || "Non configurata"],
    ["Indirizzo LAN del gateway", g.lan_address || "Non registrato"],
    ["Interfaccia LAN", g.lan_interface || "Non registrata"],
    ["Indirizzo VPN", g.vpn_ip || "Non assegnato"],
    [
      "Stato dichiarato",
      (
        {
          ACTIVE: "Attivo",
          INACTIVE: "Inattivo",
          PENDING: "In attesa",
        } as Record<string, string>
      )[g.status] || g.status,
    ],
    ["Ultimo contatto", date(g.last_seen)],
    ["Registrato il", g.created_at ? date(g.created_at) : "Non disponibile"],
  ];
  return (
    <>
      <PageTitle
        title={g.display_name || g.mac_address || g.id}
        description="Scheda gateway · rete, assistenza e inventario dell’impianto"
        back="/vpn"
        action={
          <button
            className="button secondary"
            disabled={query.isFetching}
            onClick={() => void query.refetch()}
          >
            Aggiorna scheda
          </button>
        }
      />
      <section className="card padded" aria-label="Stato e rete del gateway">
        <div className="gateway-state">
          <div>
            <p className="eyebrow">DISPONIBILITÀ</p>
            <h2>
              {!g.group_id
                ? "Non assegnato"
                : availabilityNames[g.availability]}
            </h2>
          </div>
          <Badge tone={g.availability === "available" ? "blue" : ""}>
            {g.activation_mode === "sms"
              ? "Attivazione SMS"
              : "Attivazione Wi-Fi"}
          </Badge>
        </div>
        <p className="muted">
          La disponibilità consente una richiesta VPN; non dimostra che il
          gateway o i PLC siano raggiungibili. Lo stato si aggiorna ogni 10
          secondi.
        </p>
        <dl className="vpn-details">
          {fields.map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
        <p className="page-note mono">ID: {g.id}</p>
        {auth.can("gateways.manage") && <GatewaySettings gateway={g} />}
      </section>
      <section
        className="card padded gateway-section"
        aria-label="Assistenza sul gateway"
      >
        <h2>Assistenza VPN</h2>
        {current ? (
          <>
            <p>
              <Badge>{sessionState}</Badge>
            </p>
            <p className="muted">
              Scadenza: {date(current.expires_at)}. Apri la sessione per
              verificare tunnel, client e rete, oppure chiuderla.
            </p>
            <Link className="button" to={`/vpn/${current.id}`}>
              Segui sessione
            </Link>
          </>
        ) : (
          <>
            <p>
              {g.availability === "busy" || g.availability === "cleanup_pending"
                ? "Il gateway ha una sessione riservata. I dettagli sono visibili al proprietario o a chi gestisce le sessioni."
                : "Nessuna sessione in corso su questo gateway."}
            </p>
            {auth.can("connect") && (
              <button
                className="button"
                disabled={!g.can_start || query.isFetching}
                onClick={() =>
                  setOpening({ gateway: g, key: crypto.randomUUID() })
                }
              >
                Apri sessione VPN
              </button>
            )}
            {g.start_block_reason && (
              <p className="muted">{blockNames[g.start_block_reason]}</p>
            )}
          </>
        )}
        <div className="vpn-actions">
          <Link to={`/vpn-history?gateway_id=${g.id}`}>
            {auth.can("sessions.manage")
              ? "Cronologia interventi"
              : "Cronologia dei tuoi interventi"}
          </Link>
          {auth.can("audit.read") && (
            <Link to={`/audit?gateway_id=${g.id}`}>Audit del gateway</Link>
          )}
        </div>
        <p className="page-note">
          Il client OpenVPN si avvia manualmente. Logout e scadenza dell’accesso
          chiudono le VPN avviate da quello stesso accesso.
        </p>
      </section>
      {auth.can("gateways.provision") && (
        <VPNProfile kind="gateway" peerId={id} />
      )}
      <GatewayComponents id={id} />
      <GatewayLogs key={id} id={id} sms={g.activation_mode === "sms"} />
      <Inventory gateway={g} />
      {auth.can("telemetry.read") && <GatewayTelemetry id={id} />}
      {opening && (
        <VPNStartDialog selection={opening} onClose={() => setOpening(null)} />
      )}
    </>
  );
}

function GatewaySettings({ gateway }: { gateway: Detail }) {
  const auth = useAuth();
  const groups = useGroups();
  const client = useQueryClient();
  const [draft, setDraft] = useState<{
    kind: "name" | "group";
    snapshot: Detail;
    value: string;
  } | null>(null);
  const mutation = useMutation({
    mutationFn: (data: NonNullable<typeof draft>) =>
      api(
        `/admin/gateways/${data.snapshot.id}${data.kind === "group" ? "/group" : ""}`,
        {
          method: data.kind === "group" ? "PUT" : "PATCH",
          revision: data.snapshot.revision,
          body:
            data.kind === "group"
              ? { group_id: data.value || null }
              : { display_name: data.value || null },
        },
      ),
    onSuccess: async () => {
      setDraft(null);
      await refreshGateway(client);
    },
  });
  const edit = (kind: "name" | "group") => {
    mutation.reset();
    setDraft({
      kind,
      snapshot: gateway,
      value: (kind === "name" ? gateway.display_name : gateway.group_id) || "",
    });
  };
  return (
    <>
      <div className="vpn-actions">
        <button className="button secondary small" onClick={() => edit("name")}>
          Modifica nome
        </button>
        <button
          className="button secondary small"
          disabled={groups.isPending || !!groups.error}
          onClick={() => edit("group")}
        >
          Cambia gruppo
        </button>
      </div>
      <ErrorBox error={groups.error} reload={() => void groups.refetch()} />
      {draft && (
        <Confirm
          title={
            draft.kind === "name"
              ? "Nome del gateway"
              : "Cambiare gruppo al gateway?"
          }
          label="Salva modifiche"
          onCancel={() => setDraft(null)}
          busy={mutation.isPending}
          error={mutation.error}
          onConfirm={() => {
            if (draft.kind === "name" || draft.value || auth.user?.is_admin)
              mutation.mutate(draft);
          }}
        >
          {draft.kind === "name" ? (
            <label>
              Nome descrittivo
              <input
                value={draft.value}
                maxLength={100}
                onChange={(e) => setDraft({ ...draft, value: e.target.value })}
              />
            </label>
          ) : (
            <>
              <label>
                Gruppo di destinazione
                <select
                  value={draft.value}
                  onChange={(e) =>
                    setDraft({ ...draft, value: e.target.value })
                  }
                >
                  {auth.user?.is_admin && (
                    <option value="">Non assegnato</option>
                  )}
                  {groups.data?.map((g) => (
                    <option key={g.id} value={g.id}>
                      {g.name}
                    </option>
                  ))}
                </select>
              </label>
              <p>
                Il trasferimento cambia l’accesso alla scheda e all’inventario.
                Richiede la chiusura delle VPN e il completamento della pulizia.
                Le vecchie credenziali saranno revocate: nel nuovo gruppo
                occorre preparare e installare un nuovo pacchetto.
              </p>
            </>
          )}
          {mutation.error && (
            <button
              className="text-button"
              onClick={() => {
                setDraft(null);
                void refreshGateway(client);
              }}
            >
              Ricarica scheda
            </button>
          )}
        </Confirm>
      )}
    </>
  );
}

const deviceTypes = {
  PLC: "PLC",
  HMI: "HMI",
  SERVER: "Server",
  OTHER: "Altro",
};
type Edit =
  | { kind: "add"; revision: string }
  | { kind: "edit" | "delete"; device: Device };

function Inventory({ gateway }: { gateway: Detail }) {
  const auth = useAuth();
  const client = useQueryClient();
  const [q, setQ] = useState("");
  const [type, setType] = useState("");
  const [offset, setOffset] = useState(0);
  const [draft, setDraft] = useState<Edit | null>(null);
  const form = useRef<HTMLFormElement>(null);
  const query = useQuery({
    queryKey: ["gateways", "inventory", gateway.id, q, type, offset],
    queryFn: ({ signal }) =>
      page<Device>(
        `/devices/gateway/${gateway.id}`,
        { q, device_type: type, limit: 20, offset },
        signal,
      ),
    refetchInterval: 15000,
    refetchOnWindowFocus: true,
  });
  const accessLost =
    query.error instanceof ApiError &&
    [401, 403, 404].includes(query.error.status);
  useEffect(() => {
    // Do not resurrect a stale form if access later returns. Temporary read
    // failures, however, must not unmount uncontrolled inputs and erase edits.
    if (accessLost) setDraft(null);
  }, [accessLost]);
  const mutation = useMutation({
    mutationFn: ({ edit, body }: { edit: Edit; body?: DeviceInput }) =>
      api(
        `/admin/gateways/${gateway.id}/devices${edit.kind === "add" ? "" : "/" + edit.device.id}`,
        {
          method:
            edit.kind === "add"
              ? "POST"
              : edit.kind === "edit"
                ? "PUT"
                : "DELETE",
          revision: edit.kind === "add" ? edit.revision : edit.device.revision,
          body,
        },
      ),
    onSuccess: async () => {
      setDraft(null);
      setOffset(0);
      await refreshGateway(client);
    },
  });
  const edit = (value: Edit) => {
    mutation.reset();
    setDraft(value);
  };
  const save = () => {
    if (!draft || mutation.isPending) return;
    if (draft.kind === "delete") {
      mutation.mutate({ edit: draft });
      return;
    }
    if (!form.current?.reportValidity()) return;
    const values = new FormData(form.current);
    mutation.mutate({
      edit: draft,
      body: {
        name: String(values.get("name")),
        ip_address: String(values.get("ip_address")),
        mac_address: String(values.get("mac_address")),
        device_type: String(
          values.get("device_type"),
        ) as DeviceInput["device_type"],
      },
    });
  };
  const device = draft && draft.kind !== "add" ? draft.device : null;
  return (
    <section
      className="card gateway-section"
      aria-label="Inventario dispositivi"
    >
      <div className="card-heading gateway-inventory-heading">
        <h2>Inventario dispositivi</h2>
        {auth.can("gateways.manage") && (
          <button
            className="button small"
            disabled={!!query.error || query.isPending}
            onClick={() => edit({ kind: "add", revision: gateway.revision })}
          >
            Aggiungi dispositivo
          </button>
        )}
      </div>
      <div className="gateway-inventory-note">
        <Notice>
          Questo elenco descrive PLC, HMI e altri dispositivi dell’impianto.
          Aggiungere o modificare una voce non configura il dispositivo e non ne
          verifica la raggiungibilità.
        </Notice>
      </div>
      <div className="filters">
        <label className="search-field">
          Cerca dispositivi
          <input
            value={q}
            maxLength={100}
            placeholder="Nome, indirizzo IP o MAC"
            onChange={(e) => {
              setQ(e.target.value);
              setOffset(0);
            }}
          />
        </label>
        <label>
          Tipo dispositivo
          <select
            value={type}
            onChange={(e) => {
              setType(e.target.value);
              setOffset(0);
            }}
          >
            <option value="">Tutti i tipi</option>
            {Object.entries(deviceTypes).map(([key, name]) => (
              <option key={key} value={key}>
                {name}
              </option>
            ))}
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
                      <th>Dispositivo</th>
                      <th>Tipo</th>
                      <th>Indirizzo IP</th>
                      <th>MAC</th>
                      <th>Ultimo avvistamento registrato</th>
                      {auth.can("gateways.manage") && <th>Azioni</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {query.data.items.map((d) => (
                      <tr key={d.id}>
                        <td>
                          <strong>{d.name || "Senza nome"}</strong>
                        </td>
                        <td>
                          {deviceTypes[
                            d.device_type as keyof typeof deviceTypes
                          ] || d.device_type}
                        </td>
                        <td className="mono">
                          {d.ip_address || "Non registrato"}
                        </td>
                        <td className="mono">
                          {d.mac_address || "Non registrato"}
                        </td>
                        <td>{date(d.last_seen)}</td>
                        {auth.can("gateways.manage") && (
                          <td>
                            <div className="gateway-row-actions">
                              <button
                                className="button secondary small"
                                aria-label={`Modifica ${d.name || "dispositivo"}`}
                                onClick={() =>
                                  edit({ kind: "edit", device: d })
                                }
                              >
                                Modifica
                              </button>
                              <button
                                className="button secondary small"
                                aria-label={`Rimuovi ${d.name || "dispositivo"}`}
                                onClick={() =>
                                  edit({ kind: "delete", device: d })
                                }
                              >
                                Rimuovi
                              </button>
                            </div>
                          </td>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty
                title={
                  q || type
                    ? "Nessun dispositivo con questi filtri"
                    : "Inventario vuoto"
                }
              >
                I dispositivi possono essere inseriti da chi gestisce questo
                gateway.
              </Empty>
            )}
            <Pager
              total={query.data.total}
              offset={offset}
              onChange={setOffset}
            />
          </>
        )
      )}
      {!accessLost && auth.can("gateways.manage") && draft && (
        <Confirm
          title={
            draft.kind === "delete"
              ? "Rimuovere il dispositivo?"
              : draft.kind === "edit"
                ? "Modifica dispositivo"
                : "Nuovo dispositivo"
          }
          label={
            draft.kind === "delete"
              ? "Rimuovi dall’inventario"
              : "Salva dispositivo"
          }
          danger={draft.kind === "delete"}
          onCancel={() => setDraft(null)}
          onConfirm={save}
          busy={mutation.isPending}
          error={mutation.error}
        >
          {query.error && (
            <p className="notice error" role="alert">
              Non è stato possibile aggiornare l’inventario. Le modifiche nel
              modulo sono conservate.
            </p>
          )}
          {draft.kind === "delete" ? (
            <p>
              Rimuovere <strong>{device?.name || "il dispositivo"}</strong>{" "}
              dall’inventario? Il dispositivo fisico e la sessione VPN non
              vengono modificati.
            </p>
          ) : (
            <form
              ref={form}
              onSubmit={(e) => {
                e.preventDefault();
                save();
              }}
            >
              <label>
                Nome dispositivo
                <input
                  name="name"
                  defaultValue={device?.name || ""}
                  required
                  maxLength={100}
                  autoFocus
                />
              </label>
              <label>
                Indirizzo IPv4
                <input
                  name="ip_address"
                  defaultValue={device?.ip_address || ""}
                  required
                  maxLength={15}
                  placeholder="192.168.1.25"
                />
              </label>
              <label>
                Indirizzo MAC
                <input
                  name="mac_address"
                  defaultValue={device?.mac_address || ""}
                  required
                  pattern="[0-9A-Fa-f]{2}((:|-)[0-9A-Fa-f]{2}){5}"
                  placeholder="AA:BB:CC:DD:EE:FF"
                />
              </label>
              <label>
                Tipo
                <select
                  name="device_type"
                  defaultValue={device?.device_type || "OTHER"}
                >
                  {Object.entries(deviceTypes).map(([key, name]) => (
                    <option key={key} value={key}>
                      {name}
                    </option>
                  ))}
                </select>
              </label>
            </form>
          )}
          {mutation.error && (
            <button
              className="text-button"
              onClick={() => {
                setDraft(null);
                void refreshGateway(client);
              }}
            >
              Ricarica inventario
            </button>
          )}
        </Confirm>
      )}
    </section>
  );
}
