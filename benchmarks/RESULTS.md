# Generated results

**Do not edit.** Produced by `python benchmarks/report.py --write` from the raw JSON in
`results/`. Every figure the docs quote is rendered from here, and the generator refuses to
run when two configurations in a comparison group processed different input.

## `kev` — kev-0.8b-cpu-dtype.json

- Source: `benchmarks/results/kev-0.8b-cpu-dtype.json`
- device `cpu` · dtype `fp32 / bf16` · processes 1, within-request batching
- Host: aarch64 · 24 vCPU · 33.5 GB RAM · **no GPU** · torch 2.14.0+cpu · transformers 5.17.0

| Questions | Threads | dtype | input sha256 | tokens | n | p50 ms | p95 ms | min | max | ms/question |
|---:|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
| 3 | 16 | fp32 | — (not recorded) | 101 | 1 | 1,656.1 | — | — | — | 552.0 |
| 3 | 16 | bf16 | — (not recorded) | 101 | 1 | 137,173.7 | — | — | — | 45,724.6 |

Caveats recorded with the data:

- two separate server processes (one per dtype), OMP_NUM_THREADS=16, KEV_DTYPE set explicitly. One warm-up request, then two measured requests; the figure below is kev's own server-side latency_ms from the final response, which excludes tokenization and HTTP overhead. Transcribed by hand from the response body: this is a single observation per dtype, not an aggregate, so it is recorded verbatim rather than summarised.
- bf16 is ~83x slower than fp32 on CPU with a <=0.01 probability delta and identical argmax. Both packages that would provide optimised kernels require Triton/CUDA and cannot be installed on CPU-only Linux. kev should therefore be treated as a GPU-first engine; the CPU image trades one order of magnitude of latency for functionality.

## `laya` — laya-english-sweep.json

- Source: `benchmarks/results/laya-english-sweep.json`
- device `cpu` · dtype `float32` · cold start 76.3 s · peak RSS 2.8 GB · processes 1, within-request batching
- Host: aarch64 · 24 vCPU · 33.5 GB RAM · **no GPU** · torch 2.14.0+cpu · laya 0.3.5

| Questions | Threads | dtype | input sha256 | tokens | n | p50 ms | p95 ms | min | max | ms/question |
|---:|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | float32 | `95d64a1ebe9b` | 96 | 6 | 871.6 | 874.0 | 815.0 | 906.9 | 871.6 |
| 1 | 2 | float32 | `95d64a1ebe9b` | 96 | 6 | 744.2 | 750.6 | 739.2 | 782.4 | 744.2 |
| 1 | 4 | float32 | `95d64a1ebe9b` | 96 | 6 | 615.0 | 623.7 | 602.5 | 630.1 | 615.0 |
| 1 | 8 | float32 | `95d64a1ebe9b` | 96 | 6 | 527.1 | 530.6 | 511.0 | 537.2 | 527.1 |
| 1 | 16 | float32 | `95d64a1ebe9b` | 96 | 6 | 432.3 | 447.5 | 423.5 | 454.5 | 432.3 |
| 1 | 24 | float32 | `95d64a1ebe9b` | 96 | 6 | 445.8 | 472.1 | 409.3 | 485.3 | 445.8 |
| 3 | 1 | float32 | `4b0494ad5acf` | 264 | 6 | 1,776.5 | 1,910.7 | 1,767.6 | 1,960.7 | 592.2 |
| 3 | 2 | float32 | `4b0494ad5acf` | 264 | 6 | 1,486.7 | 1,499.3 | 1,433.6 | 1,499.9 | 495.6 |
| 3 | 4 | float32 | `4b0494ad5acf` | 264 | 6 | 1,219.3 | 1,234.7 | 1,182.2 | 1,289.7 | 406.4 |
| 3 | 8 | float32 | `4b0494ad5acf` | 264 | 6 | 1,041.2 | 1,057.0 | 1,027.4 | 1,092.3 | 347.1 |
| 3 | 16 | float32 | `4b0494ad5acf` | 264 | 6 | 918.5 | 1,003.9 | 881.8 | 1,008.2 | 306.1 |
| 3 | 24 | float32 | `4b0494ad5acf` | 264 | 6 | 821.9 | 851.5 | 779.3 | 858.7 | 274.0 |
| 10 | 1 | float32 | `820e1a141f30` | 888 | 6 | 5,870.9 | 5,891.9 | 5,646.4 | 5,941.2 | 587.1 |
| 10 | 2 | float32 | `820e1a141f30` | 888 | 6 | 4,649.0 | 4,724.9 | 4,054.5 | 4,733.8 | 464.9 |
| 10 | 4 | float32 | `820e1a141f30` | 888 | 6 | 3,999.0 | 4,026.5 | 3,926.7 | 4,031.6 | 399.9 |
| 10 | 8 | float32 | `820e1a141f30` | 888 | 6 | 3,506.9 | 3,517.8 | 3,385.7 | 3,601.2 | 350.7 |
| 10 | 16 | float32 | `820e1a141f30` | 888 | 6 | 3,082.3 | 3,131.1 | 3,027.9 | 3,158.4 | 308.2 |
| 10 | 24 | float32 | `820e1a141f30` | 888 | 6 | 3,221.5 | 3,731.4 | 2,727.1 | 3,894.9 | 322.1 |
| 30 | 1 | float32 | `6a66de73d907` | 2640 | 6 | 13,635.7 | 13,673.1 | 13,270.0 | 13,897.1 | 454.5 |
| 30 | 2 | float32 | `6a66de73d907` | 2640 | 6 | 11,339.5 | 11,362.4 | 11,290.6 | 11,437.1 | 378.0 |
| 30 | 4 | float32 | `6a66de73d907` | 2640 | 6 | 9,424.7 | 9,518.8 | 9,282.5 | 9,604.4 | 314.2 |
| 30 | 8 | float32 | `6a66de73d907` | 2640 | 6 | 8,370.0 | 8,401.3 | 8,275.5 | 8,739.4 | 279.0 |
| 30 | 16 | float32 | `6a66de73d907` | 2640 | 6 | 7,042.4 | 7,077.9 | 6,943.1 | 7,191.9 | 234.8 |
| 30 | 24 | float32 | `6a66de73d907` | 2640 | 6 | 7,032.2 | 7,236.9 | 6,928.0 | 7,549.5 | 234.4 |

