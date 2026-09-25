"""全局 UI 常量：主题配色、字体、窗口尺寸。

颜色值来源：UI 规划文档配色方案 + 原型模板.html 的 CSS 变量。
业务/UI 代码一律从本模块读取，禁止硬编码颜色。
"""

# ========== 窗口尺寸（固定约束）==========
WINDOW_DEFAULT_WIDTH = 1280
WINDOW_DEFAULT_HEIGHT = 800
WINDOW_MIN_WIDTH = 960
WINDOW_MIN_HEIGHT = 640
SIDEBAR_WIDTH = 220
HEADER_HEIGHT = 40

# ========== 字体 ==========
FONT_FAMILY = '"Microsoft YaHei", "Noto Sans SC", sans-serif'
FONT_SIZE_TITLE = 16   # 一级标题
FONT_SIZE_H2 = 14      # 二级标题
FONT_SIZE_BODY = 12    # 正文
FONT_SIZE_AUX = 10     # 辅助文字

# 字体层级（family, point_size, weight；QFont.Bold=75 / QFont.Normal=50）
FONT_TITLE = ("Microsoft YaHei", 16, 75)
FONT_H2 = ("Microsoft YaHei", 14, 75)
FONT_BODY = ("Microsoft YaHei", 12, 50)
FONT_AUX = ("Microsoft YaHei", 10, 50)

# ========== 浅色主题（默认，对齐原型 :root）==========
THEME_LIGHT = {
    "theme_name": "light",
    "bg_main": "#ffffff",        # 主背景
    "bg_sidebar": "#F5F7FA",     # 侧边栏/表头底色
    "bg_card": "#ffffff",        # 卡片背景
    "bg_alt": "#F5F8FA",         # 表格交替行底色
    "bg_input": "#ffffff",       # 输入框背景
    "primary": "#E8F4F8",        # 主色（选中/高亮）
    "primary_border": "#c7e4ec", # 主色描边
    "primary_text": "#111827",   # 主色按钮上的文字
    "text_title": "#1D2129",
    "text_body": "#1D2129",
    "text_aux": "#86909C",
    "border": "#E5E6EB",
    "info": "#4096BB",
    "success": "#52C41A",
    "warn": "#FAAD14",
    "error": "#FF4D4F",
    "hover_overlay": "rgba(0, 0, 0, 0.05)",
    "mask_overlay": "rgba(0, 0, 0, 0.40)",
    "shadow": "rgba(0, 0, 0, 0.06)",
}

# ========== 深色主题（夜间模式）==========
THEME_DARK = {
    "theme_name": "dark",
    "bg_main": "#1F2937",
    "bg_sidebar": "#273444",
    "bg_card": "#273444",
    "bg_alt": "#202B3A",          # 表格交替行底色（略暗于卡片）
    "bg_input": "#1F2937",
    "primary": "#E8F4F8",
    "primary_border": "#c7e4ec",
    "primary_text": "#111827",
    "text_title": "#F9FAFB",
    "text_body": "#F9FAFB",
    "text_aux": "#9CA3AF",
    "border": "#374151",
    "info": "#4096BB",
    "success": "#52C41A",
    "warn": "#FAAD14",
    "error": "#FF4D4F",
    "hover_overlay": "rgba(255, 255, 255, 0.08)",
    "mask_overlay": "rgba(0, 0, 0, 0.55)",
    "shadow": "rgba(0, 0, 0, 0.25)",
}

THEME_MAP = {
    "light": THEME_LIGHT,
    "dark": THEME_DARK,
}
