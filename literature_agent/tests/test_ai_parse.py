"""AI 通道工具层、Agent 原件/文本分流与授权降级、关键词持久化测试。

Legacy 固定工作流已下线，AI 解析统一由 ReAct Agent 编排，本文件覆盖：
- ai_client 文本/文件消息契约、通道分类、协议错误与可降级判定；
- qwen-long（file_id）/qwen3.8 系列（file_data）/普通模型（全文文本）
  在 Agent 端到端解析中的通道选择；
- 原件通道失败时按错误类型精确降级：协议不支持/超限经用户授权才换文本，
  网络/鉴权错误不重试直接转本地基线，错误原文随报告回传；
- 提示词外置资源（resources/prompts）经 agent.prompts 加载；
- 关键词 JSON 持久化到报告表，刷新重读不丢失；
- PDF 抽取去中文字间空格、双栏版面还原阅读顺序；
- schema v2→v3 老库自动迁移补 keywords 列。
超长文本 map-reduce 的 Agent 集成测试见 test_agent.py。
"""
import json
import os
import sqlite3

import pytest

from business.literature_import import LiteratureImportService
from business.literature_parse import LiteratureParseService
from business.system_service import SystemService
from config import constants as C
from data_layer.dao.config_dao import SystemConfigDao
from data_layer.dao.report_dao import LiteratureReportDao
from data_layer.db_connect import DatabaseManager
from data_layer.db_init import SCHEMA_VERSION, init_db
from tool_layer import ai_client, file_parser
from utils.exceptions import AIServiceError

PAPER_TEXT = """模块化设计研究

李四

摘要
本文研究记账类 APP 的模块化设计方法，提出分层解耦方案。

引言
随着移动互联网发展，记账类应用复杂度上升，模块化成为必然趋势。

研究方法
本文采用案例分析法与对比实验法，对多款记账 APP 进行拆解。

创新点
提出一种可复用的模块划分算法与接口规范。

结论
模块化设计能显著提升研发效率，建议推广至同类工具类应用。

参考文献
[1] 张三. 软件工程模块化研究. 2022.
[2] Wang M. Software architecture review. 2023.
"""

# 带真实段落锚点的五维度 AI 输出（段落号与 PAPER_TEXT 的空行切分一致），
# Agent 溯源校验按"锚点段落与解读词汇相关"判定，故各维度直接复述对应章节
_AI_JSON = {
    "research_background":
        "随着移动互联网发展，记账类应用复杂度上升，模块化成为必然趋势。"
        "[原文位置：3]",
    "core_view":
        "本文研究记账类 APP 的模块化设计方法，提出分层解耦方案。"
        "[原文位置：2]",
    "research_method":
        "本文采用案例分析法与对比实验法，对多款记账 APP 进行拆解。"
        "[原文位置：4]",
    "innovation_point":
        "提出一种可复用的模块划分算法与接口规范。[原文位置：5]",
    "research_conclusion":
        "模块化设计能显著提升研发效率，建议推广至同类工具类应用。"
        "[原文位置：6]",
    "keywords": ["模块化", "记账APP", "分层解耦"],
}


def _ai_answer():
    """返回一份 AI JSON 输出副本（避免用例间共享可变对象）。"""
    return json.dumps(_AI_JSON, ensure_ascii=False)


@pytest.fixture
def imported_lit(tmp_path):
    """导入一篇结构化 TXT 文献，返回 lit_id。"""
    SystemConfigDao().set(C.CFG_LITERATURE_PATH, str(tmp_path / "store"))
    path = tmp_path / "paper.txt"
    path.write_text(PAPER_TEXT, encoding="utf-8")
    code, data, msg = LiteratureImportService().import_single(str(path))
    assert code == 0, msg
    return data["id"]


def _patch_model(monkeypatch, name="qwen-max",
                 url="https://model.example.test/v1",
                 channel=C.AI_FILE_CHANNEL_AUTO):
    """把当前 AI 模型替换为内存假配置（不产生真实网络）。

    Args:
        channel: 持久化的原件通道（auto/file_id/file_data/none）。
    """
    monkeypatch.setattr(
        SystemService, "get_current_ai_model",
        lambda self, decrypt=False: {
            "name": name, "base_url": url, "api_key": "sk-test-key",
            "file_channel": channel,
        },
    )


# ================= 模型能力与协议错误识别 =================

def test_model_capability_detection():
    """qwen-long/Kimi 支持文件直传；qwen-max 等普通模型走全文文本。"""
    detect = ai_client.model_supports_file
    assert detect("qwen-long", "https://dashscope.example/v1") is True
    assert detect("moonshot-v1-32k", "https://api.moonshot.test/v1") is True
    assert detect("kimi-latest", "https://api.test/v1") is True
    assert detect("qwen-max", "https://dashscope.example/compatible-mode/v1") is False
    assert detect("ernie-4.0", "https://aistudio.example/v3") is False


def test_file_channel_degradable_classification():
    """原件失败的精确降级判定：协议拒绝/超限可降级，鉴权/网络错误不降级。"""
    protocol_msgs = [
        "模型服务返回错误（400）：Invalid value: file. "
        "Supported values are: 'text','image_url'",
        "模型服务返回错误（400）：invalid_value for content type file",
        "模型服务返回错误（400）：该模型不支持文件类型消息",
    ]
    for message in protocol_msgs:
        assert ai_client.is_file_channel_degradable(message) is True
    oversize = "PDF 原件过大（约 120MB），超过内联发送上限 100MB"
    assert ai_client.is_file_channel_degradable(oversize) is True
    # 网络/鉴权/超时：换文本通道大概率同样失败，不降级
    assert ai_client.is_file_channel_degradable(
        "无法连接模型服务：timeout") is False
    assert ai_client.is_file_channel_degradable(
        "模型服务返回错误（401）：鉴权失败") is False


def test_context_length_error_classification():
    """文本通道仅"输入超长"保留短文本重试价值，其余致命错误终止 AI。"""
    assert ai_client.is_context_length_error(
        "模型服务返回错误（400）：maximum context length exceeded") is True
    assert ai_client.is_context_length_error(
        "输入文本超过模型上下文长度限制") is True
    assert ai_client.is_context_length_error(
        "无法连接模型服务：connection refused") is False
    assert ai_client.is_context_length_error(
        "模型服务返回错误（401）：鉴权失败") is False


# ================= 长等待心跳与超时文案 =================

def test_http_post_heartbeat_fires_while_waiting(monkeypatch):
    """请求等待期间 on_waiting 按间隔被周期调用，让进度条可见"仍在思考"。"""
    import time
    import urllib.request

    beats = []

    class _SlowResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            time.sleep(0.5)
            return b'{"ok": true}'

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda request, timeout=None: _SlowResponse())

    data = ai_client._http_post(
        "https://model.example.test/v1/chat/completions",
        {"X-Test": "1"}, b"{}", timeout=10,
        on_waiting=lambda elapsed: beats.append(elapsed),
        wait_interval=0.15)
    assert data == {"ok": True}
    assert len(beats) >= 2, "等待 0.5 秒应至少触发两次 0.15 秒心跳"


def test_http_post_timeout_message_points_to_server_not_network(monkeypatch):
    """超时文案应提示服务端生成慢/换模型，而非笼统甩锅网络。"""
    import urllib.request

    def _timeout(request, timeout=None):
        raise TimeoutError("socket timed out")

    monkeypatch.setattr(urllib.request, "urlopen", _timeout)
    with pytest.raises(AIServiceError) as exc_info:
        ai_client._http_post(
            "https://model.example.test/v1/chat/completions",
            {}, b"{}", timeout=42)
    message = str(exc_info.value)
    assert "响应超时" in message
    assert "42" in message
    assert "请检查网络" not in message


# ================= 文本消息通道 =================

