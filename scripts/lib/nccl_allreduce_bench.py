"""Two-node NCCL all-reduce benchmark (used by scripts/cluster-test.sh).

Runs inside the pinned vLLM image on both nodes with RANK/WORLD_SIZE/
MASTER_ADDR/MASTER_PORT set. Rank 0 prints one line starting with
NCCL_BENCH_RESULT followed by JSON. Data is validated (every all-reduce of a
tensor of ones must equal world_size), so a passing run proves that NCCL moved
real bytes between the two GB10s, not just that the collective returned.
"""
import datetime
import json
import os
import socket
import time

import torch
import torch.distributed as dist

rank = int(os.environ["RANK"])
world = int(os.environ["WORLD_SIZE"])
dist.init_process_group("nccl", timeout=datetime.timedelta(seconds=int(os.environ.get("NCCL_BENCH_TIMEOUT", "300"))))
torch.cuda.set_device(0)
dev = torch.device("cuda:0")


def timed(fn, iters):
    torch.cuda.synchronize()
    dist.barrier()
    t0 = time.perf_counter()
    for _ in range(iters):
        fn()
    torch.cuda.synchronize()
    return (time.perf_counter() - t0) / iters


res = {"host": socket.gethostname(), "rank": rank, "world_size": world,
       "nccl": list(torch.cuda.nccl.version()), "torch": torch.__version__,
       "gpu": torch.cuda.get_device_name(0), "results": [], "errors": []}

x = torch.ones(1, dtype=torch.float32, device=dev)
for _ in range(20):
    dist.all_reduce(x)
res["allreduce_4B_latency_us"] = round(timed(lambda: dist.all_reduce(x), 200) * 1e6, 1)

for mb in [0.0625, 0.5, 1, 4, 16, 64, 256, 512]:
    n = int(mb * 1024 * 1024 // 4)
    t = torch.ones(n, dtype=torch.float32, device=dev)
    for _ in range(3):
        dist.all_reduce(t)
    t.fill_(1.0)
    dist.all_reduce(t)
    if not bool(torch.all(t == float(world)).item()):
        res["errors"].append(f"data mismatch after all_reduce of {mb} MB")
    iters = 30 if mb <= 16 else 10
    dt = timed(lambda: dist.all_reduce(t), iters)
    algbw = (n * 4) / dt / 1e9
    busbw = algbw * 2 * (world - 1) / world
    res["results"].append({"size_MB": mb, "time_ms": round(dt * 1e3, 3),
                           "algbw_GBs": round(algbw, 3), "busbw_GBs": round(busbw, 3),
                           "busbw_Gbps": round(busbw * 8, 2)})

n = 64 * 1024 * 1024 // 4
t = torch.ones(n, dtype=torch.float32, device=dev)


def send_recv():
    if rank == 0:
        dist.send(t, 1)
    else:
        dist.recv(t, 0)


for _ in range(3):
    send_recv()
dt = timed(send_recv, 10)
res["p2p_64MB_Gbps"] = round((n * 4) / dt / 1e9 * 8, 2)
dist.barrier()
if rank == 0:
    print("NCCL_BENCH_RESULT " + json.dumps(res), flush=True)
dist.destroy_process_group()
