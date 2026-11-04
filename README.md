## ​<p align="center">OK影视、TVBox、影视仓配置文件，资源均来自于各路大神无私分享，如有侵权，请联系删除。🏅 <p align="center">

## 🔰本仓库订阅地址

多仓订阅地址：

- **订阅服务**：自动生成并对外提供 `tvbox.json`（单仓）/ `lmw.json`（多仓）/ `caiji.json`（采集源）
- **健康检测**：定时探测全部源（CMS 接口 / 外链 / 解析），区分 `ok` / `fail` / `skip`，报告记录明细
- **自动屏蔽**：连续失败 N 次自动移出配置；恢复后自动解封；支持白名单保护
- **Web 面板**：状态总览、条目管理（屏蔽/白名单/删除）、调度与范围、上游同步库、日志
- **上游自动同步**：按配置从上游 GitHub 仓库拉取最新内容（tarball 方式，容器内无需 git），ETag 去重跳过
- **双备份**：每次更新自动推送 GitHub + Gitee
- **配置自动发布**：面板/定时的每轮流水线结束后自动提交并推送 GitHub + Gitee（失败不阻塞）；用 Gitee raw 链接做订阅时配置随流水线自动更新
- **Releases 发布**：每次更新自动发布更新包 / Docker 镜像 / README 到 Releases
- **源池扩展**：自有条目池（own_sources.json）+ 外部配置源（feeds.json）自动合并，持续补充新源
- **一键清理失败源**：删除全部 fail 条目并记入移除清单（上游/外部源不会再补回），界面更精简
- **分类健康统计**：报告按 4K / 搜索 / 影视 / 机器人 / 云盘网盘 / 免费接口 / 采集 分类统计存活率

单仓订阅地址：

https://github.catvod.com/https://raw.githubusercontent.com/tushen6/Tomorrow/master/tvbox.json

- 面板：`http://<IP>:8899/`
- 订阅地址：`http://<IP>:8899/tvbox`、`/lmw`、`/caiji`（兼容旧的 `/tvbox.json` 等路径）
- 端口由 `SERVER_PORT` 环境变量控制（默认 8899，映射容器内 8080）

## ✨热门(TVBox、影视仓、OK影视)配置接口
- （1）网络接口 ： https://8815.kstore.vip/tvbox/wmz
- （2）真心：https://tvbox.catvod.com/FongMi.json
- （3）虎斑：http://hb.小虎斑.site:25252/仅供测试
- （4）饭太硬：https://www.饭太硬.cc/tv
- （5）肥猫：http://肥猫.net/tv
- （6）vox：http://rihou.cc:88/demo.php
- （7）小米：https://www.tangsan.fun/tv
- （8）摸鱼儿：https://6800.kstore.vip/fish.json
- （9）讴歌：https://欧歌.v.nxog.top/m
- （10）PG： https://tvbox.catvod.com/jsm.json
- （11）多多：https://yydsys.top/duo 
- （12）南风：https://gh-proxy.com/https://raw.githubusercontent.com/yoursmile66/TVBox/refs/heads/main/XC.json
- （13）王二小：https://9280.kstore.vip/newwex.json
- （14）东篱：https://16151.kstore.space
- （15）嗷呜：http://www.英格里希嗷呜.top/tv
- （16）潇洒：https://9877.kstore.space/one.json
 
## 🫶使用说明
- 所有资源均收集于互联网，仅供测试研究使用，切勿商用；
- 本仓库不存储任何的流媒体内容，所有的法律责任与后果应由使用者自行承担；


```sh
sh /path/to/Tomorrow/auto/nas_update.sh /path/to/update.bundle
```

脚本自动完成 8 步：快照运行数据 → 合并更新 → 重打 jar → 恢复运行数据 →（按需一次性重建镜像，使容器带 git）→ 推送 GitHub + Gitee → **发布 Release** → 重启容器（有 compose 时用 `docker compose up -d --no-build` 应用新镜像）。`RELEASE_PUBLISH=0` 可跳过发布环节。
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
内容未变（同一更新包且镜像未变化）时自动跳过发布，不会重复发 Release。

## 🖥 面板说明

打开 `http://<IP>:8899/`（旧地址 `/panel` 仍可用）：

