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

## 3b. The assumption frontier — answering "you built a world where you win"

Section 3 establishes that the *estimator* is honest. It does not touch a
different and sharper objection: this evaluation trains a learner on data
produced by `datagen/yukti_datagen/response.py` and then grades its decisions
with that same oracle.

The defences usually offered — the archetype is never a feature, treatment was
randomised — answer a narrower question than the one being asked. They establish
that the learner did not *cheat*: that it recovered the generator's structure
from observables rather than reading the label. Neither establishes that the
structure exists in real Indian payment data.

It cannot. `max_uplift[PERSUADABLE] = 0.46` against `max_uplift[SURE_THING] =
0.04` is an **input** chosen by the author, and the headline follows from it
arithmetically. A point estimate drawn from a world its author wrote is not
evidence about the world.

So the reply is not a defence. It is a sweep:

```bash
make sensitivity                            # every axis
make sensitivity AXIS=persuadable_uplift    # one
```

`control/yukti/eval/sensitivity.py` varies each load-bearing assumption across a
plausible range and reports where the thesis stops holding, **refitting the
model at every grid point** — because the question is whether uplift arbitration
pays *given that you fitted it in that world*, which is the position a real
deployment is in. Fitting once in the default world and scoring elsewhere would
measure transfer, which is a different question.

Every component in the path is the production component: the oracle, the feature
frame *including its leakage guard* (`frame_from_rows` is shared with the
database path precisely so the two cannot drift), the X-learner, the allocator,
the customer-level bootstrap. Nothing is reimplemented, because a sweep that
reimplemented the thing it tests would be measuring its own reimplementation.

**It needs no services.** The world is generated, explored, learned and graded in
process, so anyone can reproduce the frontier from a clean clone in about a
minute. That property is not convenience — it is the whole point of a rebuttal
to "your result depends on your simulator."

### The axes, and what each one is worth arguing about

| axis | headline assumes | the sceptic's position |
|---|--:|---|
| `persuadable_uplift` | 0.46 | Published uplift effects in retention marketing are usually single-digit points. |
| `sure_thing_uplift` | 0.04 | If sure things have real headroom too, propensity and uplift rank alike and there is nothing to sell. |
| `sleeping_dog_share` | 0.15 | If customers that contact actively harms do not exist, avoiding them is worth nothing. |
| `silent_retry_irritation` | 0.0 | A silent retry is not free: the issuer SMSes the customer on every debit attempt. |
| `fatigue_decay` | 0.78 | At 1.0 there is no cross-agent fatigue, and per-customer arbitration earns nothing. |

`silent_retry_irritation` deserves its own note. The allocator funds every
costless invisible action *without consulting its margin*, on the argument that
such an action has no downside branch. Until this axis existed, the **grader
shared that assumption** — the oracle also modelled retries as downside-free — so
the evaluation was structurally incapable of penalising the behaviour. A policy
and its grader agreeing on an assumption is not a test of it. The default is
still `0.0`, so no previously published number moved; what changed is that the
cost of being wrong is now measurable instead of invisible.

### The result

Run at 20,000 exploration cases per fit, 3,500 planning cases, a 200-contact
budget, seed 20260822. Every number below is `artifacts/sensitivity.json`,
rendered by `scripts/frontier_markdown.py` rather than transcribed — a
transcribed number in the one section whose whole purpose is "do not take my
word for it" would be worse than no section.

**Summary:**

| axis | headline assumes | crossover | past it, the winner is |
|---|--:|--:|---|
| `persuadable_uplift` | 0.46 | below ~0.097 | nobody — stop contacting |
| `sure_thing_uplift` | 0.04 | above ~0.162 | propensity |
| `sleeping_dog_share` | 0.15 | below ~0.019 | fixed cadence |
| `silent_retry_irritation` | 0.0 | none in range | — |
| `fatigue_decay` | 0.78 | none in range | — |

Three of five axes contain a point where this system loses. Those are the rows
that make the other two worth anything.

**`sure_thing_uplift` is the one that matters most**, because it is the thesis's
own argument running backwards. Uplift and propensity differ only insofar as
"likely to pay" and "pays *because* we asked" come apart. Give sure things real
headroom and they stop coming apart; past 0.162 the propensity ranker wins
because past 0.162 it is simply correct. Anyone evaluating this should ask what
that number actually is for their book, because it decides whether the causal
machinery is worth its complexity.

