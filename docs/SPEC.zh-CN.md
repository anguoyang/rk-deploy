# rk-deploy — 规范 v0.1（2026-11-02 冻结）

rk-deploy 是面向 Rockchip 板卡上 RKNN 应用的部署与小规模设备集群管理工具
（RK3588 / RK3576 / RK356x）。完整流程如下：

```
manifest → pack → push over SSH → install as systemd service
         → fleet roll-out / version rollback / status report
```

适用对象：显示屏厂商、停车场 / 安防集成商的交付与运维工程师。他们需要将同一个应用部署到
5–50 块板卡上。

与 rk-edge-doctor 的关系：doctor 负责只读诊断（板卡 / 模型 / 摄像头是否健康？）；
rk-deploy 负责写入和部署（将应用部署到板卡上并管理其版本）。两者的职责不重叠：
rk-deploy 的 status 只检查由它自己部署的应用。

## 设计原则

1. **只处理通用应用包。** 本工具完全不了解它所部署的应用。模型文件只作为不透明字节
   进行传输——不会打开、解析或检查其内容（只验证整个应用包的哈希值）。
2. **控制端智能，板卡端精简。** 所有解析和渲染决策都在控制端完成。板卡端只运行
   POSIX `sh` 生命周期脚本以及系统自带的 coreutils / systemd；不需要安装额外软件包，
   也不会在板卡上解析 YAML（不同 BSP 所使用的 Python 版本和库差异太大，见下文已验证的
   发现 #1）。
3. **版本目录不可变，切换保持原子性。** 每个版本都解压到自己的目录中，且不会被覆盖。
   切换到线上运行版本时，只需切换符号链接。
4. **数据的生命周期长于应用版本。** 配置、数据和板卡本地密钥都存放在版本目录之外的
   `shared/` 中；升级和回滚都不会触碰这些内容。
5. **失败时自动回滚。** 部署后的健康检查失败时，默认恢复到之前的版本，并明确报告结果。
   不会让板卡停留在不完整的部署状态。
6. **不收集凭据。** 工具只使用用户现有的 SSH 配置、密钥和 agent。它不会询问或存储密码。
7. **离线运行，不进行遥测、注册或许可证检查。**
8. 许可证为 Apache-2.0；CLI 命令名称为 `rkdeploy`。

## CLI

```
rkdeploy pack     [--manifest rkdeploy.yaml] [--out dist/]
rkdeploy deploy   --fleet fleet.yaml <package.tar.gz> [--hosts tag:x|name,...] [--jobs 4] [--no-auto-rollback]
rkdeploy rollback --fleet fleet.yaml --app <name> [--to <version>] [--hosts ...]
rkdeploy uninstall --fleet fleet.yaml --app <name> [--hosts ...] [--jobs 4] [--allow-last-copy] [--purge]
rkdeploy status   --fleet fleet.yaml --app <name> [--json report.json] [--html report.html]
rkdeploy releases --fleet fleet.yaml --app <name>
rkdeploy --version
```

退出码：0 表示全部成功；2 表示清单 / 用法错误（未接触任何板卡）；4 表示验证 / 预检
阻止了可能造成破坏的操作；5 表示设备集群中的部分操作失败；6 表示部署失败但自动回滚
成功；7 表示板卡操作失败，或部署回滚失败（可能需要人工检查）。

## Manifest（`rkdeploy.yaml`，schema 1）

```yaml
name: demo                 # required, ^[a-z][a-z0-9-]{1,31}$; also names install dir + service
version: 1.1.0             # required, semver; same name+version can never be installed twice
description: Generic heartbeat demo
runtime:
  type: python             # python | binary | shell
  entry: app.py            # required, relative path, must exist at pack time
service:                   # all optional, feed the unit-generation rules
  user: root
  restart: on-failure      # no | always | on-failure | on-abnormal
  restart_sec: 2
  env: {}                  # non-secret env only; secrets are board-local (see Layout)
models:                    # optional; transport list only, contents never inspected
  - path: models/weights.rknn
files:                     # optional extra payload, same rules as models
  - path: assets/
depends:                   # optional board preflight list (checked, reported, never auto-installed)
  binaries: [python3]
  apt: [libopencv-core4.5d]
  python: [yaml]
  libs: [librknnrt.so]
  devices: [/dev/video0]   # warn-level when missing
healthcheck:               # required, one of file | command | systemd-only
  type: file
  path: "@INSTALL_ROOT@/shared/heartbeat.json"
  timeout_sec: 15
keep_releases: 3           # optional, default 3
```

