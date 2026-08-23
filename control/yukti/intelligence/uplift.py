"""Uplift modelling: predicting the causal effect of acting, not the outcome.

The distinction this module exists to make:

  propensity  P(recover | treated)             — who is likely to pay
  uplift      P(recover | treated) - P(recover | not treated)
                                               — who pays BECAUSE we acted

They rank customers differently, and the difference is the product. A
propensity model ranks a "sure thing" first — high recovery rate, near-zero
causal effect — so a system driven by it spends its budget on people who were
going to pay anyway and bills the merchant for the privilege. That is what
every gross-recovery number in this market is measuring.

Two estimators are provided.

**T-learner** — two independent models, one per arm, uplift as their
difference. Preferred over an S-learner (one model with treatment as a feature)
because gradient boosting under-uses a single binary feature when other
features are more predictive, biasing the estimated effect toward zero, which
is exactly the quantity being measured.

**X-learner** — the one that actually works here. The T-learner's control model
sees only the control arm, and in a 75/25 RCT that is a quarter of the data
trying to learn a strongly heterogeneous outcome. Measured on a 7,506-case
run it collapsed: observed control recovery rates span 0.006 (lost causes) to
0.730 (sure things), but the T-learner's predictions compressed into
0.206-0.380 and the archetype ranking inverted. The X-learner instead imputes
each row's counterfactual using the *other* arm's model, fits effect models on
those imputed effects, and blends them by propensity — so the abundant treated
arm informs the estimate for control rows. That is precisely the imbalance
this dataset has, and an earlier note in this file dismissing X-learners as
"not yet earning their complexity" was simply wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

import lightgbm as lgb
import numpy as np
import pandas as pd

from yukti.intelligence.features import Frame


@dataclass(frozen=True, slots=True)
class UpliftScores:
    """Per-row predictions from a fitted model."""

    uplift: np.ndarray        # causal effect estimate
    p_treated: np.ndarray     # P(recover | treated)
    p_control: np.ndarray     # P(recover | not treated)

    @property
    def propensity(self) -> np.ndarray:
        """What a conventional model would rank on."""
        return self.p_treated


def _booster(seed: int) -> lgb.LGBMClassifier:
    # Deliberately small. The dataset is thousands of rows, not millions, and an
    # over-parameterised learner would fit noise in the control arm — where the
    # sample is smallest and the estimate matters most.
    return lgb.LGBMClassifier(
        n_estimators=300,
        learning_rate=0.05,
        num_leaves=15,
        min_child_samples=25,
        subsample=0.9,
        subsample_freq=1,
        colsample_bytree=0.8,
        reg_lambda=1.0,
        random_state=seed,
        verbosity=-1,
    )


class TLearner:
    """Two-model uplift estimator."""

    def __init__(self, seed: int = 20260822) -> None:
        self.seed = seed
        self.model_treated: lgb.LGBMClassifier | None = None
        self.model_control: lgb.LGBMClassifier | None = None
        self.columns: list[str] = []

    def fit(self, frame: Frame) -> TLearner:
        X, treated, y = frame.X, frame.treated.to_numpy(), frame.outcome.to_numpy()
        self.columns = list(X.columns)

        # Treatment-describing columns are dropped from BOTH models. They are
        # constant within each arm (control rows have no action), so leaving
        # them in lets the control model key off "action_kind is null" and
        # learn the arm rather than the customer.
        Xf = self._strip_treatment_columns(X)

        self.model_treated = _booster(self.seed).fit(Xf[treated == 1], y[treated == 1])
        self.model_control = _booster(self.seed + 1).fit(Xf[treated == 0], y[treated == 0])
        return self

    @staticmethod
    def _strip_treatment_columns(X: pd.DataFrame) -> pd.DataFrame:
        drop = [
            c for c in ("action_kind", "action_channel", "cost_paise",
                        "discount_paise", "discount_pct")
            if c in X.columns
        ]
        return X.drop(columns=drop)

    def predict(self, frame: Frame) -> UpliftScores:
        if self.model_treated is None or self.model_control is None:
            raise RuntimeError("model is not fitted")
        Xf = self._strip_treatment_columns(frame.X)
        p_t = self.model_treated.predict_proba(Xf)[:, 1]
        p_c = self.model_control.predict_proba(Xf)[:, 1]
        return UpliftScores(uplift=p_t - p_c, p_treated=p_t, p_control=p_c)


class XLearner:
    """Uplift estimator built for imbalanced arms.

    Three stages:
      1. outcome models per arm (as in the T-learner);
      2. imputed treatment effects — for a treated row, observed outcome minus
         what the control model predicts it would have done untreated, and
         symmetrically for control rows;
      3. effect models regressed on those imputed effects, blended by the
         propensity of being treated.

    The blend weight matters: control rows get their effect mostly from the
    model trained on treated-row imputations, which is where the data is.
    """

    def __init__(self, seed: int = 20260822) -> None:
        self.seed = seed
        self.mu_treated: lgb.LGBMClassifier | None = None
        self.mu_control: lgb.LGBMClassifier | None = None
        self.tau_treated: lgb.LGBMRegressor | None = None
        self.tau_control: lgb.LGBMRegressor | None = None
        self.p_treat: float = 0.5

    def _regressor(self, seed: int) -> lgb.LGBMRegressor:
        return lgb.LGBMRegressor(
            n_estimators=300, learning_rate=0.05, num_leaves=15,
            min_child_samples=20, subsample=0.9, subsample_freq=1,
            colsample_bytree=0.8, reg_lambda=1.0,
            random_state=seed, verbosity=-1,
        )

    def fit(self, frame: Frame) -> XLearner:
        X = TLearner._strip_treatment_columns(frame.X)
        t = frame.treated.to_numpy()
        y = frame.outcome.to_numpy()
        self.p_treat = float(t.mean())

        # Stage 1 — outcome models per arm.
        self.mu_treated = _booster(self.seed).fit(X[t == 1], y[t == 1])
        self.mu_control = _booster(self.seed + 1).fit(X[t == 0], y[t == 0])

        # Stage 2 — impute each row's counterfactual with the OTHER arm's model.
        d_treated = y[t == 1] - self.mu_control.predict_proba(X[t == 1])[:, 1]
        d_control = self.mu_treated.predict_proba(X[t == 0])[:, 1] - y[t == 0]

        # Stage 3 — effect models on the imputed effects.
        self.tau_treated = self._regressor(self.seed + 2).fit(X[t == 1], d_treated)
        self.tau_control = self._regressor(self.seed + 3).fit(X[t == 0], d_control)
        return self

    def predict(self, frame: Frame) -> UpliftScores:
        if self.tau_treated is None or self.tau_control is None:
            raise RuntimeError("model is not fitted")
        X = TLearner._strip_treatment_columns(frame.X)

        tau_t = self.tau_treated.predict(X)
        tau_c = self.tau_control.predict(X)
        # Blend by which effect model had more data to learn from. tau_treated
        # is fitted on the treated arm, tau_control on the control arm, so with
        # 76% of rows treated the treated-side estimator is the lower-variance
        # one and takes the larger weight.
        #
        # An earlier version had this inverted and put 0.757 weight on the model
        # fitted from the SCARCE arm, which is the opposite of the variance
        # argument that motivates an X-learner at all.
        g = self.p_treat
        uplift = g * tau_t + (1 - g) * tau_c

        return UpliftScores(
            uplift=uplift,
            p_treated=self.mu_treated.predict_proba(X)[:, 1],
            p_control=self.mu_control.predict_proba(X)[:, 1],
        )


class PropensityBaseline:
    """The competitor: rank by P(recover | treated), ignoring causality.

    This is what a well-built conventional recovery system does, and it is the
    arm that must be beaten. It is deliberately given the same features and the
    same learner, so any difference is the objective, not the implementation.
    """

    def __init__(self, seed: int = 20260822) -> None:
        self.seed = seed
        self.model: lgb.LGBMClassifier | None = None

    def fit(self, frame: Frame) -> PropensityBaseline:
        X, treated, y = frame.X, frame.treated.to_numpy(), frame.outcome.to_numpy()
        Xf = TLearner._strip_treatment_columns(X)
        self.model = _booster(self.seed).fit(Xf[treated == 1], y[treated == 1])
        return self

    def predict(self, frame: Frame) -> np.ndarray:
        if self.model is None:
            raise RuntimeError("model is not fitted")
        return self.model.predict_proba(TLearner._strip_treatment_columns(frame.X))[:, 1]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def qini_curve(
    score: np.ndarray, treated: np.ndarray, outcome: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Qini curve: cumulative incremental recoveries as we treat down the ranking.

    At each depth k, compare recoveries among treated rows in the top k against
    what the control rows in the top k would predict, scaled by the arm ratio.
    A model that ranks by causal effect climbs steeply; one that ranks by
    propensity wastes early budget on customers who would have paid anyway.
    """
    order = np.argsort(-score)
    t, y = treated[order], outcome[order]

    ct_t = np.cumsum(t * y)          # recoveries among treated, cumulative
    ct_c = np.cumsum((1 - t) * y)    # recoveries among control, cumulative
    n_t = np.cumsum(t)
    n_c = np.cumsum(1 - t)

    # The n_t/n_c rescaling is unbounded at shallow depth: with a handful of
    # control rows in the top slice the ratio explodes and the curve swings
    # wildly, which made random targeting outscore every real model. Require a
    # minimum of both arms before the curve is meaningful, and hold it at zero
    # until then rather than reporting noise as signal.
    min_arm = max(30, int(0.01 * len(score)))
    warm = (n_t >= min_arm) & (n_c >= min_arm)

    with np.errstate(divide="ignore", invalid="ignore"):
        scaled = np.where(n_c > 0, ct_c * (n_t / np.maximum(n_c, 1)), 0.0)
    gains = np.where(warm, ct_t - scaled, 0.0)
    return np.arange(1, len(score) + 1), gains


