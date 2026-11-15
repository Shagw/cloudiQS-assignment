"""Agent tools. Per-request state (the batch, the alert log, the webhook client) travels in
invocation_state, so concurrent invocations never share data."""

from collections import defaultdict
from statistics import mean, median

from strands import ToolContext, tool

from .models import Alert, RiskLevel, Transaction


def _batch(ctx: ToolContext) -> dict[str, Transaction]:
    return ctx.invocation_state["transactions"]


@tool(context=True)
def get_customer_profile(customer_id: str, tool_context: ToolContext) -> dict:
    """Compute behavioural statistics for one customer across the current batch.

    Use this to compare a transaction against the customer's own baseline (typical amount,
    usual merchants, transaction velocity) before deciding whether it is anomalous.

    Args:
        customer_id: The customer to profile.
    """
    txns = sorted(
        (t for t in _batch(tool_context).values() if t.customer_id == customer_id),
        key=lambda t: t.timestamp,
    )
    if not txns:
        return {"error": f"unknown customer_id {customer_id!r}"}

    amounts = [float(t.amount) for t in txns]
    gaps = [(b.timestamp - a.timestamp).total_seconds() for a, b in zip(txns, txns[1:])]
    merchants: dict[str, int] = defaultdict(int)
    for t in txns:
        merchants[t.merchant] += 1

    return {
        "customer_id": customer_id,
        "transaction_count": len(txns),
        "amount_mean": round(mean(amounts), 2),
        "amount_median": round(median(amounts), 2),
        "amount_max": max(amounts),
        "merchants": dict(merchants),
        "min_seconds_between_transactions": min(gaps) if gaps else None,
        "currencies": sorted({t.currency for t in txns}),
        "transactions": [
            {
                "transaction_id": t.transaction_id,
                "amount": float(t.amount),
                "merchant": t.merchant,
                "timestamp": t.timestamp.isoformat(),
            }
            for t in txns
        ],
    }


@tool(context=True)
def notify_risk_team(
    transaction_id: str,
    risk_level: str,
    reasons: list[str],
    tool_context: ToolContext,
) -> dict:
    """Send an alert to the risk team's webhook for a suspicious transaction.

    Call this once for each transaction you judge to be suspicious. Do not call it for
    transactions you consider normal.

    Args:
        transaction_id: ID of the suspicious transaction (must be from the input batch).
        risk_level: One of "low", "medium", "high", "critical".
        reasons: Short, specific, evidence-based reasons the transaction is suspicious.
    """
    state = tool_context.invocation_state
    txn = _batch(tool_context).get(transaction_id)
    if txn is None:
        return {"error": f"transaction_id {transaction_id!r} is not in this batch"}
    try:
        level = RiskLevel(risk_level.lower())
    except ValueError:
        return {"error": f"risk_level must be one of {[l.value for l in RiskLevel]}"}
    alerts: dict[str, Alert] = state["alerts"]
    # Strands runs parallel tool calls concurrently, so check-and-claim must be atomic.
    with state["lock"]:
        if transaction_id in state["claimed"]:
            return {"status": "already_notified"}
        state["claimed"].add(transaction_id)

    reasons = [r.strip()[:300] for r in reasons if r.strip()][:10]
    result = state["webhook"].send(
        {
            "event": "transaction.flagged",
            "risk_level": level.value,
            "reasons": reasons,
            "transaction": txn.model_dump(mode="json"),
        }
    )
    alerts[transaction_id] = Alert(
        transaction_id=transaction_id,
        customer_id=txn.customer_id,
        risk_level=level,
        reasons=reasons,
        webhook_delivered=result.delivered,
        webhook_status=result.status,
        webhook_error=result.error,
    )
    return {"delivered": result.delivered, "status": result.status, "error": result.error}
