"""Yukti control-plane CLI."""

from __future__ import annotations

import typer

from yukti.store import db

app = typer.Typer(add_completion=False, help="Yukti control plane")


@app.command()
def migrate() -> None:
    """Apply pending database migrations."""
    db.migrate()


@app.command("reset-db")
def reset_db(
    confirm: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
) -> None:
    """Drop and recreate the schema, then re-migrate. Destructive."""
    if not confirm and not typer.confirm("Drop the entire public schema?"):
        raise typer.Abort()
    db.reset_schema()
    db.migrate()


@app.command()
def consume(
    group: str = typer.Option("yukti-opportunity", help="Kafka consumer group"),
    max_events: int = typer.Option(0, help="Stop after N events (0 = until idle)"),
) -> None:
    """Consume payment events and form recovery opportunities."""
    from yukti.opportunity.consumer import run

    run(group_id=group, max_events=max_events)


@app.command()
def serve(
    port: int = typer.Option(None, help="Port to bind"),
    reload: bool = typer.Option(False, help="Auto-reload on code change"),
) -> None:
    """Run the merchant console API."""
    import uvicorn

    from yukti.config import settings

    uvicorn.run(
        "yukti.api.main:app", host="0.0.0.0",
        port=port or settings().api_port, reload=reload, log_level="info",
    )


@app.command()
def outbox(
    batch: int = typer.Option(500, help="Rows per batch"),
    watch: bool = typer.Option(False, help="Keep draining until interrupted"),
) -> None:
    """Drain the transactional outbox to Kafka."""
    import time as _time

    from yukti.dispatch.outbox import OutboxRelay, pending_count
    from yukti.store.db import connect

    with connect() as conn:
        relay = OutboxRelay(conn)
        while True:
            stats = relay.drain_all(batch_size=batch)
            if stats.published or stats.failed:
                typer.echo(f"  published={stats.published} failed={stats.failed} "
                           f"pending={pending_count(conn)}")
            if not watch:
                break
            _time.sleep(1.0)


@app.command()
def plan(
    merchant: str = typer.Option(None, help="Merchant id; omit to plan every merchant"),
    date: str = typer.Option(None, "--date", help="Planning moment, ISO (default: now)"),
    limit: int = typer.Option(0, help="Cap cases considered (0 = all)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Decide, persist, do not dispatch"),
) -> None:
    """Run one planning cycle: score, stop, allocate, check, dispatch."""
    from datetime import UTC, datetime

    from rich.console import Console
    from rich.table import Table

    from yukti.pipeline import plan_cycle
    from yukti.store.db import connect

    console = Console()
    as_of = datetime.fromisoformat(date) if date else datetime.now(UTC)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)

    with connect() as conn:
        if merchant:
            merchant_ids = [merchant]
        else:
            merchant_ids = [
                r["id"] for r in conn.execute(
                    "SELECT DISTINCT c.merchant_id AS id FROM recovery_case c "
                    " WHERE c.state IN ('open', 'planning') ORDER BY 1"
                )
            ]
        if not merchant_ids:
            console.print("[yellow]no merchant has an open case[/] — "
                          "run `make replay-fast && make consume` first")
            raise typer.Exit(1)

        results = [
            plan_cycle(conn, mid, as_of, limit=limit or None, dry_run=dry_run)
            for mid in merchant_ids
        ]

        table = Table(title=f"Planning cycle @ {as_of:%Y-%m-%d %H:%M}",
                      header_style="bold")
        for col in ("merchant", "cases", "stopped", "dispatched", "escalated",
                    "suppressed", "contacts", "discount", "opt"):
            table.add_column(col, justify="right" if col != "merchant" else "left")
        for r in results:
            table.add_row(
                r.merchant_id[-8:], f"{r.considered:,}", f"{r.stopped:,}",
                f"{r.dispatched:,}", f"{r.escalated:,}", f"{r.suppressed:,}",
                f"{r.contacts_spent:,}",
                f"Rs {r.discount_spent_paise / 100:,.0f}",
                f"{r.optimality_ratio:.3f}",
            )
        console.print(table)

        stops: dict[str, int] = {}
        not_chased = 0
        for r in results:
            not_chased += r.not_chased_paise
            for reason, n in r.stop_breakdown.items():
                stops[reason] = stops.get(reason, 0) + n
        if stops:
            st = Table(title="Stopping rules — work deliberately not done",
                       header_style="bold")
            st.add_column("rule"); st.add_column("cases", justify="right")
            for reason, n in sorted(stops.items(), key=lambda kv: -kv[1]):
                st.add_row(reason, f"{n:,}")
            console.print(st)
            console.print(f"  money deliberately not chased: "
                          f"[bold]Rs {not_chased / 100:,.0f}[/]")

        if any(r.blocked for r in results):
            console.print(f"  [red]{sum(r.blocked for r in results)} actions blocked "
                          f"AFTER allocation[/] — the feasibility filter and the "
                          f"full policy evaluation disagreed; this is a bug")