打包时，manifest 会被**渲染**为应用包内的机器可读文件 `package.json`，其中包含
名称、版本、入口、环境变量、依赖项、健康检查、文件列表以及每个文件的 sha256。
板卡端安装程序只读取该文件；YAML 快照仅供人工查看，不会在板卡上解析。

## 应用包格式

文件名为 `<name>-<version>.tar.gz`，归档条目按名称排序，且只允许相对路径：

```
rkdeploy.yaml      # manifest snapshot (documentation)
package.json       # rendered machine metadata — the only file the board parses
SHA256SUMS         # per-file sha256, fully verified on the board before anything goes live
service.template   # systemd unit template with @INSTALL_ROOT@/@APP_NAME@/@APP_VERSION@/@RUN_USER@
install.sh         # generic board installer (POSIX sh, ships with rk-deploy, app-agnostic)
DEPENDENCIES.json  # rendered from `depends:` (binaries/apt/python/libs/devices)
<payload...>       # entry point + everything listed in files:/models:
```

打包规则：

- 只有 manifest 声明的文件才会被打包——应用目录中的其他文件不会被放入包中。
  这是有意为之：可避免无关文件或意外的密钥文件混入应用包。
- 打包时会完整验证 manifest；任何一项验证失败都会以退出码 2 结束，并且不会生成应用包。
- 绝对路径和 `..` 在控制端和板卡端都会被拒绝。
- 依赖项是**检查清单，而不是安装器**：板卡端预检会通过
  (`command -v`、ldconfig / 常见库路径、import 探测、dpkg) 检查各类依赖，并报告缺失项。
  缺少关键依赖（binaries / libs）会中止该板卡的部署；缺少设备时只发出警告。
  v0.1 刻意不执行自动 `apt install`——客户板卡通常处于离线环境，在无人值守的板卡上
  静默修改系统软件包，比明确报告问题更危险。

## 板卡目录布局与 systemd unit 生成

默认安装根目录为 `/opt/<name>/`（可按 fleet 中的每块板卡单独覆盖）：

```
/opt/<name>/
  releases/<version>/    # immutable release directories
  current -> releases/<version>
  shared/                # config/ (board-local config + .env secrets), data/, app-owned persistence
  state.json             # {current, previous, history:[{version, action, at, result}]}
```

unit 名称始终为 `rkdeploy-<name>.service`（此前缀用于避免与厂商或系统自带的 unit 冲突）。
unit 会在板卡上安装时根据模板**进行渲染**——安装根目录和运行用户都是板卡级别的信息，
如果在打包时就将这些值写入应用包，会使同一个应用包绑定到某一块板卡的目录布局
（已验证的发现 #6）。

| Manifest 字段 | Unit 指令 |
|---|---|
| runtime.entry | `ExecStart=<interpreter><INSTALL_ROOT>/current/<entry>`（python → `/usr/bin/python3`，shell → `/bin/sh`，binary → 直接执行） |
| (fixed) | `WorkingDirectory=<INSTALL_ROOT>/current` |
| service.user | `User=` |
| service.restart / restart_sec | `Restart=` / `RestartSec=` |
| service.env + fixed | `Environment=` 行；始终注入 `RKDEPLOY_SHARED=<INSTALL_ROOT>/shared` 和 `RKDEPLOY_VERSION=<version>` |
| 存在 `shared/config/<name>.env` 时 | `EnvironmentFile=-...` —— 密钥的唯一来源 |
| (fixed) | `After=network.target`、`Type=simple`、`WantedBy=multi-user.target`、`StandardOutput=journal` |

