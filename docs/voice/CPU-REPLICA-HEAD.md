# The CPU speech replica on the head

A second CPU copy of `openai/whisper-large-v3` (whisper.cpp q8_0, the same image, model file and
contract as the worker's copy in [CPU-REPLICA.md](CPU-REPLICA.md)), on four cores of the head
Spark (spark-0e68). Like the worker's copy, it takes a clip only when every GPU replica is already
decoding one.

**Status, 2026-10-02:** the code is ready (`WHISPER_CPU_NODE=head scripts/whisper-cpu.sh`) and the
copy is **not deployed**. The owner's chat gate **has been measured** on the model now serving,
`Qwen/Qwen3.6-35B-A3B-NVFP4`: eight threads FAILED it and four threads PASSED it (see
"The gate, measured" below). The defaults are the
4-thread cap: `WHISPER_CPU_HEAD_CPUSET=16-19`, `WHISPER_CPU_HEAD_CPUS=4`,
`WHISPER_CPU_HEAD_THREADS=4`.

---

## The exception, and its condition

The head's memory is off limits for new services (owner, 2026-09-16). On 2026-09-30, at about 11:45
IST, the owner answered "Yes, head too": a CPU copy of whisper may also run on the head, so that
speech uses both GPUs and both CPUs. The condition given with that answer:

> Measure chat decode with it busy first. If chat drops more than 5 %, cap its cores or turn it off.

Nothing else about the head changes. Every other new or scaled service still goes on the worker.

## Where it runs

| | head copy | worker copy |
|---|---|---|
| cores | `16-19` (four Cortex-X925), `cpus: 4`, 4 threads (the cap that passed the chat gate) | `5-9,15-19` (ten X925), `cpus: 8`, 8 threads |
| bind | `172.17.0.1:30008` (Docker bridge gateway), host networking | management address `:30008`, host networking |
| image, model | loaded from the worker: same image ID, same q8_0 SHA-256 | built and converted on the worker |
| memory | peak 2.010 GiB measured on the head, 4 GiB cap, no swap | 2.0 GiB, 4 GiB cap |
| OOM score | 850. The head runs no OCR engine, so this copy is the first speech service the kernel kills | 850, after OCR (900) |
| in `ASR_CPU_BASE_URLS` | **last** | first |

- **The cores.** Per-core use on the head, 2026-09-30:
  - In a 60 s sample at 17:27 (chat serving, nothing else heavy), the A725 cores 0-4 and 10-14 ran at
    5-7 % and the X925 cores 5-9 and 15-19 at 10-57 %. The scheduler keeps the busy threads, rank
    0's among them, on the X925 cores while they have room.
  - During a chat decode (3 s per-thread sample, 11:53), the chat model's rank-0 hot threads were on
    the X925 cores: Worker_TP0 at 100 % on cpu 6, EngineCore at 68 % on cpu 5, a Worker_TP0 thread
    at 66 % on cpu 18.
  - No production container is pinned, so the scheduler moves those threads: at 17:35 they ran on
    cpus 4, 10 and 11.

  The default, `16-19` with four threads, leaves cpus 5-15 and the whole A725 cluster free. It is
  the configuration that passed the chat gate. The first one tried, eight threads on `7-9,15-19`,
  failed it on time to first token. Four threads decode about 1.8 times slower than eight, about
  0.41 s per second of audio; see Known limits for what that means for the router's estimate.
- **The bind.** The head's GPU replica listens on `172.17.0.1:30007`. The orchestrator reaches the
  gateway from its compose bridge, and the office LAN does not. The head's host guard does not judge
  30007 or 30008 (`scripts/host-guard.sh explain 30008 enP7s7 192.168.9.20 --role head` says
  accept), so the bind is the boundary, and `scripts/whisper-cpu.sh` refuses any head bind outside
  172.16.0.0/12. It needs no guard change. See Known limits for what the guard leaves open.
- **Nothing is compiled or converted on the head.** A compile runs four jobs for several minutes, and
  a conversion loads 3 GB of fp16 weights. `up` copies the worker's image (`docker save | docker load`,
  refused unless the image ID matches) and the worker's q8_0 file (refused unless it matches the
  SHA-256 pin, and removed if the copy is interrupted). So the worker's copy is started first.
- **The memory floor.** `up` refuses when the head has under 20 GiB of MemAvailable
  (`WHISPER_CPU_HEAD_MIN_AVAILABLE_GIB`). The head's memory has changed since the exception was given:

  | when | MemAvailable | swap in use |
  |---|---:|---:|
  | 11:45 (the exception) | 49 GiB | 34 GiB |
  | 17:26-18:05 (after the switch to the 27B) | 24.5-29.6 GiB | 22-23 GiB |

- **Last in `ASR_CPU_BASE_URLS`.** The router offers an overflow clip to the CPU copies in list
  order, and the head's cores also run the orchestrator, Postgres and rank 0's engine loop. `up` and
  `down` merge the list and never replace it, so one node's command leaves the other node's entry.
  The list with both copies:
  `ASR_CPU_BASE_URLS=http://192.168.9.68:30008/v1,http://172.17.0.1:30008/v1`
