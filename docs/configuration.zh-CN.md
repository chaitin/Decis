# 配置

[English](configuration.md) · **简体中文**

[文档索引](../README.zh-CN.md#文档) · [快速开始](getting-started.zh-CN.md) · [部署](deployment.zh-CN.md)

Decis 从 `.env` 和环境变量读取配置。**shell 里已经设好的变量优先于 `.env`**，所以
`DECIS_PORT=9000 decis serve` 的行为和它看起来一样。`.env.example` 是带注释的模板；
`decis doctor` 会打印实际解析出的值，以及哪些变量被设过。

本页**服务端**的每个变量都只在一个模块里读取：
[`src/decis/config.py`](../src/decis/config.py)。包里没有其他模块读环境变量。playground 是
另一个进程，有它自己的一组变量；Compose 文件也有几个自己的变量——两者都列在本页末尾。

## 认证

| 变量 | 默认值 | 含义 |
|---|---|---|
| `DECIS_API_KEY` | 未设置 | 客户端必须发送的 Bearer token。可以逗号分隔多个，用于不停机轮换密钥。 |
| `DECIS_API_KEYS` | 未设置 | 同上，作为第二个名字；两者会拼接起来。 |
| `DECIS_ALLOW_NO_AUTH` | `0` | 在非回环地址上未配置 token 时仍允许服务。 |

认证刻意分成两侧。线上 TypeSafe API 是实测过的，不是假设的，它区分**缺失**凭证和**无效**凭证：

- 没有 `Authorization` 头，或 scheme 不是 `Bearer` → **403**。
- `Bearer` token 与任何已配置的密钥都不匹配 → **401**。

两者的 body 形状相同，而且认证在请求体校验**之前**执行：未认证的请求即使 body 非法也返回
403，而不是 422。理由是安全——反过来会把校验细节泄露给未认证的调用方。比较是常数时间的。

服务在不安全时会拒绝启动：绑定非回环地址却没配密钥是硬错误，不是警告，因为不安全的默认值
会被原样部署出去。

```bash
decis serve --host 0.0.0.0                 # .env 里有 DECIS_API_KEY -> 没问题
decis serve --host 0.0.0.0                 # 没有 key -> 拒绝启动
decis serve --host 127.0.0.1               # 没有 key，回环地址 -> 允许
```

## 网络

| 变量 | 默认值 | 含义 |
|---|---|---|
| `DECIS_HOST` | `0.0.0.0` | 绑定地址。`--host` 会覆盖它。 |
| `DECIS_PORT` | `8000` | 绑定端口。`--port` 会覆盖它。 |
| `DECIS_MAX_REQUEST_BYTES` | `2097152` (2 MiB) | 大于此大小的请求会以 **413** 拒绝。 |

`0.0.0.0` 是容器的正确默认值。`--host 127.0.0.1` 是首次本地运行的正确默认值。

### 启动顺序

引擎在后台线程里加载，所以加载期间探针照常应答，端口比模型更早可用：`/readyz` 报 `loading`，
`/v1/*` 被拒，日志把这两件事都说清楚。`--preload` 反转这个顺序——先取权重、先加载，再绑定端口：

```bash
decis serve --preload
```

此时加载期间什么都不应答，`/healthz` 也不例外，所以它在控制台前是合适的取舍，而在冷启动期间
需要存活探针拿到应答的地方是错的。[快速开始](getting-started.zh-CN.md#等待就绪)里有两种顺序
在日志里的样子。

## 引擎选择与权重

| 变量 | 默认值 | 含义 |
|---|---|---|
| `DECIS_DEFAULT_ENGINE` | `laya-multilingual` | `decis serve` 加载的引擎，也是回答 `model` 为“你的默认值”这类名字（如 `jev-latest`）的请求所用的引擎。`--engine` 会覆盖它。 |
| `DECIS_ACCEPT_FOREIGN_DEFAULTS` | `1` | 用已加载的引擎回答 `jev-latest`（官方 SDK 的默认值），而不是 422。正是它让只换 `base_url` 就够了。这个替换会记在 `decis.requested_model` 里。 |
| `DECIS_MODEL_DIR` | 未设置 | 预先下载好的权重目录。期望布局：`<DECIS_MODEL_DIR>/<engine-id>/`。 |
| `DECIS_HUB` | `auto` | 从哪个 Hub 取权重：`auto`、`huggingface` 或 `modelscope`。见[权重从哪个 Hub 来](#权重从哪个-hub-来)。`--hub` 在 `serve`、`models`、`doctor`、`download` 上覆盖它。 |

权重解析的顺序是固定的；第一个命中的生效，本地目录优先于网络：

1. 用 `--model-path` 为这个引擎指定的目录（见下）。
2. `DECIS_MODEL_DIR/<engine-id>/`。
3. Hub 缓存——Hugging Face，或 Hugging Face 不应答时的 ModelScope——必要时下载。

一个候选目录只有在是**完整** checkpoint 时才算数：引擎 `WeightSpec.marker` 指名的那个文件要
在，分片 checkpoint 还要满足 `*.safetensors.index.json` 里列出的每个文件都在。任何一条不满足
就当作"这里没有"，于是下到一半的目录会落到第 3 步，而不是先被拿去服务、再在加载时失败。

```bash
uv run decis download --engine laya-multilingual                  # 进它选中的那个源的缓存
uv run decis download --engine laya-multilingual --dest ./models  # 进 ./models/laya-multilingual/
DECIS_MODEL_DIR=./models uv run decis serve
```

### 权重从哪个 Hub 来

取权重时先试 Hugging Face，只有当它的 endpoint **完全**连不上时才退回
[ModelScope](https://modelscope.cn)：DNS 失败、连接被拒或超时。HTTP 错误状态不算"连不上"——
401 或 404 恰恰证明主机答了话——所以私有或需要鉴权的镜像不会被误判成"被墙"。这条退路是给
Hugging Face 不可达的网络准备的；能连上时行为完全不变。

这个决定覆盖一条命令发起的**每一次**下载，而不只是第一次：kev 的适配器**和**它背后的基座都走
同一个选择，而且都以目录的形式交给加载器。所以 `decis download --engine kev-0.8b --hub modelscope`
打印 `source modelscope` 时，适配器和基座都不会从 Hugging Face 来。

```bash
uv run decis doctor                      # 这台机器会走哪个源、为什么
uv run decis download --engine kev-0.8b  # 传输任何字节之前先打印源
```

| 变量 | 默认值 | 含义 |
|---|---|---|
| `DECIS_HUB` | `auto` | `auto`、`huggingface` 或 `modelscope`。`auto` 会探测并回退；写死一个值则既不探测也不替换，所以在 `auto` 会换源的情况下 `huggingface` 会直接失败。 |
| `HF_ENDPOINT` | `https://huggingface.co` | `huggingface_hub` 使用的 endpoint，**也是**探测时问的那个，于是内部镜像（或 `https://hf-mirror.com`）会被当作可达，而不是当作被墙的 Hugging Face。 |

**`auto` 只回答一个问题：endpoint 答不答话。** 它不测速度，也不会先试一次传输。所以"答得慢、
对大文件限速、或者只有模型页面通"的 Hugging Face 依然会被选中，下载要么很久、要么在客户端里
失败。`decis download` 会打印选了哪个源、为什么（`https://huggingface.co answered`）——要读的
就是这一行：**可达就意味着不会回退**。想强制换源用 `--hub modelscope` 或 `DECIS_HUB=modelscope`。

**ModelScope 上无法兑现 pin 住的 revision。** 它的 revision 是分支名和 tag 名，而这些仓库的镜像
只有 `master`。把 Hugging Face 的 commit sha 交给它并不会报错：它会打一行 `No files to download`
然后返回成功，留下一个空目录，于是 pin 住的 revision 变成了静默的空操作。所以 Decis 从不把 sha
发给它。这不是措辞上的差别：2026-10-06 实测，镜像上 `kev-0.8b` 的适配器和 pin 的那份**大小相同、
字节不同**（`adapter_model.safetensors` 在镜像上是 `9b908623…`，pin 是 `c81d5716…`），因为镜像是
跟着仓库的分支走的。**换源换的就是另一份下载**，不是“同一份从更近的机器上拿”。取而代之的是：`decis download` 在传输任何东西之前打印将要使用的源、原因，以及一条警告，
点名那个源兑现不了的 revision；`decis doctor` 报告在这台机器上取权重会发生什么。需要 pin 真正
生效的部署应该设 `DECIS_HUB=huggingface`，把这件事直接变成失败。

`uv sync --extra download` 会装上两个客户端；每个引擎 extra 都已经把它带了进来，缺哪个
`decis doctor` 会指出来。

```bash
uv run decis doctor                                        # 会探测一次 endpoint，有超时上限
uv run decis download --engine laya-multilingual           # 传输前先打印源
uv run decis download --engine kev-0.8b --hub modelscope   # 跳过探测，直接用 ModelScope
```

### 让单个引擎读你自己的目录

`--model-path` 为某个引擎指定一个 checkpoint 目录，对这个引擎而言优先于 `DECIS_MODEL_DIR`。
它可以重复，并且接受引擎 id 或它的任一别名：

```bash
uv run decis serve --engine kev-0.8b --model-path kev-0.8b=/srv/finetunes/acme-triage
uv run decis serve --model-path laya-multilingual=/srv/mine --model-path jeff=/srv/jeff-qwen
```

这里**故意没有 `DECIS_MODEL_PATH_<ENGINE_ID>` 变量**。那种形式必须把引擎 id 编进变量*名*里，
全大写并把 `-` 换成 `_`，而任何变量名都装不下 `kev-0.8b`、`jeff-qwen3.5-0.8b`、
`jeff-gemma4-e2b` 里的点号：`kev-0.8b` 对应的变量会规整成 id `kev-0-8b`，它没有注册任何东西，
于是这个覆盖被静默丢弃。把 id 放进*值*里就没有这个限制。`decis doctor` 会打印所有解析到的覆盖，
命令行上出现未知引擎 id 是配置错误，而不是什么都不做。

> **`kev-0.8b` 有一个注意点。** 你指过去的那个目录是**适配器**（LoRA 加 pointer head），也正是
> 你会去微调的部分；Qwen3.5 **基座**模型是第二个仓库，声明在 `WeightSpec.bases` 里。
> `decis download` 两个都会取，引擎两个也都会解析。给了 `--dest`（或 `DECIS_MODEL_DIR`）时基座
> 落在适配器旁边，即 `<dir>/Qwen3.5-0.8B-Base/`；两个都没给时它落在应答的那个 Hub 的缓存里，
> 之后从那里读回来。基座仓库来自适配器自己的元数据，而这份构建没有声明的基座会交给加载器，
> 不会被替换掉。两个 Jeff 引擎不一样：它们各自都是全权重微调，所以一个目录里就是全部，不会再
> 额外取别的东西。

## 计算

| 变量 | 默认值 | 含义 |
|---|---|---|
| `DECIS_DEVICE` | 自动 | `cpu`、`cuda`、`mps`、`xpu` 或 `npu`。不设则自动选最可用的。 |
| `DECIS_DTYPE` | 按引擎+设备 | 强制 `fp32`、`fp16` 或 `bf16`。读取它的是那些查 `registry.DTYPE_DEFAULTS` 的引擎——目前只有 `kev-0.8b`。Laya 由它自己的 `Agent` 决定精度，两个 Jeff checkpoint 由加载器内部决定（有加速器就 bf16，否则 fp32），所以这三个都忽略它。 |
| `DECIS_TORCH_THREADS` | 每个 vCPU 一个 | 该进程的线程数。 |

`DECIS_DEVICE` 不设时由 `src/decis/engines/devices.py` 决定：取 `torch` 报告可用的第一个加速器，
顺序是 `cuda`、`xpu`、`npu`、`mps`，都没有则是 `cpu`。`npu` 需要装 `torch_npu` 插件，其余四个是
`torch` 自带的。`cpu` 永远可用，所以没有加速器的机器就从 CPU 服务。要这个选择的是 `kev-0.8b`；
Laya 把它交给自己的 `Agent`。

这里实测过的只有 `cpu`、`cuda`、`mps`。`xpu` 与 `npu` 会被探测（也会被接受），这样你可以显式
指定它们，但 `benchmarks/results/` 里没有任何一次覆盖它们的运行，而且它们会落到 dtype 表的默认
值 `fp32`。

dtype 按引擎**和**设备分别选择，因为同一个选择在不同硬件上可能差一个数量级。`kev-0.8b`
在 CPU 上用 `bf16` 比 `fp32` 慢好几个数量级；实测表见
[性能](performance.zh-CN.md#每种引擎每种设备的-dtype)——每种 dtype 只有一次观测，所以那个倍数是
数量级结论，不是统计量。`DECIS_DTYPE` 主要用来在你自己的硬件上
重测，而且只有 `kev-0.8b` 会读它。设置一个已知性能很差的组合只会记一条警告，不会拒绝启动。

`DECIS_TORCH_THREADS` 默认每个 vCPU 一个线程。这是默认值，不是推荐值：在签入的扫描里，最佳
线程数取决于请求大小，所以对单个问题最快的设置对十个问题并不最快。给部署定规模前先测量；
原始数据在
[`benchmarks/results/laya-multilingual-sweep.json`](../benchmarks/results/laya-multilingual-sweep.json)，
表在[性能](performance.zh-CN.md#延迟)。

## 超时与下线

| 变量 | 默认值 | 含义 |
|---|---|---|
| `DECIS_REQUEST_TIMEOUT_MS` | `8000` | 一个请求最多等引擎多久，超过就以 **429** 拒绝。刻意小于官方 SDK 的 10 s HTTP 超时。 |
| `DECIS_SHUTDOWN_GRACE_MS` | `20000` | 下线时等待在途引擎加载的上限。要小于你的编排器的 `terminationGracePeriodSeconds`。 |

官方 SDK 会在超时后重试，所以一个活过 10 s 的请求不只是慢——客户端会再发一次，把负载翻倍。
因此 Decis 把*等待引擎*的时间限制在 8 秒，并以 429 加 `retry-after-ms` 作答。它做不到的是
中断已经开始的前向；这个预算覆盖排队，不覆盖计算。

## 日志

| 变量 | 默认值 | 含义 |
|---|---|---|
| `DECIS_LOG_LEVEL` | `info` | 标准的 Python 日志级别。 |

每个请求都记一行日志，包含 method、path、status、耗时、引擎以及它返回的
`x-typesafe-request-id`，这样客户端报出的 id 就能在服务端日志里查到。请求体与响应体不记入日志：
它们包含你的数据。

## 文件

| 变量 | 默认值 | 含义 |
|---|---|---|
| `DECIS_ENV_FILE` | `.env` | 加载哪个 env 文件。`--env-file` 会按命令覆盖它。 |

## Playground 变量

playground 是独立的进程，它直接读 `os.environ`
（[`playground/server.py`](../playground/server.py)），不经过 `config.py`。在 Compose 下它刻意
**不**继承引擎的 `env_file`——那些值描述的是引擎，而这个容器不跑任何引擎——所以传进去的只有下面
这些变量。

| 变量 | 默认值 | 含义 |
|---|---|---|
| `DECIS_PLAYGROUND_HOST` | `0.0.0.0` | playground 的绑定地址。 |
| `DECIS_PLAYGROUND_PORT` | `8080` | 容器内的绑定端口，由 Compose 钉住。这不是宿主端口——宿主端口是下面的 `DECIS_PLAYGROUND_HOST_PORT`。 |
| `DECIS_PLAYGROUND_WEB_DIR` | `playground/web` | 它提供的静态文件目录。 |
| `DECIS_PLAYGROUND_TIMEOUT_S` | `120` | 一次经代理的 `/v1/systemone` 调用最多允许多久。故意给得宽：代理超时如果在引擎还在计算的时候触发，就会把一个即将到达的答案报成错误。 |
| `DECIS_PLAYGROUND_PROBE_TIMEOUT_S` | `2` | 探测某个候选引擎的 `/readyz` 最多允许多久。 |
| `DECIS_PLAYGROUND_UPSTREAM` | 未设置 | 直接指定一个先试的引擎，跳过搜索。它仍然要被探测，不是无条件信任。 |
| `DECIS_PLAYGROUND_CANDIDATES` | 内置列表 | 逗号分隔的 `/readyz` 候选，按顺序尝试。Compose 把它设成每个引擎的服务名（目前四个）加 `host.docker.internal`。 |

探测会绕开环境里的任何代理：走代理的探测会去问代理 `127.0.0.1`，问不出关于引擎的任何事情。

## 仅 Compose 的变量

这些由 `docker-compose.yml` 读取，不由服务端读取。见[部署](deployment.zh-CN.md)。

| 变量 | 默认值 | 含义 |
|---|---|---|
| `COMPOSE_PROFILES` | `laya-multilingual` | `docker compose up` 启动哪个引擎容器。 |
| `DECIS_HOST_PORT` | `8000` | 默认引擎的宿主端口。 |
| `DECIS_KEV_HOST_PORT` | `8001` | kev 引擎的宿主端口。 |
| `DECIS_JEFF_QWEN_HOST_PORT` | `8002` | jeff-qwen3.5-0.8b 引擎的宿主端口。 |
| `DECIS_JEFF_GEMMA_HOST_PORT` | `8003` | jeff-gemma4-e2b 引擎的宿主端口。 |
| `DECIS_PLAYGROUND_HOST_PORT` | `8080` | playground 的宿主端口，发布到容器内的 `DECIS_PLAYGROUND_PORT`。 |
