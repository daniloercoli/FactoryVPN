import { useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { Link } from "react-router";
import { api, type EmailChallenge, type User } from "../api/client";
import { useAuth } from "../auth";
import EmailCode from "../EmailCode";
import EmergencyCode from "../EmergencyCode";
import { Badge, ErrorBox, Notice } from "../ui";

export function EmailSecurity({
  onChanged,
}: {
  onChanged: () => Promise<void>;
}) {
  const auth = useAuth();
  const user = auth.user!;
  const [action, setAction] = useState(user.email ? "confirm" : "enable");
  const [password, setPassword] = useState("");
  const [emergency, setEmergency] = useState(false);
  const [emergencyCode, setEmergencyCode] = useState("");
  const [email, setEmail] = useState("");
  const [challenge, setChallenge] = useState<EmailChallenge | null>(null);
  const [saved, setSaved] = useState("");
  const enabled = user.email_two_step || user.email_two_step_required;
  const start = useMutation({
    mutationFn: () =>
      api<EmailChallenge | { completed: true }>(
        action === "confirm" ? "/web/email/confirm" : "/web/email/start",
        {
          method: "POST",
          body:
            action === "confirm"
              ? {
                  password,
                  ...(emergency
                    ? { emergency_code: emergencyCode.trim() }
                    : {}),
                }
              : { action, password, ...(email ? { email } : {}) },
        },
      ),
    onSuccess: async (result) => {
      setPassword("");
      setSaved("");
      if ("challenge_required" in result) {
        setChallenge(result);
      } else {
        setSaved(
          "Identità confermata. Puoi proseguire con le operazioni protette per cinque minuti.",
        );
        await onChanged();
      }
    },
    onSettled: () => setEmergencyCode(""),
  });
  if (user.auth_mode !== "local") return null;
  return (
    <section className="card padded">
      <h2>Verifica in due passaggi via email</h2>
      <Badge tone={enabled ? "green" : ""}>
        {user.email_two_step_required
          ? "Obbligatoria per amministratori"
          : enabled
            ? "Attiva"
            : "Facoltativa · non attiva"}
      </Badge>
      <p>
        {user.email || "Nessun indirizzo configurato"} ·{" "}
        {user.email_verified
          ? "Indirizzo verificato"
          : "Indirizzo da verificare"}
      </p>
      <p className="muted">
        Al login ricevi un codice temporaneo. Proteggi anche l’accesso alla tua
        casella email. Il cambio indirizzo richiede prima il codice sulla
        casella attuale, se verificata, poi quello sulla nuova.
      </p>
      <p className="muted">
        Modificare email o verifica in due passaggi chiude gli altri accessi e
        tutte le VPN dell’account. Questo browser rimane connesso.
      </p>
      {saved && <Notice>{saved}</Notice>}
      {challenge ? (
        <EmailCode
          challenge={challenge}
          onCancel={() => setChallenge(null)}
          onComplete={async (result) => {
            if ("user" in result) await auth.accept(result);
            setChallenge(null);
            setEmail("");
            setAction("confirm");
            setSaved(
              "Verifica completata. Puoi proseguire con le operazioni protette per cinque minuti.",
            );
            await onChanged();
          }}
        />
      ) : (
        <form
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            setSaved("");
            start.mutate();
          }}
        >
          <label>
            Operazione
            <select
              value={action}
              onChange={(e) => {
                setAction(e.target.value);
                setEmail("");
                setEmergency(false);
                setEmergencyCode("");
                start.reset();
              }}
            >
              <option value="confirm">
                Conferma identità per operazioni protette
              </option>
              {!enabled && (
                <option value="enable">Attiva verifica in due passaggi</option>
              )}
              <option value="change">Cambia indirizzo email</option>
              {user.email_two_step && !user.email_two_step_required && (
                <option value="disable">
                  Disattiva verifica in due passaggi
                </option>
              )}
            </select>
          </label>
          {(action === "change" || (!user.email && action === "enable")) && (
            <label>
              Nuovo indirizzo email
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                maxLength={255}
                required
                autoComplete="email"
              />
            </label>
          )}
          <label>
            Password di conferma
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              minLength={8}
              maxLength={128}
              required
              autoComplete="current-password"
            />
          </label>
          {action === "confirm" && (
            <EmergencyCode
              enabled={emergency}
              code={emergencyCode}
              onEnabledChange={(enabled) => {
                setEmergency(enabled);
                setEmergencyCode("");
                start.reset();
              }}
              onCodeChange={setEmergencyCode}
              disabled={start.isPending}
              confirmation
            />
          )}
          <ErrorBox error={start.error} />
          <button className="button secondary" disabled={start.isPending}>
            {action === "confirm" && emergency
              ? "Conferma identità"
              : "Invia codice email"}
          </button>
        </form>
      )}
      <p className="muted">
        Se non puoi accedere alla casella, richiedi il recupero a un
        amministratore globale. Il recupero non disattiva la verifica.
      </p>
    </section>
  );
}

export function EmailRecovery({
  user,
  reload,
}: {
  user: User;
  reload: () => Promise<unknown>;
}) {
  const auth = useAuth();
  const [email, setEmail] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const recovery = useMutation({
    mutationFn: () =>
      api(`/admin/users/${user.id}/email-recovery`, {
        method: "POST",
        revision: user.revision,
        body: { email },
      }),
    onSuccess: async () => {
      await reload();
    },
  });
  if (
    !auth.user?.is_admin ||
    user.auth_mode !== "local" ||
    user.id === auth.user.id
  )
    return null;
  return (
    <section className="card padded danger-zone">
      <h2>Recupero email dell’account</h2>
      <p>
        Usa questa procedura solo dopo aver verificato l’identità della persona
        tramite un canale fidato. Revoca tutti gli accessi e le VPN e rende
        obbligatorio il codice sulla nuova email.
      </p>
      <p>
        Serve una verifica recente dell’amministratore:{" "}
        <Link to="/profile">conferma la tua identità dal profilo</Link>.
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (confirmed) recovery.mutate();
        }}
      >
        <label>
          Email per il recupero
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            maxLength={255}
            required
          />
        </label>
        <label>
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(e) => setConfirmed(e.target.checked)}
            required
          />{" "}
          Ho verificato l’identità della persona e il nuovo indirizzo
        </label>
        <ErrorBox error={recovery.error} />
        <button
          className="button danger-outline"
          disabled={!confirmed || recovery.isPending}
        >
          Recupera email e revoca accessi
        </button>
      </form>
    </section>
  );
}