- **The same server, so the same refusals.** The head's copy runs the worker's image, so the
  round-1 fixes in [CPU-REPLICA.md](CPU-REPLICA.md) apply to it unchanged: a clip that arrives
  while it decodes gets a 503 "busy" at once and goes to the GPU queue, and a decoder restart that
  never says ready is killed at the hang budget's fixed part and counted toward the restart.
  The legacy dictation pool adds no permit for either copy; it lends each free copy its one slot
  only for the clip the router sends there (`test_with_two_cpu_copies_the_pool_lends_each_its_one_slot_and_adds_no_permit`).

## Measured on the head (2026-09-30, 12:02-12:10)

Throwaway `whisper-cpu-head-test:a08a1066`: `--cpuset-cpus 7-9,15-19 --cpus 8`, 8 threads,
`--memory 4g --memory-swap 4g --oom-score-adj 900`, bound to 127.0.0.1:30208. Removed the same day.

**Accuracy: identical to the worker's copy.** The head produced the same transcript as the worker's
CPU copy for every one of the 20 utterances, byte for byte.

| set | utterances / words | WER head CPU | WER worker CPU | WER GPU replica | same text as worker CPU | language agrees | max \|ΔP(nospeech)\| |
|---|---:|---:|---:|---:|---:|---:|---:|
| LibriSpeech test-clean | 10 / 310 | 2.26 % | 2.26 % | 2.90 % | 10/10 | 10/10 | 5e-05 |
| FLEURS-hi | 10 / 265 | 50.57 % | 50.57 % | 43.77 % | 10/10 | 10/10 | 4e-05 |

The FLEURS-hi gap to the GPU is the CPU copy's known Hindi gap, the same on both nodes. Two clips
were detected as Urdu by both CPU copies and by the GPU replica. CPU-REPLICA.md has the paired sets;
the worker track's whisper.cpp token-cap change addresses the gap.

**Speed.** Pooled RTF 0.427 on LibriSpeech (120.1 s of audio in 51.3 s) and 0.581 on FLEURS-hi (123.2 s
in 71.5 s). The worker's copy took 50.5 s and 70.6 s on the same clips, so the head was 1.6 % and 1.3 %
slower. Per clip, 3.9-11.6 s for 2.5-31 s of audio.

**Memory.** cgroup `memory.peak` was 2.010 GiB (anon 1.990 GiB) over 436 s of back-to-back decoding.

**Chat cost, first measured 2026-09-30 morning against `Qwen/Qwen3.6-35B-A3B-NVFP4`** (MoE), with
3 probes that ran alone per phase (18 attempts), median decode was:

| before | during | after |
|---:|---:|---:|
| 102.3 tok/s | 101.7 tok/s | 100.3 tok/s |

Three probes per phase decide nothing; the paired gate below does.

## The gate, measured on Qwen/Qwen3.6-35B-A3B-NVFP4

The main model is `Qwen/Qwen3.6-35B-A3B-NVFP4` again (since 22:38 IST on 2026-09-30). The gate was
run on it with the copy on the head decoding back to back against it idle, and judged against two
limits: chat decode may drop by no more than 5 %, and time to first token (TTFT) may rise by no
more than 10 %.

| head copy | decode cost | TTFT rise | verdict |
|---|---:|---:|---|
| 8 threads, `7-9,15-19`, `cpus: 8` | +1.28 % | **+10.1 %** | **FAIL** (TTFT over the 10 % limit) |
| 4 threads, `16-19`, `cpus: 4` | +0.07 % | +2.5 % | **PASS** |

So the head copy runs capped at four threads, and those are the defaults in `scripts/whisper-cpu.sh`
and `.env.example`. Decode barely moved at either size; at eight threads the cost showed in TTFT.

**The worker's copy on the same model** costs chat about 0-5 % of decode speed while it decodes,
inside the noise of the measurement (earlier: −3.6 %, 95 % CI −11.6 % to +5.3 %, 26 pairs;
[CPU-REPLICA.md](CPU-REPLICA.md)).

**The 4-thread copy's speed:** about 0.41 s of decoding per second of audio, about 1.8 times slower
than the 8-thread copy on the same clips.

## The gate

`scripts/whisper-cpu-chat-gate.py` streams 300 tokens (thinking off, one request at a time) with the
replica idle ("off") and decoding back to back ("on"). The probes run in interleaved pairs whose
order alternates, so drift in everything else lands on both arms. It counts only probes that ran
alone on the engine: the token counters grew by exactly the probe's own tokens, and at most one
request was running.

- **Verdict:** the median paired drop, with a bootstrap 95 % CI. PASS is a drop of 5 % or less.
- **Exit status:** 0 PASS, 1 FAIL, 2 not measured.
- **Safety:** it stops the load when MemAvailable falls under 20 GiB.
- **What it touches:** it starts and stops nothing.

**Preconditions:**
- Nothing else on the engine.
- No throwaway speech or benchmark containers on either node (`docker ps` on both).
- MemAvailable of at least 22 GiB on the head.
- The owner's go-ahead for the run.

