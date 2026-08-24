# EVALUATION — measured money, and what measuring it actually costs

The track's bar asks for **measured** money recovered across a batch. Most
recovery products report *gross* recovery: money that arrived after they acted.
Roughly a third of failed payments resolve on their own, so gross recovery bills
the merchant for organic behaviour.

This document is the measurement. It has three parts: how the comparison is made
fair, what the comparison found, and — the part worth reading — what it costs to
measure this honestly, which turned out to be the more defensible result.

Reproduce everything here with:

```bash
make eval                    # writes artifacts/eval-report.json
make eval                    # again — identical numbers, fixed seed
```

---

## 1. Making the arms comparable

Six arms. Every one runs **the same** allocator, the same stopping rules and the
same policy engine. Only the number being optimised changes.

| Arm | Policy | Why it is here |
|---|---|---|
| **B0** | holdout — no action | The denominator every competitor omits. |
| **B4** | retry-only | Funds every free silent retry, contacts nobody. The honest cheap baseline. |
| **B1** | fixed cadence | The industry default: assume contact helps, act on what you can afford. |
| **B2** | reason-code rules | A best-practice static routing table over the decline taxonomy. No model. |
| **B3** | propensity only | P(recover \| treated). **The one that matters** — same features, same learner, different objective. |
| **Y** | Yukti | Uplift: the causal effect of acting. |

That constraint is why `control/yukti/scoring.py` exists at all. The scorer was
made injectable during the integration work specifically so `eval/arms.py` could
be a table rather than six implementations — because if each arm had its own
pipeline, any difference in the result could always be a difference in the
plumbing.

**How every arm faces identical cases.** Each runs `plan_cycle(dry_run=True)`,
which persists a decision per case and touches no adapter and no budget. The
decisions are read back by `trace_id` and handed to
`datagen/yukti_datagen/response.py:evaluate` — the counterfactual oracle. The
oracle's recovery draw is keyed on the **case**, not on the intervention, so
every arm faces the same customer with the same luck. That is a paired design,
and it is what makes a small effect measurable without enormous samples. The
property was built and test-locked on day 1 precisely so this comparison could
be honest.

**Why B4 exists.** After the costless-action rule (below), every acting arm
funds the *same* free silent retries. On a typical merchant that shared mass is
an order of magnitude larger than the contact budget, so measured against B0 it
swamps the headline and all the arms look alike. Measured against B4 it cancels
exactly, and what remains is the only thing the arms disagree about: **who gets
contacted.** Both numbers are reported.

---

## 2. What it found

Merchant: NBFC lending book, 3,475 open cases, 1,369 customers, 372 held out.
Mean obligation ₹23,845.

| arm | contacts | recovered | gross ₹ | net incremental ₹ | contact-attributable ₹ | per 1k (95% CI) |
|---|--:|--:|--:|--:|--:|---|
| B0 holdout | 0 | 1,085 | 2,55,29,799 | 0 | — | — |
| B4 retry-only | 0 | 1,161 | 2,61,51,663 | 6,16,267 | reference | — |
| B1 fixed cadence | 86 | 1,158 | 2,60,18,498 | 4,84,269 | **−1,31,998** | −37,985 [−104,025, +25,003] |
| B2 reason-code | 88 | 1,158 | 2,60,18,498 | 4,84,267 | **−1,31,999** | −37,985 [−104,025, +25,004] |
| B3 propensity | 89 | 1,160 | 2,61,07,743 | 5,72,629 | **−43,638** | −12,558 [−59,323, +24,368] |
| **Y Yukti** | **88** | **1,172** | **2,65,16,297** | **9,77,522** | **+3,61,255** | **+103,958 [+38,351, +174,141]** |

Three things in that table are the whole argument.

**Yukti spends the same budget, not less.** 88 contacts against B2's 88 and B3's
89. It is not winning by abstaining; it is winning by choosing differently.

**Only Yukti's interval excludes zero.** +103,958 [+38,351, +174,141] per 1,000
opportunities. Every other arm's contact spending is negative with an interval
that crosses zero — they cannot even establish that contacting helped.

