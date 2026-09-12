# A/B comparison — A (A-20260912T0514Z) vs B (B-20260912T0859Z)

- A: image `vllm/vllm-openai@sha256:24f2f8975d011ea7f7066a547886a08a1fd3c4bf0880463487fae4f01ce723c6 sha256:60d84700e24f8919993c4ced08de9e54d8918f674e2fb9b213f1d529cd1b528d`, engine argv hash `d52c6568b17c`
- B: image `vllm/vllm-openai@sha256:819ec9c063412e5730d1b0e82046ba540d1bf991f3c4f661a849aae8a0c52374 sha256:5a0f8b914da56ea2ae2dbe569e4219a14605ce798e7eff12ae6db63328adc4f1`, engine argv hash `1ff8b57bbff7`

Delta is B − A; ✓ = better, ✗ = worse (lower is better for latency, failures, swap, Xid, restarts).

## c1_short

| metric | A | B | delta |
|---|---|---|---|
| requests ok | 40 | 40 | +0 (+0.0 %) |
| requests failed | 0 | 0 | +0 |
| aggregate completion tok/s | 70.9 | 77.8 | +6.920 (+9.8 %) ✓ |
| aggregate prompt tok/s | 481.8 | 557.2 | +75.400 (+15.6 %) ✓ |
| TTFT p50 s | 0.120 | 0.110 | -0.010 (-8.3 %) ✓ |
| TTFT p95 s | 0.283 | 0.120 | -0.162 (-57.4 %) ✓ |
| total p50 s | 0.435 | 0.403 | -0.032 (-7.4 %) ✓ |
| total p95 s | 1.074 | 1.187 | +0.113 (+10.5 %) ✗ |
| decode tok/s p50 | 111.5 | 109.6 | -1.910 (-1.7 %) ✗ |
| inter-token gap p95 s | 0.010 | 0.010 | -0.000 (-1.0 %) ✓ |
| inter-token gap max s | 0.023 | 0.015 | -0.008 (-33.0 %) ✓ |
| head GPU util mean % | 62.0 | 68.8 | +6.800 (+11.0 %) ✓ |
| head GPU util max % | 93.0 | 94.0 | +1.000 (+1.1 %) ✓ |
| head GPU power mean W | 24.6 | 25.1 | +0.500 (+2.0 %) ✗ |
| head swap-out pages | 0 | 0 | +0 |
| head Xid (run) | 0 | 0 | +0 |
| head restarts | 0 | 0 | +0 |
| head fault samples | 0 | 0 | +0 |
| head CPU VLLM::EngineCor mean % | 97.4 | 100.9 | +3.500 (+3.6 %) ✗ |
| head CPU VLLM::Worker_TP mean % | 176.8 | 197.3 | +20.500 (+11.6 %) ✗ |
| head CPU docker-init mean % | 0.100 | 0.000 | -0.100 (-100.0 %) ✓ |
| head CPU vllm mean % | 6.700 | 7.600 | +0.900 (+13.4 %) ✗ |
| head RoCE roceP2p1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| head RoCE roceP2p1s0f1 tx GB | 2.386 | 2.371 | -0.015 (-0.6 %) ✗ |
| head RoCE rocep1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| head RoCE rocep1s0f1 tx GB | 3.565 | 3.483 | -0.082 (-2.3 %) ✗ |
| worker GPU util mean % | 72.8 | 63.8 | -9.000 (-12.4 %) ✗ |
| worker GPU util max % | 92.0 | 88.0 | -4.000 (-4.3 %) ✗ |
| worker GPU power mean W | 26.3 | 27.3 | +1.000 (+3.8 %) ✗ |
| worker swap-out pages | 0 | 0 | +0 |
| worker Xid (run) | 0 | 0 | +0 |
| worker restarts | 0 | 0 | +0 |
| worker fault samples | 0 | 0 | +0 |
| worker CPU VLLM::Worker_TP mean % | 103.2 | 115.4 | +12.200 (+11.8 %) ✗ |
| worker CPU docker-init mean % | 0.000 | 0.100 | +0.100 ✗ |
| worker RoCE roceP2p1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| worker RoCE roceP2p1s0f1 tx GB | 2.244 | 2.161 | -0.083 (-3.7 %) ✗ |
| worker RoCE rocep1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| worker RoCE rocep1s0f1 tx GB | 3.398 | 3.243 | -0.155 (-4.6 %) ✗ |

## c10_short

