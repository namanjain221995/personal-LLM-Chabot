# OCR on the worker

Running the document-OCR engine (`baidu/Unlimited-OCR`) on Spark 2 instead
of Spark 1, and telling the launcher about it. Three commands; the same
engine, the same image digest, the same flags; a hand-over with no gap.

---

## Why

The head's own OCR service (`vllm-ocr` in `compose/compose.dgx-spark.yaml`)
sizes its KV cache as 0.10-0.14 of the head's 121 GB of unified memory, so it
reserves about **17 GB** for a 3.3B model that transcribes single 8,192-token
pages, a handful at a time. Measured 2026-09-09:

| node | unified memory | in use | GPU |
|------|---------------:|-------:|-----|
| head (Spark 1) | 121 GB | **113 GB** | main model rank 0, router, embedder, reranker, OCR |
| worker (Spark 2) | 121 GB | 52 GB (69 free) | main model rank 1, whisper; idle between TP steps |

Everything else on the head is either the main model or something that must
sit next to the orchestrator. OCR is the one engine that is both large and
indifferent to where it runs: the orchestrator already talks to it over
HTTP, one page per request. So it is the one that moves.

On the worker it runs with an **explicit 3 GiB KV budget** and
`--max-num-seqs 8` under a `--gpu-memory-utilization 0.10` ceiling: about
12 GB all in, of which 7.5 GB is weights. The head's copy reserved 17 GB for
the same work.

---

## The three commands

```
scripts/ocr.sh up          # 1. start the engine on the worker
./techsara up              # 2. repoint the orchestrator, retire the head's engine
scripts/ocr.sh verify      # 3. OCR a real image through the new engine
```

**1. `scripts/ocr.sh up`** picks the node (the worker in dual mode, this
node otherwise; `OCR_NODE=head|worker` overrides), then:

- checks the worker has the pinned vLLM image; if not, `docker pull` there,
  and if Docker Hub is unreachable from that box (its DNS is flaky), streams
  it from the head with `docker save | ssh docker load` -- ~20 GB, several
  minutes, and it says so;
- checks the worker has the weights in `<cache>/repos/baidu--Unlimited-OCR--07dea832e22a/`;
  if not, `rsync`s the 6.4 GB directory from the head's cache to the same
  path on the worker (resumable);
- copies `compose/compose.ocr.yaml` to the worker's `~/.techsara-cluster/`
  and starts it there as its own Compose project, `sf-local-ai-ocr`, bound to
  the worker's **management** address (`enP7s7`, 192.168.9.68 today -- never
  a 10.100.x RoCE address, which is the main model's fabric);
- waits up to ten minutes for `/v1/models`;
- writes `OCR_REMOTE_BASE_URL=http://192.168.9.68:30004/v1` into `.env`.

Nothing has changed for the orchestrator yet. Both engines are serving.

**2. `./techsara up`** is a routine up (the main model is not restarted).
Because `.env` now carries `OCR_REMOTE_BASE_URL`, the launcher:

- generates `OCR_BASE_URL` as that address, with `OCR_ENABLED`, `OCR_MODEL`
  and every `OCR_*` capability value exactly as before -- OCR is on, just
  elsewhere;
- leaves the head's `ocr` Compose profile **off**, so `vllm-ocr` is not
  started, and its reconcile step **stops** the copy that was still running
  from before -- which is the moment the 17 GB comes back;
- probes the remote engine from inside the orchestrator image, over the
  same path the orchestrator will use, and recreates the orchestrator with
  the new address.

If the probe fails, OCR is disabled for that run (`OCR_ENABLED=false`) and
nothing on the head is started in its place; fix the engine
(`scripts/ocr.sh status`, `scripts/ocr.sh logs`) and run `./techsara up`
again.

**3. `scripts/ocr.sh verify`** sends a real image (a rendered
"TechSara OCR check 2026", inlined in the script) as the exact request the
orchestrator sends -- `image_url` first, then the model card's
`document parsing` prompt -- and prints what the engine read. `status` shows
the container and `/v1/models`; `url` prints the endpoint.