- **状态总览**：运行中/空闲、上次结果、当前周期、自动屏蔽统计
- **调度与范围**：运行间隔（天，0=仅手动）+ 执行时间（点，如间隔 1 天、执行时间 10 点 = 每天 10:00 自动同步+重建）；自动周期始终按设定时间执行，手动运行不影响下一次自动执行时间；测活范围勾选（未勾选的组不测活/不自动屏蔽，输出仍全量重建）
- **上游同步库（GitHub）**：添加/停用/删除上游仓库，立即同步，查看同步日志；上游无变化时自动跳过（ETag 相同）
- **外部配置源（feeds）**：面板添加/启停/删除配置源（单个 tvbox.json URL），立即抓取并重建；合并条目参与测活/自动屏蔽
- **条目管理**：搜索、手动屏蔽/解封、加白名单、删除
- **清理失败源**：顶部按钮一键删除全部 fail 条目；被删条目记入 `auto/removed.json`（上游同步/外部源抓取都会跳过），在"提交新源"重新提交同 key 条目即可恢复；**已删除条目每轮同步会自动再测一次：上游/外部源仍携带且已恢复（pass）则自动取消删除并重新入库，仍失败则保持删除**；`removed.json` 已随运行时快照提交并推送到 GitHub/Gitee，NAS 重装/重克隆后不会丢失
- **日志**：流水线输出与同步日志

## 🧩 源池扩展（防止上游停更导致源池老化）

- `auto/own_sources.json`（随仓库提交）：**自有条目池**，每轮流水线自动合并进 sources.json（origin=own），不会被上游同步删除，参与测活与构建。格式：

```json
{"sites": [], "caiji": [], "parses": [{"name": "", "url": ""}], "repos": [{"url": "", "name": ""}]}
```

- `auto/feeds.json`：**外部配置源列表**（在面板"外部配置源"中管理：添加/启停/删除/立即抓取）。每轮流水线抓取指定的单个 tvbox.json（支持 gh-proxy），提取 sites/parses/repos 合并（origin=feed，测活/自动屏蔽照常生效）。**新条目入库前先测活，失败的不加入主源**（面板"调度与范围"的"外部源严格筛选"开关或 `--only-pass` 可切换严格模式：只收完全通过的可探测条目）。自动适配 qist 等"多文件"配置：`./lib/...`、`./jar/...` 等相对路径按配置 URL 展开为绝对地址（ghproxy 开启时自动加代理前缀，`$$$` 组合字段逐段处理），`csp_` 站点自动附加配置里的 spider 作为本条目的 `jar`（去掉 md5 后缀），预筛选/健康检查会按该 jar 扫描类名。内置示例（默认停用）：

```json
{"items": [{"name": "qist/tvbox 0825", "url": "https://raw.githubusercontent.com/qist/tvbox/master/0825.json", "ghproxy": true, "enabled": false}]}
```

- `auto/upstreams.json` 每条上游支持 `"jar": false`：同步该上游时**保留我方 tvbox.jar**（默认 true 即跟随上游 jar 并自动重打补丁）。
- 说明：外部配置里的相对路径（如 `./json/xxx.json`）在本仓库不存在，这类条目会走测活/自动屏蔽流程自愈。

## 📊 分类健康统计

`report.txt` / `report.json` 新增 `categories` 段：按 4K / 搜索 / 影视 / 机器人 / 云盘网盘 / 免费接口 / 采集 / 其他 统计 ok / fail / skip / 屏蔽数，便于判断哪类需要补源。

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
  upstream_sync.py     上游多库同步编排（ETag 去重，支持 jar=false 保留我方 jar）
  fetch_upstream.py    tarball 下载（免 git，支持 gh-proxy）
  feed_sync.py         源池扩展：外部配置源(feeds.json) + 自有条目池(own_sources.json)合并
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
- **面板/定时自动发布**：流水线跑完后容器内自动 `git add -A && commit && push`（origin + gitee，失败只打印不阻塞）。容器需要 git：`nas_update.sh` 会检测并在缺失时一次性重建镜像；compose 已挂载 `/root:/hostroot:ro` 读取推送凭据（与 Releases 发布一致）
- **面板设置存放**：`auto/settings.json`（运行时文件，不随更新覆盖）

## 🫶 免责声明

- 所有资源均收集于互联网，仅供测试研究使用，切勿商用
- 本仓库不存储任何流媒体内容，一切法律责任与后果由使用者自行承担
- 配置文件来源：[tushen6/Tomorrow](https://github.com/tushen6/Tomorrow)，感谢上游作者的无私分享
