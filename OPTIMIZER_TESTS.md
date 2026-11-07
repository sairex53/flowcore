# Optimizer verification

## Local commands

```sh
cmake -S . -B build -G Ninja
cmake --build build
ctest --test-dir build --output-on-failure
ctest --test-dir build -R optimizer_unit --output-on-failure
sudo python3 -B tests/optimizer_integration.py
```

The integration runner requires a disposable privileged Linux VM. It starts a dedicated research daemon with private runtime and state paths. Its link and traffic operations are confined to verified namespace inodes. The test checks host link and qdisc identity before and after.

## What the tests establish

- The native property suite generates 600 seeded four-class models. It checks capacity, class floors and upper bounds, deterministic repeated outputs and integer scores for all four algorithms. An independent exhaustive neighborhood enumerator checks the complete adaptive choice, including hold and unreserved parent budget.
- Analytic fixtures check weighted surplus shares, capped demand, proportional rounding and the largest supported capacity. Invalid native bounds are rejected. A separate timeline fixture checks convergence across an allocation change and a pending probe.
- Daemon-model fixtures check aggregate service minima, invalid owners, monotonic expiry, stale observations and soft overcommit. The static fallback exists only when all hard contracts fit.
- A simulated kernel rejects the second class operation. The test verifies decrease-first ordering, compensation to the original allocation and refusal to overwrite unexpected state. A successful transaction returns the exact verified diff.
- The privileged test checks that preview and shadow leave kernel classes alone, active mode applies an authorized ceiling, and the controller verifies a bounded reservation transfer with real competing TCP traffic. It uses a fixed application SLO trace for actuator coverage. This trace is a test fixture, separate from the real application performance measurements.
- The same privileged test checks protected background rate and actual class packet accounting, restoration with the ceiling intact, refusal to overwrite external parent capacity drift, and namespace cleanup.

See the [resource model](POLICY_OPTIMIZER.md) and [performance study](OPTIMIZATION_RESULTS.md).
