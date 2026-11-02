# rk-deploy

**把 RKNN 应用像回事地交付到一队 Rockchip 板子上：**
一份 manifest 描述应用 → 一条命令打包 → 经 SSH 推到每块板子装成
systemd 服务 → 推新版、秒级回滚、出一份可信的舰队巡检报告。

面向班牌屏厂商、停车/安防集成商的交付与运维：手里有 5–50 块
RK3588 / RK3576 / RK356x 板子、现在还在手工 scp + 逐板手写
`.service` 文件的人。

## 状态

**v0.1.0 已发布（2026-11-02）。** 全链路已用正式 CLI 在 RK3588
真机（Ubuntu 22.04 / systemd 249）验证：部署 1.0.0 成功（21 秒）
→ 巡检 ATTENTION（正确指出「只有一个版本、无回滚余地」）→
部署 1.1.0 成功 → 巡检 HEALTHY → 部署坏版本 1.2.0 时健康检查
失败、**自动回滚到 1.1.0**（退出码 6）→ 显式回滚到 1.0.0 成功。
真实巡检报告样本在 examples/status-report-rk3588.json。此前
手工走通全流程的 105 秒可行性验证与 7 个坑在
[docs/SPEC.md](docs/SPEC.md)。

安装：`pip install git+https://github.com/anguoyang/rk-deploy`

## 用法预览

```sh
rkdeploy pack                                       # → demo-1.1.0.tar.gz（带校验）
rkdeploy deploy --fleet fleet.yaml demo-1.1.0.tar.gz
rkdeploy deploy --fleet fleet.yaml --dry-run demo-1.1.0.tar.gz   # 只预览、零 SSH
rkdeploy rollback --fleet fleet.yaml --app demo
rkdeploy status --fleet fleet.yaml
```

设计铁律：只处理通用应用包、模型文件不透明搬运从不解析；
版本目录不可变 + 软链原子切换；配置与数据放在版本之外的
`shared/`，升级回滚永不触碰；健康检查不过默认自动回滚；
不装 agent、不开新端口、不收集凭据，只用你现有的 SSH 密钥体系。

英文主文档见 [README.md](README.md)。许可证 Apache-2.0。