| metric | A | B | delta |
|---|---|---|---|
| requests ok | 200 | 200 | +0 (+0.0 %) |
| requests failed | 0 | 0 | +0 |
| aggregate completion tok/s | 205.6 | 213.2 | +7.520 (+3.7 %) ✓ |
| aggregate prompt tok/s | 1243.7 | 1401.0 | +157.300 (+12.6 %) ✓ |
| TTFT p50 s | 0.195 | 0.152 | -0.042 (-21.7 %) ✓ |
| TTFT p95 s | 0.604 | 0.317 | -0.288 (-47.6 %) ✓ |
| total p50 s | 1.713 | 1.547 | -0.166 (-9.7 %) ✓ |
| total p95 s | 4.739 | 4.514 | -0.225 (-4.7 %) ✓ |
| decode tok/s p50 | 25.4 | 26.6 | +1.110 (+4.4 %) ✓ |
| inter-token gap p95 s | 0.102 | 0.107 | +0.005 (+4.7 %) ✗ |
| inter-token gap max s | 0.362 | 2.800 | +2.438 (+673.6 %) ✗ |
| head GPU util mean % | 83.4 | 79.4 | -4.000 (-4.8 %) ✗ |
| head GPU util max % | 95.0 | 94.0 | -1.000 (-1.1 %) ✗ |
| head GPU power mean W | 31.9 | 31.8 | -0.100 (-0.3 %) ✓ |
| head swap-out pages | 0 | 0 | +0 |
| head Xid (run) | 0 | 0 | +0 |
| head restarts | 0 | 0 | +0 |
| head fault samples | 0 | 0 | +0 |
| head CPU VLLM::EngineCor mean % | 98.1 | 87.8 | -10.300 (-10.5 %) ✓ |
| head CPU VLLM::Worker_TP mean % | 168.0 | 165.8 | -2.200 (-1.3 %) ✓ |
| head CPU vllm mean % | 8.000 | 9.600 | +1.600 (+20.0 %) ✗ |
| head RoCE roceP2p1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| head RoCE roceP2p1s0f1 tx GB | 15.8 | 15.3 | -0.463 (-2.9 %) ✗ |
| head RoCE rocep1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| head RoCE rocep1s0f1 tx GB | 15.9 | 15.4 | -0.486 (-3.1 %) ✗ |
| worker GPU util mean % | 92.9 | 64.3 | -28.600 (-30.8 %) ✗ |
| worker GPU util max % | 95.0 | 95.0 | +0.000 (+0.0 %) |
| worker GPU power mean W | 33.4 | 31.4 | -2.000 (-6.0 %) ✓ |
| worker swap-out pages | 3374 | 0 | -3374 (-100.0 %) ✓ |
| worker Xid (run) | 0 | 0 | +0 |
| worker restarts | 0 | 0 | +0 |
| worker fault samples | 0 | 0 | +0 |
| worker CPU VLLM::Worker_TP mean % | 118.9 | 113.2 | -5.700 (-4.8 %) ✓ |
| worker RoCE roceP2p1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| worker RoCE roceP2p1s0f1 tx GB | 15.8 | 15.3 | -0.464 (-2.9 %) ✗ |
| worker RoCE rocep1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| worker RoCE rocep1s0f1 tx GB | 15.9 | 15.4 | -0.485 (-3.1 %) ✗ |

## c16_short

