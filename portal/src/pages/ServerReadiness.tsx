import { Link } from "react-router";
import type { ServerStatus } from "../api/client";
import { Badge, date, Notice } from "../ui";

const certificateNames = {
  https: "Portale HTTPS",
  openvpn: "Server OpenVPN",
  openvpn_ca: "Autorità di certificazione VPN",
};
const certificateStates = {
  not_configured: "Controllo non configurato",
  missing: "Certificato assente",
  unreadable: "Certificato non leggibile",
  valid: "Validità temporale regolare",
  expiring: "In scadenza entro 30 giorni",
  expired: "Scaduto",
  not_yet_valid: "Non ancora valido",
  unknown: "Da aggiornare",
};
const operations = {
  create: "Creazione backup",
  import: "Importazione backup",
  verify: "Prova di ripristino",
  restore: "Ripristino",
  delete: "Eliminazione backup",
};
const operationStates = {
  queued: "in attesa",
  uploading: "caricamento",
  running: "in corso",
  succeeded: "riuscita",
  failed: "non riuscita",
};

export default function ServerReadiness({
  status,
  elapsed,
}: {
  status: ServerStatus;
  elapsed: number;
}) {
  const backup = status.backup;
  const fresh = elapsed < backup.valid_for_seconds;
  const coordinator = fresh ? backup.coordinator_state : "unknown";
  const operation = backup.active_operation ?? backup.last_operation;
  return (
    <>
      <section
        className="card padded server-readiness"
        aria-label="Certificati configurati"
      >
        <h2>Certificati configurati</h2>
        <p className="muted">
          Scadenze dei certificati pubblici dell’installazione. Il controllo
          delle date non verifica la fiducia del browser o il certificato
          presentato in rete.
        </p>
        <div
          className="table-scroll"
          tabIndex={0}
          aria-label="Scadenze dei certificati"
        >
          <table>
            <thead>
              <tr>
                <th>Certificato</th>
                <th>Stato</th>
                <th>Scadenza</th>
                <th>Rilevato</th>
              </tr>
            </thead>
            <tbody>
              {status.certificates.map((certificate) => {
                const state =
                  elapsed < certificate.valid_for_seconds
                    ? certificate.state
                    : "unknown";
                return (
                  <tr key={certificate.name}>
                    <td>
                      {certificateNames[certificate.name]}
                      {certificate.fingerprint_sha256 && (
                        <details className="certificate-details">
                          <summary>Dettagli del certificato</summary>
                          <p>Valido dal {date(certificate.valid_from)}</p>
                          <p>Impronta SHA-256</p>
                          <code>{certificate.fingerprint_sha256}</code>
                        </details>
                      )}
                    </td>
                    <td>
                      <Badge
                        tone={
                          state === "valid"
                            ? "green"
                            : [
                                  "expired",
                                  "not_yet_valid",
                                  "unreadable",
                                  "missing",
                                ].includes(state)
                              ? "red"
                              : state === "expiring"
                                ? "amber"
                                : ""
                        }
                      >
                        {certificateStates[state]}
                      </Badge>
                    </td>
                    <td>
                      {certificate.expires_at
                        ? date(certificate.expires_at)
                        : "Non disponibile"}
                    </td>
                    <td>{date(certificate.observed_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>
      <section
        className="card padded server-readiness"
        aria-label="Riepilogo backup"
      >
        <div className="telemetry-heading">
          <h2>Backup e recupero</h2>
          <Link className="button secondary" to="/backups">
            Gestisci backup
          </Link>
        </div>
        {backup.state === "not_configured" ? (
          <Notice>Il servizio di backup non è configurato.</Notice>
        ) : backup.state === "unavailable" ? (
          <Notice>
            Il catalogo backup non è leggibile. Controlla il servizio di backup
            sul server.
          </Notice>
        ) : (
          <>
            <dl className="telemetry-services">
              <div>
                <dt>Servizio backup</dt>
                <dd>
                  <Badge
                    tone={
                      coordinator === "online"
                        ? "green"
                        : coordinator === "offline"
                          ? "red"
                          : ""
                    }
                  >
                    {coordinator === "online"
                      ? "Operativo"
                      : coordinator === "offline"
                        ? "Nessun segnale recente"
                        : "Non verificabile"}
                  </Badge>
                </dd>
              </div>
              <div>
                <dt>Ultimo segnale</dt>
                <dd>{date(backup.coordinator_observed_at)}</dd>
              </div>
              <div>
                <dt>Copie a catalogo</dt>
                <dd>{backup.copies ?? "Non disponibile"}</dd>
              </div>
              <div>
                <dt>Ultima copia</dt>
                <dd>
                  {backup.latest_backup
                    ? date(backup.latest_backup.created_at)
                    : "Nessuna copia disponibile"}
                </dd>
              </div>
              <div>
                <dt>Ultima prova di ripristino</dt>
                <dd>
                  {backup.last_test ? (
                    <>
                      <strong>
                        {backup.last_test.state === "succeeded"
                          ? "Riuscita"
                          : "Non riuscita"}
                      </strong>
                      <small>{date(backup.last_test.finished_at)}</small>
                    </>
                  ) : (
                    "Mai eseguita"
                  )}
                </dd>
              </div>
              <div>
                <dt>
                  {backup.active_operation
                    ? "Operazione osservata"
                    : "Ultima operazione"}
                </dt>
                <dd>
                  {operation ? (
                    <>
                      {operations[operation.kind]} ·{" "}
                      {operationStates[operation.state]}
                      <small>
                        {date(operation.finished_at ?? operation.created_at)}
                      </small>
                    </>
                  ) : (
                    "Nessuna operazione registrata"
                  )}
                </dd>
              </div>
            </dl>
            {backup.latest_backup && !backup.latest_backup.tested_at && (
              <Notice>
                La copia più recente non ha una prova di ripristino riuscita
                registrata.
              </Notice>
            )}
          </>
        )}
        <p className="muted">
          Dati del {date(backup.observed_at)}.{" "}
          {!fresh && "Osservazione scaduta: aggiorna lo stato. "}
          Il riepilogo legge il catalogo; non esegue una verifica degli archivi
          e non attesta una copia esterna.
        </p>
      </section>
    </>
  );
}
