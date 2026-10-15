# 文献阅读整理Agent

面向大学生的轻量化桌面端文献阅读、整理与智能解析工具。基于 Python + PyQt5 开发，全程本地化运行，支持 PDF / DOCX / DOC / TXT 多格式文献导入，由 AI 主导的文献解析 Agent 自动提炼研究背景、核心观点、研究方法、创新点、研究结论、参考文献六大维度的完整解读报告，配合本地权威关键词提取、三重反幻觉校验、原件直传通道自动探测等能力，帮助学生快速搭建个人文献资料库。

## 功能亮点

- **AI 主导的智能文献解析** — 将文献原件直接交给 AI 模型通读，AI 在通读过程中自主决定调用表格提取、页面 OCR、章节识别等中间件工具完成取证，全程仅需 1 次 AI 通读调用，无需本地预处理
- **三级降级兜底** — 原件直传 → 全文文本通道 → 纯本地规则解析，任何链路下都能保证文献被解析完成；原件→全文文本降级须获用户授权，控制权在用户手中
- **三重反幻觉校验** — 完整性（每个启用维度均有内容）、一致性（AI 解读与本地章节词集交叉比对）、原文溯源（双向覆盖率校验 AI 引用锚点的真实性与相关性），AI 通读成功且关键维度齐全时自动走信任旁路仅校验完整性以提升效率
- **可视化 PDF 原文批注** — 矩形选框高亮、文字批注、下划线/删除线等多种标注类型，批注精准定位到页码与坐标，保存前保持选框可见，支持 Esc 或翻页取消
- **本地原生安全** — AI 密钥 Fernet 对称加密存储，文献原件、笔记、解析报告、备份数据全程留在本机，零网络上传
- **全格式支持** — PDF（可批注原貌渲染）、DOCX、DOC（本地 Word/WPS 自动转 DOCX）、TXT 四种主流学术文献格式
- **结构化文献资料库** — 多级分类目录 + 个性化标签，多维度模糊检索（标题、作者、标签、分类、时间区间、文件格式、解析状态组合筛选）
- **全量/增量备份与恢复** — 全量打包整个数据库与文献原件，增量仅打包变更文件并维护累计清单；恢复后自动对账补登记
- **深色/浅色主题** — 一键切换，所有界面组件同步跟随时应用
- **零额外 PDF 依赖安装** — PyMuPDF 随项目分发在 `.vendor/` 目录，用户无需单独安装

## 快速开始

### 环境要求

- Python **3.11**（requirements.txt 版本在此环境下验证通过）
- Windows 10 / 11
- （可选）Tesseract OCR — 用于扫描件/图表页的本地 OCR 识别；未安装时自动降级为 AI 视觉解析，或在 AI 不可用时提示用户

### 安装依赖

```bash
cd literature_agent
pip install -r requirements.txt
```

requirements.txt 固定了所有依赖版本，核心运行依赖包括：

| 依赖 | 用途 |
|---|---|
| PyQt5 | GUI 界面 |
| pdfplumber / PyPDF2 | 文献文本提取 |
| PyMuPDF | PDF 原貌渲染、版式抽样体检 |
| python-docx | DOCX 表格与文本提取 |
| jieba | 本地权威关键词提取、三重校验一致性比对 |
| langgraph / langchain-core | ReAct Agent 状态图编排 |
| cryptography | AI 密钥对称加密 |
| reportlab | 解析报告 PDF 导出 |

> **关于 PyMuPDF**：项目自带 `.vendor/` 目录存放 PyMuPDF 二进制文件，main.py 启动时自动加入 `sys.path`。用户不需要全局安装 PyMuPDF，也不影响已全局安装的版本。

### 启动程序

```bash
python main.py
```

启动后将看到主窗口。首次启动会自动创建本地数据库（9 张表 + 18 个索引）并写入默认种子数据（通用/文科/理科/工科四套解析规则、学术常用分类标签、默认存储路径）。

> **调试模式**：`python main.py -v`（或设置环境变量 `LIT_AGENT_VERBOSE=1`）开启控制台详细日志，便于排查解析问题。

### 配置 AI 模型

