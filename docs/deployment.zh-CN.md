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
| `kingfs/decis:laya-multilingual`（= `:latest`） | 默认引擎及其 647 MiB checkpoint | 4.4 GB amd64 / 4.5 GB arm64 |
| `kingfs/decis:kev-0.8b` | kev 适配器及其 Qwen3.5 基座，已烤进镜像 | 6.0 GB / 6.2 GB |

`kingfs/decis:playground` 是同一仓库里的第三个镜像，由同一个工作流构建并推送：三个网页小游戏，
以及挡在它们前面的那个代理——没有模型权重，Dockerfile 里也没有 `RUN`。`make build-playground`
会在几秒内从当前 checkout 构建出同一个 tag，源码目录里的 Compose 覆盖层走的就是这条路。

体积是压缩后的下载体积，由上面这些 tag 的 registry manifest 逐层求和得出。它们不属于
`benchmarks/results/`，`report.py` 也不会重新生成它们，所以把体积当成拉取大小的参考，而不是
基准。

```bash
docker run -p 8000:8000 -e DECIS_API_KEY=change-me kingfs/decis:laya-multilingual
```

`laya-multilingual` 是唯一同时拿到裸 `latest` 的 tag，所以 `docker pull kingfs/decis` 拿到的
就是默认引擎。每个 tag 都是覆盖 `amd64` 与 `arm64` 的多架构 manifest。注册的 `laya`（英文）
与 `laya-typed-decisions` checkpoint 没有镜像；这两个要在源码目录里跑。

release 还会发布带版本的 tag，这些 tag 不会移动：`laya-multilingual-v0.1.0` 与
`kev-0.8b-v0.1.0` 是 `v0.1.0` 发布的，`laya-multilingual-v0.0.1` 与 `kev-0.8b-v0.0.1` 是它
前一次发布的。带版本的 tag 在对应的 `v*` git tag 被推送时创建，而引擎名那些 tag 会随着每次
推送到 `master` 移动。

### 自带权重

想不构建镜像就跑另一个 checkpoint，挂一个已经有 `<engine-id>/` 的目录即可：

```bash
# /srv/models/laya-multilingual/multilingual/... 必须已经存在
docker run -p 8000:8000 -e DECIS_API_KEY=change-me -v /srv/models:/models kingfs/decis:laya-multilingual
```

> **不要把空目录挂到 `/models`。** 挂在那里会盖住烤进镜像的权重——命名卷只会被镜像内容初始化
> 一次，之后留自己的一份副本，而绑定挂载会直接替换掉整个目录。容器随后会悄悄下载你以为已经
> 有了的东西。`docker-compose.yml` 因此在那里不挂任何东西。

### 不带权重的镜像

如果部署要把权重只留一份放在共享卷上，或者必须让每个节点的镜像尽量小，release 还会发布不带
权重的变体，名为 `<engine>-runtime-<version>`（3.2 GB amd64 / 3.3 GB arm64）。先把卷填一次，
然后运行：

```bash
docker pull kingfs/decis:laya-multilingual-runtime-v0.1.0
docker run --rm -v decis-models:/models kingfs/decis:laya-multilingual-runtime-v0.1.0 \
  decis download --engine laya-multilingual
docker run -p 8000:8000 -e DECIS_API_KEY=change-me -v decis-models:/models \
  kingfs/decis:laya-multilingual-runtime-v0.1.0
```

卷必须在服务启动前填好：这个镜像没有任何可以退回去的东西。`v0.1.0` 是当前版本，
而钉住版本正是这个变体存在的意义——引擎名那些 tag 会随每次推送到 `master` 移动。

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
因为存活探针不能在引擎加载期间失败。如果你不想用 `--wait`：

```bash
until curl -fsS localhost:8000/readyz >/dev/null; do sleep 2; done
```

按名字指定服务只会起那一个服务。引擎 profile 里还包含 playground，所以 `docker compose up`
会两个都起；不想要游戏时，用上面最后一条命令按名字选引擎。

`-f docker-compose.yml` 不是装饰。在源码目录里，Compose 还会按文件名自动加载
`docker-compose.override.yml`，并用你工作区里的源码把同样的服务构建成 `decis-local:*`。想对着
自己的改动开发就别加 `-f`；部署只拷 `docker-compose.yml` 一个文件。

一个容器只跑一个引擎是刻意的：两个引擎同时驻留会占好几 GB 内存，所以默认 profile 只起一个。

## `make` 快捷方式

`make` 封装的是 Compose 文件，所以没有要重敲的东西：

```bash
make help                    # 目标清单，以及本目录解析到的引擎

make up                      # 用本仓库源码构建 decis-local:*，起一个引擎 + 游戏页面
make down                    # 停止；镜像保留

make build-playground        # 几秒：那个 Dockerfile 没有 RUN
make up-playground           # 只起游戏页面，旁边接一个跑在任何地方的引擎
make build-engine ENGINE=kev-0.8b
make up-engine    ENGINE=kev-0.8b

make ps / logs / images / config   # 正在运行的东西，以及它们来自哪些镜像
make pull                    # 部署路径：拉发布的 kingfs/decis:* 镜像
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
| `DECIS_PREDOWNLOAD` | 要把哪个引擎的权重烤进镜像。不设置会得到不带权重的变体。 |

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
- `/readyz` 表示“这个实例能作答”。把存活探针指向它，会让每次冷启动都变成重启循环。
- 加载失败会返回 503 `{"status":"failed"}`，且不带 `retry-after`——这个状态在进程被替换前是
  终态，所以要对它告警，而不是重试。
- 上面的内存值是占位示例。引擎加载后是常驻的，峰值是权重文件的数倍，所以要按
  [性能](performance.zh-CN.md)里的实测表、按引擎分别定 request 与 limit。

峰值内存是权重文件的数倍（见[性能](performance.zh-CN.md)）；内存上限要按实测来定，而不是按
下载体积。

## 安全说明

- 没配 token 时，服务在非回环地址上拒绝启动，除非显式设置 `DECIS_ALLOW_NO_AUTH=1`。见
  [认证](configuration.zh-CN.md#认证)。
- playground 在服务端附上你的 API key 并代理 `/v1/systemone`，所以**任何能访问它端口的人都能
  用这个模型**。这和把引擎端口直接发布出去是同一个决定，而且连 token 都不用问。在你不信任的
  主机上把它绑到回环：`DECIS_PLAYGROUND_HOST_PORT=127.0.0.1:8080`。
- 每个镜像 tag 都由 [`.github/workflows/docker-build.yml`](../.github/workflows/docker-build.yml)
  里的工作流构建，并带 SBOM 与 provenance 证明。