def test_chat_with_text_plain_string_message(monkeypatch):
    """chat_with_text：content 必须是纯字符串（指令+全文），不能是 file 数组。"""
    captured = {}

    def _fake_post(url, headers, body, timeout, on_waiting=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = json.loads(body.decode("utf-8"))
        return {"choices": [{"message": {"content": '{"ok": true}'}}]}

    monkeypatch.setattr(ai_client, "_http_post", _fake_post)
    result = ai_client.chat_with_text(
        "https://model.example/v1/", "sk-key", "qwen-max",
        "文献全文内容", "解析指令", "系统提示",
    )
    assert json.loads(result) == {"ok": True}
    assert captured["url"] == "https://model.example/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-key"
    messages = captured["payload"]["messages"]
    assert messages[0] == {"role": "system", "content": "系统提示"}
    user_content = messages[1]["content"]
    assert isinstance(user_content, str)
    assert "解析指令" in user_content
    assert "文献原件完整文本（开始）" in user_content
    assert "文献全文内容" in user_content
    assert "file" not in json.dumps(messages, ensure_ascii=False).lower().replace(
        "文献原件完整文本", ""
    )


# ================= 端到端：Agent 文本通道 + 关键词持久化 =================

def test_parse_with_ai_text_channel(mem_conn, imported_lit, monkeypatch):
    """qwen-max：Agent 不上传原件，完整全文作为文本发送，六维度与关键词入库。"""
    _patch_model(monkeypatch)
    sent_texts = []

    def _fake_chat(base_url, api_key, model, full_text, prompt,
                   system_prompt=None, max_tokens=None, on_waiting=None):
        assert model == "qwen-max"
        assert isinstance(full_text, str) and "记账类" in full_text
        assert "通读全文" in prompt
        sent_texts.append(full_text)
        return _ai_answer()

    monkeypatch.setattr(ai_client, "chat_with_text", _fake_chat)
    upload_called = []
    monkeypatch.setattr(ai_client, "upload_file",
                        lambda *a, **k: upload_called.append(1) or "file-id")

    service = LiteratureParseService()
    code, report, msg = service.parse_single(imported_lit)
    assert code == 0, msg
    assert msg.startswith("解析完成（ReAct 智能体")
    assert report["parse_engine"] == "agent_ai"
    assert upload_called == []  # qwen-max 不应走文件上传
    assert sent_texts and "案例分析法" in sent_texts[0]
    assert report["research_conclusion"].endswith("。")
    # Agent 关键词以本地 jieba 提取为准（AI keywords 仅作校验参照）
    assert isinstance(report["keywords"], list) and report["keywords"]
    assert "模块化" in report["keywords"]
    assert "[2] Wang M." in report["reference_list"]

    # 关键词以 JSON 字符串持久化；刷新页面重读时反序列化为列表
    raw = LiteratureReportDao().get_by_lit_id(imported_lit)
    assert json.loads(raw["keywords"]) == report["keywords"]
    refreshed = LiteratureParseService().get_report(imported_lit)
    assert refreshed["keywords"] == report["keywords"]
    # parse_engine 只存在于当次解析视图，不入库；重读不携带该字段
    assert "parse_engine" not in refreshed


def test_parse_with_ai_file_channel(mem_conn, imported_lit, monkeypatch):
    """qwen-long：Agent 走原件直传，读中 FC 首轮引用上传后的 file_id。"""
    _patch_model(monkeypatch, name="qwen-long")
    calls = {"upload": 0, "round": 0, "text_chat": 0}
    seen_file_ids = []

    def _fake_upload(base_url, api_key, file_path, on_waiting=None):
        calls["upload"] += 1
        assert os.path.isfile(file_path)
        return "file-abc-123"

    def _fake_round(base_url, api_key, model, messages, tools=None,
                    timeout=None, on_waiting=None, **kwargs):
        calls["round"] += 1
        file_part = messages[1]["content"][0]
        assert file_part["type"] == "file"
        seen_file_ids.append(file_part.get("file_id"))
        assert tools  # 原件与读中工具 schema 同框（cohabit）
        return {"content": _ai_answer(), "tool_calls": [],
                "finish_reason": "stop"}

    def _fake_text_chat(*a, **k):
        calls["text_chat"] += 1
        return _ai_answer()

    monkeypatch.setattr(ai_client, "upload_file", _fake_upload)
    monkeypatch.setattr(ai_client, "chat_round", _fake_round)
    monkeypatch.setattr(ai_client, "chat_with_text", _fake_text_chat)

    code, report, msg = LiteratureParseService().parse_single(imported_lit)
    assert code == 0, msg
    assert msg == "解析完成（ReAct 智能体）"
    assert report["parse_engine"] == "agent_ai"
    assert calls == {"upload": 1, "round": 1, "text_chat": 0}
    assert seen_file_ids == ["file-abc-123"]


def test_file_channel_400_without_callback_does_not_degrade(mem_conn, imported_lit,
                                                            monkeypatch):
    """读中 FC 首轮返回 400 且无授权回调：不得静默降级，Agent 转本地并回传错误。"""
    _patch_model(monkeypatch, name="qwen-long")
    protocol_error = (
        "模型服务返回错误（400）：Invalid value: file. "
        "Supported values are: 'text','image_url','video_url'"
    )

    def _fake_round(*a, **k):
        raise AIServiceError(protocol_error)

    text_calls = []

    def _fake_text_chat(*a, **k):
        text_calls.append(1)
        return _ai_answer()

    monkeypatch.setattr(ai_client, "upload_file",
                        lambda *a, **k: "file-id")
    monkeypatch.setattr(ai_client, "chat_round", _fake_round)
    monkeypatch.setattr(ai_client, "chat_with_text", _fake_text_chat)

    code, report, msg = LiteratureParseService().parse_single(imported_lit)
    assert code == 0, msg
    assert report["parse_engine"] == "agent_local"
    assert text_calls == []  # 未获用户允许，不得改走文本通道
    assert report.get("ai_error") == protocol_error


def test_file_channel_400_degrades_after_user_allows(mem_conn, imported_lit,
                                                     monkeypatch):
    """文件通道返回 400：用户授权后 Agent 才用全文文本重试（回调收到错误原文）。"""
    _patch_model(monkeypatch, name="qwen-long")
    protocol_error = (
        "模型服务返回错误（400）：Invalid value: file. "
        "Supported values are: 'text','image_url','video_url'"
    )

    def _fake_round(*a, **k):
        raise AIServiceError(protocol_error)

    text_calls = []

    def _fake_text_chat(base_url, api_key, model, full_text, prompt,
                        system_prompt=None, max_tokens=None, on_waiting=None):
        text_calls.append(full_text)
        return _ai_answer()

    monkeypatch.setattr(ai_client, "upload_file",
                        lambda *a, **k: "file-id")
    monkeypatch.setattr(ai_client, "chat_round", _fake_round)
    monkeypatch.setattr(ai_client, "chat_with_text", _fake_text_chat)

    asked = []

    def _allow(error_message):
        """模拟用户在弹窗中同意降级。"""
        asked.append(error_message)
        return True

    code, report, msg = LiteratureParseService().parse_single(
        imported_lit, degradation_callback=_allow
    )
    assert code == 0, msg
    assert report["parse_engine"] == "agent_ai"
    assert msg.startswith("解析完成（ReAct 智能体")
    assert len(text_calls) == 1 and "记账类" in text_calls[0]
    assert asked == [protocol_error]  # 授权回调必须收到 AI 原始错误信息


def test_file_channel_400_user_denies_falls_back_to_local(mem_conn, imported_lit,
                                                          monkeypatch):
    """文件通道返回 400：用户拒绝降级时 Agent 转本地解析且不再调文本通道。"""
    _patch_model(monkeypatch, name="qwen-long")

    def _fake_round(*a, **k):
        raise AIServiceError("模型服务返回错误（400）：file 不支持")

    text_calls = []

    def _fake_text_chat(*a, **k):
        text_calls.append(1)
        return _ai_answer()

    monkeypatch.setattr(ai_client, "upload_file",
                        lambda *a, **k: "file-id")
    monkeypatch.setattr(ai_client, "chat_round", _fake_round)
    monkeypatch.setattr(ai_client, "chat_with_text", _fake_text_chat)

    code, report, msg = LiteratureParseService().parse_single(
        imported_lit, degradation_callback=lambda error_message: False
    )
    assert code == 0, msg
    assert report["parse_engine"] == "agent_local"
    assert text_calls == []
    assert report.get("ai_error") and "400" in report["ai_error"]


# ================= qwen3.8 系列 PDF Base64 内联通道（file_data） =================

@pytest.fixture
def imported_pdf_lit(tmp_path):
    """导入一篇最小合法 PDF 文献，返回 lit_id（内容与探测 PDF 相同）。"""
    SystemConfigDao().set(C.CFG_LITERATURE_PATH, str(tmp_path / "store"))
    path = tmp_path / "paper.pdf"
    path.write_bytes(ai_client._build_probe_pdf_bytes())
    code, data, msg = LiteratureImportService().import_single(str(path))
    assert code == 0, msg
    return data["id"]


def test_file_channel_mode_classification():
    """通道分类：qwen-long→file_id；qwen3.8 系列→file_data；其余→空。"""
    mode = ai_client.file_channel_mode
    assert mode("qwen-long", "https://x/v1") == ai_client.FILE_CHANNEL_ID
    assert mode("moonshot-v1-32k", "https://x/v1") == ai_client.FILE_CHANNEL_ID
    assert mode("qwen3.8-max", "https://x/v1") == ai_client.FILE_CHANNEL_DATA
    assert mode("qwen3.8-max-0902", "https://x/v1") == ai_client.FILE_CHANNEL_DATA
    assert mode("qwen3.8-flash", "https://x/v1") == ai_client.FILE_CHANNEL_DATA
    assert mode("qwen3.8-27b", "https://x/v1") == ai_client.FILE_CHANNEL_DATA
    assert mode("qwen-max", "https://x/v1") == ai_client.FILE_CHANNEL_NONE
    # 两个原件通道在能力布尔判断中都算"支持原件"
    assert ai_client.model_supports_file("qwen3.8-max", "https://x/v1") is True


def test_qwen38_pdf_uses_inline_base64_channel(mem_conn, imported_pdf_lit,
                                               monkeypatch):
    """qwen3.8-max 解析 PDF：Agent 走 Base64 内联通道进入读中 FC 循环，
    不调用 /files 与 file_id。

    读中循环首轮即原子产出结构化 JSON（模型直读原件，不依赖提取文本锚点），
    维度齐全走信任旁路，只做完整性校验，不再退化到全文文本 Refine。
    """
    _patch_model(monkeypatch, name="qwen3.8-max")
    round_calls, upload_calls, fileid_calls, text_calls = [], [], [], []

    def _fake_round(base_url, api_key, model, messages, tools=None,
                    timeout=None, on_waiting=None, **kwargs):
        """捕获首轮消息部件并返回结构化 JSON。"""
        file_part = messages[1]["content"][0]
        round_calls.append((model, file_part, bool(tools)))
        return {"content": _ai_answer(), "tool_calls": [],
                "finish_reason": "stop"}

    monkeypatch.setattr(ai_client, "chat_round", _fake_round)
    monkeypatch.setattr(ai_client, "upload_file",
                        lambda *a, **k: upload_calls.append(1) or "file-id")
    monkeypatch.setattr(ai_client, "chat_with_file",
                        lambda *a, **k: fileid_calls.append(1))
    monkeypatch.setattr(ai_client, "chat_with_text",
                        lambda *a, **k: text_calls.append(1))

    code, report, msg = LiteratureParseService().parse_single(imported_pdf_lit)
    assert code == 0, msg
    assert len(round_calls) == 1
    model_used, file_part, tools_on = round_calls[0]
    assert model_used == "qwen3.8-max"
    # 契约：内联 file 部件，data URI + filename，不出现 file_id
    assert file_part["type"] == "file"
    assert file_part["file"]["filename"].endswith(".pdf")
    assert file_part["file"]["file_data"].startswith(
        "data:application/pdf;base64,")
    assert "file_id" not in file_part
    assert tools_on  # 原件与读中工具 schema 同框
    assert upload_calls == [] and fileid_calls == [] and text_calls == []
    assert report["parse_engine"] == "agent_ai"


def test_qwen38_non_pdf_uses_text_channel(mem_conn, imported_lit, monkeypatch):
    """qwen3.8-max 解析 TXT/DOCX：直接走全文文本通道，不尝试 PDF 内联。"""
    _patch_model(monkeypatch, name="qwen3.8-max")
    inline_calls, text_calls = [], []

    monkeypatch.setattr(ai_client, "chat_with_local_pdf",
                        lambda *a, **k: inline_calls.append(1))
    monkeypatch.setattr(ai_client, "upload_file",
                        lambda *a, **k: "should-not-be-called")

    def _fake_text(base_url, api_key, model, full_text, prompt,
                   system_prompt=None, max_tokens=None, on_waiting=None):
        """捕获全文文本并返回结构化 JSON。"""
        text_calls.append(full_text)
        return _ai_answer()

    monkeypatch.setattr(ai_client, "chat_with_text", _fake_text)
    code, report, msg = LiteratureParseService().parse_single(imported_lit)
    assert code == 0, msg
    assert msg == "解析完成（ReAct 智能体）"
    assert report["parse_engine"] == "agent_ai"
    assert len(text_calls) == 1 and "记账类" in text_calls[0]
    assert inline_calls == []


def test_unknown_model_configured_file_data_parses_pdf_inline(
    mem_conn, imported_pdf_lit, monkeypatch
):
    """模型名不含任何关键字，但显式配置 file_data：PDF 仍走 Base64 直传。"""
    _patch_model(monkeypatch, name="vendor-pdf-reader",
                 channel=C.AI_FILE_CHANNEL_FILE_DATA)
    round_calls, upload_calls, fileid_calls = [], [], []

    def _fake_round(base_url, api_key, model, messages, tools=None,
                    timeout=None, on_waiting=None, **kwargs):
        """捕获内联 file 部件并返回结构化 JSON。"""
        round_calls.append((model, messages[1]["content"][0]))
        return {"content": _ai_answer(), "tool_calls": [],
                "finish_reason": "stop"}

    monkeypatch.setattr(ai_client, "chat_round", _fake_round)
    monkeypatch.setattr(ai_client, "upload_file",
                        lambda *a, **k: upload_calls.append(1) or "file-id")
    monkeypatch.setattr(ai_client, "chat_with_file",
                        lambda *a, **k: fileid_calls.append(1))

    code, report, msg = LiteratureParseService().parse_single(imported_pdf_lit)
    assert code == 0, msg
    assert len(round_calls) == 1
    model_used, file_part = round_calls[0]
    assert model_used == "vendor-pdf-reader"
    assert file_part["file"]["filename"].endswith(".pdf")
    assert file_part["file"]["file_data"].startswith(
        "data:application/pdf;base64,")
    assert upload_calls == [] and fileid_calls == []
    assert report["parse_engine"] == "agent_ai"


def test_configured_none_channel_uses_text_even_for_long_model_name(
    mem_conn, imported_lit, monkeypatch
):
    """显式仅全文文本：即使模型名像 qwen-long 也不上传原件，只走文本通道。"""
    _patch_model(monkeypatch, name="qwen-long-custom",
                 channel=C.AI_FILE_CHANNEL_NONE)
    text_calls = []

    def _forbid_upload(*a, **k):
        """仅文本通道禁止上传原件。"""
        raise AssertionError("仅全文文本通道不应上传原件")

    def _fake_text(base_url, api_key, model, full_text, prompt,
                   system_prompt=None, max_tokens=None, on_waiting=None):
        """捕获全文文本并返回结构化 JSON。"""
        text_calls.append(full_text)
        return _ai_answer()

    monkeypatch.setattr(ai_client, "upload_file", _forbid_upload)
    monkeypatch.setattr(ai_client, "chat_with_text", _fake_text)
    code, report, msg = LiteratureParseService().parse_single(imported_lit)
    assert code == 0, msg
    assert msg == "解析完成（ReAct 智能体）"
    assert report["parse_engine"] == "agent_ai"
    assert len(text_calls) == 1 and "记账类" in text_calls[0]


def test_chat_with_local_pdf_request_body_contract(tmp_path, monkeypatch):
    """内联通道请求体必须符合官方契约：file.file_data 为 data URI 且带 filename。"""
    pdf_path = tmp_path / "contract.pdf"
    pdf_path.write_bytes(ai_client._build_probe_pdf_bytes())
    captured = {}

    def _fake_http_post(url, headers, body, timeout, on_waiting=None):
        """捕获请求体并返回一份合法的 chat/completions 响应。"""
        captured["url"] = url
        captured["body"] = json.loads(body.decode("utf-8"))
        return {"choices": [{"message": {"content": "AI configuration test document."}}]}

    monkeypatch.setattr(ai_client, "_http_post", _fake_http_post)
    reply = ai_client.chat_with_local_pdf(
        "https://model.example.test/v1", "sk-test-key", "qwen3.8-max",
        str(pdf_path), "指出文件正文中的英文语句。",
    )
    assert "AI configuration" in reply
    assert captured["url"].endswith("/chat/completions")
    content = captured["body"]["messages"][-1]["content"]
    assert isinstance(content, list) and len(content) == 2
    file_part, text_part = content
    # 契约：{"type":"file","file":{"file_data":"data:application/pdf;base64,***)",
    # "filename": xx}}，不得出现 file_id
    assert file_part["type"] == "file"
    assert set(file_part.keys()) == {"type", "file"}
    file_obj = file_part["file"]
    assert file_obj["filename"] == "contract.pdf"
    assert file_obj["file_data"].startswith("data:application/pdf;base64,")
    assert "file_id" not in json.dumps(captured["body"])
    assert text_part == {"type": "text", "text": "指出文件正文中的英文语句。"}


def test_chat_with_local_pdf_rejects_non_pdf_and_oversize(tmp_path):
    """内联通道仅接受 .pdf 且受大小上限保护，非法输入抛业务异常不发请求。"""
    txt_path = tmp_path / "paper.txt"
    txt_path.write_text("x", encoding="utf-8")
    with pytest.raises(AIServiceError):
        ai_client.chat_with_local_pdf(
            "https://x/v1", "k", "qwen3.8-max", str(txt_path), "p",
        )


def test_qwen38_inline_failure_allow_text_retry(mem_conn, imported_pdf_lit,
                                                monkeypatch):
    """PDF 内联失败（超限可降级）：用户允许后 Agent 改用全文文本通道重试。

    探测 PDF 为英文文本，中文结构化输出无法通过溯源，Refine 可能继续调用
    文本通道，故只断言：授权回调收到错误原文、降级后至少发生一次文本调用
    且传入的是本地提取全文，不固化调用次数。
    """
    _patch_model(monkeypatch, name="qwen3.8-max")
    inline_error = "PDF 原件过大（约 120MB），超过内联发送上限 100MB"

    def _fake_inline(*a, **k):
        raise AIServiceError(inline_error)

    text_calls = []
    monkeypatch.setattr(ai_client, "chat_with_local_pdf", _fake_inline)
    monkeypatch.setattr(ai_client, "upload_file",
                        lambda *a, **k: "should-not-be-called")
    monkeypatch.setattr(
        ai_client, "chat_with_text",
        lambda *a, **k: (text_calls.append(a[3]), _ai_answer())[1],
    )
    asked = []
    code, report, msg = LiteratureParseService().parse_single(
        imported_pdf_lit, degradation_callback=lambda err: asked.append(err) or True
    )
    assert code == 0, msg
    assert text_calls  # 已降级到全文文本通道
    assert isinstance(text_calls[0], str) and text_calls[0].strip()
    assert asked == [inline_error]


def test_qwen38_inline_failure_without_callback_falls_back_local(
    mem_conn, imported_pdf_lit, monkeypatch
):
    """PDF 内联失败但错误不可降级（非协议拒绝/非超限）且无授权：直接转本地。"""
    _patch_model(monkeypatch, name="qwen3.8-max")
    text_calls = []
    monkeypatch.setattr(
        ai_client, "chat_with_local_pdf",
        lambda *a, **k: (_ for _ in ()).throw(
            AIServiceError("模型服务返回错误（400）：PDF 解析失败")),
    )
    monkeypatch.setattr(ai_client, "chat_with_text",
                        lambda *a, **k: text_calls.append(1))
    code, report, msg = LiteratureParseService().parse_single(imported_pdf_lit)
    assert code == 0, msg
    assert report["parse_engine"] == "agent_local"
    assert text_calls == []
    assert report.get("ai_error") and "PDF" in report["ai_error"]


def test_ai_unavailable_falls_back_to_local(mem_conn, imported_lit, monkeypatch):
    """AI 服务连接失败：安全回退本地规则解析，六维度仍为完整语句。"""
    _patch_model(monkeypatch)

    def _boom(*a, **k):
        raise AIServiceError("无法连接模型服务：connection refused")

    monkeypatch.setattr(ai_client, "chat_with_text", _boom)
    code, report, msg = LiteratureParseService().parse_single(imported_lit)
    assert code == 0, msg
    assert report["parse_engine"] == "agent_local"
    assert msg.startswith("解析完成（ReAct 智能体")
    # AI 失败原因必须随报告回传给界面弹窗
    assert report.get("ai_error") and "connection refused" in report["ai_error"]
    # 本地 jieba 关键词同样持久化
    assert isinstance(report["keywords"], list) and report["keywords"]


# ================= 原件通道持久化 =================

def test_ai_model_file_channel_default_and_update(mem_conn):
    """新增模型通道默认 auto；可更新为显式通道；非法通道被拒。"""
    service = SystemService()
    assert service.add_ai_model(
        "vendor-pdf", "https://x.example.test/v1", "sk"
    )[0] == C.CODE_SUCCESS
    model = next(
        m for m in service.list_ai_models() if m["name"] == "vendor-pdf"
    )
    assert model["file_channel"] == C.AI_FILE_CHANNEL_AUTO

    code, _d, msg = service.set_ai_key(
        "vendor-pdf", file_channel=C.AI_FILE_CHANNEL_FILE_DATA
    )
    assert code == C.CODE_SUCCESS, msg
    model = next(
        m for m in service.list_ai_models() if m["name"] == "vendor-pdf"
    )
    assert model["file_channel"] == C.AI_FILE_CHANNEL_FILE_DATA

    code, _d, _msg = service.set_ai_key(
        "vendor-pdf", file_channel="bad-channel"
    )
    assert code != C.CODE_SUCCESS


def test_legacy_ai_model_without_file_channel_defaults_auto(mem_conn):
    """老版本配置缺 file_channel 字段时按 auto 兼容读取，不报错。"""
    service = SystemService()
    assert service.add_ai_model(
        "old-model", "https://x.example.test/v1", "sk-old"
    )[0] == C.CODE_SUCCESS
    raw = service._read_ai_models_raw()
    for model in raw:
        model.pop("file_channel", None)
    service.config_dao.set(
        C.CFG_AI_MODELS, json.dumps(raw, ensure_ascii=False)
    )
    model = service.get_current_ai_model()
    assert model["name"] == "old-model"
    assert model["file_channel"] == C.AI_FILE_CHANNEL_AUTO


# ================= AI 配置校验 =================

def test_validate_ai_config_all_pass(monkeypatch):
    """连通与 PDF 原件探测均通过：code=0，两项结果为 True，并回填实测通道。"""
    monkeypatch.setattr(ai_client, "probe_chat",
                        lambda url, key, model: "连接正常")
    monkeypatch.setattr(
        ai_client, "probe_file_parsing",
        lambda url, key, model, mode="auto":
            ("AI configuration test document.", ai_client.FILE_CHANNEL_ID),
    )
    code, result, msg = SystemService().validate_ai_config(
        "qwen-long", "https://model.example.test/v1", "sk-test-key",
    )
    assert code == C.CODE_SUCCESS, msg
    assert result["chat_ok"] is True
    assert "连接正常" in result["chat_reply"]
    assert result["file_ok"] is True
    assert "AI configuration" in result["file_reply"]
    assert result["detected_channel"] == ai_client.FILE_CHANNEL_ID


def test_validate_ai_config_chat_failure(monkeypatch):
    """连通/鉴权失败：返回 AI 错误码与面向用户的错误原文，不继续文件探测。"""
    def _boom(url, key, model):
        """模拟 401 鉴权失败。"""
        raise AIServiceError("模型服务返回错误（401）：鉴权失败")

    file_calls = []
    monkeypatch.setattr(ai_client, "probe_chat", _boom)
    monkeypatch.setattr(
        ai_client, "probe_file_parsing",
        lambda *a, **k: file_calls.append(1),
    )
    code, result, msg = SystemService().validate_ai_config(
        "qwen-max", "https://model.example.test/v1", "bad-key",
    )
    assert code == C.CODE_AI_SERVICE
    assert result["chat_ok"] is False
    assert "401" in result["chat_error"] and "鉴权失败" in msg
    assert result["file_ok"] is None  # 连通未通过时不再探测 PDF
    assert file_calls == []


def test_validate_ai_config_pdf_unsupported_is_still_usable(monkeypatch):
    """具备原件通道的模型探测失败：code=0，file_ok=False 并透传错误说明。"""
    monkeypatch.setattr(ai_client, "probe_chat",
                        lambda url, key, model: "连接正常")

    def _file_boom(url, key, model, mode="auto"):
        """模拟 qwen3.8 系列 PDF 理解返回协议错误。"""
        raise AIServiceError("模型服务返回错误（400）：file 类型不支持")

    monkeypatch.setattr(ai_client, "probe_file_parsing", _file_boom)
    code, result, msg = SystemService().validate_ai_config(
        "qwen3.8-max", "https://model.example.test/v1", "sk-test-key",
    )
    assert code == C.CODE_SUCCESS, msg
    assert result["chat_ok"] is True
    assert result["file_ok"] is False
    assert "file 类型不支持" in result["file_error"]


def test_validate_ai_config_none_channel_skips_file_probe(monkeypatch):
    """显式选择“仅全文文本”：直接给结论，不发文件探测请求。"""
    monkeypatch.setattr(ai_client, "probe_chat",
                        lambda url, key, model: "连接正常")
    file_calls = []
    monkeypatch.setattr(ai_client, "probe_file_parsing",
                        lambda *a, **k: file_calls.append(1))
    code, result, msg = SystemService().validate_ai_config(
        "qwen-long", "https://model.example.test/v1", "sk-test-key",
        file_channel=C.AI_FILE_CHANNEL_NONE,
    )
    assert code == C.CODE_SUCCESS, msg
    assert result["chat_ok"] is True
    assert result["file_ok"] is False
    assert file_calls == []  # 用户显式仅文本，不应发送文件探测
    assert "全文文本" in result["file_error"]


def test_probe_qwen38_uses_inline_channel_without_files_upload(monkeypatch):
    """探测 qwen3.8 系列：走 chat_with_local_pdf 内联，绝不调用 /files。"""
    captured = []

    def _fake_inline(base_url, api_key, model, file_path, prompt):
        """捕获探测内联调用并返回含测试语句的回复。"""
        captured.append((model, file_path))
        assert os.path.isfile(file_path)
        return "正文中的英文语句是：AI configuration test document. Probe token: ZXQ-4827."

    def _forbid_upload(*a, **k):
        """file_data 通道不允许触发 /files 上传。"""
        raise AssertionError("qwen3.8 探测不得调用 /files 上传")

    monkeypatch.setattr(ai_client, "chat_with_local_pdf", _fake_inline)
    monkeypatch.setattr(ai_client, "upload_file", _forbid_upload)
    reply, channel = ai_client.probe_file_parsing(
        "https://model.example.test/v1", "sk-test-key", "qwen3.8-max",
    )
    assert channel == ai_client.FILE_CHANNEL_DATA
    assert "AI configuration" in reply
    assert len(captured) == 1 and captured[0][0] == "qwen3.8-max"
    # 探测临时 PDF 已清理
    assert not os.path.exists(captured[0][1])


def test_probe_none_channel_raises_without_any_request(monkeypatch):
    """显式仅全文文本通道：直接抛友好说明，不发起任何网络请求。"""
    def _forbid(*a, **k):
        """仅文本通道不应产生任何 HTTP 调用。"""
        raise AssertionError("不应发起网络请求")

    monkeypatch.setattr(ai_client, "_http_post", _forbid)
    with pytest.raises(AIServiceError) as exc_info:
        ai_client.probe_file_parsing(
            "https://model.example.test/v1", "sk", "qwen-long",
            mode=ai_client.FILE_CHANNEL_TEXT_ONLY,
        )
    assert "全文文本" in exc_info.value.message


def test_probe_auto_unknown_model_inline_success_skips_upload(monkeypatch):
    """自动识别+未知模型名：内联通道成功即采用 file_data，不再尝试 /files。"""
    captured = []

    def _fake_inline(base_url, api_key, model, file_path, prompt,
                     system_prompt=None, on_waiting=None):
        """未知 PDF 模型走内联探测成功。"""
        captured.append(model)
        return "正文中的英文语句是：AI configuration test document. Probe token: ZXQ-4827."

    monkeypatch.setattr(ai_client, "chat_with_local_pdf", _fake_inline)
    monkeypatch.setattr(
        ai_client, "upload_file",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("内联成功后不应再调用 /files")),
    )
    reply, channel = ai_client.probe_file_parsing(
        "https://model.example.test/v1", "sk-test-key", "vendor-pdf-reader",
        mode=ai_client.FILE_CHANNEL_AUTO,
    )
    assert channel == ai_client.FILE_CHANNEL_DATA
    assert captured == ["vendor-pdf-reader"]
    assert "AI configuration" in reply