| metric | A | B | delta |
|---|---|---|---|
| requests ok | 320 | 320 | +0 (+0.0 %) |
| requests failed | 0 | 0 | +0 |
| aggregate completion tok/s | 269.6 | 306.7 | +37.060 (+13.7 %) ✓ |
| aggregate prompt tok/s | 1587.3 | 1835.7 | +248.400 (+15.6 %) ✓ |
| TTFT p50 s | 0.226 | 0.190 | -0.037 (-16.2 %) ✓ |
| TTFT p95 s | 0.662 | 0.366 | -0.296 (-44.7 %) ✓ |
| total p50 s | 2.369 | 2.033 | -0.336 (-14.2 %) ✓ |
| total p95 s | 5.900 | 5.509 | -0.391 (-6.6 %) ✓ |
| decode tok/s p50 | 19.7 | 21.6 | +1.910 (+9.7 %) ✓ |
| inter-token gap p95 s | 0.112 | 0.112 | -0.001 (-0.7 %) ✓ |
| inter-token gap max s | 0.540 | 0.159 | -0.382 (-70.6 %) ✓ |
| head GPU util mean % | 83.9 | 93.1 | +9.200 (+11.0 %) ✓ |
| head GPU util max % | 95.0 | 95.0 | +0.000 (+0.0 %) |
| head GPU power mean W | 34.5 | 36.8 | +2.300 (+6.7 %) ✗ |
| head swap-out pages | 0 | 0 | +0 |
| head Xid (run) | 0 | 0 | +0 |
| head restarts | 0 | 0 | +0 |
| head fault samples | 0 | 0 | +0 |
| head CPU VLLM::EngineCor mean % | 96.5 | 96.7 | +0.200 (+0.2 %) ✗ |
| head CPU VLLM::Worker_TP mean % | 164.1 | 172.6 | +8.500 (+5.2 %) ✗ |
| head CPU vllm mean % | 9.300 | 14.1 | +4.800 (+51.6 %) ✗ |
| head RoCE roceP2p1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| head RoCE roceP2p1s0f1 tx GB | 22.8 | 22.7 | -0.097 (-0.4 %) ✗ |
| head RoCE rocep1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| head RoCE rocep1s0f1 tx GB | 22.9 | 22.8 | -0.120 (-0.5 %) ✗ |
| worker GPU util mean % | 63.3 | 90.6 | +27.300 (+43.1 %) ✓ |
| worker GPU util max % | 95.0 | 95.0 | +0.000 (+0.0 %) |
| worker GPU power mean W | 34.5 | 36.3 | +1.800 (+5.2 %) ✗ |
| worker swap-out pages | 10164 | 0 | -10164 (-100.0 %) ✓ |
| worker Xid (run) | 0 | 0 | +0 |
| worker restarts | 0 | 0 | +0 |
| worker fault samples | 0 | 0 | +0 |
| worker CPU VLLM::Worker_TP mean % | 124.5 | 119.5 | -5.000 (-4.0 %) ✓ |
| worker RoCE roceP2p1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| worker RoCE roceP2p1s0f1 tx GB | 22.8 | 22.7 | -0.097 (-0.4 %) ✗ |
| worker RoCE rocep1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| worker RoCE rocep1s0f1 tx GB | 22.9 | 22.8 | -0.120 (-0.5 %) ✗ |

## prefill_32k

| metric | A | B | delta |
|---|---|---|---|
| requests ok | 5 | 5 | +0 (+0.0 %) |
| requests failed | 0 | 0 | +0 |
| aggregate completion tok/s | 3.490 | 3.800 | +0.310 (+8.9 %) ✓ |
| aggregate prompt tok/s | 7143.2 | 7787.8 | +644.600 (+9.0 %) ✓ |
| TTFT p50 s | 4.185 | 3.790 | -0.395 (-9.4 %) ✓ |
| TTFT p95 s | 4.377 | 3.826 | -0.552 (-12.6 %) ✓ |
| total p50 s | 4.218 | 3.932 | -0.286 (-6.8 %) ✓ |
| total p95 s | 4.379 | 3.962 | -0.417 (-9.5 %) ✓ |
| decode tok/s p50 | 231.6 | 113.1 | -118.480 (-51.2 %) ✗ |
| prefill tok/s p50 | 7827.9 | 8638.5 | +810.600 (+10.4 %) ✓ |
| inter-token gap p95 s | 0.010 | 0.011 | +0.001 (+6.8 %) ✗ |
| inter-token gap max s | 0.012 | 0.012 | -0.000 (-3.3 %) ✓ |
| head GPU util mean % | 76.8 | 76.8 | +0.000 (+0.0 %) |
| head GPU util max % | 96.0 | 96.0 | +0.000 (+0.0 %) |
| head GPU power mean W | 48.6 | 53.0 | +4.400 (+9.1 %) ✗ |
| head swap-out pages | 0 | 0 | +0 |
| head Xid (run) | 0 | 0 | +0 |
| head restarts | 0 | 0 | +0 |
| head fault samples | 0 | 0 | +0 |
| head CPU VLLM::EngineCor mean % | 76.2 | 82.0 | +5.800 (+7.6 %) ✗ |
| head CPU VLLM::Worker_TP mean % | 173.8 | 183.2 | +9.400 (+5.4 %) ✗ |
| head CPU vllm mean % | 5.900 | 5.700 | -0.200 (-3.4 %) ✓ |
| head RoCE roceP2p1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| head RoCE roceP2p1s0f1 tx GB | 28.9 | 28.9 | +0.014 (+0.0 %) ✓ |
| head RoCE rocep1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| head RoCE rocep1s0f1 tx GB | 29.0 | 29.0 | +0.016 (+0.1 %) ✓ |
| worker GPU util mean % | 76.2 | 76.8 | +0.600 (+0.8 %) ✓ |
| worker GPU util max % | 96.0 | 96.0 | +0.000 (+0.0 %) |
| worker GPU power mean W | 48.0 | 53.8 | +5.800 (+12.1 %) ✗ |
| worker swap-out pages | 14709 | 0 | -14709 (-100.0 %) ✓ |
| worker Xid (run) | 0 | 0 | +0 |
| worker restarts | 0 | 0 | +0 |
| worker fault samples | 0 | 0 | +0 |
| worker CPU VLLM::Worker_TP mean % | 159.3 | 157.5 | -1.800 (-1.1 %) ✓ |
| worker RoCE roceP2p1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| worker RoCE roceP2p1s0f1 tx GB | 28.9 | 28.9 | +0.015 (+0.1 %) ✓ |
| worker RoCE rocep1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| worker RoCE rocep1s0f1 tx GB | 29.0 | 29.0 | +0.017 (+0.1 %) ✓ |

