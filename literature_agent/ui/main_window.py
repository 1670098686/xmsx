"""主窗口：顶部 40px 导航 + 左侧 220px 导航栏 + 右侧 QStackedWidget 页面堆栈。

阶段1实现：双栏骨架、页面切换、管理中心折叠菜单、主题切换、自定义窗口按钮。
阶段4新增：Ctrl+S/Ctrl+E 全局快捷键、关闭后恢复上次页面与检索条件。
"""
import json

from PyQt5.QtCore import Qt, QEvent, QTimer
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QShortcut,
    QSizeGrip, QStackedWidget, QToolButton, QVBoxLayout, QWidget, QMainWindow,
)

from config import constants as C
from config.global_config import (
    HEADER_HEIGHT, SIDEBAR_WIDTH, WINDOW_DEFAULT_HEIGHT, WINDOW_DEFAULT_WIDTH,
    WINDOW_MIN_HEIGHT, WINDOW_MIN_WIDTH,
)
from config.settings import get_setting, save_setting
from ui.pages.import_page import ImportPage
from ui.pages.note_page import NotePage
from ui.pages.parse_page import ParsePage
from ui.pages.search_page import SearchPage
from ui.pages.setting_page import SettingPage
from ui.theme_manager import ThemeManager
from ui.widgets.fold_menu import FoldMenu
from ui.widgets.toast import show_toast
from utils.logger import get_logger

# 导航项与堆栈索引
PAGE_IMPORT = 0
PAGE_PARSE = 1
PAGE_SEARCH = 2
PAGE_NOTE = 3
PAGE_SETTING = 4

# 管理中心子菜单 key → 显示名（阶段1共用占位页，阶段3拆分为独立子页）
_SETTING_MODULES = {
    "rules": "解析规则配置",
    "storage": "存储路径配置",
    "ai": "AI模型配置",
    "backup": "资料导出备份",
    "tag": "标签管理",
}


