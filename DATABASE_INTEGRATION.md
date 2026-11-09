# PostgreSQL workload integration

The PostgreSQL example performs real SQL over TCP using psycopg 3. It creates a persistent connection per worker and runs a seeded sequence of indexed primary-key reads from `flowcore_bench`.

## Isolated lab

The end-to-end runner creates the database container with Podman's `ns:` network mode inside the receiver namespace. It mounts only `postgres_schema.sql` read-only and uses trust authentication only inside that disposable namespace. Do not reuse this trust configuration on a host or production network.

```sh
python3 -m pip install -r examples/real_apps/requirements.txt
cmake --build build -j2
sudo podman pull docker.io/library/postgres@sha256:9f5be6795d4e9dfc0dca4c8a46eb858a9ddef631fc6b57953960f578d3fb1f9b
sudo python3 scripts/real_app_lab.py --postgres \
  --modes fq_codel,cake,static,hybrid,predictive \
  --repetitions 10 --capacity-kbit 1500 --rate 60 --db-rate 20 \
  --duration 15 --concurrency 8 --db-concurrency 4
```

The lab assigns destination port 5432 to the standard HTB class, port 9000 to critical HTTP, 9001 to background artifact transfer, and 9002 to gRPC. It records the configured qdisc and kernel snapshot per run. The exact interface and namespace objects are FLOWCORE-owned and removed at teardown.

For a local standalone database, initialize the schema from `postgres_schema.sql`, then run `postgres_benchmark.py --dsn 'postgresql://…'`. The standalone command only reports client measurements; it does not configure host QoS.

## Measurement meaning

The database client records every attempted query, success/error/timeout, client-observed latency, deadline miss, a matching PostgreSQL statement_timeout, query rate, and P50/P95/P99 among completed queries. It reports completion ratio and error rate beside those percentiles.

Query time includes client scheduling, TCP, server queueing, PostgreSQL execution, and response transfer. The demo query is an indexed lookup, but FLOWCORE cannot guarantee or infer arbitrary SQL execution time from transport metrics. The service intent is an advisory target, not an end-to-end guarantee.

The example associates the PostgreSQL socket cookie where the installed psycopg/libpq exposes its descriptor. Missing descriptor support is reported as unavailable; it does not fabricate a flow match.