程序启动后默认使用纯本地规则解析。启用 AI 智能解析需在「管理中心 → AI 模型」中添加模型配置：

1. 填入模型名称、接口地址、API 密钥
2. 选择原件直传通道偏好（推荐「自动识别」）：

   | 通道模式 | 说明 |
   |---|---|
   | 自动识别（推荐） | 使用内置微型 PDF 实测探测模型能力，支持则自动启用原件直传；探测结果须用户二次确认后才保存 |
   | 文件托管（file_id） | 先 POST `/files` 上传原件取 ID 再引用，支持所有格式（PDF / DOCX / TXT） |
   | PDF Base64 内联（file_data） | 本地 PDF 直接 data URI 发送，无需托管/公网 URL，仅支持 PDF |
   | 仅全文文本 | 本地提取全文后发送，最稳妥，适合任何兼容 OpenAI 协议的纯文本模型 |

3. 点击「校验配置」验证模型是否可用。校验过程不会上传任何用户文献，仅用内置测试内容。

**关于原件→全文文本降级**：如果您选择了原件直传通道但模型网关拒绝或 PDF 文件超限，Agent 会弹窗询问您是否同意改用全文文本通道重试——这个决策完全在您手中，不会静默执行。

### 运行测试

```bash
cd literature_agent
python -m pytest tests/ -v
```

当前测试基线约 **355 项通过**（部分 AI 协议 mock 相关测试因接口变更出现断言漂移，为测试基础设施问题而非功能缺陷）。

## 项目架构

### 五层架构

从界面到数据，自上而下分层，每层只调用相邻下层：

- **界面展示层**（`ui/`）— PyQt5 双栏主窗口，左侧导航 220px、顶部导航 40px，右侧内容区自适应。页面堆栈切换，浅色/深色主题一键切换，QThread 子线程执行耗时操作，页面内进度条推送中文状态
- **业务逻辑层**（`business/`）— 八大业务模块：文献导入/解析/检索、笔记批注、导出备份、标签管理、解析规则、存储路径、AI 模型配置、系统参数。所有对外接口统一返回 `(code, data, msg)` 三元组。Agent 异常自动回退本地规则解析
- **Agent 编排层**（`agent/`）— LangGraph 状态图拓扑固定为 Plan→Act→Observe→Reflect⇄Verify→(Refine)⇄Verify→Finish。Director 编排器统一调度三级能力：原件直传（含通读过程中 AI 自主调用中间件工具的 function calling 内部循环）、全文文本通道、纯本地规则。三重反幻觉校验（完整性 / 一致性 / 溯源），原件通读成功且关键维度齐全时自动走信任旁路仅查完整性
- **工具解析层**（`tool_layer/`）— 文件文本提取、版式抽样体检、PDF/DOCX 表格结构化、OCR 双轨通道、AI 模型 HTTP 通信、文本分词、导出生成、路径安全校验。工具为纯函数，不访问数据库、不导入 Agent 层
- **数据持久层**（`data_layer/`）— SQLite 单例连接，9 张表 + 18 个索引 + 参数化查询。DAO 层封装 CRUD。Schema 版本号 4，支持 v1→v4 幂等迁移

**分层约束**：UI 层不访问数据库、不调用工具解析层/Agent 层；Agent 层不访问数据库、不导入 UI/业务层；工具解析层不访问数据库、不导入 Agent 层；所有对外接口统一返回 `(code, data, msg)` 三元组。

### 目录结构

