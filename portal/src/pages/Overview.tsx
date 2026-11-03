import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import {
  Users,
  Layers,
  Router,
  ArrowRight,
  ShieldCheck,
  KeyRound,
  Radio,
} from "lucide-react";
import { useAuth } from "../auth";
import { api, page, type Stats, type VPNClient } from "../api/client";
import { useGroups } from "../hooks";
import { Badge, ErrorBox, GroupTags, PageTitle, permissionInfo } from "../ui";

function Operations() {
  const query = useQuery({
    queryKey: ["vpn", "operations"],
    queryFn: async ({ signal }) => {
      const counts = await Promise.all(
        [
          { status_filter: "REQUESTED" },
          { status_filter: "ACTIVE" },
          { cleanup_pending: true },
        ].map((filter) =>
          page<VPNClient>("/admin/sessions", { ...filter, limit: 1 }, signal),
        ),
      );
      return counts.map((result) => result.total);
    },
    refetchInterval: 10000,
    refetchOnWindowFocus: true,
  });
  const links = [
    [
      "Richieste in attesa",
      "/clients?status=REQUESTED",
      "In attesa di attivazione o collegamento",
    ],
    [
      "Sessioni attive",
      "/clients?status=ACTIVE",
      "Verifica i collegamenti nella supervisione",
    ],
    [
      "Risorse in pulizia",
      "/clients?view=cleanup",
      "Prenotate fino alla conferma del rilascio",
    ],
  ];
  return (
    <section aria-label="Operazioni VPN">
      <ErrorBox error={query.error} reload={() => void query.refetch()} />
      <div className="metrics">
        {links.map(([label, path, note], index) => (
          <Link className="metric" to={path} key={path}>
            <p>{label}</p>
            <strong>{!query.error ? (query.data?.[index] ?? "—") : "—"}</strong>
            <small>{note}</small>
          </Link>
        ))}
      </div>
    </section>
  );
}

export default function Overview() {
  const auth = useAuth();
  const groups = useGroups();
  const stats = useQuery({
    queryKey: ["stats"],
    queryFn: ({ signal }) => api<Stats>("/admin/stats", { signal }),
    enabled: auth.can("audit.read"),
    refetchInterval: 10000,
    refetchOnWindowFocus: true,
  });
  return (
    <>
      <PageTitle
        title="Accessi sotto controllo"
        description={`Benvenuto, ${auth.user!.full_name || auth.user!.username}. Da qui puoi seguire l’assistenza e gestire le risorse a cui hai accesso.`}
        action={
          <Badge tone="blue">
            {auth.user!.is_admin ? "Ambito globale" : "Ambito per gruppi"}
          </Badge>
        }
      />
      <ErrorBox error={groups.error || stats.error} />
      <div className="metrics">
        <div className="metric">
          <span className="metric-icon">
            <Layers size={21} />
          </span>
          <p>Gruppi visibili</p>
          <strong>{groups.data?.length ?? "—"}</strong>
          <small>Il tuo ambito di risorse</small>
        </div>
        {auth.can("audit.read") && (
          <>
            <div className="metric">
              <span className="metric-icon">
                <Users size={21} />
              </span>
              <p>Account abilitati</p>
              <strong>
                {!stats.error ? (stats.data?.users_active ?? "—") : "—"}
                <span>
                  {" "}
                  / {!stats.error ? (stats.data?.users_total ?? "—") : "—"}
                </span>
              </strong>
              <small>Abilitati al login, non necessariamente connessi</small>
            </div>
            <div className="metric">
              <span className="metric-icon">
                <Router size={21} />
              </span>
              <p>Gateway registrati</p>
              <strong>
                {!stats.error ? (stats.data?.gateways_total ?? "—") : "—"}
              </strong>
              <small>Risorse visibili all’account</small>
            </div>
          </>
        )}
      </div>
      {auth.can("sessions.manage") && <Operations />}
      <div className="overview-grid">
        <section className="card">
          <div className="card-heading">
            <div>
              <p className="eyebrow">OPERAZIONI</p>
              <h2>Da dove vuoi iniziare?</h2>
            </div>
          </div>
          <div className="quick-links">
            {auth.can("sessions.manage") && (
              <Link to="/clients">
                <Radio size={22} />
                <span>
                  <strong>Supervisiona i client VPN</strong>
                  <small>
                    Collegamenti, diagnostica e chiusura delle sessioni
                  </small>
                </span>
                <ArrowRight size={19} />
              </Link>
            )}
            {auth.can("gateways.manage") && (
              <Link to="/gateways">
                <Router size={22} />
                <span>
                  <strong>Gestisci i gateway</strong>
                  <small>Impianti, inventario e credenziali</small>
                </span>
                <ArrowRight size={19} />
              </Link>
            )}
            <Link to="/vpn">
              <Radio size={22} />
              <span>
                <strong>Le mie VPN</strong>
                <small>Gateway, assistenza e stato delle connessioni</small>
              </span>
              <ArrowRight size={19} />
            </Link>
            {auth.can("users.manage") && (
              <Link to="/users">
                <Users size={22} />
                <span>
                  <strong>Gestisci gli utenti</strong>
                  <small>Account, gruppi e autorizzazioni</small>
                </span>
                <ArrowRight size={19} />
              </Link>
            )}
            <Link to="/groups">
              <Layers size={22} />
              <span>
                <strong>Esplora i gruppi</strong>
                <small>Organizza l’accesso alle risorse</small>
              </span>
              <ArrowRight size={19} />
            </Link>
            <Link to="/profile">
              <KeyRound size={22} />
              <span>
                <strong>Il tuo account</strong>
                <small>Profilo, credenziali e sessioni</small>
              </span>
              <ArrowRight size={19} />
            </Link>
          </div>
        </section>
        <section className="card access-card">
          <ShieldCheck size={27} />
          <h2>Il tuo ambito di accesso</h2>
          <p>
            {auth.user!.is_admin
              ? "Puoi amministrare l’intera istanza FactoryVPN, inclusi identità SSO e amministratori."
              : "I tuoi permessi si applicano a tutti e soli i gruppi assegnati."}
          </p>
          {!auth.user!.is_admin && (
            <GroupTags ids={auth.user!.group_ids} groups={groups.data || []} />
          )}
          <div className="tags">
            {auth.user!.permissions.map((p) => (
              <Badge key={p}>{permissionInfo[p]?.[0] || p}</Badge>
            ))}
          </div>
          <Link to="/permissions" className="text-link">
            Comprendi i permessi <ArrowRight size={15} />
          </Link>
        </section>
      </div>
    </>
  );
}