**Propensity is second-worst, and that is the prediction.** B3 has the same
features and the same learner as Yukti. Substituting P(recover | treated) for
uplift is the single change that turns this into an ordinary recovery tool, and
it costs ₹4,04,893 of net incremental margin. It spends its budget on customers
who were going to pay anyway and burns sleeping dogs.

**The merchant-facing line**, which is what actually earns trust:

> You were billed for 1,172 recoveries. 1,084 of them would have happened
> anyway. We caused 88, worth ₹9,77,522 net of MDR, discounts and channel costs.

### Segment sensitivity — the caveat that belongs next to the headline

On the d2c_subscription book (mean obligation **₹597**) the same code produces
+₹775 for Yukti against −₹844 and −₹1,856 for the rivals. The ordering holds,
but every interval crosses zero.

Contact economics scale with obligation value. A contact costs the same whether
it chases ₹597 or ₹24,000, so uplift allocation earns its keep on high-value
books and is close to irrelevant on low-value ones. Quoting only the NBFC number
would be selecting the flattering merchant; the honest claim is that **this
technique pays in proportion to what is at stake.**

### Where the budget stops binding

`python -m yukti.eval.cli sweep` varies the contact budget:

| contact budget | B1 | B2 | B3 | Y |
|--:|--:|--:|--:|--:|
| 0 | ₹0 | ₹0 | ₹0 | ₹0 |
| 90 | −844 | −844 | −1,856 | **+775** |
| 400 | −1,300 | −1,300 | −430 | **+775** |
| 1,200 | −1,300 | −1,300 | −430 | **+775** |
| 3,000 | −1,300 | −1,300 | −430 | **+775** |

At budget 0 every arm *is* retry-only, so the spread is exactly zero — a
built-in correctness check, and a non-zero row there would be a bug. Above ~400
the numbers saturate: the merchant contact pool stops binding and the
per-customer cap takes over. Yukti is flat throughout, because its contact set
is decided by which contacts have positive uplift rather than by how much budget
exists. That is the "knows when to stop" claim, measured: **given unlimited
budget it still contacts 88 people.**

---

## 3. What honest measurement costs — the more useful result

The evaluation reports two incremental numbers per arm:

1. **Oracle truth** — the counterfactual on every case. Ground truth, available
   only in simulation.
2. **Holdout estimate** — the same quantity computed the way a real deployment
   must, using only the ~10% of customers held out.

Reporting both validates *the estimator itself*. The result is not the one that
was expected.

**The holdout estimate is unusable at this scale**, and the intervals say so:
errors of −261% to −504% with intervals spanning ±₹70–80 lakh around a ₹6 lakh
truth. The intervals do cover the truth, but an interval that wide covers
anything, so "it covers" is not evidence of a working estimator.

**Stratification was tried and made it worse.** Post-stratifying the
difference-in-means on amount decile — the obvious fix, and a standard one —
moved the NBFC error from −414% to −504%. That measurement is what located the
problem, and the stratification was then removed rather than kept as
complexity that sounded principled and did nothing.

**The actual constraint, measured:**

| | |
|---|---|
| per-case standard deviation | **₹12,870** |
| Yukti's true per-case effect | **₹315** |
| effect size | **0.024σ** |
| cases needed, 11% holdout, 80% power | **136,887** |
| cases available | **3,475** — 39× short |

Recovery outcomes are a Bernoulli draw multiplied by a heavy-tailed obligation
amount, so the noise-to-effect ratio is a property of the data. No estimator
fixes it. The report prints this rather than hiding behind a wide interval:

```
What it would take to measure this for real. Yukti's true effect is ₹315.02
per case against a per-case spread of ₹12,869.87 — an effect size of 0.024 sigma.
At a 11% holdout that needs 136,887 cases for 80% power; there are 3,475 —
39x more than this merchant has.
```

**Why this is the stronger claim.** "Nobody measures incrementality honestly" is
an accusation. "At single-merchant volumes nobody *can*, with a holdout alone,
and here is the arithmetic" is a finding — and it points somewhere: measurement
has to pool across merchants, which is a statistical-power argument for the
federated idea in the roadmap rather than only a privacy one.

