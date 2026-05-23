const els = {
  phase: document.querySelector("#phase"),
  message: document.querySelector("#message"),
  day: document.querySelector("#day"),
  session: document.querySelector("#session"),
  updated: document.querySelector("#updated"),
  liveTitle: document.querySelector("#liveTitle"),
  liveState: document.querySelector("#liveState"),
  liveRefreshButton: document.querySelector("#liveRefreshButton"),
  liveSymbol: document.querySelector("#liveSymbol"),
  livePrice: document.querySelector("#livePrice"),
  liveBook: document.querySelector("#liveBook"),
  liveSpread: document.querySelector("#liveSpread"),
  liveQuoteAge: document.querySelector("#liveQuoteAge"),
  liveBridge: document.querySelector("#liveBridge"),
  liveAccountsBody: document.querySelector("#liveAccountsBody"),
  liveOrdersBody: document.querySelector("#liveOrdersBody"),
  liveTradesBody: document.querySelector("#liveTradesBody"),
  reportsList: document.querySelector("#reportsList"),
  refreshButton: document.querySelector("#refreshButton"),
  reportType: document.querySelector("#reportType"),
  reportTitle: document.querySelector("#reportTitle"),
  symbols: document.querySelector("#symbols"),
  metrics: document.querySelector("#metrics"),
  aggregateBody: document.querySelector("#aggregateBody"),
  rankingBody: document.querySelector("#rankingBody"),
  tabs: [...document.querySelectorAll(".tab")],
};

let reportFilter = "all";
let selectedPath = null;
let latestStatus = null;

async function getJSON(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function fmt(value, digits = 2) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return number.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

function fmtTime(value) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--";
  return date.toLocaleString();
}

function fmtUnix(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return new Date(number * 1000).toLocaleTimeString();
}

function fmtAge(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  if (number < 1) return `${fmt(number, 1)}s`;
  return `${fmt(number, 0)}s`;
}

function bridgeApiBase() {
  const params = new URLSearchParams(window.location.search);
  const configured = params.get("bridge");
  if (configured) return configured.replace(/\/$/, "");
  const host = window.location.hostname || "127.0.0.1";
  return `http://${host}:8780/api`;
}

function pnlClass(value) {
  const number = Number(value);
  if (number > 0) return "positive";
  if (number < 0) return "negative";
  return "";
}

function phaseLabel(value) {
  return String(value || "not_started").replaceAll("_", " ");
}

function setStatus(status) {
  latestStatus = status;
  els.phase.textContent = phaseLabel(status.phase);
  els.phase.classList.toggle("error", status.phase === "error");
  els.message.textContent = status.message || "--";
  els.day.textContent = status.day || "--";
  els.session.textContent = status.session_index ? `#${status.session_index}` : "--";
  els.updated.textContent = fmtTime(status.updated_at);
}

function renderSymbols(symbols) {
  els.symbols.innerHTML = "";
  for (const symbol of symbols || []) {
    const pill = document.createElement("span");
    pill.textContent = symbol;
    els.symbols.appendChild(pill);
  }
}

function renderMetrics(summary) {
  const rows = summary?.aggregate || [];
  const leader = rows[0];
  const totalOrders = rows.reduce((sum, row) => sum + Number(row.orders || 0), 0);
  const totalFills = rows.reduce((sum, row) => sum + Number(row.fills || 0), 0);
  const worstDrawdown = rows.reduce((worst, row) => Math.max(worst, Number(row.worst_drawdown || 0)), 0);
  const metrics = [
    ["Leader", leader ? leader.agent : "--"],
    ["Leader P/L", leader ? fmt(leader.total_pl) : "--"],
    ["Worst DD", fmt(worstDrawdown)],
    ["Orders / Fills", `${totalOrders} / ${totalFills}`],
  ];
  els.metrics.innerHTML = metrics.map(([label, value]) => (
    `<div class="metric"><span class="label">${label}</span><strong>${value}</strong></div>`
  )).join("");
}

function renderAggregate(summary) {
  const rows = summary?.aggregate || [];
  if (!rows.length) {
    els.aggregateBody.innerHTML = `<tr><td class="empty" colspan="7">No aggregate data yet.</td></tr>`;
    return;
  }
  els.aggregateBody.innerHTML = rows.map((row) => `
    <tr>
      <td>${row.agent}</td>
      <td>${row.wins}</td>
      <td class="${pnlClass(row.total_pl)}">${fmt(row.total_pl)}</td>
      <td>${fmt(row.avg_rank, 3)}</td>
      <td class="negative">${fmt(row.worst_drawdown)}</td>
      <td>${row.orders}</td>
      <td>${row.fills}</td>
    </tr>
  `).join("");
}