@app.command("audit-verify")
def audit_verify(
    merchant: str = typer.Option(None, help="Merchant id; omit to verify all"),
) -> None:
    """Walk the audit hash chain and report the first row that does not verify."""
    from rich.console import Console

    from yukti import audit
    from yukti.store.db import connect

    console = Console()
    with connect() as conn:
        statuses = ([audit.verify(conn, merchant)] if merchant
                    else audit.verify_all(conn))
    if not statuses:
        console.print("[yellow]no audit events recorded[/]")
        return
    for st in statuses:
        colour = "green" if st.intact else "red"
        console.print(f"  [{colour}]{st}[/]")
    if not all(st.intact for st in statuses):
        raise typer.Exit(1)


@app.command("llm-status")
def llm_status(
    probe: bool = typer.Option(False, "--probe", help="Actually call each provider"),
) -> None:
    """Show which LLM providers are configured, and what failed last.

    Worth having as its own command: a chain that silently degrades to
    conservative defaults behaves plausibly and looks exactly like one that is
    working. That is the same failure shape as every serious bug in this
    project, so the difference gets a place to be visible.
    """
    from rich.console import Console
    from rich.table import Table

    from yukti.llm.chain import AllProvidersFailed, client

    console = Console()
    chain = client()

    if probe:
        # One real call, which walks the chain and populates the failures.
        from pydantic import BaseModel

        class _Ping(BaseModel):
            ok: bool

        try:
            result = chain.complete(
                system="Reply with JSON.", prompt="Return {\"ok\": true}",
                schema=_Ping, tier="fast", max_tokens=64, use_cache=False,
            )
            console.print(f"  [green]answered by {result.provider} "
                          f"({result.model})[/]\n")
        except AllProvidersFailed:
            console.print("  [yellow]no provider answered[/] — "
                          "Yukti will use conservative defaults\n")

    table = Table(title="LLM providers", header_style="bold")
    for col in ("provider", "key env", "status", "free tier"):
        table.add_column(col)
    for row in chain.report():
        colour = ("green" if row["status"] == "ok"
                  else "yellow" if row["configured"] else "dim")
        table.add_row(
            f"[{colour}]{row['provider']}[/]", row["key_env"],
            row["status"], row["free_tier"] or "—",
        )
    console.print(table)

    if not chain.any_configured:
        console.print(
            "\n  [dim]No provider configured. This is a supported state: the "
            "stopping rules, allocator, policy engine and dispatcher do not "
            "use a model. The agent falls back to conservative defaults and "
            "says so.[/]\n  [dim]Copy .env.example to .env and set one key to "
            "enable narratives.[/]"
        )


@app.command()
def agent(
    merchant: str = typer.Option(..., help="Merchant id"),
    date: str = typer.Option(..., "--date", help="Planning moment, ISO"),
) -> None:
    """Run the supervisor: scan for degradations, root-cause them, advise.

    Prints provenance alongside every conclusion. That is deliberate — a
    conclusion from a fallback reads exactly like one from the model unless the
    difference is shown, and a fleet quietly running on defaults is the failure
    mode nobody notices.
    """
    from datetime import UTC, datetime

    from rich.console import Console

    from yukti.agent import memory
    from yukti.agent.supervisor import Supervisor
    from yukti.domain.ids import trace_id
    from yukti.store.db import connect

    console = Console()
    as_of = datetime.fromisoformat(date)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)

    with connect() as conn:
        sup = Supervisor(conn)
        rid = sup.open_run(merchant, trace_id())
        advice = sup.advise(rid, merchant, as_of)
        sup.close_run(rid)
        conn.commit()

        if not advice.narratives:
            console.print(f"  no degradation detected at {as_of:%Y-%m-%d %H:%M}")
            return

        for issuer, narrative in advice.narratives.items():
            drops = sorted(advice.drops_for(issuer))
            console.print(f"\n  [bold]{issuer}[/]")
            console.print(f"    {narrative}")
            console.print(f"    [dim]contact kinds withheld: "
                          f"{', '.join(drops) if drops else 'none'}[/]")

        stats = memory.provenance_stats(conn, rid)
        console.print(f"\n  provenance: {stats}")
        if advice.degraded:
            console.print(
                "  [yellow]some conclusions came from the conservative default[/] — "
                "the model was unreachable, so the system withheld contact rather "
                "than guessing"
            )


