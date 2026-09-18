# 场外基金板块扩展设计（详设提案）

> **状态**：PR-1 的数据契约与独立快照仓库已实现；PR-2 至 PR-6 的 Provider、同步、API、页面、回测等仍是【设计】，不得当作已支持能力。标记【现状】的原系统内容以核对时仓库代码为准；二开起点为 `feat/off-exchange-fund@630b21925ae60838cdf7fcae01a48d2fecc4467a`。PR-1 实际范围与验证见 §19。
>
> **二开原则**：按 [`CONTRIBUTING.md`](../CONTRIBUTING.md) 和 [`docs/secondary-development.md`](secondary-development.md) 的 L1/L2/L3 分级。参照 [`factor-system-design.md`](factor-system-design.md) 的“现状映射→完整契约→接口与界面→缓存→测试→PR→风险”组织方式，但不复用其股票因子与成交口径。

## 0. 决策摘要与验收边界

| 决策 | 必须成立的边界 | 二开级别 |
| --- | --- | --- |
| 场外基金独立业务域 | 不把基金净值当 OHLCV；不修改股票/ETF/指数的 `daily_pipeline`、仓库、策略、自选或现有回测引擎 | L2 |
| 三个独立页面 | `基金数据`、`自选基金`、`基金回测`分别注册静态路由和菜单；不进入股票自选菜单内部 | L2 |
| 独立同步 | 独立配置、源、数据目录、任务状态、执行入口、外部调度和缓存 generation | L2 |
| 类型是多维组合 | 公募/私募、开放/定开/封闭、投资类别、净值频率分别建模；成交规则以产品合同版本为准 | L2 |
| 不伪造统一基金回测 | 首期只放行具备完整合同与历史时间信息的**公募普通开放式**；其他类别可画像/自选，回测资格明确说明缺口 | L2 |
| 前端统一客户端 | 只对 `frontend/src/lib/api.ts` 与 `queryKeys.ts` 增加基金类型、方法与键；不复制请求层 | 局部 L3 |

本文的“场外”是**份额的办理渠道/交易机制**，不是投资标的里是否持有交易所证券。场外份额通过合同规定的认购、申购、赎回、转换、到期清算等方式变动，不按交易所撮合价成交。仅能在交易所交易的封闭式份额、ETF 场内份额、LOF 场内份额等不进入本域。LOF 等同时存在场内/场外份额时，只收录可识别的场外份额类别，且不得与场内代码自动合并。