Caveats recorded with the data:

- Configurations were run in ascending thread order (1,2,4,8,16,24), not randomised. Thermal drift or co-tenant load is therefore confounded with thread count; see docs/design-review.md M2.
- n = 6 iterations per configuration, reported as median. Values support order-of-magnitude conclusions, not fine-grained comparison (e.g. '16 threads is 13% better than 24').
- single_caller_questions_per_second is 1000/(p50_ms/n_questions): the rate one serial caller achieves. It is NOT throughput and does not account for concurrency or cross-request batching.
- These measure the engine, not the Decis server. Cross-request batching is not exercised here.

## `laya` — laya-multilingual-sweep.json

- Source: `benchmarks/results/laya-multilingual-sweep.json`
- device `cpu` · dtype `float32` · cold start 72.9 s · peak RSS 4.84 GB · processes 1, within-request batching
- Host: aarch64 · 24 vCPU · 33.5 GB RAM · **no GPU** · torch 2.14.0+cpu · laya 0.3.5

| Questions | Threads | dtype | input sha256 | tokens | n | p50 ms | p95 ms | min | max | ms/question |
|---:|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | float32 | `95d64a1ebe9b` | 93 | 6 | 315.9 | 317.4 | 307.4 | 317.8 | 315.9 |
| 1 | 2 | float32 | `95d64a1ebe9b` | 93 | 6 | 354.5 | 356.2 | 346.6 | 360.0 | 354.5 |
| 1 | 4 | float32 | `95d64a1ebe9b` | 93 | 6 | 299.0 | 300.1 | 294.1 | 305.6 | 299.0 |
| 1 | 8 | float32 | `95d64a1ebe9b` | 93 | 6 | 255.7 | 263.3 | 246.9 | 269.4 | 255.7 |
| 1 | 16 | float32 | `95d64a1ebe9b` | 93 | 6 | 200.8 | 207.4 | 193.3 | 216.4 | 200.8 |
| 1 | 24 | float32 | `95d64a1ebe9b` | 93 | 6 | 231.4 | 297.0 | 203.2 | 371.3 | 231.4 |
| 3 | 1 | float32 | `4b0494ad5acf` | 264 | 6 | 627.5 | 629.2 | 618.0 | 633.2 | 209.2 |
| 3 | 2 | float32 | `4b0494ad5acf` | 264 | 6 | 671.4 | 674.7 | 656.8 | 677.9 | 223.8 |
| 3 | 4 | float32 | `4b0494ad5acf` | 264 | 6 | 543.6 | 563.9 | 523.5 | 569.4 | 181.2 |
| 3 | 8 | float32 | `4b0494ad5acf` | 264 | 6 | 465.7 | 505.4 | 446.0 | 561.5 | 155.2 |
| 3 | 16 | float32 | `4b0494ad5acf` | 264 | 6 | 392.0 | 433.6 | 370.0 | 456.0 | 130.7 |
| 3 | 24 | float32 | `4b0494ad5acf` | 264 | 6 | 387.3 | 412.2 | 341.9 | 430.4 | 129.1 |
| 10 | 1 | float32 | `820e1a141f30` | 885 | 6 | 2,253.5 | 2,263.8 | 2,215.5 | 2,343.9 | 225.3 |
| 10 | 2 | float32 | `820e1a141f30` | 885 | 6 | 2,138.9 | 2,155.1 | 2,115.3 | 2,193.8 | 213.9 |
| 10 | 4 | float32 | `820e1a141f30` | 885 | 6 | 1,789.9 | 1,795.3 | 1,678.5 | 1,807.2 | 179.0 |
| 10 | 8 | float32 | `820e1a141f30` | 885 | 6 | 1,326.7 | 1,415.9 | 1,261.3 | 1,442.2 | 132.7 |
| 10 | 16 | float32 | `820e1a141f30` | 885 | 6 | 1,115.4 | 1,154.7 | 1,017.5 | 1,177.5 | 111.5 |
| 10 | 24 | float32 | `820e1a141f30` | 885 | 6 | 986.8 | 1,055.6 | 921.7 | 1,126.3 | 98.7 |
| 30 | 1 | float32 | `6a66de73d907` | 2640 | 6 | 7,390.6 | 7,420.0 | 7,377.9 | 7,424.5 | 246.3 |
| 30 | 2 | float32 | `6a66de73d907` | 2640 | 6 | 5,660.9 | 5,825.2 | 5,529.4 | 5,850.9 | 188.7 |
| 30 | 4 | float32 | `6a66de73d907` | 2640 | 6 | 4,449.2 | 4,547.4 | 4,270.0 | 5,877.5 | 148.3 |
| 30 | 8 | float32 | `6a66de73d907` | 2640 | 6 | 3,983.4 | 4,003.1 | 3,756.9 | 4,145.4 | 132.8 |
| 30 | 16 | float32 | `6a66de73d907` | 2640 | 6 | 3,659.6 | 3,772.6 | 3,371.7 | 3,798.1 | 122.0 |
| 30 | 24 | float32 | `6a66de73d907` | 2640 | 6 | 3,426.1 | 3,512.9 | 3,190.3 | 3,666.5 | 114.2 |

