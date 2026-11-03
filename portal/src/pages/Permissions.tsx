import { useQuery } from "@tanstack/react-query";
import { ShieldCheck, Check } from "lucide-react";
import { api } from "../api/client";
import { useAuth } from "../auth";
import { Badge, ErrorBox, Loading, PageTitle, permissionInfo } from "../ui";
export default function Permissions() {
  const auth = useAuth();
  const query = useQuery({
    queryKey: ["permissions"],
    queryFn: ({ signal }) => api<string[]>("/admin/permissions", { signal }),
  });
  return (
    <>
      <PageTitle
        title="Permessi"
        description="Le capacità disponibili e quelle abilitate per il tuo account."
      />
      <section className="card padded scope-explanation">
        <ShieldCheck size={26} />
        <div>
          <h2>
            {auth.user!.is_admin
              ? "Il tuo accesso è globale"
              : "Il tuo accesso è limitato ai gruppi assegnati"}
          </h2>
          <p>
            Gli amministratori delegati possono assegnare solo permessi che
            possiedono e gruppi nel proprio ambito. Un account che appartiene
            anche ad altri gruppi non è amministrabile da loro.
          </p>
          <p>
            Identità SSO, conversione del metodo di accesso e ruolo di
            amministratore globale sono riservati agli amministratori globali.
          </p>
        </div>
      </section>
      <ErrorBox error={query.error} />
      {query.isPending ? (
        <Loading />
      ) : (
        <div className="permission-grid">
          {query.data?.map((p) => (
            <section key={p} className="card padded">
              <div className="permission-title">
                <h2>{permissionInfo[p]?.[0] || p}</h2>
                <Badge tone={auth.can(p) ? "green" : ""}>
                  {auth.can(p) ? (
                    <>
                      <Check size={13} /> Abilitato
                    </>
                  ) : (
                    "Non assegnato"
                  )}
                </Badge>
              </div>
              <code>{p}</code>
              <p className="muted">{permissionInfo[p]?.[1]}</p>
            </section>
          ))}
        </div>
      )}
      <p className="page-note">
        Il portale gestisce accessi, sessioni VPN, diagnostica e telemetria
        secondo i permessi assegnati. Il client OpenVPN viene avviato
        manualmente.
      </p>
    </>
  );
}