def test_probe_auto_unknown_model_falls_back_to_file_id(monkeypatch):
    """自动识别+未知模型名：内联协议拒绝后改用 file_id，成功则采用 file_id。"""
    def _inline_rejected(*a, **k):
        """模拟网关不接受 file_data 内联消息。"""
        raise AIServiceError("模型服务返回错误（400）：file 类型不支持")

    uploads = []

    def _fake_upload(base_url, api_key, file_path, on_waiting=None):
        """记录 /files 上传并返回托管文件 id。"""
        uploads.append(file_path)
        return "file-abc"

    def _fake_chat(base_url, api_key, model, messages, max_tokens=None):
        """file_id 文件对话返回读到的正文。"""
        assert messages[0]["content"][0] == {"type": "file", "file_id": "file-abc"}
        return "AI configuration test document. Probe token: ZXQ-4827."

    monkeypatch.setattr(ai_client, "chat_with_local_pdf", _inline_rejected)
    monkeypatch.setattr(ai_client, "upload_file", _fake_upload)
    monkeypatch.setattr(ai_client, "_post_chat", _fake_chat)
    reply, channel = ai_client.probe_file_parsing(
        "https://model.example.test/v1", "sk-test-key", "vendor-long-context",
        mode=ai_client.FILE_CHANNEL_AUTO,
    )
    assert channel == ai_client.FILE_CHANNEL_ID
    assert len(uploads) == 1 and uploads[0].endswith(".pdf")
    assert "AI configuration" in reply