Caveats recorded with the data:

- Configurations were run in ascending thread order (1,2,4,8,16,24), not randomised. Thermal drift or co-tenant load is therefore confounded with thread count; see docs/design-review.md M2.
- n = 6 iterations per configuration, reported as median. Values support order-of-magnitude conclusions, not fine-grained comparison (e.g. '16 threads is 13% better than 24').
- single_caller_questions_per_second is 1000/(p50_ms/n_questions): the rate one serial caller achieves. It is NOT throughput and does not account for concurrency or cross-request batching.
- These measure the engine, not the Decis server. Cross-request batching is not exercised here.

## Provenance gaps

Things this generator could **not** verify. Listed rather than assumed:

- kev-0.8b-cpu-dtype.json (3 question(s)): no input hash recorded, so identical input could only be checked via token counts

## Cross-request batching

<!-- BATCHING:START -->
Measured cross-request batching. Raw JSON lives in `benchmarks/results/`; this
table is produced by `python benchmarks/report.py --write`. **Do not edit.**

The positive control (identical items, padding 1.00) reaches **1.92x** (`shared_short`, batch 8), which confirms the bottleneck is per-call fixed overhead and that this harness can detect a gain. For the shape real traffic has -- different states -- the best case is 1.09x (`uniform`, batch 8) and the worst is 0.34x (`skewed`, batch 8). When lengths are skewed, padding reaches 2.47x (`skewed`, batch 16): every row is padded to the longest in its batch, and that wasted compute shows up directly as a higher per-item cost.

`shared_short` and `shared` are the positive controls: every item is identical, so padding is exactly 1.00 and the only variable left is the sequence length. The short one has to show a gain, or this harness cannot detect one and nothing else here is trustworthy. It reproduces the request-internal result already published: 217.8 -> 113.7 ms/item here, 231 -> 98.7 ms/question in `laya-multilingual-sweep.json`.

### Process count vs throughput

The total thread budget is held constant (processes x threads = 24), so this compares sharing the machine between processes with giving all of it to one, not thread oversubscription.

