import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { api, ApiError, page, type ProvisioningJob } from "../api/client";
import type { Enrollment, EnrollmentState } from "../api/enrollment";
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
import { GatewayIdentifiers } from "./GatewayIdentifiers";
import { GatewayConnectionFields } from "./GatewayConnection";

const stateNames: Record<EnrollmentState, string> = {
  prepared: "Preparato in fabbrica",
  awaiting_approval: "Da approvare",
  awaiting_data: "Approvato · dati incompleti",
  approved: "Approvato · attesa richiesta",
  preparing: "Pacchetto in preparazione",
  ready: "Pacchetto pronto",
  completed: "Pacchetto attivato",
  blocked: "Bloccato",
  manual: "Gestione manuale",
};
const missingNames: Record<string, string> = {
  activation_mode: "modalità di attivazione",
  phone_number: "numero di telefono per SMS",
  mac_address: "MAC per Wi-Fi",
};

function EnrollmentBadge({ value }: { value: Enrollment }) {
  return (
    <Badge
      tone={
        value.state === "blocked"
          ? "red"
          : value.state === "completed"
            ? "green"
            : "amber"
      }
    >
      {stateNames[value.state]}
    </Badge>
  );
}

export default function Enrollments() {
  const groups = useGroups();
  const [params, setParams] = useSearchParams();
  const q = params.get("q") || "";
  const state = params.get("state") || "";
  const offset = Math.max(0, Number(params.get("offset")) || 0);
  const query = useQuery({
    queryKey: ["enrollments", "list", q, state, offset],
    queryFn: ({ signal }) =>
      page<Enrollment>(
        "/admin/enrollments",
        { q, state, offset, limit: 20 },
        signal,
      ),
    refetchInterval: 10000,
    refetchOnWindowFocus: true,
  });
  function filter(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key !== "offset") next.delete("offset");
    setParams(next, { replace: true });
  }
  return (
    <>
      <PageTitle
        title="Arruolamenti"
        description="Dispositivi preparati in fabbrica, approvazione e consegna del pacchetto."
        action={
          <Link className="button secondary" to="/gateways/new">
            Aggiungi gateway manualmente
          </Link>
        }
      />
      <Notice>
        Puoi approvare e assegnare un dispositivo anche prima che la SIM sia
        attiva. Il gateway completerà i dati quando avrà una connessione, anche
        mesi dopo. L’arruolamento non indica che una VPN sia aperta.
      </Notice>
      <section className="card gateway-section">
        <div className="filters">
          <label className="search-field">
            Cerca arruolamenti
            <input
              value={q}
              maxLength={100}
              placeholder="Nome, UUID, matricola, MAC, IMEI, ICCID o telefono"
              onChange={(event) => filter("q", event.target.value)}
            />
          </label>
          <label>
            Stato arruolamento
            <select
              value={state}
              onChange={(event) => filter("state", event.target.value)}
            >
              <option value="">Tutti gli stati</option>
              {Object.entries(stateNames).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <button
            className="button secondary small"
            disabled={query.isFetching}
            onClick={() => void query.refetch()}
          >
            Aggiorna arruolamenti
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
                        <th>Dispositivo</th>
                        <th>Identificativi</th>
                        <th>Arruolamento</th>
                        <th>Gruppo</th>
                        <th>Preparato il</th>
                      </tr>
                    </thead>
                    <tbody>
                      {query.data.items.map((entry) => (
                        <tr key={entry.id}>
                          <td>
                            <Link to={`/enrollments/${entry.id}`}>
                              <strong>
                                {entry.display_name || entry.serial_number}
                              </strong>
                            </Link>
                            <small className="cell-sub mono">
                              UUID: {entry.id}
                            </small>
                          </td>
                          <td>
                            <GatewayIdentifiers value={entry} />
                            {entry.mac_address && (
                              <small className="cell-sub mono">
                                MAC: {entry.mac_address}
                              </small>
                            )}
                            {entry.phone_number && (
                              <small className="cell-sub mono">
                                Telefono: {entry.phone_number}
                              </small>
                            )}
                          </td>
                          <td>
                            <EnrollmentBadge value={entry} />
                          </td>
                          <td>
                            {groups.data?.find(
                              (group) => group.id === entry.group_id,
                            )?.name ||
                              (entry.group_id
                                ? "Gruppo non disponibile"
                                : "Non assegnato")}
                          </td>
                          <td>{date(entry.created_at)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty
                  title={
                    q || state
                      ? "Nessun arruolamento con questi filtri"
                      : "Nessun dispositivo in arruolamento"
                  }
                >
                  I dispositivi compariranno dopo la preparazione in fabbrica.
                  Puoi sempre aggiungere un gateway manualmente, anche per le
                  prove.
                </Empty>
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
    </>
  );
}

type EditValues = {
  serial_number: string;
  display_name: string;
  activation_mode: "sms" | "wifi" | null;
  phone_number: string;
  mac_address: string;
  imei: string;
  iccid: string;
  external_reference: string;
};
type Draft = {
  action: "edit" | "approve" | "block" | "manual" | "resume";
  snapshot: Enrollment;
  values: EditValues;
  group: string;
  reason: string;
  requestKey: string;
};

const blockReasons: Record<string, string> = {
  enrollment_revoked: "Arruolamento revocato da un amministratore.",
  group_changed:
    "Il gruppo è cambiato. Occorre preparare un nuovo pacchetto nel gruppo di destinazione.",
  installation_restored:
    "L’installazione è stata ripristinata. Un amministratore deve riprendere la configurazione manualmente.",
};

export function EnrollmentDetail() {
  const { id } = useParams();
  return <EnrollmentCard key={id} id={id!} />;
}

function EnrollmentCard({ id }: { id: string }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const groups = useGroups();
  const form = useRef<HTMLFormElement>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [notice, setNotice] = useState("");
  const query = useQuery({
    queryKey: ["enrollments", "detail", id],
    queryFn: ({ signal }) =>
      api<Enrollment>(`/admin/enrollments/${encodeURIComponent(id)}`, {
        signal,
      }),
    refetchInterval: 10000,
    refetchOnWindowFocus: true,
  });
  const accessLost =
    query.error instanceof ApiError &&
    [401, 403, 404].includes(query.error.status);
  useEffect(() => {
    if (accessLost) setDraft(null);
  }, [accessLost]);
  async function refresh() {
    await Promise.all(
      [
        "enrollments",
        "gateways",
        "provisioning",
        "groups",
        "stats",
        "audit",
      ].map((key) => client.invalidateQueries({ queryKey: [key] })),
    );
  }
  const mutation = useMutation({
    mutationFn: async (value: Draft) => {
      const base = `/admin/enrollments/${id}`;
      const body =
        value.action === "edit"
          ? Object.fromEntries(
              Object.entries(value.values)
                .filter(
                  ([key]) =>
                    !value.snapshot.completed_at ||
                    ![
                      "activation_mode",
                      "phone_number",
                      "mac_address",
                    ].includes(key),
                )
                .map(([key, item]) => [
                  key,
                  typeof item === "string" ? item.trim() || null : item,
                ]),
            )
          : value.action === "approve"
            ? { group_id: value.group }
            : value.action === "block"
              ? { reason: value.reason.trim() }
              : value.action === "manual"
                ? { group_id: value.group, expected_certificate_id: null }
                : undefined;
      return api<Enrollment | ProvisioningJob>(
        value.action === "edit" ? base : `${base}/${value.action}`,
        {
          method: value.action === "edit" ? "PATCH" : "POST",
          revision: value.snapshot.revision,
          idempotencyKey:
            value.action === "manual" ? value.requestKey : undefined,
          body,
        },
      );
    },
    onSuccess: async (result, value) => {
      setDraft(null);
      setNotice(
        value.action === "approve"
          ? "Dispositivo approvato e assegnato. Il gateway può completare i dati al suo primo collegamento."
          : "Arruolamento aggiornato.",
      );
      await refresh();
      if (value.action === "manual" && "peer_id" in result)
        navigate(`/gateways/${result.peer_id}`);
    },
  });
  function open(action: Draft["action"]) {
    if (!query.data) return;
    const entry = query.data;
    mutation.reset();
    setDraft({
      action,
      snapshot: entry,
      group: entry.group_id || "",
      reason: "",
      requestKey: crypto.randomUUID(),
      values: {
        serial_number: entry.serial_number,
        display_name: entry.display_name || "",
        activation_mode: entry.activation_mode || null,
        phone_number: entry.phone_number || "",
        mac_address: entry.mac_address || "",
        imei: entry.imei || "",
        iccid: entry.iccid || "",
        external_reference: entry.external_reference || "",
      },
    });
  }
  function save() {
    if (
      !draft ||
      mutation.isPending ||
      (form.current && !form.current.reportValidity())
    )
      return;
    mutation.mutate(draft);
  }
  if (query.isPending) return <Loading />;
  if (!query.data || accessLost)
    return (
      <>
        <PageTitle
          title="Arruolamento non disponibile"
          description="Ricarica la pagina o torna all’elenco dei dispositivi."
          back="/enrollments"
        />
        <ErrorBox error={query.error} reload={() => void query.refetch()} />
      </>
    );
  const entry = query.data;
  const incomplete = entry.missing_fields.length > 0;
  const locked = !!query.error || query.isFetching;
  const fields = [
    ["Matricola", entry.serial_number],
    [
      "Gruppo",
      groups.data?.find((group) => group.id === entry.group_id)?.name ||
        (entry.group_id ? "Gruppo non disponibile" : "Non assegnato"),
    ],
    [
      "Modalità di attivazione",
      entry.activation_mode === "sms"
        ? "SMS"
        : entry.activation_mode === "wifi"
          ? "Wi-Fi"
          : "Da definire",
    ],
    ["Telefono", entry.phone_number || "Non ancora disponibile"],
    ["MAC registrato", entry.mac_address || "Non ancora disponibile"],
    ["IMEI modem", entry.imei || "Non ancora disponibile"],
    ["ICCID SIM", entry.iccid || "Non ancora disponibile"],
    ["Riferimento esterno", entry.external_reference || "Non indicato"],
    [
      "Indirizzo VPN fisso",
      entry.vpn_ip || "Assegnato dopo la registrazione del gateway",
    ],
    ["Preparato il", date(entry.created_at)],
    ["Approvato il", date(entry.approved_at)],
    ["Pacchetto attivato il", date(entry.completed_at)],
  ];
  const titles: Record<Draft["action"], string> = {
    edit: "Dati del dispositivo",
    approve: "Approva e assegna al gruppo",
    block: "Bloccare il dispositivo?",
    manual: "Passare alla gestione manuale?",
    resume: "Riprendere l’arruolamento automatico?",
  };
  const labels: Record<Draft["action"], string> = {
    edit: "Salva dati",
    approve: "Approva e assegna",
    block: "Blocca dispositivo",
    manual: "Prepara pacchetto manuale",
    resume: "Riprendi arruolamento automatico",
  };
  return (
    <>
      <PageTitle
        title={entry.display_name || entry.serial_number}
        description="Arruolamento del dispositivo · approvazione e consegna"
        back="/enrollments"
        action={
          <button
            className="button secondary"
            disabled={query.isFetching}
            onClick={() => void query.refetch()}
          >
            Aggiorna arruolamento
          </button>
        }
      />
      <ErrorBox
        error={query.error || groups.error}
        reload={() => void refresh()}
      />
      {notice && <Notice>{notice}</Notice>}
      <section className="card padded stack" aria-label="Stato arruolamento">
        <div className="card-heading">
          <h2>Arruolamento</h2>
          <EnrollmentBadge value={entry} />
        </div>
        <p>
          Questo stato riguarda la preparazione e la consegna del pacchetto. Per
          raggiungere i servizi del gateway occorre poi aprire una sessione VPN
          autorizzata.
        </p>
        {incomplete && (
          <Notice>
            Dati ancora necessari:{" "}
            {entry.missing_fields
              .map(
                (field) =>
                  missingNames[field] || "configurazione del dispositivo",
              )
              .join(", ")}
            .
            {entry.state !== "blocked" &&
              " Puoi comunque approvare e assegnare il dispositivo."}
          </Notice>
        )}
        {!entry.completed_at && (
          <p className="muted">
            La SIM può rimanere inattiva in magazzino. Il dispositivo può
            completare l’arruolamento mesi dopo, quando dispone di Internet.
            IMEI e ICCID possono essere aggiunti successivamente.
          </p>
        )}
        {entry.state === "completed" && !entry.vpn_ip && (
          <Notice>
            Il pacchetto è attivato. Si attende la registrazione del gateway con
            i file installati; l’IP VPN verrà assegnato a quel passaggio. Il
            tunnel VPN non viene aperto automaticamente.
          </Notice>
        )}
        {entry.blocked_reason && (
          <Notice>
            {blockReasons[entry.blocked_reason] || entry.blocked_reason}
          </Notice>
        )}
        {entry.job?.error_code && (
          <Notice>
            La preparazione del pacchetto non è riuscita. Verifica il servizio
            di emissione prima di riprovare.
          </Notice>
        )}
        {entry.job &&
          ["queued", "ready"].includes(entry.job.state) &&
          Date.parse(entry.job.expires_at) > Date.now() && (
            <p>
              Pacchetto disponibile fino al {date(entry.job.expires_at)}.{" "}
              {entry.delivery_mode === "automatic"
                ? "Il gateway lo scaricherà e confermerà la sua installazione."
                : "Scarica e attiva il pacchetto dalla scheda gateway con l’accesso che lo ha richiesto."}
            </p>
          )}
        <dl className="vpn-details">
          {fields.map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
        <p className="page-note mono">UUID: {entry.id}</p>
        <div className="vpn-actions">
          <button
            className="button secondary"
            disabled={locked}
            onClick={() => open("edit")}
          >
            Modifica dati
          </button>
          {!entry.completed_at &&
            entry.state !== "blocked" &&
            entry.delivery_mode === "automatic" && (
              <button
                className="button"
                disabled={locked || groups.isPending || !!groups.error}
                onClick={() => open("approve")}
              >
                {entry.approved_at
                  ? "Aggiorna gruppo di approvazione"
                  : "Approva e assegna"}
              </button>
            )}
          {!entry.completed_at && entry.delivery_mode === "automatic" && (
            <button
              className="button secondary"
              disabled={
                locked || incomplete || groups.isPending || !!groups.error
              }
              onClick={() => open("manual")}
            >
              Passa alla gestione manuale
            </button>
          )}
          {entry.can_resume && (
            <button
              className="button secondary"
              disabled={locked}
              onClick={() => open("resume")}
            >
              Riprendi arruolamento automatico
            </button>
          )}
          {entry.state !== "blocked" && (
            <button
              className="button danger-outline"
              disabled={locked}
              onClick={() => open("block")}
            >
              Blocca dispositivo
            </button>
          )}
          {entry.gateway_id && (
            <Link
              className="button secondary"
              to={`/gateways/${entry.gateway_id}`}
            >
              Apri scheda gateway
            </Link>
          )}
        </div>
        {incomplete && (
          <p className="muted">
            Per preparare uno ZIP manuale completa prima i dati richiesti. Il
            gruppo potrà essere scelto al momento della preparazione.
          </p>
        )}
        {entry.delivery_mode === "manual" && (
          <Notice>
            La consegna automatica è sospesa. Usa la scheda gateway per
            scaricare e attivare il pacchetto, poi copia i file sul dispositivo.
          </Notice>
        )}
      </section>
      {draft && (
        <Confirm
          title={titles[draft.action]}
          label={labels[draft.action]}
          onCancel={() => setDraft(null)}
          onConfirm={save}
          busy={mutation.isPending}
          error={mutation.error}
          danger={draft.action === "block"}
        >
          {query.error && (
            <p className="notice error">
              Non è stato possibile aggiornare i dati. Le modifiche nel modulo
              sono conservate.
            </p>
          )}
          <form
            ref={form}
            onSubmit={(event) => {
              event.preventDefault();
              save();
            }}
          >
            {draft.action === "edit" && (
              <>
                <label>
                  Nome descrittivo
                  <input
                    value={draft.values.display_name}
                    maxLength={100}
                    onChange={(event) =>
                      setDraft({
                        ...draft,
                        values: {
                          ...draft.values,
                          display_name: event.target.value,
                        },
                      })
                    }
                  />
                </label>
                <label>
                  Matricola
                  <input
                    required
                    value={draft.values.serial_number}
                    maxLength={100}
                    onChange={(event) =>
                      setDraft({
                        ...draft,
                        values: {
                          ...draft.values,
                          serial_number: event.target.value,
                        },
                      })
                    }
                  />
                </label>
                {!draft.snapshot.completed_at && (
                  <GatewayConnectionFields
                    incomplete={!draft.snapshot.gateway_id}
                    value={draft.values}
                    onChange={(value) =>
                      setDraft({
                        ...draft,
                        values: { ...draft.values, ...value },
                      })
                    }
                  />
                )}
                <label>
                  IMEI modem (facoltativo)
                  <input
                    value={draft.values.imei}
                    inputMode="numeric"
                    pattern="[0-9]{15}"
                    maxLength={15}
                    onChange={(event) =>
                      setDraft({
                        ...draft,
                        values: { ...draft.values, imei: event.target.value },
                      })
                    }
                  />
                </label>
                <label>
                  ICCID SIM (facoltativo)
                  <input
                    value={draft.values.iccid}
                    inputMode="numeric"
                    pattern="[0-9]{18,22}"
                    maxLength={22}
                    onChange={(event) =>
                      setDraft({
                        ...draft,
                        values: { ...draft.values, iccid: event.target.value },
                      })
                    }
                  />
                </label>
                <label>
                  Riferimento esterno (facoltativo)
                  <input
                    value={draft.values.external_reference}
                    maxLength={200}
                    onChange={(event) =>
                      setDraft({
                        ...draft,
                        values: {
                          ...draft.values,
                          external_reference: event.target.value,
                        },
                      })
                    }
                  />
                </label>
                {draft.snapshot.completed_at ? (
                  <p>
                    Le modifiche all’anagrafica e al riferimento esterno
                    conservano le credenziali e non interrompono la VPN. Per
                    cambiare modalità, telefono o MAC apri la{" "}
                    <Link to={`/gateways/${id}`}>scheda gateway</Link>.
                  </p>
                ) : (
                  <p>
                    La matricola deve essere univoca. Modificare modalità,
                    telefono o MAC richiede un nuovo pacchetto; un aggiornamento
                    dei soli IMEI o ICCID non interrompe la preparazione.
                  </p>
                )}
              </>
            )}
            {(draft.action === "approve" || draft.action === "manual") && (
              <label>
                Gruppo di destinazione
                <select
                  required
                  value={draft.group}
                  onChange={(event) =>
                    setDraft({ ...draft, group: event.target.value })
                  }
                >
                  <option value="">Seleziona un gruppo</option>
                  {groups.data?.map((group) => (
                    <option key={group.id} value={group.id}>
                      {group.name}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {draft.action === "approve" && (
              <p>
                Approvi l’identità del dispositivo e il gruppo scelto. I dati
                mancanti possono arrivare in seguito; non è necessario che la
                SIM sia già attiva.
              </p>
            )}
            {draft.action === "manual" && (
              <p>
                La consegna automatica verrà sospesa e l’eventuale pacchetto in
                preparazione annullato. Dalla scheda gateway scarica il nuovo
                ZIP, attivalo e copia i file sul dispositivo. L’identità rimane
                la stessa.
              </p>
            )}
            {draft.action === "block" && (
              <>
                <label>
                  Motivo del blocco
                  <textarea
                    required
                    maxLength={500}
                    value={draft.reason}
                    onChange={(event) =>
                      setDraft({ ...draft, reason: event.target.value })
                    }
                  />
                </label>
                <p>
                  Il dispositivo non potrà proseguire con l’arruolamento. I
                  pacchetti pendenti e le credenziali associate verranno
                  revocati.
                </p>
              </>
            )}
            {draft.action === "resume" && (
              <p>
                Il pacchetto manuale pendente verrà annullato. Il gateway potrà
                richiedere un nuovo pacchetto automatico alla prossima
                connessione. Non installare il precedente ZIP manuale.
              </p>
            )}
          </form>
          {mutation.error && (
            <button
              className="text-button"
              onClick={() => {
                setDraft(null);
                void refresh();
              }}
            >
              Ricarica arruolamento
            </button>
          )}
        </Confirm>
      )}
    </>
  );
}