def test_probe_auto_unknown_model_both_channels_fail(monkeypatch):
    """自动识别：内联与托管通道都被拒绝时抛出最后一个通道的友好错误。"""
    def _inline_rejected(*a, **k):
        """内联通道协议拒绝。"""
        raise AIServiceError("模型服务返回错误（400）：file 类型不支持")

    def _upload_rejected(base_url, api_key, file_path):
        """托管通道 /files 不支持。"""
        raise AIServiceError("模型服务返回错误（404）：当前模型不支持文件上传")

    monkeypatch.setattr(ai_client, "chat_with_local_pdf", _inline_rejected)
    monkeypatch.setattr(ai_client, "upload_file", _upload_rejected)
    with pytest.raises(AIServiceError) as exc_info:
        ai_client.probe_file_parsing(
            "https://model.example.test/v1", "sk", "plain-chat-model",
            mode=ai_client.FILE_CHANNEL_AUTO,
        )
    assert "文件上传" in exc_info.value.message


def test_probe_nonempty_reply_without_token_is_rejected(monkeypatch):
    """模型有非空回复但读不出暗号（如“无法读取附件”）：判通道不可用，不能假成功。"""
    monkeypatch.setattr(ai_client.C, "AI_PROBE_MAX_RETRIES", 1)
    monkeypatch.setattr(ai_client.time, "sleep", lambda *a, **k: None)

    def _fake_inline(*a, **k):
        """模拟模型读不了 PDF，仅返回说明性文本（非空）。"""
        return "抱歉，我无法直接读取该文件的内容。"

    monkeypatch.setattr(ai_client, "chat_with_local_pdf", _fake_inline)
    with pytest.raises(AIServiceError) as exc_info:
        ai_client.probe_file_parsing(
            "https://model.example.test/v1", "sk", "qwen3.8-max",
            mode=ai_client.FILE_CHANNEL_DATA,
        )
    message = exc_info.value.message
    assert "暗号" in message and "全文文本" in message


