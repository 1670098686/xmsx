"""系统常量定义：文件后缀、状态枚举、操作类型、统一返回码。

禁止在业务代码中硬编码魔鬼数字，所有状态统一引用本模块常量。
"""

# ========== 支持的文件格式 ==========
SUPPORTED_SUFFIX = {".pdf", ".txt", ".docx", ".doc"}

# 文献格式枚举（写入 literature_info.literature_type）
LIT_TYPE_PDF = "PDF"
LIT_TYPE_TXT = "TXT"
LIT_TYPE_DOCX = "DOCX"
LIT_TYPE_DOC = "DOC"

SUFFIX_TO_TYPE = {
    ".pdf": LIT_TYPE_PDF,
    ".txt": LIT_TYPE_TXT,
    ".docx": LIT_TYPE_DOCX,
    ".doc": LIT_TYPE_DOC,
}

# 检索页格式筛选下拉（固定顺序）与中文显示名
LIT_TYPE_OPTIONS = (LIT_TYPE_PDF, LIT_TYPE_TXT, LIT_TYPE_DOCX, LIT_TYPE_DOC)
LIT_TYPE_LABELS = {
    LIT_TYPE_PDF: "PDF",
    LIT_TYPE_TXT: "TXT",
    LIT_TYPE_DOCX: "DOCX（新版 Word）",
    LIT_TYPE_DOC: "DOC（旧版 Word）",
}

# ========== 检索筛选：解析状态 / 入库时间段 ==========
# 解析状态筛选项（literature_info.is_parsed：已解析=2，其余视为未解析）
PARSE_FILTER_ALL = "all"
PARSE_FILTER_DONE = "done"
PARSE_FILTER_TODO = "todo"
# 入库时间段筛选项
TIME_RANGE_ALL = "all"
TIME_RANGE_OLDER_THAN_YEAR = "older_than_year"
TIME_RANGE_THIS_YEAR = "this_year"
TIME_RANGE_THIS_MONTH = "this_month"
TIME_RANGE_THIS_WEEK = "this_week"

# ========== 旧版 .doc 转换（本地 Word/WPS/LibreOffice，无网络上传）==========
# 依次尝试的 Office COM ProgID（Microsoft Word → WPS 文字）
DOC_COM_PROGIDS = ("Word.Application", "KWps.Application")
# LibreOffice 可执行文件名（PATH 中查找）
DOC_SOFFICE_NAMES = ("soffice.exe", "soffice.com", "soffice")
# LibreOffice 常见安装路径（PATH 未配置时兜底）
DOC_SOFFICE_FALLBACK_PATHS = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
)
# Word SaveAs 格式枚举：wdFormatDocumentDefault（.docx）
DOC_WORD_FORMAT_DOCX = 16
# Word SaveAs/ExportAsFixedFormat 格式枚举：wdFormatPDF/wdExportFormatPDF（.pdf）
DOC_WORD_FORMAT_PDF = 17
# 可转换为 PDF 原貌展示的 Word 格式
WORD_RENDER_SUFFIX = (".doc", ".docx")
# 转换后 PDF 渲染缓存目录名（位于系统临时目录下）
PDF_RENDER_CACHE_DIR = "la_pdf_render"
# 原文渲染遮罩延迟展示毫秒数（PDF/TXT 快速返回时遮罩不出现）
RENDER_MASK_DELAY_MS = 200
# 单次转换超时秒数（Office 冷启动可能较慢）
DOC_CONVERT_TIMEOUT_SEC = 120

# OLE2 复合文档魔数（旧版 .doc/.xls 等）与 ZIP 魔数（OOXML .docx 实质为 zip）
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP_MAGIC = b"PK\x03\x04"

# ========== 解析状态（literature_info.is_parsed）==========
PARSE_NOT_STARTED = 0   # 未解析
PARSE_IN_PROGRESS = 1   # 解析中
PARSE_DONE = 2          # 已解析
PARSE_FAILED = 3        # 解析失败

