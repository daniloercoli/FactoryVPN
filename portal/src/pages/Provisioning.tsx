import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router";
import {
  api,
  downloadFile,
  type ProvisioningState,
  type ProvisioningJob,
} from "../api/client";
import { useAuth } from "../auth";
import { useGroups } from "../hooks";
import { GatewayIdentifierFields } from "./GatewayIdentifiers";
import {
  Badge,
  Confirm,
  ErrorBox,
  Loading,
  Notice,
  PageTitle,
  date,
} from "../ui";

const jobLabels: Record<string, string> = {
  queued: "In preparazione",
  ready: "Pronto da scaricare",
  active: "Attivato",
  cancelled: "Annullato",
  failed: "Preparazione non riuscita",
  expired: "Consegna scaduta",
};

type ProfileAction = "activate" | "revoke" | "cancel";

export function VPNProfile({
  kind,
  peerId,
}: {
  kind: "gateway" | "technician";
  peerId: string;
}) {
  const auth = useAuth();
  const client = useQueryClient();
  const [confirmation, setConfirmation] = useState<{
    action: ProfileAction;
    certificateId: string | null;
    jobId?: string;
  } | null>(null);
  const [requestKey, setRequestKey] = useState(() => crypto.randomUUID());
  const [notice, setNotice] = useState("");
  const path = `/provisioning/peers/${kind}/${peerId}`;
  const query = useQuery({
    queryKey: ["provisioning", kind, peerId],
    queryFn: ({ signal }) => api<ProvisioningState>(path, { signal }),
    refetchInterval: 5000,
    refetchOnWindowFocus: true,
  });
  const data = query.data;
  useEffect(() => {
    if (data?.enrollment_pending) setConfirmation(null);
  }, [data?.enrollment_pending]);
  const pending = data?.pending_job;
  const own = kind === "technician" && peerId === auth.user!.id;
  const canPrepare = kind === "gateway" || (own && auth.can("connect"));
  async function refresh() {
    await Promise.all(
      ["provisioning", "vpn", "gateways", "audit"].map((key) =>
        client.invalidateQueries({ queryKey: [key] }),
      ),
    );
  }
  const prepare = useMutation({
    mutationFn: () =>
      api<ProvisioningJob>(path + "/prepare", {
        method: "POST",
        idempotencyKey: requestKey,
        body: { expected_certificate_id: data?.certificate?.id || null },
      }),
    onSuccess: async () => {
      setRequestKey(crypto.randomUUID());
      setNotice("");
      await refresh();
    },
  });
  const renewCertificate = useMutation({
    mutationFn: () =>
      api<ProvisioningState>(path + "/renew-certificate", { method: "POST" }),
    onSuccess: async () => {
      setNotice(
        "Rinnovo richiesto. L’agente lo rileverà al prossimo controllo HTTPS; un gateway SMS inattivo lo rileva al successivo risveglio.",
      );
      await refresh();
    },
  });
  const download = useMutation({
    mutationFn: () =>
      downloadFile(
        `/provisioning/jobs/${pending!.id}/download`,
        `factoryvpn-${kind}-${peerId}.${kind === "gateway" ? "zip" : "ovpn"}`,
      ),
    onSuccess: async () => {
      setNotice(
        "Download avviato. Verifica di avere salvato il file prima di attivarlo.",
      );
      await refresh();
    },
  });
  const change = useMutation({
    mutationFn: async () => {
      if (confirmation!.action === "revoke")
        return api(path + "/revoke", {
          method: "POST",
          body: { expected_certificate_id: confirmation!.certificateId },
        });
      return api(
        `/provisioning/jobs/${confirmation!.jobId}/${confirmation!.action}`,
        {
          method: "POST",
        },
      );
    },
    onSuccess: async () => {
      setConfirmation(null);
      setNotice("");
      await refresh();
    },
  });
  const busy = prepare.isPending || download.isPending || change.isPending;
  const error =
    query.error || prepare.error || download.error || renewCertificate.error;
  const cert = data?.certificate;
  function confirm(action: ProfileAction) {
    change.reset();
    // Polling must not retarget an open confirmation to replacement credentials.
    setConfirmation({
      action,
      certificateId: cert?.id || null,
      jobId: pending?.id,
    });
  }
  if (!query.isPending && !query.error && data?.enrollment_pending) {
    return (
      <section
        className="card padded stack"
        aria-label="Credenziali del gateway"
      >
        <h2>Arruolamento del gateway</h2>
        <Notice>
          La prima consegna del pacchetto è gestita dall’arruolamento. Per
          sostituirla con uno ZIP da installare in loco, un amministratore
          globale deve prendere in carico il dispositivo manualmente.
        </Notice>
        {auth.user?.is_admin && (
          <Link className="button secondary" to={`/enrollments/${peerId}`}>
            Gestisci arruolamento
          </Link>
        )}
        <p className="muted">
          L’attivazione del pacchetto e la registrazione del gateway precedono
          l’apertura di una sessione VPN.
        </p>
      </section>
    );
  }
  return (
    <section
      className="card padded stack"
      aria-label={
        kind === "gateway" ? "Credenziali del gateway" : "Profilo VPN personale"
      }
    >
      <h2>
        {kind === "gateway"
          ? "Provisioning e credenziali"
          : own
            ? "Il mio profilo VPN"
            : "Certificato VPN dell’utente"}
      </h2>
      <ErrorBox error={error} reload={() => void query.refetch()} />
      {query.isPending ? (
        <Loading />
      ) : (
        !query.error &&
        data && (
          <>
            {!data.enabled && (
              <Notice>
                Il servizio di provisioning deve essere configurato
                dall’amministratore dell’installazione.
              </Notice>
            )}
            {data.enabled && data.issuer_status !== "ok" && (
              <Notice>
                Servizio di emissione{" "}
                {data.issuer_status === "error"
                  ? "in errore"
                  : "non ancora disponibile o non aggiornato"}
                . Le richieste saranno elaborate quando il servizio torna
                disponibile.
              </Notice>
            )}
            {cert ? (
              <>
                <Badge
                  tone={
                    Date.parse(cert.not_after) > Date.now() ? "green" : "amber"
                  }
                >
                  {Date.parse(cert.not_after) > Date.now()
                    ? "Certificato attivo"
                    : "Certificato scaduto"}
                </Badge>
                <p>
                  Scadenza: <strong>{date(cert.not_after)}</strong>
                  <br />
                  Emesso il: {date(cert.not_before)}
                </p>
                <small className="mono">Seriale: {cert.serial}</small>
              </>
            ) : (
              <p>Nessun certificato attivo.</p>
            )}
            {kind === "gateway" && (
              <p>
                {data.registered_at
                  ? `Registrazione confermata: ${date(data.registered_at)}`
                  : "In attesa della registrazione del gateway."}
              </p>
            )}
            {data.revocation_pending && (
              <Notice>
                Revoca registrata. Aggiornamento della lista di revoca in corso.
              </Notice>
            )}
            {kind === "gateway" && cert && data.registered_at && (
              <div className="stack">
                <p>
                  Rinnovo remoto del solo certificato:{" "}
                  {data.remote_renewal_state
                    ? {
                        challenge: "verifica del dispositivo",
                        queued: "emissione in coda",
                        ready: "in attesa di attivazione",
                        active: "attesa conferma del gateway",
                      }[data.remote_renewal_state] || "in corso"
                    : data.remote_renewal_requested
                      ? "richiesto"
                      : "nessuna richiesta in corso"}
                  .
                </p>
                <button
                  className="button secondary"
                  disabled={
                    busy ||
                    renewCertificate.isPending ||
                    !!data.remote_renewal_state ||
                    data.remote_renewal_requested
                  }
                  onClick={() => renewCertificate.mutate()}
                >
                  Richiedi rinnovo remoto
                </button>
                <small>
                  Conserva API key e segreto SMS. Richiede un agente compatibile
                  e attende la fine delle sessioni VPN.
                </small>
              </div>
            )}
            {data.cleanup_pending && (
              <Notice>
                Chiusura delle connessioni e rilascio delle risorse in corso.
              </Notice>
            )}
            {pending ? (
              <div className="stack">
                <p>
                  <strong>{jobLabels[pending.state]}</strong> · disponibile fino
                  al {date(pending.expires_at)}
                </p>
                {pending.state === "ready" && !pending.can_download && (
                  <Notice>
                    Il pacchetto può essere scaricato soltanto dall’accesso che
                    lo ha richiesto. Puoi annullare la preparazione e
                    richiederne una nuova.
                  </Notice>
                )}
                <div className="form-actions">
                  {pending.can_download && (
                    <button
                      className="button"
                      disabled={busy}
                      onClick={() => download.mutate()}
                    >
                      Scarica{" "}
                      {kind === "gateway" ? "pacchetto gateway" : "profilo VPN"}
                    </button>
                  )}
                  {pending.can_activate && (
                    <button
                      className="button secondary"
                      disabled={busy}
                      onClick={() => confirm("activate")}
                    >
                      Attiva il nuovo profilo
                    </button>
                  )}
                  <button
                    className="button secondary"
                    disabled={busy}
                    onClick={() => confirm("cancel")}
                  >
                    Annulla preparazione
                  </button>
                </div>
              </div>
            ) : (
              <>
                {data.recent_job &&
                  ["failed", "expired", "cancelled"].includes(
                    data.recent_job.state,
                  ) && (
                    <Notice>
                      {jobLabels[data.recent_job.state]}. Puoi richiedere un
                      nuovo pacchetto.
                      {data.recent_job.state === "failed" &&
                        " Se l’errore persiste, verifica il servizio di emissione."}
                    </Notice>
                  )}
                {canPrepare && (
                  <button
                    className="button"
                    disabled={busy || !data.enabled}
                    onClick={() => prepare.mutate()}
                  >
                    {cert ? "Prepara rinnovo" : "Prepara profilo VPN"}
                  </button>
                )}
              </>
            )}
            {notice && <Notice>{notice}</Notice>}
            {cert && (
              <button
                className="button danger-outline"
                disabled={busy}
                onClick={() => confirm("revoke")}
              >
                Revoca credenziali VPN
              </button>
            )}
            {canPrepare && (
              <p className="muted">
                Il file contiene chiavi private. Scaricalo da questo accesso e
                conservalo in modo protetto. Dopo l’attivazione il download
                viene eliminato; se perdi il file, prepara un nuovo profilo.
              </p>
            )}
            {own && (
              <p className="muted">
                Importa il file .ovpn nel client OpenVPN. Apri la sessione dal
                portale, quindi avvia il client manualmente. Il logout chiude le
                VPN di quell’accesso; il profilo resta utilizzabile per gli
                accessi successivi.
              </p>
            )}
            {kind === "gateway" && (
              <p className="muted">
                Dopo l’attivazione installa i file del pacchetto sul gateway e
                riavvia l’agente. La registrazione conferma l’accesso HTTPS;
                verifica il tunnel con una sessione VPN.
              </p>
            )}
          </>
        )
      )}
      {confirmation && (
        <Confirm
          title={
            confirmation.action === "activate"
              ? "Attivare il nuovo profilo?"
              : confirmation.action === "revoke"
                ? "Revocare le credenziali VPN?"
                : "Annullare la preparazione?"
          }
          label={
            confirmation.action === "activate"
              ? "Ho salvato il file: attiva"
              : confirmation.action === "revoke"
                ? "Revoca e chiudi le VPN"
                : "Annulla preparazione"
          }
          onCancel={() => setConfirmation(null)}
          onConfirm={() => change.mutate()}
          busy={change.isPending}
          error={change.error}
        >
          <p>
            {confirmation.action === "activate"
              ? "Il profilo precedente sarà revocato. L’attivazione richiede che le sessioni VPN siano chiuse e la pulizia terminata. Conserva il nuovo file prima di continuare."
              : confirmation.action === "revoke"
                ? "Le credenziali diventeranno inutilizzabili per nuove connessioni. Le VPN interessate verranno chiuse. Per riprendere servirà un nuovo profilo."
                : "Il pacchetto preparato sarà eliminato. Le credenziali già attive restano utilizzabili."}
          </p>
        </Confirm>
      )}
    </section>
  );
}

