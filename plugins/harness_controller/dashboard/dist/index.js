(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  const Registry = window.__HERMES_PLUGINS__;
  if (!SDK || !Registry) return;
  const React = SDK.React;
  const h = React.createElement;
  const { useEffect, useState, useCallback } = SDK.hooks;
  const { Badge, Button, Card, CardContent } = SDK.components;
  const { timeAgo } = SDK.utils;
  const API = "/api/plugins/harness_controller/overview";

  function fmt(ts) {
    if (!ts) return "No messages yet";
    try { return timeAgo ? timeAgo(ts) : new Date(ts).toLocaleString(); }
    catch (_e) { return ts; }
  }

  function DefaultHarness({ value }) {
    const v = value || {};
    return h("div", { className: "hc-default" }, [
      h("div", { className: "hc-kicker", key: "k" }, "Default harness"),
      h("div", { className: "hc-default-main", key: "m" }, [
        h(Badge, { key: "h" }, v.harness || "not set"),
        v.model ? h("span", { key: "model", className: "hc-muted" }, v.model) : null,
        v.mode ? h("span", { key: "mode", className: "hc-muted" }, v.mode) : null,
      ]),
      (v.repo || v.workdir || v.branch) ? h("div", { className: "hc-path", key: "p" }, [v.repo, v.workdir, v.branch].filter(Boolean).join(" · ")) : null,
    ]);
  }

  function ThreadRow({ thread }) {
    return h("div", { className: "hc-thread" }, [
      h("div", { className: "hc-thread-time", key: "time", title: thread.latest_message_time || "" }, fmt(thread.latest_message_time)),
      h("div", { className: "hc-thread-body", key: "body" }, [
        h("div", { className: "hc-about", key: "about" }, thread.about || "Harness run"),
        h("div", { className: "hc-meta", key: "meta" }, [
          h("span", { key: "h" }, thread.harness || "unknown"),
          thread.model ? h("span", { key: "m" }, thread.model) : null,
          thread.state ? h(Badge, { key: "s" }, thread.state) : null,
        ]),
      ]),
    ]);
  }

  function AgentCard({ agent }) {
    return h(Card, { className: "hc-agent" }, h(CardContent, { className: "hc-agent-content" }, [
      h("div", { className: "hc-agent-header", key: "header" }, [
        h("div", { key: "title" }, [
          h("div", { className: "hc-kicker" }, "Agent"),
          h("h2", { className: "hc-agent-name" }, agent.agent || "Unassigned"),
        ]),
        h("div", { className: "hc-count", key: "count" }, `${(agent.threads || []).length} thread${(agent.threads || []).length === 1 ? "" : "s"}`),
      ]),
      h(DefaultHarness, { key: "default", value: agent.default_harness }),
      h("div", { className: "hc-thread-list", key: "threads" }, (agent.threads || []).map((thread) => h(ThreadRow, { key: thread.run_id, thread }))),
    ]));
  }

  function HarnessPage() {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const [loading, setLoading] = useState(true);
    const load = useCallback(function () {
      setLoading(true);
      SDK.fetchJSON(API)
        .then(function (res) { setData(res); setError(null); })
        .catch(function (err) { setError(err && err.message ? err.message : String(err)); })
        .finally(function () { setLoading(false); });
    }, []);
    useEffect(function () { load(); }, [load]);
    const agents = (data && data.agents) || [];
    return h("div", { className: "hc-page" }, [
      h("div", { className: "hc-titlebar", key: "title" }, [
        h("div", { key: "copy" }, [
          h("h1", { className: "hc-title" }, "Harnesses"),
          h("p", { className: "hc-subtitle" }, "Threads grouped by agent. Each agent shows its default harness first, then latest threads newest to oldest."),
        ]),
        h(Button, { key: "refresh", onClick: load, disabled: loading }, loading ? "Refreshing…" : "Refresh"),
      ]),
      error ? h("div", { className: "hc-error", key: "error" }, error) : null,
      (!loading && agents.length === 0) ? h("div", { className: "hc-empty", key: "empty" }, "No harness threads yet.") : null,
      h("div", { className: "hc-grid", key: "grid" }, agents.map((agent) => h(AgentCard, { key: agent.agent, agent }))),
    ]);
  }

  Registry.register("harness_controller", HarnessPage);
})();