# 关键词作为解析报告的附加信息固定提取条数（不在规则模板中提供数量配置）
PARSE_KEYWORD_TOP_N = 20

# ========== 标签类型（category_tag.tag_type）==========
TAG_TYPE_CATEGORY = 1   # 分类目录
TAG_TYPE_LABEL = 2      # 标签

# ========== 操作日志状态（operation_log.operate_status）==========
OP_STATUS_SUCCESS = 1
OP_STATUS_FAILED = 0

# ========== 备份类型（backup_record.backup_type）==========
BACKUP_TYPE_FULL = "full"
BACKUP_TYPE_INCREMENTAL = "incremental"

# ========== 操作类型（operation_log.operate_type）==========
OP_IMPORT = "导入"
OP_PARSE = "解析"
OP_EXPORT = "导出"
OP_DELETE = "删除"
OP_CONFIG = "修改配置"
OP_BACKUP = "备份"
OP_RESTORE = "恢复"
OP_NOTE = "笔记"

# ========== 主题枚举 ==========
THEME_LIGHT_NAME = "light"
THEME_DARK_NAME = "dark"

# ========== 统一返回码（business 层三元组 code）==========
CODE_SUCCESS = 0              # 成功
CODE_ERROR = 1                # 通用错误
CODE_FILE_NOT_FOUND = 2       # 文件不存在
CODE_FILE_INVALID = 3         # 文件格式不支持/文件损坏
CODE_PARSE_FAILED = 4         # 解析失败
CODE_DB_ERROR = 5             # 数据库错误
CODE_STORAGE_NOT_ENOUGH = 6   # 存储空间不足
CODE_DUPLICATE = 7            # 文件/数据重复
CODE_PERMISSION_DENIED = 8    # 权限不足
CODE_AI_SERVICE = 9           # AI 模型服务调用失败

# ========== 笔记类型（literature_note.note_type）==========
NOTE_TYPE_PARAGRAPH = "paragraph"   # 段落批注（纯文本文献段落 / PDF 区域批注共用）
NOTE_TYPE_GLOBAL = "global"         # 全局笔记

# ========== PDF 批注锚点（复用 literature_note.paragraph_pos，VARCHAR(50)）==========
# 区域框批注：形如 "pdf:2:90,170,506,590"：pdf:页序(0基):矩形左上右下边（PDF 点）
NOTE_PDF_ANCHOR_PREFIX = "pdf:"
# WPS 式文字标记：词区间锚点，形如 "pdfh:2:12-38"（页序0基:起词序号-止词序号）
NOTE_PDF_HIGHLIGHT_PREFIX = "pdfh:"   # 荧光高亮
NOTE_PDF_UNDERLINE_PREFIX = "pdfu:"   # 下划线
NOTE_PDF_STRIKEOUT_PREFIX = "pdfs:"   # 删除线
# TXT 字符区间锚点：形如 "txh:120-186"（渲染文本字符起-止，半开区间）
NOTE_TEXT_COMMENT_PREFIX = "txc:"     # 选中文字批注（浅底）
NOTE_TEXT_HIGHLIGHT_PREFIX = "txh:"
NOTE_TEXT_UNDERLINE_PREFIX = "txu:"
NOTE_TEXT_STRIKEOUT_PREFIX = "txs:"
PDF_ANNOT_MIN_DRAG_PX = 6    # 小于该拖拽距离视为点击而非框选（屏幕像素）
PDF_ANNOT_BADGE_MARGIN = 3   # 批注序号角标与矩形左上的间距（像素）

