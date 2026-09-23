# Decis

[English](README.md) · **简体中文**

**一个 API，跑所有轻量决策模型。**

Decis 是一个体量很小、可以自托管的服务端，说的是 [TypeSafe 的 System One API](https://docs.typesafe.ai/api)，也就是和 Jev 相同的 `/v1/systemone` 契约，并用你选定的开源决策模型来回答这些请求。把官方 `typesafe-sdk` 指向 Decis 而不是 `api.typesafe.ai`，其他什么都不用改。

> **状态：两个真实模型家族跑在同一个契约后面。** `Stage 0`–`Stage 2` 已完成。线格式契约、认证、
> 错误形状、引擎抽象、权重解析、CLI、Dockerfile 与 CI 都已实现，**445 个无权重测试通过**——
> 其中包括官方 `typesafe-sdk` 0.7.1 走真实 socket 的验收测试。已注册四个真实
> checkpoint，**`decis serve --engine laya-multilingual` 与 `decis serve --engine kev-0.8b`
> 现在都能回答真实请求**；另有 27 个测试会加载真实权重。接 kev 的过程**没有改 `render.py`、
> `answers.py` 或路由层**——这正是 Stage 2 要验证的事（见
> [`docs/design-review.md §2`](docs/design-review.md)）。
>
> 契约不是推断出来的，而是建立在[官方 OpenAPI 快照](docs/contract/typesafe-openapi-0.2.0.json)和[线上 API 实际返回什么的记录](docs/contract/observations-2026-09-22.md)之上。[`docs/design-review.md`](docs/design-review.md) 是对这套设计的对抗性审查，包括哪些部分还没有被验证。

---

## 快速开始

```bash
git clone https://github.com/kingfs/Decis && cd Decis
uv sync --extra dev --extra laya
cp .env.example .env                               # 然后把 DECIS_API_KEY 改成任意值
uv run decis download --engine laya-multilingual   # 647 MiB，只需一次
uv run decis serve --host 127.0.0.1 --port 8000    # 默认引擎就是 laya-multilingual
```

`laya-multilingual` 是默认引擎。CPU 上加载约需 **80 秒**，所以在发流量之前先轮询 `/readyz`——
`/healthz` 立刻可用，`/readyz` 在模型可用之前一直报 `loading`。

```python
from typesafe_sdk import Choice, Noul, TypeSafeClient

# 不传 model：SDK 会发它的默认值 "jev-latest"，Decis 用它实际加载的引擎作答。
# 迁移的全部工作量就是换掉 TYPESAFE_BASE_URL。
client = TypeSafeClient(api_key="local", base_url="http://127.0.0.1:8000")

response = client.system_one(
    state={
        "subject": "Duplicate charge on invoice #4411",
        "body": "We were billed twice for March. Please refund the duplicate today or we will cancel our plan.",
    },
    questions={
        "department": Choice(
            instructions="Which team should handle this?",
            criteria={
                "billing": "invoices, payments, refunds",
                "technical": "bugs, outages, system errors",
                "sales": "pricing, new contracts",
            },
        ),
        "churn_risk": Noul(instructions="Does the user threaten to cancel or leave?"),
    },
)

print(response.model)  # decis/laya-multilingual@<laya 版本>
print(response.choices["department"].choice)  # 引擎选出的那一项
print(response.choices["department"].confidence)  # 0..1
print(response.nouls["churn_risk"].noul)  # P(true)，0..1
```

同一个请求用 curl：

```bash
curl -s localhost:8000/v1/systemone \
  -H 'authorization: Bearer local' -H 'content-type: application/json' -d '{
  "state": "We were billed twice for March. Please refund the duplicate today.",
  "model": "jev-latest",
  "questions": {
    "department": {"type": "choice", "instructions": "Which team should handle this?",
                   "criteria": {"billing": "invoices, payments, refunds",
                                "technical": "bugs, outages, system errors"}},
    "churn_risk": {"type": "noul", "instructions": "Does the user threaten to cancel?"}
  }}'
```

```jsonc
{
  "model": "decis/laya-multilingual@<laya 版本>",
  "answers": {
    "department": { "type": "choice", "choice": "<你的某个 criteria 键>", "confidence": "<0..1>",
                    "probabilities": { "billing": "<p>", "technical": "<p>" } },
    "churn_risk": { "type": "noul", "noul": "<P(true)，0..1>" }
  },
  "usage": { "input_tokens": "<n>", "output_tokens": "<n>" },
  // Decis 在契约之外加的所有东西都在同一个键下面，官方 SDK 会忽略它。
  // batch_size 就是"批处理到底有没有发生"的证据。
  "decis": { "engine": "laya-multilingual", "engine_version": "<laya 版本>", "device": "cpu",
             "dtype": "float32", "latency_ms": "<n>", "batch_size": 2 }
}
```

具体数值取决于引擎和它的权重，所以这里写成 `<…>` 而不是编造数字；
每个字段的含义见 [`examples/curl.md`](examples/curl.md)。

现在就能用的其他命令：

```bash
uv run decis models     # 本机注册了哪些引擎、哪些可用
uv run decis doctor     # 环境与配置自检
```

### 加载、就绪与离线镜像

CPU 上冷启动约 **80 秒**，整个过程里探针都可用：引擎在后台线程里加载，所以 `/healthz` 立刻就有响应，
`/readyz` 会把当前状态说清楚，不用你去猜。

```
$ curl -s localhost:8000/healthz   # 启动后 0.5 秒
{"status":"ok","version":"0.0.1"}
$ curl -s localhost:8000/readyz    # 还在加载
{"status":"loading","engine":"laya-multilingual"}     # 503，带 retry-after
$ curl -s localhost:8000/readyz    # 约 80 秒后
{"status":"ready","engine":"laya-multilingual"}
```

加载期间到达的请求会收到 `503` 和 `"The model is still loading. Please retry shortly."`；
如果权重加载**失败**了，收到的 `503` 会明确说明失败，并且**不带** `retry-after`——对一个不会恢复的
服务反复重试只是浪费时间。注意别把**存活探针**指向 `/readyz`，否则慢启动会变成重启循环。
已发布的镜像里已经带了权重，所以容器起来之后完全不需要网络：

```bash
docker build -f docker/Dockerfile \
  --build-arg DECIS_EXTRAS=laya --build-arg DECIS_ENGINE=laya-multilingual \
  --build-arg DECIS_PREDOWNLOAD=laya-multilingual -t decis:laya-multilingual .
docker run --rm -p 8000:8000 -e DECIS_API_KEY=local decis:laya-multilingual
```

要跑自己的微调，就把那个引擎指向一个目录——它的优先级高于其他一切，包括网络：

```bash
DECIS_MODEL_PATH_LAYA_MULTILINGUAL=/srv/finetunes/acme-triage uv run decis serve
```

kev 有一点要注意：这个目录是**适配器**（LoRA 加 pointer head），也正是你会去微调的部分。
Qwen3.5 **基座**在 checkpoint 元数据里是以 Hub repo id 的形式记着的，所以单独挂载一份基座
不会被读到——`decis download` 会把基座放进 Hugging Face 缓存，之后就能完全离线运行。
详见 [`docs/design-review.md §2-D11`](docs/design-review.md)。

## 问题

Jev 已经证明，*决策模型*（针对一段 state 回答带类型的问题，返回校准过的概率，而不是生成文本）应该放在请求路径上，而不是放在聊天窗口里。但 Jev 是托管、闭源的 API：

- **延迟。** 独立实测每题 p50 为 236–276 ms。如果你的决策只是一次路由判断，那这个网络往返就是每次调用都要付的成本。
- **数据。** 你的工单、发票、用户消息都要离开你自己的基础设施。
- **成本。** 每十亿输入 token 42 美元，永久如此。

现在已经有开源的替代品，小到可以和应用跑在一起。缺的不是模型，而是**统一的对外服务方式**。

Decis 就是这种方式：一个稳定的 API，多个引擎，每个模型打成一个容器。

## 你能得到什么

- **jev 契约只实现一次。** `POST /v1/systemone`、`GET /v1/models`、`choice` / `score` / `noul` 三种原语、官方 SDK 的错误形状和 request-id 头。线格式固定在 [`docs/api-compatibility.md`](docs/api-compatibility.md) 里，每条结论都标了证据等级——而且线上 API 是**实测过**而不是推断的，401/403 的分工就是这么发现的。
- **引擎可插拔。** 引擎只需要给出每个选项的概率；服务端负责把它转成 `Noul` / `Choice` / `Score` answer，所以每个引擎返回的形状完全一致、可以互相比较，`confidence` 也只有一处有文档的定义。引擎按字符串路径注册，所以只装了某一个引擎依赖的镜像依然能列出其他引擎。
- **为高频小请求而做。** 两个开源决策模型都只有一次前向，所以请求路径被设计成在问题送到模型之前*跨请求*合并成批——但这条收益还没测过，诚实说明见下面的性能一节。
- **权重就打在镜像里。** 每个已发布的镜像都带着自己的 checkpoint，所以 `docker run` 不需要网络、
  不需要挂卷、也不需要下载步骤；`DECIS_MODEL_DIR` 可以改用你自己的目录，另外还有一个不带权重的
  `-runtime` 变体，给"权重放共享卷、每个副本只挂一次"这类部署用。
- **默认安全。** Bearer token 认证（从 `.env` 读）、常数时间比较、请求体大小上限，以及在公网地址上没配 token 就拒绝启动。

## 引擎

| 引擎 | 骨干模型 | 参数量 | 权重 | 状态 | 说明 |
|---|---|---|---|---|---|
| `laya-multilingual` | mmBERT-base | 322M | 647 MiB | **已可用** | 支持 100+ 种语言，快约 2.2 倍，默认引擎 |
| `laya` | ModernBERT-large | 421M | 807 MiB | **已可用** | 英语，在英语基准上最强 |
| `laya-typed-decisions` | ModernBERT-large | 421M | 807 MiB | **已可用** | 同一个仓库里的 typed-decisions checkpoint |
| `kev-0.8b` | Qwen3.5-0.8B + LoRA + pointer head | 0.8B | 1.69 GiB | 可用 | 架构不同（只做 prefill，不生成文本），错误分布也不同 |
| `kev-4b` / `kev-9b` | Qwen3.5 + LoRA | 4B / 9B | ~8 GB / ~18 GB | 未注册 | 精度更高，但已经不算“轻量” |
| `remote` | — | — | — | 计划中 | 转发到真正的 `api.typesafe.ai`，可用于 A/B 对比，也可当测试基准 |

注册的每个引擎都是有真实权重的 checkpoint。线格式契约测试跑在一个无权重的测试替身上，
它位于 [`tests/`](tests/fixture_engine.py)，并且**故意不注册到服务端**——`decis serve`
永远不会用假模型作答。

新增一个引擎就是一个模块加一行注册，**不需要改归一化层**——如果接一个引擎要改 `render.py` 或 `answers.py`，说明抽象错了。见 [`AGENTS.md §5`](AGENTS.md)。

## 性能

决策模型只做一次前向，所以大家最常引用的漂亮数字来自 **Apple 芯片上用 MLX 跑的小而短的输入**：M3 Max 上每题约 11–18 ms。这是真实的，但只有 CPU 的容器做不到。下面是在**一台 24 vCPU、没有 GPU 的 aarch64 机器**上实测的结果——最差的情况，也是大多数人会先试的情况：

<!-- LATENCY:START -->

| 引擎 | 设备 | dtype | 线程 | 1 个问题 | 10 个问题 | 冷启动 | 峰值 RSS |
|---|---|---|---:|---:|---:|---:|---:|
| `laya` | cpu | float32 | 24 | 446 ms | 3,222 ms (322.1 ms/题) | 76.3 s | 2.8 GB |
| `laya-multilingual` | cpu | float32 | 24 | 231 ms | 987 ms (98.7 ms/题) | 72.9 s | 4.84 GB |

由 [`benchmarks/report.py`](benchmarks/report.py) 从 [`benchmarks/results/`](benchmarks/results/) 的原始 JSON 生成；**整行取自同一个配置**（torch 在本机的默认线程数，每个 vCPU 一个），样本 p50，单进程，仅请求内批处理。

**这是延迟，不是吞吐。** 每个请求的问题共享同一个 `state`，这是容易的情况。跨请求批处理随后已经测过，结论是在 CPU 上**不提升吞吐**（见下面的批处理一节与 [`docs/design-review.md`](docs/design-review.md) §4-M5）。

<!-- LATENCY:END -->

这张表里有三点值得记住：

- **单请求内批处理是真实的杠杆。** 一次调用问十个问题，每题的代价大约是单独问一个问题的一半。
- **冷启动约 75 秒**，峰值内存是权重文件的 3–7 倍。readiness 探针和容器内存要按这个来规划。
- **`kev-0.8b` 需要 GPU。** 它的 Qwen3.5 骨干要跑得快就得用 `flash-linear-attention` 和 `causal_conv1d`，而这两个都依赖 Triton/CUDA。在 CPU 上它比 Laya 慢一个数量级。它在 CPU 上的 `bf16` 路径又比 `fp32` 慢一个数量级（见下表）——所以 Decis 按引擎*和*设备分别选 dtype，并且会给出警告，而不是悄悄慢下去。

<!-- DTYPE:START -->

| 引擎 | dtype | 3 个问题 | 相对 | 与原答案对比 |
|---|---|---:|---:|---|
| `kev-0.8b` | `fp32` | 1,656 ms | 1x | 基线 |
| `kev-0.8b` | `bf16` | 137,174 ms | 83x | argmax 相同，概率差 ≤0.01 |

由 [`benchmarks/report.py`](benchmarks/report.py) 生成。每个 dtype 一次观测，所以倍数是数量级结论而不是统计量；对比列由记录的答案正文算出，不是旁边的散文。

<!-- DTYPE:END -->

### 跨请求批处理：测过了，结论是否定的

把*不同*请求的问题合并进同一次前向，是 `docs/design.md §6` 原本的吞吐主张。这件事已经在本机测过：

<!-- BATCHING:START -->

| 序列集 | 序列长度 | padding | 最好的批大小 | 相对串行 |
|---|---|---:|---:|---:|
| 同一 state（正对照，短） | 115–115 | 1.00 | 8 | 1.92x |
| 同一 state（正对照） | 393–393 | 1.00 | 16 | 1.24x |
| 不同 state，长度接近 | 530–674 | 1.10 | 8 | 1.09x |
| 不同 state，长度倾斜 | 140–821 | 2.41 | 8 | 0.34x |

正对照（所有项完全相同、padding 为 1.00）达到 **1.92x**（`shared_short`，batch 8），证明瓶颈确实是每次调用的固定开销，也证明这套测量能测出收益。 真实流量形状（不同 state）最高 1.09x（`uniform`，batch 8），而最差 0.34x（`skewed`，batch 8）。 长度倾斜时 padding 最高 2.47x（`skewed`，batch 16）：每一行都要补齐到批内最长，浪费的算力直接变成更慢的每项成本。

原始 JSON：[`benchmarks/results/`](benchmarks/results/)；完整表格（每个批大小、padding、盈亏平衡等待、进程数对比）见 [`docs/design-review.md`](docs/design-review.md) §4-M5。批是**直接调用引擎**合成的，没有队列与取消，所以这些数字是真实攒批器的**上界**。

<!-- BATCHING:END -->

所以：**不要指望跨请求批处理提高吞吐。** 它的收益只存在于序列很短的时候（摊薄每次调用的固定开销），
而真实 state 几百 token 时固定开销已经不重要；长度一倾斜，padding 会让它慢好几倍。
每个响应里仍然带 `decis.batch_size`——机制必须可观测，否则"它会批处理"就是一句无法证伪的话。

每个样本的原始输出、完整的命令和机器配置都在 [`benchmarks/results/`](benchmarks/results/) 里。文档里引用的每个数字都来自那里。

## 部署

一个引擎一个镜像，因为引擎依赖彼此冲突而且体积很大。它们在 Docker Hub 上共用一个仓库，**引擎放在 tag 里**：

```bash
docker run -p 8000:8000 -e DECIS_API_KEY=change-me kingfs/decis:laya-multilingual
```

| Tag | 引擎 | 里面是什么 |
|---|---|---|
| `laya-multilingual`、`latest` | Laya 多语言（322M）——默认引擎 | 已经打进去的 647 MiB 权重；CPU 上约 1-2 分钟到 `/readyz`，常驻约 3 GiB |
| `kev-0.8b` | kev 0.8B | 已经打进去的 1.7 GiB adapter + Qwen 基座；想要 GPU |

只有 `laya-multilingual` 另外拿一个裸 `latest`，所以 `docker pull kingfs/decis` 拿到的是默认引擎。
每个 tag 都是覆盖 `amd64` 与 `arm64` 的多架构 manifest。
注册表里的 `laya`（英语）与 `laya-typed-decisions` **没有镜像**：构建矩阵只覆盖上面两个 tag，
这两个 checkpoint 请从源码目录运行。

权重就在镜像**里面**，所以那个容器首次启动既不需要网络也不需要卷。想在不重新拉镜像的前提下换
checkpoint，就挂一个目录——但那个目录里必须已经有 `<engine-id>/`：

```bash
# /srv/models/laya-multilingual/multilingual/... 必须已经存在
docker run -p 8000:8000 -e DECIS_API_KEY=change-me -v /srv/models:/models kingfs/decis:laya-multilingual
```

**不要**把一个空目录挂在 `/models` 上：挂载会**盖住烤进镜像的权重**（命名卷会用镜像内容初始化
一次然后自己留一份，bind mount 则直接替换掉整个目录），于是容器悄悄退回联网下载你以为已经有的
东西。`docker-compose.yml` 因此什么都不往那里挂。

release tag 还会发布不带权重的变体 `<engine>-runtime-<version>`，给"权重放共享卷"或"每个节点
的镜像要尽量小"的部署用。它是 release 产物而不是滚动的 tag，所以下面的例子带版本号——用当前那个。
先把卷填一次，再让服务端读它：

```bash
docker pull kingfs/decis:laya-multilingual-runtime-v1.2.0
docker run --rm -v decis-models:/models kingfs/decis:laya-multilingual-runtime-v1.2.0 \
  decis download --engine laya-multilingual
docker run -p 8000:8000 -e DECIS_API_KEY=change-me -v decis-models:/models \
  kingfs/decis:laya-multilingual-runtime-v1.2.0
```

`docker compose` 只跑发布的镜像本身——没有预取容器、没有卷、没有下载：

```bash
cp .env.example .env        # 至少改 DECIS_API_KEY；COMPOSE_PROFILES 决定起哪个引擎
docker compose -f docker-compose.yml up -d --wait   # 默认只起 laya-multilingual

# 这里的 `--wait` 会一直等到引擎真的能作答：compose 层的探针打的是 `/readyz`，
# 所以它把 CPU 上约 80-100 秒的冷启动等完了。镜像自带的 HEALTHCHECK 仍然留在
# `/healthz` 上给编排器用——liveness 探针不能在引擎还在加载时就判失败。
#
# 不想用 `--wait` 就自己等：
until curl -fsS localhost:8000/readyz >/dev/null; do sleep 2; done

docker compose --profile kev-0.8b up -d     # 或者另一个引擎（宿主端口 8001）
```

`-f docker-compose.yml` 不是装饰：在源码目录里 Compose 还会自动加载
`docker-compose.override.yml`，它会用你**当前的工作区**把同样的服务构建成 `decis-local:*`。
想让改动生效就别加 `-f`；部署时只拷 `docker-compose.yml` 这一个文件。

想自己构建：

```bash
docker build -f docker/Dockerfile \
  --build-arg DECIS_EXTRAS=laya \
  --build-arg DECIS_ENGINE=laya-multilingual \
  --build-arg DECIS_PREDOWNLOAD=laya-multilingual \
  -t decis:laya-multilingual .
```

除非显式指定，镜像不会安装任何引擎 extra，所以裸 `docker build` 得到的是纯 API 镜像：
它能启动并回答 `/healthz` 与 `/v1/models`，但在引擎依赖和权重就位之前 `/readyz` 一直是 503；
不传 `DECIS_PREDOWNLOAD` 得到的就是上面那个不带权重的变体。

需要代理的网络里，代理要单独传给**构建**：`env_file` 只作用于容器，Docker 也不会把你 shell 里的
`HTTP_PROXY` 带进 `RUN`，于是权重下载报 `Network is unreachable`，而依赖安装那一步可能照样成功。

```bash
docker compose build --build-arg HTTP_PROXY="$HTTP_PROXY" --build-arg HTTPS_PROXY="$HTTPS_PROXY" \
  --build-arg NO_PROXY="$NO_PROXY"
```

容器以非 root 用户运行，不需要任何外部服务——没有 Redis、没有 Postgres、没有 Celery——并且在公网地址上没配 token 时会拒绝启动。

## 原语

三种问题类型，可以在一个请求里混用，针对同一段 state 并行计算：

| 类型 | 问什么 | 返回 |
|---|---|---|
| `choice` | 从一组命名的选项中选一个 | `choice`、`probabilities`、`confidence` |
| `score` | 按有序的评分档打分 | `score`（期望档位）、`legend`、`probabilities`、`confidence` |
| `noul` | 这是真的吗？ | `noul` — 校准后的 P(true)，0–1 |

## 文档

| 文档 | 内容 |
|---|---|
| [`examples/`](examples/README.md) | 可运行示例：裸 `curl` 与官方 SDK。每条命令都由 CI 执行 |
| [`docs/api-compatibility.md`](docs/api-compatibility.md) | 精确的线格式契约，每条结论都有证据等级，并列出所有已知偏差 |
| [`docs/design.md`](docs/design.md) | 架构、引擎抽象、批处理、打包、路线图 |
| [`docs/design-review.md`](docs/design-review.md) | 对本设计的自我审查：发现并修掉的缺陷、方法论局限、尚未验证的部分 |
| [`docs/feasibility.md`](docs/feasibility.md) | 调研结果、实测数字、风险清单 |
| [`docs/contract/`](docs/contract/) | 官方 OpenAPI 快照，以及线上 API 实际返回什么的原始记录 |
| [`AGENTS.md`](AGENTS.md) | 工程契约：唯一事实来源、契约不变量、禁用清单 |

动手贡献之前如果只看一份文档，看设计审查。缺口都写在里面，而不是藏起来。

## 与其他项目的关系

Decis 不训练模型，只负责把它们服务起来；它尽量给出署名，而不是重复做别人做过的事。

- **[Jev](https://docs.typesafe.ai/introduction)**（TypeSafe AI）——本项目对标的闭源模型。Decis 重新实现的是*接口*，不是模型。
- **[kev](https://github.com/jaredpalmer/kev)**（Jared Palmer）——基于 Qwen3.5 的 Jev 风格决策模型，本身已经是一个只服务自己权重的 TypeSafe 兼容服务端。Decis 复用 kev 的推理内核（vendor 固定版本，Apache-2.0，署名见 `NOTICE`），并把服务层泛化到多个引擎。
- **[Laya](https://huggingface.co/convaiinnovations/laya)**（Convai Innovations）——Apache-2.0，多语言，非自回归，一次前向。Decis 把官方的 `laya` 包当作一个引擎使用。
- **[UniTS-Hub](https://github.com/kingfs/UniTS-Hub)**——Decis 沿用的多模型容器构建方式（一个 Dockerfile 加一个 build arg，GitHub Actions matrix 按 digest 推送，再用 `imagetools create` 合并）。

## 开发

```bash
uv sync --extra dev
uv run pytest -q                        # 445 个测试，约 11 秒，无权重、无网络
uv run pytest -m weights                # 27 个加载真实权重（Laya + kev）的测试
uv run ruff check && uv run ruff format --check
uv run decis serve --host 127.0.0.1     # 回环地址允许不带 token
```

测试按它提供的证据分层组织，与 [`docs/api-compatibility.md §8`](docs/api-compatibility.md) 对应：

| 层 | 证明了什么 | 位置 |
|---|---|---|
| L0 | 我们的模型仍然与仓库里的官方 OpenAPI 快照一致 | [`tests/test_contract_openapi.py`](tests/test_contract_openapi.py) |
| L1/L2 | 响应形状，以及 `AGENTS.md §3` 的每一条不变量 | [`tests/test_contract_shape.py`](tests/test_contract_shape.py) |
| L3b | 错误契约：401 与 403 的分工、认证先于校验、request id | [`tests/test_contract_errors.py`](tests/test_contract_errors.py) |
| L4 | **真正的 `typesafe-sdk` 走真实 socket**——唯一能证明兼容性的测试 | [`tests/test_contract_sdk.py`](tests/test_contract_sdk.py) |
| — | 就绪探针、冷启动、优雅下线 | [`tests/test_readiness.py`](tests/test_readiness.py) |
| — | 用 AST 解析强制"唯一事实来源"与分层规则 | [`tests/test_conventions.py`](tests/test_conventions.py) |

改动 `docs/api-compatibility.md` 里的任何内容之前，先跑一次对线上 API 的差分——**没有任何离线测试能发现线上服务端偏离它自己的 OpenAPI**：

```bash
TYPESAFE_LIVE_API_KEY=<key> uv run pytest tests/test_contract_sdk.py -m network
```

## 许可证

Apache-2.0。第三方署名见 [`NOTICE`](NOTICE)。