---

## Going back

```
scripts/ocr.sh down        # stop and remove the worker engine (weights stay); clears OCR_REMOTE_BASE_URL
./techsara up              # start the head's vllm-ocr again and repoint the orchestrator
```

`down` (or `stop`) sets `OCR_REMOTE_BASE_URL=` (empty) in `.env` at once,
because leaving a dead address recorded is worse than never having started:
the orchestrator would send every page there, get nothing, and quietly go
pixels-only. Between `down` and the next `./techsara up` that is exactly the
state you are in, so run them together.

---

## What the orchestrator's `OCR_BASE_URL` resolves to

Three layers decide it. `.env` is the first `--env-file`, `generated.env`
the last (later wins for interpolation and for `env_file`), and the
orchestrator's `environment:` block in `compose.yaml` names the key as
`${OCR_REMOTE_BASE_URL:-${OCR_BASE_URL:-http://disabled.invalid/v1}}` --
`environment:` overrides `env_file`, so the remote address wins over the
generated head address whenever it is set.

| state | `.env` `OCR_REMOTE_BASE_URL` | `generated.env` `OCR_BASE_URL` | orchestrator container | head `vllm-ocr` |
|---|---|---|---|---|
| never moved | unset | `http://vllm-ocr:30004/v1` | `http://vllm-ocr:30004/v1` | running |
| after `scripts/ocr.sh up`, before `./techsara up` | `http://192.168.9.68:30004/v1` | still `http://vllm-ocr:30004/v1` (stale) | unchanged until recreated; a recreate already gets the remote address | running (both serve) |
| after `./techsara up` | `http://192.168.9.68:30004/v1` | `http://192.168.9.68:30004/v1` | `http://192.168.9.68:30004/v1` | stopped |
| remote probe failed during `./techsara up` | `http://192.168.9.68:30004/v1` | `http://disabled.invalid/v1`, `OCR_ENABLED=false` | the remote address, but `OCR_ENABLED=false` governs: OCR off | stopped |
| after `scripts/ocr.sh down`, before `./techsara up` | empty | `http://192.168.9.68:30004/v1` (stale, dead) | the dead address: pages go pixels-only | stopped |
| after the next `./techsara up` | empty | `http://vllm-ocr:30004/v1` | `http://vllm-ocr:30004/v1` | running |

A malformed `OCR_REMOTE_BASE_URL` (no `http(s)://`, whitespace, embedded
credentials, over 512 characters) makes `./techsara up` refuse by name before
anything is generated, rather than fall back to starting the head's engine
on the node you were emptying.

---

## Single node

With no second Spark (`TECHSARA_CLUSTER_MODE=single`), `scripts/ocr.sh up`
starts the same project on this node, bound to the Docker bridge gateway
(`172.17.0.1`), and records that address. There is no memory to win that way;
it exists so the same script and compose file work everywhere, and so a
single-node deployment can run OCR outside the launcher's lifecycle if it
ever needs to.

## What it never touches

`sf-local-ai-worker`, the main model's tensor-parallel rank 1 on the worker,
is a different Compose project and is never started, stopped or recreated by
any of this. Neither is `sf-local-ai-whisper`. `scripts/ocr.sh down` removes
one container and one project, and leaves the weights on disk.

## Monitoring follows the engine

Prometheus does not scrape OCR from a fixed address. `scripts/ocr.sh up`
and `down`, and `scripts/monitoring.sh up`, render
`.runtime/prometheus/ocr.json` (a file_sd target, gitignored) from the same
`OCR_REMOTE_BASE_URL` the orchestrator follows, and Prometheus re-reads it
within 30 seconds. The Service-health tile keeps its `service="ocr"` label;
the `node`/`role` labels say which Spark answers today. After a move the
retired address lingers as a stale series for up to five minutes, so the
tile can show both briefly.