It is also the honest answer to the only question that matters about a lift
figure, which is *how do you know it is right?* Here: the oracle column is
trustworthy in simulation, the holdout column is what a real deployment gets,
and the gap between them is reported rather than papered over.

---

## 4. The upstream gate

The evaluation is only meaningful if uplift is learnable from observable
features at all. That was checked before any of the above was built, and it was
the scheduled risk of the project — if it had failed, the honest move was to
pivot to shipping the measurement plane alone.

**Gate: PASS, 7/7 splits on all four checks**, on the exploration-only training
set:

| archetype | uplift score | propensity |
|---|--:|--:|
| persuadable | **+0.0807** | 0.2282 |
| sure_thing | +0.0055 | **0.6764** |
| sleeping_dog | −0.0476 | — |
| lost_cause | +0.0147 | — |

Propensity ranks the sure thing first. Uplift ranks the persuadable first. They
disagree, and the disagreement is the product.

Two properties make this trustworthy rather than circular:

- **The archetype is never a feature.** `intelligence/features.py` asserts the
  column is absent, and `tests/integration/test_feature_leakage.py` builds a
  frame through the real code path and fails if the archetype or a perfect proxy
  appears. A model that read the archetype would score beautifully and mean
  nothing.
- **Treatment was randomised.** Uplift is not identified from observational data
  where treatment correlates with outcome, so the training set is a simulated
  exploration period in which each case received a uniformly random intervention
  or none — an RCT by construction, and exactly what a real deployment would
  have to obtain from its holdout plus a deliberate exploration budget.

---

## 5. Bugs this evaluation caught

Recorded because the failure mode matters more than the list: **every one
destroyed or biased data while reporting success.** None was caught by a
component's own tests, because each component was individually correct.

**Holdout contamination.** `_act` deliberately records the action it *would*
have taken on a held-out case, so the console can show the counterfactual. The
harness read `agent_decision.action_kind` back as an action performed, and
scored **213 held-out cases as treated** — moving the denominator every lift
number rests on, in the flattering direction, with nothing failing.
Now: the harness reads the *arm assignment*, held-out cases land in
`CaseState.HELD_OUT` rather than `SCHEDULED`, and
`tests/integration/test_holdout_integrity.py` asserts both the dispatch
guarantee and the measurement guarantee separately, because they broke
separately. Both were mutation-checked: reintroducing either bug fails them.

**Free actions declined on estimator noise — in four places.** A silent retry
costs nothing and the customer never sees it, so its true effect is small but
never negative; it has no downside branch. That makes `margin > 0` a test of the
*estimate's sign*, which near zero is close to a coin flip. The allocator,
`negative_expected_margin`, `lost_cause` and `contact_budget_spent` each
declined free money on that flip. Together they were the entire reason Yukti
lost its own first evaluation to a fixed cadence. The invariant is now asserted
once over the rules as a set in `tests/unit/test_costless_actions.py`, with the
mirror case proving it does not pass by nothing ever stopping.

**An estimand mismatch.** Truth sums over every case and a held-out case
contributes zero; the holdout estimate scaled by *total* cases, projecting the
effect onto cases truth had already counted as zero.

**Order-dependent results.** Facts were loaded before any reset, so whatever the
previous run left stopped dropped out of the case set — two runs on identical
data picked different merchants. The reset now runs first.

**A target leak, found two days after it was introduced.** `discount_paise` was
recorded only when a case recovered — economically true, catastrophic as a
feature. Measured: 1,936 of 1,936 discounted rows recovered (100%) against 7.9%
without. It survived because the T-learner and X-learner strip action columns,
so nothing in the gate could see it; the action-conditional model keeps them by
design and scored a 5% discount at **+0.92 uplift** immediately. Post-fix: 30.6%
vs 29.7% — no signal.

The pattern is consistent enough to be worth stating as a rule: **distrust any
metric that looks fine on one sample.** The bootstrap coverage test and the
seven-split gate both exist because a single split looked fine and wasn't.
