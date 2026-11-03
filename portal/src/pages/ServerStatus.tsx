import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  api,
  type ServerStatus as Status,
  type SmtpResult,
} from "../api/client";
import { useAuth } from "../auth";
import {
  Badge,
  Confirm,
  date,
  ErrorBox,
  Loading,
  Notice,
  PageTitle,
} from "../ui";
import MetricsHistory from "./MetricsHistory";
import ServerReadiness from "./ServerReadiness";
import { ServerTelemetry, clock, useTelemetryClock } from "./Telemetry";

const names: Record<string, string> = {
  api: "API FactoryVPN",
  python_api: "Python API",
  postgresql: "PostgreSQL",
  worker: "Worker VPN",
  python_worker: "Python worker",
  openvpn: "Server OpenVPN",
  provisioning: "Worker di provisioning",
};
const states = {
  ok: "Operativo",
  error: "Problema segnalato",
  idle: "In attesa",
  unknown: "Non verificabile",
};
const outcomes: Record<SmtpResult["code"], string> = {
  running: "Verifica in corso.",
  connected:
    "Connessione SMTP riuscita, con la sicurezza e l’autenticazione configurate.",
  accepted:
    "Email accettata dal server SMTP. Controlla la casella per confermare la consegna.",
  not_configured: "Configura il server e il mittente SMTP nell’installazione.",
  tls_failed:
    "Verifica TLS non riuscita. Controlla modalità, certificato e autorità di certificazione.",
  authentication_failed:
    "Autenticazione SMTP non riuscita. Controlla le credenziali dell’installazione.",
  recipient_rejected: "Il server SMTP ha rifiutato il destinatario.",
  connection_failed:
    "Verifica SMTP non riuscita. Controlla host, porta e raggiungibilità.",
  timeout:
    "Tempo di verifica scaduto. L’esito dell’eventuale invio non è confermato; controlla la casella prima di ripetere.",
};