## prefill_128k

| metric | A | B | delta |
|---|---|---|---|
| requests ok | 3 | 3 | +0 (+0.0 %) |
| requests failed | 0 | 0 | +0 |
| aggregate completion tok/s | 0.580 | 0.620 | +0.040 (+6.9 %) ✓ |
| aggregate prompt tok/s | 4762.1 | 5057.1 | +295.000 (+6.2 %) ✓ |
| TTFT p50 s | 26.3 | 25.0 | -1.357 (-5.2 %) ✓ |
| TTFT p95 s | 26.7 | 25.1 | -1.589 (-5.9 %) ✓ |
| total p50 s | 26.4 | 25.1 | -1.302 (-4.9 %) ✓ |
| total p95 s | 26.8 | 25.3 | -1.532 (-5.7 %) ✓ |
| decode tok/s p50 | 176.0 | 113.4 | -62.630 (-35.6 %) ✗ |
| prefill tok/s p50 | 4976.1 | 5243.9 | +267.800 (+5.4 %) ✓ |
| inter-token gap p95 s | 0.014 | 0.013 | -0.000 (-2.9 %) ✓ |
| inter-token gap max s | 0.014 | 0.016 | +0.002 (+14.4 %) ✗ |
| head GPU util mean % | 89.6 | 89.1 | -0.500 (-0.6 %) ✗ |
| head GPU util max % | 96.0 | 96.0 | +0.000 (+0.0 %) |
| head GPU power mean W | 66.6 | 69.4 | +2.800 (+4.2 %) ✗ |
| head swap-out pages | 0 | 0 | +0 |
| head Xid (run) | 0 | 0 | +0 |
| head restarts | 0 | 0 | +0 |
| head fault samples | 0 | 0 | +0 |
| head CPU VLLM::EngineCor mean % | 84.2 | 84.5 | +0.300 (+0.4 %) ✗ |
| head CPU VLLM::Worker_TP mean % | 186.3 | 192.8 | +6.500 (+3.5 %) ✗ |
| head CPU vllm mean % | 4.200 | 3.800 | -0.400 (-9.5 %) ✓ |
| head RoCE roceP2p1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| head RoCE roceP2p1s0f1 tx GB | 69.3 | 69.3 | +0.024 (+0.0 %) ✓ |
| head RoCE rocep1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| head RoCE rocep1s0f1 tx GB | 69.3 | 69.4 | +0.024 (+0.0 %) ✓ |
| worker GPU util mean % | 89.5 | 95.7 | +6.200 (+6.9 %) ✓ |
| worker GPU util max % | 96.0 | 96.0 | +0.000 (+0.0 %) |
| worker GPU power mean W | 69.2 | 71.8 | +2.600 (+3.8 %) ✗ |
| worker swap-out pages | 22622 | 0 | -22622 (-100.0 %) ✓ |
| worker Xid (run) | 0 | 0 | +0 |
| worker restarts | 0 | 0 | +0 |
| worker fault samples | 0 | 0 | +0 |
| worker CPU VLLM::Worker_TP mean % | 178.4 | 186.5 | +8.100 (+4.5 %) ✗ |
| worker RoCE roceP2p1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| worker RoCE roceP2p1s0f1 tx GB | 69.3 | 69.3 | +0.021 (+0.0 %) ✓ |
| worker RoCE rocep1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| worker RoCE rocep1s0f1 tx GB | 69.3 | 69.4 | +0.021 (+0.0 %) ✓ |

## needle_950k

