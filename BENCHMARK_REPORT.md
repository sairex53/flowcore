# Independent benchmark report

Maintainer-run reproduction on Fedora 45 ARM64/QEMU. No external reviewer or physical system participated.
The performance suite was frozen before pilot/evaluation. Static policy selection used disjoint pilot seeds.
The primary gate endpoint is overall successful-request P99 across traffic classes; critical-class metrics are secondary exploratory endpoints.
Every reported latency is offered/scheduled-to-completion application latency, including dispatch lag, TCP establishment, a request payload and 64-byte response. Failures and deadlines are reported separately.

## Static pilot

Selected static40: 40% critical guarantee.
Selection: lower median failure ratio, then lower overall P99, then higher background goodput; two pilot repetitions.

## primary

Evidence: /home/elliot/projects/flowcore/results/review-primary-1794013173664498860

| Mode | Valid/invalid | Overall P50/P95/P99 ms | Critical P99 ms | Failed/offered | Deadline attainment | Background Mbps | Background Jain | CPU s | Peak RSS MiB | Changes |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| static | 10/0 | 15.032/21.010/26.806 | 26.910 | 0/17600 | 1.000 | 7.580 | 1.000 | 1.479 | 32.660 | 0 |
| hybrid | 10/0 | 15.559/25.244/30.993 | 32.062 | 0/17600 | 1.000 | 7.576 | 1.000 | 1.525 | 34.057 | 0 |
| queue-aware | 10/0 | 15.468/25.014/30.269 | 31.064 | 0/17600 | 1.000 | 7.581 | 1.000 | 1.535 | 34.693 | 0 |
| predictive | 10/0 | 15.474/25.339/30.327 | 31.225 | 0/17600 | 1.000 | 7.591 | 1.000 | 1.575 | 34.316 | 0 |
| predictive-shadow | 10/0 | 15.484/25.421/31.012 | 31.875 | 0/17600 | 0.999 | 7.569 | 1.000 | 1.543 | 34.494 | 0 |

### Run-level variability and uncertainty

| Mode | Overall P99 min–max ms | 95% distribution-free median interval ms |
|---|---:|---|
| static | 25.740–28.088 | 25.808–27.851 (coverage 0.9785) |
| hybrid | 27.855–36.346 | 29.014–32.975 (coverage 0.9785) |
| queue-aware | 28.501–33.030 | 28.768–32.774 (coverage 0.9785) |
| predictive | 28.216–33.429 | 28.905–32.214 (coverage 0.9785) |
| predictive-shadow | 28.297–110.076 | 28.299–61.475 (coverage 0.9785) |

### Paired effects

- hybrid: overall P99 median relative regression 16.037%; gate INCONCLUSIVE; simultaneous interval {'low': 0.03360955223800179, 'high': 0.3408810723832358, 'coverage': 0.998046875}.
  Critical P99 median paired effect 19.352%; exploratory interval {'low': 7.5698670156146175, 'high': 24.92978463620057, 'coverage': 0.978515625}.
- queue-aware: overall P99 median relative regression 15.301%; gate INCONCLUSIVE; simultaneous interval {'low': 0.01469432854843248, 'high': 0.2699175914052256, 'coverage': 0.998046875}.
  Critical P99 median paired effect 16.757%; exploratory interval {'low': 7.033380921150423, 'high': 21.218897740929886, 'coverage': 0.978515625}.
- predictive: overall P99 median relative regression 13.861%; gate INCONCLUSIVE; simultaneous interval {'low': 0.069716180179356, 'high': 0.23299609814256148, 'coverage': 0.998046875}.
  Critical P99 median paired effect 15.011%; exploratory interval {'low': 10.3197156922481, 'high': 23.27908575428168, 'coverage': 0.978515625}.
- predictive-shadow: overall P99 median relative regression 15.373%; gate INCONCLUSIVE; simultaneous interval {'low': 0.060742396285317406, 'high': 3.213206520466899, 'coverage': 0.998046875}.
  Critical P99 median paired effect 17.290%; exploratory interval {'low': 7.21730096289912, 'high': 116.70445098915665, 'coverage': 0.978515625}.
- predictive_vs_shadow: {"overall_p99_percent_effect": {"n": 10, "median": -1.2055847161001387, "minimum": -70.73497128249191, "maximum": 6.62500245049189, "median_ci_95": {"low": -52.09830657647758, "high": 6.288319461738889, "coverage": 0.978515625}}, "qualification": "zero-action series cannot identify the benefit of dynamic actuation"}

## baselines

Evidence: /home/elliot/projects/flowcore/results/review-baselines-1794014094472115202

