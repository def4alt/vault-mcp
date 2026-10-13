// Minimal MCP Apps view <-> host bridge (protocol 2026-01-26): JSON-RPC 2.0 over postMessage.
const mcpApp = (() => {
  let next = 1;
  const pending = new Map();
  const handlers = {};
  const send = (msg) => window.parent.postMessage({ jsonrpc: "2.0", ...msg }, "*");

  window.addEventListener("message", (event) => {
    if (event.source !== window.parent) return;
    const m = event.data;
    if (!m || m.jsonrpc !== "2.0") return;
    if ("id" in m && !m.method) {
      const p = pending.get(m.id);
      if (!p) return;
      pending.delete(m.id);
      m.error ? p.reject(new Error(m.error.message || "request failed")) : p.resolve(m.result);
      return;
    }
    (handlers[m.method] || (() => {}))(m.params || {});
    if ("id" in m) send({ id: m.id, result: {} }); // e.g. ui/resource-teardown
  });

  const request = (method, params) =>
    new Promise((resolve, reject) => {
      const id = next++;
      pending.set(id, { resolve, reject });
      send({ id, method, params });
    });
  const notify = (method, params) => send({ method, params });

  function applyTheme(ctx) {
    const root = document.documentElement;
    for (const [k, v] of Object.entries(ctx?.styles?.variables || {})) root.style.setProperty(k, v);
    if (ctx?.theme) root.dataset.theme = ctx.theme;
  }

  return {
    on(method, fn) { handlers[method] = fn; },
    async start(name) {
      const result = await request("ui/initialize", {
        protocolVersion: "2026-01-26", appCapabilities: {}, clientInfo: { name, version: "1" },
      });
      applyTheme(result?.hostContext);
      handlers["ui/notifications/host-context-changed"] = applyTheme;
      notify("ui/notifications/initialized", {});
      const report = () => notify("ui/notifications/size-changed", {
        width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight,
      });
      new ResizeObserver(report).observe(document.body);
      report();
      return result;
    },
    async callTool(name, args) {
      const r = await request("tools/call", { name, arguments: args });
      if (r?.isError) throw new Error((r.content || []).map((c) => c.text).join(" ") || `${name} failed`);
      return r?.structuredContent ?? r;
    },
    message: (text) => request("ui/message", { role: "user", content: { type: "text", text } }),
    openLink: (url) => request("ui/open-link", { url }),
  };
})();