| processes | threads | items/s | ms/item | vs best | source |
|---:|---:|---:|---:|---:|---|
| 1 | 24 | 1.23 | 814.1 | 0.93x | `laya-multilingual-batch-gain.json` |
| 2 | 12 | 1.12 | 890.4 | 0.85x | `laya-multilingual-multiprocess-p2.json` |
| 4 | 6 | 1.32 | 759.0 | 1.00x | `laya-multilingual-multiprocess-p4.json` |

The slowest and fastest differ by only 1.18x: on this host **adding processes does not add throughput**. A single Laya forward pass already uses 24 threads well, so splitting the budget only reallocates the same capacity.

The 1-process row comes from the serial configuration inside the batching file (`laya-multilingual-batch-gain.json`) -- the same item set and the same thread budget, not a separate experiment.

### `laya-multilingual` — 1 process(es) x 24 threads

- `benchmarks/results/laya-multilingual-batch-gain.json` · cpu · float32
- load 82.0 s · peak RSS 4.83 GB

| item set | batch | n | ms/item (p50) | vs serial | break-even wait ms | padding |
|---|---:|---:|---:|---:|---:|---:|
| shared_short (control) | 1 | 64 | 217.8 | 1.00x | 0.0 | 1.00 |
| shared_short (control) | 2 | 32 | 152.4 | 1.43x | 65.5 | 1.00 |
| shared_short (control) | 4 | 16 | 127.8 | 1.71x | 90.0 | 1.00 |
| shared_short (control) | 8 | 8 | 113.7 | 1.92x | 104.1 | 1.00 |
| shared_short (control) | 16 | 4 | 117.1 | 1.86x | 100.8 | 1.00 |
| shared (control) | 1 | 64 | 464.9 | 1.00x | 0.0 | 1.00 |
| shared (control) | 2 | 32 | 422.7 | 1.10x | 42.2 | 1.00 |
| shared (control) | 4 | 16 | 696.6 | 0.67x | -231.7 | 1.00 |
| shared (control) | 8 | 8 | 500.4 | 0.93x | -35.5 | 1.00 |
| shared (control) | 16 | 4 | 374.0 | 1.24x | 90.9 | 1.00 |
| uniform | 1 | 64 | 814.1 | 1.00x | 0.0 | 1.00 |
| uniform | 2 | 32 | 769.6 | 1.06x | 44.5 | 1.06 |
| uniform | 4 | 16 | 1535.9 | 0.53x | -721.9 | 1.10 |
| uniform | 8 | 8 | 748.8 | 1.09x | 65.2 | 1.10 |
| uniform | 16 | 4 | 885.2 | 0.92x | -71.2 | 1.13 |
| skewed | 1 | 64 | 322.1 | 1.00x | 0.0 | 1.00 |
| skewed | 2 | 32 | 1514.6 | 0.21x | -1192.6 | 1.37 |
| skewed | 4 | 16 | 1176.8 | 0.27x | -854.7 | 2.38 |
| skewed | 8 | 8 | 932.5 | 0.34x | -610.4 | 2.41 |
| skewed | 16 | 4 | 933.6 | 0.34x | -611.5 | 2.47 |

sequence tokens (state + one question): shared_short 115-115, shared 393-393, uniform 530-674, skewed 140-821

- not measured: queue wait, cancellation and partial-failure behaviour -- they need the batcher
- not measured: cross-state prefix sharing, which a real engine could exploit and this cannot
- not measured: p99 under sustained load

### `laya-multilingual` — 2 process(es) x 12 threads

- `benchmarks/results/laya-multilingual-multiprocess-p2.json` · cpu · float32
- load 104.1 s · peak RSS 4.82 GB

| processes | threads | items | wall s | ms/item | items/s |
|---:|---:|---:|---:|---:|---:|
| 2 | 12 | 128 | 113.97 | 890.4 | 1.12 |

- not measured: whether the OS scheduler actually keeps them on separate cores
- not measured: p99 under this load

### `laya-multilingual` — 4 process(es) x 6 threads

- `benchmarks/results/laya-multilingual-multiprocess-p4.json` · cpu · float32
- load 86.1 s · peak RSS 4.81 GB

| processes | threads | items | wall s | ms/item | items/s |
|---:|---:|---:|---:|---:|---:|
| 4 | 6 | 256 | 194.29 | 759.0 | 1.32 |

- not measured: whether the OS scheduler actually keeps them on separate cores
- not measured: p99 under this load

**Scope**: batches are synthesised by calling the engine directly, with no queue, no waiting, no client giving up and no partial failure. These figures are an **upper bound** on a real batcher; every mechanism listed above makes it worse.

<!-- BATCHING:END -->

