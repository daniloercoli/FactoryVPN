import { useEffect, useState, type FormEvent } from "react";
import { api, type Session, type EmailChallenge } from "./api/client";
import { ErrorBox } from "./ui";

export default function EmailCode({
  challenge,
  prefix = "/web/email",
  onComplete,
  onCancel,
}: {
  challenge: EmailChallenge;
  prefix?: string;
  onComplete: (
    result: Session | { completed: boolean },
  ) => void | Promise<void>;
  onCancel: () => void;
}) {
  const [current, setCurrent] = useState(challenge);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [resendAt, setResendAt] = useState(
    Date.now() + challenge.resend_after * 1000,
  );
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const expired = now >= Date.parse(current.expires_at);
  const wait = Math.max(0, Math.ceil((resendAt - now) / 1000));
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await api<
        Session | EmailChallenge | { completed: boolean }
      >(prefix + "/verify", {
        method: "POST",
        body: { challenge_token: current.challenge_token, code },
      });
      setCode("");
      if ("challenge_required" in result) {
        setCurrent(result);
        setResendAt(Date.now() + result.resend_after * 1000);
      } else await onComplete(result);
    } catch (e) {
      setError(e);
      setCode("");
    } finally {
      setBusy(false);
    }
  }
  async function resend() {
    setBusy(true);
    setError(null);
    try {
      const result = await api<EmailChallenge>(prefix + "/resend", {
        method: "POST",
        body: { challenge_token: current.challenge_token },
      });
      setCurrent(result);
      setCode("");
      setResendAt(Date.now() + result.resend_after * 1000);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={submit} className="stack">
      <p role="status">
        Codice inviato a <strong>{current.destination}</strong>. Controlla anche
        la posta indesiderata.
      </p>
      <label>
        Codice email
        <input
          name="email-code"
          value={code}
          onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
          inputMode="numeric"
          autoComplete="one-time-code"
          pattern="[0-9]{6}"
          minLength={6}
          maxLength={6}
          required
          autoFocus
          disabled={expired}
        />
      </label>
      {expired && (
        <p role="alert">
          Codice scaduto. Torna indietro e ripeti l’operazione.
        </p>
      )}
      <ErrorBox error={error} />
      <div className="form-actions">
        <button className="button" disabled={busy || expired}>
          Verifica codice
        </button>
        <button
          type="button"
          className="button secondary"
          disabled={busy || expired || wait > 0}
          onClick={() => void resend()}
        >
          {wait ? `Invia di nuovo tra ${wait}s` : "Invia di nuovo"}
        </button>
        <button
          type="button"
          className="button secondary"
          disabled={busy}
          onClick={onCancel}
        >
          Indietro
        </button>
      </div>
      <small>
        Il codice dura 5 minuti e consente al massimo 5 tentativi. Un nuovo
        invio sostituisce il codice senza prolungarne la scadenza.
      </small>
    </form>
  );
}
