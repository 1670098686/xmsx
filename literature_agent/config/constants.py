"""系统常量定义：文件后缀、状态枚举、操作类型、统一返回码。

禁止在业务代码中硬编码魔鬼数字，所有状态统一引用本模块常量。
"""

# ========== 支持的文件格式 ==========
SUPPORTED_SUFFIX = {".pdf", ".txt", ".docx", ".doc"}

# 文献格式枚举（写入 literature_info.literature_type）
LIT_TYPE_PDF = "PDF"
LIT_TYPE_TXT = "TXT"
LIT_TYPE_DOCX = "DOCX"

SUFFIX_TO_TYPE = {
    ".pdf": LIT_TYPE_PDF,
    ".txt": LIT_TYPE_TXT,
    ".docx": LIT_TYPE_DOCX,
    ".doc": LIT_TYPE_DOCX,
}

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
# 默认解析规则最近一次修改时间（UTC 字符串），用于提示旧报告重新解析
CFG_PARSE_RULE_VERSION = "parse_rule_version"
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
AI_MAX_TOKENS = 4096               # 单次响应上限
