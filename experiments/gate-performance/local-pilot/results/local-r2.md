# Local timing observations

Descriptive local tuning only; no E1 quality pass or improvement claim.

The unit is a whole CLI invocation paired with its mate on the same shared WSL host. Internal schedule runs are not independent samples. All intervals below use seconds; SD is sample standard deviation and IQR uses linear-interpolated quartiles.

| Workload | Artifact-matched / planned pairs | B median (IQR; SD) | B+ median (IQR; SD) | B+/B geometric mean | SD of log ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| example-fixed-explore | 8 / 8 | 93.8231 (12.3704; 7.2117) | 90.5360 (11.2488; 8.0996) | 0.9918 | 0.0609 |
| example-fixed-replay | 8 / 8 | 115.3297 (11.9876; 8.6035) | 110.1017 (9.0202; 7.2454) | 0.9648 | 0.0839 |
| example-vulnerable-explore | 8 / 8 | 110.2799 (25.0176; 20.6224) | 116.8741 (19.6381; 14.0308) | 1.0415 | 0.2385 |
| example-vulnerable-replay | 8 / 8 | 118.9726 (17.9561; 12.9715) | 115.2757 (13.4669; 8.6287) | 1.0386 | 0.0786 |

B is the published release. B+ adds timing only. Their ratio includes instrumentation, build differences, cache effects, and shared-host variation; it is not an optimization effect.

## Exclusive stage coverage

Every B+ gate is partitioned without summing nested or overlapping intervals. Percentages are the median of per-gate stage/wall fractions, so medians need not sum to 100%. The raw reduction retains cumulative inclusive work separately.

### example-fixed-explore

| Stage | Gates | Median seconds | IQR seconds | SD seconds | Median wall % |
| --- | ---: | ---: | ---: | ---: | ---: |
| adapter_stop | 8 | 6.3136 | 0.0493 | 0.0455 | 6.9645 |
| block_recognition | 8 | 19.1246 | 0.3329 | 0.2073 | 21.1050 |
| bookkeeping | 8 | 0.0120 | 0.0028 | 0.0033 | 0.0126 |
| fixture_provision | 8 | 9.1576 | 0.4689 | 0.3735 | 9.9112 |
| fixture_reset | 8 | 0.8939 | 0.3397 | 0.2016 | 0.9923 |
| fixture_teardown | 8 | 2.0675 | 0.3617 | 0.3143 | 2.2070 |
| jar_snapshot | 8 | 0.4078 | 0.0529 | 0.0561 | 0.4332 |
| oracle_evaluate | 8 | 0.0213 | 0.0024 | 0.0016 | 0.0226 |
| outside_timing | 8 | 0.0465 | 0.0202 | 0.0131 | 0.0487 |
| preflight | 8 | 0.0289 | 0.0043 | 0.0059 | 0.0310 |
| process_start_to_ready | 8 | 51.9045 | 11.2653 | 7.7756 | 57.5410 |
| schedule_execute | 8 | 0.0022 | 0.0009 | 0.0010 | 0.0024 |
| step_wait | 8 | 0.8135 | 0.0997 | 0.0682 | 0.8520 |

### example-fixed-replay

| Stage | Gates | Median seconds | IQR seconds | SD seconds | Median wall % |
| --- | ---: | ---: | ---: | ---: | ---: |
| adapter_stop | 8 | 7.0989 | 0.0975 | 0.0597 | 6.5157 |
| block_recognition | 8 | 21.3179 | 0.3439 | 0.2084 | 19.3655 |
| bookkeeping | 8 | 0.0142 | 0.0021 | 0.0027 | 0.0137 |
| fixture_provision | 8 | 9.0411 | 0.8667 | 1.3241 | 8.6558 |
| fixture_reset | 8 | 1.2055 | 1.7857 | 1.1780 | 1.0668 |
| fixture_teardown | 8 | 2.0748 | 0.5241 | 0.2938 | 1.8057 |
| jar_snapshot | 8 | 0.4815 | 0.0428 | 0.0392 | 0.4462 |
| oracle_evaluate | 8 | 0.0256 | 0.0031 | 0.0037 | 0.0239 |
| outside_timing | 8 | 0.0462 | 0.0127 | 0.0108 | 0.0435 |
| preflight | 8 | 0.0298 | 0.0199 | 0.0133 | 0.0288 |
| process_start_to_ready | 8 | 64.6862 | 9.4195 | 6.5947 | 58.7453 |
| schedule_execute | 8 | 0.0033 | 0.0013 | 0.0007 | 0.0031 |
| step_wait | 8 | 0.9362 | 0.2217 | 0.1919 | 0.8777 |