function renderRankings(summary) {
  const rows = summary?.symbol_rankings || [];
  if (!rows.length) {
    els.rankingBody.innerHTML = `<tr><td class="empty" colspan="7">No symbol rankings yet.</td></tr>`;
    return;
  }
  els.rankingBody.innerHTML = rows.map((row) => `
    <tr>
      <td>${row.symbol}</td>
      <td>${row.rank}</td>
      <td>${row.agent}</td>
      <td class="${pnlClass(row.profit_loss)}">${fmt(row.profit_loss)}</td>
      <td class="negative">${fmt(row.max_drawdown)}</td>
      <td>${row.orders}</td>
      <td>${row.fills}</td>
    </tr>
  `).join("");
}

function renderSummary(summary) {
  renderMetrics(summary);
  renderAggregate(summary);
  renderRankings(summary);
}

function liveUnavailable(message) {
  els.liveTitle.textContent = message || "No active bridge";
  els.liveState.textContent = "Disconnected";
  els.liveState.className = "live-state disconnected";
  els.liveSymbol.textContent = "--";
  els.livePrice.textContent = "--";
  els.liveBook.textContent = "--";
  els.liveSpread.textContent = "--";
  els.liveQuoteAge.textContent = "--";
  els.liveBridge.textContent = "--";
  els.liveAccountsBody.innerHTML = `<tr><td class="empty" colspan="10">No live account data yet.</td></tr>`;
  els.liveOrdersBody.innerHTML = `<tr><td class="empty" colspan="5">No open orders.</td></tr>`;
  els.liveTradesBody.innerHTML = `<tr><td class="empty" colspan="5">No recent trades.</td></tr>`;
}

async function directLivePayload() {
  const apiUrl = bridgeApiBase();
  const [health, state, accountsPayload] = await Promise.all([
    getJSON(`${apiUrl}/health`),
    getJSON(`${apiUrl}/state`),
    getJSON(`${apiUrl}/accounts`),
  ]);
  return {
    connected: true,
    api_url: apiUrl,
    health,
    state,
    accounts: accountsPayload.accounts || [],
    status: latestStatus,
    updated_at: Date.now() / 1000,
  };
}

async function getLivePayload() {
  try {
    const proxied = await getJSON("/api/live-session");
    if (proxied.connected || proxied.health || proxied.state) return proxied;
  } catch (_error) {
    // Older dashboard servers do not have the proxy endpoint; use bridge CORS.
  }
  return directLivePayload();
}

function renderLiveAccounts(accounts) {
  const rows = [...(accounts || [])].sort((left, right) => Number(right.profit_loss || 0) - Number(left.profit_loss || 0));
  if (!rows.length) {
    els.liveAccountsBody.innerHTML = `<tr><td class="empty" colspan="10">No live accounts yet.</td></tr>`;
    return;
  }
  els.liveAccountsBody.innerHTML = rows.map((row) => `
    <tr>
      <td>${row.owner || row.user || "--"}</td>
      <td class="${pnlClass(row.profit_loss)}">${fmt(row.profit_loss)}</td>
      <td>${fmt(row.equity)}</td>
      <td>${fmt(row.inventory, 3)}</td>
      <td class="${pnlClass(row.realized_pnl)}">${fmt(row.realized_pnl)}</td>
      <td class="${pnlClass(row.unrealized_pnl)}">${fmt(row.unrealized_pnl)}</td>
      <td class="negative">${fmt(row.max_drawdown)}</td>
      <td>${row.orders ?? "--"}</td>
      <td>${row.fills ?? "--"}</td>
      <td>${fmtAge(row.last_active_seconds_ago)}</td>
    </tr>
  `).join("");
}

function renderLiveOrders(orders) {
  const rows = [...(orders || [])].slice(0, 8);
  if (!rows.length) {
    els.liveOrdersBody.innerHTML = `<tr><td class="empty" colspan="5">No open orders.</td></tr>`;
    return;
  }
  els.liveOrdersBody.innerHTML = rows.map((row) => `
    <tr>
      <td>${row.owner || row.user || "--"}</td>
      <td class="${row.side === "buy" ? "positive" : "negative"}">${row.side || "--"}</td>
      <td>${fmt(row.remaining_quantity ?? row.quantity, 3)}</td>
      <td>${fmt(row.price, 4)}</td>
      <td>${row.status || "--"}</td>
    </tr>
  `).join("");
}