**Two axes show no crossover, and neither is a victory.**

`fatigue_decay` reaching 1.0 does not remove Niyama's edge — it *increases*
every arm's returns, because contacts stop decaying, and Niyama captures more of
the larger pool. The axis therefore does not isolate the value of cross-agent
arbitration; it would need a design where fatigue is removed while total
contactable value is held fixed. Stated as a limitation rather than a result.

`silent_retry_irritation` moves nothing because retries and contacts turn out to
go to **disjoint** case populations: `_menu` offers a silent retry only where the
decline is `retryable_silently` (funds and system failures) and a contact only
where it is `customer_actionable` (auth failures, expired cards). Measured
directly, the overlap between the cases Niyama contacts and the cases retry-only
acts on is **zero**. So pricing retry irritation shifts the shared baseline for
every arm equally and cancels out of a contact-attributable number. That is a
real finding about the action space, and it also means this axis cannot be used
to argue the costless-action rule is safe — it is measuring something else.

### Per-axis detail

#### `persuadable_uplift`

Headroom for the only profitable archetype. The single number the whole thesis rests on; the headline run assumes 0.46. 

| `persuadable_uplift` | fixed cadence | propensity | **Niyama** | winner |
|---|--:|--:|--:|---|
| 0.46 | -22,022 | 3,838 | **26,936** | **Niyama** |
| 0.34 | -22,022 | -3,176 | **25,542** | **Niyama** |
| 0.24 | -22,022 | -10,710 | **1,295** | **Niyama** |
| 0.16 | -43,606 | -10,420 | **11,745** | **Niyama** |
| 0.1 | -43,606 | -10,418 | **843** | **Niyama** |
| 0.06 | -43,606 | -10,460 | **-9,464** | retry-only (nobody should contact) |
| 0.03 | -43,606 | -10,521 | **-9,486** | retry-only (nobody should contact) |

**Crossover ≈ 0.097** — below this the uplift objective no longer pays for itself.

| assumption | arm | persuadable | sure thing | lost cause | sleeping dog |
|---|---|--:|--:|--:|--:|
| 0.46 | fixed cadence | 42 | 69 | 54 | 35 |
| 0.46 | propensity | 3 | 168 | 0 | 29 |
| 0.46 | **Niyama** | 66 | 96 | 17 | 21 |
| 0.34 | fixed cadence | 42 | 69 | 54 | 35 |
| 0.34 | propensity | 4 | 167 | 0 | 29 |
| 0.34 | **Niyama** | 76 | 91 | 17 | 16 |
| 0.24 | fixed cadence | 42 | 69 | 54 | 35 |
| 0.24 | propensity | 4 | 165 | 0 | 31 |
| 0.24 | **Niyama** | 48 | 104 | 25 | 23 |
| 0.16 | fixed cadence | 42 | 69 | 54 | 35 |
| 0.16 | propensity | 4 | 163 | 0 | 33 |
| 0.16 | **Niyama** | 56 | 100 | 23 | 21 |
| 0.1 | fixed cadence | 42 | 69 | 54 | 35 |
| 0.1 | propensity | 3 | 166 | 0 | 31 |
| 0.1 | **Niyama** | 47 | 102 | 29 | 22 |
| 0.06 | fixed cadence | 42 | 69 | 54 | 35 |
| 0.06 | propensity | 4 | 165 | 0 | 31 |
| 0.06 | **Niyama** | 45 | 113 | 20 | 22 |
| 0.03 | fixed cadence | 42 | 69 | 54 | 35 |
| 0.03 | propensity | 2 | 165 | 0 | 33 |
| 0.03 | **Niyama** | 43 | 115 | 19 | 23 |

#### `sleeping_dog_share`

Population share of customers that contact actively harms. If they do not exist, avoiding them is worth nothing. 

| `sleeping_dog_share` | fixed cadence | propensity | **Niyama** | winner |
|---|--:|--:|--:|---|
| 0.15 | -22,022 | 3,838 | **26,936** | **Niyama** |
| 0.11 | -22,022 | 121 | **46,140** | **Niyama** |
| 0.07 | -896 | 2,327 | **32,769** | **Niyama** |
| 0.04 | -896 | -7,249 | **18,064** | **Niyama** |
| 0.02 | 21,818 | 12,756 | **22,503** | **Niyama** |
| 0 | 32,724 | 12,056 | **15,762** | fixed cadence |