```
literature_agent/
├── main.py              # 程序入口（启动序列：DB → 配置 → 主题 → 主窗口）
├── requirements.txt     # 固定版本依赖清单
│
├── config/              # 全局配置
│   ├── constants.py         # 文件后缀 / 返回码 / 进度锚点 / AI 常量
│   ├── global_config.py     # 主题配色 / 字体 / 窗口尺寸
│   └── settings.py          # 系统配置读写（包装 SQLite）
│
├── agent/               # ReAct 文献解析 Agent
│   ├── graph.py             # LangGraph StateGraph 拓扑装配
│   ├── state.py             # AgentState TypedDict + 常量
│   ├── director.py          # 首轮播种 / 条件尾部 / 单步兜底 / 信任旁路判定
│   ├── llm.py               # LLM 适配层 + 工具调用协议校验
│   ├── prompts.py           # 外置提示词模板加载
│   ├── tools.py             # 工具注册表 + 原件通读内部 FC 多轮循环
│   ├── verifiers.py         # 三重反幻觉校验
│   ├── profiler.py          # 文献画像（短篇/常规/综述/实验/标准）
│   └── nodes/               # 七个 LangGraph 节点
│
├── business/            # 八大业务模块
│   ├── literature_import.py    # 文献导入（SHA256 哈希去重）
│   ├── literature_parse.py     # Agent 调起入口 + 降级授权弹窗编排
│   ├── literature_search.py    # 多维度检索 + 时间区间边界计算
│   ├── note_manage.py          # 笔记批注（1 秒防抖自动保存）
│   ├── export_backup.py        # 全量/增量备份恢复
│   ├── tag_category.py         # 标签分类 CRUD + 删除级联清理
│   ├── parse_rule_service.py   # 解析规则（四套预设学科权重）
│   ├── system_service.py       # AI 模型配置（Fernet 加密）
│   └── service_common.py       # 操作日志 + 路径获取 + 异常转码
│
├── tool_layer/          # 工具解析层（无业务逻辑）
│   ├── file_parser.py         # PDF/TXT/DOCX 文本提取 + 双栏重建
│   ├── document_vision.py     # 版式抽样体检（PyMuPDF）
│   ├── text_analysis.py       # jieba 分词 + 章节识别
│   ├── ai_client.py           # OpenAI 兼容协议 HTTP 客户端
│   ├── file_helper.py         # 路径安全校验 + 文件操作
│   └── export_generator.py    # Word/PDF/TXT 导出
│
├── data_layer/          # SQLite 数据库层（仅 CRUD）
│   ├── db_connect.py          # 单例 DatabaseManager
│   ├── db_init.py             # 9 表 + 种子数据 + Schema 迁移
│   └── dao/                   # 九张表各自的 DAO
│
├── ui/                  # PyQt5 界面层（仅渲染 + 事件捕获）
│   ├── main_window.py         # 双栏主窗口
│   ├── theme_manager.py       # 浅色/深色主题
│   ├── pages/                 # 五大功能页面 + 六大管理子页面
│   └── widgets/               # 通用组件（进度条/Toast/环形图/PDF 批注...）
│
├── utils/               # 通用工具
│   ├── exceptions.py         # 自定义业务异常
│   ├── logger.py              # 控制台详细日志
│   └── common_helper.py       # 时间格式化等
│
├── resources/           # 静态资源
│   ├── prompts/               # 外置提示词 + 精度档位 JSON
│   ├── qss/                   # 浅色/深色主题样式表
│   └── icons/                 # 线性图标
│
├── .vendor/             # 随项目分发的 PyMuPDF（免全局安装）
│
├── output/              # 运行时备份缓存
│
└── tests/               # pytest 单元测试（内存 SQLite fixture）
```

### 9 张数据表

| 表名 | 用途 |
|---|---|
| `literature_info` | 文献元数据（标题/作者/格式/hash/入库时间） |
| `literature_report` | 解析报告六维度文本 + 关键词（本地 jieba 权威结果） |
| `literature_note` | 笔记批注（段落 / 全局共用，PDF 锚点精准定位页码坐标） |
| `category_tag` | 分类目录 + 标签（共用一张表，tag_type 区分） |
| `literature_tag` | 文献–标签多对多中间表 |
| `parse_rule` | 解析规则（四套预设学科权重 + 维度开关 + 精度档位） |
| `system_config` | 系统配置键值对（含 Fernet 加密的 AI 密钥） |
| `operation_log` | 操作日志（导入/解析/删除/备份/配置修改） |
| `backup_record` | 备份记录（全量/增量 + 累计清单） |

Schema 版本号 4，支持 v1→v4 幂等迁移。

## AI 主导解析流程

文献解析 Agent 是系统唯一的 AI 解析入口。一次完整解析的标准路径：