| Mode | Valid/invalid | Overall P50/P95/P99 ms | Critical P99 ms | Failed/offered | Deadline attainment | Background Mbps | Background Jain | CPU s | Peak RSS MiB | Changes |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| fifo | 3/0 | 282.842/310.559/312.710 | 311.544 | 0/1080 | 0.000 | 2.812 | 0.999 | 0.416 | 32.531 | 0 |
| fq_codel | 3/0 | 76.948/109.207/120.497 | 122.829 | 0/1080 | 0.353 | 2.686 | 0.998 | 0.433 | 32.258 | 0 |
| cake | 3/0 | 85.886/95.457/98.050 | 98.222 | 0/1080 | 0.333 | 2.682 | 1.000 | 0.451 | 31.684 | 0 |
| static | 3/0 | 31.180/45.305/52.545 | 51.003 | 0/1080 | 0.986 | 2.667 | 0.985 | 0.458 | 32.523 | 0 |

### Run-level variability and uncertainty

| Mode | Overall P99 min–max ms | 95% distribution-free median interval ms |
|---|---:|---|
| fifo | 311.312–315.347 | Insufficient independent runs |
| fq_codel | 115.663–122.829 | Insufficient independent runs |
| cake | 96.748–98.249 | Insufficient independent runs |
| static | 49.573–54.262 | Insufficient independent runs |

### Paired effects

- fifo: overall P99 median relative regression 500.152%; gate INCONCLUSIVE; simultaneous interval None.
  Critical P99 median paired effect 518.291%; exploratory interval None.
- fq_codel: overall P99 median relative regression 126.361%; gate INCONCLUSIVE; simultaneous interval None.
  Critical P99 median paired effect 128.177%; exploratory interval None.
- cake: overall P99 median relative regression 84.126%; gate INCONCLUSIVE; simultaneous interval None.
  Critical P99 median paired effect 90.994%; exploratory interval None.

## low

Evidence: /home/elliot/projects/flowcore/results/review-low-1794014196307277881

| Mode | Valid/invalid | Overall P50/P95/P99 ms | Critical P99 ms | Failed/offered | Deadline attainment | Background Mbps | Background Jain | CPU s | Peak RSS MiB | Changes |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| static | 3/0 | 16.822/22.445/24.442 | 27.292 | 0/360 | 1.000 | 0.000 | N/A | 0.339 | 31.902 | 0 |
| hybrid | 3/0 | 16.347/22.215/23.930 | 27.420 | 0/360 | 1.000 | 0.000 | N/A | 0.354 | 33.523 | 0 |
| queue-aware | 3/0 | 15.774/22.404/23.301 | 23.606 | 0/360 | 1.000 | 0.000 | N/A | 0.349 | 32.555 | 0 |
| predictive | 3/0 | 16.637/22.776/23.997 | 23.997 | 0/360 | 1.000 | 0.000 | N/A | 0.361 | 33.215 | 0 |

### Run-level variability and uncertainty

| Mode | Overall P99 min–max ms | 95% distribution-free median interval ms |
|---|---:|---|
| static | 23.301–25.349 | Insufficient independent runs |
| hybrid | 23.521–26.086 | Insufficient independent runs |
| queue-aware | 23.138–24.741 | Insufficient independent runs |
| predictive | 23.165–30.448 | Insufficient independent runs |

### Paired effects

- hybrid: overall P99 median relative regression -2.095%; gate INCONCLUSIVE; simultaneous interval None.
  Critical P99 median paired effect 0.468%; exploratory interval None.
- queue-aware: overall P99 median relative regression -4.667%; gate INCONCLUSIVE; simultaneous interval None.
  Critical P99 median paired effect -13.507%; exploratory interval None.
- predictive: overall P99 median relative regression 2.983%; gate INCONCLUSIVE; simultaneous interval None.
  Critical P99 median paired effect -14.963%; exploratory interval None.

## linux_default

Evidence: /home/elliot/projects/flowcore/results/review-linux_default-1794014296991932472

| Mode | Valid/invalid | Overall P50/P95/P99 ms | Critical P99 ms | Failed/offered | Deadline attainment | Background Mbps | Background Jain | CPU s | Peak RSS MiB | Changes |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| linux-default | 3/0 | 6.190/12.569/14.679 | 14.679 | 0/360 | 1.000 | 0.000 | N/A | 0.331 | 31.633 | 0 |

### Run-level variability and uncertainty

| Mode | Overall P99 min–max ms | 95% distribution-free median interval ms |
|---|---:|---|
| linux-default | 12.761–15.024 | Insufficient independent runs |