# ========== WPS 式划线标记类型 ==========
MARK_KIND_BOX = "box"                 # PDF 区域框批注 / TXT 段落批注
MARK_KIND_COMMENT = "comment"         # 选中文字后书写的批注
MARK_KIND_HIGHLIGHT = "highlight"     # 荧光笔高亮
MARK_KIND_UNDERLINE = "underline"     # 下划线
MARK_KIND_STRIKEOUT = "strikeout"     # 删除线
# 标记类型 → PDF 锚点前缀
PDF_MARK_PREFIXES = {
    MARK_KIND_HIGHLIGHT: NOTE_PDF_HIGHLIGHT_PREFIX,
    MARK_KIND_UNDERLINE: NOTE_PDF_UNDERLINE_PREFIX,
    MARK_KIND_STRIKEOUT: NOTE_PDF_STRIKEOUT_PREFIX,
}
# 标记类型 → TXT 锚点前缀
TEXT_MARK_PREFIXES = {
    MARK_KIND_COMMENT: NOTE_TEXT_COMMENT_PREFIX,
    MARK_KIND_HIGHLIGHT: NOTE_TEXT_HIGHLIGHT_PREFIX,
    MARK_KIND_UNDERLINE: NOTE_TEXT_UNDERLINE_PREFIX,
    MARK_KIND_STRIKEOUT: NOTE_TEXT_STRIKEOUT_PREFIX,
}
# 标记颜色：用户在批注工具条自定义（QColorDialog），持久化于系统配置表
NOTE_DEFAULT_MARK_COLOR = "#FFE066"       # 首次使用的初始色 / 无记录时兜底
NOTE_DEFAULT_LINE_COLOR = "#E0463C"       # 下划线/删除线业务层兜底色
# 标记绘制视觉参数
MARK_FILL_ALPHA = 110            # 高亮填充透明度 0~255
MARK_COMMENT_BG_ALPHA = 70       # 文字批注浅底透明度
MARK_LINE_ALPHA = 230            # 下划线/删除线颜色透明度
MARK_LINE_WIDTH_PX = 2           # 下划线/删除线线宽（像素）
MARK_SELECTED_BORDER_ALPHA = 200  # 选中标记描边透明度
PDF_MARK_WORD_HIT_PT = 6.0       # PDF 词命中容差（PDF 点），未直接命中时吸附最近词

# ========== 系统配置键名（system_config.config_key）==========
CFG_LITERATURE_PATH = "literature_path"
CFG_REPORT_PATH = "report_path"
CFG_NOTE_PATH = "note_path"
CFG_BACKUP_PATH = "backup_path"
CFG_AUTO_SAVE_TIME = "auto_save_time"
CFG_BACKUP_CYCLE = "backup_cycle"
CFG_EXPORT_DEFAULT_TYPE = "export_default_type"
CFG_UI_STYLE = "ui_style"
CFG_NOTE_MARK_COLOR = "note_mark_color"  # 批注标记自定义颜色（#RRGGBB）
CFG_PARSE_DEFAULT_RULE = "parse_default_rule"
CFG_PRECISION_DEFAULT = "precision_default"
CFG_AUTO_SAVE_DEBOUNCE = "auto_save_debounce"
CFG_AI_MODELS = "ai_models"
# 阶段4：会话状态恢复（上次页面 / 管理子页 / 检索条件）
CFG_LAST_PAGE = "last_page"
CFG_LAST_SETTING_MODULE = "last_setting_module"
CFG_SEARCH_STATE = "search_state"

# 笔记自动保存防抖间隔（毫秒）
NOTE_SAVE_DEBOUNCE_MS = 1000

# ========== 用户数据默认色（标签/分类，属于业务数据而非界面主题）==========
DEFAULT_LABEL_COLOR = "#52C41A"
DEFAULT_CATEGORY_COLOR = "#E8F4F8"
TAG_TEXT_COLOR = "#111827"  # 标签色块上的文字色（标签底色统一为浅色系）

# ========== 批量任务性能（低配设备让步，避免批量导入/解析时界面卡顿）==========
BATCH_INTERVAL_MS_NORMAL = 10
BATCH_INTERVAL_MS_LOW_END = 50
LOW_END_CPU_CORES = 2

