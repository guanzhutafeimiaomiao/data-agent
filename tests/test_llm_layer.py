from __future__ import annotations

from insight_agent.config import Settings
from insight_agent.llm import prompts
from insight_agent.llm.chat import extract_json
from insight_agent.runtime import Runtime


def test_semantics_block_renders_into_prompts():
    """模板占位符漏掉会直接 KeyError，用一条断言兜住。"""
    block = "- 客单价 = 销售额 / 客户数"
    sql_system = prompts.SQL_GEN_SYSTEM.substitute(schema="t(a)", semantics=block)
    critic_system = prompts.CRITIC_SYSTEM.substitute(
        question="q", sql="s", rows="[]", semantics=block
    )
    extract_system = prompts.EXTRACT_SEMANTICS_SYSTEM.substitute(question="以后客单价按客户数算")

    assert block in sql_system and "优先级最高" in sql_system
    assert block in critic_system
    assert "以后客单价按客户数算" in extract_system


def test_extract_json_survives_code_fence_and_chatter():
    assert extract_json('```json\n{"sql": "select 1"}\n```')["sql"] == "select 1"
    assert extract_json('好的，结果如下：{"verdict": "ok"} 以上。')["verdict"] == "ok"
    assert extract_json("完全不是 JSON") == {}


def test_backend_falls_back_to_fake_without_key(workdir):
    settings = Settings(db_path=workdir / "demo.sqlite", api_key="", fake_llm=False, _env_file=None)
    rt = Runtime.create(settings)
    assert rt.llm.name == "fake"


def test_backend_switches_to_chat_when_key_present(workdir):
    settings = Settings(
        db_path=workdir / "demo.sqlite",
        api_key="sk-test",
        fake_llm=False,
        _env_file=None,
    )
    rt = Runtime.create(settings)
    assert rt.llm.name == "chat"
    assert rt.llm.strong.model_name == settings.model_name
