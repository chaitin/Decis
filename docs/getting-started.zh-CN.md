# 快速开始

[English](getting-started.md) · **简体中文**

[文档索引](../README.zh-CN.md#文档) · [配置](configuration.zh-CN.md) · [API](api.zh-CN.md)

Decis 是一个自托管的 HTTP 服务端，用开源决策模型回答 TypeSafe System One 请求。本页带你从
clone 仓库走到第一个答案。部署见[部署](deployment.zh-CN.md)；线格式上的每个字段见 [API 参考](api.zh-CN.md)。

## 环境要求

| | |
|---|---|
| Python | 3.11 或更高版本，以及 [uv](https://docs.astral.sh/uv/)。两个 Jeff 引擎需要 3.12+；`decis models` 会直接这么说，而不是报成缺依赖。 |
| 磁盘 | 默认引擎的权重约 650 MiB，另加 Python 环境 |
| 内存 | 加载后常驻几个 GiB，峰值 RSS 是权重文件的数倍。容器要按[性能](performance.zh-CN.md)里的实测数字来定，而不是按下载体积。 |
| GPU | 可选；有 GPU 会自动探测并使用，见[使用 GPU](#使用-gpu) |

Docker 是源码 checkout 之外的另一条路——已发布的镜像里已经带了权重。如果你不想装 Python，
见[部署](deployment.zh-CN.md)。

## 从源码运行

```bash
git clone https://github.com/chaitin/Decis && cd Decis
uv sync --all-extras
cp .env.example .env                               # 设 DECIS_API_KEY=local，示例就是这么用的
uv run decis download --engine laya-multilingual   # 647 MiB，只需一次
uv run decis serve --host 127.0.0.1 --port 8000
```

引擎依赖都在 extras 里，`--all-extras` 会把所有引擎和开发工具一起装上。只服务一个引擎只需要
它的 extra（`uv sync --extra dev --extra laya`）；不带任何 extra 的 `uv sync` 一个都不点名，而且会删掉
上一次 sync 装上的引擎依赖。见[引擎](engines.zh-CN.md#安装引擎)。

token 填什么值都可以，但下面的示例发的都是 `local`，所以第一次运行就用它。

`laya-multilingual` 是默认引擎。`--host 127.0.0.1` 让服务只留在本机，而本机默认允许不带
token；第一次运行就该用它。要在网络上暴露服务，先设 `DECIS_API_KEY`，并读一遍
[认证](configuration.zh-CN.md#认证)。

当 `DECIS_MODEL_DIR` 未设置、权重也还没缓存时，`decis download` 是可选的——但先跑一次能把
“下载慢”和“加载慢”分开，第一次启动的日志会好读得多。

### 使用 GPU

不需要配置：加载时引擎会问机器能用哪个设备，顺序是 `cuda`、`xpu`、`npu`、`mps`、`cpu`，取第一个可用的。
`decis doctor` 会打印它查到的结果。还有两件事会让 GPU 闲着，它两件都会点名。

**wheel 本身。** PyTorch 为每种加速器发布不同的构建，而 PyPI 上的 Windows wheel 完全没有 CUDA 支持——
这是 wheel 的属性，不是你硬件的属性。CPU-only 构建报“没有 CUDA 设备”的方式，和一台没有 N 卡的机器
一模一样，所以进程里没有任何东西能把两者分开。因此本仓库在 Windows 上把 `torch` 指向 PyTorch 的 CUDA
index（`pyproject.toml` 的 `[tool.uv.sources]`），`uv sync --all-extras` 在那里装到的就是 CUDA 构建；
Linux 从 PyPI 拿到的本来就带 CUDA，macOS 拿到的是 Metal 版。代价是真实的：那个 wheel 约 1.9 GiB，
而 CPU-only 那个是 124 MiB。在一台没有 N 卡的 Windows 上，把小 wheel 换回来：

```bash
uv sync --all-extras --no-sources     # 忽略 tool.uv.sources：回到原来的 PyPI wheel
```

**驱动。** 这里 pin 的通道是 CUDA 13.0，需要同一代的 NVIDIA 驱动。驱动更老时 `torch` 会报没有可用设备；
`decis doctor` 会把这种情况和“CPU-only wheel”分开，并打印它看到的驱动版本。uv 能自己按驱动挑通道，
但只作用于它的 `uv pip` 接口，所以之后的一次 `uv sync` 或 `uv run` 会把 pin 住的构建装回来：

```bash
uv pip install --torch-backend=auto --reinstall torch
```

`DECIS_DEVICE=cuda`（或 `mps`、`xpu`、`npu`、`cpu`）用来钉住某个设备，而不是取最可用的那个——
当一次比较必须固定设备时就是它。机器给不出被钉住的设备时，日志会说明并回退。

### 等待就绪

在 CPU 上，默认引擎加载约需 **75 秒**；更慢的机器或 ARM 主机上可能要几分钟。整个过程里
进程都能应答探针：`/healthz` 立刻可用，`/readyz` 会说明当前在做什么。

`decis serve` 在绑定端口之前先打印它要做的事——引擎、权重来自哪里、绑定地址——随后在日志里
说明引擎**还没就绪**：

```
decis 0.4.0
  engine    laya-multilingual
  weights   convaiinnovations/laya/multilingual@1c5edc17a7acd8701df6fc341c0d179f1c62c982
            fetched on first use -- from Hugging Face, or from ModelScope when that
            cannot be reached; 646.8 MiB on a cold cache
  bind      127.0.0.1:8000
  startup   the socket opens first, so a probe can tell "starting" from "crashed":
            /readyz returns 503 and every /v1/* request is refused until the log says
            "engine laya-multilingual ready". `decis serve --preload` loads first instead.
```

所以 "Uvicorn running on http://127.0.0.1:8000" 的含义是**端口开了**，不是**模型能答**。
在引擎就绪之前，`/v1/*` 的请求一律被拒（503）。

```bash
curl -s localhost:8000/healthz   # {"status":"ok","version":"..."}
curl -s localhost:8000/readyz    # 503 {"status":"loading","engine":"laya-multilingual"}
```

轮询 `/readyz`，直到它返回 200：

```bash
until curl -fsS localhost:8000/readyz >/dev/null; do sleep 2; done
```

如果你不想盯着这个日志（在控制台前、也没有探针在等），`decis serve --preload` 会先取权重并
加载引擎，**然后**才打开端口：

```bash
uv run decis serve --host 127.0.0.1 --preload
```

这时端口就意味着"能答了"，日志的顺序也与此一致。代价是明确的：加载期间什么都不应答，
`/healthz` 也不例外，所以**不要**在冷启动期间需要存活探针拿到应答的地方用它——容器里、
编排器下，保持默认。

不要把*存活*探针指向 `/readyz`：一个在前 75 秒必然失败的探针，会让编排器反复重启一个本来
在正常工作的服务。存活用 `/healthz`，就绪用 `/readyz`。加载失败是终态——`/readyz` 返回
503、`"status":"failed"`，并且**不带** `retry-after`，因为对一个永久损坏的引擎反复重试只是
浪费时间。

## 发送一个请求

Decis 的意义在于：官方 SDK 除了 `base_url` 之外不需要任何改动：

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
uv run decis engines    # 这个构建出厂带了什么：id、别名、extra、Python 下限，
                        # 以及 `decis download` 会去哪个 Hub 仓库取它的权重
uv run decis models     # 本机注册了哪些引擎、每个是否可用
uv run decis doctor     # 依赖、配置安全性、绑定地址、线程数、本机下载权重会走哪个 Hub，
                        # 以及 compute 一节：选中的设备、torch 构建、驱动报告的 GPU
```

`decis engines` 是出厂目录，在任何机器上都是同一份：`--engine` 与请求的 `model` 接受哪些 id、
每个引擎的权重来自哪里。`decis models` 则是**这台机器**的结论，对每个引擎回答一个问题——本机能不能跑它——答案是 `ready`、`deps missing`、
`needs weights`、`no weights`、`needs Python 3.12+` 或 `unavailable`，并给出补救办法，例如
`uv sync --extra laya` 或 `decis download --engine laya-multilingual`。所以缺依赖、权重从没下载过、
指到的目录不是合法 checkpoint、解释器低于某个引擎的下限，都是各不相同的答案，哪一个都不会被报成
引擎坏了。引擎能接受的容量上限不在这里打印，而在
[`GET /v1/models`](api.zh-CN.md#get-v1models)。

`decis doctor` 管的是另一半：它讲机器而不是引擎。它的 `compute` 一节打印本机会用哪个设备、
装的是哪个 `torch` 构建、`nvidia-smi` 报出了什么——当这三者互相矛盾时（正是
[使用 GPU](#使用-gpu) 描述的那种情况），再多打一行 `advice`。

## 下一步

| | |
|---|---|
| [配置](configuration.zh-CN.md) | 每个 `DECIS_*` 变量、认证、模型路径 |
| [API 参考](api.zh-CN.md) | 端点、请求与响应 schema、错误码 |
| [引擎](engines.zh-CN.md) | 每个引擎是什么、它的限制，以及如何新增一个 |
| [部署](deployment.zh-CN.md) | Docker、Compose、`make`、Kubernetes 探针 |
| [Playground](playground.zh-CN.md) | 三个调用实时引擎的浏览器小游戏 |
