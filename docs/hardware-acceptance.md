# RTX 3060 H.264 硬件验收

此工具只产生测试图案，不读取屏幕、不申请录屏权限、不连接网络、不安装驱动、不修改 OpenCore 或光梭服务。当前范围是 **macOS 15、Intel、PCI `10de:2504` 的 RTX 3060**；其他 3060 PCI ID 和 HEVC 不在本轮验收范围。

GitHub 托管的 macOS runner 没有这张显卡。Actions 能构建和执行拒绝软件回退的负向测试，**不能据此标记真实 3060 已验证**。真实验收应在单独安排的 3060 测试机运行，且仅在已有备份和回滚条件下安装候选驱动。

## 官方基线安装检查

从旧驱动升级时，先保存关机状态的系统盘、实际 EFI 引导盘、UEFI NVRAM 和 VM 配置；用户态 bundle 的备份不足以恢复旧版内核扩展和辅助内核集合。官方包的 `CHECK=1` 是环境变量：`sudo env CHECK=1 bash /path/to/pkgroot/install.sh`。检查通过后才执行官方安装器。

v2.3.6 安装器明确提示：若已加载 WhateverGreen，OpenCore 的 `boot-args` 需要 `gfxrst=4`，避免其启动画面复制路径触发显示打开时的崩溃。先备份并修改实际使用的 EFI 配置，用对应版本 `ocvalidate` 检查，并在重启后确认实际 `boot-args`。不要只修改仓库模板。

官方安装后先重启，确认四个 NullMoth 内核扩展均为目标版本，显示和恢复通道正常，再执行覆盖工具的 `--check`、应用覆盖包并再次重启。不要在磁盘上的新驱动与内存里的旧驱动混用时测试编码。覆盖工具会遍历完整 PCI 子树并按 registry ID 去重；位于 PCI 桥后的显卡也必须满足单卡 `10de:2504` 限制。

官方安装器打印的 `uninstall.sh` 不能替代完整旧版本恢复；覆盖工具的 `--restore` 也只恢复同版本官方用户态 bundle。回到旧 1.9.0 应使用此前保存的完整 VM/驱动备份。

## Actions 预编译探针

从成功运行的 Actions Artifacts 下载 `nvenc3060-probe-v<version>.tar.gz`，核对同次运行的 `SHA256SUMS.txt` 后解压。包内已经包含 `nvenc-acceptance` 和 `validate_hardware_report.py`，不需要目标机安装 Xcode。进入该解压目录执行：

```sh
./nvenc-acceptance --profile smoke --frames 30 --fps 60 --report smoke.json
python3 validate_hardware_report.py smoke.json --profile smoke
```

下文的 `build/probes/nvenc-acceptance`、`scripts/validate_hardware_report.py` 为从源码构建时的路径；使用预编译包时换成上述两个路径。

## 从源码编译与短测

```sh
bash probes/build.sh build/probes
build/probes/nvenc-acceptance --profile smoke --frames 30 --fps 60 --report smoke.json
python3 scripts/validate_hardware_report.py smoke.json --profile smoke
```

返回码：`0` 表示测试通过；`1` 表示硬件、编码或图像验收失败；`2` 表示参数或报告写入失败；`124` 表示总时限看门狗终止进程。每帧编码和解码回调各有 3 秒时限，整个进程最多运行 `frames / 60 + 30` 秒。报告路径必须不存在，工具不会覆盖已有结果。

短测只证明基本硬编与图像正确性，不证明持续 60 FPS。没有目标显卡时必须失败，绝不改用 Apple 软件编码器。CI 应检查此时进程非零退出，且报告不能通过验证器；不能把失败进程包装为硬件测试通过。

## 60 秒验收与候选版本绑定

先从构建出的候选包计算包本身及其中 `libnvenc_h264.dylib` 的 SHA-256。仅填写已检查的构建结果，不填写示例值。安装和加载候选驱动后：

```sh
build/probes/nvenc-acceptance \
  --profile qualification --frames 3600 --fps 60 \
  --candidate-sha256 "$CANDIDATE_ARCHIVE_SHA256" \
  --upstream-tag "$UPSTREAM_TAG" \
  --report qualification.json

python3 scripts/validate_hardware_report.py qualification.json \
  --profile qualification \
  --candidate-sha256 "$CANDIDATE_ARCHIVE_SHA256" \
  --encoder-sha256 "$CANDIDATE_ENCODER_DYLIB_SHA256" \
  --upstream-tag "$UPSTREAM_TAG"
```

报告中的候选包哈希和上游 tag 是调用者提供的版本绑定；工具还会独立定位**当前进程实际加载的编码库**并计算其 SHA-256，验证器必须与候选包内编码库的已知哈希匹配。若安装完但进程仍加载旧库、发现同名编码库有歧义，或者无法确认库哈希，就不能通过。