v0.1 只支持 `Type=simple`；自行转入后台运行的应用必须提供一个前台包装程序，
规范对此有明确说明，而不是自行猜测。安装顺序固定为：
渲染 unit → `daemon-reload` → `enable` → `restart`。
回滚时，会根据目标版本中的模板重新渲染 unit，因此如果某个版本的 unit 定义发生变化，
回滚时也会恢复对应版本的 unit 定义。

## Fleet 传输

`fleet.yaml` 只保存在控制端，不会被打包：

```yaml
hosts:
  - alias: board-a          # reports use aliases only, never real addresses
    address: 192.0.2.10     # documentation example
    user: root
    tags: [pilot]
  - alias: board-b
    address: 192.0.2.11
    user: root
    install_root: /srv/apps # optional override
    tags: [pilot]
ssh:
  connect_timeout_sec: 15
```

- 传输使用标准的 **SSH/scp**：一个 tarball 加一个安装脚本，以及一次远程执行。
  不需要 agent 进程，不开放新监听端口，也不引入自定义协议——集成商现有的跳板机、
  VPN 或局域网都可以直接使用。
- **每块板卡的每项操作尽量只建立一次连接**（已验证的发现 #2）：健康检查等待循环在
  板卡端安装程序中执行，而不是由控制端反复轮询。控制端还应启用 SSH 连接复用
  （ControlMaster）。
- 默认并发数为 4（`--jobs`），也可使用 `--serial`。一块板卡的失败不会阻塞其他板卡；
  汇总结果会指出失败板卡，退出码为 5。
- v0.1 传输完整 tarball（典型 RKNN 应用包远小于 200 MB）。v0.2 可以考虑增加可选的
  可续传 rsync 通道，但它只用于加速传输，绝不能以牺牲哈希校验为代价。

## 部署流程（全部在一次板卡端执行中完成）

1. 将应用包上传到板卡的临时收件目录（`/tmp/rkdeploy-inbox-<name>/`），而不是直接上传到安装根目录。
2. 执行 `sha256sum -c SHA256SUMS`——任何校验和不匹配都会中止操作（退出码 4），并保持当前运行版本不变。
3. 预检：检查可用磁盘空间（低于应用包大小的 3 倍或低于 5% 时失败）、依赖项清单、
   运行用户是否存在，以及 systemd 是否可用。
4. 解压到 `releases/<version>/`；如果该目录已经存在，则拒绝操作（版本不可变——重新安装
   同一版本必须由人工有意执行，而不是通过工具快捷完成）。
5. 渲染 unit，并执行 `daemon-reload`。
6. 在 `state.json` 中记录 `previous`，然后原子地切换 `current`：创建新的符号链接，
   再通过 `mv -Tf` 替换旧链接（绝不能对指向目录的符号链接使用 `ln -sfn`；
   这一问题已在发现 #5 中验证）。
7. 执行 `systemctl restart`，然后在**板卡上**等待健康检查通过（等待时间由 manifest 指定）。
   对于 `file` 类型的健康检查，仅检查文件是否存在是不够的：文件必须在本次部署的
   restart 时刻或之后写入。不能让上一个版本仍在运行的进程持续更新旧心跳文件，
   从而使新版本错误地通过健康检查（这是 v0.1 开发期间、真实板卡测试之前，
   由生命周期测试套件发现的问题）。
8. 成功：更新 `state.json`；如果版本数量超过 `keep_releases`，就清理最旧的版本
   （绝不会删除当前版本或上一个版本）。
9. 失败：默认自动回滚——切回旧版本、重新渲染旧 unit、重启并再次检查健康状态；
   然后报告 `DEPLOY_FAILED_ROLLED_BACK`（退出码 6）。

## 回滚

回滚由切换符号链接、重新渲染 unit、重启和健康检查组成。无需重新上传应用包；
目标版本已经保存在板卡上。默认目标版本是 `state.json` 中的 `previous`；
使用 `--to <version>` 可以指定任何仍保留的版本。普通部署会拒绝版本号小于或等于
当前运行版本的应用包——降级只能通过显式回滚命令完成，并会记录在 history 中。