function ServerContent() {
  const [confirm, setConfirm] = useState(false);
  const now = useTelemetryClock();
  const query = useQuery({
    queryKey: ["server-status"],
    queryFn: async ({ signal }) => {
      const requestedAt = clock();
      const value = await api<Status>("/admin/server", { signal });
      return { value, requestedAt };
    },
    refetchInterval: 15000,
    refetchOnWindowFocus: true,
  });
  const check = useMutation({
    mutationFn: (mode: "connection" | "send") =>
      api<SmtpResult>("/admin/server/smtp-check", {
        method: "POST",
        body: { mode, request_id: crypto.randomUUID() },
      }),
    onSuccess: () => setConfirm(false),
    onSettled: () => {
      void query.refetch();
    },
  });
  const status = !query.error ? query.data?.value : undefined;
  const smtp = status?.smtp;
  const started = query.data?.requestedAt ?? now;
  const elapsed =
    Math.max(0, now.wall - started.wall, now.monotonic - started.monotonic) /
    1000;
  const cooldown = Math.max(
    0,
    Math.ceil((smtp?.retry_after_seconds ?? 0) - elapsed),
  );
  const blocked = check.isPending || cooldown > 0 || !smtp?.configured;
  // Refetch failures hide the old operational states; aged responses never stay green.
  return (
    <>
      <PageTitle
        title="Stato server"
        description="Componenti centrali, certificati, backup e risorse dell’installazione."
        action={
          <button
            className="button secondary"
            disabled={query.isFetching}
            onClick={() => void query.refetch()}
          >
            Aggiorna stato
          </button>
        }
      />
      <ErrorBox error={query.error} reload={() => void query.refetch()} />
      {query.isPending && <Loading />}
      {status && (
        <>
          <section
            className="card padded server-components"
            aria-label="Componenti centrali"
          >
            <h2>Componenti centrali</h2>
            <p className="muted">
              Versioni rilevate dai servizi. I componenti senza dati recenti
              sono indicati come non verificabili.
            </p>
            <div
              className="table-scroll"
              tabIndex={0}
              aria-label="Versioni dei componenti"
            >
              <table>
                <thead>
                  <tr>
                    <th>Componente</th>
                    <th>Versione</th>
                    <th>Stato</th>
                    <th>Rilevato</th>
                  </tr>
                </thead>
                <tbody>
                  {status.components.map((component) => {
                    const state =
                      elapsed < component.valid_for_seconds
                        ? component.state
                        : "unknown";
                    return (
                      <tr key={component.name}>
                        <td>{names[component.name] ?? component.name}</td>
                        <td>
                          <span className="mono">
                            {component.version ?? "Non disponibile"}
                          </span>
                          {component.build && component.build !== "unknown" && (
                            <small className="muted build-label">
                              Build {component.build}
                            </small>
                          )}
                        </td>
                        <td>
                          <Badge
                            tone={
                              state === "ok"
                                ? "green"
                                : state === "error"
                                  ? "red"
                                  : ""
                            }
                          >
                            {states[state]}
                          </Badge>
                        </td>
                        <td>{date(component.observed_at)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>
          {smtp && (
            <section
              className="card padded smtp-status"
              aria-label="Verifica SMTP"
            >
              <div className="telemetry-heading">
                <h2>Posta in uscita · SMTP</h2>
                <Badge tone={smtp.configured ? "blue" : ""}>
                  {smtp.configured ? "Configurato" : "Da configurare"}
                </Badge>
              </div>
              <dl className="telemetry-services">
                <div>
                  <dt>Server</dt>
                  <dd>
                    {smtp.host ?? "Non configurato"}:{smtp.port}
                  </dd>
                </div>
                <div>
                  <dt>Sicurezza</dt>
                  <dd>
                    {smtp.security === "plain"
                      ? "Connessione senza TLS"
                      : smtp.security.toUpperCase()}
                  </dd>
                </div>
                <div>
                  <dt>Mittente</dt>
                  <dd>{smtp.sender ?? "Non configurato"}</dd>
                </div>
                <div>
                  <dt>Autenticazione</dt>
                  <dd>
                    {smtp.authentication ? "Configurata" : "Non configurata"}
                  </dd>
                </div>
                <div>
                  <dt>Certificati</dt>
                  <dd>
                    {smtp.custom_ca ? "CA personalizzata" : "CA di sistema"}
                  </dd>
                </div>
              </dl>
              <p className="muted">
                La verifica controlla connessione, TLS e autenticazione
                configurati. L’invio di prova usa l’indirizzo verificato del tuo
                account: {smtp.test_recipient ?? "non disponibile"}.
              </p>
              <div className="history-actions">
                <button
                  className="button secondary"
                  disabled={blocked}
                  onClick={() => check.mutate("connection")}
                >
                  {check.isPending
                    ? "Verifica in corso…"
                    : "Verifica connessione"}
                </button>
                <button
                  className="button"
                  disabled={blocked || !smtp.test_recipient}
                  onClick={() => {
                    check.reset();
                    setConfirm(true);
                  }}
                >
                  Invia email di prova
                </button>
              </div>
              {cooldown > 0 && (
                <p className="page-note" role="status">
                  Nuova verifica disponibile tra {cooldown} secondi.
                </p>
              )}
              <ErrorBox error={check.error} />
              {smtp.last_check && (
                <div className="smtp-result" role="status">
                  <strong>
                    {smtp.last_check.mode === "send"
                      ? "Ultima prova di invio"
                      : "Ultima verifica della connessione"}
                  </strong>
                  <p>{outcomes[smtp.last_check.code]}</p>
                  <small className="muted">
                    {date(
                      smtp.last_check.finished_at ?? smtp.last_check.started_at,
                    )}
                  </small>
                </div>
              )}
            </section>
          )}
          <ServerReadiness status={status} elapsed={elapsed} />
          <ServerTelemetry />
          <MetricsHistory />
        </>
      )}
      {confirm && smtp && (
        <Confirm
          title="Inviare un’email di prova?"
          label="Invia email"
          onCancel={() => setConfirm(false)}
          onConfirm={() => check.mutate("send")}
          busy={check.isPending}
          error={check.error}
        >
          <p>
            Il server invierà un messaggio di prova a{" "}
            <strong>{smtp.test_recipient}</strong>.
          </p>
        </Confirm>
      )}
    </>
  );
}

export default function ServerStatus() {
  const auth = useAuth();
  return auth.user?.is_admin ? (
    <ServerContent />
  ) : (
    <>
      <PageTitle
        title="Stato server"
        description="Componenti centrali e posta in uscita."
      />
      <Notice>Questa pagina è riservata agli amministratori globali.</Notice>
    </>
  );
}