**Crossover ≈ 0.019** — below this the uplift objective no longer pays for itself.

| assumption | arm | persuadable | sure thing | lost cause | sleeping dog |
|---|---|--:|--:|--:|--:|
| 0.15 | fixed cadence | 42 | 69 | 54 | 35 |
| 0.15 | propensity | 3 | 168 | 0 | 29 |
| 0.15 | **Niyama** | 66 | 96 | 17 | 21 |
| 0.11 | fixed cadence | 41 | 76 | 56 | 27 |
| 0.11 | propensity | 2 | 177 | 0 | 21 |
| 0.11 | **Niyama** | 87 | 77 | 18 | 18 |
| 0.07 | fixed cadence | 36 | 86 | 61 | 17 |
| 0.07 | propensity | 1 | 188 | 0 | 11 |
| 0.07 | **Niyama** | 71 | 93 | 20 | 16 |
| 0.04 | fixed cadence | 41 | 91 | 59 | 9 |
| 0.04 | propensity | 1 | 192 | 0 | 7 |
| 0.04 | **Niyama** | 77 | 96 | 16 | 11 |
| 0.02 | fixed cadence | 39 | 94 | 62 | 5 |
| 0.02 | propensity | 0 | 197 | 0 | 3 |
| 0.02 | **Niyama** | 74 | 104 | 16 | 6 |
| 0 | fixed cadence | 39 | 97 | 64 | 0 |
| 0 | propensity | 1 | 199 | 0 | 0 |
| 0 | **Niyama** | 77 | 111 | 12 | 0 |

#### `sure_thing_uplift`

Headroom for customers who pay anyway. As this rises, propensity and uplift converge and the distinction stops paying. 

| `sure_thing_uplift` | fixed cadence | propensity | **Niyama** | winner |
|---|--:|--:|--:|---|
| 0.04 | -22,022 | 3,838 | **26,936** | **Niyama** |
| 0.1 | -10,334 | -3,382 | **12,608** | **Niyama** |
| 0.18 | -10,334 | 55,232 | **51,500** | propensity |
| 0.26 | 12,064 | 72,781 | **69,402** | propensity |
| 0.34 | 33,806 | 143,320 | **112,987** | propensity |

**Crossover ≈ 0.162** — above this the uplift objective no longer pays for itself.

| assumption | arm | persuadable | sure thing | lost cause | sleeping dog |
|---|---|--:|--:|--:|--:|
| 0.04 | fixed cadence | 42 | 69 | 54 | 35 |
| 0.04 | propensity | 3 | 168 | 0 | 29 |
| 0.04 | **Niyama** | 66 | 96 | 17 | 21 |
| 0.1 | fixed cadence | 42 | 69 | 54 | 35 |
| 0.1 | propensity | 4 | 167 | 0 | 29 |
| 0.1 | **Niyama** | 70 | 94 | 17 | 19 |
| 0.18 | fixed cadence | 42 | 69 | 54 | 35 |
| 0.18 | propensity | 3 | 170 | 0 | 27 |
| 0.18 | **Niyama** | 63 | 96 | 21 | 20 |
| 0.26 | fixed cadence | 42 | 69 | 54 | 35 |
| 0.26 | propensity | 5 | 165 | 0 | 30 |
| 0.26 | **Niyama** | 63 | 101 | 17 | 19 |
| 0.34 | fixed cadence | 42 | 69 | 54 | 35 |
| 0.34 | propensity | 4 | 168 | 0 | 28 |
| 0.34 | **Niyama** | 57 | 111 | 13 | 19 |

#### `silent_retry_irritation`

Per-attempt opt-out risk of a retry the merchant cannot see. The allocator funds costless actions unconditionally on the assumption this is zero. 

| `silent_retry_irritation` | fixed cadence | propensity | **Niyama** | winner |
|---|--:|--:|--:|---|
| 0 | -22,022 | 3,838 | **26,936** | **Niyama** |
| 0.02 | -22,022 | -3,363 | **35,484** | **Niyama** |
| 0.05 | -22,022 | -20,292 | **3,771** | **Niyama** |
| 0.09 | -22,022 | -3,079 | **44,701** | **Niyama** |
| 0.15 | -22,022 | -3,362 | **37,413** | **Niyama** |

