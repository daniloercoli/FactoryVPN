import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router";
import { api, page, type Gateway } from "../api/client";
import { useAuth } from "../auth";
import { useGroups } from "../hooks";
import { availabilityNames } from "./VPN";
import { GatewayIdentifiers } from "./GatewayIdentifiers";
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
export default function Gateways() {
  const auth = useAuth();
  const groups = useGroups();
  const client = useQueryClient();
  const [params] = useSearchParams();
  const [group, setGroup] = useState(params.get("group") || "");
  const [q, setQ] = useState("");
  const [availability, setAvailability] = useState("");
  const [mode, setMode] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Gateway | null>(null);
  const [target, setTarget] = useState("");
  const [saved, setSaved] = useState(false);
  const [naming, setNaming] = useState<Gateway | null>(null);
  const [name, setName] = useState("");
  const query = useQuery({
    queryKey: ["gateways", q, group, availability, mode, offset],
    queryFn: ({ signal }) =>
      page<Gateway>(
        "/admin/gateways",
        {
          q,
          group_id: group,
          availability,
          activation_mode: mode,
          limit: 20,
          offset,
        },
        signal,
      ),
    refetchInterval: 10000,
    refetchOnWindowFocus: true,
  });
  const move = useMutation({
    mutationFn: () =>
      api<Gateway>(`/admin/gateways/${selected!.id}/group`, {
        method: "PUT",
        revision: selected!.revision,
        body: { group_id: target || null },
      }),
    onSuccess: async () => {
      setSelected(null);
      setSaved(true);
      await Promise.all(
        ["gateways", "groups", "group-list", "group", "stats", "vpn"].map(
          (key) => client.invalidateQueries({ queryKey: [key] }),
        ),
      );
    },
  });
  const rename = useMutation({
    mutationFn: () =>
      api<Gateway>(`/admin/gateways/${naming!.id}`, {
        method: "PATCH",
        revision: naming!.revision,
        body: { display_name: name || null },
      }),
    onSuccess: async () => {
      setNaming(null);
      await Promise.all(
        ["gateways", "vpn", "audit"].map((key) =>
          client.invalidateQueries({ queryKey: [key] }),
        ),
      );
    },
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
        title="Gateway"
        description="Cerca un impianto, verifica la disponibilità e apri la sua scheda per inventario, credenziali e assistenza."
      />
      {saved && <Notice>Assegnazione aggiornata.</Notice>}
      <section className="card">
        <div className="filters">
          <label className="search-field">
            Cerca gateway
            <input
              value={q}
              maxLength={100}
              placeholder="Nome, matricola, MAC, IMEI, ICCID, telefono o UUID"
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
              aria-label="Gruppo"
              onChange={(e) => {
                setGroup(e.target.value);
                setOffset(0);
              }}
            >
              <option value="">Tutti i gruppi visibili</option>
              {groups.data?.map((g) => (
                <option value={g.id} key={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Disponibilità
            <select
              value={availability}
              aria-label="Disponibilità"
              onChange={(e) => {
                setAvailability(e.target.value);
                setOffset(0);
              }}
            >
              <option value="">Tutte</option>
              {Object.entries(availabilityNames).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Attivazione
            <select
              value={mode}
              aria-label="Attivazione"
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
          <button
            className="button secondary small"
            disabled={query.isFetching}
            onClick={() => void query.refetch()}
          >
            Aggiorna gateway
          </button>
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
                        <th>Gateway / identificativi</th>
                        <th>Gruppo</th>
                        <th>Disponibilità</th>
                        <th>Ultimo contatto</th>
                        <th>Assegnazione</th>
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
                            {g.display_name && (
                              <small className="cell-sub mono">
                                {g.mac_address || "MAC non registrato"}
                              </small>
                            )}
                            <GatewayIdentifiers value={g} />
                            <small className="cell-sub">
                              {g.activation_mode === "wifi" ? "Wi-Fi" : "SMS"}
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
                            <Badge>{availabilityNames[g.availability]}</Badge>
                            <small className="cell-sub">
                              Stato registrato: {g.status}
                            </small>
                          </td>
                          <td className="nowrap muted">{date(g.last_seen)}</td>
                          <td>
                            <button
                              className="button secondary small"
                              onClick={() => {
                                rename.reset();
                                setNaming(g);
                                setName(g.display_name || "");
                              }}
                            >
                              Modifica nome
                            </button>{" "}
                            <button
                              className="button secondary small"
                              disabled={groups.isPending || !!groups.error}
                              onClick={() => {
                                move.reset();
                                setSaved(false);
                                setSelected(g);
                                setTarget(g.group_id || "");
                              }}
                            >
                              Cambia gruppo
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty title="Nessun gateway nel tuo ambito" />
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
      <p className="page-note">
        I gateway non assegnati sono visibili e assegnabili solo agli
        amministratori globali. “Disponibile” indica risorse libere e
        configurazione presente, non conferma che il gateway sia online.
      </p>
      {selected && (
        <Confirm
          title="Cambiare gruppo al gateway?"
          label="Conferma assegnazione"
          onCancel={() => setSelected(null)}
          onConfirm={() => {
            if (target || auth.user!.is_admin) move.mutate();
          }}
          busy={move.isPending}
          error={move.error}
        >
          <p>
            <strong className="mono">
              {selected.display_name || selected.mac_address || selected.id}
            </strong>
          </p>
          <p>
            Lo spostamento richiede che non ci siano sessioni VPN in corso o
            risorse ancora in pulizia. Le vecchie credenziali saranno revocate:
            nel nuovo gruppo occorre preparare e installare un nuovo pacchetto.
          </p>
          <label>
            Gruppo di destinazione
            <select value={target} onChange={(e) => setTarget(e.target.value)}>
              {auth.user!.is_admin && <option value="">Non assegnato</option>}
              {groups.data?.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
          </label>
          <p className="muted">
            In caso di dati modificati da un altro amministratore, chiudi questa
            finestra e ricarica l’elenco.
          </p>
          <button
            className="text-button"
            onClick={() => {
              setSelected(null);
              void query.refetch();
            }}
          >
            Ricarica elenco
          </button>
        </Confirm>
      )}
      {naming && (
        <Confirm
          title="Nome del gateway"
          label="Salva nome"
          onCancel={() => setNaming(null)}
          onConfirm={() => rename.mutate()}
          busy={rename.isPending}
          error={rename.error}
        >
          <p className="mono">{naming.mac_address || naming.id}</p>
          <label>
            Nome descrittivo
            <input
              value={name}
              maxLength={100}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <p>
            Lascia il campo vuoto per usare il MAC. Il nome non cambia la
            connessione né l’assegnazione.
          </p>
          {rename.error && (
            <button
              className="text-button"
              onClick={() => {
                setNaming(null);
                void query.refetch();
              }}
            >
              Ricarica elenco
            </button>
          )}
        </Confirm>
      )}
    </>
  );
}
