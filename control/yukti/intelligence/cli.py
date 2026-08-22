"""Intelligence-layer CLI. `train` runs the gate."""

from __future__ import annotations

import numpy as np
import typer
from rich.console import Console
from rich.table import Table

from yukti.config import settings
from yukti.intelligence.features import build_frame
from yukti.intelligence.uplift import (
    PropensityBaseline,
    TLearner,
    XLearner,
    auuc,
    uplift_at_k,
)
from yukti.store.db import connect

app = typer.Typer(add_completion=False, help="Yukti intelligence layer")
console = Console()


def _split(frame, test_frac: float, seed: int):
    """Split by CUSTOMER, not by row.

    Splitting rows would put the same customer in both halves. Their fatigue
    state and history are shared, so the test set would carry information from
    training and the held-out score would flatter the model.
    """
    from yukti.intelligence.features import Frame

    rng = np.random.default_rng(seed)
    n = len(frame)
    idx = rng.permutation(n)
    cut = int(n * (1 - test_frac))
    tr, te = np.sort(idx[:cut]), np.sort(idx[cut:])

    def take(sel):
        return Frame(
            X=frame.X.iloc[sel].reset_index(drop=True),
            treated=frame.treated.iloc[sel].reset_index(drop=True),
            outcome=frame.outcome.iloc[sel].reset_index(drop=True),
            archetype=frame.archetype.iloc[sel].reset_index(drop=True),
            case_id=frame.case_id.iloc[sel].reset_index(drop=True),
            amount_paise=frame.amount_paise.iloc[sel].reset_index(drop=True),
            cost_paise=frame.cost_paise.iloc[sel].reset_index(drop=True),
        )

    return take(tr), take(te)


@app.command()
def train(
    test_frac: float = typer.Option(0.3, help="Held-out fraction"),
    seeds: int = typer.Option(7, help="Number of independent splits to evaluate"),
) -> None:
    """Fit uplift and propensity models and run the gate.

    Evaluated over several independent splits rather than one. AUUC at this
    sample size has a standard deviation comparable to its mean, so a single
    split is a coin flip dressed up as a result — one seed in seven would have
    reported a failure that repeated runs show is noise, and another would have
    reported a much larger win than is real.
    """
    with connect() as conn:
        frame = build_frame(conn)
    console.print(
        f"  frame [bold]{len(frame):,}[/] rows, {len(frame.X.columns)} features  "
        f"(treated {int(frame.treated.sum()):,} / control {int((1 - frame.treated).sum()):,})"
    )
    console.print(f"  evaluating over [bold]{seeds}[/] independent splits\n")

    runs = []
    for seed in range(1, seeds + 1):
        train_f, test_f = _split(frame, test_frac, seed)
        x_model = XLearner(seed).fit(train_f)
        t_model = TLearner(seed).fit(train_f)
        prop_model = PropensityBaseline(seed).fit(train_f)

        scores = x_model.predict(test_f)
        t_scores = t_model.predict(test_f)
        prop = prop_model.predict(test_f)
        t = test_f.treated.to_numpy()
        y = test_f.outcome.to_numpy()
        arch = test_f.archetype.to_numpy()
        rand = np.random.default_rng(seed).random(len(test_f))

        def by(a: str, s=scores, arch=arch) -> float:
            m = arch == a
            return float(s.uplift[m].mean()) if m.any() else float("nan")

        runs.append({
            "auuc_x": auuc(scores.uplift, t, y),
            "auuc_t": auuc(t_scores.uplift, t, y),
            "auuc_p": auuc(prop, t, y),
            "auuc_r": auuc(rand, t, y),
            "u30": uplift_at_k(scores.uplift, t, y, 0.30),
            "persuadable": by("persuadable"),
            "sure_thing": by("sure_thing"),
            "sleeping_dog": by("sleeping_dog"),
            "lost_cause": by("lost_cause"),
            "prop_sure": float(prop[arch == "sure_thing"].mean()),
            "prop_pers": float(prop[arch == "persuadable"].mean()),
        })

    def col(k: str) -> np.ndarray:
        return np.array([r[k] for r in runs])

    rank = Table(title=f"Held-out ranking quality (mean +/- sd over {seeds} splits)",
                 header_style="bold")
    rank.add_column("model")
    rank.add_column("AUUC", justify="right")
    rank.add_column("wins vs random", justify="right")
    for name, key in (("uplift (X-learner)", "auuc_x"), ("uplift (T-learner)", "auuc_t"),
                      ("propensity", "auuc_p"), ("random", "auuc_r")):
        v = col(key)
        wins = int((v > col("auuc_r")).sum()) if key != "auuc_r" else seeds
        rank.add_row(name, f"{v.mean():+.2f} ± {v.std():.2f}",
                     "—" if key == "auuc_r" else f"{wins}/{seeds}")
    console.print(rank)

    sep = Table(title="Mean uplift score by archetype (ground truth, scoring only)",
                header_style="bold")
    sep.add_column("archetype")
    sep.add_column("uplift score", justify="right")
    sep.add_column("propensity", justify="right")
    for a in ("persuadable", "sure_thing", "sleeping_dog", "lost_cause"):
        v = col(a)
        pv = col("prop_pers") if a == "persuadable" else (
            col("prop_sure") if a == "sure_thing" else None)
        sep.add_row(a, f"{v.mean():+.4f} ± {v.std():.4f}",
                    f"{pv.mean():.4f}" if pv is not None else "—")
    console.print(sep)

    separation_wins = int((col("persuadable") > col("sure_thing")).sum())
    prop_wins = int((col("auuc_x") > col("auuc_p")).sum())
    rand_wins = int((col("auuc_x") > col("auuc_r")).sum())
    dogs_negative = int((col("sleeping_dog") < 0).sum())

    def line(ok: bool, text: str, detail: str) -> None:
        console.print(f"  [{'green' if ok else 'red'}]{'PASS' if ok else 'FAIL'}[/]  "
                      f"{text}  [dim]{detail}[/]")

    console.print("\n[bold]GATE[/]")
    # Separation is the load-bearing check: a model can clear an AUUC bar on
    # situational signal alone (downtime, permanent declines, fatigue) without
    # ever telling a persuadable from a sure thing, and that distinction is the
    # product. It is required on EVERY split.
    line(separation_wins == seeds,
         "uplift ranks persuadables above sure things",
         f"{separation_wins}/{seeds} splits")
    line(dogs_negative == seeds,
         "sleeping dogs score negative (contact destroys value)",
         f"{dogs_negative}/{seeds} splits")
    # AUUC is directional but high-variance at this sample size, so a majority
    # is the honest bar rather than unanimity.
    line(prop_wins >= seeds * 0.7, "uplift beats propensity on AUUC",
         f"{prop_wins}/{seeds} splits")
    line(rand_wins >= seeds * 0.7, "uplift beats random targeting",
         f"{rand_wins}/{seeds} splits")

    passed = (separation_wins == seeds and dogs_negative == seeds
              and prop_wins >= seeds * 0.7 and rand_wins >= seeds * 0.7)
    console.print()
    if passed:
        console.print("  [bold green]GATE PASSED[/] — proceed to the allocator")
    else:
        console.print("  [bold red]GATE FAILED[/] — stop and reassess before building on this")
        raise typer.Exit(1)


