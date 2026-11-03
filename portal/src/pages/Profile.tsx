import { EmailSecurity } from "./EmailSecurity";
import { VPNProfile } from "./Provisioning";
import { useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useOutletContext } from "react-router";
import { ShieldCheck, KeyRound } from "lucide-react";
import { useAuth } from "../auth";
import { api, type Session, type CurrentUser } from "../api/client";
import { useGroups } from "../hooks";
import {
  Badge,
  Confirm,
  ErrorBox,
  GroupTags,
  Notice,
  PageTitle,
  permissionInfo,
} from "../ui";

export default function Profile() {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const groups = useGroups();
  const { logoutAll } = useOutletContext<{ logoutAll: () => void }>();
  const [reloadError, setReloadError] = useState<unknown>(null);
  const [snapshot, setSnapshot] = useState(auth.user!);
  const [name, setName] = useState(snapshot.full_name || "");
  const [email, setEmail] = useState(snapshot.email || "");
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [saved, setSaved] = useState("");
  const profile = useMutation({
    mutationFn: () =>
      api<CurrentUser>("/auth/me", {
        method: "PATCH",
        revision: snapshot.revision,
        body: { full_name: name || null },
      }),
    onSuccess: (user) => {
      setSnapshot(user);
      queryClient.setQueryData(["session"], { ...auth.session!, user });
      setSaved("Profilo aggiornato.");
    },
  });
  const password = useMutation({
    mutationFn: () =>
      api<Session>("/web/password", {
        method: "POST",
        body: { current_password: current, new_password: next },
      }),
    onSuccess: async (session) => {
      await auth.accept(session);
      setSnapshot(session.user);
      setConfirm(false);
      setCurrent("");
      setNext("");
      setSaved(
        "Password aggiornata. Gli altri accessi e le VPN sono stati revocati.",
      );
    },
  });
  async function reload() {
    try {
      setReloadError(null);
      const session = await api<Session>("/web/session");
      setSnapshot(session.user);
      setName(session.user.full_name || "");
      setEmail(session.user.email || "");
      queryClient.setQueryData(["session"], session);
      profile.reset();
    } catch (error) {
      setReloadError(error);
    }
  }
  function submitPassword(e: FormEvent) {
    e.preventDefault();
    password.reset();
    setConfirm(true);
  }
  return (
    <>
      <PageTitle
        title="Il mio profilo"
        description="Le tue informazioni, le credenziali e l’ambito di accesso."
      />
      {saved && <Notice>{saved}</Notice>}
      <div className="editor-grid">
        <div className="stack">
          <section className="card padded">
            <h2>Informazioni personali</h2>
            <p className="muted">
              Username: <strong>{auth.user!.username}</strong>
            </p>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                profile.mutate();
              }}
            >
              <div className="form-grid">
                <label>
                  Nome completo
                  <input
                    value={name}
                    maxLength={200}
                    onChange={(e) => setName(e.target.value)}
                    autoComplete="name"
                  />
                </label>
                <label>
                  Email
                  <input
                    type="email"
                    value={email}
                    maxLength={255}
                    readOnly
                    autoComplete="email"
                  />
                </label>
              </div>
              <ErrorBox
                error={reloadError || profile.error}
                reload={() => void reload()}
              />
              <div className="form-actions">
                <button className="button" disabled={profile.isPending}>
                  Salva profilo
                </button>
              </div>
            </form>
          </section>
          <section className="card padded">
            <h2>
              <KeyRound size={20} /> Credenziali
            </h2>
            {auth.user!.auth_mode === "local" ? (
              <form onSubmit={submitPassword}>
                <p className="muted">
                  Una nuova password revoca gli altri accessi e le sessioni VPN
                  dell’account.
                </p>
                <label>
                  Password attuale
                  <input
                    type="password"
                    required
                    minLength={8}
                    maxLength={128}
                    value={current}
                    onChange={(e) => setCurrent(e.target.value)}
                    autoComplete="current-password"
                  />
                </label>
                <label>
                  Nuova password
                  <input
                    type="password"
                    required
                    minLength={8}
                    maxLength={128}
                    value={next}
                    onChange={(e) => setNext(e.target.value)}
                    autoComplete="new-password"
                  />
                </label>
                <button
                  className="button secondary"
                  disabled={password.isPending}
                >
                  Aggiorna password
                </button>
              </form>
            ) : (
              <p className="muted">
                Il tuo account usa SSO. Password, recupero dell’account e MFA
                sono gestiti dal provider aziendale.
              </p>
            )}
          </section>
          <EmailSecurity onChanged={reload} />
          <VPNProfile kind="technician" peerId={auth.user!.id} />
          <section className="card padded danger-zone">
            <h2>Revoca globale</h2>
            <p>
              Disconnetti tutti gli accessi dell’account, compresi CLI, altri
              browser e VPN.
            </p>
            <button className="button danger-outline" onClick={logoutAll}>
              Disconnetti tutti gli accessi
            </button>
          </section>
        </div>
        <aside className="card padded summary-card">
          <ShieldCheck size={25} />
          <h2>Accesso effettivo</h2>
          <Badge tone="blue">
            {auth.user!.is_admin
              ? "Amministratore globale"
              : "Permessi per gruppi"}
          </Badge>
          <p className="section-label">GRUPPI</p>
          <GroupTags ids={auth.user!.group_ids} groups={groups.data || []} />
          {auth.user!.is_admin && (
            <p className="muted">
              L’amministratore globale opera su tutti i gruppi.
            </p>
          )}
          <p className="section-label">PERMESSI</p>
          <ul className="plain-list">
            {auth.user!.permissions.map((p) => (
              <li key={p}>{permissionInfo[p]?.[0] || p}</li>
            ))}
          </ul>
        </aside>
      </div>
      {confirm && (
        <Confirm
          title="Aggiornare la password?"
          label="Aggiorna e revoca gli altri accessi"
          onCancel={() => setConfirm(false)}
          onConfirm={() => password.mutate()}
          busy={password.isPending}
          error={password.error}
        >
          <p>
            Rimarrai connesso in questo browser. Gli altri accessi e le sessioni
            VPN saranno chiusi.
          </p>
        </Confirm>
      )}
    </>
  );
}
