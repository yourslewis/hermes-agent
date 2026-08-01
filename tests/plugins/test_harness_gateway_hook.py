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
    assert plan_posts[-1]["blocks"][1]["elements"][0]["action_id"] == "harness_approve"
