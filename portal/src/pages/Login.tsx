import EmailCode from "../EmailCode";
import EmergencyCode from "../EmergencyCode";
import { useState, type FormEvent } from "react";
import {
  Navigate,
  useLocation,
  useNavigate,
  useSearchParams,
} from "react-router";
import { ArrowRight, ShieldCheck, LockKeyhole, Network } from "lucide-react";
import { useAuth } from "../auth";
import { APP_NAME, APP_INITIAL } from "../branding";
import {
  api,
  ApiError,
  type Session,
  type EmailChallenge,
} from "../api/client";
import { useProviders } from "../hooks";
import { Badge, ErrorBox, Loading } from "../ui";

export default function Login() {
  const auth = useAuth();
  const providers = useProviders();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const location = useLocation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [emergency, setEmergency] = useState(false);
  const [emergencyCode, setEmergencyCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [challenge, setChallenge] = useState<EmailChallenge | null>(null);
  const urls: string[] = location.state?.logoutUrls || [];
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await api<Session | EmailChallenge>("/web/login", {
        method: "POST",
        body: {
          username,
          password,
          ...(emergency ? { emergency_code: emergencyCode.trim() } : {}),
        },
      });
      setPassword("");
      if ("challenge_required" in result) {
        setChallenge(result);
        return;
      }
      await auth.accept(result);
      navigate("/", { replace: true });
    } catch (e) {
      setError(e);
      setPassword("");
    } finally {
      setEmergencyCode("");
      setBusy(false);
    }
  }
  async function sso(provider: string) {
    setBusy(true);
    setError(null);
    try {
      const result = await api<{ authorization_url: string }>(
        "/web/sso/start",
        { method: "POST", body: { provider_id: provider } },
      );
      const url = new URL(result.authorization_url);
      if (
        url.origin !== window.location.origin ||
        !url.pathname.startsWith("/api/v1/auth/sso/authorize/")
      )
        throw new Error("Unexpected SSO destination");
      window.location.assign(url.href);
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  }
  if (auth.loading) return <Loading />;
  if (auth.user) return <Navigate to="/" replace />;
  return (
    <main className="login-page">
      <section className="login-story">
        <a className="brand" href="/app/">
          <span className="brand-mark" aria-hidden="true">
            {APP_INITIAL}
          </span>
          <span className="brand-name">{APP_NAME}</span>
        </a>
        <div className="login-story-copy">
          <p className="eyebrow">IL CONTROLLO, AL POSTO GIUSTO</p>
          <h1>
            Le persone giuste.
            <br />
            Gli accessi giusti.
          </h1>
          <p>
            Gestisci utenti, gruppi e autorizzazioni da un unico spazio. Ogni
            risorsa, nel suo ambito.
          </p>
          <div className="network-art" aria-hidden="true">
            <div>
              <Network size={25} /> <span>{APP_NAME}</span>
            </div>
            <span>UTENTI</span>
            <span>GRUPPI</span>
            <span>RISORSE</span>
          </div>
        </div>
        <p className="login-foot">
          <ShieldCheck size={17} /> Accesso alle risorse industriali
        </p>
      </section>
      <section className="login-form-side">
        <div className="login-box">
          <span className="login-icon">
            <LockKeyhole size={25} />
          </span>
          <p className="eyebrow">PORTALE DI AMMINISTRAZIONE</p>
          <h2>Accedi a {APP_NAME}</h2>
          <p className="muted">
            Usa il tuo account locale o l’accesso aziendale.
          </p>
          {params.has("error") && (
            <div className="notice error" role="alert">
              Accesso SSO non completato. Verifica che l’identità sia collegata
              e riprova.
            </div>
          )}
          {location.state?.signedOut && (
            <div className="notice" role="status">
              Disconnessione completata.
            </div>
          )}
          {urls
            .filter((url) => url.startsWith("https://"))
            .map((url) => (
              <p key={url}>
                <a href={url}>
                  Completa la disconnessione dal provider SSO{" "}
                  <ArrowRight size={14} />
                </a>
              </p>
            ))}
          {error instanceof ApiError && error.status === 401 ? (
            <div className="notice error" role="alert">
              Username o password non validi, oppure account non abilitato
              all’accesso locale.
            </div>
          ) : (
            <ErrorBox error={error || auth.error} />
          )}
          {challenge ? (
            <EmailCode
              challenge={challenge}
              prefix="/web/login"
              onCancel={() => setChallenge(null)}
              onEmergency={() => {
                setChallenge(null);
                setEmergency(true);
                setEmergencyCode("");
                setPassword("");
                setError(null);
              }}
              onComplete={async (result) => {
                if ("user" in result) {
                  await auth.accept(result);
                  navigate("/", { replace: true });
                }
              }}
            />
          ) : (
            <form onSubmit={submit}>
              <label>
                Username
                <input
                  name="username"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  required
                  minLength={3}
                  maxLength={50}
                  autoComplete="username"
                  autoFocus={!emergency}
                />
              </label>
              <label>
                Password
                <input
                  name="password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  minLength={8}
                  maxLength={128}
                  autoComplete="current-password"
                  autoFocus={emergency}
                />
              </label>
              <EmergencyCode
                enabled={emergency}
                code={emergencyCode}
                onEnabledChange={(enabled) => {
                  setEmergency(enabled);
                  setEmergencyCode("");
                  setError(null);
                }}
                onCodeChange={setEmergencyCode}
                disabled={busy}
              />
              <button className="button full" disabled={busy}>
                {busy ? "Accesso in corso…" : "Accedi"}
                <ArrowRight size={17} />
              </button>
            </form>
          )}
          {!challenge && !!providers.data?.length && (
            <>
              <div className="divider">
                <span>oppure con l’account aziendale</span>
              </div>
              <div className="sso-buttons">
                {providers.data.map((p) => (
                  <button
                    className="button secondary full"
                    key={p.id}
                    disabled={busy}
                    onClick={() => void sso(p.id)}
                  >
                    {p.label}
                    <Badge tone="amber">SSO WIP</Badge>
                  </button>
                ))}
              </div>
            </>
          )}
          <ErrorBox error={providers.error} />
          <p className="login-help">
            Per recuperare una password locale, contatta il tuo amministratore.
            Per SSO, usa il recupero del provider aziendale.
          </p>
        </div>
        <p className="muted login-copyright">{APP_NAME} · Portale accessi</p>
      </section>
    </main>
  );
}