| metric | A | B | delta |
|---|---|---|---|
| requests ok | — | 1 | — |
| requests failed | — | 0 | — |
| aggregate completion tok/s | — | 0.050 | — |
| aggregate prompt tok/s | — | 1181.9 | — |
| total p50 s | — | 799.2 | — |
| total p95 s | — | 799.2 | — |
| prefill tok/s p50 | — | 1188.6 | — |
| inter-token gap max s | — | 0 | — |
| correctness | — | 1.000 | — |
| head GPU util mean % | — | 95.3 | — |
| head GPU util max % | — | 96.0 | — |
| head GPU power mean W | — | 82.3 | — |
| head swap-out pages | — | 0 | — |
| head Xid (run) | — | 0 | — |
| head restarts | — | 0 | — |
| head fault samples | — | 0 | — |
| head CPU VLLM::EngineCor mean % | — | 27.5 | — |
| head CPU VLLM::Worker_TP mean % | — | 198.0 | — |
| head CPU vllm mean % | — | 1.500 | — |
| head RoCE roceP2p1s0f0 tx GB | — | 0.000 | — |
| head RoCE roceP2p1s0f1 tx GB | — | 167.4 | — |
| head RoCE rocep1s0f0 tx GB | — | 0.000 | — |
| head RoCE rocep1s0f1 tx GB | — | 167.5 | — |
| worker GPU util mean % | — | 95.3 | — |
| worker GPU util max % | — | 96.0 | — |
| worker GPU power mean W | — | 85.2 | — |
| worker swap-out pages | — | 0 | — |
| worker Xid (run) | — | 0 | — |
| worker restarts | — | 0 | — |
| worker fault samples | — | 0 | — |
| worker CPU VLLM::Worker_TP mean % | — | 198.3 | — |
| worker RoCE roceP2p1s0f0 tx GB | — | 0.000 | — |
| worker RoCE roceP2p1s0f1 tx GB | — | 167.5 | — |
| worker RoCE rocep1s0f0 tx GB | — | 0.000 | — |
| worker RoCE rocep1s0f1 tx GB | — | 167.5 | — |

## decode_burst

| metric | A | B | delta |
|---|---|---|---|
| requests ok | 16 | 16 | +0 (+0.0 %) |
| requests failed | 0 | 0 | +0 |
| aggregate completion tok/s | 555.3 | 538.4 | -16.940 (-3.1 %) ✗ |
| aggregate prompt tok/s | 65.2 | 63.2 | -2.000 (-3.1 %) ✗ |
| TTFT p50 s | 0.209 | 0.203 | -0.006 (-2.8 %) ✓ |
| TTFT p95 s | 0.211 | 0.204 | -0.006 (-3.0 %) ✓ |
| total p50 s | 6.591 | 6.598 | +0.007 (+0.1 %) ✗ |
| total p95 s | 6.592 | 6.599 | +0.007 (+0.1 %) ✗ |
| decode tok/s p50 | 40.1 | 40.0 | -0.080 (-0.2 %) ✗ |
| inter-token gap p95 s | 0.029 | 0.028 | -0.001 (-4.2 %) ✓ |
| inter-token gap max s | 0.127 | 0.127 | +0.000 (+0.2 %) ✗ |
| head GPU util mean % | 95.0 | 95.3 | +0.300 (+0.3 %) ✓ |
| head GPU util max % | 95.0 | 96.0 | +1.000 (+1.1 %) ✓ |
| head GPU power mean W | 34.7 | 27.6 | -7.100 (-20.5 %) ✓ |
| head swap-out pages | 0 | 0 | +0 |
| head Xid (run) | 0 | 0 | +0 |
| head restarts | 0 | 0 | +0 |
| head fault samples | 0 | 0 | +0 |
| head CPU VLLM::EngineCor mean % | 89.9 | 56.0 | -33.900 (-37.7 %) ✓ |
| head CPU VLLM::Worker_TP mean % | 191.3 | 153.8 | -37.500 (-19.6 %) ✓ |
| head CPU vllm mean % | 9.900 | 11.1 | +1.200 (+12.1 %) ✗ |
| head RoCE roceP2p1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| head RoCE roceP2p1s0f1 tx GB | 2.172 | 2.176 | +0.004 (+0.2 %) ✓ |
| head RoCE rocep1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| head RoCE rocep1s0f1 tx GB | 2.186 | 2.189 | +0.003 (+0.1 %) ✓ |
| worker GPU util mean % | 47.0 | 31.7 | -15.300 (-32.6 %) ✗ |
| worker GPU util max % | 94.0 | 95.0 | +1.000 (+1.1 %) ✓ |
| worker GPU power mean W | 29.6 | 28.2 | -1.400 (-4.7 %) ✓ |
| worker swap-out pages | 0 | 0 | +0 |
| worker Xid (run) | 0 | 0 | +0 |
| worker restarts | 0 | 0 | +0 |
| worker fault samples | 0 | 0 | +0 |
| worker CPU VLLM::Worker_TP mean % | 109.5 | 83.7 | -25.800 (-23.6 %) ✓ |
| worker RoCE roceP2p1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| worker RoCE roceP2p1s0f1 tx GB | 2.172 | 2.176 | +0.004 (+0.2 %) ✓ |
| worker RoCE rocep1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| worker RoCE rocep1s0f1 tx GB | 2.186 | 2.189 | +0.003 (+0.1 %) ✓ |

