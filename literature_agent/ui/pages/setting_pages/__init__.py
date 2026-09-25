"""管理中心 5 个子页面：解析规则 / 存储路径 / AI 模型 / 导出备份 / 标签管理。"""
from PyQt5.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget


def build_section_card(title: str = ""):
    """构造管理页通用分区卡片。

    Args:
        title: 分区标题（为空则不创建标题）。
    Returns:
        (QFrame, QVBoxLayout) 卡片与内容布局，调用方向布局内追加控件。
    """
    card = QFrame()
    card.setObjectName("settingCard")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(10)
    if title:
        label = QLabel(title)
        label.setObjectName("blockTitle")
        layout.addWidget(label)
    return card, layout


def build_scroll_content(page: QWidget):
    """为管理子页面组装“标题栏 + 滚动内容区”骨架。

    Args:
        page: 子页面 QWidget。
    Returns:
        内容容器的 QVBoxLayout（滚动区内），调用方继续 addWidget 卡片。
    """
    from PyQt5.QtWidgets import QScrollArea

    root = QVBoxLayout(page)
    root.setContentsMargins(20, 20, 20, 20)
    root.setSpacing(10)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)

    content = QWidget()
    content.setObjectName("settingScrollContent")
    content_layout = QVBoxLayout(content)
    content_layout.setContentsMargins(0, 0, 4, 0)
    content_layout.setSpacing(12)
    scroll.setWidget(content)
    root.addWidget(scroll, stretch=1)
    return content_layout
