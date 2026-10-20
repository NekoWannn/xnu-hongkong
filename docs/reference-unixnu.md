# 参考项目：UniXNU

- 仓库：<https://github.com/UniXNU/UniXNU>
- 定位：在**非 Apple 的 ARM64 硬件**上通过 **UEFI** 引导 Darwin/XNU，运行 iOS/iPadOS 用户态（进行中）
- 公开成果：小米平板 6S Pro 上运行 iPadOS 17
  - 报道：<https://www.vgover.com/news/241755>
  - 演示：<https://www.bilibili.com/video/BV1jzJo6rEa8/>

## 与本项目的差异

| 维度 | 本项目（Xiaomi 18 Pro / hongkong）| UniXNU |
|---|---|---|
| 引导链 | ABL → 自制 shim → `SWEETXNU` payload → XNU | UEFI |
| 用户态 | 自制 mockfs + 16 KB `sweet-init` | 真实 iPadOS 17 根文件系统 |
| 硬件平台 | SM8950，EL2 由 Gunyah 占用 | 小米平板 6S Pro（骁龙 8 Gen 2）|
| 出发点 | ios-sweet（Redmi Note 10 Pro）的补丁集 | 自有 UEFI 方案 |

## 值得重点关注的三块

1. **UEFI 引导**
   绕开 ABL 对 boot.img 格式的限制，也可能避免 Gunyah 环境下 PSCI 的复杂性。

2. **从核唤醒**
   本项目当前 `PSCI CPU_ON` 返回 `-4`（ALREADY_ON），被迫用 `cpus=1` 单核运行。
   UniXNU 在 UEFI 下的 SMP 启动方式可能给出答案。

3. **根文件系统**
   本项目目前只能挂载 mockfs（内存盘 + 单个 Mach-O）。
   UniXNU 已解决 dyld shared cache、keybag、AMFI 授权等完整用户态所需环节。

## 后续动作

- 等仓库开源后，优先阅读：UEFI 启动流程、SMP 唤醒、根文件系统挂载
- 对比其内核补丁与本项目 `port/hongkong-*.patch` 的差异
