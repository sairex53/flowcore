# Real application benchmark methodology

## Reproduction

The runner requires root because it creates two disposable network namespaces, veth links, qdiscs, and (when requested) a Podman database container joined to the receiver namespace. It refuses to run when another FLOWCORE daemon or research runner owns the lab.

```sh
cmake -S . -B build -DFLOWCORE_BPF=OFF
cmake --build build -j2
sudo python3 scripts/real_app_lab.py --postgres \
  --modes linux-default,fq_codel,cake,static,hybrid,predictive \
  --repetitions 10 --seed 47031 --capacity-kbit 1500 \
  --rate 60 --db-rate 20 --duration 15 --concurrency 8 --db-concurrency 4
```

Use --postgres to add PostgreSQL and --grpc to add gRPC to the same namespace pair. With both flags, the runner starts the API, database, gRPC service, background upload, FLOWCORE, OTLP receiver, and clients together. The runner randomizes mode order while pairing mode runs by repetition seed. Each run starts a fresh daemon, new namespace pair, application server, OTLP receiver, and (with `--postgres`) uniquely named PostgreSQL container. It saves full per-request JSON, environment/configuration, controller history, network snapshots, an aggregate CSV, and HTML summary below `results/real-applications-*/`. Privileged setups that fail readback are marked invalid rather than counted.

`linux-default` removes the egress shaper entirely. It is an unshaped control and does not have the configured 1.5 Mbit/s bottleneck; interpret it separately. `fq_codel` is one HTB capacity class with fq_codel, not a pure root fq_codel link shaper. CAKE shapes at the requested rate. `static` uses fixed HTB class shares. `hybrid` and `predictive` run the existing FLOWCORE active controller strategies on the same class model. CAKE and single-class fq_codel do not provide per-service class guarantees.

## Recorded outcomes

The HTTP cohort records attempted/completed requests, errors, timeouts, completion ratio, deadline misses, P50/P95/P99 of successful requests, dispatch lag, and offered request rate. The artifact transfer records bytes, completion time, and goodput. With PostgreSQL, the database cohort additionally records query completion/error/deadline metrics and query throughput. The runner records daemon CPU and HTTP service CPU deltas; database container CPU is not currently included. The background backup is an upload from sender to receiver, so it traverses the same shaped egress as critical API requests. A separate HTTP artifact download endpoint is also provided, but download traffic follows the reverse direction and is not used as the lab egress contender.

The application client exports one bounded OTLP/HTTP JSON batch after its workload has completed, avoiding periodic exporter traffic during the measured interval. The benchmark report stores whether this export succeeded. The collector exposes Prometheus output and bounded series state.

## Statistical limits

Ten paired repetitions are the configured default recommendation; requests within a run are not independent experiments. Each run preserves raw samples, and the HTML table only summarizes observed runs. The current report does not calculate confidence intervals or a formal causal effect. Increase repetitions and vary request rates, capacities, impairment profiles, and seeds before making general performance claims.

No comparison has been run as part of this methodology document yet. Do not infer that FLOWCORE wins from the presence of an active controller or a low single-run P99. Publish losses, failure rates, deadline misses, background completion, and resource overhead with the same prominence as successful-request percentiles.
