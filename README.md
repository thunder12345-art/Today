# istoreos_tvbox — TVBox / OK影视 配置自托管与自动运维

基于 [tushen6/Tomorrow](https://github.com/tushen6/Tomorrow) 的配置数据，在自有 NAS（iStoreOS + Docker）上长期稳定运行：
聚合配置订阅 + 定时健康检测 + 失效源自动屏蔽 + Web 面板运维，每次更新自动备份（GitHub + Gitee）并发布 Releases。

## ✨ 功能特性

- **订阅服务**：自动生成并对外提供 `tvbox.json`（单仓）/ `lmw.json`（多仓）/ `caiji.json`（采集源）
- **健康检测**：定时探测全部源（CMS 接口 / 外链 / 解析），区分 `ok` / `fail` / `skip`，报告记录明细
- **自动屏蔽**：连续失败 N 次自动移出配置；恢复后自动解封；支持白名单保护
- **Web 面板**：状态总览、条目管理（屏蔽/白名单/删除）、调度与范围、上游同步库、日志
- **上游自动同步**：按配置从上游 GitHub 仓库拉取最新内容（tarball 方式，容器内无需 git），ETag 去重跳过
- **双备份**：每次更新自动推送 GitHub + Gitee
- **Releases 发布**：每次更新自动发布更新包 / Docker 镜像 / README 到 Releases

## 🚀 快速部署（NAS / Linux + Docker）

```sh
git clone https://github.com/thunder12345-art/Today.git
cd Today
docker compose up -d --build
```

- 面板：`http://<IP>:8899/panel`
- 订阅地址：`http://<IP>:8899/tvbox.json`、`/lmw.json`、`/caiji.json`
- 端口由 `SERVER_PORT` 环境变量控制（默认 8899，映射容器内 8080）

## 🔄 更新部署（日常维护）

开发机提交代码后生成累积更新包 `update.bundle`，在 NAS 上执行：

```sh
sh /path/to/Tomorrow/auto/nas_update.sh /path/to/update.bundle
```

脚本自动完成 8 步：快照运行数据 → 合并更新 → 重打 jar → 恢复运行数据 → 推送 GitHub + Gitee → **发布 Release** → 重启容器。
`RELEASE_PUBLISH=0` 可跳过发布环节。

也可以直接从 Releases 下载最新更新包（无需开发机）：

```sh
wget https://github.com/thunder12345-art/Today/releases/latest/download/update.bundle -O /tmp/update.bundle
sh /path/to/Tomorrow/auto/nas_update.sh /tmp/update.bundle
```

## 📦 Releases 资产说明

| 文件 | 说明 |
|---|---|
| `update.bundle` | 累积 git 更新包（含全部提交，可跳过中间版本直接部署，配合 nas_update.sh 使用） |
| `tomorrow-auto-image.tar.gz` | Docker 镜像离线包（构建机架构，NAS 为 ARM64；`docker load -i` 导入） |
| `README.md` | 本文档（同时作为 Release 说明正文） |

镜像仅在 Dockerfile / 基础镜像变化时才会上传，日常更新只需 `update.bundle`。

## 🖥 面板说明

打开 `http://<IP>:8899/panel`：

- **状态总览**：运行中/空闲、上次结果、当前周期、自动屏蔽统计
- **调度与范围**：运行间隔（天，0=仅手动）+ 执行时间（点，如 `1` 天 `10` 点 = 每天 10:00 自动同步+重建）；测活范围勾选（未勾选的组不测活/不自动屏蔽，输出仍全量重建）
- **上游同步库（GitHub）**：添加/停用/删除上游仓库，立即同步，查看同步日志；上游无变化时自动跳过（ETag 相同）
- **条目管理**：搜索、手动屏蔽/解封、加白名单、删除
- **日志**：流水线输出与同步日志

## ⚙️ 环境变量（docker-compose.yml）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SERVER_PORT` | 8899 | 映射到宿主机的端口 |
| `AUTO_BLOCK` | 1 | 自动屏蔽总开关 |
| `AUTO_BLOCK_AFTER` | 2 | 连续失败几次后自动屏蔽 |
| `CHECK_WORKERS` | 12 | 检测并发数 |
| `CHECK_TIMEOUT` | 8 | 单目标检测超时（秒） |
| `BUILD_SCOPE` | all | 构建范围（local=仅本地源 / all=含投稿源） |
| `DROP_DEAD` | 0 | 构建时是否直接剔除检测失败的源 |
| `RUN_ON_START` | 1 | 容器启动时跑一次流水线 |
| `PANEL_TOKEN` | 空 | 面板访问令牌（空=免密） |

## 📁 目录结构（auto/ 工具链）

```
auto/
  server.py            面板 + 调度服务（容器入口，默认端口 8080）
  check.py             健康检测：ok/fail/skip + 自动屏蔽策略
  build.py             由 sources.json 生成 tvbox.json / lmw.json / caiji.json
  submit.py            投稿入库
  upstream_sync.py     上游多库同步编排（ETag 去重）
  fetch_upstream.py    tarball 下载（免 git，支持 gh-proxy）
  patch_jar.py         tvbox.jar 等长补丁（幂等）
  nas_update.sh        NAS 更新部署脚本（8 步全自动）
  publish_release.py   Releases 发布（GitHub + Gitee API）
  sources.json         源数据库（含运行状态元数据）
  panel.html           面板页面
```

## ❓ 常见问题

- **健康检测 skip**：该条没有可探测的 HTTP 目标（纯 jar 内部源，只有 APP 能验证），不算失败、不会被屏蔽
- **上游同步 skip**：上游仓库没有新提交（文件指纹未变），跳过下载，属正常
- **推送凭据**：在部署机上一次性配置——GitHub 用 PAT（`ghp_...`）、Gitee 用私人令牌（当密码用），随后 `git config credential.helper store` 保存；`nas_update.sh` 会自动读取 `/root/.git-credentials` 用于 Releases 发布
- **面板设置存放**：`auto/settings.json`（运行时文件，不随更新覆盖）

## 🫶 免责声明

- 所有资源均收集于互联网，仅供测试研究使用，切勿商用
- 本仓库不存储任何流媒体内容，一切法律责任与后果由使用者自行承担
- 配置文件来源：[tushen6/Tomorrow](https://github.com/tushen6/Tomorrow)，感谢上游作者的无私分享