function renderLiveTrades(trades) {
  const rows = [...(trades || [])].sort((left, right) => Number(right.timestamp || 0) - Number(left.timestamp || 0)).slice(0, 10);
  if (!rows.length) {
    els.liveTradesBody.innerHTML = `<tr><td class="empty" colspan="5">No recent trades.</td></tr>`;
    return;
  }
  els.liveTradesBody.innerHTML = rows.map((row) => `
    <tr>
      <td>${fmtUnix(row.timestamp)}</td>
      <td>${row.owner || row.user || "--"}</td>
      <td class="${row.side === "buy" ? "positive" : "negative"}">${row.side || "--"}</td>
      <td>${fmt(row.quantity, 3)}</td>
      <td>${fmt(row.price, 4)}</td>
    </tr>
  `).join("");
}

function renderLive(payload) {
  const state = payload.state || {};
  const accounts = state.api_users || payload.accounts || [];
  const health = payload.health || {};
  els.liveTitle.textContent = state.symbol ? `${state.symbol} live paper match` : "Live paper match";
  els.liveState.textContent = health.last_quote_error ? "Quote Error" : "Connected";
  els.liveState.className = `live-state ${health.last_quote_error ? "error" : "connected"}`;
  els.liveSymbol.textContent = state.symbol || "--";
  els.livePrice.textContent = state.last_price ? `${fmt(state.last_price, 4)} / ${fmt(state.mid_price, 4)}` : "--";
  els.liveBook.textContent = state.best_bid ? `${fmt(state.best_bid, 4)} / ${fmt(state.best_ask, 4)}` : "--";
  els.liveSpread.textContent = fmt(state.spread, 4);
  els.liveQuoteAge.textContent = fmtAge(state.quote_age_seconds);
  els.liveBridge.textContent = payload.api_url || bridgeApiBase();
  renderLiveAccounts(accounts);
  renderLiveOrders(state.open_orders || []);
  renderLiveTrades(state.trades || []);
}

async function refreshLive() {
  if (latestStatus && latestStatus.phase !== "running_session") {
    liveUnavailable("Waiting for the next live session");
    return;
  }
  try {
    const payload = await getLivePayload();
    if (!payload.connected || !payload.state) {
      liveUnavailable("Waiting for active bridge");
      return;
    }
    renderLive(payload);
  } catch (_error) {
    liveUnavailable("Waiting for active bridge");
  }
}

function showStatus(status) {
  selectedPath = null;
  els.reportType.textContent = "Live Status";
  els.reportTitle.textContent = "Latest Summary";
  renderSymbols(status.symbols || []);
  renderSummary(status.summary || {});
}

async function loadReport(path) {
  const report = await getJSON(`/api/report?path=${encodeURIComponent(path)}`);
  selectedPath = path;
  els.reportType.textContent = report.report_type === "full_day" ? "Full Day Report" : "30 Minute Report";
  els.reportTitle.textContent = report.stage || report.day || "Report";
  renderSymbols(report.symbols || []);
  renderSummary(report.summary || {});
  document.querySelectorAll(".report-row").forEach((row) => {
    row.classList.toggle("active", row.dataset.path === selectedPath);
  });
}

function reportKindLabel(kind) {
  return kind === "full_day" ? "Full day" : "30 min";
}

function renderReports(reports) {
  const filtered = reports.filter((report) => reportFilter === "all" || report.kind === reportFilter);
  if (!filtered.length) {
    els.reportsList.innerHTML = `<div class="empty">No reports yet.</div>`;
    return;
  }
  els.reportsList.innerHTML = "";
  for (const report of filtered) {
    const row = document.createElement("div");
    row.className = "report-row";
    row.dataset.path = report.path;
    row.innerHTML = `<strong>${report.name}</strong><span>${reportKindLabel(report.kind)} · ${new Date(report.updated_at * 1000).toLocaleString()}</span>`;
    row.addEventListener("click", () => loadReport(report.path).catch(showError));
    els.reportsList.appendChild(row);
  }
}

function showError(error) {
  els.message.textContent = error.message || String(error);
  els.phase.textContent = "ui error";
  els.phase.classList.add("error");
}

async function refresh() {
  const [status, reports] = await Promise.all([
    getJSON("/api/status"),
    getJSON("/api/reports"),
  ]);
  setStatus(status);
  renderReports(reports.reports || []);
  if (!selectedPath) showStatus(status);
  refreshLive();
}

els.refreshButton.addEventListener("click", () => refresh().catch(showError));
els.liveRefreshButton.addEventListener("click", () => refreshLive());
els.tabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    reportFilter = tab.dataset.kind;
    els.tabs.forEach((item) => item.classList.toggle("active", item === tab));
    refresh().catch(showError);
  });
});

refresh().catch(showError);
window.setInterval(() => refresh().catch(showError), 15000);
window.setInterval(() => refreshLive(), 3000);
