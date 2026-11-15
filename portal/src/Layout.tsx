import { useEffect, useState, useRef, type ReactNode } from "react";
import { Link, NavLink, Navigate, Outlet, useNavigate } from "react-router";
import {
  LayoutDashboard,
  Users,
  Layers,
  ShieldCheck,
  Router,
  History,
  UserRound,
  LogOut,
  Menu,
  X,
  Clock3,
  ArrowUpRight,
  Radio,
  Activity,
  DatabaseBackup,
  Server,
  ClipboardCheck,
} from "lucide-react";
import { useMutation } from "@tanstack/react-query";
import { api } from "./api/client";
import { useAuth } from "./auth";
import { Avatar, Badge, Confirm, ErrorBox, Loading, PageTitle } from "./ui";

export function Allowed({
  permission,
  children,
}: {
  permission: string;
  children: ReactNode;
}) {
  const auth = useAuth();
  return auth.can(permission) ? (
    children
  ) : (
    <>
      <PageTitle
        title="Accesso non disponibile"
        description="Questa sezione richiede un permesso aggiuntivo."
      />
      <Link to="/" className="button secondary">
        Torna alla panoramica
      </Link>
    </>
  );
}

export function GlobalAdmin({ children }: { children: ReactNode }) {
  const auth = useAuth();
  return auth.user?.is_admin ? (
    children
  ) : (
    <>
      <PageTitle
        title="Accesso non disponibile"
        description="Questa sezione è riservata agli amministratori globali."
      />
      <Link to="/" className="button secondary">
        Torna alla panoramica
      </Link>
    </>
  );
}

