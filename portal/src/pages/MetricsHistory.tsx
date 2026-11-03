import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type TelemetryHistory } from "../api/client";
import { date, Empty, ErrorBox, Loading } from "../ui";

type Point = TelemetryHistory["points"][number];
type Metric = "cpu" | "memory" | "disk";
const metrics: [Metric, string][] = [
  ["cpu", "CPU"],
  ["memory", "RAM utilizzata"],
  ["disk", "Disco utilizzato"],
];
const percent = (value: number | null) =>
  value == null
    ? "Nessun dato"
    : `${value.toLocaleString("it-IT", { maximumFractionDigits: 1 })}%`;

function Chart({
  points,
  metric,
  title,
  selected,
  onSelect,
}: {
  points: Point[];
  metric: Metric;
  title: string;
  selected: number;
  onSelect: (index: number) => void;
}) {
  const svg = useRef<SVGSVGElement>(null);
  const [width, setWidth] = useState(710);
  useEffect(() => {
    if (!svg.current) return;
    const observer = new ResizeObserver(([entry]) =>
      setWidth(Math.max(260, entry.contentRect.width)),
    );
    observer.observe(svg.current);
    return () => observer.disconnect();
  }, []);
  const right = width - 22;
  const x = (index: number) =>
    38 + (index * (right - 38)) / Math.max(1, points.length - 1);
  const y = (value: number) => 136 - value * 1.16;
  function segments(kind: "mean" | "peak") {
    const paths: { x: number; y: number }[][] = [];
    let current: { x: number; y: number }[] = [];
    points.forEach((p, index) => {
      const value = p[metric][kind];
      if (value == null) {
        if (current.length) paths.push(current);
        current = [];
      } else current.push({ x: x(index), y: y(value) });
    });
    if (current.length) paths.push(current);
    return paths.map((path, index) =>
      path.length === 1 ? (
        <circle
          key={index}
          cx={path[0].x}
          cy={path[0].y}
          r={kind === "mean" ? 2.5 : 1.5}
        />
      ) : (
        <polyline
          key={index}
          points={path.map((p) => `${p.x},${p.y}`).join(" ")}
          fill="none"
          strokeWidth={kind === "mean" ? 2 : 1.5}
          strokeDasharray={kind === "peak" ? "5 4" : undefined}
        />
      ),
    );
  }
  const point = points[selected];
  return (
    <article className="metric-chart" aria-label={title}>
      <div className="telemetry-heading">
        <h3>{title}</h3>
        <strong>{percent(point?.[metric].mean ?? null)}</strong>
      </div>
      <svg
        ref={svg}
        viewBox={`0 0 ${width} 170`}
        role="img"
        aria-label={`${title}: media e picco, intervalli senza campioni lasciati vuoti`}
        onPointerMove={(event) => {
          const box = event.currentTarget.getBoundingClientRect();
          onSelect(
            Math.max(
              0,
              Math.min(
                points.length - 1,
                Math.round(
                  ((((event.clientX - box.left) / box.width) * width - 38) /
                    (right - 38)) *
                    (points.length - 1),
                ),
              ),
            ),
          );
        }}
      >
        {[0, 50, 100].map((value) => (
          <g key={value} className="chart-grid">
            <line x1="38" y1={y(value)} x2={right} y2={y(value)} />
            <text x="30" y={y(value) + 4} textAnchor="end">
              {value}%
            </text>
          </g>
        ))}
        <g className="chart-peak">{segments("peak")}</g>
        <g className="chart-mean">{segments("mean")}</g>
        <line
          className="chart-cursor"
          x1={x(selected)}
          x2={x(selected)}
          y1="18"
          y2="136"
        />
        <g className="chart-grid">
          <text x="38" y="160">
            {date(points[0]?.at)}
          </text>
          <text x={right} y="160" textAnchor="end">
            {date(points.at(-1)?.at)}
          </text>
        </g>
      </svg>
      <small className="muted">
        Media {percent(point?.[metric].mean ?? null)} · Picco{" "}
        {percent(point?.[metric].peak ?? null)} · {point?.[metric].samples ?? 0}{" "}
        campioni
      </small>
    </article>
  );
}