@app.command("reset-planning")
def reset_planning(
    merchant: str = typer.Option(None, help="Merchant id; omit for every merchant"),
    confirm: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
) -> None:
    """Undo PLANNING output: reopen planned cases, clear their decisions and actions.

    A development affordance for re-running a cycle after changing the logic, not
    an operational tool — in production a decision that was made is a fact, and
    the audit trail exists precisely so it cannot be quietly withdrawn.

    **It must never touch the exploration history.** Those cases carry the
    randomised treatment assignment that identifies uplift, and they live in the
    same tables as planning output. An earlier version of this command reopened
    them: 2,156 exploration cases lost their `agent_decision` and
    `recovery_action` rows while keeping their outcomes, which would have made
    every one of them read as a CONTROL row that recovered — silently poisoning
    the RCT rather than breaking anything visibly.

    Two independent guards, because one is a single edit away from being wrong:

      1. `agent_run.kind = 'planner'` — exploration decisions are written by the
         generator with a NULL run_id, so only planning rows match.
      2. a case with a `recovery_outcome` row is finished history and is never
         reopened, whatever else is true of it.

    The audit chain is truncated WHOLE for the merchant rather than edited.
    Deleting rows out of the middle is exactly the tampering `audit.verify`
    detects, and leaving a knowingly-broken chain behind would train us to
    ignore the one signal that matters.
    """
    from rich.console import Console

    from yukti.store.db import connect

    console = Console()
    if not confirm and not typer.confirm(
        f"Discard planning output for {merchant or 'EVERY merchant'}?"
    ):
        raise typer.Abort()

    merchant_filter = "AND c.merchant_id = %(merchant)s" if merchant else ""
    params = {"merchant": merchant}

    # Cases this command is allowed to touch: planned by a planner run, and not
    # part of the finished exploration history.
    planned_cases = f"""
        SELECT DISTINCT c.id
          FROM recovery_case c
          JOIN agent_decision d ON d.case_id = c.id
          JOIN agent_run r      ON r.id = d.run_id AND r.kind = 'planner'
         WHERE NOT EXISTS (SELECT 1 FROM recovery_outcome o WHERE o.case_id = c.id)
           {merchant_filter}
    """

    with connect() as conn:
        conn.execute("CREATE TEMP TABLE _planned ON COMMIT DROP AS " + planned_cases,
                     params)
        counts = {}
        counts["recovery_action"] = conn.execute(
            "DELETE FROM recovery_action WHERE decision_id IN "
            "(SELECT d.id FROM agent_decision d JOIN agent_run r ON r.id = d.run_id "
            "  WHERE r.kind = 'planner' AND d.case_id IN (SELECT id FROM _planned))"
        ).rowcount
        counts["policy_evaluation"] = conn.execute(
            "DELETE FROM policy_evaluation WHERE decision_id IN "
            "(SELECT id FROM agent_decision WHERE run_id IS NOT NULL "
            "   AND case_id IN (SELECT id FROM _planned))"
        ).rowcount
        counts["agent_decision"] = conn.execute(
            "DELETE FROM agent_decision WHERE run_id IS NOT NULL "
            "  AND case_id IN (SELECT id FROM _planned)"
        ).rowcount
        counts["reopened"] = conn.execute(
            "UPDATE recovery_case SET state = 'open', stop_reason = NULL, "
            "       closed_at = NULL, version = version + 1 "
            " WHERE id IN (SELECT id FROM _planned) AND state <> 'open'"
        ).rowcount

        where_m = "WHERE merchant_id = %(merchant)s" if merchant else ""
        counts["agent_run"] = conn.execute(
            f"DELETE FROM agent_run {where_m + ' AND' if where_m else 'WHERE'} "
            f"kind = 'planner'", params).rowcount
        counts["audit_event"] = conn.execute(
            f"DELETE FROM audit_event {where_m}", params).rowcount
        counts["outbox"] = conn.execute(
            "DELETE FROM outbox WHERE published_at IS NULL").rowcount
        conn.execute(f"UPDATE budget_ledger SET consumed_val = 0 {where_m}", params)
        conn.commit()

    for table, n in counts.items():
        if n:
            console.print(f"  {table:20} {n:,}")


@app.command("seed-policy")
def seed_policy() -> None:
    """Give every merchant an active policy pack from its segment defaults."""
    from yukti.policy.store import seed_defaults
    from yukti.store.db import connect

    with connect() as conn:
        n = seed_defaults(conn)
        conn.commit()
    typer.echo(f"  seeded {n} merchant policy pack(s)")


if __name__ == "__main__":
    app()
