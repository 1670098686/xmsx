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
AI_TIMEOUT_CHAT = 180              # 模型解析响应超时（秒）
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
# 内联 PDF 需要随请求上传并由服务端解析，给予比普通对话更长的超时（秒）
AI_TIMEOUT_PDF_CHAT = 300

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
