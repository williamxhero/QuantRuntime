# Quant Runtime：AI 部署与研究指南

本文供 AI 执行代理使用。目标是在任意用户电脑上部署 Quant Runtime，并完成一次真实策略研究运行。`README.md` 已冻结：保留其现状，后续部署说明只维护本文。

## 系统边界

- Quant Runtime 是执行层，不保存策略注册、任务状态或最终资产。
- Strategy Workspace 是控制层，管理策略包、请求、运行记录和制品。
- MarketHub 是唯一生产行情源，必须提供冻结版本的数据。
- Qlib 只用于发现研究；NautilusTrader 用于正式回测。
- 不得用样例数据、其他数据源或模拟适配器代替不可用的生产服务。

## 前提条件

部署前逐项确认：

1. 64 位 Windows 或 Linux，具备足够磁盘和内存；大规模分钟数据应预留明显高于原始数据量的内存与临时空间。
2. 已安装 Git、`uv` 和 Python 3.12。项目不支持 Python 3.11 或 3.13。
3. 目标机可访问 GitHub，以及用户自己的 MarketHub v2 服务。
4. MarketHub 的 `/api/health` 返回 `status=ok`，并包含股票日线版本；期货分钟研究还须包含 `future_bar_1m` 版本。
5. 用户已经拥有合法可用的行情数据、策略参数和交易规则。不要猜测或伪造缺失值。

## 安装

两个仓库必须位于同一父目录，且目录名保持如下结构；Linux 区分大小写：

```text
quant-research/
├── strategy-workspace/
└── quant-runtime/
```

```powershell
mkdir quant-research
cd quant-research
git clone https://github.com/williamxhero/StrategyWorkspace.git strategy-workspace
git clone https://github.com/williamxhero/QuantRuntime.git quant-runtime
cd quant-runtime
uv sync --python 3.12 --extra dev
```

`pyproject.toml` 通过 `../strategy-workspace` 加载控制层。若目录结构不同，先修正安装布局，不要复制 Strategy Workspace 的代码到本仓库。

## 部署验证

在 `quant-runtime` 目录执行：

```powershell
uv run python -c "import quant_runtime, strategy_workspace, qlib, nautilus_trader; print('ok')"
uv run ruff check .
uv run pytest -m "not connected"
uv build
```

再测试目标 MarketHub；将地址替换为用户自己的服务：

```powershell
uv run python -c "import httpx; print(httpx.get('http://HOST:PORT/api/health', timeout=10).json())"
```

完成标准：导入成功、离线测试通过、构建成功、MarketHub 健康且目标数据集有版本号。联网测试可用 `uv run pytest -m connected`，但仓库内测试地址可能是维护者环境；其他电脑应先改为等价的外部连通性检查，不要把私人地址提交回仓库。

### OCI worker 镜像准入

`src/quant_runtime/sandbox/oci.py` 的 `APPROVED_OCI_IMAGE` 是生产 OCI worker 镜像的唯一批准源，
只接受完整的 `sha256:<64 位小写十六进制>` 本地镜像 ID 或 `repository@sha256:<64 位小写十六进制>` 引用。
当前所有者批准的本地 worker 镜像 ID 为
`sha256:fa7631435b780e7968992e5d55b0e0bc6cd89b6349afe98327e55df3227b7e0e`，
本地标签为 `quant-runtime-sandbox-worker:20261008`，由本仓库的
`containers/sandbox-worker.Dockerfile` 构建，运行用户为 `65534:65534`。
其 `org.quant-runtime.dependency-lock` 标签为
`sha256:5690bb318285226c8cd3a06ed91bdff9fd82fa05156f6db73ea923708d64be22`。

2026-10-08 在本机产生的真实 containment proof 的 12 项 probes 全部为 true，
`proof_id=sha256:61a4598aea2b929b640f246b8a5da1d115530be83c4aa94ebb4ee1934e5f5dc7`。
该证据绑定 backend `docker-engine-linux-oci`、mechanism `linux-namespaces-cgroups-seccomp-oci`，
以及 Docker 29.3.1 / containerd v2.2.1 / runc 1.3.4 / kernel
`6.6.87.2-microsoft-standard-WSL2`。已验证控制包括只读 rootfs、只读输入 bind、限额输出 tmpfs、
仅 loopback 的隔离网络 namespace、128 的 pids 上限、uid 65534、cap-drop-all、
no-new-privileges、seccomp、不转发主机环境变量，以及 engine-kill 后确认容器已停止。
此记录仅描述该主机上的验证，不代表其他主机也已通过验证。