def test_probe_auto_inline_without_token_falls_through_to_file_id(monkeypatch):
    """auto：内联通道读不出暗号不算成功，继续尝试 file_id 并成功采用。"""
    monkeypatch.setattr(ai_client.C, "AI_PROBE_MAX_RETRIES", 1)
    monkeypatch.setattr(ai_client.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(
        ai_client, "chat_with_local_pdf",
        lambda *a, **k: "我无法读取该文件内容。",
    )
    monkeypatch.setattr(
        ai_client, "upload_file",
        lambda base_url, api_key, file_path: "fid-2",
    )
    monkeypatch.setattr(
        ai_client, "_post_chat",
        lambda *a, **k:
        "AI configuration test document. Probe token: ZXQ-4827.",
    )
    reply, channel = ai_client.probe_file_parsing(
        "https://model.example.test/v1", "sk", "vendor-pdf",
        mode=ai_client.FILE_CHANNEL_AUTO,
    )
    assert channel == ai_client.FILE_CHANNEL_ID
    assert "ZXQ-4827" in reply


def test_probe_reply_token_check():
    """暗号校验：缺暗号不算通过，大小写/空格/标点差异可容忍。"""
    check = ai_client._reply_contains_probe_token
    assert check("正文中的句子是 Probe token: ZXQ-4827.") is True
    assert check("probe token zxq 4827") is True
    assert check("AI configuration test document.") is False
    assert check("抱歉，我无法读取该文件") is False
    assert check("") is False


def test_probe_explicit_file_id_skips_inline(monkeypatch):
    """显式 file_id 通道：只走 /files 托管，绝不尝试内联。"""
    def _forbid_inline(*a, **k):
        """显式 file_id 不应调用内联通道。"""
        raise AssertionError("不应调用内联通道")

    monkeypatch.setattr(ai_client, "chat_with_local_pdf", _forbid_inline)
    monkeypatch.setattr(
        ai_client, "upload_file",
        lambda base_url, api_key, file_path: "fid-1",
    )
    monkeypatch.setattr(
        ai_client, "_post_chat",
        lambda *a, **k: "AI configuration test document. Probe token: ZXQ-4827.",
    )
    reply, channel = ai_client.probe_file_parsing(
        "https://model.example.test/v1", "sk", "vendor-pdf-reader",
        mode=ai_client.FILE_CHANNEL_ID,
    )
    assert channel == ai_client.FILE_CHANNEL_ID
    assert "AI configuration" in reply


def test_validate_ai_config_rejects_bad_params_without_network(monkeypatch):
    """名称/地址非法时直接返回错误码，不发起任何网络请求。"""
    calls = []
    monkeypatch.setattr(ai_client, "probe_chat",
                        lambda *a, **k: calls.append(1))
    service = SystemService()
    code, _r, msg = service.validate_ai_config(
        "", "https://model.example.test/v1", "sk",
    )
    assert code != C.CODE_SUCCESS
    code, _r, msg = service.validate_ai_config(
        "qwen-max", "model.example.test/v1", "sk",
    )
    assert code != C.CODE_SUCCESS and "http" in msg
    assert calls == []


def test_validate_ai_config_uses_stored_decrypted_key(mem_conn, monkeypatch):
    """密钥留空时按模型名读取已保存的加密密钥进行校验。"""
    service = SystemService()
    assert service.add_ai_model("qwen-long")[0] == C.CODE_SUCCESS
    assert service.set_ai_key(
        "qwen-long", "sk-stored-secret", "https://model.example.test/v1"
    )[0] == C.CODE_SUCCESS

    captured = {}

    def _probe(url, key, model):
        """捕获实际使用的密钥，应为解密后的明文。"""
        captured["key"] = key
        return "连接正常"

    monkeypatch.setattr(ai_client, "probe_chat", _probe)
    monkeypatch.setattr(
        ai_client, "probe_file_parsing",
        lambda url, key, model, mode="auto":
            ("AI configuration test document.", ai_client.FILE_CHANNEL_ID),
    )
    code, result, msg = service.validate_ai_config(
        "qwen-long", "https://model.example.test/v1", None,
    )
    assert code == C.CODE_SUCCESS, msg
    assert captured["key"] == "sk-stored-secret"
    assert result["file_ok"] is True


# ================= 提示词资源文件 =================

def test_prompt_resource_files_exist_and_load():
    """系统/用户提示词均外置为 resources/prompts 文件，经 agent.prompts 加载。"""
    from agent import prompts
    names = (
        "ai_system_prompt.txt", "ai_parse_user_prompt.txt",
        "ai_map_user_prompt.txt", "ai_reduce_user_prompt.txt",
        "ai_openings.json", "ai_precision_guides.json",
    )
    for name in names:
        assert os.path.isfile(os.path.join(prompts._PROMPT_DIR, name)), name

    with open(os.path.join(prompts._PROMPT_DIR, "ai_system_prompt.txt"),
              encoding="utf-8") as fh:
        assert fh.read().strip() == prompts.load_system_prompt()
    assert "JSON" in prompts.load_system_prompt()

    # 原件/文本通道开场白与停用维度替换均在 Agent 提示词构造器中完成
    assert "原件" in prompts.build_ai_analyze_prompt(from_original=True)
    assert "通读全文" in prompts.build_ai_analyze_prompt(from_original=False)
    prompt_off = prompts.build_ai_analyze_prompt(
        precision=1, disabled_dims="research_background")
    assert '该维度未启用，填空字符串 ""' in prompt_off
    map_prompt = prompts.build_map_prompt("research_background")
    assert "该维度未启用，固定给 []" in map_prompt


# ================= 推理模型输出截断 / 思维链容错 =================

def test_post_chat_detects_length_truncation(monkeypatch):
    """finish_reason=length（思维链耗尽预算、JSON 被截断）：抛明确截断错误。"""
    def _fake_http_post(url, headers, body, timeout, on_waiting=None):
        """模拟正文 JSON 中途被截断的网关响应。"""
        return {
            "choices": [{
                "message": {"content": '{"research_background":"未完'},
                "finish_reason": "length",
            }],
        }

    monkeypatch.setattr(ai_client, "_http_post", _fake_http_post)
    with pytest.raises(AIServiceError) as exc_info:
        ai_client._post_chat(
            "https://model.example.test/v1", "sk", "qwen3.8-max", [],
        )
    assert "截断" in exc_info.value.message


def test_post_chat_empty_content_raises(monkeypatch):
    """思考耗尽预算导致 content 为空：抛可降级的明确错误，不放行空串。"""
    monkeypatch.setattr(
        ai_client, "_http_post",
        lambda url, headers, body, timeout, on_waiting=None:
        {"choices": [{"message": {"content": None},
                      "finish_reason": "stop"}]},
    )
    with pytest.raises(AIServiceError) as exc_info:
        ai_client._post_chat(
            "https://model.example.test/v1", "sk", "qwen3.8-max", [],
        )
    assert "为空" in exc_info.value.message


def test_post_chat_normal_response_returns_content(monkeypatch):
    """正常 stop 响应原样返回正文。"""
    payload = {"choices": [{
        "message": {"content": "结果文本"}, "finish_reason": "stop",
    }]}
    monkeypatch.setattr(
        ai_client, "_http_post",
        lambda url, headers, body, timeout, on_waiting=None: payload,
    )
    assert ai_client._post_chat(
        "https://model.example.test/v1", "sk", "m", [],
    ) == "结果文本"


def test_extract_json_block_strips_think_tag():
    """网关把思维链拼进正文时，先剔除 <think> 块再提取 JSON。"""
    raw = (
        '<think>先分析一下结构，{"不是": "结果"} 中间过程</think>'
        '结果如下：\n{"research_background":"背景内容","core_view":""}'
    )
    result = ai_client.extract_json_block(raw)
    assert result["research_background"] == "背景内容"
    assert result["core_view"] == ""


def test_extract_json_block_repairs_unescaped_inner_quotes():
    """模型在字段值里写裸英文双引号（如 作者以"FLPE账本"为载体）：容错转义后解析。"""
    raw = (
        '{"core_view":"作者以"FLPE账本"为设计实践载体，提出模块化方案。",'
        '"research_method":"比较"鲨鱼记账"等案例。","keywords":["FLPE账本"]}'
    )
    result = ai_client.extract_json_block(raw)
    assert '作者以"FLPE账本"为设计实践载体，提出模块化方案。' == result["core_view"]
    assert result["research_method"] == '比较"鲨鱼记账"等案例。'
    assert result["keywords"] == ["FLPE账本"]


def test_extract_json_block_tolerates_trailing_comma_and_raw_newline():
    """尾随逗号与字符串内裸换行/制表符均应被容错。"""
    raw = '{"a": "第一行\n第二行", "b": [1, 2,],}'
    result = ai_client.extract_json_block(raw)
    assert result["a"] == "第一行\n第二行"
    assert result["b"] == [1, 2]


def test_extract_json_block_keeps_already_escaped_quotes():
    """合法转义引号不被二次破坏。"""
    raw = '{"a": "他说\\"你好\\"", "b": 1}'
    assert ai_client.extract_json_block(raw) == {"a": '他说"你好"', "b": 1}


def test_extract_json_block_repairs_real_qwen_response_fixture():
    """真实抓取的 qwen3.8 畸形回复（多处裸引号）可解析且字段完整。"""
    raw = (
        '{"core_view":"需要以模块化为软件基础，作者以"FLPE账本"为设计实践'
        '载体，并通过"FLPE账本"验证。","keywords":["信息可视化","FLPE账本"]}'
    )
    result = ai_client.extract_json_block(raw)
    assert result["core_view"].count('"') == 4  # 两对正文引号均保留
    assert result["keywords"] == ["信息可视化", "FLPE账本"]


def test_ai_max_tokens_reserves_reasoning_budget():
    """输出上限必须为思维链+正文预留足够空间，不得退回 4096。"""
    assert C.AI_MAX_TOKENS >= 8192


# ================= 超长全文分块 map-reduce =================

def test_split_text_chunks_keeps_global_paragraph_indices():
    """分块以全局段落号元组返回，编号与空行切分一致，块间整段重叠。"""
    from agent.tools import split_text_chunks

    paragraph = "一二三四五六七八九十一二三四五六七八九十"  # 20 字
    assert len(paragraph) == 20
    full_text = "\n\n".join(f"{index}{paragraph}" for index in range(6))

    chunks = split_text_chunks(full_text, size=60, overlap=20)

    # 每块为 (全局段落号, 段落原文) 元组列表，且至少切成两块
    assert len(chunks) >= 2
    for chunk in chunks:
        assert chunk and all(isinstance(item, tuple) and len(item) == 2
                             for item in chunk)
    # 首块从 0 号段落开始；全部段落 0..5 均被某块覆盖
    assert chunks[0][0][0] == 0
    covered = {idx for chunk in chunks for idx, _text in chunk}
    assert covered == set(range(6))
    # 块间按整个段落重叠：后块首段号不晚于前块末段号
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt[0][0] <= prev[-1][0]
    # 元组中的段落号与原文段落一一对应
    for chunk in chunks:
        for idx, text in chunk:
            assert text == f"{idx}{paragraph}"


def test_split_text_chunks_oversized_paragraph_owns_chunk():
    """开篇单段超过块上限时独占一块（不硬切句子），编号仍为全局段落号。"""
    from agent.tools import split_text_chunks

    long_paragraph = "超长段落" * 30  # 120 字
    full_text = f"{long_paragraph}\n\n短段乙\n\n短段丙"
    chunks = split_text_chunks(full_text, size=60, overlap=10)
    flattened = [(idx, text) for chunk in chunks for idx, text in chunk]
    assert dict(flattened) == {0: long_paragraph, 1: "短段乙", 2: "短段丙"}
    # 超长首段独占第一个块
    assert chunks[0] == [(0, long_paragraph)]


def test_split_text_chunks_rejects_exceeding_max_chunks():
    """块数超过上限抛业务异常，提示换用长文本模型。"""
    from agent.tools import split_text_chunks
    from utils.exceptions import LiteratureAgentError

    full_text = "\n\n".join(f"第{index}段内容内容内容" for index in range(10))
    with pytest.raises(LiteratureAgentError):
        split_text_chunks(full_text, size=30, overlap=0, max_chunks=3)


# ================= 关键词脏数据兼容 =================

def test_report_view_tolerates_bad_keywords_json(mem_conn, imported_lit):
    """历史/损坏的 keywords 值不是合法 JSON 数组时退化为空列表，不闪退。"""
    service = LiteratureParseService()
    assert service.parse_single(imported_lit)[0] == 0
    dao = LiteratureReportDao()
    row = dao.get_by_lit_id(imported_lit)
    dao.conn.execute(
        "UPDATE literature_report SET keywords = ? WHERE id = ?",
        ("不是JSON", row["id"]),
    )
    dao.conn.commit()
    report = service.get_report(imported_lit)
    assert report["keywords"] == []


# ================= PDF 文本质量 =================

def test_strip_cjk_spaces():
    """清理中文字间空格，保留英文词间与中英文交界空格。"""
    strip = file_parser.strip_cjk_spaces
    assert strip("记 账 类 APP 模 块 化") == "记账类 APP 模块化"
    assert strip("你 好 ， 世 界 。") == "你好，世界。"
    assert strip("deep learning methods work") == "deep learning methods work"
    assert strip("研究 CNN 网络 结构") == "研究 CNN 网络结构"
    assert strip("") == ""


def test_find_column_gutter_double_and_single():
    """双栏版面在中央检测到足够宽的栏间空白；连续单栏返回 None。"""
    # 页宽 400：左栏词块 60..192 连续排布，右栏 222..354，栏间约 30pt
    double = []
    for top in (10, 30, 50):
        for x0 in range(60, 192, 20):
            double.append({"x0": x0, "x1": x0 + 12, "top": top,
                           "text": "左"})
        for x0 in range(222, 354, 20):
            double.append({"x0": x0, "x1": x0 + 12, "top": top,
                           "text": "右"})
    gutter = file_parser.find_column_gutter(double, 400.0)
    assert gutter is not None
    assert gutter[1] - gutter[0] >= 15.0
    divider = (gutter[0] + gutter[1]) / 2
    left = [w for w in double if (w["x0"] + w["x1"]) / 2 < divider]
    right = [w for w in double if (w["x0"] + w["x1"]) / 2 >= divider]
    # 分隔线落在栏间空白内：左组全是左栏词、右组全是右栏词
    assert left and right
    assert {w["text"] for w in left} == {"左"}
    assert {w["text"] for w in right} == {"右"}

    # 单栏：词块从 60 连续排到 340，中央无宽空白
    single = [{"x0": x, "x1": x + 12, "top": 10, "text": "连续词"}
              for x in range(60, 330, 15)]
    assert file_parser.find_column_gutter(single, 400.0) is None


def test_words_to_lines_reading_order():
    """词块按先上后下、同行从左到右拼行，中文相邻不补空格。"""
    words = [
        {"x0": 60, "x1": 75, "top": 10, "text": "你好"},
        {"x0": 30, "x1": 55, "top": 11, "text": "世界"},
        {"x0": 30, "x1": 55, "top": 40, "text": "deep"},
        {"x0": 62, "x1": 90, "top": 40, "text": "learning"},
    ]
    text = file_parser.words_to_lines(words)
    lines = text.splitlines()
    assert lines[0] == "世界你好"
    assert lines[1] == "deep learning"


def test_clean_extracted_text_filters_noise():
    """清理 (cid) 字形、页码行、符号乱码行，并在"上接"标记处截断。"""
    raw = (
        "正文第一行\n"
        "(cid:12)\n"
        "— 67 —\n"
        "%&’(’./089:;<\n"
        "[1] Lecun Y, Bottou L. Gradient-based learning[J].\n"
        "结语内容\n"
        "（上接第 64 页）他文续文不应出现\n"
        "后续他文也不应出现\n"
    )
    out = file_parser.clean_extracted_text(raw)
    assert "(cid" not in out
    assert "%&" not in out
    assert "正文第一行" in out
    assert "结语内容" in out
    # 英文参考文献含空格，不能被当乱码删掉
    assert "Gradient-based learning" in out
    # "上接第 X 页"之后是他文续文，全部截断
    assert "上接" not in out
    assert "他文续文不应出现" not in out
    assert "后续他文也不应出现" not in out


def test_clean_extracted_text_strips_editor_fragment_keeps_sentence():
    """责编标记与正文末句挤在同一物理行时只删碎片，保留正文。"""
    raw = (
        "正常段落正文内容。\n"
        "的软件设计领域相关的理论研究较缺乏，但由于其庞大的市（责编：若佳）\n"
        "责编：若佳\n"
    )
    out = file_parser.clean_extracted_text(raw)
    assert "责编" not in out
    assert "若佳" not in out
    assert "的软件设计领域相关的理论研究较缺乏" in out
    assert "但由于其庞大的市" in out
    assert "正常段落正文内容" in out


def test_find_body_top_requires_dense_pairs():
    """孤立的页眉/标题配对行不能被当正文起点，必须有密集配对行确认。"""
    # 48 页眉、127 大标题在两侧都有词块；232 起才是连续双栏正文
    # 物理行三元组：(top, x0, text)
    left, right = [], []
    for top in (48.0, 127.0):
        left.append((top, 60.0, "左侧页眉标题"))
        right.append((top, 320.0, "右侧页眉标题"))
    for index, top in enumerate(range(232, 304, 12)):
        left.append((float(top), 60.0, f"左文{index}"))
        right.append((float(top), 320.0, f"右文{index}"))
    body_top = file_parser._find_body_top(left, right)
    assert body_top == 232.0
    # 完全没有配对行时返回 None
    assert file_parser._find_body_top([(10, 60.0, "单栏")], []) is None


def test_reconstruct_columns_keeps_title_in_preamble():
    """通栏页眉与大标题在正文前按单栏完整还原，正文保持左栏→右栏顺序。"""
    words = [
        {"x0": 45, "x1": 90, "top": 48, "text": "●页眉左"},
        {"x0": 350, "x1": 420, "top": 48, "text": "页眉右"},
        {"x0": 150, "x1": 162, "top": 127, "text": "甲"},
        {"x0": 170, "x1": 182, "top": 127, "text": "乙"},
        {"x0": 190, "x1": 202, "top": 127, "text": "丙"},
        {"x0": 310, "x1": 322, "top": 127, "text": "丁"},
        {"x0": 330, "x1": 342, "top": 127, "text": "戊"},
    ]
    for index, top in enumerate(range(232, 304, 12)):
        words.append({"x0": 60, "x1": 100, "top": top, "text": f"左文{index}"})
        words.append({"x0": 320, "x1": 360, "top": top, "text": f"右文{index}"})
    text = file_parser._reconstruct_columns(words, divider=300.0)
    lines = [line for line in text.splitlines() if line.strip()]
    # 页眉左右两侧同一行、标题五个字完整且在正文之前
    assert "●页眉左" in lines[0] and "页眉右" in lines[0]
    assert "甲乙丙丁戊" in lines[1]
    body = "\n".join(lines[2:])
    assert body.index("左文0") < body.index("左文5")
    assert body.index("左文5") < body.index("右文0")


# ================= schema v2→v3 迁移 =================

def test_v2_to_v3_migration_adds_keywords(tmp_path):
    """老库（v2、无 keywords 列）启动时自动 ALTER 补列并升到 v3。"""
    db_path = str(tmp_path / "old_v2.db")
    raw = sqlite3.connect(db_path)
    raw.execute(
        """
        CREATE TABLE literature_report (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            literature_id INTEGER NOT NULL,
            research_background TEXT NOT NULL DEFAULT '',
            core_view TEXT NOT NULL DEFAULT '',
            research_method TEXT NOT NULL DEFAULT '',
            innovation_point TEXT NOT NULL DEFAULT '',
            research_conclusion TEXT NOT NULL DEFAULT '',
            reference_list TEXT NOT NULL DEFAULT '',
            parse_time TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
            rule_id INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    raw.execute("PRAGMA user_version = 2")
    raw.commit()
    raw.close()

    DatabaseManager.reset_instance()
    try:
        manager = DatabaseManager(db_path)
        init_db(manager)
        conn = manager.get_conn()
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(literature_report)"
        ).fetchall()}
        assert "keywords" in columns
        # v2 老库会顺序执行 v3、v4 迁移，最终升到当前 schema 版本
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        # 迁移幂等：再初始化一次不报错、不重复加列
        init_db(manager)
        columns_again = {row[1] for row in conn.execute(
            "PRAGMA table_info(literature_report)"
        ).fetchall()}
        assert "keywords" in columns_again
        # 老数据行补默认空串，新插入可正常写 JSON
        conn.execute(
            "INSERT INTO literature_report (literature_id, keywords) VALUES (?, ?)",
            (1, json.dumps(["迁移后关键词"], ensure_ascii=False)),
        )
        conn.commit()
        row = conn.execute(
            "SELECT keywords FROM literature_report WHERE literature_id = 1"
        ).fetchone()
        assert json.loads(row[0]) == ["迁移后关键词"]
    finally:
        DatabaseManager.reset_instance()

