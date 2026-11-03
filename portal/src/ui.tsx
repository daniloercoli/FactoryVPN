import { useEffect, useId, useRef, type ReactNode } from "react";
import {
  AlertCircle,
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  LoaderCircle,
  X,
  Inbox,
} from "lucide-react";
import { Link } from "react-router";
import { errorMessage, type Group } from "./api/client";

export const permissionInfo: Record<string, [string, string]> = {
  "logs.read": [
    "Lettura log tecnici",
    "Consulta ed esporta i log dei gateway nei tuoi gruppi; le sessioni altrui richiedono anche la gestione sessioni.",
  ],
  "telemetry.read": [
    "Lettura telemetria",
    "Consulta risorse e servizi dei gateway nei propri gruppi. Il server è riservato agli amministratori globali.",
  ],
  connect: [
    "Connessione VPN",
    "Avvia sessioni di assistenza nei gruppi assegnati.",
  ],
  "users.manage": [
    "Gestione utenti",
    "Amministra gli account interamente nel proprio ambito.",
  ],
  "groups.manage": ["Gestione gruppi", "Crea e modifica gruppi di risorse."],
  "gateways.manage": [
    "Gestione gateway",
    "Assegna i gateway e gestisce l’inventario.",
  ],
  "gateways.provision": [
    "Provisioning gateway",
    "Prepara e revoca credenziali dei gateway nei gruppi assegnati.",
  ],
  "sessions.manage": [
    "Gestione sessioni",
    "Consulta e chiude anche le sessioni degli altri tecnici.",
  ],
  "audit.read": [
    "Lettura audit",
    "Consulta gli eventi e le statistiche del proprio ambito.",
  ],
};
export function date(value?: string | null) {
  return value
    ? new Intl.DateTimeFormat("it-IT", {
        dateStyle: "short",
        timeStyle: "short",
      }).format(new Date(value))
    : "Mai";
}
export function Badge({
  children,
  tone = "",
}: {
  children: ReactNode;
  tone?: string;
}) {
  return <span className={`badge ${tone}`}>{children}</span>;
}
export function Avatar({ name }: { name: string }) {
  return (
    <span className="avatar" aria-hidden="true">
      {name.slice(0, 2).toUpperCase()}
    </span>
  );
}
export function Loading() {
  return (
    <div className="loading" role="status">
      <LoaderCircle className="spin" size={22} /> Caricamento…
    </div>
  );
}
export function ErrorBox({
  error,
  reload,
}: {
  error: unknown;
  reload?: () => void;
}) {
  if (!error) return null;
  return (
    <div className="notice error" role="alert">
      <AlertCircle size={18} />
      <div>
        {errorMessage(error)}
        {reload && (
          <button type="button" className="text-button" onClick={reload}>
            Ricarica i dati
          </button>
        )}
      </div>
    </div>
  );
}
export function Notice({ children }: { children: ReactNode }) {
  return (
    <div className="notice" role="status">
      {children}
    </div>
  );
}
export function Empty({
  title = "Nessun risultato",
  children,
}: {
  title?: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      <Inbox size={30} />
      <h3>{title}</h3>
      <p>{children || "Prova a cambiare i filtri di ricerca."}</p>
    </div>
  );
}
export function PageTitle({
  title,
  description,
  action,
  back,
}: {
  title: string;
  description: string;
  action?: ReactNode;
  back?: string;
}) {
  return (
    <header className="page-title">
      <div>
        {back && (
          <Link className="back-link" to={back}>
            <ArrowLeft size={15} /> Torna all’elenco
          </Link>
        )}
        <p className="eyebrow">CONTROLLO ACCESSI</p>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action}
    </header>
  );
}
export function Pager({
  total,
  offset,
  size = 20,
  onChange,
}: {
  total: number;
  offset: number;
  size?: number;
  onChange: (offset: number) => void;
}) {
  return (
    <div className="pager">
      <span>
        {total === 0
          ? "0 risultati"
          : `${offset + 1}–${Math.min(offset + size, total)} di ${total}`}
      </span>
      <div>
        <button
          className="icon-button"
          type="button"
          aria-label="Pagina precedente"
          disabled={offset === 0}
          onClick={() => onChange(Math.max(0, offset - size))}
        >
          <ChevronLeft size={18} />
        </button>
        <button
          className="icon-button"
          type="button"
          aria-label="Pagina successiva"
          disabled={offset + size >= total}
          onClick={() => onChange(offset + size)}
        >
          <ChevronRight size={18} />
        </button>
      </div>
    </div>
  );
}
export function GroupTags({ ids, groups }: { ids: string[]; groups: Group[] }) {
  if (!ids.length) return <span className="muted">Nessun gruppo</span>;
  return (
    <div className="tags">
      {ids.map((id) => (
        <Badge key={id}>
          {groups.find((g) => g.id === id)?.name || "Gruppo non disponibile"}
        </Badge>
      ))}
    </div>
  );
}
export function Confirm({
  title,
  children,
  onCancel,
  onConfirm,
  busy = false,
  error,
  label = "Conferma",
  danger = false,
}: {
  title: string;
  children: ReactNode;
  onCancel: () => void;
  onConfirm: () => void;
  busy?: boolean;
  error?: unknown;
  label?: string;
  danger?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const id = useId();
  useEffect(() => {
    const dialog = ref.current;
    dialog?.showModal();
    return () => dialog?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      className="dialog"
      aria-labelledby={id}
      onCancel={(e) => {
        e.preventDefault();
        if (!busy) onCancel();
      }}
    >
      <div className="dialog-head">
        <h2 id={id}>{title}</h2>
        <button
          className="icon-button"
          type="button"
          aria-label="Annulla"
          onClick={onCancel}
          disabled={busy}
        >
          <X size={18} />
        </button>
      </div>
      <div className="dialog-content">
        {children}
        <ErrorBox error={error} />
      </div>
      <div className="dialog-actions">
        <button
          type="button"
          className="button secondary"
          disabled={busy}
          onClick={onCancel}
        >
          Annulla
        </button>
        <button
          type="button"
          className={`button ${danger ? "danger" : ""}`}
          disabled={busy}
          onClick={onConfirm}
        >
          {busy ? "Operazione in corso…" : label}
        </button>
      </div>
    </dialog>
  );
}