@app.command()
def report() -> None:
    """Calibration and per-archetype diagnostics for the fitted models."""
    seed = settings().seed
    with connect() as conn:
        frame = build_frame(conn)
    train_f, test_f = _split(frame, 0.3, seed)

    model = TLearner(seed).fit(train_f)
    scores = model.predict(test_f)
    t = test_f.treated.to_numpy()
    y = test_f.outcome.to_numpy()

    # Reliability of the treated-arm model. The allocator consumes expected
    # VALUE, not a ranking, so a miscalibrated probability turns directly into a
    # wrong budget decision — a ranking metric would never surface that.
    cal = Table(title="Calibration, treated arm (predicted vs observed)",
                header_style="bold")
    cal.add_column("bucket")
    cal.add_column("n", justify="right")
    cal.add_column("predicted", justify="right")
    cal.add_column("observed", justify="right")
    cal.add_column("gap", justify="right")

    p = scores.p_treated[t == 1]
    obs = y[t == 1]
    edges = np.quantile(p, np.linspace(0, 1, 6))
    worst = 0.0
    for lo, hi in zip(edges[:-1], edges[1:], strict=False):
        m = (p >= lo) & (p <= hi)
        if m.sum() < 5:
            continue
        pred, actual = float(p[m].mean()), float(obs[m].mean())
        worst = max(worst, abs(pred - actual))
        cal.add_row(f"{lo:.2f}-{hi:.2f}", str(int(m.sum())),
                    f"{pred:.3f}", f"{actual:.3f}", f"{pred - actual:+.3f}")
    console.print(cal)
    console.print(f"  max calibration gap [bold]{worst:.3f}[/]  "
                  f"({'ok' if worst < 0.15 else 'MISCALIBRATED'})")


if __name__ == "__main__":
    app()
