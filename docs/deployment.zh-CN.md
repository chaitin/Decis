# 部署

[English](deployment.md) · **简体中文**

[文档索引](../README.zh-CN.md#文档) · [配置](configuration.zh-CN.md) · [快速开始](getting-started.zh-CN.md)

Decis **一个引擎一个镜像**，因为各引擎的依赖互相冲突，而且体积很大。每个以引擎为 tag 的镜像都
带着自己的 checkpoint，所以容器首次启动不需要网络、不需要卷，也不需要第二个服务。`-runtime`
变体与 `playground` 镜像是刻意的例外，下面会分别说明。

## 已发布的镜像

所有引擎共用一个 Docker Hub 仓库；引擎就是 tag：

| tag | 内容 | 压缩后体积 |
|---|---|---|
| `chaitin/decis:laya-multilingual`（= `:latest`） | 默认引擎及其 647 MiB checkpoint | 4.4 GB amd64 / 4.5 GB arm64 |
| `chaitin/decis:kev-0.8b` | kev 适配器及其 Qwen3.5 基座，已内置在镜像里 | 6.0 GB / 6.2 GB |
| `chaitin/decis:jeff-qwen3.5-0.8b` | Qwen3.5-0.8B 的 Jeff 微调，已内置在镜像里（1.61 GiB 权重） | 5.9 GB / 6.0 GB |
| `chaitin/decis:jeff-gemma4-e2b` | Gemma 4 E2B 的 Jeff 微调，已内置在镜像里（8.65 GiB 权重） | 17.7 GB / 17.9 GB |

那四个 Jeff 数字是 2026-09-30 工作流第一次构建这两个 tag 后，从已发布 tag 的 registry manifest
里读出来的。Gemma 那个如预期是这里最大的镜像：它的 checkpoint 本身就是 8.65 GiB 权重。每个 Jeff
引擎都是全权重微调，没有单独的基座仓库，所以一个目录里就是全部。**未实测**：这两个 tag 的容器从来
没起过，所以它们的冷启动与容器内加载都还是未知。两者也都会发布下面说的不带权重的 `jeff-*-runtime`
变体，但那个变体也没被拉下来跑过。

`chaitin/decis:playground` 是同一仓库里唯一的非引擎镜像，由同一个工作流构建并推送：三个网页小游戏，
以及挡在它们前面的那个代理——没有模型权重，Dockerfile 里也没有 `RUN`。`make build-playground`
会在几秒内从当前 checkout 构建出 `decis-local:playground`；源码目录里的 Compose 覆盖层用的就是
这个名字，它绝不会复用发布用的 tag，所以本地构建不会把它覆盖掉。

体积是压缩后的下载体积，由上面这些 tag 的 registry manifest 逐层求和得出。它们不属于
`benchmarks/results/`，`report.py` 也不会重新生成它们，所以把体积当成拉取大小的参考，而不是
基准。

```bash
docker run -p 8000:8000 -e DECIS_API_KEY=change-me chaitin/decis:laya-multilingual
```

`laya-multilingual` 是唯一还带一个不带后缀的 `latest` 的引擎 tag，所以 `docker pull chaitin/decis`
拿到的就是默认引擎。用户会拉的那些 tag——每个引擎 tag 和 `playground`——都是覆盖 `amd64` 与
`arm64` 的多架构 manifest。工作流还会把单架构的中间 tag（`<tag>-<arch>`、
`<tag>-sha-<short>-<arch>`）推到同一个仓库，那些不是多架构的。注册的 `laya`（英文）与
`laya-typed-decisions` checkpoint 没有镜像；这两个要在源码目录里跑。

release 会发布带版本的 tag，这些 tag 不会移动：推送一个 `v*` git tag 会为矩阵里的每个引擎生成
`<engine>-v<version>`——当前 release 是 `laya-multilingual-v0.4.0` 与 `kev-0.8b-v0.4.0`——以及
下面说的不带权重的 `-runtime-v<version>` 变体，还有该 tag
对应的 GitHub Release。引擎名那些 tag 正好相反——它们随每次推送到 `master` 移动，所以要钉住的
是带版本的 tag。

### 自带权重

想不构建镜像就跑另一个 checkpoint，挂一个已经有 `<engine-id>/` 的目录即可：

```bash
# /srv/models/laya-multilingual/multilingual/... 必须已经存在
docker run -p 8000:8000 -e DECIS_API_KEY=change-me -v /srv/models:/models chaitin/decis:laya-multilingual
```

> **不要把空目录挂到 `/models`。** 挂在那里会盖住镜像里已有的权重——命名卷只会被镜像内容初始化
> 一次，之后留自己的一份副本，而绑定挂载会直接替换掉整个目录。容器随后会悄悄下载你以为已经
> 有了的东西。`docker-compose.yml` 因此在那里不挂任何东西。

### 不带权重的镜像

如果部署要把权重只留一份放在共享卷上，或者必须让每个节点的镜像尽量小，release 还会发布不带
权重的变体，名为 `<engine>-runtime-v<version>`（3.2 GB amd64 / 3.3 GB arm64）。先把卷填一次，
然后运行：

```bash
docker pull chaitin/decis:laya-multilingual-runtime-v0.4.0
docker run --rm -v decis-models:/models chaitin/decis:laya-multilingual-runtime-v0.4.0 \
  decis download --engine laya-multilingual
docker run -p 8000:8000 -e DECIS_API_KEY=change-me -v decis-models:/models \
  chaitin/decis:laya-multilingual-runtime-v0.4.0
```

要在离线环境服务，就得先把卷填好：卷为空时 `paths.resolve` 会退回 Hub，容器在加载时下载权重。
这个 runtime 镜像确实装了引擎的依赖，所以那次下载能成功——它只是需要网络，而气隙部署没有网络。
`v0.4.0` 是当前版本，要钉住的就是带版本的 tag（见上面的 tag 规则）。

## Docker Compose

`docker-compose.yml` 只跑发布镜像，别的什么都不跑：没有预取容器、没有卷、也没有下载。profile
决定起哪个引擎，而 profile 名就是引擎 id。

```bash
cp .env.example .env                                # 设置 DECIS_API_KEY
docker compose -f docker-compose.yml up -d --wait   # laya-multilingual 在 :8000
docker compose --profile kev-0.8b up -d             # kev 在 :8001
docker compose up -d laya-multilingual              # 只起引擎，不起 playground
```

`--wait` 在引擎真的能作答时才返回：Compose 层的探针打 `/readyz`，所以它会一直等到 CPU 冷启动
结束（见[性能](performance.zh-CN.md)里的延迟表）。镜像自带的 `HEALTHCHECK` 仍然留在 `/healthz`，
因为存活探针不能在引擎加载期间失败。如果你不想用 `--wait`，轮询的那条命令在
[快速开始](getting-started.zh-CN.md#等待就绪)里。

按名字指定服务只会起那一个服务。引擎 profile 里还包含 playground，所以 `docker compose up`
会两个都起；不想要游戏时，用上面最后一条命令按名字选引擎。

`-f docker-compose.yml` 不能省，原因如下：在源码目录里，Compose 还会按文件名自动加载
`docker-compose.override.yml`，并用你工作区里的源码把同样的服务构建成 `decis-local:*`。想对着
自己的改动开发就别加 `-f`；部署只拷 `docker-compose.yml` 一个文件。

一个容器只跑一个引擎是刻意的：两个引擎同时驻留会占好几 GB 内存，所以默认 profile 只起一个。

## `make` 快捷方式

`make` 封装的是 Compose 文件，所以你不必重敲那些命令：

```bash
make help                    # 目标清单，以及本目录解析到的引擎

make up                      # 用本仓库源码跑 decis-local:*；缺什么才构建什么
make up-local                # 同上，但先重建：重新构建会把引擎的权重重新写进镜像
make down                    # 停止；镜像保留

make build                   # 当前 profile 要跑的每个镜像
make build-engine ENGINE=kev-0.8b
make build-engines           # 所有引擎镜像，不论当前是哪个 profile
make build-playground        # 几秒，因为那个镜像不安装任何东西
make up-engine    ENGINE=kev-0.8b
make up-playground           # 只起游戏页面，旁边接一个跑在任何地方的引擎
make restart                 # 重启正在运行的东西

make ps / logs / images / config   # 正在运行的东西，以及它们来自哪些镜像
make pull                    # 部署路径：拉发布的 chaitin/decis:* 镜像
make test / lint
```

`Makefile` 里不写任何镜像 tag、引擎 id 或构建参数：它去问 Compose。这就是 `make up` 与
`docker compose up` 不会各说各话的原因，也是 `COMPOSE_PROFILES=kev-0.8b make build-engine`
构建 kev 的原因——shell 能覆盖 `.env`（对 Compose 成立，对 `make` 也成立）。

## 构建镜像

```bash
docker build -f docker/Dockerfile \
  --build-arg DECIS_EXTRAS=laya \
  --build-arg DECIS_ENGINE=laya-multilingual \
  --build-arg DECIS_PREDOWNLOAD=laya-multilingual \
  -t decis:laya-multilingual .
```

| 构建参数 | 作用 |
|---|---|
| `DECIS_EXTRAS` | 要安装哪个引擎的 extra。不设置会得到纯 API 镜像：能启动，`/healthz` 与 `/v1/models` 有响应，但 `/readyz` 一直是 503。 |
| `DECIS_ENGINE` | 镜像被配置为服务哪个引擎。 |
| `DECIS_PREDOWNLOAD` | 要把哪个引擎的权重写进镜像。不设置会得到不带权重的变体。 |

在需要代理的网络上，还要把代理传给**构建**。`env_file` 只作用于容器，而 Docker 既不转发
`.env`，也不把 shell 里的 `HTTP_PROXY` 转发进 `RUN`——于是权重下载会以
`Network is unreachable` 失败，而依赖安装可能还是成功的：

```bash
docker compose build --build-arg HTTP_PROXY="$HTTP_PROXY" --build-arg HTTPS_PROXY="$HTTPS_PROXY" \
  --build-arg NO_PROXY="$NO_PROXY"
```

容器以非 root 用户运行，不需要任何外部服务——没有 Redis、没有 Postgres、没有 Celery——并且在
公开地址上没配 token 时拒绝启动。

## Kubernetes

两个探针，不能互换：

```yaml
livenessProbe:
  httpGet: { path: /healthz, port: 8000 }
  periodSeconds: 10
readinessProbe:
  httpGet: { path: /readyz, port: 8000 }
  periodSeconds: 5
  failureThreshold: 60        # 模型加载可能要约 2 分钟
resources:
  requests: { memory: 3Gi, cpu: "2" }   # 模型加载后常驻，不是一层薄 API
  limits:   { memory: 6Gi }
terminationGracePeriodSeconds: 30   # > DECIS_SHUTDOWN_GRACE_MS
```

- `/healthz` 表示“进程还活着”。只把它用于存活探针。
- `/readyz` 表示“这个实例能作答”。不要把存活探针指向它：冷启动会被看成崩溃循环。完整契约见
  [快速开始](getting-started.zh-CN.md#等待就绪)。
- 加载失败会返回 503 `{"status":"failed"}`，且不带 `retry-after`——这个状态在进程被替换前是
  终态，所以要对它告警，而不是重试。
- 上面的内存值是占位示例。引擎加载后是常驻的，峰值是权重文件的数倍，所以要按
  [性能](performance.zh-CN.md)里的实测表、按引擎分别定 request 与 limit。

## 安全说明

- 没配 token 时，服务在非回环地址上拒绝启动，除非显式设置 `DECIS_ALLOW_NO_AUTH=1`。见
  [认证](configuration.zh-CN.md#认证)。
- playground 在服务端附上你的 API key 并代理 `/v1/systemone`，所以**任何能访问它端口的人都能
  用这个模型**。这和把引擎端口直接发布出去是同一个决定，而且连 token 都不用问。在你不信任的
  主机上把它绑到回环：`DECIS_PLAYGROUND_HOST_PORT=127.0.0.1:8080`。
- 每个镜像 tag 都由 [`.github/workflows/docker-build.yml`](../.github/workflows/docker-build.yml)
  里的工作流构建。会推送的构建——推送到 `master`、发布 release tag、或手动 dispatch 时要求
  推送——还会带上 SBOM 与 provenance 证明；只构建不推送的运行（例如 pull request）两者都不
  请求，因为证明本身就要求推送。