function HistoryCharts({ value }: { value: TelemetryHistory }) {
  const [selected, setSelected] = useState(value.points.length - 1);
  const [table, setTable] = useState(false);
  const index = Math.min(selected, value.points.length - 1);
  if (!value.points.some((p) => p.samples > 0))
    return (
      <Empty title="Nessun campione in questo intervallo">
        Lo storico inizia con le misure ricevute. Un periodo senza dati non
        equivale a utilizzo zero.
      </Empty>
    );
  return (
    <>
      <p className="page-note">
        Dal {date(value.since)} al {date(value.until)} · Intervalli di{" "}
        {value.resolution_seconds / 60} minuti · Media continua, picco
        tratteggiato
      </p>
      <div className="metric-charts">
        {metrics.map(([metric, title]) => (
          <Chart
            key={metric}
            points={value.points}
            metric={metric}
            title={title}
            selected={index}
            onSelect={setSelected}
          />
        ))}
      </div>
      <label className="history-selector">
        Esplora i campioni: {date(value.points[index]?.at)}
        <input
          type="range"
          min={0}
          max={value.points.length - 1}
          value={index}
          onChange={(e) => setSelected(Number(e.target.value))}
          aria-label="Intervallo del grafico"
          aria-valuetext={date(value.points[index]?.at)}
        />
      </label>
      <details onToggle={(e) => setTable(e.currentTarget.open)}>
        <summary>Valori in tabella</summary>
        {table && (
          <div className="table-scroll history-table">
            <table>
              <caption>
                Media e picco per intervallo; campi assenti indicati come Nessun
                dato
              </caption>
              <thead>
                <tr>
                  <th>Data</th>
                  <th>Campioni</th>
                  {metrics.map(([key, label]) => (
                    <th key={key}>{label} (media / picco)</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {value.points.map((point) => (
                  <tr key={point.at}>
                    <td>{date(point.at)}</td>
                    <td>{point.samples}</td>
                    {metrics.map(([key]) => (
                      <td key={key}>
                        {percent(point[key].mean)} / {percent(point[key].peak)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </details>
    </>
  );
}

export default function MetricsHistory({
  gatewayId,
  sms = false,
}: {
  gatewayId?: string;
  sms?: boolean;
}) {
  const [period, setPeriod] = useState("24h");
  const query = useQuery({
    queryKey: ["telemetry-history", gatewayId ?? "server", period],
    queryFn: ({ signal }) =>
      api<TelemetryHistory>(
        `/telemetry/${gatewayId ? `gateways/${gatewayId}` : "server"}/history?period=${period}`,
        { signal },
      ),
    refetchInterval: 60000,
    refetchOnWindowFocus: true,
  });
  return (
    <section
      className="card padded metrics-history"
      aria-label="Storico delle metriche"
    >
      <div className="telemetry-heading">
        <div>
          <h2>Storico delle metriche</h2>
          <p className="muted">CPU, RAM e disco · Fino a 30 giorni</p>
        </div>
        <div className="history-actions">
          <label>
            Periodo
            <select value={period} onChange={(e) => setPeriod(e.target.value)}>
              <option value="1h">Ultima ora</option>
              <option value="24h">Ultime 24 ore</option>
              <option value="7d">Ultimi 7 giorni</option>
              <option value="30d">Ultimi 30 giorni</option>
            </select>
          </label>
          <button
            className="button secondary"
            disabled={query.isFetching}
            onClick={() => void query.refetch()}
          >
            Aggiorna storico
          </button>
        </div>
      </div>
      <p className="page-note">
        {sms
          ? "I gateway SMS inviano misure quando sono attivi; i periodi di attesa possono restare vuoti. "
          : ""}
        Le interruzioni restano visibili. Il picco è il massimo dei campioni
        ricevuti; l’ultimo intervallo può essere parziale.
      </p>
      <ErrorBox error={query.error} reload={() => void query.refetch()} />
      {query.isPending ? (
        <Loading />
      ) : (
        !query.error &&
        query.data && (
          <HistoryCharts
            key={`${gatewayId ?? "server"}:${period}`}
            value={query.data}
          />
        )
      )}
    </section>
  );
}