export default function Layout() {
  const auth = useAuth();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [mobile, setMobile] = useState(
    () => window.matchMedia("(max-width: 650px)").matches,
  );
  const menuButton = useRef<HTMLButtonElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 650px)");
    const update = () => {
      setMobile(media.matches);
      setOpen(false);
    };
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    if (!mobile || !open) return;
    closeButton.current?.focus();
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !document.querySelector("dialog[open]")) {
        setOpen(false);
        menuButton.current?.focus();
      }
    };
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("keydown", escape);
      menuButton.current?.focus();
    };
  }, [mobile, open]);
  const [logout, setLogout] = useState<"current" | "all" | null>(null);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 15000);
    return () => clearInterval(timer);
  }, []);
  const signOut = useMutation({
    mutationFn: () =>
      api<{ provider_logout_urls: string[] }>(
        logout === "all" ? "/web/logout-all" : "/web/logout",
        { method: "POST" },
      ),
    onSuccess: async (result) => {
      await auth.clear();
      setLogout(null);
      navigate("/login", {
        replace: true,
        state: { signedOut: true, logoutUrls: result.provider_logout_urls },
      });
      if (
        result.provider_logout_urls.length === 1 &&
        result.provider_logout_urls[0].startsWith("https://")
      )
        window.location.assign(result.provider_logout_urls[0]);
    },
  });
  if (auth.loading) return <Loading />;
  if (!auth.user) return <Navigate to="/login" replace />;
  const user = auth.user;
  const minutes = Math.max(
    0,
    Math.ceil((new Date(auth.session!.expires_at).getTime() - now) / 60000),
  );
  const links = [
    { to: "/", title: "Panoramica", icon: LayoutDashboard },
    ...(user.is_admin
      ? [
          { to: "/server", title: "Stato server", icon: Server },
          {
            to: "/backups",
            title: "Backup e ripristino",
            icon: DatabaseBackup,
          },
        ]
      : []),
    { to: "/vpn", title: "Le mie VPN", icon: Radio },
    { to: "/vpn-history", title: "Cronologia VPN", icon: History },
    ...(auth.can("telemetry.read")
      ? [{ to: "/telemetry", title: "Telemetria", icon: Activity }]
      : []),
    ...(auth.can("sessions.manage")
      ? [{ to: "/clients", title: "Client VPN", icon: Activity }]
      : []),
    ...(auth.can("users.manage")
      ? [{ to: "/users", title: "Utenti", icon: Users }]
      : []),
    { to: "/groups", title: "Gruppi", icon: Layers },
    { to: "/permissions", title: "Permessi", icon: ShieldCheck },
    ...(auth.can("gateways.manage")
      ? [{ to: "/gateways", title: "Gateway", icon: Router }]
      : []),
    ...(user.is_admin
      ? [{ to: "/enrollments", title: "Arruolamenti", icon: ClipboardCheck }]
      : []),
    ...(auth.can("audit.read")
      ? [{ to: "/audit", title: "Registro attività", icon: History }]
      : []),
  ];
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main">
        Vai al contenuto
      </a>
      <aside
        className={`sidebar ${open ? "open" : ""}`}
        aria-label="Navigazione principale"
        inert={mobile && !open}
        aria-hidden={mobile && !open}
      >
        <Link to="/" className="brand" onClick={() => setOpen(false)}>
          <span className="brand-mark">F</span>Factory<span>VPN</span>
        </Link>
        <button
          className="mobile-close icon-button"
          ref={closeButton}
          aria-label="Chiudi menu"
          onClick={() => setOpen(false)}
        >
          <X size={20} />
        </button>
        <p className="nav-caption">WORKSPACE</p>
        <nav>
          {links.map(({ to, title, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              onClick={() => setOpen(false)}
            >
              <Icon size={18} />
              <span>{title}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="scope-label">
            <ShieldCheck size={15} />
            <span>
              {user.is_admin
                ? "Amministrazione globale"
                : `${user.group_ids.length} gruppi assegnati`}
            </span>
          </div>
          <NavLink
            to="/profile"
            onClick={() => setOpen(false)}
            className="sidebar-profile"
          >
            <Avatar name={user.full_name || user.username} />
            <span>
              <strong>{user.full_name || user.username}</strong>
              <small>{user.username}</small>
            </span>
            <ArrowUpRight size={15} />
          </NavLink>
          <button
            className="signout"
            onClick={() => {
              setLogout("current");
              signOut.reset();
            }}
          >
            <LogOut size={16} /> Esci dal portale
          </button>
        </div>
      </aside>
      {open && (
        <button
          className="nav-backdrop"
          aria-label="Chiudi navigazione"
          onClick={() => setOpen(false)}
        />
      )}
      <div className="app-body" inert={mobile && open}>
        <header className="topbar">
          <div>
            <button
              className="mobile-menu icon-button"
              ref={menuButton}
              aria-label="Apri menu"
              aria-expanded={open}
              onClick={() => setOpen(true)}
            >
              <Menu size={22} />
            </button>
            <span className="topbar-label">
              Portale <span>/</span> Accessi e VPN
            </span>
          </div>
          <div className="topbar-actions">
            <span
              className={`session-time ${minutes <= 3 ? "expiring" : ""}`}
              title="Scadenza della sessione web"
            >
              <Clock3 size={14} /> {minutes ? `${minutes} min` : "In scadenza"}
            </span>
            <Badge tone={user.auth_mode === "sso" ? "blue" : ""}>
              {user.auth_mode === "sso" ? "SSO" : "Accesso locale"}
            </Badge>
            <Link
              className="icon-button"
              to="/profile"
              aria-label="Il mio profilo"
            >
              <UserRound size={18} />
            </Link>
          </div>
        </header>
        <main id="main" className="content">
          <ErrorBox error={auth.error} />
          {minutes <= 3 && (
            <div className="notice amber">
              La sessione sta per scadere. Salva le modifiche; al termine sarà
              necessario accedere nuovamente. Termineranno anche le VPN aperte
              da questo accesso.
            </div>
          )}
          <Outlet
            context={{
              logoutAll: () => {
                setLogout("all");
                signOut.reset();
              },
            }}
          />
        </main>
        <footer className="app-footer">
          FactoryVPN <span>Accessi definiti. Risorse separate.</span>
        </footer>
      </div>
      {logout && (
        <Confirm
          title={
            logout === "all"
              ? "Disconnettere tutti gli accessi?"
              : "Uscire dal portale?"
          }
          danger={logout === "all"}
          label={logout === "all" ? "Disconnetti tutto" : "Esci dal portale"}
          onCancel={() => setLogout(null)}
          onConfirm={() => signOut.mutate()}
          busy={signOut.isPending}
          error={signOut.error}
        >
          <p>
            {logout === "all"
              ? "Verranno revocati tutti gli accessi dell’account, comprese le altre sessioni del portale, i token CLI e le VPN attive."
              : "Verranno chiusi questo accesso web, le VPN aperte da questo accesso e la relativa sessione SSO, se presente. Gli altri accessi dell’account rimarranno validi."}
          </p>
        </Confirm>
      )}
    </div>
  );
}
