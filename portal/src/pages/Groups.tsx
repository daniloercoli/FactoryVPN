import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router";
import { Plus, Layers, ArrowRight, Trash2 } from "lucide-react";
import { api, page, type Group } from "../api/client";
import { useAuth } from "../auth";
import { useGroups } from "../hooks";
import {
  Badge,
  Confirm,
  Empty,
  ErrorBox,
  Loading,
  PageTitle,
  Pager,
} from "../ui";

export default function Groups() {
  const auth = useAuth();
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const query = useQuery({
    queryKey: ["group-list", q, offset],
    queryFn: ({ signal }) =>
      page<Group>("/groups/", { q, limit: 20, offset }, signal),
  });
  return (
    <>
      <PageTitle
        title="Gruppi"
        description="Definisci quali persone possono accedere a quali risorse."
        action={
          auth.can("groups.manage") && (
            <Link to="/groups/new" className="button">
              <Plus size={17} /> Nuovo gruppo
            </Link>
          )
        }
      />
      <section className="card">
        <div className="filters">
          <label className="search-field">
            Cerca gruppi
            <input
              type="search"
              maxLength={100}
              placeholder="Nome del gruppo"
              value={q}
              onChange={(e) => {
                setQ(e.target.value);
                setOffset(0);
              }}
            />
          </label>
          <Badge tone="blue">
            {auth.user!.is_admin ? "Tutti i gruppi" : "Gruppi assegnati"}
          </Badge>
        </div>
        <ErrorBox error={query.error} reload={() => void query.refetch()} />
        {query.isPending ? (
          <Loading />
        ) : (
          query.data && (
            <>
              {query.data.items.length ? (
                <div className="group-list">
                  {query.data.items.map((g) => (
                    <div key={g.id} className="group-row">
                      <span className="group-icon">
                        <Layers size={23} />
                      </span>
                      <div>
                        <h2>{g.name}</h2>
                        <p>{g.description || "Nessuna descrizione"}</p>
                        <div className="row-links">
                          {auth.can("users.manage") && (
                            <Link to={`/users?group=${g.id}`}>Utenti</Link>
                          )}
                          {auth.can("gateways.manage") && (
                            <Link to={`/gateways?group=${g.id}`}>Gateway</Link>
                          )}
                        </div>
                      </div>
                      {auth.can("groups.manage") && (
                        <Link
                          to={`/groups/${g.id}`}
                          className="button secondary"
                        >
                          Gestisci <ArrowRight size={16} />
                        </Link>
                      )}
                    </div>
                  ))}
                </div>
              ) : (
                <Empty title="Nessun gruppo disponibile">
                  {auth.can("groups.manage")
                    ? "Crea un gruppo per iniziare a organizzare gli accessi."
                    : "Contatta l’amministratore per assegnare gruppi al tuo account."}
                </Empty>
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
        Un utente può appartenere a più gruppi. Ogni gateway è assegnato a un
        solo gruppo.
      </p>
    </>
  );
}
export function GroupDetail() {
  const { id } = useParams();
  const create = id === "new";
  const query = useQuery({
    queryKey: ["group", id],
    queryFn: ({ signal }) => api<Group>(`/admin/groups/${id}`, { signal }),
    enabled: !create,
  });
  if (!create && query.isPending) return <Loading />;
  if (!create && query.error)
    return (
      <>
        <PageTitle
          title="Gruppo non disponibile"
          description="Verifica il tuo ambito di accesso."
          back="/groups"
        />
        <ErrorBox error={query.error} reload={() => void query.refetch()} />
      </>
    );
  return (
    <GroupEditor
      key={id + ":" + (query.data?.revision || "new")}
      group={query.data}
      reload={() => void query.refetch()}
    />
  );
}
function GroupEditor({ group, reload }: { group?: Group; reload: () => void }) {
  const auth = useAuth();
  const client = useQueryClient();
  const navigate = useNavigate();
  const [name, setName] = useState(group?.name || "");
  const [description, setDescription] = useState(group?.description || "");
  const [deleting, setDeleting] = useState(false);
  async function refresh() {
    await Promise.all(
      ["groups", "group-list", "users", "stats", "session"].map((key) =>
        client.invalidateQueries({ queryKey: [key] }),
      ),
    );
  }
  const save = useMutation({
    mutationFn: () =>
      api<Group>(group ? `/admin/groups/${group.id}` : "/admin/groups", {
        method: group ? "PUT" : "POST",
        revision: group?.revision,
        body: { name, description: description || null },
      }),
    onSuccess: async (result) => {
      client.setQueryData(["group", result.id], result);
      navigate("/groups");
      await refresh();
    },
  });
  const remove = useMutation({
    mutationFn: () =>
      api<void>(`/admin/groups/${group!.id}`, {
        method: "DELETE",
        revision: group!.revision,
      }),
    onSuccess: async () => {
      client.removeQueries({ queryKey: ["group", group!.id] });
      navigate("/groups", { replace: true });
      await refresh();
    },
  });
  return (
    <>
      <PageTitle
        title={group ? group.name : "Nuovo gruppo"}
        description="Organizza persone e gateway in un ambito condiviso."
        back="/groups"
      />
      <div className="editor-grid">
        <section className="card padded">
          <h2>Informazioni del gruppo</h2>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              save.mutate();
            }}
          >
            <label>
              Nome del gruppo
              <input
                required
                maxLength={100}
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </label>
            <label>
              Descrizione
              <textarea
                rows={4}
                maxLength={500}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </label>
            <ErrorBox error={save.error} reload={reload} />
            <div className="form-actions">
              <Link className="button secondary" to="/groups">
                Annulla
              </Link>
              <button
                className="button"
                disabled={save.isPending || !name.trim()}
              >
                {group ? "Salva gruppo" : "Crea gruppo"}
              </button>
            </div>
          </form>
        </section>
        <aside className="card padded summary-card">
          <Layers size={25} />
          <h2>Persone e risorse</h2>
          <p className="muted">
            Le appartenenze si gestiscono dalla scheda di ciascun utente; i
            gateway dalla pagina delle assegnazioni.
          </p>
          {group && (
            <div className="stack">
              {auth.can("users.manage") && (
                <Link
                  className="button secondary"
                  to={`/users?group=${group.id}`}
                >
                  Vedi utenti del gruppo <ArrowRight size={15} />
                </Link>
              )}
              {auth.can("gateways.manage") && (
                <Link
                  className="button secondary"
                  to={`/gateways?group=${group.id}`}
                >
                  Vedi gateway del gruppo <ArrowRight size={15} />
                </Link>
              )}
            </div>
          )}
          {!group && !auth.user!.is_admin && (
            <p>Verrai aggiunto automaticamente al gruppo che crei.</p>
          )}
        </aside>
      </div>
      {group && (
        <section className="card padded danger-zone">
          <h2>Elimina gruppo</h2>
          <p>
            Sposta prima tutti i gateway e attendi la chiusura delle VPN. Per
            eliminare un gruppo con membri serve anche il permesso di gestione
            utenti e l’autorizzazione a modificare tutti gli account coinvolti.
          </p>
          <button
            className="button danger-outline"
            onClick={() => {
              remove.reset();
              setDeleting(true);
            }}
          >
            <Trash2 size={16} /> Elimina gruppo
          </button>
          <ErrorBox error={remove.error} reload={reload} />
        </section>
      )}
      {deleting && (
        <Confirm
          title={`Eliminare ${group!.name}?`}
          label="Elimina gruppo"
          danger
          onCancel={() => setDeleting(false)}
          onConfirm={() => remove.mutate()}
          busy={remove.isPending}
          error={remove.error}
        >
          <p>
            Il gruppo e le sue appartenenze verranno rimossi definitivamente.
            Gli account resteranno presenti.
          </p>
        </Confirm>
      )}
    </>
  );
}