# ========== 存储占用环形图配色（文献/报告/笔记/备份/数据库，浅深主题均可辨识）==========
STORAGE_CHART_COLORS = ("#4A90D9", "#52C41A", "#FAAD14", "#9254DE", "#8C8C8C")
STORAGE_DB_COLOR = "#8C8C8C"  # 数据库分段配色（与上图末段一致，单独暴露便于引用）
STORAGE_CHART_SIZE = 200  # 环形图直径（像素）
STORAGE_CHART_CARD_MIN_HEIGHT = 260  # 存储分布卡片最小高度，保证环形完整显示

# 存储目录用途说明（正文存数据库，目录用于导出/备份文件）
STORAGE_DIR_HINTS = {
    "literature": "存放导入的文献原件",
    "report": "解析报告正文保存在数据库，此目录存放导出的报告文件",
    "note": "笔记与批注保存在数据库，此目录用于存放导出的笔记文件",
    "backup": "存放全量/增量备份压缩包",
}

# ========== 导入记录表格（行高自适应内嵌按钮，列宽可拖拽）==========
IMPORT_TABLE_ACTION_COL_WIDTH = 200  # 操作列初始宽：容纳「预览」+「前往资料库」
TABLE_ROW_MIN_HEIGHT = 50  # 表格行最小高度（像素，含高 DPI 缩放余量）
# 各列初始宽度（Interactive 模式，用户可自行拖拽调整）
IMPORT_COL_WIDTHS = (420, 80, 170, 100, IMPORT_TABLE_ACTION_COL_WIDTH)

# ========== AI 模型服务（OpenAI 兼容接口：原件直传，不做本地文本提取）==========
AI_FILE_PURPOSE = "file-extract"   # 文件上传用途（qwen-long/兼容接口约定）
AI_TIMEOUT_UPLOAD = 120            # 原件上传超时（秒，大文件）
AI_TIMEOUT_CHAT = 800              # 模型解析响应超时（秒，全文 4.8 万字 + 8192 输出留足生成时间）
# AI 请求等待期间进度条心跳间隔（秒）：长文档生成常需 1-5 分钟，
# 周期提示"仍在思考中"避免用户误以为界面卡死
AI_WAIT_HEARTBEAT_SEC = 15
AI_TEMPERATURE = 0.2               # 结构化解析要求稳定输出
# 单次响应上限：推理类模型（如 qwen3 系列）的思维链也占用该预算，
# 解析长文献需输出多维度完整 JSON，4096 易把正文 JSON 截断导致解析失败，
# 故预留思维链+正文的充足空间
AI_MAX_TOKENS = 8192

# AI 全文文本解析（不支持文件消息的模型，如 qwen-max）：
# 全文不超过该字符数时单轮发送；超长则分块提炼后再综合，保证完整覆盖全文
AI_INLINE_FULL_CHARS = 48000
AI_INLINE_CHUNK_CHARS = 12000
AI_INLINE_CHUNK_OVERLAP = 200
AI_INLINE_MAX_CHUNKS = 20

# AI 配置校验（管理中心「校验配置」按钮）：
# 探测请求只要求模型回一句固定短文本，不涉及任何用户文献
AI_PROBE_TIMEOUT = 60              # 探测请求超时（秒）
AI_PROBE_MAX_TOKENS = 16           # 探测回复上限，足够返回一句话
AI_PROBE_REPLY_PREVIEW = 80        # 结果弹窗中回显回复的最大字符数
AI_PROBE_MAX_RETRIES = 3           # 文件探测时服务端解析等待的最大次数
AI_PROBE_RETRY_INTERVAL = 3        # 文件探测重试间隔（秒）

