import { EmailRecovery } from "./EmailSecurity";
import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { Plus, Search, ArrowUpRight, Trash2, Link2 } from "lucide-react";
import {
  api,
  page,
  type User,
  type UserInput,
  type UserUpdate,
  type Permission,
  type Identity,
} from "../api/client";
import { VPNProfile } from "./Provisioning";
import { useAuth } from "../auth";
import { useGroups, useProviders } from "../hooks";
import {
  Avatar,
  Badge,
  Confirm,
  Empty,
  ErrorBox,
  GroupTags,
  Loading,
  Notice,
  PageTitle,
  Pager,
  date,
  permissionInfo,
} from "../ui";

export default function Users() {
  const [search] = useSearchParams();
  const groups = useGroups();
  const [q, setQ] = useState("");
  const [group, setGroup] = useState(search.get("group") || "");
  const [active, setActive] = useState("");
  const [mode, setMode] = useState("");
  const [offset, setOffset] = useState(0);
  const query = useQuery({
    queryKey: ["users", q, group, active, mode, offset],
    queryFn: ({ signal }) =>
      page<User>(
        "/admin/users",
        {
          q,
          group_id: group,
          is_active: active,
          auth_mode: mode,
          limit: 20,
          offset,
        },
        signal,
      ),
  });
  return (
    <>
      <PageTitle
        title="Utenti"
        description="Gestisci account, appartenenze ai gruppi e autorizzazioni."
        action={
          <Link to="/users/new" className="button">
            <Plus size={17} /> Nuovo utente
          </Link>
        }
      />
      <section className="card">
        <div className="filters">
          <label className="search-field">
            <span>Cerca utenti</span>
            <div>
              <Search size={17} />
              <input
                type="search"
                maxLength={100}
                placeholder="Username, nome o email"
                value={q}
                onChange={(e) => {
                  setQ(e.target.value);
                  setOffset(0);
                }}
              />
            </div>
          </label>
          <label>
            Gruppo
            <select
              value={group}
              onChange={(e) => {
                setGroup(e.target.value);
                setOffset(0);
              }}
            >
              <option value="">Tutti i gruppi</option>
              {groups.data?.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Stato
            <select
              value={active}
              onChange={(e) => {
                setActive(e.target.value);
                setOffset(0);
              }}
            >
              <option value="">Tutti gli stati</option>
              <option value="true">Attivi</option>
              <option value="false">Disabilitati</option>
            </select>
          </label>
          <label>
            Accesso
            <select
              value={mode}
              onChange={(e) => {
                setMode(e.target.value);
                setOffset(0);
              }}
            >
              <option value="">Tutti i metodi</option>
              <option value="local">Locale</option>
              <option value="sso">SSO</option>
            </select>
          </label>
        </div>
        <ErrorBox
          error={query.error || groups.error}
          reload={() => void query.refetch()}
        />
        {query.isPending ? (
          <Loading />
        ) : (
          query.data && (
            <>
              {query.data.items.length ? (
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Utente</th>
                        <th>Stato / accesso</th>
                        <th>Gruppi</th>
                        <th>Ultimo accesso</th>
                        <th>
                          <span className="sr-only">Apri</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {query.data.items.map((u) => (
                        <tr key={u.id}>
                          <td>
                            <Link className="person-cell" to={`/users/${u.id}`}>
                              <Avatar name={u.full_name || u.username} />
                              <span>
                                <strong>{u.full_name || u.username}</strong>
                                <small>
                                  {u.username}
                                  {u.is_admin && " · Admin globale"}
                                </small>
                              </span>
                            </Link>
                          </td>
                          <td>
                            <div className="tags">
                              <Badge tone={u.is_active ? "green" : ""}>
                                {u.is_active ? "Attivo" : "Disabilitato"}
                              </Badge>
                              <Badge>
                                {u.auth_mode === "sso" ? "SSO" : "Locale"}
                              </Badge>
                            </div>
                          </td>
                          <td>
                            <GroupTags
                              ids={u.group_ids}
                              groups={groups.data || []}
                            />
                          </td>
                          <td className="nowrap muted">{date(u.last_login)}</td>
                          <td>
                            <Link
                              className="icon-button"
                              aria-label={`Apri ${u.username}`}
                              to={`/users/${u.id}`}
                            >
                              <ArrowUpRight size={18} />
                            </Link>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty />
              )}
              <Pager
                total={query.data.total}
                offset={offset}
                onChange={setOffset}
              />
            </>
          )
        )}
      </section>
      <p className="page-note">
        Gli amministratori di gruppo vedono solo gli account interamente
        compresi nel proprio ambito.
      </p>
    </>
  );
}

export function UserDetail() {
  const { id } = useParams();
  const create = id === "new";
  const query = useQuery({
    queryKey: ["user", id],
    queryFn: ({ signal }) => api<User>(`/admin/users/${id}`, { signal }),
    enabled: !create,
  });
  if (!create && query.isPending) return <Loading />;
  if (!create && query.error)
    return (
      <>
        <PageTitle
          title="Utente non disponibile"
          description="Verifica l’accesso all’account richiesto."
          back="/users"
        />
        <ErrorBox error={query.error} reload={() => void query.refetch()} />
      </>
    );
  return (
    <UserEditor
      key={id + ":" + (query.data?.revision || "new")}
      user={query.data}
      reload={() => query.refetch()}
    />
  );
}

function UserEditor({
  user,
  reload,
}: {
  user?: User;
  reload: () => Promise<unknown>;
}) {
  const auth = useAuth();
  const groups = useGroups();
  const client = useQueryClient();
  const navigate = useNavigate();
  const [username, setUsername] = useState(user?.username || "");
  const [name, setName] = useState(user?.full_name || "");
  const [email, setEmail] = useState(user?.email || "");
  const [mode, setMode] = useState<"local" | "sso">(user?.auth_mode || "local");
  const [password, setPassword] = useState("");
  const [active, setActive] = useState(user?.is_active ?? true);
  const [admin, setAdmin] = useState(user?.is_admin ?? false);
  const [permissions, setPermissions] = useState<string[]>(
    user?.permissions || (auth.can("connect") ? ["connect"] : []),
  );
  const [groupIds, setGroupIds] = useState<string[]>(user?.group_ids || []);
  const [pending, setPending] = useState<UserUpdate | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [saved, setSaved] = useState(false);
  const editable =
    !user ||
    auth.user!.is_admin ||
    (!user.is_admin && user.permissions.every((p) => auth.can(p)));
  async function refresh() {
    await client.invalidateQueries({ queryKey: ["users"] });
    await client.invalidateQueries({ queryKey: ["groups"] });
    await client.invalidateQueries({ queryKey: ["stats"] });
  }
  const save = useMutation({
    mutationFn: (body: UserInput | UserUpdate) =>
      api<User>(user ? `/admin/users/${user.id}` : "/admin/users", {
        method: user ? "PUT" : "POST",
        body,
        revision: user?.revision,
      }),
    onSuccess: async (result) => {
      setPassword("");
      setPending(null);
      setSaved(true);
      client.setQueryData(["user", result.id], result);
      navigate(`/users/${result.id}`, {
        replace: !user,
        state: { created: !user },
      });
      await refresh();
      if (user?.id === auth.user!.id)
        await client.invalidateQueries({ queryKey: ["session"] });
    },
  });
  const remove = useMutation({
    mutationFn: () =>
      api<void>(`/admin/users/${user!.id}`, {
        method: "DELETE",
        revision: user!.revision,
      }),
    onSuccess: async () => {
      client.removeQueries({ queryKey: ["user", user!.id] });
      navigate("/users", { replace: true });
      await refresh();
    },
  });
  const equal = (a: string[], b: string[]) =>
    [...a].sort().join(",") === [...b].sort().join(",");
  function submit(e: FormEvent) {
    e.preventDefault();
    setSaved(false);
    save.reset();
    if (!user) {
      save.mutate({
        username,
        full_name: name || null,
        email: email || null,
        auth_mode: mode,
        ...(mode === "local" ? { password } : {}),
        is_admin: admin,
        permissions: permissions as Permission[],
        group_ids: groupIds,
      });
      return;
    }
    const body: UserUpdate = {};
    if ((name || null) !== user.full_name) body.full_name = name || null;

    if (active !== user.is_active) body.is_active = active;
    if (admin !== user.is_admin) body.is_admin = admin;
    if (password) body.password = password;
    if (!equal(permissions, user.permissions))
      body.permissions = permissions as Permission[];
    if (!equal(groupIds, user.group_ids)) body.group_ids = groupIds;
    if (Object.keys(body).some((k) => !["email", "full_name"].includes(k)))
      setPending(body);
    else save.mutate(body);
  }
  function toggle(
    value: string,
    list: string[],
    setter: (values: string[]) => void,
  ) {
    setter(
      list.includes(value) ? list.filter((p) => p !== value) : [...list, value],
    );
  }
  return (
    <>
      <PageTitle
        title={user ? user.username : "Nuovo utente"}
        description={
          user
            ? "Informazioni, appartenenze e permessi dell’account."
            : "Crea un account e definisci il suo ambito di accesso."
        }
        back="/users"
        action={
          user && (
            <Badge tone={user.is_active ? "green" : ""}>
              {user.is_active ? "Account attivo" : "Account disabilitato"}
            </Badge>
          )
        }
      />
      {saved && <Notice>Utente salvato.</Notice>}
      {user && (
        <p className="muted">
          Email {user.email_verified ? "verificata" : "da verificare"} ·
          Verifica in due passaggi{" "}
          {user.email_two_step_required
            ? "obbligatoria"
            : user.email_two_step
              ? "attiva"
              : "facoltativa"}
          . L’utente può cambiare email dal proprio profilo.
        </p>
      )}
      {!editable && (
        <Notice>
          Puoi consultare questo account, ma i suoi permessi superano quelli che
          puoi amministrare.
        </Notice>
      )}
      <form onSubmit={submit}>
        <fieldset
          className="editor-fieldset"
          disabled={!editable || save.isPending}
        >
          <div className="editor-grid">
            <div className="stack">
              <section className="card padded">
                <h2>Informazioni dell’account</h2>
                <div className="form-grid">
                  <label>
                    Username
                    <input
                      required
                      minLength={3}
                      maxLength={50}
                      value={username}
                      disabled={!!user}
                      onChange={(e) => setUsername(e.target.value)}
                      autoComplete="off"
                    />
                  </label>
                  <label>
                    Nome completo
                    <input
                      maxLength={200}
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                      autoComplete="off"
                    />
                  </label>
                  <label>
                    Email
                    <input
                      type="email"
                      readOnly={!!user}
                      maxLength={255}
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      autoComplete="off"
                    />
                  </label>
                  {!user && auth.user!.is_admin && (
                    <label>
                      Metodo di accesso
                      <select
                        value={mode}
                        onChange={(e) => {
                          setMode(e.target.value as "local" | "sso");
                          setPassword("");
                        }}
                      >
                        <option value="local">Password locale</option>
                        <option value="sso">SSO aziendale</option>
                      </select>
                    </label>
                  )}
                  {mode === "local" && (
                    <label>
                      {user
                        ? "Nuova password (facoltativa)"
                        : "Password iniziale"}
                      <input
                        type="password"
                        required={!user}
                        minLength={8}
                        maxLength={128}
                        value={password}
                        onChange={(e) => setPassword(e.target.value)}
                        autoComplete="new-password"
                      />
                      <small>
                        {user
                          ? "Lascia vuoto per mantenere la password attuale."
                          : "Almeno 8 caratteri. Comunicala attraverso un canale sicuro."}
                      </small>
                    </label>
                  )}
                </div>
                {!user && mode === "sso" && (
                  <Notice>
                    Dopo la creazione, collega il provider e il subject
                    dell’identità aziendale. Fino ad allora l’account non potrà
                    accedere.
                  </Notice>
                )}
                {user && (
                  <label className="check-row">
                    <input
                      type="checkbox"
                      checked={active}
                      onChange={(e) => setActive(e.target.checked)}
                    />
                    <span>
                      <strong>Account attivo</strong>
                      <small>
                        Disabilitare l’account revoca anche il suo profilo VPN.
                        Dopo la riattivazione dovrà prepararne uno nuovo.
                      </small>
                    </span>
                  </label>
                )}
              </section>
              <section className="card padded">
                <h2>Gruppi assegnati</h2>
                <p className="muted">
                  I permessi si applicano a ciascuno dei gruppi selezionati.
                </p>
                <ErrorBox error={groups.error} />
                <div className="choices">
                  {groups.data?.map((g) => (
                    <label key={g.id} className="check-row">
                      <input
                        type="checkbox"
                        checked={groupIds.includes(g.id)}
                        onChange={() => toggle(g.id, groupIds, setGroupIds)}
                      />
                      <span>
                        <strong>{g.name}</strong>
                        <small>{g.description}</small>
                      </span>
                    </label>
                  ))}
                </div>
                {groups.data?.length === 0 && (
                  <p className="muted">
                    Nessun gruppo disponibile.{" "}
                    {auth.can("groups.manage") && (
                      <Link to="/groups/new">Crea un gruppo</Link>
                    )}
                  </p>
                )}
                {!auth.user!.is_admin && !groupIds.length && (
                  <p className="field-warning">
                    Seleziona almeno un gruppo per un account delegato.
                  </p>
                )}
              </section>
            </div>
            <aside className="card padded">
              <h2>Ruolo e permessi</h2>
              {auth.user!.is_admin && (
                <label className="check-row role-choice">
                  <input
                    type="checkbox"
                    checked={admin}
                    onChange={(e) => setAdmin(e.target.checked)}
                  />
                  <span>
                    <strong>Amministratore globale</strong>
                    <small>
                      Accesso completo, indipendente dai gruppi e dai permessi
                      selezionati.
                    </small>
                  </span>
                </label>
              )}
              {admin && (
                <Notice>
                  I permessi sotto si applicheranno se il ruolo globale verrà
                  rimosso.
                </Notice>
              )}
              <div className="choices">
                {Object.entries(permissionInfo).map(
                  ([p, [label, description]]) => (
                    <label key={p} className="check-row">
                      <input
                        type="checkbox"
                        checked={permissions.includes(p)}
                        disabled={!auth.can(p)}
                        onChange={() => toggle(p, permissions, setPermissions)}
                      />
                      <span>
                        <strong>{label}</strong>
                        <small>{description}</small>
                      </span>
                    </label>
                  ),
                )}
              </div>
            </aside>
          </div>
          <ErrorBox error={save.error} reload={() => void reload()} />
          <div className="form-actions">
            <Link className="button secondary" to="/users">
              Torna agli utenti
            </Link>
            <button
              className="button"
              disabled={
                groups.isPending ||
                !!groups.error ||
                (!auth.user!.is_admin && !groupIds.length) ||
                groupIds.length > 200
              }
            >
              {save.isPending
                ? "Salvataggio…"
                : user
                  ? "Salva modifiche"
                  : "Crea utente"}
            </button>
          </div>
        </fieldset>
      </form>
      {user && auth.user!.is_admin && (
        <VPNProfile kind="technician" peerId={user.id} />
      )}
      {user && auth.user!.is_admin && (
        <IdentityEditor user={user} refresh={reload} />
      )}
      {user && <EmailRecovery user={user} reload={reload} />}
      {user && editable && user.id !== auth.user!.id && (
        <section className="card padded danger-zone">
          <h2>Elimina account</h2>
          <p>
            L’account verrà eliminato. Le attività storiche rimarranno nel
            registro.
          </p>
          <button
            className="button danger-outline"
            onClick={() => {
              setDeleting(true);
              remove.reset();
            }}
          >
            <Trash2 size={16} /> Elimina utente
          </button>
        </section>
      )}
      {pending && (
        <Confirm
          title="Applicare le modifiche di accesso?"
          label="Salva e revoca gli accessi"
          onCancel={() => setPending(null)}
          onConfirm={() => save.mutate(pending)}
          busy={save.isPending}
          error={save.error}
        >
          <p>
            Le credenziali e le sessioni di accesso di{" "}
            <strong>{user!.username}</strong> saranno invalidate. Il backend
            chiuderà le VPN interessate dalla revoca.
          </p>
          {user!.id === auth.user!.id && (
            <p>Stai modificando il tuo account: dovrai accedere nuovamente.</p>
          )}
        </Confirm>
      )}
      {deleting && (
        <Confirm
          title={`Eliminare ${user!.username}?`}
          label="Elimina utente"
          danger
          onCancel={() => setDeleting(false)}
          onConfirm={() => remove.mutate()}
          busy={remove.isPending}
          error={remove.error}
        >
          <p>
            L’operazione è definitiva. Prima devono essere chiuse le sessioni
            VPN dell’account e completata la pulizia della rete.
          </p>
        </Confirm>
      )}
    </>
  );
}

function IdentityEditor({
  user,
  refresh,
}: {
  user: User;
  refresh: () => Promise<unknown>;
}) {
  const auth = useAuth();
  const client = useQueryClient();
  const providers = useProviders();
  const identities = useQuery({
    queryKey: ["identities", user.id],
    queryFn: ({ signal }) =>
      api<Identity[]>(`/admin/users/${user.id}/identities`, { signal }),
  });
  const [provider, setProvider] = useState("");
  const [subject, setSubject] = useState("");
  const [password, setPassword] = useState("");
  const [unlink, setUnlink] = useState<Identity | null>(null);
  const [switchMode, setSwitchMode] = useState(false);
  async function changed() {
    await refresh();
    await client.invalidateQueries({ queryKey: ["identities", user.id] });
    if (user.id === auth.user!.id)
      await client.invalidateQueries({ queryKey: ["session"] });
  }
  const link = useMutation({
    mutationFn: () =>
      api<Identity>(`/admin/users/${user.id}/identities`, {
        method: "POST",
        revision: user.revision,
        body: { provider_id: provider, subject },
      }),
    onSuccess: async () => {
      setSubject("");
      await changed();
    },
  });
  const remove = useMutation({
    mutationFn: () =>
      api<void>(`/admin/users/${user.id}/identities/${unlink!.id}`, {
        method: "DELETE",
        revision: user.revision,
      }),
    onSuccess: async () => {
      setUnlink(null);
      await changed();
    },
  });
  const mode = useMutation({
    mutationFn: () =>
      api<User>(`/admin/users/${user.id}/authentication`, {
        method: "PUT",
        revision: user.revision,
        body:
          user.auth_mode === "local"
            ? { auth_mode: "sso" }
            : { auth_mode: "local", password },
      }),
    onSuccess: async () => {
      setPassword("");
      setSwitchMode(false);
      await changed();
    },
  });
  return (
    <section className="card padded identity-section">
      <div className="card-heading inline">
        <div>
          <h2>
            <Link2 size={20} /> Identità e metodo di accesso
          </h2>
          <p className="muted">
            Solo amministratori globali. Il collegamento usa provider e subject,
            senza associazioni automatiche per email.
          </p>
        </div>
        <Badge tone="blue">
          {user.auth_mode === "sso" ? "SSO" : "Password locale"}
        </Badge>
      </div>
      <ErrorBox error={identities.error || providers.error} />
      {identities.isPending ? (
        <Loading />
      ) : identities.data?.length ? (
        <div className="identity-list">
          {identities.data.map((i) => (
            <div key={i.id}>
              <span>
                <strong>
                  {providers.data?.find((p) => p.id === i.provider_id)?.label ||
                    i.provider_id}
                </strong>
                <code>{i.subject}</code>
                <small>{i.issuer}</small>
              </span>
              <button
                className="button secondary"
                onClick={() => {
                  remove.reset();
                  setUnlink(i);
                }}
              >
                Scollega
              </button>
            </div>
          ))}
        </div>
      ) : (
        <Notice>
          Nessuna identità collegata.{" "}
          {user.auth_mode === "sso" &&
            "Questo account SSO non può ancora accedere."}
        </Notice>
      )}
      {!!providers.data?.length && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            link.mutate();
          }}
        >
          <div className="form-grid">
            <label>
              Provider SSO
              <select
                required
                value={provider}
                onChange={(e) => setProvider(e.target.value)}
              >
                <option value="">Seleziona un provider</option>
                {providers.data.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.label} · WIP
                  </option>
                ))}
              </select>
            </label>
            <label>
              Subject dell’identità
              <input
                required
                maxLength={255}
                value={subject}
                onChange={(e) => setSubject(e.target.value)}
                autoComplete="off"
              />
              <small>
                Identificatore stabile «sub» del provider, non l’email.
              </small>
            </label>
          </div>
          <ErrorBox error={link.error} reload={() => void refresh()} />
          <button className="button secondary" disabled={link.isPending}>
            Collega identità
          </button>
        </form>
      )}
      <form
        className="mode-switch"
        onSubmit={(e) => {
          e.preventDefault();
          mode.reset();
          setSwitchMode(true);
        }}
      >
        <h3>Cambia metodo di accesso</h3>
        <p className="muted">
          La conversione revoca gli accessi e le VPN dell’account.
        </p>
        {user.auth_mode === "sso" && (
          <label>
            Nuova password locale
            <input
              type="password"
              required
              minLength={8}
              maxLength={128}
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
        )}
        <button
          className="button secondary"
          disabled={user.auth_mode === "local" && !identities.data?.length}
        >
          Passa a {user.auth_mode === "local" ? "SSO" : "password locale"}
        </button>
      </form>
      {unlink && (
        <Confirm
          title="Scollegare questa identità?"
          danger
          label="Scollega e revoca"
          onCancel={() => setUnlink(null)}
          onConfirm={() => remove.mutate()}
          busy={remove.isPending}
          error={remove.error}
        >
          <p>
            L’identità <strong>{unlink.subject}</strong> non potrà più accedere
            a questo account. Verranno revocati gli accessi e le VPN. Un account
            SSO senza altre identità rimarrà senza un metodo di accesso.
          </p>
        </Confirm>
      )}
      {switchMode && (
        <Confirm
          title="Cambiare il metodo di accesso?"
          label="Cambia e revoca"
          onCancel={() => {
            setSwitchMode(false);
            setPassword("");
          }}
          onConfirm={() => mode.mutate()}
          busy={mode.isPending}
          error={mode.error}
        >
          <p>
            Verranno revocati tutti gli accessi e le VPN di{" "}
            <strong>{user.username}</strong>.{" "}
            {user.auth_mode === "local"
              ? "La password locale verrà rimossa."
              : "L’accesso SSO verrà disabilitato."}
          </p>
        </Confirm>
      )}
    </section>
  );
}