*No crossover in the swept range.*

| assumption | arm | persuadable | sure thing | lost cause | sleeping dog |
|---|---|--:|--:|--:|--:|
| 0 | fixed cadence | 42 | 69 | 54 | 35 |
| 0 | propensity | 3 | 168 | 0 | 29 |
| 0 | **Niyama** | 66 | 96 | 17 | 21 |
| 0.02 | fixed cadence | 42 | 69 | 54 | 35 |
| 0.02 | propensity | 2 | 169 | 0 | 29 |
| 0.02 | **Niyama** | 75 | 88 | 17 | 20 |
| 0.05 | fixed cadence | 42 | 69 | 54 | 35 |
| 0.05 | propensity | 4 | 165 | 0 | 31 |
| 0.05 | **Niyama** | 73 | 91 | 16 | 20 |
| 0.09 | fixed cadence | 42 | 69 | 54 | 35 |
| 0.09 | propensity | 4 | 165 | 0 | 31 |
| 0.09 | **Niyama** | 70 | 95 | 19 | 16 |
| 0.15 | fixed cadence | 42 | 69 | 54 | 35 |
| 0.15 | propensity | 4 | 164 | 0 | 32 |
| 0.15 | **Niyama** | 72 | 94 | 17 | 17 |

#### `fatigue_decay`

Response decay per prior contact. At 1.0 there is no cross-agent fatigue and per-customer arbitration earns nothing. 

| `fatigue_decay` | fixed cadence | propensity | **Niyama** | winner |
|---|--:|--:|--:|---|
| 0.78 | -22,022 | 3,838 | **26,936** | **Niyama** |
| 0.85 | -22,022 | 8,076 | **42,391** | **Niyama** |
| 0.92 | -22,022 | 3,710 | **60,865** | **Niyama** |
| 0.97 | -22,022 | -4,056 | **57,935** | **Niyama** |
| 1 | -11,076 | 812 | **73,879** | **Niyama** |

*No crossover in the swept range.*

| assumption | arm | persuadable | sure thing | lost cause | sleeping dog |
|---|---|--:|--:|--:|--:|
| 0.78 | fixed cadence | 42 | 69 | 54 | 35 |
| 0.78 | propensity | 3 | 168 | 0 | 29 |
| 0.78 | **Niyama** | 66 | 96 | 17 | 21 |
| 0.85 | fixed cadence | 42 | 69 | 54 | 35 |
| 0.85 | propensity | 4 | 167 | 0 | 29 |
| 0.85 | **Niyama** | 73 | 92 | 17 | 18 |
| 0.92 | fixed cadence | 42 | 69 | 54 | 35 |
| 0.92 | propensity | 3 | 166 | 0 | 31 |
| 0.92 | **Niyama** | 74 | 89 | 17 | 20 |
| 0.97 | fixed cadence | 42 | 69 | 54 | 35 |
| 0.97 | propensity | 4 | 166 | 0 | 30 |
| 0.97 | **Niyama** | 80 | 80 | 19 | 21 |
| 1 | fixed cadence | 42 | 69 | 54 | 35 |
| 1 | propensity | 6 | 164 | 0 | 30 |
| 1 | **Niyama** | 78 | 81 | 26 | 15 |


### What this does and does not establish

**Does:** that the advantage is bounded, that the boundaries are locatable, and
that the targeting mechanism — not merely the money — separates the arms. The
contact mix is the strongest single artifact here: in the default world the
propensity arm reaches **3** persuadables out of 200 contacts against Niyama's
**66**, and that ratio is stable across every grid point, unlike the rupee
columns.

**Does not:** establish significance at any individual grid point. Niyama's
bootstrap CI at the default world is [−6,35,261, +22,22,373] per 1,000
opportunities and contains zero — precisely as §3's power analysis says it must,
since a 200-contact sample cannot resolve a 0.024σ effect. The signal is the
shape of the curve and the targeting counts. Anyone quoting a single cell of
these tables as a measured effect is misreading them, and that includes me.

**Also does not:** tell you the assumptions are right. It tells you which ones
the conclusion is hostage to. `max_uplift[PERSUADABLE]` remains a number chosen
by the author; what changed is that its influence is now visible and bounded
instead of buried in a module constant.


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
