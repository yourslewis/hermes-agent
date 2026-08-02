from __future__ import annotations

from types import SimpleNamespace


def test_hrun_hook_skips_gateway_and_posts_plan_blocks(monkeypatch):
    import plugins.harness_controller as hc

    posts = []

    class Client:
        async def chat_postMessage(self, **kwargs):
            posts.append(kwargs)
            return {"ts": "1.2"}

    class Adapter:
        def _get_client(self, chat_id):
            return Client()

        async def send(self, chat_id, text, metadata=None):
            posts.append({"channel": chat_id, "text": text, "metadata": metadata})

    class Platform:
        value = "slack"

    source = SimpleNamespace(platform=Platform(), chat_id="C1", thread_id="123.4")
    event = SimpleNamespace(text="/hrun --harness copilot --model gpt-5.4 Test task", source=source, message_id="123.4")
    gateway = SimpleNamespace(adapters={source.platform: Adapter()})

    async def fake_run_capture(argv, cwd=None, timeout=600):
        return 0, "Plan body"

    monkeypatch.setattr(hc, "_run_capture", fake_run_capture)
    result = hc._pre_gateway_dispatch(event=event, gateway=gateway)

    assert result == {"action": "skip", "reason": "harness controller handling /hrun"}
    assert any("Planning `Test task`" in post["text"] for post in posts)
    plan_posts = [post for post in posts if "blocks" in post]
    assert plan_posts
    action_blocks = [block for block in plan_posts[-1]["blocks"] if block.get("type") == "actions"]
    assert action_blocks
    assert action_blocks[-1]["elements"][0]["action_id"] == "harness_approve"



def test_hrun_hook_posts_long_codex_plan_without_oversized_slack_sections(monkeypatch):
    import plugins.harness_controller as hc

    posts = []

    class Client:
        async def chat_postMessage(self, **kwargs):
            for block in kwargs.get("blocks") or []:
                if block.get("type") == "section":
                    assert len(block["text"]["text"]) <= 3000
            posts.append(kwargs)
            return {"ts": "1.2"}

    class Adapter:
        def _get_client(self, chat_id):
            return Client()

        async def send(self, chat_id, text, metadata=None):
            posts.append({"channel": chat_id, "text": text, "metadata": metadata})

    class Platform:
        value = "slack"

    source = SimpleNamespace(platform=Platform(), chat_id="C1", thread_id="123.4")
    event = SimpleNamespace(text="/hrun --harness codex --model gpt-5.6-sol " + "Fix it " * 300, source=source, message_id="123.4")
    gateway = SimpleNamespace(adapters={source.platform: Adapter()})

    async def fake_run_capture(argv, cwd=None, timeout=600):
        return 0, "Codex plan body.\n" * 900

    monkeypatch.setattr(hc, "_run_capture", fake_run_capture)

    result = hc._pre_gateway_dispatch(event=event, gateway=gateway)

    assert result == {"action": "skip", "reason": "harness controller handling /hrun"}
    assert any("blocks" in post for post in posts)


def test_source_dict_records_current_agent_from_environment(monkeypatch):
    import plugins.harness_controller as hc

    class Platform:
        value = "slack"

    source = SimpleNamespace(platform=Platform(), chat_id="C1", thread_id="123.4")
    event = SimpleNamespace(source=source, message_id="123.4")
    monkeypatch.setenv("HERMES_PROFILE", "selin")

    data = hc._source_dict_from_event(event)

    assert data["agent"] == "selin"