监管上的开放/封闭运作方式与合同约定的申赎时间、价格、费用有关；私募信息的披露和可访问性不能按公募处理。工程规则以逐只基金合同、招募说明书/公告或已获授权的私募合同为最终输入，而非以类别枚举猜测具体 T+N、费率和披露时点。依据：[《证券投资基金法》](https://www.csrc.gov.cn/csrc/c101939/c1045353/content.shtml)、[《公开募集证券投资基金运作管理办法》](https://www.csrc.gov.cn/csrc/c106256/c1653978/content.shtml)、[中国基金业协会私募披露规则公告](https://www.amac.org.cn/xwfb/tzgg/202606/t20260605_27780.html)。本文是工程设计，不提供交易、合规或税务判断。

### 0.1 范围与明确不做

本期覆盖：场外基金的分类目录和净值/事件/条款数据；数据范围配置与画像；独立同步及可插拔源；独立自选基金；独立、无未来函数的基金回测框架及首期公募普通开放式可验证规则。所有类型都可进入数据模型，**不等于所有类型首期可回测**。

不做：将基金塞进现有自选/股票回测、在 `daily_pipeline` 增加 `fund` 分支、净值伪装成日 K、场内 ETF/LOF 交易仿真、实时盘口或分钟级价格、多用户私募权限系统、全局基金能力矩阵、应用内常驻基金 scheduler、未授权抓取私募数据、自动推断缺失合同条款。若后续必须做，另立设计与 PR。

### 0.2 可验收场景（需求编号供后文追踪）

| ID | Given / When / Then |
| --- | --- |
| R1 数据范围 | 给定可用基金 Provider，选择“类别/代码/全部”范围并同步后，基金目录和画像只反映基金仓库；停用扩展时旧数据页面不变。 |
| R2 数据画像 | 给定净值缺口、条款缺口、历史修订，画像分别显示覆盖率、缺口原因和快照代次，不把“未同步”当成零。 |
| R3 独立同步 | 基金同步运行、失败、取消、重启或与股票同步同时执行时，基金与股票各自的任务状态、文件和缓存互不覆盖。 |
| R4 自选基金 | 搜索精确份额类别后加入、分组、置顶、备注、批量导入/移除，操作只更新基金自选；无净值时仍保留条目。 |
| R5 回测 | 信号时点未知的净值/公告不可用于信号；订单只能在合同允许的窗口申请，结果按确认/到账过账并携带条款版本。 |
| R6 类型差异 | 定开窗口外、封闭期赎回、私募无权限/无完整条款、特殊类型无适配器均明确拒绝“真实回测”。 |

## 1. 当前代码基线与差距【现状】

| 主题 | 可核对位置及现有能力 | 缺口/本设计结论 |
| --- | --- | --- |
| 前端注册 | `frontend/src/extensions/bootstrap.ts` 发现 `custom/*/extension.tsx`；`types.ts` 提供静态绝对路由、导航；`registry.ts` 做冲突/版本隔离 | 可直接注册三个独立基金页面；无 `data.*`、`backtest.*` 或 `watchlist.main` 插槽 |
| 导航位置 | `frontend/src/components/Layout.tsx` 的 `allNav=[...nav,...analysisNav,...extensionNav]`；`frontend/src/pages/settings/MenuSettings.tsx` 仅管理内置/分析项 | 扩展菜单默认在末尾；“紧邻自选”和菜单设置中隐藏/排序不在首期保证 |
| 前端数据 | `frontend/src/lib/api.ts` 中 `request` 未导出；`queryKeys.ts` 集中键与行情 SSE 失效前缀 | 需局部 L3 增加基金 API 方法/键；基金键不进股票行情 SSE 前缀 |
| 后端注册 | `backend/app/extensions/loader.py` 发现 `app.custom.*`；`registry.py` 可 `include_router`；`contracts.py` 的 `ExtensionContext` 给 `data_dir` 与只读股票仓库 | 可注册独立 `/api/funds/*`；无扩展 `shutdown` 钩子，也无可写基金仓库协议 |
| Provider | `backend/app/data_providers/custom/loader.py` 动态加载 `plugin.yaml` 的 Python entry；`provider_has_dataset` 实际查 `provider.config.datasets` | **只在清单中写基金 dataset 不够**；插件实例必须真实暴露对应配置和调用方法；普通 YAML 源的 `custom/config.py` 过滤基金 dataset |
| 能力矩阵 | `backend/app/data_providers/capabilities.py` 注册现有通用数据集 | 不声称新基金能力已进入全局矩阵；基金管道只选一个显式 Provider，不建立平行全局路由 |
| 核心同步任务 | `backend/app/api/pipeline.py` 使用 `daily_pipeline`、进程内 `job_store` 与执行槽；`services/pipeline_jobs.py` 的 `JobStore` 构造时会把磁盘 `pending/running` 当孤儿 | **不可让基金 API 与独立 CLI 共用该类实例/目录**：第二进程启动会误判第一进程任务；基金需独立的跨进程租约与原子状态存储 |
| 自选 | `backend/app/services/watchlist.py` 使用 `data/user_data/watchlist.parquet`，支持多分组、置顶、清空；`api/watchlist.py` 有 CSV/图片等导入 | 参考交互，不复用股票身份/报价，不写原自选文件 |
| 回测 | `backend/app/api/backtest.py` 与 `backend/app/backtest/` 按股票/ETF K 线交易语义工作 | 不传入 `asset_type=fund`；新增基金净值/申赎账本，不复用股票成交内核 |
| 认证 | `backend/app/services/auth.py` 明确为**单密码、自托管、非多用户**；`main.py` 对 `/api/*` 统一认证 | 私募只能设计为授权的单用户本地/自托管数据；现有认证不能证明每位投资者的数据隔离 |

现有相邻测试：`backend/tests/test_extensions.py`（注册与隔离）、`test_watchlist_groups.py`（自选组和旧 schema）、`test_pipeline_and_monitor_fixes.py` 与 `test_pipeline_pull_types.py`（现有管道）、`test_backtest_etf.py`（交易资产边界）。实施时先扩这些特征化/回归测试，再添加基金域测试；不得将本提案中的拟议接口当成已有实现。

## 2. 分层架构、依赖与目录【设计】

```text
基金 Python 插件〔fund_catalog / fund_nav / fund_events / fund_terms〕
        │ 标准化记录 + 实际能力声明（不把净值转为 K 线）
        ▼
FundProviderPort → FundPipeline ── FundRunStore / CLI 外部调度
                      │ 校验、批量暂存、快照发布、代次
                      ▼
                   FundRepository ── data/funds/
                      ├─ FundProfileService ── /api/funds/data/*      ── 基金数据页
                      ├─ FundWatchlistService ── /api/funds/watchlist/* ── 自选基金页
                      └─ FundBacktestService ── /api/funds/backtests/*  ── 基金回测页
                                                                   │
                                          frontend/src/lib/api.ts + queryKeys.ts
```

建议落点：

| 模块 | 拟议文件 | 职责 | 禁止依赖 |
| --- | --- | --- | --- |
| 注册入口 | `backend/app/custom/funds.py` | `setup(registrar)` 注册路由，`startup(context)` 注入数据目录/启动轻量恢复 | 不在 import 时启动线程、写用户数据 |
| 领域契约 | `backend/app/funds/contracts.py` | 标识、四类数据集、错误码、数值/时间类型 | 不依赖供应商字段 |
| Provider 适配 | `backend/app/funds/provider.py` 与 `backend/app/plugins/<provider>/` | 对真实插件做能力检查、数据归一、速率限制 | 不从页面或回测直接请求供应商 |
| 仓库 | `backend/app/funds/repository.py` | 分区、快照代次、原子发布、点时读取 | 不写 `kline_*` 或 `watchlist.parquet` |
| 同步 | `backend/app/funds/pipeline.py`、`run_store.py`、`cli.py` | 增量、恢复、并发/租约、任务/CLI | 不调用 `daily_pipeline.run_now` 或核心全局 `job_store` |
| 业务 | `profile.py`、`watchlist.py`、`backtest.py` | 画像、自选、申赎账本 | 不继承大型股票回测引擎 |
| API | `backend/app/funds/api.py` | FastAPI 参数、状态码、响应映射 | 不做重计算/逐行仓库扫描 |
| 页面 | `frontend/src/custom/funds/extension.tsx` 及页面/组件 | 三条路由、导航、五态交互 | 不复制完整股票页面 |

后端 `setup` 在应用初始化时发生、`startup` 在核心数据层准备后发生；基金服务应经显式依赖获取在 `startup` 创建的上下文，若未初始化则给出可诊断的 503，不用隐式全局股票 `app.state.repo`。插件/基金扩展失败应只禁用基金功能，核心路由可用。版本边界：前后端扩展注册的 `apiVersion=1`【现状】与基金持久化的 `schema_version`【设计】相互独立，不能混用。

## 3. 基金身份、分类与准入【设计】

### 3.1 分类模型

`FundIdentity` 至少包含 `fund_id`（内部稳定 ID，不以六位代码作唯一键）、`issuer_id`、`source_namespace`、`provider_code`、`share_class`、`name`、`aliases[]`、`channel=off_exchange`。跨源匹配需要明确映射表与人工/规则审核，不因代码相似、名称相同自动合并。A/C 份额、币种份额或费率不同的份额类别是不同 `fund_id`；更名/转型以有效期记录，不覆盖历史。

`FundClassification` 是独立轴，不用一个 `fund_type` 枚举承载全部含义：

| 轴 | 允许值（首期） | 规则含义 |
| --- | --- | --- |
| `offering_type` | `public / private / unknown` | 数据公开性、权限与合同来源；`unknown` 不可回测 |
| `operation_mode` | `open / periodic_open / closed / other / unknown` | 可申请窗口；私募也可以开放、定开或封闭 |
| `investment_category` | `equity / bond / mixed / money_market / qdii / fof / alternative / other / unknown` | 展示及特殊估值适配，不决定是否“自动可回测” |
| `nav_frequency` | `daily / weekly / monthly / irregular / unknown` | 预期披露节奏；不能从缺净值反推停牌或零收益 |
| `channel` | `off_exchange`（本域固定） | 拒绝只有场内交易的份额 |

每个分类/状态记录带 `valid_from`、`valid_to`（半开区间）、`published_at`、`source_ref`、`revision`。历史计算先限制生效时间，再限制当时可获得时间；旧分类不能被今天的新分类回填。`unknown` 可以搜索/展示，但不得猜测申赎规则。

### 3.2 类型 × 功能矩阵

| 组合 | 数据与自选 | 同步特殊点 | 真实回测准入 |
| --- | --- | --- | --- |
| 公募普通开放式 | 公开目录/净值/详情/自选 | 开放日与实际披露时间分开；分红、拆分、费率版本 | **首期候选**；有完整日历、合同、费率、事件覆盖和点时净值才放行 |
| 公募定期开放 | 同上并标开放窗口 | 开放期/暂停公告需要点时版本 | 首期只报资格缺口；后续规则实现后仅窗口内可申请 |
| 公募封闭运作的场外份额 | 展示估值、期限/到期或转型事件 | 净值可低频；不把无净值日补成日线 | 封闭期无普通赎回；仅合同规定的到期/转型/场外转让规则经单独验证后启用 |
| 私募开放/定开/封闭 | **仅授权的单用户数据域可见**；不进公开搜索 | 合同、持有人资格、披露和管理/业绩报酬约束 | 首期一律不可真实回测；权限模型与完整条款样本具备后单独评审 |
| 公募货币、QDII 等特殊类别 | 可分类展示和自选 | 收益指标/计价币种/跨市场日历可能不同 | 不套普通开放式规则；专用估值与币种适配、有黄金样本后放行 |

`backtest_eligibility(fund_id, start, end, mode)` 的返回是“**基金 × 窗口 × 规则版本**”结果，含 `eligible`、`policy_id`、`reasons[]`、`data_generation`、`checked_at`；不是仅按 `operation_mode` 的布尔开关。首期只需内置一个经过验证的 `PublicOpenPolicy`，`FundTradePolicy/FundValuationPolicy` 为基金域内部小接口【设计】，不预建万能插件策略框架。

## 4. 标准数据契约与质量闸【设计】

### 4.1 通用约定

- `nav_date` 是估值归属日，`published_at` 是外部首次公开/授权可得时间，`ingested_at` 是本系统收到时间，三者绝不互代。`ingested_at` 不能反推历史 `published_at`；无法追溯历史披露时点时，可展示净值但不能做依赖该历史窗口的真实回测。
- 时间戳存带时区的 UTC 值，页面按时区展示；基金截止时点以合同指定时区解释，不能用服务器本地时区。日期是日历日期，不假定 A 股交易日；基金自己的开放日历由条款和公告确定。
- 金额、份额、净值和费率使用十进制定点数；精度、舍入方向、费用计算顺序与份额确认方式从当期合同条款读取。API 用十进制字符串传值，不用 JS 浮点作账本权威。
- 各数据集均有 `source_id`、`source_ref`、`revision`、`ingested_at`、`schema_version`；原始供应商负载仅在授权允许时留存且不写公开日志。
- 所有数据读取以同一已发布 `generation` 快照为基准；尚在同步中的分区不能混入画像、自选或回测。

### 4.2 数据集字段

| 数据集 | 必需字段 / 主键 | 语义与校验 |
| --- | --- | --- |
| `fund_catalog` | `fund_id, provider_code, share_class, name, offering_type, operation_mode, investment_category, nav_frequency, channel, currency, status, valid_from, valid_to?, published_at?`；键为 `(fund_id, valid_from, revision)` | 类型必须独立轴；`channel` 必须是 `off_exchange`；有效区间不重叠；来源与更名映射可追溯 |
| `fund_nav` | `fund_id, nav_date, unit_nav, currency, published_at?, revision`；`accumulated_nav?`、`valuation_status`；键为 `(fund_id, nav_date, revision)` | `unit_nav>0`；修订保留旧版/新版本；未知披露时间可展示但不可真实回测；累计净值**不与分红现金重复计收益** |
| `fund_events` | `event_id, fund_id, event_type, effective_date, published_at, revision`；分红还需权益登记/除息/支付日和每份金额，拆分需比例 | 支持 `distribution/split/conversion/liquidation/suspension/resumption` 等；事件去重；不能把“没拿到”当“无事件” |
| `fund_terms` | `term_id, fund_id, valid_from, valid_to?, published_at, source_ref, rule_version`，并含开放窗口、截止时间/时区、确认/到账、暂停、费用阶梯、最低额、币种和舍入规则 | 合同字段缺失按用途标 `unknown`；不能填全局默认 T+N 或费率；重叠/无来源版本拒绝发布 |
| `fund_coverage` | `fund_id, dataset, start, end, status, source_id, checked_at, generation` | `status=complete/partial/unavailable/unchecked`；对事件/条款明确“已核验无事件/无变更”还是“未取得”，供回测准入判定 |

`fund_coverage` 是系统核验结果，不是供应商凭空给出的金融数据。其 `complete` 只表示指定来源、指定窗口的检查按该源能力完成，不保证源本身绝对完整；回测资格还需来源可信度和合同样本校验。源冲突时保留各源记录并标冲突，不按“最后写入者胜”静默覆盖。首期不做自动跨源融合，只选择一个显式来源及其配套合同数据。

### 4.3 净值、事件与条款的发布闸

1. Provider 输出标准化记录；校验基金 ID、份额、币种、日期、正净值、修订顺序、重复键和非空的来源引用。
2. 增量批次先写基金域暂存目录，核对目录/净值/事件/条款的引用完整性；非必需数据集可标 `unavailable`，但不得宣称回测可用。
3. 若修订了历史净值或事件，保留修订来源、时间与受影响日期范围；不能覆盖原值后只留最终值。
4. 聚合出 `coverage`、画像摘要和新 `generation`，再一次性切换已发布 manifest 指针；失败保持上一代可读且标注“最近同步失败/数据仍为旧快照”。
5. 无法给出估值日与披露时间关系、条款有效期或事件覆盖时，`eligibility` fail-closed；显示侧可降级为“可浏览、不可真实回测”。

## 5. 可插拔 Provider 与数据范围【设计】

### 5.1 最小 Provider 端口

```python
class FundProviderPort(Protocol):  # 拟议内部接口，仓库中不存在
    name: str
    def list_funds(self, scope, cursor=None): ...
    def get_nav(self, fund_ids, start, end, cursor=None): ...
    def get_events(self, fund_ids, start, end, cursor=None): ...
    def get_terms(self, fund_ids, start, end, cursor=None): ...
```

每种方法返回标准化记录、分页游标、来源/披露时间证据和明确的 `success_empty / unsupported / failed` 状态；不能用 `[]` 同时代表“查完无数据”和“网络失败”。`get_events/get_terms` 可缺，但缺能力时回测资格应显示缺口。字段标准化属于 Provider 边界，基金 pipeline 不知道供应商的原始字段名。分页、超时、限速、重试和幂等请求按源能力声明，不能无限重试。

当前内置插件 loader 可以加载 Python entry，但其 `provider_has_dataset(name, dataset)` 检查的是 `provider.config.datasets`【现状】。故基金插件需**同时**在 `plugin.yaml` 写 dataset 列表、在实例 `config.datasets` 暴露同名真实能力、实现对应基金方法和现有 loader 所需的关闭/自检生命周期，并通过契约测试；仅填清单不足以路由。普通用户 YAML 源当前只识别 `daily/adj_factor/realtime/minute/financial`，基金首期不走 YAML。若未来要支持 YAML，必须先扩其 schema、映射、校验和测试（单独小 PR，可能触及核心 L3），不能把现状写成已经可用。

### 5.2 选源与范围

`FundSourceConfig` 存在基金域配置：`provider_id`、`scope={all|categories|ids}`、`categories[]`、`fund_ids[]`、`history_start`、`rate_limit`、`retry_budget`、`schedule_target?`、`config_version`。`all` 仅在 Provider 明确支持目录枚举、数据使用许可允许且用户确认范围时可选；私募不允许“全市场扫取”。私募源不加入公开选源默认列表，必须显式启用且确认授权。

首期一个同步 run 只选一个 Provider，源切换不覆盖旧源的原始记录；同一基金跨源差异显示冲突并需人工选择生效口径。基金页面做域内**选源和可用性说明**，不把它伪装成 `/api/settings/capability-matrix` 的全局能力路由；现有矩阵仍是股票等通用能力的单一权威。若以后要统一进矩阵，需按 `capabilities.py` 的注册表、偏好 getter、设置 API、前端与矩阵测试完整接入，单独评估 L3。

数据使用、再分发与本地存储许可应在选定 Provider 前完成核验；无合法可用源时首期交付可用固定测试 Provider 和明确的“未配置生产数据源”空态，不用无授权网站抓取作为兜底。

## 6. 基金仓库与版本化【设计】

```text
data/funds/
  staging/<generation>/public/{fund_catalog,fund_nav,fund_events,fund_terms,fund_coverage}.parquet
  snapshots/<generation>/public/{同上}.parquet
  snapshots/<generation>/manifest.json
  current.json                   # 已发布代次指针
  config.json                    # PR-2/3 拟议，尚不存在
  private/                       # 首期仅保留边界，不自动创建多用户目录
  watchlist/entries.parquet + groups.json  # PR-4 拟议
  runs/<run_id>.json             # PR-2 拟议
  backtests/<id>.json            # PR-5 拟议
```

`FundRepository`【PR-1 已实现基础】是基金数据唯一读写入口，不继承 `DataStore` 或 `KlineRepository`，不读写 `data/user_data/watchlist.parquet`、`kline_*`、`enriched` 或现有回测缓存。PR-1 的 `publish(snapshot)` 接受完整快照，负责校验引用、修订和区间、写 Parquet、计算哈希/行数/分类摘要并原子切换指针；`current()/read(dataset, generation?)` 只读已发布代次。PR-2 再负责增量合并与跨进程租约，PR-1 不承诺多写者并发发布。路径验证必须保证后续清理由 `data/funds/staging/<validated_generation>` 等精确范围发起，禁止任意用户路径。PR-1 对未知高版本拒读；未来旧 schema 兼容/迁移应逐版设计，不能用缺失值伪造回测资格。

发布流程：暂存完整代次 → 校验 manifest 文件清单/哈希与引用 → 在短临界区原子替换 `current.json` → 推进基金 `generation` → 失效基金服务缓存和前端基金查询。读请求先固定一次 generation，再读同代次各数据集，避免新净值配旧条款。快照版本应保留至少上一有效代以支持失败回退；清理策略另行受控实现，不在发布中删除用户自选或旧结果。

## 7. 独立同步 pipeline、任务与调度【设计】

### 7.1 状态机与阶段

```text
queued → running(catalog → nav → events → terms → verify → publish) → succeeded
   │          │                          │                 │
   └──────────┴──────────→ failed / cancelled / interrupted
```

`events/terms` 阶段在 Provider 无能力时记录 `unsupported`，不把它记为成功覆盖；相应基金不可真实回测。每个阶段记录 `attempted/succeeded/empty/failed/unsupported` 数量、游标、日期范围、耗时与安全的错误摘要。游标只在该批次校验且暂存完成后推进；重试相同游标不得产生重复发布。取消/超时在批次边界检查，发布阶段不可部分取消。旧快照继续可读。

`FundPipeline.run(config, resume_from?)` 只组合 Provider→标准化→仓库暂存/校验/发布，不调用 `daily_pipeline`，不向股票 SSE 发事件。API 手动触发和 CLI 定时触发走同一业务入口及状态记录；前者用基金专属有界 worker，后者由外部调度器启动单次进程。共享磁盘/CPU 需并发预算，但“任务独立”不意味着完全无资源竞争。

### 7.2 跨进程执行语义

不能直接复用核心 `JobStore` 作为 API/CLI 共用任务账本：它在构造时把磁盘上的 `pending/running` 补录为孤儿，第二进程会误伤第一进程；`run_with_capacity()` 也绑定核心全局 `job_store`。基金域需要**小型专属** `FundRunStore`：任务 JSON 原子替换、单 run 租约（原子创建，含随机 owner ID、PID、心跳/租约截止）、明确的进程存活/租约过期恢复、跨进程单飞、取消请求持久化。租约只表示运行所有权，不在网络请求期间持有进程内互斥锁；短锁仅用于元数据更改与快照指针切换。不能单靠 PID 判活（容器/主机 PID 复用），恢复需 owner ID + 心跳 + 到期后的受控抢占。两进程竞争同一 `data/funds` 时只能有一个同步 run；第二个返回活跃 run ID 或明确 409。租约失败不得开始下载或覆盖。

API 取消将 `cancel_requested` 写盘，worker/CLI 每批检查；超时要能终止源调用或在超时后阻止其发布，不能仅把 UI 标成失败而僵尸线程继续写文件。进程崩溃后的 `interrupted` 与用户取消 `cancelled` 分开。任务快照中错误摘要不得含 Token、私募合同全文或净值明细。

CLI 不应为了拿到 Provider 而导入 `backend/app/main.py`（否则会构造 FastAPI 与核心全局任务对象）；它应显式初始化基金配置和现有插件加载器、执行一次 `FundPipeline`、最后关闭源与释放租约。跨进程保证只针对同一台主机/同一受支持本地文件系统；若部署到不提供可靠原子创建/替换的网络共享存储，启动前拒绝并提示需要另行设计分布式协调，不宣称文件租约天然支持多主机。

### 7.3 外部调度

首期只提供单次 CLI（拟议：`cd backend && uv run python -m app.funds.cli sync --scheduled`）；cron、systemd timer、Windows 任务计划或 Docker sidecar/cron 负责定时。**当前扩展只有 `startup` 没有 `shutdown`**，不在应用进程启动永久 scheduler。基金页面只展示“目标时间（配置）”“最近计划触发”“最近成功”，不能因为填写了目标时间就显示“已启用”。跨平台无法通用地探测外部任务是否已安装：未收到调度心跳时标“未验证/未触发”，并提供部署说明。若要页面一键安装并管理系统任务，另评估权限和跨平台风险，非首期。

## 8. 基金数据范围与画像页面【设计】

路由 `/funds/data`，导航“基金数据”。顶部配置区：当前源、可用 dataset、全量/分类/指定 ID、历史起点、数据许可提示、手动同步/取消及 CLI 调度状态。中部同步区：状态机、阶段进度、已完成/空/失败/不支持数、错误摘要、旧快照代次。下部画像：基金数与份额数、按募集/运作/投资类别交叉分布、净值覆盖、最新估值日、最新披露时间、事件/条款核验覆盖、可真实回测数和原因分布。所有统计由发布时生成的小型汇总/分区元数据读取，不在页面请求里逐只全量扫描。

画像字段示例：`universe_count`、`share_class_count`、`nav_coverage_ratio`（分母明确为选定范围内按该基金披露频率预期的观测点，未知频率不计入比例并单列）、`terms_verified_count`、`events_checked_count`、`eligible_count`、`as_of_generation`、`last_success_at`、`last_attempt_at`、`is_stale`。私募首期不计入公开画像；即便返回 0 也不能泄露未授权私募记录数量。

页面状态：无源→引导配置/测试源；源不支持目录→禁用全量；同步中→保留上一代画像并显示进度；失败→保留上一代并展示原因；无净值→目录可见但覆盖缺失；数据冲突/未授权→阻断发布或隐藏敏感字段。桌面与窄屏都要检查表格滚动、操作禁用和长错误文案。

## 9. “自选基金”独立页面与存储【设计】

路由 `/funds/watchlist`，导航“自选基金”。身份键为 `fund_id`，条目存 `fund_id, added_at, note, pinned_rank?, group_ids[], row_revision`；分组存 `id, name, color, order`。与现有股票自选一样，一个条目可以属于多个组；“从组移出”仅删关联，“从自选移出”才删条目。删除组不删除成员，清空组和清空全部均需确认；服务端做分组 ID/名称、份额身份和权限校验。基金文件在 `data/funds/watchlist/`，绝不进入 `data/user_data/watchlist.parquet`。并发写以基金域短事务锁/版本比较和原子替换防丢更新，不能以浏览器旧列表覆盖新列表。

| 股票自选已有体验 | 基金页首期等价/差异 |
| --- | --- |
| 名称/代码搜索、单条/批量添加 | 用基金目录搜索；返回份额类别、募集/运作方式、来源；同码多份额须显式选择，未知代码不猜为股票 |
| 分组创建/重命名/排序/删除、多组归属 | 保持相同交互语义；组与成员持久化独立于股票 |
| 置顶、备注、筛选/排序、移除/清空 | 保持；按最近估值日/披露日、分类或名称排序，不按实时涨跌幅默认排序 |
| CSV/代码导入 | 先实现文本/CSV 候选预览→歧义确认→批量添加；限文件大小、条数、编码和重复项，不能上传后直接覆写 |
| 图片 OCR 导入 | **不直接复用股票 OCR 的代码推断**；仅在基金识别准确率、候选确认和误识别测试通过后追加，未实现时 UI 不展示入口 |
| 行情/图表/详情 | 单位净值、累计净值、估值日、披露时间、历史净值曲线、分红/折算、数据来源；无盘口、量价、涨停、分钟 K |

无净值的基金仍可加入，卡片显示“未同步净值”，不显示 0 或“今日跌 0%”。区间收益只能在数据/事件覆盖满足口径时计算，并注明“净值变化”或“含分红总回报”；未知事件不能默认为无分红。净值图仅展示历史观测点，低频披露不能补画每日虚拟点。私募条目首期默认不开放；后续仅在单用户自托管且授权、存储和导出路径均核验后启用，不能进入公开搜索/通用导出。删除自选不删除基金历史净值或回测结果。

线框：

```text
[自选基金] [搜索名称/代码/份额类别...] [导入] [新建分组]
[全部 | 我的分组...]  [募集方式] [运作方式] [投资类别] [排序]
基金名称·份额  分类  最近估值日/披露时间  单位/累计净值  净值区间变化  数据状态
  └─ [净值详情/事件] [备注] [置顶] [加入/移出分组] [移出自选] [去基金回测]
```

“去基金回测”仅带 `fund_id` 跳转，不默认宣称可回测；到回测页后重新检查资格。加载、无目录、无净值、导入歧义、同步中、无权限、失败和窄屏状态均需设计。所有新增弹窗重开时重置草稿，批量预览不修改正式条目。

## 10. 独立基金回测【设计】

### 10.1 服务边界与策略

`FundBacktestService` 读取基金仓库同一已发布 generation 的目录、净值、事件、条款和覆盖记录；输出基金专属订单/确认/现金/份额账本及绩效。它不调用现有 `BacktestEngine` 的股票成交函数，也不向其矩阵塞一列虚构 `fund`。可以复用通用图表或纯数学绩效函数，但复用前必须证明其日期、分红、现金流及年化口径与基金含义相同；不直接继承股票绩效模型。

首期只需 `periodic_invest`（定额申购）和 `hold_redeem`（买入持有/按条件申请赎回）两个可测试策略；目标权重再平衡可在多基金订单账本稳定后增加。定期投资的周期按基金开放日历处理，节假日或暂停时执行“拒绝/顺延”必须由**显式策略参数与当期条款**共同决定，不能静默顺延。股票因子、分钟择时、盘口成本与涨跌停不可成交规则不进入基金策略。

### 10.2 资格闸与结果模式

真实回测 `mode=verified` 的入口逐项核查：基金身份和渠道可确认；整个窗口内分类和合同有效期连续；开放日/截止/暂停规则可回放；费率和舍入规则完整；净值与披露时间可点时读取；分红/拆分/转换等事件已核验；所有持有日估值和订单确认日所需数据充分；币种无需未实现的换汇。任一项不满足返回结构化 `reasons[]` 与最早缺口，不创建“成功但简化”的真实结果。

`mode=scenario` 只能由用户明确选择。用户可输入假设费率/确认日等，结果标题、API 与导出均标“情景模拟”，回显全部假设与无法验证的字段，不得与 `verified` 结果同一列表不加区分。对于缺历史 `published_at` 的旧净值，不能仅因用户勾选某项就把它冒充当时已知；情景模式可做事后静态现金流演示，但须明确不可用于无未来函数策略评价。首期如果没有安全的情景计算实现，可只显示资格缺口而不开放 `scenario` 提交。

### 10.3 时间线与订单状态机

```text
信号时刻(signal_at)
  → 申请时刻(submitted_at) [合同开放窗、截止点、暂停检查]
  → 适用估值日(nav_date) [提交时价格可以未知]
  → 净值披露(published_at)
  → 份额/金额确认(confirmed_at)
  → 赎回款到账(settled_at)
```

在 `signal_at=t` 只允许读取当时 `published_at<=t` 且已生效的目录/净值/事件/条款。若 6 月 2 日 14:30 申请申购，6 月 2 日净值到 6 月 3 日才披露，**申请决策不得读取 6 月 2 日净值**；但申购确认可在净值披露后按合同适用该净值结算。下单路径与事后估值路径分离：事后净值可以用于结果复盘，不得回灌历史信号。条款即使 `valid_from<=t`，若 `published_at>t` 也不能用于当时的决策；如果现实会以某合同先行执行却缺历史披露证据，真实回测仍不可验证。

订单状态：`draft → submitted → pending_nav → confirmed → settled`，并有 `rejected/cancelled` 终态。申购按金额申请，赎回按份额申请；申购费、赎回费、销售服务费、管理费、业绩报酬依合同确定计提主体与时点，不能全部再从已扣费净值中重复扣一次。确认前不增份额，赎回未到账前不把应收款作为可再投资现金；份额折算、现金分红、红利再投、到期清算单独过账。基金合同若不提供特定行为（例如场外份额转让），该行为不存在于引擎中，不猜规则。

账本金额使用 Decimal，形成 `cash_available`、`cash_receivable`、`units_by_fund`、`fees`、`event_cashflows`。每笔必须可追踪 `order_id, fund_id, signal_at, submitted_at, nav_date, nav_revision, published_at, confirmed_at, settled_at, term_id, rule_version, fee_breakdown, state`。现金/份额守恒、不得负可用现金或赎回超过已确认份额；现金流后绩效应明确采用时间加权收益还是资金加权收益，不以累计净值代替投资者总回报。多币种/汇率缺失时拒绝真实组合回测。

### 10.4 结果与缓存键

结果含：`run_id, mode, strategy_id/version, input_echo, generation, provider_id, fund_ids, date_range, policy_id/version, term_versions[], assumptions[], warnings[], orders[], daily_account_values[], metrics, created_at`。指标至少给期末资产、净投入、资金加权收益（若可解）、时间加权净值/收益、最大回撤与费用合计；低频净值区间不伪造日日收益，年化与 Sharpe 的样本频率/缺失情况必须显示，样本不足返回 null 而非 0。分红处理选择要进入结果与缓存键。

缓存键必须覆盖：`fund_ids+share_class`、窗口、策略及参数、订单日历、费用/条款版本、事件/净值 generation、披露时间策略、币种、模式和指标口径版本。历史结果携带旧 generation 可只读查看并标“源数据已修订”，不能静默用新数据改写旧结果。改同一个基金的历史净值/条款后仅失效受影响基金与窗口的结果，不清空股票回测缓存。

## 11. HTTP API 与错误契约【设计】

全部路径均为**拟议** `/api/funds/*`，经扩展 `include_router` 接入并继承现有 `/api/*` 认证中间件。服务层先校验域身份/授权，API 层只做参数与响应映射。响应携带 `schema_version=1`；成功、空数据与错误有不同状态，不把 `200 + []` 当成网络失败。对列表加 `limit/cursor` 和最大范围约束；对创建任务要求防重复提交的 `idempotency_key`。动态 `{fund_id}` 路由应置于 `/catalog/{fund_id}` 等固定前缀下，避免与 `/search` 等静态路径冲突。

| 端点 | 请求关键字段 | 响应关键字段 / 失败语义 |
| --- | --- | --- |
| `GET /api/funds/providers` | 无 | 真实加载状态、dataset 能力与失败原因；未装插件不可选择 |
| `GET/PUT /api/funds/config` | `provider_id, scope, history_start, config_version` | 生效配置/版本；未知源或未授权范围 422/403；旧配置缺新字段走显式默认 |
| `GET /api/funds/data/profile` | `scope, as_of_generation?` | 第 8 节画像和覆盖缺口；无已发布快照则空态 |
| `POST /api/funds/sync/runs` | `scope, start, end, idempotency_key` | `run_id, reused, state`；已有活跃 run 返回复用或 409 |
| `GET /api/funds/sync/runs/{run_id}` | 无 | 阶段、计数、游标摘要、状态、最近心跳；不存在 404 |
| `POST /api/funds/sync/runs/{run_id}/cancel` | 无 | 协作式取消请求已记录；终态任务返回明确冲突 |
| `GET /api/funds/catalog/search` | `q, offering_type?, operation_mode?, limit, cursor` | 份额级候选与来源；空搜索不返回私募受限记录 |
| `GET /api/funds/catalog/{fund_id}/nav` | `start, end, limit` | 估值日、披露日、单位/累计净值与覆盖状态；未知标的 404 |
| `GET/POST /api/funds/watchlist`；`DELETE /api/funds/watchlist/{fund_id}` | `fund_id`、预期版本 | 自选列表/操作结果；版本冲突 409；删除仅删自选条目 |
| `POST /api/funds/watchlist/import-preview`；`POST /api/funds/watchlist/batch` | 文本/CSV 候选、份额歧义的用户选择 | 先预览后确认批量添加；容量/编码/重复项验证 |
| `GET/POST /api/funds/watchlist/groups`；`PATCH/DELETE /api/funds/watchlist/groups/{group_id}` | 组 ID、名称、颜色 | 组列表/变更；非法组、重复名、越权均拒绝 |
| `PUT /api/funds/watchlist/groups/order`；`PUT/DELETE /api/funds/watchlist/groups/{group_id}/members/{fund_id}` | 完整组顺序或成员关系、预期版本 | 多组归属、顺序稳定；不一致则 409 |
| `PATCH /api/funds/watchlist/{fund_id}` | 备注、置顶序号、预期版本 | 更新单条；无净值也允许编辑 |
| `GET /api/funds/backtest/eligibility` | `fund_ids, start, end, mode` | 每只基金的资格/缺口/规则版本；无权限不泄露基金身份 |
| `POST /api/funds/backtests` | 基金、窗口、策略、模式、参数、幂等键 | `run_id`；未满足 `verified` 资格时 422 且无“成功”结果 |
| `GET /api/funds/backtests/{run_id}` | 无 | 状态、订单、账本、指标/风险提示；不返回私募未授权详情 |

示例：真实回测资格失败（错误码和字段名均为拟议契约）：

```json
{
  "schema_version": 1,
  "eligible": false,
  "fund_id": "public:issuer-001:class-a",
  "window": {"start": "2024-01-01", "end": "2025-12-31"},
  "reasons": [
    {"code": "FUND_NAV_PUBLICATION_UNKNOWN", "dataset": "fund_nav", "first_gap": "2024-01-02"},
    {"code": "FUND_TERMS_INCOMPLETE", "dataset": "fund_terms", "field": "redemption_fee"}
  ],
  "generation": "g42"
}
```

示例：真实回测提交（若资格不通过，不创建结果）：

```json
{
  "fund_ids": ["public:issuer-001:class-a"],
  "start": "2024-01-01",
  "end": "2025-12-31",
  "strategy": {"id": "periodic_invest", "version": 1, "amount": "1000.00", "currency": "CNY", "frequency": "monthly"},
  "mode": "verified",
  "idempotency_key": "ui-20260918-001"
}
```

服务器应返回任务 ID、资格快照版本和输入回显；即便当前资格检查通过，执行期间发现修订、数据代次不一致或权限撤销也要终止 verified 任务并返回明确错误，不得切换成 scenario。

错误目录：`FUND_PROVIDER_UNAVAILABLE`（503）、`FUND_SCOPE_UNSUPPORTED`（422）、`FUND_ID_AMBIGUOUS`（409）、`FUND_DATA_INCOMPLETE`（422）、`FUND_NAV_PUBLICATION_UNKNOWN`（422）、`FUND_TERMS_INCOMPLETE`（422）、`FUND_WINDOW_CLOSED`（422）、`FUND_SYNC_ALREADY_RUNNING`（409）、`FUND_PRIVATE_FORBIDDEN`（403）、`FUND_SCHEMA_TOO_NEW`（409）。服务端日志可带匿名 run ID，但 API 错误不回传绝对路径、Token、原始私募合同、供应商完整错误体。无权限私募搜索/详情还需防枚举（统一不可见/404 策略），不能从 403 与 404 差异反推出名称。

## 12. 前端导航、查询与状态【设计】

`frontend/src/custom/funds/extension.tsx` 注册三个静态路由 `/funds/data`、`/funds/watchlist`、`/funds/backtest`，对应独立导航 ID。无 `/watchlist` 页签或 `/backtest` 页签，扩展缺失时核心页面正常。侧栏默认位置在扩展导航区域；产品如要求“自选基金紧邻自选”或出现在 `MenuSettings` 的排序/隐藏列表，须对 `Layout.tsx`、`MenuSettings.tsx` 与偏好存储做额外局部 L3，单独 PR，不把这种行为写成现有 L2 能力。

`api.ts` 追加基金 API 方法与 TS 类型；`queryKeys.ts` 追加独立 `fund*` 键族，例如：

```text
fundProviders()
fundConfig()
fundProfile(scopeHash, generation?)
fundSyncRun(runId)
fundCatalogSearch(query, filtersHash, cursor)
fundNav(fundId, start, end, generation)
fundWatchlist()
fundWatchlistGroups()
fundEligibility(fundIdsHash, start, end, generation)
fundBacktest(runId)
```

键的哈希来自稳定排序与规范化参数，不能把不同份额、窗口、来源或私募权限上下文混成同一缓存。基金同步成功只精确失效基金画像/目录/净值/资格相关键；基金自选增删只失效基金自选键；基金任务轮询只刷新基金任务键。基金键不进入 `SSE_INVALIDATE_PREFIXES` 的股票行情热路径。私募访问策略变化时必须清除可见缓存、禁止旧查询结果回显；首期既无多用户私募支持，也就不声称已有完整的细粒度缓存隔离。

三个页面统一覆盖“加载/空/失败/不可用/无权限/同步中/旧快照”状态。数值展示标注币种、单位、估值归属日、实际披露日与数据源；不显示虚构实时涨跌。导航注册测试覆盖无扩展、路径冲突、直接刷新、未知路由、窄屏、错误边界。

## 13. 缓存、并发、权限与性能预算【设计】

| 层 | 基金域键或状态 | 写后失效/失败行为 |
| --- | --- | --- |
| 持久化快照 | `current_generation` + manifest | 只有全闸通过才原子推进；失败保留上一代 |
| 后端目录/画像 | `generation + scope + provider + access_context` | 新代发布后失效相应范围，私募始终不得进公开缓存 |
| 自选展示 | `watchlist_revision + generation + fund_ids` | 自选操作或新净值发布刷新，不能全表逐标的 N+1 |
| 回测 | 第 10.4 节完整键 | 历史修订标记旧结果、重跑得新版本；旧结果只读 |
| 前端 TanStack Query | 第 12 节 `fund*` 键 | 精确失效，不触发股票 `watchlist-*` 或 `pipeline-*` |

同步/回测属于有界重任务：目录和净值用批量读取/Polars 向量化；画像读取发布摘要；搜索走已发布目录索引/小表，不在每次按键时扫描所有净值；自选净值详情走批量接口，不逐行发 N 请求；回测限定最大基金数、日期跨度、并发 run 数，超限明确 422/429。目标性能需在实现 PR 中以固定基准数据集测量并与空模块/同量级基线对比，本文不伪造耗时数字。不得把基金同步、净值轮询或回测计算放进股票实时行情线程。

现有单密码认证只控制整个应用入口，不证明私募持有人资格，也不提供用户级数据隔离。首期默认禁用私募导入、公开搜索、API 展示和回测；只有在已授权单用户自托管部署中，可通过单独受控开关开放私募数据浏览/自选，且须验证数据许可、目录权限、日志/备份/导出路径及停用后的缓存清除。多用户私募不是本设计 L2 首期目标，需独立安全评审。静态页面可被访问不代表私募 API 可放行，服务端每个读取/写入入口均须执行相同授权检查。

## 14. 兼容、升级与回滚【设计】

- 扩展关闭、插件失败或基金目录为空，股票/ETF/指数的同步、现有自选、策略、回测、API 和历史配置结果不变；不自动迁移任何股票文件。
- 新基金配置只写 `data/funds/config.json`，带 `config_version`。旧字段缺失时给安全默认；未知必需字段或高版本配置拒写并提示备份/升级；同版本只做向后兼容字段新增。
- 基金 Parquet schema 版本与快照 manifest 版本独立。版本升级以新代发布、旧代只读保留；必须迁移时先备份且迁移幂等，不直接覆写唯一数据副本。回滚代码只停用扩展/切回旧二开 commit，保留 `data/funds` 用户数据；不指示用户手工删目录。
- 扩展注册契约的 `apiVersion` 只按现有系统规则；基金内部 API/结果的 `schema_version` 变化不得混淆扩展注册版本。上游升级时重点复核 `api.ts`、`queryKeys.ts` 两个热点及插件 loader、扩展注册接口，运行契约回归。
- 修改源选择、净值修订或条款版本不自动改写历史回测；旧结果显示版本/来源/时间戳，用户显式重跑才产生新结果。

## 15. 风险评估：升级级别与业务严重度分开

| 风险 | 接入级别 | 严重度 | 控制与合入门槛 |
| --- | --- | --- | --- |
| 基金页/路由与导航冲突 | L2 | P2 | 注册冲突/无注册/直接刷新/错误边界测试；冲突只禁用基金扩展 |
| Provider 清单与 `config.datasets` 不一致 | L1/L2 | P1 | 契约测试同时断言清单、实际能力、返回字段；不可用即禁用源 |
| 基金 API/CLI 复用核心 `JobStore` 导致跨进程误判 | L2 | P1 | 专属 run store、租约与双进程竞争/崩溃恢复测试 |
| 净值日、披露时间或条款有效期混淆，产生未来函数 | L2 | **P1** | 点时闸、篡改未来净值不影响既有信号的黄金测试；缺数据拒绝 verified |
| 封闭期错误赎回、费率/分红重复计费或到账前复投 | L2 | **P1** | 逐笔 Decimal 账本与类别/合同版本黄金样本 |
| 私募信息泄漏或无授权获取 | L2（首期禁用）/后续可能 L3 | **P0/P1** | 默认禁用、服务端授权、缓存/日志/导出负例；多用户能力另立项 |
| 同步部分发布、历史修订污染旧结果 | L2 | P1 | 快照原子切换、受影响代次失效和故障注入测试 |
| 前端统一请求/键接线 | **局部 L3** | P2 | 只追加基金契约，运行现有前端构建及相关查询回归 |
| 强制菜单紧邻自选或进入菜单设置 | 可选 L3 | P3 | 首期不承诺；真实需求出现时单独最小改动 |
| 应用内常驻调度或基金纳入全局能力矩阵 | 可选 L3 | P2 | 首期外部 CLI/域内选源；如立项需生命周期/矩阵回归 |

这里 L1/L2/L3 表示**上游合并冲突与二开接入级别**，P0/P1/P2/P3 表示**业务错误的阻断严重度**，不能说“L2 就低风险”。若真实数据源不提供条款、事件覆盖与可信披露时间，回测不能用估计值“先上线”；可先交付目录、净值与自选。

## 16. 分阶段实施路线（每 PR 可独立审查）

| PR | 交付与依赖 | 主要文件/接入级别 | 完成标准与回滚 |
| --- | --- | --- | --- |
| 1 契约/仓库（已实现） | 身份与多轴分类、四数据集、coverage、manifest；合成固定样本 | 新 `app/funds/contracts.py`、`repository.py`；L2 | 已覆盖 schema、版本、原子发布/失败旧代测试；回滚代码不删 `data/funds` |
| 2 Provider/同步 | 至少一个**有授权且可测试**的 Python Provider、独立 pipeline/run store/CLI；依赖 PR1 | 新插件与 `app/funds/{provider,pipeline,run_store,cli}.py`；L1/L2 | 能力、空/错、双进程竞争、断点、取消、恢复、与旧管道隔离测试 |
| 3 数据页面/API | 基金扩展注册、范围配置、画像、任务页；依赖 PR2 | `custom/funds.py`、`funds/api.py`、前端扩展及 `api.ts/queryKeys.ts`；L2 + 局部 L3 | 旧路由不变、前后端契约/页面五态/构建通过 |
| 4 自选基金 | 搜索、份额确认、分组、备注、置顶、批量文本/CSV 导入；依赖 PR1/3 | 新 `funds/watchlist.py`、基金页面；L2 | 不写股票自选、导入歧义、并发和旧数据空态测试 |
| 5 回测 MVP | 资格闸、公募普通开放式两策略、逐笔账本、结果/页面；依赖 PR1/2/3 | 新 `funds/backtest.py` 与页面；L2 | 未来函数、费用/确认/到账、分红、缺条款、修订和结果代次黄金测试 |
| 6 后续类型 | 定开、场外封闭、货币/QDII 等按真实数据和合同样本逐类型启用 | 基金域规则/测试；通常 L2 | 每类独立数据与规则验收；私募需额外授权/安全评审，不随该 PR 自动开放 |

每个 PR 的描述按贡献规范列问题、方案、兼容、性能、验证、界面证据和回滚。PR2 没有合法生产源时仍可用合成固定样本完成研发验证，但不得称为真实数据能力上线；PR5 没有条款/披露时间则只交付资格缺口，不能用“模拟”替代验收。实施各阶段后更新本文的【设计/已实现】标记以及 `docs/plugin-development.md` 或 `docs/custom-data-source.md` 中实际新增的源契约；不提前改写用户操作说明为“已上线”。

## 17. 测试矩阵、固定样本与发布门槛【设计】

| 模块 | 正常路径 | 边界/失败路径 |
| --- | --- | --- |
| 分类/身份 | 公募开放、定开、封闭、私募开放/封闭组合；A/C 与更名历史 | `unknown`、同码不同份额、场内专属份额误入、区间重叠 |
| Provider | 清单与实例能力一致、分页、单位/时区转换 | 缺 dataset、空成功、网络失败、字段缺失、冲突源、不授权私募 |
| 仓库 | 同代次一致读取、修订保留、coverage/画像汇总 | 暂存中断、校验失败、发布中崩溃、旧 schema/高版本拒写 |
| 同步/任务 | 手动/CLI 同入口，断点续传，租约单飞 | 两进程竞争、进程崩溃、僵尸超时、取消后仍写入、核心管道并发 |
| 自选基金 | 搜索/添加/移除/备注/置顶/多组/CSV 预览 | 重复/歧义、越权、无净值、分组并发版本冲突、删除组保留成员 |
| 回测点时 | 旧净值已披露可用于信号，申请后披露的净值仅用于确认 | 把未来 `published_at`/条款/公告提前使用，未知披露时间 |
| 回测成交 | 金额申购/份额赎回、费用阶梯、确认/到账、现金分红、折算 | 定开窗外、封闭期赎回、暂停、费用/分红缺口、资金/份额为负 |
| 回测版本 | 同窗口同代次复现，历史修订后新 run 结果变更且旧 run 不变 | 缓存键少条款/币种/事件/模式字段，旧结果静默改写 |
| API/权限 | 成功/空/错误码映射，认证后访问 | 无授权私募、未知 ID 枚举、过大窗口、错误泄露密钥/路径 |
| 前端 | 三路由、搜索/分组、进度、结果五态 | 无注册、路径冲突、页面刷新、窄屏、私募缓存撤销、基金键误入股票 SSE |
| 回归 | 基金扩展开/关两态比较原系统结果 | 股票 `daily_pipeline`、自选、回测、配置、API、缓存任一改变即阻断 |

固定样本至少覆盖：公募 A/C 两份额、跨节假日开放、截止点前后、净值 T 日估值/T+1 日披露、一次分红与一次份额折算、费率阶梯变更、暂停申赎、定开窗口、封闭期/到期、历史净值修订、私募未授权与无合同、QDII 币种不支持。每份样本附数据来源和可用时间，数值断言逐笔核对现金/份额/费用/期末资产，不只断言 HTTP 200。

文档阶段的验证只需：现状代码引用核对、文档内部契约一致性、链接与 Markdown/空白检查、工作区状态检查。**实施阶段**最低执行定向 `pytest`、Ruff、`pnpm build`、前后端联调、扩展无注册/冲突测试，以及 Windows/macOS/Linux/Docker 的文件锁与调度失败路径检查；未运行项必须如实记录，不能预填“通过”。

## 18. 待确认事项与明确阻断条件

1. **首个合法数据源**：需明确可获取的公募目录、历史净值、首次披露时间、事件与合同条款的来源、许可、限速及历史覆盖。没有可信 `published_at` 和合同样本，PR5 的真实回测不可发布。
2. **私募边界**：用户是否拥有具体产品数据/合同的使用权；若期望多用户各自私募基金，则现有单密码模型不满足，必须另立权限设计（可能 L3），不能以本方案直接上线。
3. **封闭式/定开实物样本**：至少一只场外份额产品的历史窗口、公告与合同版本供逐笔验证；无样本时只保留分类/画像与不可回测原因。
4. **自动同步部署**：目标环境采用哪种外部调度器；首期 CLI 不自动安装系统任务，也不谎称“定时已启用”。
5. **菜单位置**：首期扩展导航追加尾部；若“自选基金”必须紧跟“自选”且可在菜单设置排序，按第 15 节另评估局部 L3。

实施阻断线：无授权数据源仍抓取；用今天的净值/条款回填过去决策；缺事件/费率仍返回 `verified` 成功；基金数据写入股票仓库；私募记录可从公开搜索/日志/缓存枚举；同步失败导致旧快照不可读；新增代码让基金扩展关闭时原系统行为改变。

## 19. 本次设计核对记录

- 仓库基线：`630b21925ae60838cdf7fcae01a48d2fecc4467a`；PR-1 在 `feat/off-exchange-fund` 上实施，原先未跟踪的本设计文档已保留并同步修订，未覆盖其他工作区文件。
- 已核对的关键现状：`extensions/{bootstrap,registry,types}.ts`、`Layout.tsx`、`pages/settings/MenuSettings.tsx`、`lib/{api,queryKeys}.ts`；`backend/app/extensions/{loader,registry,contracts}.py`、`data_providers/custom/{loader,config}.py`、`services/pipeline_jobs.py`、`api/pipeline.py`、`services/watchlist.py`、`api/watchlist.py`、`api/backtest.py`、`services/auth.py`、`main.py`。
- 关键修订：纠正“只填插件清单即能被 `provider_has_dataset` 检出”和“核心 `JobStore` 可直接供基金 API/CLI 共用”两个不成立的假设；两者均已转为显式实施/测试契约。
- PR-1 实际代码：`backend/app/funds/{contracts,repository}.py` 与 `backend/tests/test_fund_repository.py`。五个模型覆盖目录/净值/事件/条款/核验覆盖；`FundTerm.rules` 在 PR-1 仅为带出处的原样条款载荷，**不是**已验证、可执行的申赎规则。仓库拒绝私募写入公开快照，保留净值修订和旧代次，读取时核对文件哈希及 schema。未接入主应用、Provider、CLI、页面、自选或回测。
- PR-1 验证：已建立项目 `backend/.venv`，并按 `backend/uv.lock` 使用 `uv sync --locked --extra dev` 同步；`uv lock --check`、`uv run pytest tests/test_fund_repository.py tests/test_atomic_parquet_writes.py -q`（**22 passed**）、`ruff check app/funds tests/test_fund_repository.py`、`ruff format --check app/funds tests/test_fund_repository.py` 与 `git diff --check` 均通过。未运行全量后端测试或前端构建（PR-1 未改前端）。
- 本设计不是法规、产品合同或数据许可审查。法规引用见第 0 节；任何具体申赎、净值披露、费用和私募可见性均要以实际产品条款与获授权数据复核。
