# 配置

[English](configuration.md) · **简体中文**

[文档索引](../README.zh-CN.md#文档) · [快速开始](getting-started.zh-CN.md) · [部署](deployment.zh-CN.md)

Decis 从 `.env` 和环境变量读取配置。**shell 里已经设好的变量优先于 `.env`**，所以
`DECIS_PORT=9000 decis serve` 的行为和它看起来一样。`.env.example` 是带注释的模板；
`decis doctor` 会打印实际解析出的值，以及哪些变量被设过。

本页的每个变量都只在一个模块里读取：
[`src/decis/config.py`](../src/decis/config.py)。包里的其他任何地方都不碰环境变量。

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

## 引擎选择与权重

| 变量 | 默认值 | 含义 |
|---|---|---|
| `DECIS_DEFAULT_ENGINE` | `laya-multilingual` | `decis serve` 加载的引擎，也是回答 `model` 为“你的默认值”这类名字（如 `jev-latest`）的请求所用的引擎。`--engine` 会覆盖它。 |
| `DECIS_ACCEPT_FOREIGN_DEFAULTS` | `1` | 用已加载的引擎回答 `jev-latest`（官方 SDK 的默认值），而不是 422。正是它让只换 `base_url` 就够了。这个替换会记在 `decis.requested_model` 里。 |
| `DECIS_MODEL_DIR` | 未设置 | 预先下载好的权重目录。期望布局：`<DECIS_MODEL_DIR>/<engine-id>/`。 |
| `DECIS_MODEL_PATH_<ENGINE_ID>` | 未设置 | 让单个引擎指向它自己的目录。优先级高于 `DECIS_MODEL_DIR`。名字是引擎 id 全大写，所以只能寻址不含点号的 id——见下面的 `kev-0.8b` 注意点。 |

权重解析的顺序是固定的；第一个命中的生效，本地目录优先于网络：

1. `DECIS_MODEL_PATH_<ENGINE_ID>` —— 例如 `DECIS_MODEL_PATH_LAYA_MULTILINGUAL=/srv/finetunes/acme-triage`。
2. `DECIS_MODEL_DIR/<engine-id>/`。
3. Hugging Face 缓存，必要时下载。

```bash
uv run decis download --engine laya-multilingual                  # 下载进 HF 缓存
uv run decis download --engine laya-multilingual --dest ./models  # 下载进 ./models/laya-multilingual/
DECIS_MODEL_DIR=./models uv run decis serve
```

> **`kev-0.8b` 有一个注意点。** 按引擎的覆盖变量寻址不到它：变量名是引擎 id 全大写并把 `-`
> 换成 `_`，而点号在那里没有任何表示，所以这个引擎对应的变量会规整成 id `kev-0-8b`——它没有
> 注册任何东西，于是被静默忽略。请改用 `DECIS_MODEL_DIR` 加一个 `kev-0.8b/` 子目录。那个目录
> 是**适配器**（LoRA 加 pointer head），也正是你会去微调的部分；Qwen3.5 **基座**模型在适配器的
> checkpoint 元数据里是以 Hub repo id 的形式被引用的，所以单独挂载一份基座不会被读到。
> `decis download` 会把基座放进 Hugging Face 缓存，之后引擎就能离线运行。见
> [`design-review.md §2-D11`](design-review.md)。

## 计算

| 变量 | 默认值 | 含义 |
|---|---|---|
| `DECIS_DEVICE` | 自动 | `cpu`、`cuda` 或 `mps`。不设则自动选最可用的。 |
| `DECIS_DTYPE` | 按引擎+设备 | 强制 `fp32`、`fp16` 或 `bf16`。读取它的是那些查 `registry.DTYPE_DEFAULTS` 的引擎——目前只有 `kev-0.8b`。Laya 由它自己的 `Agent` 决定精度，会忽略它。 |
| `DECIS_TORCH_THREADS` | 每个 vCPU 一个 | 该进程的线程数。 |

dtype 按引擎**和**设备分别选择，因为同一个选择在不同硬件上可能差一个数量级。`kev-0.8b`
在 CPU 上用 `bf16` 比 `fp32` 慢 83 倍；实测表见
[性能](performance.zh-CN.md#每种引擎每种设备的-dtype)。`DECIS_DTYPE` 主要用来在你自己的硬件上
重测，而且只有 `kev-0.8b` 会读它。设置一个已知很糟的组合只会记一条警告，不会拒绝启动。

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
| `DECIS_LOG_PAYLOADS` | `0` | 记录完整的请求体和响应体。默认关闭：它们包含你的数据。 |

每个请求都记一行日志，包含 method、path、status、耗时、引擎以及它返回的
`x-typesafe-request-id`，这样客户端报出的 id 就能在服务端日志里查到。

## 文件

| 变量 | 默认值 | 含义 |
|---|---|---|
| `DECIS_ENV_FILE` | `.env` | 加载哪个 env 文件。`--env-file` 会按命令覆盖它。 |

## 仅 Compose 的变量

这些由 `docker-compose.yml` 读取，不由服务端读取。见[部署](deployment.zh-CN.md)。

| 变量 | 默认值 | 含义 |
|---|---|---|
| `COMPOSE_PROFILES` | `laya-multilingual` | `docker compose up` 启动哪个引擎容器。 |
| `DECIS_HOST_PORT` | `8000` | 默认引擎的宿主端口。 |
| `DECIS_KEV_HOST_PORT` | `8001` | kev 引擎的宿主端口。 |
| `DECIS_PLAYGROUND_HOST_PORT` | `8080` | playground 的宿主端口。 |
| `DECIS_PLAYGROUND_UPSTREAM` | 未设置 | 直接指定引擎，不让 playground 自己搜索。 |
| `DECIS_PLAYGROUND_CANDIDATES` | 内置列表 | 逗号分隔的 `/readyz` 候选，供 playground 依次尝试。 |
