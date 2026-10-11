from langchain_core.messages import AIMessage, ToolMessage

from robot_api import agent


def test_api_key_from_env_file(tmp_path, monkeypatch):
    for k in ("claude_key", "CLAUDE_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / ".env").write_text("claude_key = sk-test-123\n")
    monkeypatch.setattr(agent.os.path, "abspath", lambda p: str(tmp_path / "agent.py"))
    assert agent.load_api_key() == "sk-test-123"


def image_msg(i):
    return ToolMessage(content=[{"type": "image", "base64": "x", "mime_type": "image/jpeg"}],
                       tool_call_id=f"c{i}")


def test_prune_keeps_only_latest_image():
    h = [{"role": "user", "content": "hi"}, image_msg(1), image_msg(2), image_msg(3)]
    agent.prune_images(h)
    assert [m.content for m in h[1:3]] == [agent.IMAGE_OMITTED] * 2
    assert isinstance(h[3].content, list)


def test_trim_dangling_tool_calls():
    call = AIMessage(content="", tool_calls=[{"name": "walk", "args": {}, "id": "t1"}])
    h = [{"role": "user", "content": "walk"}, call]
    assert agent.trim_dangling(list(h)) == h[:1]
    done = h + [ToolMessage(content="ok", tool_call_id="t1")]
    assert agent.trim_dangling(list(done)) == done


def test_thinking_kwargs_per_model_family():
    assert agent.thinking_kwargs("claude-haiku-5-5", "low")["effort"] == "low"
    assert agent.thinking_kwargs("claude-opus-5", "high")["effort"] == "high"
    old = agent.thinking_kwargs("claude-haiku-4-5-20251001", "low")
    assert old["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert "thinking" not in agent.thinking_kwargs("claude-haiku-4-5-20251001", "off")


def test_ollama_models_default_to_no_thinking():
    assert agent.default_thinking("qwen3.6:35b") == "off"
    assert agent.default_thinking("gemma4:26b") == "off"
    assert agent.default_thinking("claude-haiku-5-5") == "low"


def test_ollama_agent_built_without_reasoning(monkeypatch):
    import pytest

    pytest.importorskip("langchain_ollama")
    import langchain_ollama

    seen = {}

    class FakeChat:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def bind_tools(self, tools):
            return self

    monkeypatch.setattr(langchain_ollama, "ChatOllama", FakeChat)
    try:
        agent.build_agent(None, "qwen3.6:35b", agent.default_thinking("qwen3.6:35b"), None, None)
    except Exception:  # building the rest of the agent needs more than the fake model
        pass
    assert seen.get("reasoning") is False