回滚不会触碰 `shared/`。如果应用更改了磁盘上的数据格式，导致旧版本无法兼容新数据，
那么确保旧代码能够读取新数据是应用自身的责任——rk-deploy 不会执行任何方向的数据迁移。
README 和本规范都明确说明了这一边界，因为人们很容易误以为部署工具会替应用处理数据迁移；
事实并非如此。

## 卸载

`rkdeploy uninstall --fleet fleet.yaml --app <name>` 默认会从设备集群的所有板卡中移除
由 rk-deploy 管理的应用。

`--hosts` 可以缩小实际执行卸载的板卡范围，但不会缩小安全检查的范围：在执行任何
破坏性操作之前，控制端都会探测整个 fleet 中的每一块板卡。若存在重复的 host alias，
或任何板卡不可访问 / 无法验证，操作都会在做出更改前中止。

```sh
rkdeploy uninstall --fleet fleet.yaml --app <name> [--hosts ...] [--jobs 4]
                 [--allow-last-copy] [--purge]
```

如果所选目标包含了所有已安装该应用的板卡，控制端会拒绝删除最后一个已安装副本。
显式指定 `--allow-last-copy` 可以覆盖这一项“最后副本”保护，但不会绕过 fleet
清点或重复 alias 检查。

在每块选中的板卡上，卸载脚本会执行以下操作：

1. 验证应用名称、安装根目录和受管理路径。对于受管理路径中的符号链接，脚本会拒绝操作，
   而不是跟随链接访问其他位置的数据。
2. 停止 `rkdeploy-<name>.service` unit，将其禁用，删除 unit 文件，并执行
   `systemctl daemon-reload`。如果停止、禁用或重新加载失败，会报告错误，并阻止后续
   删除应用文件的操作。
3. 删除 `current` 符号链接、`releases/`、`state.json`、`history.jsonl` 和临时状态文件。
4. 默认保留 `shared/`。只有显式指定 `--purge` 才会删除该目录。

只有在删除受管理文件后安装根目录变为空目录时，才会删除该目录。其他意外存在的文件
都会保留。普通卸载具备幂等性；在之前执行普通卸载后，仍可再次指定 `--purge`，
删除此前保留下来的 shared 数据。

状态探测不会将仅包含 shared 数据的安装根目录视为已安装应用。只有存在部署标记
（state、current 版本链接、release 安装元数据、受管理的 unit 文件或正在运行的服务），
才会将 `INSTALLED=1`。

## 状态检查（设备集群巡检）

每块板卡建立一次连接，并在板卡端执行一次组合探测。
仅包含已保留 `shared/` 数据的根目录不被视为已安装；必须存在部署元数据、
受管理的 unit 或正在运行的受管理服务，才能确认应用仍处于安装状态。

| 项目 | 来源 | 判定规则 |
|---|---|---|
| reachable | SSH result | 无法连接 → 板卡判定为 `UNREACHABLE`，其他项目留空 |
| service | 仅针对**当前应用自己的 unit**执行 `systemctl is-active/is-enabled/show` | 未处于 active 状态 → fail。绝不会使用整台机器的 `is-system-running`——健康的设备集群板卡也可能因为无关原因（例如 logrotate unit）处于 `degraded` 状态（已验证的发现 #3） |
| systemd | `systemctl --version` 输出的数字版本号 | 仅作参考信息；读不到则留空 |
| libraries | `ldconfig -p` 输出的共享库 SONAME（只含名字，不含文件系统路径） | 仅作参考信息；读不到链接器缓存则为空 |
| version | `current` 符号链接 + state.json | 与设备集群中的多数版本不一致 → warn（版本漂移） |
| releases | `releases/` 清单及大小 | 没有可用于回滚的候选版本 → warn |
| disk | 安装根目录所在文件系统的可用空间 | 低于 10% → warn；低于 5% → fail |
| thermal | 按类型名称枚举所有 thermal zones；报告 SoC 温度和最高温度 | ≥75 °C → warn；≥85 °C → fail；绝不硬编码 thermal zone 编号（已验证的发现 #7） |
| health | 实时执行 manifest 健康检查，并统计最近一小时的 journal 错误数 | 健康检查失败 → fail |

