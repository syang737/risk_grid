import { useEffect, useState } from "react";
import { admin } from "./client";
import type { AlertRule, AlertTest, Condition } from "./types";

const MEASURES = [
  { value: "worst", label: "Max Risk" },
  { value: "market_value", label: "Current NLV" },
  { value: "delta", label: "Delta" },
  { value: "gamma", label: "Gamma" },
  { value: "vega", label: "Vega" },
  { value: "positions", label: "Position count" },
];

const COMPARATORS = [
  { value: "lessThan", label: "is below" },
  { value: "greaterThan", label: "is above" },
  { value: "lessThanOrEqual", label: "is at or below" },
  { value: "greaterThanOrEqual", label: "is at or above" },
];

const DIMENSIONS = [
  { value: "sector", label: "Product" },
  { value: "industry", label: "Industry" },
  { value: "underlying", label: "Instrument" },
  { value: "account", label: "Account" },
  { value: "master_account", label: "Master Account" },
  { value: "desk", label: "Desk" },
  { value: "contract", label: "Contract" },
];

interface Draft {
  name: string;
  dimension: string;
  condition: Condition;
  mode: "transition" | "every_batch";
  recipients: string;
}

const EMPTY: Draft = {
  name: "",
  dimension: "sector",
  condition: { column: "worst", op: "lessThan", value: -1_000_000 },
  mode: "transition",
  recipients: "",
};

function toBody(draft: Draft) {
  return {
    name: draft.name,
    spec: { dimensions: [draft.dimension], conditions: [draft.condition] },
    mode: draft.mode,
    recipients: draft.recipients.split(",").map((r) => r.trim()).filter(Boolean),
  };
}

/**
 * Alert rules.
 *
 * A rule is a saved pivot plus a threshold, so the editor is deliberately the
 * same vocabulary as the grid's filters: group by a dimension, compare a
 * measure. The test button matters more than it looks -- a threshold nobody has
 * checked against real data either never fires or fires on everything.
 */
export function AlertsScreen({ onError }: { onError: (m: string | null) => void }) {
  const [rules, setRules] = useState<AlertRule[]>([]);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [tested, setTested] = useState<AlertTest | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = () =>
    admin.alerts().then(setRules).catch((e) => onError(String(e)));

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function test() {
    setBusy(true);
    try {
      setTested(await admin.testAlert(toBody({ ...draft, name: draft.name || "untitled" })));
      onError(null);
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    setBusy(true);
    try {
      await admin.saveAlert(toBody(draft));
      setDraft(EMPTY);
      setTested(null);
      await refresh();
      onError(null);
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="admin-page">
      <div className="admin-head">
        <h2>Alerts</h2>
        <p className="hint">
          A rule is a pivot plus a threshold: group by a dimension, compare a measure. Test it
          against the latest batch before saving — a threshold nobody has checked either never
          fires or fires on everything.
        </p>
      </div>

      <section className="admin-block">
        <div className="panel-head">New rule</div>
        <div className="form-row wrap">
          <label>
            Name
            <input
              value={draft.name}
              placeholder="Sector loss limit"
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            />
          </label>
          <label>
            Any
            <select
              value={draft.dimension}
              onChange={(e) => setDraft({ ...draft, dimension: e.target.value })}
            >
              {DIMENSIONS.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
            </select>
          </label>
          <label>
            where
            <select
              value={draft.condition.column}
              onChange={(e) =>
                setDraft({ ...draft, condition: { ...draft.condition, column: e.target.value } })
              }
            >
              {MEASURES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
            </select>
          </label>
          <label>
            <select
              value={draft.condition.op}
              onChange={(e) =>
                setDraft({ ...draft, condition: { ...draft.condition, op: e.target.value } })
              }
            >
              {COMPARATORS.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
            </select>
          </label>
          <label>
            <input
              type="number"
              value={String(draft.condition.value ?? "")}
              onChange={(e) =>
                setDraft({
                  ...draft,
                  condition: { ...draft.condition, value: Number(e.target.value) },
                })
              }
            />
          </label>
        </div>

        <div className="form-row wrap">
          <label>
            Tell me
            <select
              value={draft.mode}
              onChange={(e) =>
                setDraft({ ...draft, mode: e.target.value as Draft["mode"] })
              }
            >
              <option value="transition">once, when it starts breaching</option>
              <option value="every_batch">every batch while it breaches</option>
            </select>
          </label>
          <label className="grow">
            Email
            <input
              value={draft.recipients}
              placeholder="risk@firm.com, ops@firm.com"
              onChange={(e) => setDraft({ ...draft, recipients: e.target.value })}
            />
          </label>
          <button type="button" onClick={test} disabled={busy}>Test</button>
          <button type="button" onClick={save} disabled={busy || !draft.name}>Save</button>
        </div>
        {draft.mode === "every_batch" && (
          <p className="hint">
            At an intraday cadence this can mean an email every 30 minutes for the same standing
            breach, which is how people learn to ignore alerts.
          </p>
        )}

        {tested && (
          <div className={tested.breaches ? "banner warn inline" : "banner ok inline"}>
            <div><b>{tested.description}</b></div>
            <div>
              {tested.breaches === 0
                ? `Nothing breaches in ${tested.batch}.`
                : `${tested.breaches} would breach in ${tested.batch}: ` +
                  tested.rows.slice(0, 5).map((r) => String(Object.values(r)[0])).join(", ")}
            </div>
          </div>
        )}
      </section>

      <section className="admin-block">
        <div className="panel-head">Active rules</div>
        {rules.length === 0 && <p className="hint">No rules yet.</p>}
        <table className="mapping-table">
          <tbody>
            {rules.map((rule) => (
              <tr key={rule.id}>
                <td><b>{rule.name}</b></td>
                <td className="muted">{rule.description}</td>
                <td className="muted">
                  {rule.mode === "transition" ? "on transition" : "every batch"}
                </td>
                <td className="muted">{rule.recipients.join(", ") || "no recipients"}</td>
                <td>
                  <button
                    type="button"
                    onClick={() =>
                      admin.deleteAlert(rule.id).then(refresh).catch((e) => onError(String(e)))
                    }
                  >
                    ×
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
