# rk-deploy

**把 RKNN 应用像回事地交付到一队 Rockchip 板子上：**
一份 manifest 描述应用 → 一条命令打包 → 经 SSH 推到每块板子装成
systemd 服务 → 推新版、秒级回滚、出一份可信的舰队巡检报告。

面向班牌屏厂商、停车/安防集成商的交付与运维：手里有 5–50 块
RK3588 / RK3576 / RK356x 板子、现在还在手工 scp + 逐板手写
`.service` 文件的人。

## 状态

**规格已冻结、可行性已在真机验证，代码开发中。** v0.1 排在
2026-11-09 那一周。本仓目前放的是设计规格
（[docs/SPEC.md](docs/SPEC.md)，英文），欢迎在实现落地前先审
格式与规则。

全流程已在 RK3588 真机（Ubuntu 22.04 / systemd 249）手工走通：
打包 → SSH 推送 → 装成 systemd 服务 → 推 v1.1.0 → 回滚
v1.0.0 → 舰队巡检快照，全程含清理 105 秒；一路上踩到的坑
（SSH 连接开销主导一切、板端不能解析 YAML、目录软链切换的
经典陷阱、整机 degraded 与应用健康无关）都已写进规格。

## 用法预览

```sh
rkdeploy pack                                       # → demo-1.1.0.tar.gz（带校验）
rkdeploy deploy --fleet fleet.yaml demo-1.1.0.tar.gz
rkdeploy rollback --fleet fleet.yaml --app demo
rkdeploy status --fleet fleet.yaml
```

设计铁律：只处理通用应用包、模型文件不透明搬运从不解析；
版本目录不可变 + 软链原子切换；配置与数据放在版本之外的
`shared/`，升级回滚永不触碰；健康检查不过默认自动回滚；
不装 agent、不开新端口、不收集凭据，只用你现有的 SSH 密钥体系。

英文主文档见 [README.md](README.md)。许可证 Apache-2.0。
