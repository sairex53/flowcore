# Adaptive action validation results

## Audit of earlier no-action campaigns

The mixed-collapse and overload confirmation evidence already
recorded no optimizer actuation. The original mixed matrix retained
43 hold windows across optimizer runs. Of 129 service-window records,
117 had fewer than 100 successful samples and 113 lacked the class
pressure predicate. The overload confirmation retained 41 hold
windows. Of 123 service-window records, 113 had fewer than 100
successes and 111 lacked the class pressure predicate. The categories
overlap. These observations identify concrete admission barriers
in the recorded windows. They do not assign the measured failures
to a controller action.

The audit also found that the native solver could consider a
reservation boost for an otherwise eligible class with a met
application objective. The Python planner now excludes that
candidate. Separately, the old probe could retain an allocation
from a latency improvement without an observed target-class
effect. The retention gate now requires corroborating class evidence.

## Reproduction

~~~sh
cmake --build build
ctest --test-dir build --output-on-failure
sudo build/flowcore bench run scenarios/adaptive-action-paired.json
sudo build/flowcore bench run scenarios/adaptive-action-holdout.json
sudo build/flowcore bench run scenarios/adaptive-action-transport-ablation.json
sudo python3 -B tests/optimizer_integration.py
~~~

The paired matrix compares static HTB, the existing threshold
strategy, queue-aware, SLO-aware and the instrumented optimizer.
The same offered requests and class configuration are replayed
within each seed block. Queue-aware versus SLO-aware probes the
effect of the objective signal within the same allocator family.
The distinct holdout changes capacity, request sizes, RTT, jitter
and background load. The transport ablation changes only whether
a bounded diagnostic dump is required for an otherwise justified
owned HTB action. The matrices are exploratory and below the
prespecified ten-pair release gate.

[Engine](ADAPTIVE_ACTION_ENGINE.md) · [Trace schema](DECISION_TRACE.md)
