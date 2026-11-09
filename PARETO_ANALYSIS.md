# Pareto analysis and selection

The evaluation set is the bounded set of policies named in the paired lab
manifest. The existing allocation engine supplies weighted water filling,
proportional shares and a one-transfer local search. There is no need for a
general-purpose solver to compare the resulting measured candidates.

## Objective vectors

Each configured objective has a positive weight and an explicit scale in the
metric's units. For a minimized metric its normalized loss is the measured
value divided by scale. A maximized metric reverses that sign. The native
solver rounds losses to millionths of a scale unit and computes the weighted
sum with signed 64-bit integers. It handles up to eight policies and ten
objectives with fixed bounds and no heap allocation in the native routine.

The report exposes two sets.

- The **point front** uses componentwise comparisons of the run medians.
  A policy is dominated if another is at least as good on every selected
  objective and strictly better on one. Weight magnitudes do not change
  this front. Descriptive regret is the difference from the lowest eligible
  weighted point score in the measured set.
- The **evidence front** uses paired intervals on signed metric differences
  between independent randomized blocks. A policy leaves this front only
  when an eligible alternative is supported as no worse on all selected
  objectives and better on at least one. An interval spanning zero remains
  unresolved. An observed advantage on one objective can coexist with an
  observed disadvantage on another.

Confidence intervals use the existing distribution-free order-statistic
median routine, with a Bonferroni family over the planned policy pairs,
guardrail and objective metrics, and weighted score. It assumes independent
blocks and preserves each within-block workload seed. Small sample counts
or noisy runs can prevent a comparison from resolving. Requests within a
single run are used to estimate its metrics and are not independent trials.

## Decision order

1. Check sampled kernel ownership, HTB constraints, complete independent
   blocks and each observed hard budget.
2. Calculate the descriptive front and paired evidence on eligible arms.
3. Keep the declared baseline unless an eligible alternative clears all four
   paired guardrails for critical P99, deadline misses, success and background
   goodput, and the upper paired weighted-loss bound is below the required
   improvement margin. If several qualify, sort by paired median weighted
   gain and then by policy name.
4. When measurement is missing, a policy fails a hard budget or the
   solver CPU budget is exceeded, retain the previous validated policy.
   An ineligible baseline produces no replacement recommendation. A report
   never commits a kernel change.

Observed cost and fairness in the chosen lab do not transfer automatically to
another scheduler, host, link or workload. The example profile budgets CPU,
peak daemon RSS and verified policy frequency from lab measurements. It
uses ten metrics in the schema and activates nine in the stable matrix.
Select the recovery metric only in an experiment with usable restoration
evidence. Right-censored recovery needs a survival-analysis extension before
it can contribute to the current paired score.