# 模型不支持任何"原件直传"方式时的统一友好说明（弹窗/校验结果共用）
# 说明：file_url 方式要求 PDF 为公网可访问链接，与本系统"全程本地化、
# 禁止把用户文献托管到公网"的约束冲突，故不采用，统一走本地全文文本通道
AI_FILE_ID_UNSUPPORTED_HINT = (
    "该模型不支持 PDF 原件直传（file_id 方式仅 qwen-long、Kimi/Moonshot 等"
    "长文档模型提供；部分模型虽支持 file_url，但要求 PDF 必须是公网可访问的"
    "链接，本软件全程本地化、不会把文献上传到公网，因此不采用该方式）。"
    "解析文献时将自动使用本地提取的全文文本通道，文本型 PDF/Word/TXT "
    "效果不受影响；仅扫描件、复杂表格版式等依赖服务端 OCR 的场景会有差距。"
)

# qwen3.8 系列（qwen3.8-max/flash/27b）PDF 理解通道：
# OpenAI 兼容协议用 {"type":"file","file":{"file_data": base64, "filename": xx}}
# 本地 PDF 直接 Base64 内联发送，不需要 /files 托管、不需要公网 URL。
# Base64 体积约膨胀 1/3，官方提示约 150MB 文件编码后约 200MB 会超请求体上限，
# 故本地保守限制源文件 100MB，超限改走全文文本通道
AI_PDF_INLINE_MAX_BYTES = 100 * 1024 * 1024
# 内联 PDF 需要随请求上传并由服务端解析，给予比普通对话更长的超时（秒）。
# 3.7 万字、Base64 后 5.7MB 的 PDF 实测 300 秒不足以等回六维度结果，放宽至 800 秒
AI_TIMEOUT_PDF_CHAT = 800
# 单页图片视觉 OCR（扫描件/图表页）请求超时（秒）
AI_TIMEOUT_VISION_CHAT = 180

# 原件解析通道（每个模型可单独配置，支持任意兼容该协议的 PDF 模型）：
# auto      自动识别：按模型名关键字预判；校验配置时依次实测两种通道并记录结果
# file_id   文件托管通道：先 POST /files 上传原件取 file_id 再引用
#           （qwen-long、Kimi/Moonshot、智谱等长文档模型）
# file_data PDF Base64 内联通道：本地 PDF 直接 data URI 发送，无需托管/公网 URL
#           （qwen3.8 系列及兼容该格式的多模态模型，仅支持 PDF）
# none      仅全文文本：本地提取全文后发送（任意纯文本模型可用，最稳妥）
AI_FILE_CHANNEL_AUTO = "auto"
AI_FILE_CHANNEL_FILE_ID = "file_id"
AI_FILE_CHANNEL_FILE_DATA = "file_data"
AI_FILE_CHANNEL_NONE = "none"
AI_FILE_CHANNELS = (
    AI_FILE_CHANNEL_AUTO, AI_FILE_CHANNEL_FILE_ID,
    AI_FILE_CHANNEL_FILE_DATA, AI_FILE_CHANNEL_NONE,
)
# 通道中文说明（UI 下拉与校验结果展示共用）
AI_FILE_CHANNEL_LABELS = {
    AI_FILE_CHANNEL_AUTO: "自动识别（推荐：校验时实测原件通道）",
    AI_FILE_CHANNEL_FILE_DATA: "PDF Base64 直传（本地 PDF 内联，适合 qwen3.8 等）",
    AI_FILE_CHANNEL_FILE_ID: "文件 ID 托管直传（qwen-long / Kimi 等）",
    AI_FILE_CHANNEL_NONE: "仅全文文本（不直传原件，最稳妥）",
}
AI_FILE_CHANNEL_SHORT_LABELS = {
    AI_FILE_CHANNEL_AUTO: "自动识别",
    AI_FILE_CHANNEL_FILE_DATA: "PDF Base64 直传",
    AI_FILE_CHANNEL_FILE_ID: "文件 ID 托管直传",
    AI_FILE_CHANNEL_NONE: "仅全文文本",
}

# ========== 受管文件删除（Windows 句柄占用兜底）==========
# 预览器（PyMuPDF）句柄已由删除前钩子释放，但杀软实时扫描/索引服务等
# 外部进程仍可能瞬时占用文件，首次 os.remove 遇 WinError 32 时短暂重试
FILE_DELETE_MAX_ATTEMPTS = 3      # 总尝试次数（含首次）
FILE_DELETE_RETRY_INTERVAL = 0.2  # 每次重试前等待（秒）