## mixed

| metric | A | B | delta |
|---|---|---|---|
| requests ok | 550 | 692 | +142 (+25.8 %) ✓ |
| requests failed | 0 | 0 | +0 |
| aggregate completion tok/s | 160.4 | 200.6 | +40.100 (+25.0 %) ✓ |
| aggregate prompt tok/s | 3843.6 | 4761.8 | +918.200 (+23.9 %) ✓ |
| TTFT p50 s | 0.561 | 0.256 | -0.306 (-54.5 %) ✓ |
| TTFT p95 s | 2.950 | 2.686 | -0.264 (-8.9 %) ✓ |
| total p50 s | 9.357 | 7.534 | -1.823 (-19.5 %) ✓ |
| total p95 s | 24.0 | 18.7 | -5.322 (-22.2 %) ✓ |
| decode tok/s p50 | 18.5 | 23.9 | +5.380 (+29.1 %) ✓ |
| inter-token gap p95 s | 0.137 | 0.093 | -0.043 (-31.6 %) ✓ |
| inter-token gap max s | 4.797 | 3.188 | -1.609 (-33.5 %) ✓ |
| head GPU util mean % | 89.6 | 94.3 | +4.700 (+5.2 %) ✓ |
| head GPU util max % | 96.0 | 96.0 | +0.000 (+0.0 %) |
| head GPU power mean W | 41.8 | 44.1 | +2.300 (+5.5 %) ✗ |
| head swap-out pages | 0 | 0 | +0 |
| head Xid (run) | 0 | 0 | +0 |
| head restarts | 0 | 0 | +0 |
| head fault samples | 0 | 0 | +0 |
| head CPU VLLM::EngineCor mean % | 99.5 | 99.5 | +0.000 (+0.0 %) |
| head CPU VLLM::Worker_TP mean % | 189.8 | 199.8 | +10.000 (+5.3 %) ✗ |
| head CPU vllm mean % | 5.300 | 8.200 | +2.900 (+54.7 %) ✗ |
| head RoCE roceP2p1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| head RoCE roceP2p1s0f1 tx GB | 478.4 | 593.4 | +114.965 (+24.0 %) ✓ |
| head RoCE rocep1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| head RoCE rocep1s0f1 tx GB | 482.6 | 598.5 | +115.882 (+24.0 %) ✓ |
| worker GPU util mean % | 87.9 | 93.2 | +5.300 (+6.0 %) ✓ |
| worker GPU util max % | 96.0 | 96.0 | +0.000 (+0.0 %) |
| worker GPU power mean W | 42.8 | 46.5 | +3.700 (+8.6 %) ✗ |
| worker swap-out pages | 0 | 0 | +0 |
| worker Xid (run) | 0 | 0 | +0 |
| worker restarts | 0 | 0 | +0 |
| worker fault samples | 0 | 0 | +0 |
| worker CPU VLLM::Worker_TP mean % | 145.3 | 146.9 | +1.600 (+1.1 %) ✗ |
| worker RoCE roceP2p1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| worker RoCE roceP2p1s0f1 tx GB | 478.5 | 593.4 | +114.956 (+24.0 %) ✓ |
| worker RoCE rocep1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| worker RoCE rocep1s0f1 tx GB | 482.7 | 598.6 | +115.875 (+24.0 %) ✓ |

## cancellations

