# Decis

[English](README.md) · **简体中文**

[![CI](https://github.com/kingfs/Decis/actions/workflows/ci.yml/badge.svg)](https://github.com/kingfs/Decis/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Docker Pulls](https://img.shields.io/docker/pulls/kingfs/decis.svg)](https://hub.docker.com/r/kingfs/decis)

**一个 API，跑所有轻量决策模型。**

Decis 是一个体量很小、可以自托管的服务端，说的是 [TypeSafe 的 System One API](https://docs.typesafe.ai/api)——
也就是和 Jev 相同的 `/v1/systemone` 契约——并用你选定的开源决策模型来回答这些请求。把官方
`typesafe-sdk` 指向 Decis 而不是 `api.typesafe.ai`，其他什么都不用改。

*决策模型*不生成文本，而是针对有类型的问题返回校准过的概率。它只做一次前向，小到可以和应用
跑在一起，也快到可以放进请求路径。Decis 就是这些模型缺的那层服务：一套稳定契约，多种引擎，
一个引擎一个容器。

> **状态：v0.1.0。** 两个模型家族跑在同一份契约后面——`laya-multilingual`（默认）与
> `kev-0.8b`——另有 Laya 的英文与 typed-decisions checkpoint。线格式契约、认证、错误形状、
> 引擎抽象、权重解析、CLI、Docker 镜像与 CI 都已实现并有测试。契约建立在[官方 OpenAPI
> 快照](docs/contract/typesafe-openapi-0.2.0.json)和[线上 API 实际返回什么的记录](docs/contract/observations-2026-09-22.md)
> 之上，不是推断出来的。**没有做完的部分**写在
> [`docs/design-review.md`](docs/design-review.md) 里，而不是被省略。

## 快速开始

```bash
git clone https://github.com/kingfs/Decis && cd Decis
uv sync --extra dev --extra laya
cp .env.example .env                               # 设 DECIS_API_KEY=local，示例就是这么用的
uv run decis download --engine laya-multilingual   # 647 MiB，只需一次
uv run decis serve --host 127.0.0.1 --port 8000
```

`laya-multilingual` 是默认引擎，CPU 上加载约需 **75 秒**（[实测](docs/performance.zh-CN.md#延迟)）。
`/healthz` 立刻可用，`/readyz` 在模型能作答之前一直报 `loading`：

```bash
until curl -fsS localhost:8000/readyz >/dev/null; do sleep 2; done
```

官方 SDK 只需要改一行：

```python
from typesafe_sdk import Choice, Noul, TypeSafeClient

# 不传 model：SDK 会发它的默认值 "jev-latest"，Decis 用它实际加载的引擎作答。
# 迁移的全部工作量就是换掉 base_url。
client = TypeSafeClient(api_key="local", base_url="http://127.0.0.1:8000")

response = client.system_one(
    state={
        "subject": "Duplicate charge on invoice #4411",
        "body": "We were billed twice for March. Refund it today or we cancel our plan.",
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

print(response.choices["department"].choice)  # 你某个 criteria 的键
print(response.choices["department"].confidence)  # 0..1
print(response.nouls["churn_risk"].noul)  # P(true)，0..1
```

就绪语义、裸 `curl` 写法，以及 `decis models` / `decis doctor`，见
[快速开始](docs/getting-started.zh-CN.md)。

## 你能得到什么

- **jev 契约只实现一次。** `POST /v1/systemone`、`GET /v1/models`、`choice` / `score` /
  `noul` 三种原语、官方的错误形状与 request id 头。线格式在
  [`docs/api-compatibility.md`](docs/api-compatibility.md) 里逐条钉死，每条结论都标了证据等级。
- **可插拔的引擎。** 引擎只需给出每个选项的概率，服务端负责变成一致的 `Noul` / `Choice` /
  `Score` 答案，包括唯一一份公开的 `confidence` 公式。接一个新引擎不需要改归一化层的任何代码。
- **权重烤进镜像。** 以引擎为 tag 的那个镜像带着该引擎的 checkpoint，`docker run` 不需要网络、
  不需要卷、也不需要下载步骤。（`-runtime` 变体与 `playground` 镜像是例外，文档里会分别说明。）
- **默认安全。** Bearer 认证、常数时间比较、请求体上限，以及在公开地址上没配 token 时拒绝启动。

## 引擎

| 引擎 | 骨干 | 参数量 | 权重 | 说明 |
|---|---|---|---|---|
| `laya-multilingual` | mmBERT-base | 322M | 647 MiB | 100+ 语言；默认引擎 |
| `laya` | ModernBERT-large | 421M | 807 MiB | 英文 |
| `laya-typed-decisions` | ModernBERT-large | 421M | 807 MiB | 同仓库的 typed-decisions checkpoint |
| `kev-0.8b` | Qwen3.5-0.8B + LoRA + pointer head | 0.8B | 1.69 GiB | 只有 prefill；建议用 GPU |

注册表里每个引擎都是带真实权重的 checkpoint。契约测试用的无权重替身住在
[`tests/fixture_engine.py`](tests/fixture_engine.py)，并且**刻意不注册**到服务里。详见[引擎](docs/engines.zh-CN.md)。

## 性能

决策模型只做一次前向，所以常被引用的数字来自 Apple 芯片上 MLX 的小输入。那是真的，但不是
只有 CPU 的容器能做到的。下面这台是 **24 vCPU、没有 GPU 的 aarch64 机器**：

<!-- LATENCY:START -->

| 引擎 | 设备 | dtype | 线程 | 1 个问题 | 10 个问题 | 冷启动 | 峰值 RSS |
|---|---|---|---:|---:|---:|---:|---:|
| `laya` | cpu | float32 | 24 | 446 ms | 3,222 ms (322.1 ms/题) | 76.3 s | 2.8 GB |
| `laya-multilingual` | cpu | float32 | 24 | 231 ms | 987 ms (98.7 ms/题) | 72.9 s | 4.84 GB |

由 [`benchmarks/report.py`](benchmarks/report.py) 从 [`benchmarks/results/`](benchmarks/results/) 的原始 JSON 生成；**整行取自同一个配置**（torch 在本机的默认线程数，每个 vCPU 一个），样本 p50，单进程，仅请求内批处理。

**这是延迟，不是吞吐。** 每个请求的问题共享同一个 `state`，这是容易的情况。跨请求批处理此后也测过，结论是在 CPU 上**不提升吞吐**（见 [`docs/performance.zh-CN.md`](docs/performance.zh-CN.md) 与 [`docs/design-review.md`](docs/design-review.md) §4-M5）。

<!-- LATENCY:END -->

值得从表里读出两件事：一次问十个问题，每题的代价明显低于单独问一个；峰值内存是权重
文件的数倍。原本的吞吐主张，跨请求批处理，**测过，在 CPU 上不提升吞吐**；证据与上界见
[性能](docs/performance.zh-CN.md)。

## 部署

```bash
docker run -p 8000:8000 -e DECIS_API_KEY=change-me kingfs/decis:laya-multilingual
```

一个引擎一个镜像，所有引擎共用一个 Docker Hub 仓库，引擎就是 tag。以引擎为 tag 的每个镜像都是
多架构（`amd64` + `arm64`）并带着权重。在源码目录里，Compose 与 `make` 封装的是同一件事：

```bash
docker compose up -d --wait                         # 发布镜像，默认引擎
docker compose --profile kev-0.8b up -d             # 另一个引擎，宿主端口 8001

make help                    # 目标清单 + 本目录解析到的引擎
make up / down               # 用本仓库源码构建并启动，或停止
make build-playground        # 只重建游戏页面镜像：几秒
make up-playground           # 只起游戏页面，旁边接一个跑在任何地方的引擎
make pull                    # 部署路径：拉发布镜像
```

体积、挂卷的注意事项、不带权重的 `-runtime` 变体、Kubernetes 探针与代理构建，见
[部署](docs/deployment.zh-CN.md)。

## Playground

`docker compose up` 还会在 <http://localhost:8080> 起三个网页小游戏——贪吃蛇、恐龙、俄罗斯
方块。每个都可以用键盘自己玩，也可以交给模型：手动/AI 开关、推理面板和"最近一次调用"的控制台
在三个页面上是同一套，AI 档下每个决策都真的发一次 `/v1/systemone`。API key 留在 playground
服务端，页面永远拿不到；引擎也由它自己找到。见 [Playground](docs/playground.zh-CN.md)，
包括游戏改编自哪些项目。


## 文档

| 文档 | 内容 |
|---|---|
| [快速开始](docs/getting-started.zh-CN.md) | 从 clone 到第一个答案、就绪语义、CLI |
| [API 参考](docs/api.zh-CN.md) | 端点、Schema、原语、错误码、容量上限 |
| [API Schema](docs/schema/) | 生成的 JSON Schema 与服务端 OpenAPI 文档 |
| [配置](docs/configuration.zh-CN.md) | 全部 `DECIS_*` 变量、认证、权重路径、精度 |
| [引擎](docs/engines.zh-CN.md) | 每个引擎是什么、它的上限、如何新增 |
| [部署](docs/deployment.zh-CN.md) | Docker、Compose、`make`、Kubernetes |
| [性能](docs/performance.zh-CN.md) | 实测延迟、内存、精度与批处理结果 |
| [Playground](docs/playground.zh-CN.md) | 三个游戏、代理架构与致谢 |
| [线格式契约](docs/api-compatibility.md) | 精确的 jev 契约，每条结论带证据等级 |
| [设计](docs/design.md) | 架构、引擎抽象、打包 |
| [设计审查](docs/design-review.md) | 自我审查：已修缺陷、方法论局限、尚未验证的部分 |
| [可行性](docs/feasibility.md) | 调查结果与风险登记 |
| [`examples/`](examples/README.md) | 可运行的 `curl` 与官方 SDK 示例，CI 逐条执行 |
| [AGENTS.md](AGENTS.md) | 面向贡献者与 agent 的工程契约 |

如果只读一份文档再动手，读设计审查。它列出了已发现的缺陷和仍未回答的问题。

线格式契约、设计、设计审查与可行性研究用中文写成——它们就是在这个语言里调研和评审的。
契约本身不锁在散文里：[`docs/schema/`](docs/schema/) 由 `src/decis/schema.py` 生成并进 CI，
[API 参考](docs/api.md) 就是它的英文版本。

## 与其他项目的关系

Decis 不训练模型。它只做服务，并且尽量致谢而不是重复造轮子。

- **[Jev](https://docs.typesafe.ai/introduction)**（TypeSafe AI）——本项目对标的闭源模型。
  Decis 复刻的是它的*接口*，不是模型。
- **[kev](https://github.com/jaredpalmer/kev)**（Jared Palmer）——基于 Qwen3.5 的 Jev 风格决策
  模型。Decis 复用了 kev 的推理内核（vendor 固定版本，Apache-2.0，署名见 [`NOTICE`](NOTICE)），
  并把服务层推广到多个引擎。
- **[Laya](https://huggingface.co/convaiinnovations/laya)**（Convai Innovations）——Apache-2.0，
  多语言，一次前向。Decis 把官方 `laya` 包作为引擎使用。
- **[UniTS-Hub](https://github.com/kingfs/UniTS-Hub)**——Decis 沿用的多模型容器构建方式。
- **[djev-run](https://github.com/taeold/djev-run)**（Daniel Lee）——playground 的游戏改编自它；
  见 [Playground](docs/playground.zh-CN.md#致谢)。

## 参与贡献

欢迎贡献。[`CONTRIBUTING.md`](CONTRIBUTING.md) 写了开发环境、两套测试环境与 PR 规则；
[`AGENTS.md`](AGENTS.md) 是**规范性的工程契约**——唯一事实来源、不变量与禁用清单——对人和
自动化贡献者同样有效。

```bash
uv sync --extra dev
uv run pytest -q            # 无权重的那套：CI 跑这个
uv run ruff check && uv run ruff format --check
```

安全问题请走 [`SECURITY.md`](SECURITY.md)，不要开公开 issue。

## 许可证

Apache-2.0。第三方署名见 [`NOTICE`](NOTICE)。