# ========== ReAct 文献解析 Agent（agent/ 编排层）==========
AGENT_MAX_STEPS = 8                     # ReAct 循环最大工具调用步数
AGENT_REFINE_MAX_ATTEMPTS = 2           # 同一维度同一类校验问题最多补救次数
# AI 主导编排：Director LLM 连续返回非法/无法执行决策的次数上限，
# 达到后放弃 LLM 编排转确定性兜底，防止模型持续输出无效动作耗尽步数
AGENT_DIRECTOR_MAX_LLM_FAILURES = 2
# 一致性校验：AI 解读与本地章节的词集 Jaccard 重叠度下限，
# 同时要求 AI 文本长度不超过本地章节的该倍数（两条件同时违例才判矛盾）
AGENT_VERIFY_OVERLAP_MIN = 0.12
AGENT_VERIFY_AI_LOCAL_LEN_RATIO = 3.0
# 溯源校验（原口径）：单个引用段落词集中被 AI 解读命中的比例下限。
# 注意分母是段落词数——仅对小段严格有效；PDF 段落可能偏大（数百词），
# 该口径数学上不可达，故同时采用下方"AI 侧覆盖率"口径，任一满足即通过
AGENT_VERIFY_CITATION_COVER_MIN = 0.15
# 溯源校验（AI 侧口径）：AI 解读实词中能在其所引全部锚点段落里找到的
# 比例下限；锚点段落允许聚合 1-3 个。忠于原文的解读通常 >=0.5
AGENT_VERIFY_CITATION_AI_COVER_MIN = 0.35
# 溯源校验：AI 解读与锚点段落的共同实词数下限（防极短文本偶然命中）
AGENT_VERIFY_CITATION_COMMON_WORDS_MIN = 3
# 本地章节原文短于该字数时不参与一致性比对（章节误切时避免误判）
AGENT_VERIFY_MIN_LOCAL_CHARS = 30
# 整体 AI 解读提速：本地章节达到该字数即视为内容充实，
# 该维度不再交 AI 复述（直接用本地原文）；全部维度充实则跳过本次 AI 调用
AGENT_LOCAL_DIM_SELF_SUFFICIENT_CHARS = 50
# 喂给规划器/观测的文献片段长度
AGENT_PLAN_TEXT_HEAD = 2000
AGENT_OBSERVATION_TEXT_HEAD = 1200

# ========== 阶段2：原件视觉感知（inspect_document / ocr_page） ==========
AGENT_INSPECT_SAMPLE_PAGES = 5      # 布局探测抽样页数（只抽前 N 页，毫秒级）
AGENT_OCR_DPI = 300                 # OCR 页面渲染分辨率
AGENT_OCR_LANG = "chi_sim+eng"      # 本地 Tesseract 识别语言包
# 单页可提取文本少于该字数视为"无文本层"（扫描页）
AGENT_LAYOUT_PAGE_TEXT_MIN_CHARS = 20
# 抽样页中无文本层页面占比达到该值即判定整篇为扫描件
AGENT_LAYOUT_SCANNED_RATIO = 0.8
# 页面内嵌图片覆盖面积占比达到该值视为"图表页"（排除小 logo/分隔图）
AGENT_LAYOUT_FIGURE_AREA_RATIO = 0.25
# 双栏判定：中央带低覆盖中缝最小宽度（pt），且中缝两侧词数/字数下限
# （中文无空格，PyMuPDF 按行聚合成"词"，故同时要求两侧字数）
AGENT_LAYOUT_GUTTER_MIN_PT = 12
AGENT_LAYOUT_COLUMN_SIDE_WORDS = 2
AGENT_LAYOUT_COLUMN_SIDE_CHARS = 20
# 扫描件循环步数：N 页 OCR + 尾部（结构/关键词/AI）+ 补救余量
AGENT_SCANNED_STEP_TAIL = 6
# OCR 进度区间（早段泳道）：扫描件 OCR 是"提取阶段"（10→28）；
# 图文混合的图表页补充识别排在关键词之后（56→61）
AGENT_PCT_OCR_SCAN_START = 10
AGENT_PCT_OCR_SCAN_END = 28
AGENT_PCT_OCR_FIGURE_START = 56
AGENT_PCT_OCR_FIGURE_END = 61
# OCR 进度区间（原件泳道）：原件直读失败后的扫描件逐页 OCR 是 62→66 的
# 降级基线段（与护栏 extract_text 同段，二者不会在同一链路同时出现）；
# 降级结构化尾部的图表页补充识别排在关键词之后（72→75）；
# 原件成功链的图表页 OCR 由 Director 预规划排定，进度在 70→78 段内打标
AGENT_PCT_OCR_LATE_SCAN_START = 62
AGENT_PCT_OCR_LATE_SCAN_END = 66
AGENT_PCT_OCR_LATE_FIGURE_START = 72
AGENT_PCT_OCR_LATE_FIGURE_END = 75
# 图表页 OCR 文本并入全文时的分隔标题（也让 AI 能区分其来源）
AGENT_FIGURE_OCR_BLOCK_TITLE = "图表页识别文字"