报告采用 JSON schema 1（包含每块板卡记录的 `fleet[]` 和汇总信息），同时输出终端表格，
每块板卡占一行。报告只包含 alias，不包含真实地址、主机名或序列号。

`status --html report.html` 生成单文件离线 HTML 报告：内联 CSS、无 JavaScript、
无外部资源与网络引用，直接用浏览器打开。它与 `--json` 使用同一个 schema-1 报告对象，
展示设备集群 verdict 与生成时间，每块板卡一张卡片，含 JSON 的全部字段（逐区 thermal 诊断
与 systemd 版本在内）。库只以 `system_library_count` 数量呈现，不展开可能很长的
`system_libraries` 名单。缺失或空值显示为 `-`（false 与 0 仍正常显示）。

## 可行性验证（2026-11-02，真实 RK3588 板卡）

环境：RK3588、Ubuntu 22.04.5、kernel 6.1.118、systemd 249、Python 3.10.12；
根文件系统使用率为 87%（剩余 15.9 GB）；机器负载约为 16.6，该负载来自既有的无关任务
（部署操作不受 CPU 限制，因此不会影响计时结果）。

方法：使用控制端打包脚本和随应用包提供的 POSIX `sh` 安装程序，并使用测试安装前缀
代替 `/opt`。demo 应用是一个通用心跳程序；其“模型”文件是一个不透明的占位数据文件，
用于验证传输过程不会检查模型文件内容这一规则。

结果：**所有阶段均通过**，端到端总耗时 105.2 秒（包括清理）。

| 阶段 | 结果 | 时间 |
|---|---|---|
| 打包 v1.0.0（2.4 KB 应用包） | 校验和、package.json 和 unit 模板均已生成 | 0.01 s |
| 推送 v1（scp，2 个文件） | 文件已到达板卡收件目录 | 14.0 s —— 几乎全部耗时都来自连接建立 |
| 安装 v1 | 验证 → 解压 → 渲染 unit → daemon-reload → enable → restart → 心跳确认版本为 1.0.0 | 19.9 s（包含控制端轮询，见发现 #2） |
| 打包并推送 v1.1.0 | 与上述相同 | 0.01 s + 7.3 s |
| 安装 v1.1.0 | `current` 已切换，服务处于 active 状态，心跳报告 1.1.0；v1 写入 `shared/` 的标记文件仍然存在 | 20.8 s（包含轮询） |
| 回滚到 v1.0.0 | 无需重新上传；切换符号链接并重启；心跳恢复报告 1.0.0 | 14.0 s（主要耗时来自连接和检查；切换本身是即时的） |
| 状态快照 | 在一次探测中获取服务 / enabled / 版本 / 磁盘 / 7 个 thermal zones（SoC 49.0 °C，最高 50.8 °C） | 4.7 s |
| 清理 | 删除测试 unit 和目录，执行 `daemon-reload`；`systemctl --failed` 与测试前完全一致 | 已计入总时间 |

### 发现（以下各项均已落实到上述规则中）

1. **不能依赖板卡端的 Python。** 验证板卡使用 Python 3.10（没有 `tomllib`），并且存在一个
   非预期安装的 PyYAML 5.4.1；其他 BSP 可能连 PyYAML 都没有。因此板卡永远不会解析 manifest，
   只会读取渲染后的 `package.json` 和纯 shell 脚本。
2. **SSH 连接建立占据了主要耗时。** 通过隧道建立每次连接约需 5–7 秒；推送一个 2.4 KB
   的文件花费了 7–14 秒，而控制端每秒轮询健康状态又将安装时间拉长到约 20 秒。
   因此得出以下规则：每块板卡的每项操作只建立一次连接；健康检查等待过程放在板卡端；
   控制端启用 ControlMaster。在 20 块板卡的场景下，如果每条 SSH 命令都单独建立连接，
   一轮状态检查就会变成纯粹的等待。