重建时先执行下列命令；将 `<version>` 与 `<date>` 替换为实际版本与标签日期：

```text
uv build --wheel
docker build --build-arg RUNTIME_WHEEL=dist/quant_runtime-<version>-py3-none-any.whl -f containers/sandbox-worker.Dockerfile -t quant-runtime-sandbox-worker:<date> .
```

Dockerfile 的默认 `ARG RUNTIME_WHEEL` 仍指向 `dist/quant_runtime-0.2.3-py3-none-any.whl`，
而当前包版本为 `0.2.7`；仅构建当前版本 wheel 后，不传 `--build-arg` 的 Docker 构建会因缺少旧 wheel 而失败。
重建不自动批准新镜像，也不保证得到相同镜像 ID。镜像所有者须在目标 Linux Docker 主机上确认真实
worker 镜像及 containment/resource-enforcement 证据后，通过代码审查更新此常量；
不得把旧测试摘要或 Dockerfile 的基础镜像摘要当作批准。
仓库外的本地 OCI 测试也必须读取此源，不得维护自己的镜像字面量。

对当前批准镜像重新验证 containment，并对已注册策略包执行行为一致性验证（工作区与请求须明确提供）：

```text
uv run quant-runtime sandbox-proof --image sha256:fa7631435b780e7968992e5d55b0e0bc6cd89b6349afe98327e55df3227b7e0e
uv run quant-runtime conformance --workspace <工作区绝对路径> --request <一致性请求.json>
```

批准源缺失或无法解析时，显式配置 OCI 会在 Docker 探测前拒绝；无批准且无显式选择时返回不支持的 backend，
不执行候选代码。配置有效时，Runtime 默认选择此批准引用；`QUANT_RUNTIME_OCI_IMAGE` 和
`sandbox-proof --image` 只能选择完全相同的引用，不能绕过批准或静默回退到其他镜像。
请求中的 `dependency_environment.identity` 仍必须匹配证明所绑定的镜像摘要，lock identity 与 containment proof
也必须精确匹配。离线配置/不匹配测试不需要 Docker，但不能替代真实 OCI 隔离验证。

`benchmark-exec` 的 `production_attested_oci` 请求只有在 backend 不是 `OciSandboxBackend` 时才返回
`blocked` / `benchmark_oci_unavailable`。已选择 OCI backend 后，无法验证 capability 或请求 profile
不匹配会返回 `failed` / `policy_rejection`，payload code 为 `sandbox_capability_unverified`，不会执行候选代码。
本地 CLI benchmark 测试须从批准源选择镜像，以当前真实 `sandbox-proof` 的 image、lock、containment 身份
构造 profile，并绑定已证明的 process capacity；不能沿用空 profile 或假设批准镜像仍不可用。
该成功路径应标记为 `oci`，验证 `completed` / `success`、真实 worker 输出与终止证据，同时保留单行 JSON
及不含 Workspace run 字段的 transport-only 断言；不可用 backend 的离线拒绝测试仍须保留。

## 创建工作区

为每位用户选择独立、可写、可备份的绝对路径。始终显式传入路径，不依赖 CLI 中维护者电脑的默认值。

```powershell
$env:STRATEGY_WORKSPACE_ROOT = "D:\quant-data\workspace"
uv run strategy-workspace --root $env:STRATEGY_WORKSPACE_ROOT init
uv run strategy-workspace --root $env:STRATEGY_WORKSPACE_ROOT doctor
```

Linux 使用对应的环境变量语法。工作区包含数据库和不可变制品，不应放进 Git，也不要直接读写其 SQLite、锁文件或制品目录。

## 准备策略包

优先从 `strategy-workspace/strategies/` 复制最接近目标市场的策略包，在新目录中修改。一个可运行策略包至少包含：

- `strategy.toml`：策略身份、参数 Schema、所需能力和引擎入口；
- `parameters.schema.json`：完整参数约束；
- Nautilus 正式策略实现；需要发现阶段时再提供 Qlib 入口；
- 策略依赖的包内辅助文件。

更新策略逻辑或辅助文件时递增 `revision`。先运行该策略包自带测试，再注册：

```powershell
uv run strategy-workspace --root $env:STRATEGY_WORKSPACE_ROOT package register <策略包目录>
```

