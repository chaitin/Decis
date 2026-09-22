# Decis

[English](README.md) · **简体中文**

**一个 API，跑所有轻量决策模型。**

Decis 是一个体量很小、可以自托管的服务端，说的是 [TypeSafe 的 System One API](https://docs.typesafe.ai/api)，也就是和 Jev 相同的 `/v1/systemone` 契约，并用你选定的开源决策模型来回答这些请求。把官方 `typesafe-sdk` 指向 Decis 而不是 `api.typesafe.ai`，其他什么都不用改。

> **状态：设计阶段。** 还没有写代码。[`docs/design.md`](docs/design.md) 描述目标的实现方案；[`AGENTS.md`](AGENTS.md) 是把它做出来时的工程契约；[`docs/design-review.md`](docs/design-review.md) 是对这套设计的对抗性审查，包括哪些部分还没有被验证。契约不是推断出来的，而是建立在[官方 OpenAPI 快照](docs/contract/typesafe-openapi-0.2.0.json)和[线上 API 实际返回什么的记录](docs/contract/observations-2026-09-22.md)之上。

---

## 问题

Jev 已经证明，*决策模型*（针对一段 state 回答带类型的问题，返回校准过的概率，而不是生成文本）应该放在请求路径上，而不是放在聊天窗口里。但 Jev 是托管、闭源的 API：

- **延迟。** 独立实测每题 p50 为 236–276 ms。如果你的决策只是一次路由判断，那这个网络往返就是每次调用都要付的成本。
- **数据。** 你的工单、发票、用户消息都要离开你自己的基础设施。
- **成本。** 每十亿输入 token 42 美元，永久如此。

现在已经有开源的替代品，小到可以和应用跑在一起。缺的不是模型，而是**统一的对外服务方式**。

Decis 就是这种方式：一个稳定的 API，多个引擎，每个模型打成一个容器。

## 你能得到什么

- **jev 契约只实现一次。** `POST /v1/systemone`、`GET /v1/models`、`choice` / `score` / `noul` 三种原语、官方 SDK 的错误形状和 request-id 头。线格式固定在 [`docs/api-compatibility.md`](docs/api-compatibility.md) 里，每条结论都标了证据等级。
- **引擎可插拔。** 引擎只需要给出每个选项的概率；服务端负责把它转成 `Noul` / `Choice` / `Score` answer，所以每个引擎返回的形状完全一致、可以互相比较，`confidence` 也只有一处有文档的定义。
- **为高频小请求而做。** 两个开源决策模型都只有一次前向，所以 Decis 在问题送到模型之前会*跨请求*合并成批。这就是“一个 FastAPI 封装”和服务端之间的区别。
- **权重可以打进镜像，也可以挂卷。** 镜像自带模型，`docker run` 离线就能跑；`DECIS_MODEL_DIR` 可以用你自己的目录覆盖。

## 引擎

| 引擎 | 骨干模型 | 参数量 | 权重 | 说明 |
|---|---|---|---|---|
| `laya-multilingual` | mmBERT-base | 322M | 614 MiB | 支持 100+ 种语言，快约 2.2 倍，最好的默认选择 |
| `laya` | ModernBERT-large | 421M | 804 MiB | 英语，在英语基准上最强 |
| `laya-typed-decisions` | ModernBERT-large | 421M | 804 MiB | 针对 typed-decisions 工作流调过 |
| `kev-0.8b` | Qwen3.5-0.8B + LoRA | 0.8B | ~1.7 GB | 架构不同，错误分布也不同 |
| `kev-4b` / `kev-9b` | Qwen3.5 + LoRA | 4B / 9B | ~8 GB / ~18 GB | 精度更高，但已经不算“轻量” |
| `remote` | — | — | — | 转发到真正的 `api.typesafe.ai`，可用于 A/B 对比，也可当测试基准 |

新增一个引擎就是一个模块加一行注册。见 [`AGENTS.md §5`](AGENTS.md)。

## 性能

决策模型只做一次前向，所以大家最常引用的漂亮数字来自 **Apple 芯片上用 MLX 跑的小而短的输入**：M3 Max 上每题约 11–18 ms。这是真实的，但只有 CPU 的容器做不到。下面是在**一台 24 vCPU、没有 GPU 的 aarch64 机器**上实测的结果——最差的情况，也是大多数人会先试的情况：

| 引擎 | 冷启动 | 峰值 RSS | 1 个问题 | 10 个问题 |
|---|---:|---:|---:|---:|
| `laya-multilingual`（322M） | 73 s | 4.84 GB | 201 ms | 987 ms — **98.7 ms/题** |
| `laya`（英语，421M） | 76 s | 2.80 GB | 432 ms | 3222 ms — 322 ms/题 |
| `kev-0.8b`（fp32，3 个问题） | 15 s | — | — | 1656 ms — 552 ms/题 |

这张表里有三点值得记住：

- **批处理是杠杆。** 一次调用问十个问题，每题的代价大约是单独问一个问题的一半。Decis 是跨请求成批，不只是在一个请求内部。
- **冷启动约 75 秒**，峰值内存是权重文件的 3–7 倍。readiness 探针和容器内存要按这个来规划。
- **`kev-0.8b` 需要 GPU。** 它的 Qwen3.5 骨干要跑得快就得用 `flash-linear-attention` 和 `causal_conv1d`，而这两个都依赖 Triton/CUDA。在 CPU 上它比 Laya 慢一个数量级。它在 CPU 上的 `bf16` 路径又比 `fp32` 慢 **83 倍**——所以 Decis 按引擎*和*设备分别选 dtype，并且会给出警告，而不是悄悄慢下去。

每个样本的原始输出、完整的命令和机器配置都在 [`benchmarks/results/`](benchmarks/results/) 里。文档里引用的每个数字都来自那里。

## 用法

```bash
docker run -p 8000:8000 ghcr.io/kingfs/decis:laya-multilingual-latest
```

```python
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

client = TypeSafeClient(api_key="local", base_url="http://127.0.0.1:8000", model="laya-multilingual")

response = client.system_one(
    state={"subject": "Duplicate charge on invoice #4411",
           "body": "We were billed twice for March. Please refund the duplicate today or we will cancel our plan."},
    questions={
        "department":  Choice(instructions="Which team should handle this?",
                              criteria={"billing": "invoices, payments, refunds",
                                        "technical": "bugs, outages, system errors",
                                        "sales": "pricing, new contracts"}),
        "urgency":     Score(instructions="How urgent is this?",
                             criteria=["not urgent", "soon", "critical deadline or blocking issue"]),
        "churn_risk":  Noul(instructions="Does the user threaten to cancel or leave?"),
    },
)

print(response.choices["department"].choice)      # billing
print(response.scores["urgency"].score)           # 1.84
print(response.nouls["churn_risk"].noul)          # 0.89
```

或者直接手动调：

```bash
curl -s localhost:8000/v1/systemone -H 'content-type: application/json' -d '{
  "state": "We were billed twice for March. Please refund the duplicate today.",
  "model": "laya-multilingual",
  "questions": {
    "department": {"type": "choice", "instructions": "Which team should handle this?",
                   "criteria": {"billing": "invoices, payments, refunds",
                                "technical": "bugs, outages, system errors"}},
    "churn_risk": {"type": "noul", "instructions": "Does the user threaten to cancel?"}
  }}'
```

```jsonc
{
  "model": "decis/laya-multilingual@0.3.5",
  "answers": {
    "department": { "type": "choice", "choice": "billing", "confidence": 0.71,
                    "probabilities": { "billing": 0.85, "technical": 0.15 } },
    "churn_risk": { "type": "noul", "noul": 0.89 }
  },
  "usage": { "input_tokens": 96, "output_tokens": 21 }
}
```

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
uv sync --extra server --extra laya
uv run pytest -q           # no model weights needed
uv run decis serve
```

## 许可证

Apache-2.0。第三方署名见 [`NOTICE`](NOTICE)。
