import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type GatewayDetail } from "../api/client";
import { Confirm, Notice } from "../ui";

export type Connection = {
  activation_mode: "sms" | "wifi" | null;
  phone_number: string;
  mac_address: string;
};

export function GatewayConnectionFields({
  value,
  onChange,
  incomplete = false,
}: {
  value: Connection;
  onChange: (value: Connection) => void;
  incomplete?: boolean;
}) {
  return (
    <>
      <label>
        Modalità di attivazione
        <select
          value={value.activation_mode || ""}
          required={!incomplete}
          onChange={(event) =>
            onChange({
              ...value,
              activation_mode:
                (event.target.value as Connection["activation_mode"]) || null,
            })
          }
        >
          {incomplete && <option value="">Da definire</option>}
          <option value="sms">SMS</option>
          <option value="wifi">Wi-Fi</option>
        </select>
      </label>
      <label>
        Numero di telefono
        <input
          value={value.phone_number}
          type="tel"
          maxLength={16}
          pattern="\+[1-9][0-9]{9,14}"
          required={!incomplete && value.activation_mode === "sms"}
          placeholder="+393331234567"
          onChange={(event) =>
            onChange({ ...value, phone_number: event.target.value })
          }
        />
      </label>
      <label>
        MAC registrato
        <input
          value={value.mac_address}
          maxLength={17}
          pattern="[0-9A-Fa-f]{2}((:|-)[0-9A-Fa-f]{2}){5}"
          required={!incomplete && value.activation_mode === "wifi"}
          placeholder="AA:BB:CC:DD:EE:FF"
          onChange={(event) =>
            onChange({ ...value, mac_address: event.target.value })
          }
        />
      </label>
    </>
  );
}

export default function GatewayConnection({
  gateway,
}: {
  gateway: GatewayDetail;
}) {
  const client = useQueryClient();
  const form = useRef<HTMLFormElement>(null);
  const [draft, setDraft] = useState<{
    revision: string;
    connection: Connection;
  } | null>(null);
  const [saved, setSaved] = useState(false);
  const mutation = useMutation({
    mutationFn: (value: NonNullable<typeof draft>) =>
      api(`/admin/gateways/${gateway.id}`, {
        method: "PATCH",
        revision: value.revision,
        body: {
          activation_mode: value.connection.activation_mode,
          phone_number: value.connection.phone_number.trim() || null,
          mac_address: value.connection.mac_address.trim() || null,
        },
      }),
    onSuccess: async () => {
      setDraft(null);
      setSaved(true);
      await Promise.all(
        ["gateways", "vpn", "provisioning", "enrollments", "audit"].map((key) =>
          client.invalidateQueries({ queryKey: [key] }),
        ),
      );
    },
  });
  const reserved = ["busy", "cleanup_pending"].includes(gateway.availability);
  function save() {
    if (!draft || mutation.isPending || !form.current?.reportValidity()) return;
    mutation.mutate(draft);
  }
  return (
    <>
      <div className="vpn-actions">
        <button
          className="button secondary small"
          disabled={reserved}
          onClick={() => {
            mutation.reset();
            setDraft({
              revision: gateway.revision,
              connection: {
                activation_mode: gateway.activation_mode,
                phone_number: gateway.phone_number || "",
                mac_address: gateway.mac_address || "",
              },
            });
          }}
        >
          Modifica connessione
        </button>
        {reserved && (
          <span className="muted">
            Per modificare la connessione, chiudi la VPN e attendi il rilascio.
          </span>
        )}
      </div>
      {saved && (
        <Notice>
          Connessione aggiornata. Se hai cambiato modalità, telefono o MAC,
          prepara un nuovo pacchetto nella sezione Provisioning e credenziali,
          scaricalo, attivalo e installalo sul gateway.
        </Notice>
      )}
      {draft && (
        <Confirm
          title="Modificare la connessione del gateway?"
          label="Salva e revoca il pacchetto precedente"
          onCancel={() => setDraft(null)}
          onConfirm={save}
          busy={mutation.isPending}
          error={mutation.error}
        >
          <form
            ref={form}
            onSubmit={(event) => {
              event.preventDefault();
              save();
            }}
          >
            <GatewayConnectionFields
              value={draft.connection}
              onChange={(connection) => setDraft({ ...draft, connection })}
            />
          </form>
          <p>
            Cambiare modalità, numero di telefono o MAC revoca le credenziali
            precedenti. Dopo il salvataggio devi preparare e installare un nuovo
            pacchetto. Il gateway conserva identità, inventario e IP VPN.
          </p>
          {mutation.error && (
            <button
              className="text-button"
              onClick={() => {
                setDraft(null);
                void client.invalidateQueries({ queryKey: ["gateways"] });
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
