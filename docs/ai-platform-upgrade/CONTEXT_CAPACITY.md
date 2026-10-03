# Context capacity

NOT STARTED as a verified programme deliverable (Phase A task A-05, Phase E). The numbers below were recorded before this programme and are not yet re-verified here.

| Item | Prior record | Source | Verified in this programme |
|---|---|---|---|
| Served maximum model length of the main model | 1,000,000 (YaRN factor 3.82) | `GET /v1/models` on the main engine, 2026-10-03 (observed); README and CHANGELOG | Served value observed; capability not yet verified |
| Needle recall at 949,915 tokens | 3 of 3 needles, 878 s, with `CLUSTER_KV_CACHE_MEMORY_GIB=8` | CHANGELOG, memory note `context-window-memory-ceiling` | No |
| KV bytes per token (35B-A3B, TP=2, fp8 KV) | 5,120 | `launcher/techsara_cli/modelshape.py` | No |
| KV pool | 1,663,201 tokens with prefix caching off | README | No |
| Probe script | `orchestrator/scripts/validate_long_context.py` (defaults to the production engine; sizes 65,536–240,000 unless `--sizes`) | repository | — |

The ~250K symptom (§18.3) and the ~1M path (§18.4) are investigated in Phases C and E with the budget law P + G + M ≤ W. Production inference changes are prepared and tested on dev resources and handed to the operator (operator decision 3).
