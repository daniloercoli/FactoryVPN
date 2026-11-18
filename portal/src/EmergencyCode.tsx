export default function EmergencyCode({
  enabled,
  code,
  onEnabledChange,
  onCodeChange,
  disabled = false,
  confirmation = false,
}: {
  enabled: boolean;
  code: string;
  onEnabledChange: (enabled: boolean) => void;
  onCodeChange: (code: string) => void;
  disabled?: boolean;
  confirmation?: boolean;
}) {
  return (
    <>
      <label className="check-row">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(event) => onEnabledChange(event.target.checked)}
          disabled={disabled}
        />
        <span>Usa codice di emergenza</span>
      </label>
      {enabled && (
        <>
          <label>
            Codice di emergenza
            <input
              name="emergency-code"
              value={code}
              onChange={(event) => onCodeChange(event.target.value)}
              autoComplete="one-time-code"
              autoCapitalize="characters"
              spellCheck={false}
              maxLength={64}
              required
              disabled={disabled}
            />
          </label>
          <small className="emergency-code-help">
            {confirmation
              ? "Chiedi all’amministratore un nuovo codice per confermare questa operazione. Il codice usato per accedere è già consumato."
              : "Usa il codice fornito dall’amministratore per il tuo account. La durata dell’accesso rimane quella normale."}{" "}
            Il codice è monouso e scade dieci minuti dopo l’emissione.
          </small>
        </>
      )}
    </>
  );
}