| metric | A | B | delta |
|---|---|---|---|
| requests ok | 21 | 21 | +0 (+0.0 %) |
| requests failed | 0 | 0 | +0 |
| aggregate completion tok/s | 11.8 | 15.9 | +4.140 (+35.1 %) ✓ |
| aggregate prompt tok/s | 10.2 | 13.8 | +3.600 (+35.3 %) ✓ |
| TTFT p50 s | 0.289 | 0.155 | -0.133 (-46.2 %) ✓ |
| TTFT p95 s | 0.367 | 0.196 | -0.171 (-46.5 %) ✓ |
| total p50 s | 0.289 | 0.157 | -0.132 (-45.7 %) ✓ |
| total p95 s | 0.368 | 0.198 | -0.170 (-46.2 %) ✓ |
| inter-token gap max s | 0 | 0 | +0 |
| correctness | 1.000 | 1.000 | +0.000 (+0.0 %) |
| head GPU util mean % | 33.5 | 77.0 | +43.500 (+129.9 %) ✓ |
| head GPU util max % | 67.0 | 77.0 | +10.000 (+14.9 %) ✓ |
| head GPU power mean W | 18.9 | 23.6 | +4.700 (+24.9 %) ✗ |
| head swap-out pages | 0 | 0 | +0 |
| head Xid (run) | 0 | 0 | +0 |
| head restarts | 0 | 0 | +0 |
| head fault samples | 0 | 0 | +0 |
| head CPU VLLM::EngineCor mean % | 48.5 | 38.4 | -10.100 (-20.8 %) ✓ |
| head CPU VLLM::Worker_TP mean % | 109.8 | 123.8 | +14.000 (+12.8 %) ✗ |
| head CPU vllm mean % | 4.100 | 8.700 | +4.600 (+112.2 %) ✗ |
| head RoCE roceP2p1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| head RoCE roceP2p1s0f1 tx GB | 0.177 | 0.177 | +0.000 (+0.0 %) |
| head RoCE rocep1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| head RoCE rocep1s0f1 tx GB | 0.181 | 0.180 | -0.001 (-0.6 %) ✗ |
| worker GPU util mean % | 93.0 | 0.000 | -93.000 (-100.0 %) ✗ |
| worker GPU util max % | 93.0 | 0.000 | -93.000 (-100.0 %) ✗ |
| worker GPU power mean W | 22.1 | 21.4 | -0.700 (-3.2 %) ✓ |
| worker swap-out pages | 0 | 0 | +0 |
| worker Xid (run) | 0 | 0 | +0 |
| worker restarts | 0 | 0 | +0 |
| worker fault samples | 0 | 0 | +0 |
| worker CPU VLLM::Worker_TP mean % | 52.8 | 50.0 | -2.800 (-5.3 %) ✓ |
| worker RoCE roceP2p1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| worker RoCE roceP2p1s0f1 tx GB | 0.177 | 0.177 | +0.000 (+0.0 %) |
| worker RoCE rocep1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| worker RoCE rocep1s0f1 tx GB | 0.181 | 0.180 | -0.001 (-0.6 %) ✗ |

## json_structured

| metric | A | B | delta |
|---|---|---|---|
| requests ok | 30 | 30 | +0 (+0.0 %) |
| requests failed | 0 | 0 | +0 |
| aggregate completion tok/s | 216.8 | 214.4 | -2.390 (-1.1 %) ✗ |
| aggregate prompt tok/s | 151.3 | 150.4 | -0.900 (-0.6 %) ✗ |
| total p50 s | 1.226 | 1.198 | -0.028 (-2.3 %) ✓ |
| total p95 s | 1.554 | 1.803 | +0.249 (+16.0 %) ✗ |
| inter-token gap max s | 0 | 0 | +0 |
| correctness | 1.000 | 1.000 | +0.000 (+0.0 %) |
| head GPU util mean % | 91.5 | 93.0 | +1.500 (+1.6 %) ✓ |
| head GPU util max % | 94.0 | 94.0 | +0.000 (+0.0 %) |
| head GPU power mean W | 29.9 | 30.7 | +0.800 (+2.7 %) ✗ |
| head swap-out pages | 0 | 0 | +0 |
| head Xid (run) | 0 | 0 | +0 |
| head restarts | 0 | 0 | +0 |
| head fault samples | 0 | 0 | +0 |
| head CPU VLLM::EngineCor mean % | 94.0 | 91.6 | -2.400 (-2.6 %) ✓ |
| head CPU VLLM::Worker_TP mean % | 182.6 | 178.2 | -4.400 (-2.4 %) ✓ |
| head CPU vllm mean % | 4.300 | 4.700 | +0.400 (+9.3 %) ✗ |
| head RoCE roceP2p1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| head RoCE roceP2p1s0f1 tx GB | 1.564 | 1.552 | -0.012 (-0.8 %) ✗ |
| head RoCE rocep1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| head RoCE rocep1s0f1 tx GB | 1.680 | 1.681 | +0.001 (+0.1 %) ✓ |
| worker GPU util mean % | 68.0 | 90.7 | +22.700 (+33.4 %) ✓ |
| worker GPU util max % | 94.0 | 94.0 | +0.000 (+0.0 %) |
| worker GPU power mean W | 26.5 | 29.2 | +2.700 (+10.2 %) ✗ |
| worker swap-out pages | 0 | 0 | +0 |
| worker Xid (run) | 0 | 0 | +0 |
| worker restarts | 0 | 0 | +0 |
| worker fault samples | 0 | 0 | +0 |
| worker CPU VLLM::Worker_TP mean % | 119.8 | 99.0 | -20.800 (-17.4 %) ✓ |
| worker RoCE roceP2p1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| worker RoCE roceP2p1s0f1 tx GB | 1.564 | 1.552 | -0.012 (-0.8 %) ✗ |
| worker RoCE rocep1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| worker RoCE rocep1s0f1 tx GB | 1.681 | 1.681 | +0.000 (+0.0 %) |