注册会生成内容哈希。不要手工修改注册后的包引用，也不要把策略实现放入 Quant Runtime。

### 价格限制能力的准入兼容性

新的人工策略包只要声明 `market.cn.equity.price_limit`，就必须通过 Runtime 的行为一致性收据门禁。`quant-research.strategy-package.v1` 无法表达
`implementations.conformance`，因此声明该能力的新 v1 包会以类型化的
`price_limit_conformance_required` 拒绝；不能用旧版请求 Schema 绕过门禁。

已有的 Reference 包 `equity.cross-sectional-momentum-topk` revision 1 是历史准入例外。Runtime 会在预检证据中标记
`schema=quant-runtime.price-limit-admission.v1`、`status=historical_legacy`，但不会改写或重新注册它的包记录和字节。迁移时应复制策略并递增 revision，发布新的
`quant-research.strategy-package.v2` 包，同时加入
`[implementations.conformance] runtime = "conformance.py:conform"`，再为新包生成当前行为一致性收据。

## 生成研究请求

请求必须符合 Strategy Workspace 内置的 `quant-research.workspace-run-request.v2` Schema。以目标策略包的 `examples/*.json` 为模板，并填满全部占位符。至少核对：

- `strategy_package`：注册生成的包引用；使用运行命令的 `--package` 时会自动替换；
- `market_snapshot.source.base_url`：目标机可访问的 MarketHub 地址；
- `data_revision`：日线使用 `<data_version>:<stock_daily_1d版本>`，期货分钟使用 `future_bar_1m:<版本>`；
- `snapshot_id`：该冻结数据源、查询和版本的稳定 SHA-256 标识；任一内容变化都生成新标识；
- `query`：有序且不重复的标的、日期、频率和复权方式；A 股代码格式为 `SH.600000`、`SZ.000001` 等；
- `parameters`：必须通过策略的参数 Schema；
- `execution`：选择 `formal_only`、`discovery_formal`、`formal_comparison` 或 `agreement_gate`，并提供匹配的执行腿；
- 期货分钟请求必须给出 `contract_mapping`、连续合约方式、交易日、合约规格、滑点等有证据的冻结配置。

正式运行前，通过 `/api/health` 获取当前版本，并确认请求区间、标的和版本在 MarketHub 中完整可用。缺数据、版本漂移、排序错误或覆盖不全都应中止研究并修复数据源。

## 开始研究

```powershell
uv run quant-runtime run `
  --workspace $env:STRATEGY_WORKSPACE_ROOT `
  --package <策略包目录> `
  --request <请求文件.json>
```

命令标准输出只有一个 JSON。`completed` 表示完成，`rejected` 表示研究门有效拒绝，二者都不是进程故障；`failed` 才是执行失败。查看记录：

```powershell
uv run strategy-workspace --root $env:STRATEGY_WORKSPACE_ROOT run list
uv run strategy-workspace --root $env:STRATEGY_WORKSPACE_ROOT run show <任务ID>
```

失败后先修复原因，再显式创建新尝试：

```powershell
uv run quant-runtime retry --workspace $env:STRATEGY_WORKSPACE_ROOT --request-id <任务ID>
```

相同请求具有幂等身份；不要通过轻微改写 JSON 来逃避失败记录。研究结论必须引用 Workspace 中的结果、原生引擎证据和冻结数据版本。

## 常见故障

- 安装找不到 `strategy-workspace`：检查两个仓库是否同级及目录名是否准确。
- Python 或二进制依赖安装失败：确认使用 64 位 Python 3.12，并删除错误解释器创建的虚拟环境后重新 `uv sync`。
- MarketHub 无法连接：检查 DNS、端口、防火墙、代理和服务监听地址；不要改用本地假数据。
- 版本漂移或覆盖不全：冻结新版本并生成新快照身份，或先补齐 MarketHub 数据。
- 能力不匹配：让策略包声明与实现一致，且只使用已注册的 Qlib/Nautilus 能力。
- 运行占用过高：缩小标的或日期范围做冒烟测试，成功后再逐步扩大；不要降低完整性校验。

## 完成交付

只有同时满足以下条件，才可向用户报告部署完成：环境验证通过、MarketHub 真实可达、工作区健康、策略包测试并注册成功、至少一个真实请求达到 `completed` 或有证据的 `rejected`、任务 ID 与冻结数据版本已记录。任何条件未满足都应明确报告阻塞项，不得把“代码已安装”描述为“研究已完成”。
