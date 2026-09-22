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

