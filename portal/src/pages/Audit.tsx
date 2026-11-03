import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { page, downloadCsv, type Audit as AuditEntry } from "../api/client";
import { useGroups } from "../hooks";
import { useSearchParams } from "react-router";
import {
  HistoryFilters,
  historyParams,
  emptyHistoryFilters,
} from "./Diagnostics";
import { Badge, Empty, ErrorBox, Loading, PageTitle, Pager, date } from "../ui";
export default function Audit() {
  const groups = useGroups();
  const [params] = useSearchParams();
  const [draft, setDraft] = useState({
    ...emptyHistoryFilters,
    session_id: params.get("session_id") || "",
    gateway_id: params.get("gateway_id") || "",
  });
  const [filter, setFilter] = useState(draft);
  const [offset, setOffset] = useState(0);
  const qs = historyParams(filter);
  const query = useQuery({
    queryKey: ["audit", qs.toString(), offset],
    queryFn: ({ signal }) =>
      page<AuditEntry>(
        "/admin/audit",
        { ...Object.fromEntries(qs), limit: 20, offset },
        signal,
      ),
  });
  const download = useMutation({
    mutationFn: () =>
      downloadCsv(`/admin/audit.csv?${qs}`, "factoryvpn-audit.csv"),
  });
  return (
    <>
      <PageTitle
        title="Registro attività"
        description="Traccia le operazioni di amministrazione nel tuo ambito di accesso."
      />
      <section className="card">
        <div className="card-heading">
          <h2>Eventi amministrativi</h2>
          <button
            className="button secondary small"
            disabled={download.isPending || !!query.error || !query.data?.total}
            onClick={() => download.mutate()}
          >
            Esporta CSV
          </button>
        </div>
        <HistoryFilters
          value={draft}
          set={setDraft}
          audit
          apply={() => {
            setFilter(draft);
            setOffset(0);
            download.reset();
          }}
        />
        <ErrorBox
          error={query.error || groups.error || download.error}
          reload={() => void query.refetch()}
        />
        {query.isPending ? (
          <Loading />
        ) : (
          !query.error &&
          query.data && (
            <>
              {query.data.items.length ? (
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Data e ora</th>
                        <th>Evento</th>
                        <th>Gruppo</th>
                        <th>Dettagli</th>
                      </tr>
                    </thead>
                    <tbody>
                      {query.data.items.map((row) => (
                        <tr key={row.id}>
                          <td className="nowrap">{date(row.timestamp)}</td>
                          <td>
                            <Badge>{row.event_type}</Badge>
                          </td>
                          <td>
                            {groups.data?.find((g) => g.id === row.group_id)
                              ?.name ||
                              (row.group_id
                                ? "Gruppo non disponibile"
                                : "Globale")}
                          </td>
                          <td>
                            <details className="audit-details">
                              <summary>Visualizza dettagli</summary>
                              <dl>
                                <dt>Utente</dt>
                                <dd>{row.user_id || "Sistema"}</dd>
                                {row.gateway_id && (
                                  <>
                                    <dt>Gateway</dt>
                                    <dd>{row.gateway_id}</dd>
                                  </>
                                )}
                                {row.session_id && (
                                  <>
                                    <dt>Sessione</dt>
                                    <dd>{row.session_id}</dd>
                                  </>
                                )}
                                {row.ip_address && (
                                  <>
                                    <dt>IP</dt>
                                    <dd>{row.ip_address}</dd>
                                  </>
                                )}
                              </dl>
                              {row.details && (
                                <pre>
                                  {JSON.stringify(row.details, null, 2)}
                                </pre>
                              )}
                            </details>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty title="Nessuna attività trovata" />
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
    </>
  );
}