export default function ProvisionGateway() {
  const groups = useGroups();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [form, setForm] = useState({
    display_name: "",
    serial_number: "",
    imei: "",
    iccid: "",
    group_id: "",
    mac_address: "",
    activation_mode: "sms",
    phone_number: "",
  });
  const [operation, setOperation] = useState<{
    key: string;
    fingerprint: string;
    id: string;
  } | null>(null);
  const create = useMutation({
    mutationFn: ({ body, key }: { body: object; key: string }) =>
      api<ProvisioningJob>("/provisioning/gateways", {
        method: "POST",
        idempotencyKey: key,
        body,
      }),
    onSuccess: async (job) => {
      await client.invalidateQueries({ queryKey: ["gateways"] });
      navigate(`/gateways/${job.peer_id}`);
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    const values = {
      ...form,
      mac_address: form.mac_address.trim() || null,
      phone_number: form.activation_mode === "sms" ? form.phone_number : null,
    };
    const fingerprint = JSON.stringify(values);
    const current =
      operation?.fingerprint === fingerprint
        ? operation
        : { key: crypto.randomUUID(), id: crypto.randomUUID(), fingerprint };
    setOperation(current);
    create.mutate({
      key: current.key,
      body: { ...values, gateway_id: current.id },
    });
  }
  return (
    <>
      <PageTitle
        title="Aggiungi gateway"
        description="Prepara identità, configurazione e profilo VPN del nuovo gateway."
        back="/vpn"
      />
      <section className="card padded">
        <form onSubmit={submit}>
          <fieldset disabled={create.isPending} className="provisioning-fields">
            <div className="form-grid">
              <label>
                Nome
                <input
                  required
                  maxLength={100}
                  value={form.display_name}
                  onChange={(e) =>
                    setForm({ ...form, display_name: e.target.value })
                  }
                />
              </label>
              <label>
                Gruppo
                <select
                  required
                  value={form.group_id}
                  onChange={(e) =>
                    setForm({ ...form, group_id: e.target.value })
                  }
                >
                  <option value="">Seleziona gruppo</option>
                  {groups.data?.map((g) => (
                    <option key={g.id} value={g.id}>
                      {g.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                MAC registrato{" "}
                {form.activation_mode === "sms" ? "(facoltativo)" : ""}
                <input
                  required={form.activation_mode === "wifi"}
                  placeholder="AA:BB:CC:DD:EE:FF"
                  value={form.mac_address}
                  onChange={(e) =>
                    setForm({ ...form, mac_address: e.target.value })
                  }
                />
              </label>
              <GatewayIdentifierFields
                value={form}
                onChange={(field, value) =>
                  setForm({ ...form, [field]: value })
                }
              />
              <label>
                Attivazione
                <select
                  value={form.activation_mode}
                  onChange={(e) =>
                    setForm({ ...form, activation_mode: e.target.value })
                  }
                >
                  <option value="sms">SMS</option>
                  <option value="wifi">Wi-Fi</option>
                </select>
              </label>
              {form.activation_mode === "sms" && (
                <label>
                  Numero telefono
                  <input
                    required
                    type="tel"
                    placeholder="+393331234567"
                    value={form.phone_number}
                    onChange={(e) =>
                      setForm({ ...form, phone_number: e.target.value })
                    }
                  />
                </label>
              )}
            </div>
          </fieldset>
          <ErrorBox error={groups.error || create.error} />
          <div className="form-actions">
            <button
              className="button"
              disabled={create.isPending || !groups.data?.length}
            >
              Prepara gateway
            </button>
            <Link to="/vpn" className="button secondary">
              Torna ai gateway
            </Link>
          </div>
        </form>
      </section>
    </>
  );
}