# ========== 表格结构化提取（extract_tables，Director 按需显式调用） ==========
# 表格 Markdown 块并入全文时的分隔标题（PDF 按页分块；DOCX 用「Word表N」）
AGENT_TABLE_BLOCK_TITLE = "表格数据"
# 单次抽表返回的表格数量上限（体检只抽样页提示；全量抽表由此兜底防爆量）
AGENT_TABLE_MAX_DEFAULT = 20

# ========== Agent 文献画像与自适应策略 ==========
AGENT_SHORT_TEXT_CHARS = 1500        # 短文献阈值（短摘要/简讯）：精简动作、少提关键词
AGENT_SHORT_KEYWORD_TOP_N = 10       # 短文献关键词条数
AGENT_HEADING_DENSE_COUNT = 6        # 章节标题命中数达到该值视为章节密集
# 文献类型判定关键词（在标题/开头片段中匹配，命中即归类）
AGENT_SURVEY_KEYWORDS = ("综述", "研究进展", "文献综述", "review", "survey")
AGENT_EXPERIMENT_KEYWORDS = ("实验", "数据集", "消融", "experiment", "实证")
# 画像类型稳定标识
LIT_KIND_SHORT = "short_paper"
LIT_KIND_SURVEY = "survey"
LIT_KIND_EXPERIMENTAL = "experimental"
LIT_KIND_STANDARD = "standard"