这不是防篡改硬件证明。应同时保存构建 run、候选包及其哈希、上游 commit、补丁和测试命令。报告不能替代候选驱动安装来源检查。公开报告只包含固定字段、数值、版本、哈希和固定错误代码，不包含序列号、Apple ID、密码、主机名、文件路径或屏幕内容。验证器拒绝额外字段、重复 JSON 键和非有限数值。

## 软件解码证据与报告版本

实机报告使用 `schema_version: 2`、`probe: 3060-nvenc-h264-v2`。验证器不接受旧版报告冒充新版验收；保留旧版原始失败报告，使用新版探针重新测试，不手工改写旧报告。

创建独立解码会话时，探针明确传入 `EnableHardwareAcceleratedVideoDecoder = kCFBooleanFalse`。Apple 文档说明此值[禁止硬件解码](https://developer.apple.com/documentation/VideoToolbox/kVTVideoDecoderSpecification_EnableHardwareAcceleratedVideoDecoder)。只有创建成功后，报告才记录 `decoder_hardware_disabled_by_spec: true`。

Apple 软件解码器可能不实现 `UsingHardwareAcceleratedVideoDecoder` 查询；本机 macOS 15.8.1 在解码前后都返回 `kVTPropertyNotSupportedErr`（`-12900`）。该属性[用于查询启用硬件解码后的选择结果](https://developer.apple.com/documentation/videotoolbox/kvtdecompressionpropertykey_usinghardwareacceleratedvideodecoder)，不能把不支持查询伪造为观测到了 `false`。

验证器仅接受以下两种软件解码证据，且两者均要求上述显式禁用参数、成功创建解码会话，以及所有帧实际解码和像素验收通过：

| 查询结果 | `decoder_hardware_query_status` | `decoder_hardware_query_success` | `decoder_hardware_used` |
| --- | --- | --- | --- |
| 查询成功且返回 CFBoolean false | `0` | `true` | `false` |
| 属性不受支持 | `-12900` | `false` | `null`（未知） |

查询为真、其他错误、成功状态却返回空值或非 CFBoolean 类型，以及报告字段互相矛盾，均不通过。软件解码用于独立检查 NVENC 输出；编码端仍必须使用精确指定的 NVIDIA 硬件编码器，软件编码回退仍不允许。

## 必须同时满足的条件

- I/O Registry 中恰有一个 NVIDIA 显示设备，且为目标 `10de:2504`，避免由另一张 NVIDIA 卡完成编码而误判。
- 创建编码器时要求硬件，并固定 `com.nvmtl.videoencoder.avc.nvenc`；会话返回的实际 encoder ID 必须一致，硬件标志必须为真。
- 输入为普通 `420v` NV12、1920×1080；**不做光梭旧版的 CPU 预排列补偿**。新驱动适配应使普通布局直接正确。
- H.264 为实时模式、禁止帧重排，最多一个在途帧；每 60 帧主动请求一个 IDR，并解析 H.264 NAL 单元确认对应帧确实是 IDR。
- 使用明确禁止硬件加速的独立 VideoToolbox 软件解码会话，并按上述两种证据之一确认软件路径，避免用同一个有问题的 GPU 解码器掩盖错误。
- 每帧必须成功编码、软件解码、时间戳匹配，并对所有可见 Y/UV 字节与原始图案比较。图案包含区域网格、单像素细线、不同色度区域和移动标记；统计每帧误差最大值、16 个区域的最大误差、细线误差及移动标记误差。
- 像素容差：Y 平均绝对误差 ≤12；UV ≤15；任一区域 Y ≤24；细线 ≤45；标记任一采样分量误差 ≤32。H.264 有损，因此不要求字节完全一致；这些容差不能检测所有可能的视觉缺陷，人工观察和真实桌面测试仍有价值。
- qualification 至少 3600 帧；请求、编码、解码、像素验收数量完全一致；回调错误和丢帧均为 0；强制 IDR 全部通过。
- 按 60 Hz 节奏运行，3600 帧至少持续 59.5 秒；实际吞吐需落在 **59.5–60.5 FPS**，编码回调延迟 p95 ≤16.667 ms。59.5 是计时容差，不是宣称能在任意负载下保持 60 FPS。

单帧软件解码及像素比较也计入测试总耗时，因此吞吐是一条严格的合成链路验收，不等于纯 NVENC 峰值。编码延迟仅计算从提交到编码回调，不包含桌面捕获、网络、客户端解码和显示延迟。通过该工具仍不能直接宣称光梭端到端稳定 60 FPS。

## 与现有 1.9.0 回滚方案的关系

现有光梭为了已知的旧版编码库，可能在提交前对 NV12 做 CPU 预排列。此探针有意不做该补偿，因此旧版驱动即使在光梭中可用，也可能在此处因画面布局错误而失败。这是有意义的负向对照，不能据此破坏现有可用服务，也不能把旧版光梭的短测结果当作新版候选已经通过。

只有候选编码库在普通 NV12 输入下通过像素验收后，才评估光梭能否按照新编码库哈希选择普通输入路径；不能在未验收的新库上继续套用旧预排列。测试失败时保留错误报告，并使用已有备份回滚，不自动安装下一个驱动版本。