class MainWindow(QMainWindow):
    """应用主窗口（无边框 + 自定义标题栏）。"""

    def __init__(self):
        """初始化主窗口：构建界面、注册快捷键并恢复上次会话。"""
        super().__init__()
        self.setWindowTitle("文献阅读整理Agent")
        self.resize(WINDOW_DEFAULT_WIDTH, WINDOW_DEFAULT_HEIGHT)
        self.setMinimumSize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)
        self.setWindowFlags(Qt.FramelessWindowHint)

        self._drag_pos = None
        self._restoring = False
        # 窗口被其他软件遮挡后，用户点回本窗口的那一下「激活点击」在 Windows 上
        # 会同时投递给控件，若落点恰好在侧边栏就会意外切换页面。
        # 该标志用于吞掉窗口重新激活后短时间内的第一次侧边栏点击。
        self._activate_click_guard = False
        self._first_activate = True
        self._build_ui()
        self._register_shortcuts()
        self._install_sidebar_guard()
        self._restore_last_session()

    # ================= UI 构建 =================

    def _build_ui(self) -> None:
        """构建整体界面（顶部栏 + 主体）。"""
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())
        root.addWidget(self._build_body(), stretch=1)

    def _build_header(self) -> QWidget:
        """构建顶部 40px 导航栏。"""
        header = QFrame()
        header.setObjectName("appHeader")
        header.setFixedHeight(HEADER_HEIGHT)
        header.installEventFilter(self)

        layout = QHBoxLayout(header)
        layout.setContentsMargins(12, 0, 8, 0)
        layout.setSpacing(8)

        logo = QLabel("📖")
        title = QLabel("文献阅读整理Agent")
        title.setObjectName("headerTitle")
        layout.addWidget(logo)
        layout.addWidget(title)
        layout.addStretch(1)

        self.btn_theme = self._make_win_button("🌓")
        self.btn_min = self._make_win_button("─")
        self.btn_max = self._make_win_button("□")
        self.btn_close = self._make_win_button("✕")
        for btn in (self.btn_theme, self.btn_min, self.btn_max, self.btn_close):
            layout.addWidget(btn)

        self.btn_theme.clicked.connect(self._toggle_theme)
        self.btn_min.clicked.connect(self.showMinimized)
        self.btn_max.clicked.connect(self._toggle_max_restore)
        self.btn_close.clicked.connect(self.close)
        return header

    def _make_win_button(self, text: str) -> QToolButton:
        """创建一个固定尺寸的窗口控制按钮。"""
        btn = QToolButton()
        btn.setObjectName("winBtn")
        btn.setText(text)
        btn.setFixedSize(28, 28)
        return btn

    def _build_body(self) -> QWidget:
        """构建左侧导航 + 右侧堆栈主体。"""
        body = QWidget()
        layout = QHBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_sidebar())
        layout.addWidget(self._build_stack(), stretch=1)
        return body

    def _build_sidebar(self) -> QWidget:
        """构建左侧导航栏与管理中心折叠菜单。"""
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(SIDEBAR_WIDTH)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 12, 0, 12)
        layout.setSpacing(0)

        self.nav_list = QListWidget()
        self.nav_list.setObjectName("navList")
        for icon, name in [
            ("📥", "文献导入"),
            ("🔍", "文献解析"),
            ("📚", "文献检索"),
            ("✏️", "笔记批注"),
        ]:
            QListWidgetItem(f"  {icon}  {name}", self.nav_list)
        self.nav_list.setCurrentRow(0)
        self.nav_list.currentRowChanged.connect(self._on_nav_changed)
        layout.addWidget(self.nav_list)

        self.fold_menu = FoldMenu(
            "⚙️ 管理中心",
            items=[(key, text) for key, text in _SETTING_MODULES.items()],
        )
        self.fold_menu.item_clicked.connect(self._on_setting_item)
        layout.addWidget(self.fold_menu)
        layout.addStretch(1)
        return sidebar

    def _build_stack(self) -> QWidget:
        """构建右侧页面堆栈并连接跨页联动信号。"""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        self.stack = QStackedWidget()
        self.page_import = ImportPage()
        self.page_parse = ParsePage()
        self.page_search = SearchPage()
        self.page_note = NotePage()
        self.page_setting = SettingPage()
        for page in (
            self.page_import, self.page_parse, self.page_search,
            self.page_note, self.page_setting,
        ):
            self.stack.addWidget(page)

        # 页面间联动信号
        self.page_search.go_import_requested.connect(
            lambda: self._switch_page(PAGE_IMPORT)
        )
        self.page_search.go_parse_requested.connect(self._goto_parse)
        self.page_parse.go_import_requested.connect(
            lambda: self._switch_page(PAGE_IMPORT)
        )
        self.page_note.go_import_requested.connect(
            lambda: self._switch_page(PAGE_IMPORT)
        )
        self.page_import.go_library_requested.connect(
            lambda: self._switch_page(PAGE_SEARCH)
        )

        layout.addWidget(self.stack)

        grip = QSizeGrip(container)
        grip_layout = QHBoxLayout()
        grip_layout.setContentsMargins(0, 0, 2, 2)
        grip_layout.addStretch(1)
        grip_layout.addWidget(grip)
        layout.addLayout(grip_layout)
        return container

    # ================= 交互处理 =================

    def _register_shortcuts(self) -> None:
        """注册全局快捷键：Ctrl+S 保存笔记、Ctrl+E 导出解析报告。"""
        shortcut_save = QShortcut(QKeySequence("Ctrl+S"), self)
        shortcut_save.activated.connect(self._on_shortcut_save)
        shortcut_export = QShortcut(QKeySequence("Ctrl+E"), self)
        shortcut_export.activated.connect(self._on_shortcut_export)

    def _install_sidebar_guard(self) -> None:
        """在侧边栏全部可点击控件上安装事件过滤器，用于吞掉窗口激活首击。

        覆盖主导航列表（含 viewport）与管理中心折叠菜单（header 与 5 个子项）。
        """
        targets = [self.nav_list, self.nav_list.viewport(),
                   self.fold_menu, self.fold_menu._header]
        targets.extend(self.fold_menu._buttons.values())
        for widget in targets:
            widget.installEventFilter(self)

    def changeEvent(self, event) -> None:
        """窗口重新激活时打开「首击保护」，150ms 后自动解除。

        Windows 行为：点击一个非激活窗口将其激活时，这一下点击会同时下发给
        被点中的控件；用户从其他软件切回时极易误触侧边栏导致页面被切换。
        保护只覆盖激活后极短时间窗：真实鼠标激活点击与激活同帧到达（<16ms），
        而 Alt+Tab/任务栏激活后用户的正常点击通常晚于 150ms，不受影响。
        """
        if event.type() == QEvent.WindowActivate:
            if self._first_activate:
                self._first_activate = False
            else:
                self._activate_click_guard = True
                QTimer.singleShot(150, self._release_activate_guard)
        super().changeEvent(event)

    def _release_activate_guard(self) -> None:
        """超时解除首击保护（任务栏/Alt+Tab 激活后无点击的场景）。"""
        self._activate_click_guard = False

    def _on_shortcut_save(self) -> None:
        """Ctrl+S：仅笔记批注页响应，立即落库防抖笔记。"""
        if self.stack.currentIndex() == PAGE_NOTE:
            self.page_note.save_now()

    def _on_shortcut_export(self) -> None:
        """Ctrl+E：仅文献解析页响应，按默认格式导出当前报告。"""
        if self.stack.currentIndex() == PAGE_PARSE:
            self.page_parse.export_current()

    def _on_nav_changed(self, row: int) -> None:
        """主导航切换页面。"""
        if row < 0:
            return
        self.fold_menu.clear_current()
        self.stack.setCurrentIndex(row)
        self._refresh_page(row)
        self._persist_session()

    def _refresh_page(self, index: int) -> None:
        """切换到核心业务页时刷新数据，保证跨页操作后状态同步。"""
        page = self.stack.widget(index)
        refresh = getattr(page, "refresh", None)
        if callable(refresh):
            refresh()

    def _goto_parse(self, lit_id: int) -> None:
        """检索页卡片“解析”：跳解析页并选中该文献。"""
        self._switch_page(PAGE_PARSE)
        self.page_parse.select_literature(lit_id)

    def closeEvent(self, event) -> None:
        """关闭窗口前落库全部防抖笔记并持久化会话状态。"""
        try:
            self.page_note.flush_before_exit()
        except Exception:
            get_logger().warning("关闭前笔记落库失败", exc_info=True)
        self._persist_session()
        super().closeEvent(event)

    def _on_setting_item(self, key: str) -> None:
        """管理中心子菜单：切换到对应设置子页面。"""
        self.nav_list.setCurrentRow(-1)
        self.page_setting.show_module(key)
        self.stack.setCurrentIndex(PAGE_SETTING)
        self._persist_session()

    def _switch_page(self, index: int) -> None:
        """外部（如空状态引导按钮）请求切换页面。"""
        self.nav_list.setCurrentRow(index)

    # ================= 会话状态持久化 / 恢复（阶段4）=================

    def _persist_session(self) -> None:
        """当前页面、管理子模块、检索条件写入 system_config（失败仅记日志）。"""
        # 启动恢复过程中页面切换会触发本方法，此时控件还是空状态，
        # 不能写回，否则会覆盖待恢复的持久化数据
        if self._restoring:
            return
        try:
            current = self.stack.currentIndex()
            save_setting(C.CFG_LAST_PAGE, str(current))
            if current == PAGE_SETTING:
                save_setting(
                    C.CFG_LAST_SETTING_MODULE, self.page_setting.current_module()
                )
            save_setting(
                C.CFG_SEARCH_STATE,
                json.dumps(self.page_search.capture_state(), ensure_ascii=False),
            )
        except Exception:
            get_logger().warning("会话状态持久化失败", exc_info=True)

    def _restore_last_session(self) -> None:
        """启动恢复上次页面、管理子模块与检索条件；配置损坏时回退导入页。"""
        self._restoring = True
        try:
            page = int(get_setting(C.CFG_LAST_PAGE, "0") or 0)
        except (TypeError, ValueError):
            page = PAGE_IMPORT
        valid_pages = {PAGE_IMPORT, PAGE_PARSE, PAGE_SEARCH, PAGE_NOTE}

        try:
            if page == PAGE_SETTING:
                module = get_setting(C.CFG_LAST_SETTING_MODULE, "rules") or "rules"
                if module not in _SETTING_MODULES:
                    module = "rules"
                # 恢复管理中心：主导航必须取消高亮，折叠菜单高亮对应子项，
                # 否则启动后会出现「文献导入 + 设置子项」上下两个选中态
                self.nav_list.setCurrentRow(-1)
                self.fold_menu.set_current(module)
                self.page_setting.show_module(module)
                self.stack.setCurrentIndex(PAGE_SETTING)
                self._refresh_page(PAGE_SETTING)
            elif page in valid_pages:
                # setCurrentRow 与当前相同时不发信号，统一显式刷新
                self.nav_list.setCurrentRow(page)
                self.stack.setCurrentIndex(page)
                self._refresh_page(page)
                if page == PAGE_SEARCH:
                    self._restore_search_state()
            else:
                self.nav_list.setCurrentRow(PAGE_IMPORT)
                self._refresh_page(PAGE_IMPORT)
        finally:
            self._restoring = False

    def _restore_search_state(self) -> None:
        """回填并执行上次退出时的检索条件（JSON 损坏则保持全部检索）。"""
        try:
            raw = get_setting(C.CFG_SEARCH_STATE, "")
            state = json.loads(raw) if raw else {}
            self.page_search.restore_state(state)
        except (ValueError, TypeError):
            get_logger().warning("检索条件恢复失败", exc_info=True)

    def _toggle_theme(self) -> None:
        """切换浅色/深色主题并弹出轻提示。"""
        new_theme = ThemeManager().toggle_theme(self.app_qapp())
        show_toast(f"已切换{'深色' if new_theme == C.THEME_DARK_NAME else '浅色'}主题")

    @staticmethod
    def app_qapp():
        """返回当前 QApplication 实例。"""
        from PyQt5.QtWidgets import QApplication
        return QApplication.instance()

    def _toggle_max_restore(self) -> None:
        """在窗口最大化与还原状态之间切换。"""
        if self.isMaximized():
            self.showNormal()
            self.btn_max.setText("□")
        else:
            self.showMaximized()
            self.btn_max.setText("❐")

    # ================= 无边框窗口拖动 =================

    def eventFilter(self, obj, event):
        """顶部导航栏按住拖动窗口（双击最大化/还原），并吞掉窗口激活首击。"""
        from PyQt5.QtCore import QEvent
        # 激活首击保护：按下（含双击）与配套松开都吞掉，避免按钮半选状态
        if self._activate_click_guard:
            if event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
                return True
            if event.type() == QEvent.MouseButtonRelease:
                self._activate_click_guard = False
                return True
        if obj.objectName() == "appHeader":
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                if self.isMaximized():
                    self._drag_pos = None
                else:
                    self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
            elif event.type() == QEvent.MouseMove and self._drag_pos is not None:
                if event.buttons() & Qt.LeftButton:
                    self.move(event.globalPos() - self._drag_pos)
            elif event.type() == QEvent.MouseButtonRelease:
                self._drag_pos = None
            elif event.type() == QEvent.MouseButtonDblClick:
                self._toggle_max_restore()
        return super().eventFilter(obj, event)
