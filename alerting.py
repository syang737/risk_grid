"""Alert rules, evaluated with the pivot engine.

A rule is a saved pivot plus a threshold. "Any sector whose Max Risk is worse
than -10MM" is a group-by on sector with a post-aggregation filter on `worst`,
which is exactly what `risk/aggregate.py` already does and already tests. So
there is no second query language here: a rule serialises to a `PivotRequest`,
firing means the result came back non-empty, and the rows that came back are
what goes in the email.

The only genuinely new thing is remembering what was already breaching, so that
a standing breach does not alert every thirty minutes and train people to ignore
it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from control.models import AlertEvent, AlertMode, AlertRule, AlertState
from risk.aggregate import GREEK_COLUMNS, Filter, PivotRequest

# Measures a condition can be written against, beyond the scenario columns.
CONDITION_COLUMNS = ("worst", "positions", *GREEK_COLUMNS)

# Comparators offered in the rule editor, in AG Grid's vocabulary so the two
# filter UIs agree.
COMPARATORS = {
    "lessThan": "is below",
    "lessThanOrEqual": "is at or below",
    "greaterThan": "is above",
    "greaterThanOrEqual": "is at or above",
    "equals": "equals",
    "notEqual": "is not",
}


def _filters_from(raw: list[dict] | tuple) -> tuple[Filter, ...]:
    out = []
    for item in raw or ():
        if isinstance(item, Filter):
            out.append(item)
        else:
            out.append(Filter(
                column=item["column"], op=item["op"],
                value=item.get("value"), value2=item.get("value2"),
            ))
    return tuple(out)


def _filters_to(filters: tuple[Filter, ...]) -> list[dict]:
    return [
        {"column": f.column, "op": f.op, "value": f.value, "value2": f.value2}
        for f in filters
    ]


@dataclass
class AlertSpec:
    """What to watch, and when to care.

    `scope` narrows the book before aggregation (only options, only this desk).
    `conditions` test the aggregate afterwards -- which is the distinction
    `risk/aggregate.py` already enforces, and the reason a threshold on Max Risk
    means what a user expects.
    """

    dimensions: tuple[str, ...] = ("sector",)
    scope: tuple[Filter, ...] = ()
    conditions: tuple[Filter, ...] = ()
    measures: tuple[str, ...] = GREEK_COLUMNS

    def to_pivot_request(self) -> PivotRequest:
        if not self.conditions:
            raise ValueError("an alert rule needs at least one condition")
        return PivotRequest(
            dimensions=self.dimensions,
            filters=self.scope + self.conditions,
            measures=self.measures,
        )

    def describe(self) -> str:
        """One line a human can check against what they meant."""
        from risk.aggregate import DIMENSIONS

        label = DIMENSIONS[self.dimensions[0]].label.lower() if self.dimensions else "group"
        clauses = []
        for condition in self.conditions:
            verb = COMPARATORS.get(condition.op, condition.op)
            value = condition.value
            pretty = f"{value:,.0f}" if isinstance(value, (int, float)) else value
            clauses.append(f"{_measure_label(condition.column)} {verb} {pretty}")

        text = f"Any {label} where {' and '.join(clauses)}"
        if self.scope:
            scoped = ", ".join(f"{f.column} {COMPARATORS.get(f.op, f.op)} {f.value}" for f in self.scope)
            text += f" (within {scoped})"
        return text

    def to_dict(self) -> dict:
        return {
            "dimensions": list(self.dimensions),
            "scope": _filters_to(self.scope),
            "conditions": _filters_to(self.conditions),
            "measures": list(self.measures),
        }

    @classmethod
    def from_dict(cls, data: dict) -> AlertSpec:
        return cls(
            dimensions=tuple(data.get("dimensions") or ("sector",)),
            scope=_filters_from(data.get("scope", [])),
            conditions=_filters_from(data.get("conditions", [])),
            measures=tuple(data.get("measures") or GREEK_COLUMNS),
        )


def _measure_label(column: str) -> str:
    return {
        "worst": "Max Risk",
        "market_value": "Current NLV",
        "positions": "position count",
        "und_qty": "underlying quantity",
    }.get(column, column.replace("_", " ").title())


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


@dataclass
class Breach:
    group_key: str
    row: dict[str, Any]

    def value(self, column: str) -> Any:
        return self.row.get(column)


@dataclass
class Evaluation:
    """What a rule found in one batch."""

    rule_id: int
    rule_name: str
    description: str
    batch_id: str
    breaches: list[Breach] = field(default_factory=list)
    new_breaches: list[Breach] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)

    @property
    def should_notify_transition(self) -> bool:
        return bool(self.new_breaches)

    @property
    def should_notify_every(self) -> bool:
        return bool(self.breaches)


def evaluate_spec(batch, spec: AlertSpec) -> list[Breach]:
    """Run a rule against a batch. Rows returned are rows in breach."""
    request = spec.to_pivot_request()
    result = batch.aggregate(request)

    group_column = result.group_column
    breaches = []
    for row in result.rows.to_dicts():
        key = str(row.get(group_column, ""))
        # Scenario columns are noise in an alert email; keep the readable ones.
        trimmed = {
            k: v for k, v in row.items()
            if not (k.startswith("s") and k[1:].isdigit()) and not k.endswith("__n")
        }
        breaches.append(Breach(group_key=key, row=trimmed))
    return breaches


def evaluate_rule(session: Session, batch, rule: AlertRule) -> Evaluation:
    """Evaluate one rule and reconcile it against what was already breaching."""
    spec = AlertSpec.from_dict(rule.spec or {})
    breaches = evaluate_spec(batch, spec)

    evaluation = Evaluation(
        rule_id=rule.id,
        rule_name=rule.name,
        description=rule.description or spec.describe(),
        batch_id=batch.id,
        breaches=breaches,
    )

    now = datetime.now(timezone.utc)
    current = {b.group_key for b in breaches}
    states = {
        state.group_key: state
        for state in session.scalars(select(AlertState).where(AlertState.rule_id == rule.id))
    }

    for breach in breaches:
        state = states.get(breach.group_key)
        if state is None:
            state = AlertState(rule_id=rule.id, group_key=breach.group_key)
            session.add(state)
            states[breach.group_key] = state
        if not state.breaching:
            evaluation.new_breaches.append(breach)
            state.breaching = True
            state.since = now
        state.last_seen_at = now

    for key, state in states.items():
        if key not in current and state.breaching:
            state.breaching = False
            state.since = None
            state.last_seen_at = now
            evaluation.resolved.append(key)

    return evaluation


def evaluate_batch(session: Session, batch, firm_id: str) -> list[Evaluation]:
    """Evaluate every active rule for a firm against a freshly built batch.

    Called after the batch is registered, never before: an alert that fires on
    a batch nobody can open yet sends people to a screen that is not there.
    """
    rules = session.scalars(
        select(AlertRule).where(AlertRule.firm_id == firm_id, AlertRule.active.is_(True))
    ).all()

    out = []
    for rule in rules:
        try:
            out.append(evaluate_rule(session, batch, rule))
        except Exception as exc:
            # One malformed rule must not stop the others from being checked.
            out.append(Evaluation(
                rule_id=rule.id, rule_name=rule.name,
                description=f"rule could not be evaluated: {exc}",
                batch_id=batch.id,
            ))
    return out


def record_event(
    session: Session, firm_id: str, evaluation: Evaluation, breaches: list[Breach]
) -> AlertEvent:
    event = AlertEvent(
        firm_id=firm_id,
        rule_id=evaluation.rule_id,
        batch_id=evaluation.batch_id,
        rows=[b.row for b in breaches],
    )
    session.add(event)
    return event


def breaches_to_notify(evaluation: Evaluation, mode: AlertMode) -> list[Breach]:
    """Which breaches a rule in this mode should actually send."""
    if mode is AlertMode.every_batch:
        return evaluation.breaches
    return evaluation.new_breaches