**Commands, on the head.** The image and model already exist there from the 12:02 run.

```bash
M=/path/to/dir/with/ggml-large-v3-q8_0.bin   # sha256 37efc6b68f300ab717465685f7c3e175a66c11cf92bb3ab9912e86f4116c465e
C=/path/to/clips                            # .wav/.flac, e.g. the 20 LibriSpeech + FLEURS-hi clips above
docker run -d --name whisper-cpu-head-test --network host \
  -e WHISPER_BIND=127.0.0.1 -e WHISPER_PORT=30208 -e WHISPER_CPU_THREADS=8 \
  -e WHISPER_CPU_MODEL_FILE=/models/ggml-large-v3-q8_0.bin -e OMP_WAIT_POLICY=PASSIVE \
  -e WHISPER_MAX_AUDIO_SECONDS=600 \
  --cpuset-cpus 7-9,15-19 --cpus 8 --memory 4g --memory-swap 4g --oom-score-adj 900 \
  --read-only --tmpfs /tmp:size=128m,mode=1777 -v "$M":/models:ro \
  --user 10008:10008 --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 256 \
  whisper-cpu-head-test:a08a1066
until curl -fsS http://127.0.0.1:30208/health | grep -q '"ready":true'; do sleep 2; done
python3 scripts/whisper-cpu-chat-gate.py --replica http://127.0.0.1:30208 --clips "$C" \
  --pairs 12 --model Qwen/Qwen3.6-35B-A3B-NVFP4 --out gate-c8.jsonl
docker rm -f whisper-cpu-head-test
```

**If that FAILs,** rerun the same `docker run` with `--cpuset-cpus 16-19 --cpus 4` and
`-e WHISPER_CPU_THREADS=4`, then write the gate to `--out gate-c4.jsonl`.

`scripts/whisper-cpu-chat-gate.py` judges decode only; it records each probe's `ttft_ms`, and the
TTFT limit of 10 % was applied to those medians.

**Decision (taken):** FAIL at 8, PASS at 4, so the copy deploys capped at four threads, and that cap
is now the default. Had both failed, the head would get no copy and the worker's would be
unaffected.

## Deploy (after a PASS)

Run this from the deploy root, on `main` with this branch merged. The worker's copy goes first,
because the head's image and model come from it.

```bash
cd /home/techsphere/Documents/project/personal-LLM-Chabot
scripts/whisper-cpu.sh up                          # worker: build, convert, start, list first
WHISPER_CPU_NODE=head scripts/whisper-cpu.sh up    # head: load, copy, start on 16-19 (4 threads), list last
WHISPER_CPU_NODE=head scripts/whisper-cpu.sh verify
./techsara up                                      # orchestrator recreate; the main model is not restarted
```

The defaults are the 4-thread cap. A different placement goes in `.env` before the head's `up`,
and the next `up` keeps it; any change needs the gate run again first.

```
WHISPER_CPU_HEAD_CPUSET=16-19
WHISPER_CPU_HEAD_CPUS=4
WHISPER_CPU_HEAD_THREADS=4
```

To turn it off: `WHISPER_CPU_NODE=head scripts/whisper-cpu.sh down`, then `./techsara up`. The
worker's entry stays in `ASR_CPU_BASE_URLS`.

## Known limits

- **One speed estimate for every CPU copy.** The router has no per-copy estimate:
  `ASR_CPU_FIXED_S` (8.5) and `ASR_CPU_S_PER_AUDIO_S` (0.45) apply to every URL in
  `ASR_CPU_BASE_URLS`, and they were measured with eight X925 threads. The head's 4-thread copy
  measured **about 0.41 s per second of audio, about 1.8 times slower than the 8-thread copy** on
  the same clips. On the slowest long-form material (Hindi-English, where eight threads needed up
  to 0.45 s/s), 1.8 times slower is about 0.8 s/s, past the shared estimate and past what the 1.5
  margin absorbs. Session windows (120 s timeout) and ASR_TIMEOUT_S (600 s) are generous, so the
  failure mode is a late transcript, not a lost one. The router has no setting for the head copy
  alone, so its value is recorded here: **0.41 s per audio second** at four threads. A per-copy
  estimate, or re-timing the 4-thread copy on the long-form sets, is open work before the head copy
  is listed.
- **The head's guard leaves 30007 and 30008 unjudged.** Both engines bind 172.17.0.1, which the LAN
  has no route to. Linux accepts a packet for any local address on any interface, though, so a LAN
  host that adds a route to 172.17.0.0/16 via 192.168.9.54 would reach both unauthenticated engines.
  To close it, add `30007-30008` to `GUARD_HEAD_PORTS` in `scripts/host-guard.sh`. The head ruleset
  already accepts lo, docker0 and the compose bridges, and drops the LAN and tailnet. It needs
  `sudo scripts/host-guard.sh apply --role head` (root) and a consumer-map entry in
  `launcher/tests/test_host_guard.py`.
- **The head's copy depends on the worker at `up` only.** Once started, it runs without the worker.
