import EmailCode from "../EmailCode";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { DatabaseBackup, Download, RefreshCw } from "lucide-react";
import {
  api,
  downloadFile,
  type EmailChallenge,
  type BackupEntry,
  type BackupStatus,
} from "../api/client";
import { useAuth } from "../auth";
import { APP_NAME } from "../branding";
import {
  Badge,
  Confirm,
  Empty,
  ErrorBox,
  Loading,
  Notice,
  PageTitle,
  date,
} from "../ui";

const phases: Record<string, string> = {
  queued: "In attesa",
  running: "In esecuzione",
  verifying: "Verifica del ripristino",
  closing_sessions: "Chiusura delle VPN",
  safety_backup: "Copia di sicurezza",
  restoring: "Ripristino dei dati",
  restarting: "Riavvio e verifica dei servizi",
  complete: "Completata",
  uploading: "Caricamento",
};
const kinds: Record<string, string> = {
  create: "Creazione backup",
  import: "Importazione",
  verify: "Prova di ripristino",
  restore: "Ripristino",
  delete: "Eliminazione",
};
const failures: Record<string, string> = {
  incompatible_origin:
    "Il backup usa un dominio o una porta diversi. Ripristina prima la configurazione pubblica prevista per questa installazione.",
  authentication_failed:
    "Il file non è autentico oppure la chiave di recupero non corrisponde.",
  incompatible_schema: "Il backup appartiene a una versione non compatibile.",
  different_installation:
    "Il backup appartiene a un’altra installazione. È necessario il recupero da console.",
  insufficient_disk:
    "Spazio su disco insufficiente per completare l’operazione.",
  vpn_cleanup_timeout:
    "La pulizia delle VPN non è terminata. Il ripristino è stato interrotto.",
  operation_interrupted:
    "Operazione interrotta dal riavvio del coordinatore. Controlla l’esito prima di riprovare.",
  verification_restore_failed:
    "La prova di ripristino non è riuscita. I dati in uso sono rimasti invariati.",
  certificate_expired:
    "Il backup contiene certificati scaduti. È necessario l’intervento dell’amministratore del server.",
  service_command_failed:
    "Un servizio non ha completato l’operazione. Controlla il coordinatore di recupero.",
};

export default function Backups() {
  const { user } = useAuth();
  if (!user?.is_admin)
    return (
      <>
        <PageTitle
          title="Accesso non disponibile"
          description="Backup e ripristino sono riservati agli amministratori globali."
        />
      </>
    );
  return <BackupManagement />;
}

