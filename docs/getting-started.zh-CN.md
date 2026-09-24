# 快速开始

[English](getting-started.md) · **简体中文**

[文档索引](../README.zh-CN.md#文档) · [配置](configuration.zh-CN.md) · [API](api.zh-CN.md)

Decis 是一个自托管的 HTTP 服务端，用开源决策模型回答 TypeSafe System One 请求。本页带你从
clone 仓库走到第一个答案。部署见[部署](deployment.zh-CN.md)；线格式上的每个字段见 [API 参考](api.zh-CN.md)。

## 环境要求

| | |
|---|---|
| Python | 3.11 或更高版本，以及 [uv](https://docs.astral.sh/uv/) |
| 磁盘 | 默认引擎的权重约 650 MiB，另加 Python 环境 |
| 内存 | 加载后常驻几个 GiB，峰值 RSS 是权重文件的数倍。容器要按[性能](performance.zh-CN.md)里的实测数字来定，而不是按下载体积。 |
| GPU | 可选；本文的一切都在 CPU 上跑 |

Docker 是源码 checkout 之外的另一条路——已发布的镜像里已经带了权重。如果你不想装 Python，
见[部署](deployment.zh-CN.md)。

## 从源码运行

```bash
git clone https://github.com/chaitin/Decis && cd Decis
uv sync --extra dev --extra laya
cp .env.example .env                               # 设 DECIS_API_KEY=local，示例就是这么用的
uv run decis download --engine laya-multilingual   # 647 MiB，只需一次
uv run decis serve --host 127.0.0.1 --port 8000
```

token 填什么值都可以，但下面的示例发的都是 `local`，所以第一次运行就用它。

`laya-multilingual` 是默认引擎。`--host 127.0.0.1` 让服务只留在本机，而本机默认允许不带
token；第一次运行就该用它。要在网络上暴露服务，先设 `DECIS_API_KEY`，并读一遍
[认证](configuration.zh-CN.md#认证)。

当 `DECIS_MODEL_DIR` 未设置、权重也还没缓存时，`decis download` 是可选的——但先跑一次能把
“下载慢”和“加载慢”分开，第一次启动的日志会好读得多。

### 等待就绪

在 CPU 上，默认引擎加载约需 **75 秒**。整个过程里进程都能应答探针：`/healthz` 立刻可用，
`/readyz` 会说明当前在做什么。

```bash
curl -s localhost:8000/healthz   # {"status":"ok","version":"..."}
curl -s localhost:8000/readyz    # 503 {"status":"loading","engine":"laya-multilingual"}
```

轮询 `/readyz`，直到它返回 200：

```bash
until curl -fsS localhost:8000/readyz >/dev/null; do sleep 2; done
```

不要把*存活*探针指向 `/readyz`：一个在前 75 秒必然失败的探针，会让编排器反复重启一个本来
在正常工作的服务。存活用 `/healthz`，就绪用 `/readyz`。加载失败是终态——`/readyz` 返回
503、`"status":"failed"`，并且**不带** `retry-after`，因为对一个永久损坏的引擎反复重试只是
浪费时间。

## 发送一个请求

Decis 的意义在于：官方 SDK 察觉不到自己在跟别的东西说话。要改的只有 `base_url`：

```python
from typesafe_sdk import Choice, Noul, TypeSafeClient

# 不传 model=：SDK 会发它的默认值 "jev-latest"，Decis 用它实际在跑的引擎作答。
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

print(response.model)  # decis/laya-multilingual@<版本>
print(response.choices["department"].choice)  # criteria 里的某一个键
print(response.choices["department"].confidence)  # 0..1
print(response.nouls["churn_risk"].noul)  # P(true)，0..1
```

同一个请求用 `curl`：

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
  "model": "decis/laya-multilingual@<version>",
  "answers": {
    "department": { "type": "choice", "choice": "billing", "confidence": 0.87,
                    "probabilities": { "billing": 0.87, "technical": 0.13 } },
    "churn_risk": { "type": "noul", "noul": 0.62 }
  },
  "usage": { "input_tokens": 128, "output_tokens": 3 },
  // Decis 在契约之外加的所有东西都收在同一个命名空间下，官方 SDK 会忽略它。
  "decis": { "engine": "laya-multilingual", "device": "cpu", "dtype": "float32",
             "latency_ms": 231.4, "batch_size": 1 }
}
```

具体数值取决于引擎和它的权重，所以自己跑一遍命令看你的结果。
[`examples/curl.md`](../examples/curl.md) 逐个走完全部端点与三种问题原语，CI 会执行里面的
每一条命令。

## 检查本机能跑什么

```bash
uv run decis models     # 本机注册了哪些引擎、每个是否可用
uv run decis doctor     # 依赖、配置安全性、绑定地址、线程数
```

`decis models` 对每个引擎回答一个问题——本机能不能跑它——答案是 `ready`、`deps missing`、
`needs weights` 或 `unavailable`，并给出补救办法，例如 `uv sync --extra laya` 或
`decis download --engine laya-multilingual`。所以“依赖缺失”和“权重缺失”是两个不同的答案，
两者都不会被报成引擎坏了。引擎能接受的容量上限不在这里打印，而在
[`GET /v1/models`](api.zh-CN.md#get-v1models)。

## 下一步

| | |
|---|---|
| [配置](configuration.zh-CN.md) | 每个 `DECIS_*` 变量、认证、模型路径 |
| [API 参考](api.zh-CN.md) | 端点、请求与响应 schema、错误码 |
| [引擎](engines.zh-CN.md) | 每个引擎是什么、它的限制，以及如何新增一个 |
| [部署](deployment.zh-CN.md) | Docker、Compose、`make`、Kubernetes 探针 |
| [Playground](playground.zh-CN.md) | 三个调用实时引擎的浏览器小游戏 |
