# Decis

[English](README.md) · **简体中文**

**一个 API，跑所有轻量决策模型。**

Decis 是一个体量很小、可以自托管的服务端，说的是 [TypeSafe 的 System One API](https://docs.typesafe.ai/api)，也就是和 Jev 相同的 `/v1/systemone` 契约，并用你选定的开源决策模型来回答这些请求。把官方 `typesafe-sdk` 指向 Decis 而不是 `api.typesafe.ai`，其他什么都不用改。

> **状态：API server 已经能跑，Laya 已经在它后面跑起来了。** `Stage 0` 与 `Stage 1` 已完成。线格式契约、认证、错误形状、引擎抽象、权重解析、CLI、Dockerfile 与 CI 都已实现，**274 个无权重测试通过**——其中包括官方 `typesafe-sdk` 0.7.1 走真实 socket 的验收测试。除 `mock` 外已注册三个真实 Laya checkpoint，**`decis serve --engine laya-multilingual` 现在就能回答真实请求**；另有 14 个测试会加载真实权重并检查批不变性。kev 在 Stage 2（见 [`docs/design.md §12`](docs/design.md)）。
>
> 契约不是推断出来的，而是建立在[官方 OpenAPI 快照](docs/contract/typesafe-openapi-0.2.0.json)和[线上 API 实际返回什么的记录](docs/contract/observations-2026-09-22.md)之上。[`docs/design-review.md`](docs/design-review.md) 是对这套设计的对抗性审查，包括哪些部分还没有被验证。

---

## 快速开始

不需要权重、不需要 GPU、不需要下载模型。`mock` 引擎的答案是确定性的。

```bash
git clone https://github.com/kingfs/Decis && cd Decis
uv sync --extra dev
cp .env.example .env          # 然后把 DECIS_API_KEY 改成任意值
uv run decis serve --host 127.0.0.1 --port 8000
```

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

print(response.model)  # decis/mock@0.1.0
print(response.choices["department"].choice)  # 引擎选出的那一项
print(response.choices["department"].confidence)  # 0..1
print(response.nouls["churn_risk"].noul)  # P(true)，0..1
```

同一个请求用 curl：

```bash
curl -s localhost:8000/v1/systemone \
  -H 'authorization: Bearer local' -H 'content-type: application/json' -d '{
  "state": "We were billed twice for March. Please refund the duplicate today.",
  "model": "mock",
  "questions": {
    "department": {"type": "choice", "instructions": "Which team should handle this?",
                   "criteria": {"billing": "invoices, payments, refunds",
                                "technical": "bugs, outages, system errors"}},
    "churn_risk": {"type": "noul", "instructions": "Does the user threaten to cancel?"}
  }}'