function BackupManagement() {
  const cache = useQueryClient();
  const { user } = useAuth();
  const [challenge, setChallenge] = useState<EmailChallenge | null>(null);
  const [password, setPassword] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [keep, setKeep] = useState<number | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [selected, setSelected] = useState<{
    backup: BackupEntry;
    action: "restore" | "delete";
  } | null>(null);
  const query = useQuery({
    queryKey: ["backups"],
    queryFn: ({ signal }) => api<BackupStatus>("/backups", { signal }),
    refetchInterval: 5000,
  });
  const reload = () => cache.invalidateQueries({ queryKey: ["backups"] });
  const action = useMutation({
    mutationFn: async (request: {
      path: string;
      body?: unknown;
      file?: File;
      method?: string;
    }) =>
      api(request.path, {
        method: request.method || "POST",
        body: request.body,
        rawBody: request.file,
        idempotencyKey: crypto.randomUUID(),
      }),
    onSuccess: async () => {
      setSelected(null);
      setConfirmation("");
      setFile(null);
      await reload();
    },
  });
  const download = useMutation({
    mutationFn: (id: string) =>
      downloadFile(`/backups/${id}/download`, `factoryvpn-${id}.fvbackup`),
  });
  const reauthenticate = useMutation({
    mutationFn: async () => {
      if (user?.auth_mode === "sso") {
        const result = await api<{ authorization_url: string }>(
          "/web/sso/start",
          {
            method: "POST",
            body: {
              provider_id: query.data?.sso_provider_id,
              reauthenticate: true,
            },
          },
        );
        window.location.assign(result.authorization_url);
      } else {
        setChallenge(
          await api<EmailChallenge>("/backups/reauthenticate", {
            method: "POST",
            body: { password },
          }),
        );
        setPassword("");
        await reload();
      }
    },
  });
  const data = query.data;
  const busy =
    action.isPending ||
    !!data?.jobs.some((job) =>
      ["queued", "running", "uploading"].includes(job.state),
    );
  const ready =
    !!data?.enabled && data.coordinator_online && !data.needs_reauthentication;
  const command = (id: string, operation: "verify" | "restore" | "delete") =>
    action.mutate({
      path: `/backups/${id}/commands`,
      body: {
        action: operation,
        ...(operation === "restore"
          ? { confirmation: `RIPRISTINA ${id}` }
          : {}),
      },
    });
  return (
    <>
      <PageTitle
        title="Backup e ripristino"
        description={`Proteggi dati, configurazione e credenziali dell’intera installazione ${APP_NAME}.`}
        action={
          <button
            className="button"
            disabled={!ready || busy}
            onClick={() => action.mutate({ path: "/backups" })}
          >
            <DatabaseBackup size={17} /> Crea backup
          </button>
        }
      />
      <ErrorBox
        error={query.error || action.error || download.error}
        reload={() => void reload()}
      />
      {query.isLoading && <Loading />}
      {data && (
        <>
          {!data.enabled ? (
            <Notice>
              Il servizio di backup deve essere configurato sul server.
              L’amministratore dell’installazione deve inizializzare il
              coordinatore e custodire una copia esterna della chiave di
              recupero.
            </Notice>
          ) : (
            <>
              <div className="backup-summary card padded">
                <div>
                  <h2>Protezione dell’installazione</h2>
                  <p>
                    Gli archivi contengono anche chiavi private. Conserva una
                    copia cifrata fuori dal server e custodisci separatamente la
                    chiave di recupero.
                  </p>
                </div>
                <Badge tone={data.coordinator_online ? "green" : "red"}>
                  {data.coordinator_online
                    ? "Coordinatore disponibile"
                    : "Coordinatore non raggiungibile"}
                </Badge>
              </div>
              {data.needs_reauthentication && (
                <section className="card padded" aria-label="Conferma identità">
                  <h2>Conferma la tua identità</h2>
                  <p>
                    Le operazioni sui backup richiedono un’autenticazione
                    effettuata negli ultimi cinque minuti.
                  </p>
                  {challenge ? (
                    <EmailCode
                      challenge={challenge}
                      onCancel={() => setChallenge(null)}
                      onComplete={async () => {
                        setChallenge(null);
                        await reload();
                      }}
                    />
                  ) : (
                    <form
                      className="backup-form"
                      onSubmit={(event) => {
                        event.preventDefault();
                        reauthenticate.mutate();
                      }}
                    >
                      {user?.auth_mode === "local" && (
                        <label>
                          Password attuale
                          <input
                            type="password"
                            autoComplete="current-password"
                            value={password}
                            onChange={(event) =>
                              setPassword(event.target.value)
                            }
                            required
                          />
                        </label>
                      )}
                      <button
                        className="button secondary"
                        disabled={reauthenticate.isPending}
                      >
                        {user?.auth_mode === "sso"
                          ? "Conferma con SSO"
                          : "Conferma identità"}
                      </button>
                    </form>
                  )}
                  <ErrorBox error={reauthenticate.error} />
                </section>
              )}
              <div className="backup-settings">
                <section className="card padded">
                  <h2>Importa un backup</h2>
                  <p>
                    Carica un archivio {APP_NAME} cifrato di questa
                    installazione. L’importazione verifica il file senza
                    sostituire i dati in uso.
                  </p>
                  <form
                    className="backup-form"
                    onSubmit={(event) => {
                      event.preventDefault();
                      if (file && file.size <= data.maximum_bytes)
                        action.mutate({ path: "/backups/import", file });
                    }}
                  >
                    <label>
                      Archivio cifrato
                      <input
                        type="file"
                        accept=".fvbackup"
                        disabled={!ready || busy}
                        onChange={(event) =>
                          setFile(event.target.files?.[0] || null)
                        }
                      />
                    </label>
                    <small>
                      Massimo {Math.floor(data.maximum_bytes / 1024 / 1024)}{" "}
                      MiB.
                    </small>
                    {file && file.size > data.maximum_bytes && (
                      <p role="alert">
                        Il file supera la dimensione consentita.
                      </p>
                    )}
                    <button
                      className="button secondary"
                      disabled={
                        !ready ||
                        busy ||
                        !file ||
                        file.size > data.maximum_bytes
                      }
                    >
                      Importa e verifica
                    </button>
                  </form>
                </section>
                <section className="card padded">
                  <h2>Conservazione</h2>
                  <p>
                    Alla creazione di un nuovo backup vengono eliminate le copie
                    più vecchie oltre questo limite. L’ultima copia di sicurezza
                    del ripristino resta protetta.
                  </p>
                  <form
                    className="backup-form"
                    onSubmit={(event) => {
                      event.preventDefault();
                      action.mutate({
                        path: "/backups/retention",
                        method: "PUT",
                        body: { count: keep ?? data.retention },
                      });
                    }}
                  >
                    <label>
                      Copie da conservare
                      <input
                        type="number"
                        min={1}
                        max={100}
                        value={keep ?? data.retention}
                        onChange={(event) =>
                          setKeep(Number(event.target.value))
                        }
                        required
                      />
                    </label>
                    <button
                      className="button secondary"
                      disabled={!ready || busy}
                    >
                      Salva conservazione
                    </button>
                  </form>
                </section>
              </div>
            </>
          )}
          <section aria-label="Archivio backup" className="card padded">
            <h2>Backup disponibili</h2>
            {!data.backups.length ? (
              <Empty title="Nessun backup disponibile">
                Crea la prima copia oppure importa un archivio già salvato.
              </Empty>
            ) : (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Creazione</th>
                      <th>Dimensione</th>
                      <th>Verifiche</th>
                      <th>Azioni</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.backups.map((backup) => (
                      <tr key={backup.id}>
                        <td>
                          {date(backup.created_at)}
                          <small className="backup-id">{backup.id}</small>
                          {backup.protected && (
                            <Badge>Copia di sicurezza protetta</Badge>
                          )}
                        </td>
                        <td>{(backup.size / 1024 / 1024).toFixed(1)} MiB</td>
                        <td>
                          <Badge tone="green">Integrità verificata</Badge>
                          <p>
                            {backup.tested_at
                              ? `Ripristino provato: ${date(backup.tested_at)}`
                              : "Ripristino non ancora provato"}
                          </p>
                        </td>
                        <td>
                          <div className="backup-actions">
                            <button
                              className="button secondary small"
                              disabled={!ready || download.isPending}
                              onClick={() => download.mutate(backup.id)}
                            >
                              <Download size={15} /> Scarica
                            </button>
                            <button
                              className="button secondary small"
                              disabled={!ready || busy}
                              onClick={() => command(backup.id, "verify")}
                            >
                              <RefreshCw size={15} /> Prova ripristino
                            </button>
                            <button
                              className="button secondary small"
                              disabled={!ready || busy}
                              onClick={() => {
                                setSelected({ backup, action: "restore" });
                                setConfirmation("");
                              }}
                            >
                              Ripristina
                            </button>
                            {!backup.protected && (
                              <button
                                className="button danger small"
                                disabled={!ready || busy}
                                onClick={() =>
                                  setSelected({ backup, action: "delete" })
                                }
                              >
                                Elimina
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
          <section className="card padded" aria-label="Operazioni recenti">
            <h2>Operazioni recenti</h2>
            {data.jobs.length === 0 ? (
              <p className="muted">Nessuna operazione richiesta.</p>
            ) : (
              <ol className="backup-jobs">
                {data.jobs.map((job) => (
                  <li key={job.id}>
                    <div>
                      <strong>{kinds[job.kind] || "Operazione"}</strong>
                      <small>{date(job.created_at)}</small>
                    </div>
                    <Badge
                      tone={
                        job.state === "failed"
                          ? "red"
                          : job.state === "succeeded"
                            ? "green"
                            : ""
                      }
                    >
                      {job.state === "failed"
                        ? "Non riuscita"
                        : phases[job.phase] || "In corso"}
                    </Badge>
                    {job.error && (
                      <p role="status">
                        {failures[job.error] ||
                          "Operazione non riuscita. Controlla il servizio di backup prima di riprovare."}
                      </p>
                    )}
                  </li>
                ))}
              </ol>
            )}
          </section>
        </>
      )}
      {selected && (
        <Confirm
          title={
            selected.action === "restore"
              ? "Ripristinare l’installazione?"
              : "Eliminare il backup?"
          }
          label={
            selected.action === "restore"
              ? "Conferma ripristino"
              : "Elimina definitivamente"
          }
          danger
          busy={action.isPending}
          error={action.error}
          onCancel={() => setSelected(null)}
          onConfirm={() => {
            if (selected.action !== "restore" || confirmation === "RIPRISTINA")
              command(selected.backup.id, selected.action);
          }}
        >
          <p>Backup del {date(selected.backup.created_at)}.</p>
          {selected.action === "restore" ? (
            <>
              <p>
                Le VPN saranno chiuse e il portale sarà temporaneamente
                indisponibile. I dati verranno riportati a questa copia; gli
                accessi dovranno essere eseguiti nuovamente. Le revoche e le
                restrizioni più recenti saranno mantenute.
              </p>
              <p>
                Prima della sostituzione verranno eseguite una prova isolata e
                una copia di sicurezza. Se il ripristino fallisce, il
                coordinatore tenterà il recupero dello stato precedente.
              </p>
              <label>
                Scrivi RIPRISTINA per confermare
                <input
                  autoComplete="off"
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                />
              </label>
              {confirmation !== "RIPRISTINA" && (
                <small>
                  La conferma sarà accettata solo con la parola completa.
                </small>
              )}
            </>
          ) : (
            <p>
              Questa copia verrà rimossa dall’archivio del server.
              L’eliminazione è definitiva.
            </p>
          )}
        </Confirm>
      )}
    </>
  );
}