def auuc(score: np.ndarray, treated: np.ndarray, outcome: np.ndarray) -> float:
    """Area under the uplift curve, normalised by population size.

    Reported relative to the random-targeting baseline, so 0 means "no better
    than treating in arbitrary order" and negative means actively worse.
    """
    x, gains = qini_curve(score, treated, outcome)
    if len(x) < 2:
        return 0.0
    area = float(np.trapezoid(gains, x))
    # Random targeting is the straight line from origin to the final gain.
    random_area = 0.5 * gains[-1] * x[-1]
    return (area - random_area) / len(x)


def uplift_at_k(
    score: np.ndarray, treated: np.ndarray, outcome: np.ndarray, k: float = 0.3
) -> float:
    """Observed incremental recovery rate in the top k fraction of the ranking.

    More legible than AUUC for a merchant: "if you could only act on 30% of
    cases, how much better off are you?"
    """
    n = max(1, int(len(score) * k))
    top = np.argsort(-score)[:n]
    t, y = treated[top], outcome[top]
    if t.sum() == 0 or (1 - t).sum() == 0:
        return 0.0
    return float(y[t == 1].mean() - y[t == 0].mean())


class ActionConditionalUplift:
    """Uplift as a function of the ACTION, not just of being treated.

    `TLearner` and `XLearner` strip the action columns from both arms, for a
    good reason: those columns are null for every control row, so a model that
    keeps them can identify the arm from their absence and learn the assignment
    instead of the customer. But the consequence is that they estimate a single
    number per case — the effect of being treated by whatever mix the
    exploration policy happened to use — and the allocator then has nothing to
    choose between a WhatsApp message and a Rs 9 voice call except cost.

    That is the wrong question to leave unanswered, because choosing the
    intervention IS the product.

    The fix exploits an asymmetry the earlier models did not. Within the treated
    arm the action was drawn by `sample_intervention`, which takes only (rng, at)
    and never looks at the customer — so action is randomised *given* treatment,
    and E[Y | X, A=a] is identified for every a. So:

        mu_treated(X, a)   fitted on treated rows, action columns KEPT
        mu_control(X)      fitted on control rows, action columns absent
        tau(X, a)        = mu_treated(X, a) - mu_control(X)

    The asymmetry is the point, not an oversight: the treated model may condition
    on the action because the action varied randomly there; the control model
    may not, because there is nothing to condition on. Keeping the columns in
    the control model is the leak; keeping them in the treated model is the
    estimate.

    What this cannot do is estimate an effect for an action the exploration
    period never sampled. `EXPLORE_ACTIONS` covers all six actionable kinds, so
    every action the planner can propose has support — but a new channel added
    later would need exploration before this model could price it.
    """

    # Columns describing the intervention. Kept by the treated model, stripped
    # from the control model.
    ACTION_COLUMNS: tuple[str, ...] = (
        "action_kind", "action_channel", "cost_paise", "discount_paise",
        "discount_pct",
    )

    def __init__(self, seed: int = 20260822) -> None:
        self.seed = seed
        self.mu_treated: lgb.LGBMClassifier | None = None
        self.mu_control: lgb.LGBMClassifier | None = None
        self.treated_columns: list[str] = []
        self.control_columns: list[str] = []

    @classmethod
    def _strip(cls, X: pd.DataFrame) -> pd.DataFrame:
        return X.drop(columns=[c for c in cls.ACTION_COLUMNS if c in X.columns])

    def fit(self, frame: Frame) -> ActionConditionalUplift:
        X = frame.X
        t = frame.treated.to_numpy()
        y = frame.outcome.to_numpy()

        treated_X = X[t == 1]
        control_X = self._strip(X[t == 0])
        self.treated_columns = list(treated_X.columns)
        self.control_columns = list(control_X.columns)

        self.mu_treated = _booster(self.seed).fit(treated_X, y[t == 1])
        self.mu_control = _booster(self.seed + 1).fit(control_X, y[t == 0])
        return self

    def predict(self, frame: Frame) -> UpliftScores:
        """Score rows whose action columns describe a PROPOSED action.

        At training time those columns describe what was done; at scoring time
        they describe what we are considering doing. That substitution is the
        counterfactual, and it is only valid because the action was randomised.
        """
        if self.mu_treated is None or self.mu_control is None:
            raise RuntimeError("model is not fitted")

        X = frame.X
        p_t = self.mu_treated.predict_proba(X[self.treated_columns])[:, 1]
        p_c = self.mu_control.predict_proba(self._strip(X)[self.control_columns])[:, 1]
        return UpliftScores(uplift=p_t - p_c, p_treated=p_t, p_control=p_c)