```
Plan ──→ 版式抽样体检（毫秒级，感知双栏/扫描件/图表页）
 │
 ▼
Reflect ──→ Director 首轮播种
 │              ├── 原件通道可用 → 排原件通读（第一步）
 │              ├── 扫描件无通道 → 排逐页 OCR
 │              └── 其余 → 排文本提取
 │
 ▼
Act ──→ 执行动作（进度双泳道单调推进）
 │
 ▼
Observe ──→ 结果回灌状态
 │
 ▼
Reflect ⇄ Act（AI 通读过程中的 function calling 内部循环）
 │
 ▼
Verify ──→ 三重反幻觉校验
 │              ├── 原件信任旁路成功 → 仅查完整性
 │              └── 否则 → 完整性 + 一致性 + 溯源
 │
 ▼
Refine ⇄ Act（校验未通过时补救，限次防死循环）
 │
 ▼
Finish ──→ 归一化六维度文本 + 组装 final_report
 │
 ▼
业务层落库 + 展示结果
```

Function calling 内部循环安全边界（常量统一在系统常量模块）：

| 常量 | 值 | 含义 |
|---|---|---|
| 最大轮数 | 5 | 每轮可含多个并行 tool_calls |
| 最大工具调用总数 | 8 | 跨轮累计，与轮数双重约束 |
| 单轮结果截断 | 6000 字符 | 尾部保留 1000 字符 |
| 签名去重 | — | 同一（工具名 + 参数 JSON）签名不重复执行 |
| 页码越界拒绝 | — | 不在页面范围内直接返回错误消息让 AI 改道 |

## 常见问题

### 启动后提示"尚未配置 AI 模型"

正常现象。程序默认使用纯本地规则解析，不强制依赖 AI。要启用 AI 智能解析，进入「管理中心 → AI 模型」添加配置即可。未配置 AI 模型时本地 jieba + 本地规则链仍可产出解析结果。

### 原件直传失败后弹窗询问降级

Agent 检测到当前 AI 网关拒绝"原件消息 + tools 同框"或 PDF 文件超限时，会弹窗询问您是否同意改用全文文本通道重试。这是系统设计的预期行为——网络/鉴权类错误不会静默降级，只有协议不兼容才进入可降级判定。

### 想要更详细的控制台日志

```bash
python main.py -v
```

或设置环境变量：

```bash
set LIT_AGENT_VERBOSE=1
python main.py
```

开启后控制台由简洁 INFO 格式升为 DEBUG 全量格式（时间 | 级别 | 文件:行号），已埋点：启动序列、plan 任务开始、reflect 动作链、verify 三校验明细、act 工具耗时/错误、ai_client 请求 URL/体大小/超时、file_parser 提取类型/字数/耗时。

### 扫描件 / 图表页的 OCR 效果不理想

项目默认 AI 视觉识别（勾选支持视觉能力的多模态模型）。如果使用纯文本模型，扫描件无文本层时自动降级为本地 Tesseract OCR（需系统安装 Tesseract + chi_sim 中文语言包）。requirements.txt 中 pytesseract 是可选依赖。

### Windows 切换应用后第一次点击没反应

这是刻意设计的行为——Windows 下从其他软件切回时，第一次点击仅激活窗口、不触发被点中的控件（避免误触侧边栏导致管理子页被切换）。再次点击才会触发控件操作。

### 删除文献时提示"文件可能正被其他程序占用"

文献原件被其他程序（如 PDF 阅读器）打开时，Windows 文件句柄禁止删除。关闭占用程序后重试即可。

## 技术栈一览

| 类别 | 技术 |
|---|---|
| 语言 | Python 3.11 |
| GUI | PyQt5 |
| 智能体 | LangGraph + ReAct（Director 编排器） |
| PDF 处理 | pdfplumber / PyPDF2 / PyMuPDF（随项目分发） |
| DOCX | python-docx |
| 分词 | jieba |
| 加密 | cryptography.Fernet |
| 数据库 | SQLite（单例连接 + 参数化查询） |
| 测试 | pytest（内存 SQLite fixture） |