## streaming

| metric | A | B | delta |
|---|---|---|---|
| requests ok | 20 | 20 | +0 (+0.0 %) |
| requests failed | 0 | 0 | +0 |
| aggregate completion tok/s | 234.3 | 234.8 | +0.540 (+0.2 %) ✓ |
| aggregate prompt tok/s | 286.4 | 287.6 | +1.200 (+0.4 %) ✓ |
| TTFT p50 s | 0.460 | 0.199 | -0.262 (-56.8 %) ✓ |
| TTFT p95 s | 0.470 | 0.239 | -0.231 (-49.2 %) ✓ |
| total p50 s | 3.260 | 3.101 | -0.159 (-4.9 %) ✓ |
| total p95 s | 3.277 | 3.144 | -0.133 (-4.1 %) ✓ |
| decode tok/s p50 | 71.2 | 69.1 | -2.110 (-3.0 %) ✗ |
| inter-token gap p95 s | 0.016 | 0.016 | -0.000 (-1.9 %) ✓ |
| inter-token gap max s | 0.252 | 0.127 | -0.125 (-49.7 %) ✓ |
| head GPU util mean % | 70.5 | 94.0 | +23.500 (+33.3 %) ✓ |
| head GPU util max % | 94.0 | 95.0 | +1.000 (+1.1 %) ✓ |
| head GPU power mean W | 26.6 | 28.6 | +2.000 (+7.5 %) ✗ |
| head swap-out pages | 0 | 0 | +0 |
| head Xid (run) | 0 | 0 | +0 |
| head restarts | 0 | 0 | +0 |
| head fault samples | 0 | 0 | +0 |
| head CPU VLLM::EngineCor mean % | 79.0 | 72.2 | -6.800 (-8.6 %) ✓ |
| head CPU VLLM::Worker_TP mean % | 174.8 | 172.3 | -2.500 (-1.4 %) ✓ |
| head CPU vllm mean % | 5.500 | 7.200 | +1.700 (+30.9 %) ✗ |
| head RoCE roceP2p1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| head RoCE roceP2p1s0f1 tx GB | 3.034 | 3.084 | +0.050 (+1.6 %) ✓ |
| head RoCE rocep1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| head RoCE rocep1s0f1 tx GB | 3.058 | 3.114 | +0.056 (+1.8 %) ✓ |
| worker GPU util mean % | 65.8 | 69.8 | +4.000 (+6.1 %) ✓ |
| worker GPU util max % | 93.0 | 94.0 | +1.000 (+1.1 %) ✓ |
| worker GPU power mean W | 26.9 | 25.5 | -1.400 (-5.2 %) ✓ |
| worker swap-out pages | 0 | 0 | +0 |
| worker Xid (run) | 0 | 0 | +0 |
| worker restarts | 0 | 0 | +0 |
| worker fault samples | 0 | 0 | +0 |
| worker CPU VLLM::Worker_TP mean % | 122.7 | 90.3 | -32.400 (-26.4 %) ✓ |
| worker RoCE roceP2p1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| worker RoCE roceP2p1s0f1 tx GB | 3.034 | 3.084 | +0.050 (+1.6 %) ✓ |
| worker RoCE rocep1s0f0 tx GB | 0.000 | 0.000 | +0.000 |
| worker RoCE rocep1s0f1 tx GB | 3.058 | 3.114 | +0.056 (+1.8 %) ✓ |

[ab] written /tmp/claude-1000/-home-techsphere-Documents-project-personal-LLM-Chabot/067399a7-df7b-4c10-a124-b42d49e5990d/scratchpad/ab/B-20260912T0859Z/compare-A-20260912T0514Z-vs-B-20260912T0859Z.md
