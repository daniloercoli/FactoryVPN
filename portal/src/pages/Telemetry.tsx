import { useEffect, useState } from "react";
import { Link } from "react-router";
import MetricsHistory from "./MetricsHistory";
import { useQuery } from "@tanstack/react-query";
import { api, page, type Telemetry } from "../api/client";
import { useAuth } from "../auth";
import { useGroups } from "../hooks";
import {
  Badge,
  Empty,
  ErrorBox,
  Loading,
  Notice,
  PageTitle,
  Pager,
  date,
} from "../ui";

const serviceNames: Record<string, string> = {
  api: "API",
  database: "Database",
  worker: "Worker VPN",
  openvpn: "OpenVPN",
  agent: "Agente gateway",
  control: "Controllo VPN",
  network: "Configurazione rete",
};
const serviceStates = {
  ok: "Operativo",
  error: "Problema segnalato",
  idle: "In attesa",
  unknown: "Non verificabile",
};
export const clock = () => ({ monotonic: performance.now(), wall: Date.now() });
function bytes(value: number | null | undefined) {
  if (value == null) return "Non disponibile";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index++;
  }
  return (
    new Intl.NumberFormat("it-IT", { maximumFractionDigits: 1 }).format(value) +
    " " +
    units[index]
  );
}
function Meter({
  title,
  percent,
  detail,
}: {
  title: string;
  percent: number | null;
  detail: string;
}) {
  return (
    <div className="telemetry-meter">
      <div>
        <strong>{title}</strong>
        <span>
          {percent == null ? "Non verificabile" : `${Math.round(percent)}%`}
        </span>
      </div>
      {percent != null && (
        <progress
          max={100}
          value={percent}
          aria-label={title}
          className={percent >= 90 ? "high" : ""}
        />
      )}
      <small className="muted">{detail}</small>
    </div>
  );
}
function utilization(total?: number | null, available?: number | null) {
  return total && available != null
    ? ((total - available) / total) * 100
    : null;
}
function Snapshot({
  value,
  requestedAt,
  now,
  server = false,
  link = true,
}: {
  value: Telemetry;
  requestedAt: ReturnType<typeof clock>;
  now: ReturnType<typeof clock>;
  server?: boolean;
  link?: boolean;
}) {
  // Use the API lifetime and relative elapsed time, without comparing server and
  // browser clocks. Monotonic time covers backward clock changes; wall time covers
  // sleep on browsers whose monotonic clock pauses. Slow responses consume lifetime.
  const elapsed =
    Math.max(
      0,
      now.monotonic - requestedAt.monotonic,
      now.wall - requestedAt.wall,
    ) / 1000;
  const freshness =
    value.freshness === "fresh" && elapsed >= value.valid_for_seconds
      ? "stale"
      : value.freshness;
  const m = freshness === "fresh" ? value.metrics : null;
  return (
    <article className="card padded telemetry-node">
      <div className="telemetry-heading">
        <div>
          <h2>
            {value.display_name ||
              value.mac_address ||
              value.gateway_id ||
              "Gateway"}
          </h2>
          {value.mac_address && (
            <small className="muted mono">{value.mac_address}</small>
          )}
        </div>
        <Badge tone={freshness === "fresh" ? "green" : ""}>
          {freshness === "fresh"
            ? "Dati recenti"
            : freshness === "stale"
              ? "Dati scaduti"
              : "Mai ricevuti"}
        </Badge>
      </div>
      {freshness !== "fresh" && (
        <Notice>
          {value.activation_mode === "sms"
            ? "Nessun campione recente. Un gateway SMS può essere in attesa di attivazione; questo non prova un guasto."
            : "Le misure correnti non sono verificabili. Controllare connettività e collector."}
        </Notice>
      )}
      <div className="telemetry-meters">
        <Meter
          title="CPU"
          percent={m?.cpu_percent ?? null}
          detail="Utilizzo medio tra due campioni del sistema Linux"
        />
        <Meter
          title="RAM utilizzata"
          percent={utilization(
            m?.memory_total_bytes,
            m?.memory_available_bytes,
          )}
          detail={
            m?.memory_total_bytes
              ? `${bytes(m.memory_available_bytes)} disponibili su ${bytes(m.memory_total_bytes)}`
              : "Memoria disponibile non misurabile"
          }
        />
        <Meter
          title="Disco utilizzato"
          percent={utilization(m?.disk_total_bytes, m?.disk_available_bytes)}
          detail={
            m?.disk_total_bytes
              ? `${bytes(m.disk_available_bytes)} disponibili su ${bytes(m.disk_total_bytes)}`
              : "Filesystem non misurabile"
          }
        />
      </div>
      <dl className="telemetry-services">
        {Object.entries(value.services).map(([key, reported]) => {
          const current =
            server && (key === "api" || key === "database")
              ? elapsed < 90
              : freshness === "fresh";
          const state = current ? reported : "unknown";
          return (
            <div key={key}>
              <dt>{serviceNames[key] || key}</dt>
              <dd>
                <Badge
                  tone={
                    state === "ok" ? "green" : state === "error" ? "red" : ""
                  }
                >
                  {serviceStates[state]}
                </Badge>
              </dd>
            </div>
          );
        })}
      </dl>
      <p className="page-note">
        Ultimo campione: {date(value.observed_at)} · Ricevuto:{" "}
        {date(value.received_at)}
        {m?.uptime_seconds != null && (
          <>
            {" "}
            · Sistema avviato da {Math.floor(m.uptime_seconds / 3600)} h{" "}
            {Math.floor((m.uptime_seconds % 3600) / 60)} min
          </>
        )}
      </p>
      {server && (
        <p className="page-note">
          CPU e RAM del sistema Linux visibile al worker; disco del filesystem
          configurato per il collector. In Docker possono riferirsi alla VM
          Linux e al filesystem del container, non ai volumi del database.
        </p>
      )}
      {link && (
        <Link to={server ? "/server" : `/gateways/${value.gateway_id}`}>
          {server ? "Stato server e storico" : "Scheda gateway e storico"}
        </Link>
      )}
    </article>
  );
}

