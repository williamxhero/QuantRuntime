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