### example-vulnerable-explore

| Stage | Gates | Median seconds | IQR seconds | SD seconds | Median wall % |
| --- | ---: | ---: | ---: | ---: | ---: |
| adapter_stop | 8 | 7.5957 | 0.0945 | 0.0999 | 6.4969 |
| block_recognition | 8 | 3.0542 | 0.4333 | 0.3188 | 2.6133 |
| bookkeeping | 8 | 0.0191 | 0.0064 | 0.0136 | 0.0148 |
| fixture_provision | 8 | 9.3028 | 0.8504 | 1.0996 | 8.3273 |
| fixture_reset | 8 | 2.1162 | 0.8315 | 0.7310 | 1.6464 |
| fixture_teardown | 8 | 1.6737 | 0.5335 | 0.3717 | 1.4231 |
| jar_snapshot | 8 | 0.7542 | 0.1365 | 0.0854 | 0.6148 |
| oracle_evaluate | 8 | 0.0474 | 0.0080 | 0.0060 | 0.0398 |
| outside_timing | 8 | 0.0425 | 0.0202 | 0.0125 | 0.0349 |
| preflight | 8 | 0.0263 | 0.0032 | 0.0021 | 0.0235 |
| process_start_to_ready | 8 | 91.6172 | 15.3160 | 12.2944 | 77.5323 |
| schedule_execute | 8 | 0.4206 | 0.0745 | 0.0472 | 0.3771 |
| step_wait | 8 | 0.3977 | 0.0906 | 0.0536 | 0.3344 |

### example-vulnerable-replay

| Stage | Gates | Median seconds | IQR seconds | SD seconds | Median wall % |
| --- | ---: | ---: | ---: | ---: | ---: |
| adapter_stop | 8 | 7.3136 | 0.0556 | 0.0994 | 6.3586 |
| block_recognition | 8 | 2.9119 | 0.3440 | 0.2503 | 2.4739 |
| bookkeeping | 8 | 0.0151 | 0.0011 | 0.0023 | 0.0132 |
| fixture_provision | 8 | 9.6496 | 0.3994 | 1.0880 | 8.5950 |
| fixture_reset | 8 | 2.1165 | 0.6795 | 0.6274 | 1.8419 |
| fixture_teardown | 8 | 2.1704 | 0.4339 | 0.4812 | 1.7681 |
| jar_snapshot | 8 | 0.7149 | 0.0860 | 0.0681 | 0.5952 |
| oracle_evaluate | 8 | 0.0437 | 0.0074 | 0.0062 | 0.0387 |
| outside_timing | 8 | 0.0328 | 0.0331 | 0.0204 | 0.0281 |
| preflight | 8 | 0.0324 | 0.0051 | 0.0036 | 0.0286 |
| process_start_to_ready | 8 | 88.4544 | 11.2169 | 7.3999 | 77.0175 |
| schedule_execute | 8 | 0.4015 | 0.0401 | 0.0415 | 0.3351 |
| step_wait | 8 | 0.3731 | 0.0396 | 0.0331 | 0.3295 |

## Accounting

| Workload | Arm | Planned | Completed receipts | Artifact checks passed |
| --- | --- | ---: | ---: | ---: |
| example-fixed-explore | B | 8 | 8 | 8 |
| example-fixed-explore | Bplus | 8 | 8 | 8 |
| example-fixed-replay | B | 8 | 8 | 8 |
| example-fixed-replay | Bplus | 8 | 8 | 8 |
| example-vulnerable-explore | B | 8 | 8 | 8 |
| example-vulnerable-explore | Bplus | 8 | 8 | 8 |
| example-vulnerable-replay | B | 8 | 8 | 8 |
| example-vulnerable-replay | Bplus | 8 | 8 | 8 |

Artifact checks passed means the local checks described in the registration. It does not include the missing resource, dedicated-host, cache restoration, or full acceptance obligations of E1. The initial stopped campaign remains separate.

Collection stop reason: none; assigned collection finished.