export function useTelemetryClock() {
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
  return now;
}

export function GatewayTelemetry({ id }: { id: string }) {
  const now = useTelemetryClock();
  const query = useQuery({
    queryKey: ["telemetry", "gateway", id],
    queryFn: async ({ signal }) => {
      const requestedAt = clock();
      const value = await api<Telemetry>(`/telemetry/gateways/${id}`, {
        signal,
      });
      return { value, requestedAt };
    },
    refetchInterval: 15000,
    refetchOnWindowFocus: true,
  });
  return (
    <section aria-label="Telemetria del gateway">
      <h2 className="gateway-section">Telemetria del gateway</h2>
      <ErrorBox error={query.error} reload={() => void query.refetch()} />
      {query.isPending ? (
        <Loading />
      ) : (
        !query.error &&
        query.data && <Snapshot {...query.data} now={now} link={false} />
      )}
      {!query.error && query.data && (
        <MetricsHistory
          gatewayId={id}
          sms={query.data.value.activation_mode === "sms"}
        />
      )}
    </section>
  );
}

export function ServerTelemetry() {
  const now = useTelemetryClock();
  const query = useQuery({
    queryKey: ["telemetry", "server"],
    queryFn: async ({ signal }) => {
      const requestedAt = clock();
      const value = await api<Telemetry>("/telemetry/server", { signal });
      return { value, requestedAt };
    },
    refetchInterval: 15000,
    refetchOnWindowFocus: true,
  });
  return (
    <section aria-label="Telemetria server">
      <ErrorBox error={query.error} reload={() => void query.refetch()} />
      {query.isPending ? (
        <Loading />
      ) : (
        !query.error &&
        query.data && <Snapshot {...query.data} now={now} server link={false} />
      )}
    </section>
  );
}

export default function TelemetryPage() {
  const auth = useAuth();
  const groups = useGroups();
  const [q, setQ] = useState("");
  const [group, setGroup] = useState("");
  const [offset, setOffset] = useState(0);
  const now = useTelemetryClock();
  const gateways = useQuery({
    queryKey: ["telemetry", "gateways", q, group, offset],
    queryFn: async ({ signal }) => {
      const requestedAt = clock();
      const result = await page<Telemetry>(
        "/telemetry/gateways",
        { q, group_id: group, limit: 20, offset },
        signal,
      );
      return { ...result, requestedAt };
    },
    refetchInterval: 15000,
    refetchOnWindowFocus: true,
  });
  const server = useQuery({
    queryKey: ["telemetry", "server"],
    queryFn: async ({ signal }) => {
      const requestedAt = clock();
      const value = await api<Telemetry>("/telemetry/server", { signal });
      return { value, requestedAt };
    },
    enabled: !!auth.user?.is_admin,
    refetchInterval: 15000,
    refetchOnWindowFocus: true,
  });
  return (
    <>
      <PageTitle
        title="Telemetria"
        description="Risorse e stato dei servizi, con origine e freschezza delle misure."
        action={
          <button
            className="button secondary"
            onClick={() => {
              void gateways.refetch();
              if (auth.user?.is_admin) void server.refetch();
            }}
          >
            Aggiorna telemetria
          </button>
        }
      />
      <Notice>
        Campionamento ogni 30 secondi; dopo 90 secondi i valori sono indicati
        come scaduti. Una misura assente non equivale a zero o a un servizio
        guasto. La soglia visiva del 90% è indicativa e non genera notifiche.
      </Notice>
      {auth.user?.is_admin && (
        <section aria-label="Telemetria server">
          <ErrorBox error={server.error} reload={() => void server.refetch()} />
          {server.isPending ? (
            <Loading />
          ) : (
            !server.error &&
            server.data && <Snapshot {...server.data} now={now} server />
          )}
        </section>
      )}
      <section aria-label="Telemetria gateway">
        <div className="filters">
          <label>
            Cerca gateway
            <input
              value={q}
              maxLength={100}
              placeholder="Nome o MAC"
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
                <option value={g.id} key={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
          </label>
        </div>
        <ErrorBox
          error={gateways.error || groups.error}
          reload={() => void gateways.refetch()}
        />
        {gateways.isPending ? (
          <Loading />
        ) : (
          !gateways.error &&
          gateways.data && (
            <>
              {gateways.data.items.length ? (
                <div className="telemetry-grid">
                  {gateways.data.items.map((value) => (
                    <Snapshot
                      value={value}
                      key={value.gateway_id}
                      requestedAt={gateways.data.requestedAt}
                      now={now}
                    />
                  ))}
                </div>
              ) : (
                <Empty title="Nessun gateway visibile con questi filtri" />
              )}
              <Pager
                total={gateways.data.total}
                offset={offset}
                onChange={setOffset}
              />
            </>
          )
        )}
      </section>
    </>
  );
}