```

```jsonc
{
  "model": "decis/mock@0.1.0",
  "answers": {
    "department": { "type": "choice", "choice": "billing", "confidence": 0.763,
                    "probabilities": { "billing": 0.8815, "technical": 0.1185 } },
    "churn_risk": { "type": "noul", "noul": 0.89 }
  },
  "usage": { "input_tokens": 67, "output_tokens": 40 },
  // Decis 在契约之外加的所有东西都在同一个键下面，官方 SDK 会忽略它。
  // batch_size 就是"批处理到底有没有发生"的证据。
  "decis": { "engine": "mock", "engine_version": "0.1.0", "device": "cpu", "dtype": "none",
             "latency_ms": 0.2, "batch_size": 2 }
}
```

现在就能用的其他命令：

```bash
uv run decis models     # 本机注册了哪些引擎、哪些可用
uv run decis doctor     # 环境与配置自检
```

### 换成真实模型

把 `mock` 换成 Laya。上面的请求一个字都不用改——变的只是谁来回答。

```bash
uv sync --extra laya
uv run decis download --engine laya-multilingual --dest ./models   # 647 MiB，只需一次
uv run decis serve --engine laya-multilingual --host 127.0.0.1
```

CPU 上冷启动约 **80 秒**，而且**在此之前服务不回答任何请求**——引擎在启动过程中加载，
此时 HTTP 协议循环还没开始，所以这期间探针是挂起而不是收到 503。要放到探针后面的话，
把启动宽限期设得足够长（本镜像的 `HEALTHCHECK` 用的是 180 秒），否则编排系统会反复重启一个
其实在正常加载的容器。把权重打进镜像后它就能离线运行：

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
- **权重可以打进镜像，也可以挂卷。** 镜像自带模型，`docker run` 离线就能跑；`DECIS_MODEL_DIR` 可以用你自己的目录覆盖。
- **默认安全。** Bearer token 认证（从 `.env` 读）、常数时间比较、请求体大小上限，以及在公网地址上没配 token 就拒绝启动。

## 引擎

| 引擎 | 骨干模型 | 参数量 | 权重 | 状态 | 说明 |
|---|---|---|---|---|---|
| `mock` | — | — | 无 | **已实现** | 确定性、无权重。用于契约测试与演示 |
| `laya-multilingual` | mmBERT-base | 322M | 647 MiB | **已可用** | 支持 100+ 种语言，快约 2.2 倍，预期的默认选择 |
| `laya` | ModernBERT-large | 421M | 807 MiB | **已可用** | 英语，在英语基准上最强 |
| `laya-typed-decisions` | ModernBERT-large | 421M | 807 MiB | **已可用** | 同一个仓库里的 typed-decisions checkpoint |
| `kev-0.8b` | Qwen3.5-0.8B + LoRA | 0.8B | ~1.7 GB | Stage 2 | 架构不同，错误分布也不同 |
| `kev-4b` / `kev-9b` | Qwen3.5 + LoRA | 4B / 9B | ~8 GB / ~18 GB | Stage 2 | 精度更高，但已经不算“轻量” |
| `remote` | — | — | — | 计划中 | 转发到真正的 `api.typesafe.ai`，可用于 A/B 对比，也可当测试基准 |

新增一个引擎就是一个模块加一行注册，**不需要改归一化层**——如果接一个引擎要改 `render.py` 或 `answers.py`，说明抽象错了。见 [`AGENTS.md §5`](AGENTS.md)。

## 性能

决策模型只做一次前向，所以大家最常引用的漂亮数字来自 **Apple 芯片上用 MLX 跑的小而短的输入**：M3 Max 上每题约 11–18 ms。这是真实的，但只有 CPU 的容器做不到。下面是在**一台 24 vCPU、没有 GPU 的 aarch64 机器**上实测的结果——最差的情况，也是大多数人会先试的情况：

| 引擎 | 冷启动 | 峰值 RSS | 1 个问题 | 10 个问题 |
|---|---:|---:|---:|---:|
| `laya-multilingual`（322M） | 73 s | 4.84 GB | 201 ms | 987 ms — **98.7 ms/题** |
| `laya`（英语，421M） | 76 s | 2.80 GB | 432 ms | 3222 ms — 322 ms/题 |
| `kev-0.8b`（fp32，3 个问题） | 15 s | — | — | 1656 ms — 552 ms/题 |

这张表里有三点值得记住：

- **单请求内批处理是真实的杠杆。** 一次调用问十个问题，每题的代价大约是单独问一个问题的一半。
- **冷启动约 75 秒**，峰值内存是权重文件的 3–7 倍。readiness 探针和容器内存要按这个来规划。
- **`kev-0.8b` 需要 GPU。** 它的 Qwen3.5 骨干要跑得快就得用 `flash-linear-attention` 和 `causal_conv1d`，而这两个都依赖 Triton/CUDA。在 CPU 上它比 Laya 慢一个数量级。它在 CPU 上的 `bf16` 路径又比 `fp32` 慢 **83 倍**（单次观测，不是基准）——所以 Decis 按引擎*和*设备分别选 dtype，并且会给出警告，而不是悄悄慢下去。

### 这张表**没有**告诉你的

Decis 的核心吞吐主张是**跨请求批处理**——把*不同*请求的问题合并进同一次前向。这件事**从未被测过**，因为它需要 Stage 3 才会有的攒批调度器。上面这张表只测了共享同一个 `state` 的问题，那是容易的情况。真实流量里每个请求的 state 都不同，那里的收益可能更小。

所以：**这里不报任何 QPS 数字，在它被测出来之前也不应该报。** 这就是 [`docs/design-review.md`](docs/design-review.md) 里的缺口 M5，也是每个响应里都带 `decis.batch_size` 的原因——机制必须可观测，否则"它会批处理"就是一句无法证伪的话。设计里包含一个断言攒批器**确实被触发**的测试，所以一个从不触发的批处理实现过不了 CI。

每个样本的原始输出、完整的命令和机器配置都在 [`benchmarks/results/`](benchmarks/results/) 里。文档里引用的每个数字都来自那里。

## 部署

一个引擎一个镜像，因为引擎依赖彼此冲突而且体积很大。带权重的镜像会随各引擎一起发布（Stage 4）；现在这个镜像就是 API 加 `mock` 引擎。

```bash
docker build -f docker/Dockerfile -t decis:mock .
docker run -p 8000:8000 -e DECIS_API_KEY=change-me decis:mock
```

想更新模型而不重建镜像时，挂卷比把权重打进镜像更好：

```bash
docker run -p 8000:8000 -e DECIS_API_KEY=change-me -v /srv/models:/models decis:laya
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
uv run pytest -q                        # 274 个测试，约 7 秒，无权重、无网络
uv run pytest -m weights                # 14 个加载真实 Laya 权重的测试
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