# ========== Agent 进度条节奏（内部区间 8-90；5/95/100 由业务层负责） ==========
# 进度分两条泳道，按"是否已尝试原件直读（ai_read_original）"选择锚点，
# 任何链路下百分比都必须单调不减：
# - 早段泳道（离线 / 无原件通道，从未尝试直读）：
#   inspect 8-10 → 提取 / 扫描 OCR 10-28 → 结构 30-42 → 关键词 48-55
#   → 图表页 OCR 56-61 → 文本通道 AI 62-75 → 校验 62/77；
# - 原件泳道（直读成功或失败后的全部动作）：
#   ai_read_original 12-70（含 function calling 读中多轮工具调用，轮次在段内
#   推进）→ 本地关键词收尾 72-74（扫描件基线 OCR / 条件结构识别在同段显式打标）
#   → 信任校验 76 / 完整校验 77
#   → 仍缺维度的 Director 单步补救 80-84 → Refine 补救 85-88/封顶 89 → 收尾 90。
AGENT_PCT_START = 8                    # Plan：准备解析任务
AGENT_PCT_REFLECT = 24                 # 早段泳道：文献画像与动作规划完成
AGENT_PCT_ORIGINAL_READ_START = 12     # 原件直读开始（与 inspect 完成 10 衔接）
AGENT_PCT_ORIGINAL_READ_END = 70       # 原件直读+读中工具循环完成（主段终点）
# 原件通读成功后的条件本地基线段（扫描件补页 OCR / 缺全文时补提取 /
# AI 未给参考文献时条件触发章节识别）：读中循环已取证的内容不再重复执行
AGENT_PCT_ORIGINAL_BASELINE = 71
AGENT_PCT_KEYWORD_TAIL_START = 72      # 原件通读后的本地关键词收尾段
AGENT_PCT_KEYWORD_TAIL_END = 74
AGENT_PCT_VERIFY_TRUSTED = 76          # 原件信任旁路：只查完整性
AGENT_PCT_VERIFY_FIRST = 77            # 首次完整三重校验
AGENT_PCT_VERIFY_FIRST_NO_AI = 62      # 纯本地链路首次校验（紧跟图表 OCR 61）
# 信任校验后仍缺维度时，Director 单步补救动作段（Verify 之前）
AGENT_PCT_STAGE5_START = 80
AGENT_PCT_STAGE5_END = 84
AGENT_PCT_REFINE_BASE = 85             # Refine 补救阶段起点（随步数推进，封顶 89）
AGENT_PCT_VERIFY_RETRY = 88            # 补救后再次校验
AGENT_PCT_REFINE_ACTION_CAP = 89       # Refine 补救动作进行/完成封顶（88 与 90 之间）
AGENT_PCT_FINISH = 90                  # Finish：智能体流程结束，等待业务层落库
# 原件直读成功后，额外为 Director 单步补维度预留的步数
AGENT_DIRECTOR_STEP_RESERVE = 3

# ========== 读中 function calling 多轮循环（ai_read_original 内部） ==========
# AI 通读原件时自主发起工具调用的最大轮数（每轮可含多个并行 tool_calls），
# 达到上限后强制要求模型用现有结果收尾，防止工具调用死循环
AGENT_TOOL_MAX_ROUNDS = 5
# 单个工具执行结果回灌给模型的字符上限（超长表格/OCR 文本截断，防多轮上下文膨胀）
AGENT_TOOL_RESULT_MAX_CHARS = 6000
# 单个工具结果截断后保留的尾部长度（保证表格末尾的结论行不被裁掉）
AGENT_TOOL_RESULT_TAIL_CHARS = 1000
# 读中工具的总调用次数上限（跨轮累计，与轮数上限双重约束）
AGENT_TOOL_MAX_CALLS = 8

# 早段泳道工具锚点（离线 / 扫描件无通道 / 原件从未尝试）
AGENT_TOOL_PROGRESS_EARLY = {
    "extract_text": (10, 14),
    "detect_structure": (30, 42),
    "extract_keywords": (48, 55),
    "ai_deep_analyze": (62, 75),
}
# 原件泳道工具锚点（仅用于直读失败降级后的结构化尾部：
# extract → detect → keywords → 图表 OCR → 文本通道 AI）；
# 原件成功链的关键词/条件基线由 Reflect 显式打标 72-74，不查本表
AGENT_TOOL_PROGRESS_LATE = {
    "extract_text": (62, 65),
    "detect_structure": (66, 68),
    "extract_keywords": (69, 71),
    "extract_tables": (72, 75),
    "ai_deep_analyze": (76, 81),
}
# 全链路共享锚点（与泳道无关）；保留 AGENT_TOOL_PROGRESS 名兼容其余引用，
# 内容为共享锚点 + 早段泳道（Act/Observe 取锚点统一走 tool_progress_pair）
AGENT_TOOL_PROGRESS = {
    "inspect_document": (8, 10),
    "ai_read_original": (AGENT_PCT_ORIGINAL_READ_START,
                         AGENT_PCT_ORIGINAL_READ_END),
    **AGENT_TOOL_PROGRESS_EARLY,
}
