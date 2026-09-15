import { useEffect, useState } from "react";
import { admin } from "./client";
import type { ReportInfo, Run, SavedView } from "./types";

const STATUS_CLASS: Record<Run["status"], string> = {
  succeeded: "ok",
  running: "muted",
  quarantined: "warn",
  failed: "error",
};

/**
 * What arrived, when, and what happened to it.
 *
 * The question an ops person actually asks is "what happened to the 9:30 file",
 * so quarantined runs show the reason and the findings rather than just a
 * status -- "nothing in the logs" is not an answer.
 */
export function RunsScreen({ onError }: { onError: (m: string | null) => void }) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [reports, setReports] = useState<ReportInfo[]>([]);
  const [views, setViews] = useState<SavedView[]>([]);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = () =>
    Promise.all([admin.runs(), admin.reports(), admin.views()])
      .then(([r, p, v]) => {
        setRuns(r);
        setReports(p);
        setViews(v);
      })
      .catch((e) => onError(String(e)));

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function runNow(report: ReportInfo) {
    setBusy(true);
    try {
      const result = await admin.runReport(report.id);
      onError(null);
      alert(
        `Sent to ${result.sent.join(", ") || "nobody (no recipients)"}\n` +
          result.attachments.map((a) => `${a.filename} (${Math.round(a.bytes / 1024)}KB)`).join("\n"),
      );
      await refresh();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="admin-page">
      <div className="admin-head">
        <h2>Ingestion &amp; reports</h2>
        <p className="hint">
          Every attempt is recorded whether or not it worked. A quarantined file produced no
          batch at all — a batch that loads and is wrong is worse than no batch.
        </p>
      </div>

      <section className="admin-block">
        <div className="panel-head">Recent runs</div>
        {runs.length === 0 && <p className="hint">Nothing has arrived yet.</p>}
        <table className="mapping-table">
          <thead>
            <tr>
              <th>File</th><th>Status</th><th className="num">Rows</th>
              <th>Batch</th><th>Started</th><th />
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <>
                <tr key={run.id}>
                  <td>{run.fileName}</td>
                  <td className={STATUS_CLASS[run.status]}>{run.status}</td>
                  <td className="num">{run.rows.toLocaleString()}</td>
                  <td className="muted">{run.batchId || "—"}</td>
                  <td className="muted">
                    {run.startedAt ? new Date(run.startedAt).toLocaleString() : ""}
                  </td>
                  <td>
                    {(run.findings.length > 0 || run.error) && (
                      <button
                        type="button"
                        onClick={() => setExpanded(expanded === run.id ? null : run.id)}
                      >
                        {expanded === run.id ? "hide" : "why"}
                      </button>
                    )}
                  </td>
                </tr>
                {expanded === run.id && (
                  <tr key={`${run.id}-detail`}>
                    <td colSpan={6}>
                      {run.error && <div className="banner error inline">{run.error}</div>}
                      {run.findings.map((f, i) => (
                        <div key={i} className={`banner ${f.severity === "error" ? "error" : "warn"} inline`}>
                          {f.message}
                          {f.sample.length > 0 && (
                            <pre className="sample">{JSON.stringify(f.sample, null, 1)}</pre>
                          )}
                        </div>
                      ))}
                      {run.quarantineKey && (
                        <p className="hint">Quarantined at {run.quarantineKey}</p>
                      )}
                    </td>
                  </tr>
                )}
              </>
            ))}
          </tbody>
        </table>
      </section>

      <section className="admin-block">
        <div className="panel-head">Reports</div>
        {reports.length === 0 && (
          <p className="hint">
            No reports yet. A report is a saved view plus a schedule and recipients; it sends a
            picture of the view and the same rows as CSV.
          </p>
        )}
        <table className="mapping-table">
          <tbody>
            {reports.map((report) => (
              <tr key={report.id}>
                <td><b>{report.name}</b></td>
                <td className="muted">
                  {views.find((v) => v.id === report.viewId)?.name ?? `view ${report.viewId}`}
                </td>
                <td className="muted">{report.recipients.join(", ") || "no recipients"}</td>
                <td className="muted">{report.formats.join(" + ")}</td>
                <td className={report.lastError ? "error" : "muted"}>
                  {report.lastError
                    ? report.lastError
                    : report.lastRunAt
                      ? new Date(report.lastRunAt).toLocaleString()
                      : "never run"}
                </td>
                <td>
                  <button type="button" disabled={busy} onClick={() => runNow(report)}>
                    Send now
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
