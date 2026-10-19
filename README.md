# RTX 3060 NVENC · macOS

跟踪 [NullMoth NVIDIA macOS 驱动](https://github.com/nullmoth/nvidia-macos-driver)，通过 **GitHub Actions** 构建 RTX 3060 的实验性 **H.264 NVENC 兼容覆盖包**。

目标是继续使用新版驱动的显示、Metal 和浏览器修复，同时恢复 GA106 的硬件编码。这里不是完整显卡驱动，也不是已经验证可用于生产的发行版。

| 项目 | 当前状态 |
| --- | --- |
| 上游基线 | `v2.3.7` / `e2b0e547a18841b7d944850b03c9e605df76720b` |
| 适配对象 | Intel macOS，RTX 3060 GA106 LHR，PCI `10de:2504` |
| 编码范围 | H.264 NVENC；不启用 HEVC |
| 构建与更新 | GitHub Actions，不依赖 AI 服务或模型密钥 |
| 新版适配后的实机验证 | v2.3.7 指定候选已通过；[升级记录、首轮未达标与复测报告](docs/upgrade-2026-10-19-v2.3.7.md) |

参考机器为 Unraid 虚拟机、macOS 15.8.1、RTX 3060 12GB。2026-10-19 的 2.3.7 指定候选复测完成 3600 帧编码、独立软件解码和像素校验，约 **60.00 FPS**，NVENC 编码延迟 **p95 2.78 ms**；现有 [GlideBeam](https://github.com/dixtdf/GlideBeam) 的生产编码路径短测也通过。首轮刚启动时合成吞吐约 55.33 FPS，报告一并保留。结果绑定候选及编码库 SHA-256，不自动适用于其他构建、显卡或 macOS 版本，也不代表远控端到端或浏览器场景保证 60 FPS。此前 [2.3.6 验收记录](docs/trial-2026-10-18-v2.3.6.md)仍可查阅。

## 为什么需要这个项目

上游 v2.3.6 / v2.3.7 对非 Blackwell 显卡跳过自带 H.264/HEVC 编码器注册。上游解释是编码库采用 Blackwell 输入布局，Ampere/Turing 需要经典布局；这不表示 3060 硬件没有 NVENC。[源码说明](https://github.com/nullmoth/nvidia-macos-driver/blob/v2.3.6/plugin/NVMTLDevice.m#L185-L189)、[布局实验](https://github.com/nullmoth/nvidia-macos-driver/issues/65)。

本项目只研究 H.264 注册和 NV12 输入布局。Metal 渲染与 NVENC 编码分开验证，软件编码回退不算通过。未知二进制不会套用旧版本偏移。

## Actions 怎么工作

1. 定时或手动检查官方最新正式版，锁定 tag、提交、发行包及 SHA-256。
2. 新版出现时更新本仓库的候选 PR，明确记录待适配版本；不会向上游自动提交 PR 或 issue。
3. 构建读取 `profiles/<tag>.json`。只有已审阅的完整文件摘要、原始指令和补丁后摘要全部匹配，才生成候选。
4. Intel macOS runner 完成补丁、临时签名与校验，上传实验覆盖包和构建报告。
5. 没有对应 profile 或原始字节变化时，构建停止，等待审阅和新增规则。

GitHub 托管 runner 没有这张 3060。它能做静态检查、CPU 布局测试、签名校验，以及“没有 NVIDIA 时必须拒绝硬编”的负向测试；**不能自动宣布实机可用**。没有自动安装、自动重启或自动发布“硬件验证通过”版本。

运行入口：[Build candidate](https://github.com/dixtdf/nvidia-macos-driver-3060-nvenc/actions/workflows/build.yml)、[Check upstream](https://github.com/dixtdf/nvidia-macos-driver-3060-nvenc/actions/workflows/check-upstream.yml)。构建成功后在该次运行的 Artifacts 下载覆盖包。上游检测使用仓库自带 `GITHUB_TOKEN`；首次需允许 Actions 创建 PR，默认分支的工作流权限仍保持只读，写权限只给检测任务。

## 构建产物

- `nvenc3060-overlay-v<version>-experimental.tar.gz`：两个修改后的用户态二进制、外层签名资源、应用工具及许可。
- `nvenc3060-probe-v<version>.tar.gz`：Actions 已编译并临时签名的 Intel Mac 实机探针、报告校验器和验收说明，无需在目标机重新编译。
- `build-report.json`：上游出处、补丁审计、签名后摘要，状态始终保留 `hardware_validation: not-run`。
- `SHA256SUMS.txt`：覆盖包与探针包摘要。

覆盖包不包含内核驱动、固件、私人签名、序列号、Apple 账户或光梭配对资料。修改后的二进制使用 ad-hoc 签名。

H.264 注册放行补丁本身不是逐设备的 PCI 检查。应用工具因此要求系统中只有一个 NVIDIA 显示设备，且精确匹配 `10de:2504`。不要绕过工具把这些二进制复制到其他显卡上；其他代际不在本项目的验收范围内。

## 本地检查

Python 3.11+；从仓库根目录执行：

```sh
PYTHONPATH=src python -m unittest discover -s tests
python scripts/check_upstream.py --output state/upstream.json
python scripts/build_candidate.py --state state/upstream.json --output build/candidate --cache build/cache
```

完整签名构建需要 Intel macOS。其他系统可追加 `--inspect`，仅生成静态审计，不生成可安装覆盖包。输出目录必须为空。下载被阻断时可使用本机可用代理，例如 `HTTPS_PROXY=http://127.0.0.1:10808`；该端口不是构建服务的依赖。

## 实机验证与回滚

先读 [硬件验收](docs/hardware-acceptance.md) 与 [架构和版本规则](docs/architecture.md)。正式测试前保留 VM 磁盘、EFI、配置和当前完整驱动的可恢复备份。

覆盖包要求**同版本官方驱动已经安装并重启**。它不能直接应用于旧 1.9.0，也不能代替官方安装器。解压并核对下载摘要后，首先执行只读检查：

```sh
python3 apply_overlay.py --check
```

只有决定开始实验性部署时才显式应用，备份路径必须是新目录：

```sh
sudo python3 apply_overlay.py --apply --backup /Users/Shared/nvenc3060-backup-<unique-name>
```

工具检查 GPU、已加载的 NVRM 版本、完整原始 bundle 摘要及覆盖文件摘要；先备份，再校验暂存目录，通过目录原子交换更新用户态 bundle。它不改 EFI、内核扩展或 SIP，也不自动重启。重启后运行硬件探针并核对实际编码器、像素、关键帧和持续性能。

恢复该次覆盖前的官方用户态 bundle：

```sh
sudo python3 /Users/Shared/nvenc3060-backup-<unique-name>/apply_overlay.py --restore /Users/Shared/nvenc3060-backup-<unique-name>
```

恢复后仍需重启。这个备份只回滚本覆盖包，不回滚此前官方驱动升级；完整 VM/驱动备份用于恢复到旧 1.9.0。

## English

This independent project tracks the NullMoth macOS NVIDIA driver and builds exact-version experimental H.264 NVENC compatibility overlays for a GA106 RTX 3060 (`10de:2504`). GitHub Actions detects upstream releases, records provenance, checks reviewed binary patch profiles, signs candidates on Intel macOS and uploads artifacts. Unknown versions fail closed. No AI API, unattended installation or automatic hardware-qualified release is involved.

Hosted CI cannot validate a physical RTX 3060. Candidate artifacts remain **not hardware-qualified** until exact encoder selection, hardware use, decoded pixels, IDR behavior and sustained performance pass on the target machine. HEVC and other GPUs are outside the initial scope. See [hardware acceptance](docs/hardware-acceptance.md).

## 许可与来源

本仓库原创工具和探针使用 [MIT](LICENSE)。上游代码及派生驱动二进制保留其 [PolyForm Noncommercial 条款](LICENSES/UPSTREAM-LICENSE.txt) 和 [NOTICE](NOTICE)，不会因为工具使用 MIT 而改变。构建产物携带相应许可与来源说明。
