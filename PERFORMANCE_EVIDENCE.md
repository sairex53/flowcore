# Held-out performance evidence

The [preregistered plan](config/release-rc-plan.json) froze workload
parameters, independent seeds and regression margins at 061855d.
The mixed and Hybrid V3 profiles vary offered load and capacity.
The mixed profile uses two critical services, asymmetric shaping and
TCP and UDP traffic. The application stack uses an HTTP gateway,
backend and PostgreSQL. UDP scheduling and MPTCP are evaluated in
their own comparable topologies.

The first campaign stopped after a reproducible timing-ledger defect
was identified. All 24 recorded entries, including the interrupted
entry, remain under results/research-1794270125017657985. No invalid
measurements from that campaign are promoted to evidence. The
restarted campaign retained the plan and seeds at source revision
811e70e. It then hit ENOSPC. The 40-entry mixed matrix recorded 24
measured and 16 invalid runs, and the subsequent group startup
failures are retained. Those invalid blocks prevent a release-gate
comparison. Reinstallable dashboard-browser caches and a duplicate
toolchain archive were removed. A second retry retained the original
workloads and changed only execution order and the application
output directory. Raw outputs are indexed under results/rc-811e70e
and results/rc-811e70e-retry.

## Analysis contract

Use paired within-block differences and retain all blocks, including
workload and infrastructure failures. P50/P95/P99 are for successful
attempts and require the failure and deadline denominators beside them.
A shape with many missing or rejected requests can have an attractive
conditional percentile. The primary gate requires at least ten pairs.
The manifests predeclare relative limits of 10% for P99 and goodput,
absolute limits of one percentage point for failures and five for
deadline attainment, and additional fairness and CPU guards. The
Research Lab uses run-level paired bootstrap intervals with a
multiple-comparison correction. Other lab reports use exact
order-statistic median intervals with the coverage stated, and are
exploratory when their evidence cannot satisfy a primary gate.

Linux defaults are a separate unshaped, unimpaired reference. The
controlled shaped comparisons use matching bottlenecks. Static HTB
reservations were fixed before the held-out runs. Per-message UDP
cannot be applied to an ordinary TCP echo or a PostgreSQL stream.
The MPTCP comparator uses the Linux scheduler with both endpoints
configured. CPU, memory and update timings are reported only where
the specific runner instrumented them.

Run the individual argv arrays from the preregistered plan in order
with the trusted root runner. For integrity campaigns use the
following read-only commands on the completed summary.

~~~sh
./build/flowcore bench verify results/EXPERIMENT/summary.json
./build/flowcore bench report results/EXPERIMENT/summary.json
~~~

[Measurement integrity](MEASUREMENT_INTEGRITY.md) · [Research Lab](RESEARCH.md)