3. **整台机器的 systemd 状态不能说明应用是否健康。** 板卡因为一个无关的 logrotate unit
   而报告 `degraded`。状态检查只判断应用自己的 unit。
4. **可用磁盘空间确实是一个约束。** 验证时磁盘使用率达到 87%，因此部署预检需要检查磁盘空间，
   并通过 `keep_releases` 清理旧版本。
5. **目录符号链接陷阱确实存在。** 对一个指向目录的符号链接执行 `ln -sfn`，会跟随该链接，
   并在目标目录内部创建新链接。原子切换应使用：创建新符号链接 + `mv -Tf`。
6. **unit 文件无法在打包时渲染。** `ExecStart` 取决于安装根目录和运行用户，而这些都是板卡级别的信息。
7. **必须按类型名称枚举 thermal zones。** 该板卡有 7 个 zone（soc / bigcore0 / bigcore1 /
   littlecore / gpu / npu / center）；不同 BSP 的布局并不相同。

### 此次验证的局限（如实说明）

- 只使用了一块板卡：尚未验证多板卡并发及部分失败汇总；计划在 v0.1 开发期间通过第二块
  可访问的板卡（或为同一块板卡配置多个 alias）进行验证。
- 使用测试前缀而不是 `/opt`：生产路径上的权限问题仍是 v0.1 开发期间的首要检查项。
- demo 应用没有执行真正的 RKNN 推理——这是有意为之，因为部署层与模型无关；
  推理正确性属于诊断工具的职责，而不是部署工具的职责。

## v0.1 实现验证（2026-11-02，使用已发布 CLI 和同一块板卡）

已发布的实现通过正式的 `rkdeploy` CLI 在同一块 RK3588 板卡上运行
（单板 fleet，安装根目录位于测试前缀下，服务名称为 `rkdeploy-demo`）：

| 步骤 | 结果 |
|---|---|
| `deploy` demo 1.0.0 | 成功，耗时 21.0 s（mkdir + scp + install = 3 次隧道连接） |
| `status` | ATTENTION —— 只安装了一个版本，也就是没有可用于回滚的候选版本；这是规范中的警告规则正确触发 |
| `deploy` demo 1.1.0 | 成功，耗时 19.4 s |
| `status` | HEALTHY —— 服务 active，版本为 1.1.0，磁盘可用空间 13%，SoC 温度 49 °C / 最高温度 50 °C |
| `deploy` 损坏的 1.2.0（心跳文件始终未出现，超时 5 s） | 健康检查失败 → **自动回滚到 1.1.0**，CLI 退出码为 6，总耗时 25.6 s |
| `rollback --to 1.0.0` | 成功；板卡的 `state.json` history 按顺序记录了全部四项操作 |
| `releases` / 最终 `status` | releases 为 [1.0.0, 1.1.0, 1.2.0]，当前版本为 1.0.0，设备集群判定为 HEALTHY |

最终状态检查生成的真实报告位于 `examples/status-report-rk3588.json`。
验证完成后，测试服务和目录均已从板卡上删除；此前就存在的、与本次测试无关的失败 unit
仍是 `systemctl --failed` 中唯一的条目，与测试前完全相同。

有意采用的一项实现细节与早期草案略有不同：应用包除了 `package.json`，还包含渲染后的
`package.env`（扁平的、经过 shell 引号处理的 KEY=VALUE 格式），因此板卡安装程序甚至
不需要 JSON 解析器——它只读取 `package.env`。每个已安装的 release 还会生成
`install.env`，其中已经替换了板卡级别的值（安装根目录、解析后的健康检查路径）。

## v0.1 不在范围内的功能

常驻板卡 agent、中央服务器和 Web UI、自动安装软件包、模型转换、日志聚合、canary 策略
（v0.1 使用 `--hosts` 选择作为批次管理机制）、非 Linux 板卡、应用商店 / 注册中心。
