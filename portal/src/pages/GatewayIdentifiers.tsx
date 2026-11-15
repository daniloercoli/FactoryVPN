type Identifiers = {
  serial_number?: string | null;
  imei?: string | null;
  iccid?: string | null;
};

const fields = [
  ["serial_number", "Matricola", 100],
  ["imei", "IMEI modem", 15],
  ["iccid", "ICCID SIM", 22],
] as const;

export function GatewayIdentifierFields({
  value,
  onChange,
}: {
  value: Identifiers;
  onChange: (field: keyof Identifiers, value: string) => void;
}) {
  return fields.map(([field, label, maxLength]) => (
    <label key={field}>
      {label} (facoltativo)
      <input
        value={value[field] || ""}
        maxLength={maxLength}
        inputMode={field === "serial_number" ? "text" : "numeric"}
        onChange={(event) => onChange(field, event.target.value)}
      />
    </label>
  ));
}

export function GatewayIdentifiers({ value }: { value: Identifiers }) {
  return fields.map(([field, label]) =>
    value[field] ? (
      <small key={field} className="cell-sub mono">
        {label}: {value[field]}
      </small>
    ) : null,
  );
}
