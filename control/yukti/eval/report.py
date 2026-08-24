"""Rendering the comparison.

The report is built around one claim and is arranged so that claim is checkable
rather than merely stated: **the arm that recovers the most money is not the arm
that earns the most.** Gross and net sit in adjacent columns for exactly that
reason, and the merchant-facing receipt at the bottom is the sentence no
competitor in this market can print.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from yukti.domain.money import format_inr
from yukti.eval.arms import BY_KEY, HOLDOUT, REFERENCE, RIVAL
from yukti.eval.harness import EvalResult


def render(result: EvalResult, console: Console | None = None) -> None:
    console = console or Console()

    console.print(
        f"\n[bold]Evaluation[/] — merchant [cyan]{result.merchant_id[-8:]}[/] "
        f"@ {result.as_of:%Y-%m-%d %H:%M}   "
        f"{result.cases:,} cases · {result.customers:,} customers · "
        f"{result.holdout_cases:,} held out\n"
    )

    _arms_table(result, console)
    _headline(result, console)
    _estimator_validation(result, console)
    _receipt(result, console)


def _arms_table(result: EvalResult, console: Console) -> None:
    table = Table(title="Six arms, identical cases, identical draws",
                  header_style="bold")
    table.add_column("arm")
    table.add_column("policy")
    for col in ("acted", "contacts", "recovered", "gross ₹", "spend ₹",
                "net incremental ₹", "per 1k (95% CI)",
                "contact-attributable ₹"):
        table.add_column(col, justify="right")

    best_net = max(
        (m.true_incremental_margin_paise for m in result.metrics.values()),
        default=0)

    for key, metrics in result.metrics.items():
        arm = BY_KEY[key]
        is_best = (metrics.true_incremental_margin_paise == best_net
                   and metrics.cases > 0)
        style = "bold green" if is_best else ""
        interval = metrics.net_incremental_per_1k

        table.add_row(
            f"[{style}]{key}[/]" if style else key,
            arm.label,
            f"{metrics.actions_taken:,}",
            f"{metrics.contacts:,}",
            f"{metrics.recovered_cases:,}",
            format_inr(metrics.gross_recovered_paise),
            format_inr(metrics.cost_paise),
            f"[{style}]{format_inr(metrics.true_incremental_margin_paise)}[/]"
            if style else format_inr(metrics.true_incremental_margin_paise),
            f"{interval.point / 100:+,.0f} "
            f"[{interval.low / 100:+,.0f}, {interval.high / 100:+,.0f}]"
            if interval else "—",
            format_inr(metrics.contact_incremental_margin_paise)
            if key not in (REFERENCE.key, "B0") else "—",
        )
    console.print(table)


def _headline(result: EvalResult, console: Console) -> None:
    """The single comparison the project exists to make."""
    gross_winner = result.winner_by_gross()
    net_winner = result.winner_by_net()

    console.print()
    if gross_winner != net_winner:
        gw = result.metrics[gross_winner]
        nw = result.metrics[net_winner]
        console.print(
            f"  [bold]{BY_KEY[gross_winner].label}[/] recovered the most money "
            f"([bold]{format_inr(gw.gross_recovered_paise)}[/]).\n"
            f"  [bold green]{BY_KEY[net_winner].label}[/] EARNED the most "
            f"([bold green]{format_inr(nw.true_incremental_margin_paise)}[/] "
            f"incremental, against "
            f"{format_inr(gw.true_incremental_margin_paise)}).\n"
            f"  [dim]That gap is the product.[/]"
        )
    else:
        # Winning both is not the same as the arms failing to separate, and the
        # earlier version conflated them — it printed the "did not diverge"
        # warning while Yukti was ahead of the nearest rival by 1.7x. Divergence
        # is a property of the SPREAD, so measure that instead of inferring it
        # from which arm happened to top both columns.
        acting = [m.true_incremental_margin_paise for k, m in result.metrics.items()
                  if BY_KEY[k].acts]
        spread = (max(acting) - min(acting)) if acting else 0
        widest = max(abs(v) for v in acting) if acting else 0
        diverged = widest > 0 and spread / widest > 0.05

        nw = result.metrics[net_winner]
        if diverged:
            console.print(
                f"  [bold green]{BY_KEY[net_winner].label}[/] leads on both gross "
                f"and net "
                f"([bold green]{format_inr(nw.true_incremental_margin_paise)}[/] "
                f"incremental).\n"
                f"  [dim]It recovered the most money AND earned the most from "
                f"it — the arms separated by "
                f"{format_inr(spread)} across the acting policies.[/]"
            )
        else:
            console.print(
                f"  [yellow]{BY_KEY[net_winner].label} leads, but the arms barely "
                f"separated[/] (spread {format_inr(spread)}). Worth checking "
                f"whether the contact budget binds hard enough on this merchant "
                f"to force a real choice — `yukti.eval.cli sweep` answers that "
                f"directly."
            )

    rival = result.metrics.get(RIVAL.key)
    yukti = result.metrics.get("Y")
    if rival and yukti:
        console.print(
            f"\n  [dim]{RIVAL.label} spent "
            f"{format_inr(rival.cost_paise)} to earn "
            f"{format_inr(rival.true_incremental_margin_paise)}; "
            f"Yukti spent {format_inr(yukti.cost_paise)} to earn "
            f"{format_inr(yukti.true_incremental_margin_paise)}.[/]"
        )

    _contact_headline(result, console)


def _contact_headline(result: EvalResult, console: Console) -> None:
    """The comparison with the shared free-retry mass taken out.

    Every acting arm funds the same costless silent retries, and on a typical
    merchant that mass is an order of magnitude larger than the contact budget.
    Measured against doing nothing, it swamps the total and all the arms look
    alike. Measured against the retry-only arm it cancels exactly, and what is
    left is the only thing they actually disagree about: who gets contacted.
    """
    contenders = {
        k: m for k, m in result.metrics.items()
        if k not in (REFERENCE.key, "B0") and m.contact_incremental_per_1k
    }
    if not contenders:
        return
    best = max(contenders, key=lambda k: contenders[k].contact_incremental_margin_paise)
    ref = result.metrics.get(REFERENCE.key)

    console.print(
        f"\n  [bold]Spending the contact budget[/] — each arm against "
        f"{REFERENCE.label}, which they all share:"
    )
    for key, m in sorted(contenders.items(),
                         key=lambda kv: -kv[1].contact_incremental_margin_paise):
        iv = m.contact_incremental_per_1k
        mark = "[bold green]" if key == best else "[dim]"
        console.print(
            f"    {mark}{key:<3} {BY_KEY[key].label:<22} "
            f"{format_inr(m.contact_incremental_margin_paise):>16}   "
            f"per 1k {iv.point / 100:+,.0f} "
            f"[{iv.low / 100:+,.0f}, {iv.high / 100:+,.0f}][/]"
        )
    if ref:
        console.print(
            f"    [dim]{'':<3} {REFERENCE.label:<22} "
            f"{format_inr(0):>16}   (the reference)[/]"
        )

    winner = contenders[best]
    if winner.contact_incremental_margin_paise <= 0:
        console.print(
            "\n  [yellow]No arm earned its contact budget back.[/] On this "
            "merchant the contacts were value-destroying whoever picked them, "
            "which is a finding about the budget, not about the ranking."
        )


def _estimator_validation(result: EvalResult, console: Console) -> None:
    """Does the holdout estimate recover the truth?

    The whole incrementality claim rests on this. Everyone can compute a gross
    number; the question is whether a lift number is trustworthy, and the only
    honest answer is to show the estimator being checked against a known truth.
    """
    table = Table(
        title="Can a 10% holdout recover the true causal number?",
        header_style="bold",
        caption="Oracle truth is simulation-only. The holdout estimate is what a "
                "real deployment could actually compute.",
    )
    table.add_column("arm")
    for col in ("oracle truth ₹", "holdout estimate ₹ (95% CI)", "error", "covers?"):
        table.add_column(col, justify="right")

    covered = 0
    acting = 0
    for key, m in result.metrics.items():
        if not BY_KEY[key].acts or m.holdout_incremental is None:
            continue
        acting += 1
        brackets = m.holdout_brackets_truth
        covered += brackets
        iv = m.holdout_incremental
        table.add_row(
            key,
            format_inr(m.true_incremental_margin_paise),
            f"{iv.point / 100:+,.0f} "
            f"[{iv.low / 100:+,.0f}, {iv.high / 100:+,.0f}]",
            f"{m.holdout_estimate_error:+.1%}",
            "[green]yes[/]" if brackets else "[red]NO[/]",
        )
    console.print()
    console.print(table)

    # The point estimate being off is expected — a 10% holdout is a few hundred
    # observations. What matters is whether the interval covers the truth: a
    # wide honest interval is a working estimator, a tight interval that misses
    # is a broken one.
    if acting:
        if covered == acting:
            console.print(
                f"  [green]The holdout interval covers the true value for all "
                f"{acting} acting arms.[/] The estimate is noisy, as a 10% "
                f"holdout must be — and it is honest about how noisy."
            )
        else:
            console.print(
                f"  [red]{acting - covered} of {acting} holdout intervals miss "
                f"the true value.[/] That is an estimator problem, not sampling "
                f"noise, and the lift numbers cannot be trusted until it is fixed."
            )


def _receipt(result: EvalResult, console: Console) -> None:
    """The line a merchant is actually shown.

    No competitor in this market prints this, which is precisely why printing it
    earns trust: it is a harder claim than "we recovered X", and it is the one a
    CFO can act on.
    """
    yukti = result.metrics.get("Y")
    if not yukti or not yukti.recovered_cases:
        return

    caused = yukti.recovered_cases - yukti.would_have_recovered_anyway
    console.print(
        f"\n[bold]The receipt[/]\n"
        f"  You were billed for [bold]{yukti.recovered_cases:,}[/] recoveries.\n"
        f"  [bold]{yukti.would_have_recovered_anyway:,}[/] of them would have "
        f"happened anyway.\n"
        f"  We caused [bold green]{caused:,}[/], worth "
        f"[bold green]{format_inr(yukti.true_incremental_margin_paise)}[/] "
        f"net of MDR, discounts and channel costs.\n"
    )

    if yukti.opt_outs:
        console.print(
            f"  [dim]Cost side, stated plainly: {yukti.opt_outs:,} customers "
            f"opted out who would not have otherwise.[/]"
        )


# ---------------------------------------------------------------------------
# Machine-readable export
#
# The console renders the lift chart from this rather than re-running the
# evaluation: a five-arm run takes minutes, and an HTTP handler that could take
# minutes is not a handler. Writing it to a file also means the number the
# console shows is provably the same number the CLI printed, rather than a
# second computation that could drift from it.
# ---------------------------------------------------------------------------

import json
import pathlib

EXPORT_PATH = pathlib.Path(__file__).resolve().parents[3] / "artifacts" / "eval-report.json"


def to_dict(result: EvalResult) -> dict:
    return {
        "merchant_id": result.merchant_id,
        "as_of": result.as_of.isoformat(),
        "cases": result.cases,
        "customers": result.customers,
        "holdout_cases": result.holdout_cases,
        "winner_by_gross": result.winner_by_gross(),
        "winner_by_net": result.winner_by_net(),
        "arms": [
            {
                "key": key,
                "label": BY_KEY[key].label,
                "description": BY_KEY[key].description,
                "acts": BY_KEY[key].acts,
                "cases": m.cases,
                "actions_taken": m.actions_taken,
                "contacts": m.contacts,
                "recovered_cases": m.recovered_cases,
                "would_have_recovered_anyway": m.would_have_recovered_anyway,
                "opt_outs": m.opt_outs,
                "gross_recovered_paise": m.gross_recovered_paise,
                "spend_paise": m.cost_paise,
                "net_incremental_paise": m.true_incremental_margin_paise,
                "per_1k": _interval(m.net_incremental_per_1k),
                "contact_incremental_paise": m.contact_incremental_margin_paise,
                "contact_per_1k": _interval(m.contact_incremental_per_1k),
                "holdout_estimate": _interval(m.holdout_incremental),
                "holdout_error": m.holdout_estimate_error,
                "holdout_brackets_truth": m.holdout_brackets_truth,
            }
            for key, m in result.metrics.items()
        ],
    }


def _interval(iv) -> dict | None:
    if iv is None:
        return None
    return {"point": iv.point, "low": iv.low, "high": iv.high,
            "excludes_zero": iv.excludes_zero}


def save(result: EvalResult, path: pathlib.Path | None = None) -> pathlib.Path:
    target = path or EXPORT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(to_dict(result), indent=2, default=str))
    return target
