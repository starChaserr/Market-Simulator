const els = {
  connectionState: document.querySelector("#connectionState"),
  toggleRun: document.querySelector("#toggleRun"),
  resetMarket: document.querySelector("#resetMarket"),
  scenarioSelect: document.querySelector("#scenarioSelect"),
  seedInput: document.querySelector("#seedInput"),
  refreshSelect: document.querySelector("#refreshSelect"),
  chaosLevel: document.querySelector("#chaosLevel"),
  chaosReadout: document.querySelector("#chaosReadout"),
  chaosSource: document.querySelector("#chaosSource"),
  applyChaos: document.querySelector("#applyChaos"),
  applyRegime: document.querySelector("#applyRegime"),
  lastPrice: document.querySelector("#lastPrice"),
  bidAsk: document.querySelector("#bidAsk"),
  spread: document.querySelector("#spread"),
  microprice: document.querySelector("#microprice"),
  realizedVol: document.querySelector("#realizedVol"),
  liquidityStress: document.querySelector("#liquidityStress"),
  orderFlow: document.querySelector("#orderFlow"),
  volume: document.querySelector("#volume"),
  symbolTitle: document.querySelector("#symbolTitle"),
  chartSubhead: document.querySelector("#chartSubhead"),
  tickValue: document.querySelector("#tickValue"),
  scenarioValue: document.querySelector("#scenarioValue"),
  priceCanvas: document.querySelector("#priceCanvas"),
  bookCanvas: document.querySelector("#bookCanvas"),
  depthImbalance: document.querySelector("#depthImbalance"),
  orderForm: document.querySelector("#orderForm"),
  userName: document.querySelector("#userName"),
  quantity: document.querySelector("#quantity"),
  orderType: document.querySelector("#orderType"),
  limitPrice: document.querySelector("#limitPrice"),
  stopPrice: document.querySelector("#stopPrice"),
  timeInForce: document.querySelector("#timeInForce"),
  postOnly: document.querySelector("#postOnly"),
  buyButton: document.querySelector("#buyButton"),
  sellButton: document.querySelector("#sellButton"),
  fundingAmount: document.querySelector("#fundingAmount"),
  fundButton: document.querySelector("#fundButton"),
  orderResult: document.querySelector("#orderResult"),
  tradesTable: document.querySelector("#tradesTable"),
  accountsTable: document.querySelector("#accountsTable"),
  accountCount: document.querySelector("#accountCount"),
  agentsTable: document.querySelector("#agentsTable"),
  agentCounts: document.querySelector("#agentCounts"),
  ordersTable: document.querySelector("#ordersTable"),
  eventsList: document.querySelector("#eventsList"),
};

const dashboardHeaders = { "X-API-User": "Dashboard UI" };
const fallbackScenarios = [
  "default",
  "calm",
  "high_volatility",
  "trending_up",
  "trending_down",
  "flash_crash",
  "liquidity_drought",
  "mean_reverting",
  "news_shock",
];

let currentState = null;
let refreshTimer = null;
let refreshMs = Number(els.refreshSelect.value);
let lastDrawState = null;

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function num(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function compact(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toLocaleString(undefined, {
    notation: "compact",
    maximumFractionDigits: digits,
  });
}

function pct(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return `${(Number(value) * 100).toFixed(digits)}%`;
}

function signed(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  const number = Number(value);
  return `${number > 0 ? "+" : ""}${num(number, digits)}`;
}

function classForNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || Math.abs(number) < 1e-9) return "flat";
  return number > 0 ? "positive" : "negative";
}

async function request(path, options = {}) {
  const headers = { ...dashboardHeaders, ...(options.headers || {}) };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { cache: "no-store", ...options, headers });
  const raw = await response.text();
  let payload = {};
  if (raw) {
    try {
      payload = JSON.parse(raw);
    } catch (_error) {
      payload = { raw };
    }
  }
  if (!response.ok) {
    throw new Error(payload.error || `Request failed with status ${response.status}`);
  }
  return payload;
}

function setConnection(online, running = true) {
  els.connectionState.classList.toggle("offline", !online);
  els.connectionState.classList.toggle("paused", online && !running);
  els.connectionState.textContent = online ? (running ? "Live" : "Paused") : "Offline";
}

function setResult(message, isError = false) {
  els.orderResult.textContent = message;
  els.orderResult.classList.toggle("negative", isError);
}

function fillScenarioSelect(scenarios, selected) {
  const current = selected || els.scenarioSelect.value || "default";
  els.scenarioSelect.innerHTML = "";
  for (const scenario of scenarios) {
    const option = document.createElement("option");
    option.value = scenario;
    option.textContent = scenario.replaceAll("_", " ");
    els.scenarioSelect.appendChild(option);
  }
  if (scenarios.includes(current)) {
    els.scenarioSelect.value = current;
  }
}

async function loadConfig() {
  try {
    const config = await request("/api/config");
    fillScenarioSelect(config.scenarios || fallbackScenarios, config.scenario);
    els.seedInput.value = config.seed ?? "";
    updateChaosControls(config.chaos);
  } catch (_error) {
    fillScenarioSelect(fallbackScenarios, "default");
  }
}

async function refresh() {
  try {
    const state = await request("/api/state");
    currentState = state;
    render(state);
    setConnection(true, state.running);
  } catch (error) {
    setConnection(false);
    setResult(error.message, true);
  }
}

function render(state) {
  lastDrawState = state;
  els.lastPrice.textContent = num(state.last_price, 2);
  els.bidAsk.textContent = `${num(state.best_bid, 2)} / ${num(state.best_ask, 2)}`;
  els.spread.textContent = num(state.spread, 2);
  els.microprice.textContent = num(state.microprice, 2);
  els.realizedVol.textContent = pct(state.realized_volatility, 3);
  els.liquidityStress.textContent = num(state.liquidity_stress, 2);
  els.orderFlow.textContent = signed(state.order_flow_imbalance, 3);
  els.volume.textContent = compact(state.total_volume, 2);

  els.symbolTitle.textContent = state.symbol || "SIM";
  els.chartSubhead.textContent = `mark ${num(state.mark_price, 2)}  high ${num(state.session_high, 2)}  low ${num(state.session_low, 2)}`;
  els.tickValue.textContent = `Tick ${state.tick ?? "--"}`;
  els.scenarioValue.textContent = `${state.scenario || "default"}${state.seed === null || state.seed === undefined ? "" : ` #${state.seed}`}`;
  updateChaosControls(state.chaos);
  els.toggleRun.textContent = state.running ? "Pause" : "Resume";
  els.depthImbalance.textContent = `imbalance ${signed(state.depth_imbalance, 3)}`;
  if (state.scenario && document.activeElement !== els.scenarioSelect) {
    els.scenarioSelect.value = state.scenario;
  }
  if (document.activeElement !== els.seedInput) {
    els.seedInput.value = state.seed ?? "";
  }
  setOrderPlaceholders(state);

  drawPriceChart(state);
  drawDepth(state);
  renderTrades(state.trades || []);
  renderAccounts(state.api_users || []);
  renderAgents(state.agents || [], state.agent_counts || {});
  renderOrders(state.open_orders || []);
  renderEvents(state.events || []);
}

function updateChaosControls(chaos) {
  if (!chaos) return;
  const level = Math.round(Number(chaos.level) || 0);
  if (document.activeElement !== els.chaosLevel) {
    els.chaosLevel.value = String(level);
  }
  els.chaosReadout.value = String(level);
  els.chaosReadout.textContent = String(level);
  els.chaosSource.textContent = `${chaos.profile || "controlled"} · ${chaos.source || "manual"}`;
}

function setOrderPlaceholders(state) {
  const tick = Number(state.tick_size || 0.01);
  const bid = Number(state.best_bid || state.last_price || 0);
  const ask = Number(state.best_ask || state.last_price || 0);
  const mid = Number(state.mid_price || state.last_price || 0);
  els.limitPrice.placeholder = mid ? `limit near ${num(mid, tick >= 1 ? 0 : 2)}` : "price";
  els.stopPrice.placeholder = bid && ask ? `trigger ${num((bid + ask) / 2, 2)}` : "trigger";
}

function renderTrades(trades) {
  const rows = trades.slice(0, 24).map((trade) => `
    <tr>
      <td class="side-${esc(trade.aggressor_side)}">${esc(trade.aggressor_side)}</td>
      <td>${num(trade.price, 2)}</td>
      <td>${num(trade.quantity, 4)}</td>
      <td>${esc(trade.buyer)}</td>
      <td>${esc(trade.seller)}</td>
    </tr>
  `);
  els.tradesTable.innerHTML = rows.join("") || emptyRow(5, "No trades yet");
}

function renderAccounts(accounts) {
  els.accountCount.textContent = `${accounts.length} active`;
  const rows = accounts.slice(0, 20).map((account) => `
    <tr>
      <td>${esc(account.owner)}</td>
      <td>${num(account.equity, 2)}</td>
      <td class="${classForNumber(account.profit_loss)}">${signed(account.profit_loss, 2)}</td>
      <td>${num(account.inventory, 4)}</td>
      <td>${num(account.reserved_buying_power, 2)}</td>
      <td>${account.orders ?? 0}</td>
    </tr>
  `);
  els.accountsTable.innerHTML = rows.join("") || emptyRow(6, "No API accounts yet");
}

function renderAgents(agents, counts) {
  els.agentCounts.textContent = Object.entries(counts).map(([type, count]) => `${type}: ${count}`).join("  ") || "--";
  const rows = agents.slice(0, 24).map((agent) => {
    const extra = agent.extra || {};
    return `
      <tr>
        <td>${esc(agent.owner)}</td>
        <td>${esc(agent.agent_type)}</td>
        <td>${esc(extra.last_action || "hold")}</td>
        <td class="${classForNumber(agent.profit_loss)}">${signed(agent.profit_loss, 2)}</td>
        <td>${num(agent.inventory, 2)}</td>
      </tr>
    `;
  });
  els.agentsTable.innerHTML = rows.join("") || emptyRow(5, "No background agents");
}

function renderOrders(orders) {
  const rows = orders.slice(0, 30).map((order) => `
    <tr>
      <td class="side-${esc(order.side)}">${esc(order.side)}</td>
      <td>${esc(order.order_type)}</td>
      <td>${num(order.price, 2)}</td>
      <td>${num(order.remaining_quantity, 4)}</td>
      <td>${esc(order.owner)}</td>
      <td><button class="mini-button" type="button" data-cancel-order="${esc(order.id)}" data-owner="${esc(order.owner)}">Cancel</button></td>
    </tr>
  `);
  els.ordersTable.innerHTML = rows.join("") || emptyRow(6, "No open API orders");
}

function renderEvents(events) {
  const rows = events.slice(-12).reverse().map((event) => {
    const payload = event.payload || {};
    const detail = payload.name
      || payload.reject_reason
      || payload.status
      || payload.side
      || payload.owner
      || "";
    return `
      <div class="event-item">
        <strong>${esc(event.type || "event")}</strong>
        <span>tick ${esc(payload.tick ?? "")} ${esc(detail)}</span>
      </div>
    `;
  });
  els.eventsList.innerHTML = rows.join("") || '<div class="event-item"><strong>Waiting</strong><span>No events yet</span></div>';
}

function emptyRow(colspan, message) {
  return `<tr><td colspan="${colspan}" class="flat">${esc(message)}</td></tr>`;
}

function fitCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(1, Math.floor(rect.width));
  const height = Math.max(1, Math.floor(rect.height));
  const backingWidth = Math.max(1, Math.floor(width * dpr));
  const backingHeight = Math.max(1, Math.floor(height * dpr));
  if (canvas.width !== backingWidth || canvas.height !== backingHeight) {
    canvas.width = backingWidth;
    canvas.height = backingHeight;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, width, height };
}

function drawPriceChart(state) {
  const { ctx, width, height } = fitCanvas(els.priceCanvas);
  ctx.clearRect(0, 0, width, height);
  const history = (state.history || []).slice(-150);
  drawPanelBackground(ctx, width, height);

  if (history.length < 2) {
    drawCenteredText(ctx, width, height, "Waiting for price history");
    return;
  }

  const pad = { left: 54, right: 14, top: 18, bottom: 30 };
  const chartWidth = width - pad.left - pad.right;
  const chartHeight = height - pad.top - pad.bottom;
  const highs = history.map((item) => Number(item.high ?? item.close ?? item.last));
  const lows = history.map((item) => Number(item.low ?? item.close ?? item.last));
  let maxPrice = Math.max(...highs, Number(state.fundamental_price || 0), Number(state.mark_price || 0));
  let minPrice = Math.min(...lows, Number(state.fundamental_price || Infinity), Number(state.mark_price || Infinity));
  if (!Number.isFinite(maxPrice) || !Number.isFinite(minPrice) || maxPrice <= minPrice) {
    maxPrice = Number(state.last_price || 1) + 1;
    minPrice = Number(state.last_price || 1) - 1;
  }
  const padding = Math.max((maxPrice - minPrice) * 0.08, 0.01);
  maxPrice += padding;
  minPrice -= padding;
  const yFor = (price) => pad.top + (maxPrice - price) / (maxPrice - minPrice) * chartHeight;
  const xFor = (index) => pad.left + index / Math.max(1, history.length - 1) * chartWidth;

  drawGrid(ctx, pad, chartWidth, chartHeight, minPrice, maxPrice, yFor);
  drawReferenceLine(ctx, history, xFor, yFor, "mark", "#2563eb");
  drawReferenceLine(ctx, history, xFor, yFor, "fundamental", "#6d4aff");

  const candleWidth = Math.max(3, Math.min(9, chartWidth / history.length * 0.56));
  history.forEach((bar, index) => {
    const open = Number(bar.open ?? bar.close ?? bar.last);
    const close = Number(bar.close ?? bar.last ?? open);
    const high = Number(bar.high ?? Math.max(open, close));
    const low = Number(bar.low ?? Math.min(open, close));
    const x = xFor(index);
    const up = close >= open;
    ctx.strokeStyle = up ? "#087f5b" : "#c92a2a";
    ctx.fillStyle = up ? "rgba(8, 127, 91, 0.72)" : "rgba(201, 42, 42, 0.72)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, yFor(high));
    ctx.lineTo(x, yFor(low));
    ctx.stroke();
    const top = yFor(Math.max(open, close));
    const bodyHeight = Math.max(2, Math.abs(yFor(open) - yFor(close)));
    ctx.fillRect(x - candleWidth / 2, top, candleWidth, bodyHeight);
  });

  drawLastPriceLabel(ctx, width, yFor(Number(state.last_price)), state.last_price);
}

function drawPanelBackground(ctx, width, height) {
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, width, height);
}

function drawGrid(ctx, pad, chartWidth, chartHeight, minPrice, maxPrice, yFor) {
  ctx.strokeStyle = "#e5e9ed";
  ctx.fillStyle = "#65717b";
  ctx.lineWidth = 1;
  ctx.font = "11px system-ui, sans-serif";
  for (let i = 0; i <= 4; i += 1) {
    const price = minPrice + (maxPrice - minPrice) * i / 4;
    const y = yFor(price);
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(pad.left + chartWidth, y);
    ctx.stroke();
    ctx.fillText(num(price, 2), 8, y + 4);
  }
  ctx.strokeStyle = "#aeb8c1";
  ctx.strokeRect(pad.left, pad.top, chartWidth, chartHeight);
}

function drawReferenceLine(ctx, history, xFor, yFor, key, color) {
  const points = history
    .map((item, index) => ({ x: xFor(index), y: yFor(Number(item[key])), valid: Number.isFinite(Number(item[key])) }))
    .filter((point) => point.valid);
  if (points.length < 2) return;
  ctx.strokeStyle = color;
  ctx.globalAlpha = 0.72;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  points.forEach((point, index) => {
    if (index === 0) ctx.moveTo(point.x, point.y);
    else ctx.lineTo(point.x, point.y);
  });
  ctx.stroke();
  ctx.globalAlpha = 1;
}

function drawLastPriceLabel(ctx, width, y, price) {
  if (!Number.isFinite(y)) return;
  const label = num(price, 2);
  ctx.strokeStyle = "#172026";
  ctx.fillStyle = "#172026";
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(54, y);
  ctx.lineTo(width - 14, y);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#172026";
  ctx.fillRect(width - 82, y - 12, 68, 24);
  ctx.fillStyle = "#ffffff";
  ctx.font = "11px system-ui, sans-serif";
  ctx.fillText(label, width - 76, y + 4);
}

function drawCenteredText(ctx, width, height, text) {
  ctx.fillStyle = "#65717b";
  ctx.font = "13px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.fillText(text, width / 2, height / 2);
  ctx.textAlign = "left";
}

function drawDepth(state) {
  const { ctx, width, height } = fitCanvas(els.bookCanvas);
  ctx.clearRect(0, 0, width, height);
  drawPanelBackground(ctx, width, height);

  const bids = (state.order_book?.bids || []).slice(0, 14);
  const asks = (state.order_book?.asks || []).slice(0, 14);
  if (!bids.length && !asks.length) {
    drawCenteredText(ctx, width, height, "No depth");
    return;
  }

  const pad = { left: 44, right: 44, top: 20, bottom: 26 };
  const chartWidth = width - pad.left - pad.right;
  const chartHeight = height - pad.top - pad.bottom;
  const center = pad.left + chartWidth / 2;
  const maxCum = Math.max(
    ...bids.map((level) => Number(level.cumulative || level.quantity || 0)),
    ...asks.map((level) => Number(level.cumulative || level.quantity || 0)),
    1,
  );
  const rowHeight = chartHeight / Math.max(bids.length, asks.length, 1);

  ctx.strokeStyle = "#e5e9ed";
  ctx.beginPath();
  ctx.moveTo(center, pad.top);
  ctx.lineTo(center, pad.top + chartHeight);
  ctx.stroke();

  drawDepthSide(ctx, bids, center, pad.top, rowHeight, chartWidth / 2 - 8, maxCum, -1, "#087f5b");
  drawDepthSide(ctx, asks, center, pad.top, rowHeight, chartWidth / 2 - 8, maxCum, 1, "#c92a2a");

  ctx.fillStyle = "#65717b";
  ctx.font = "11px system-ui, sans-serif";
  ctx.fillText("bid", pad.left, height - 8);
  ctx.fillText("ask", center + 8, height - 8);
}

function drawDepthSide(ctx, levels, center, top, rowHeight, maxWidth, maxCum, direction, color) {
  ctx.font = "11px system-ui, sans-serif";
  levels.forEach((level, index) => {
    const cumulative = Number(level.cumulative || level.quantity || 0);
    const width = Math.max(1, cumulative / maxCum * maxWidth);
    const y = top + index * rowHeight + 3;
    const x = direction < 0 ? center - width : center;
    ctx.fillStyle = color === "#087f5b" ? "rgba(8, 127, 91, 0.16)" : "rgba(201, 42, 42, 0.16)";
    ctx.fillRect(x, y, width, Math.max(2, rowHeight - 6));
    ctx.fillStyle = color;
    const priceText = num(level.price, 2);
    const qtyText = compact(level.quantity, 1);
    if (direction < 0) {
      ctx.textAlign = "right";
      ctx.fillText(priceText, center - 8, y + rowHeight / 2 + 2);
      ctx.fillStyle = "#65717b";
      ctx.fillText(qtyText, Math.max(42, center - width - 6), y + rowHeight / 2 + 2);
    } else {
      ctx.textAlign = "left";
      ctx.fillText(priceText, center + 8, y + rowHeight / 2 + 2);
      ctx.fillStyle = "#65717b";
      ctx.fillText(qtyText, Math.min(center + width + 6, center + maxWidth - 30), y + rowHeight / 2 + 2);
    }
  });
  ctx.textAlign = "left";
}

function updateOrderInputs() {
  const type = els.orderType.value;
  const needsLimit = type === "limit" || type === "stop_limit";
  const needsStop = type === "stop" || type === "stop_limit";
  els.limitPrice.disabled = !needsLimit;
  els.stopPrice.disabled = !needsStop;
  els.postOnly.disabled = !needsLimit;
  if (!needsLimit) {
    els.limitPrice.value = "";
    els.postOnly.checked = false;
  }
  if (!needsStop) els.stopPrice.value = "";
}

async function submitOrder(side) {
  try {
    const type = els.orderType.value;
    const payload = {
      user: els.userName.value.trim() || "Dashboard User",
      side,
      quantity: Number(els.quantity.value),
      order_type: type,
      time_in_force: els.timeInForce.value,
      post_only: els.postOnly.checked,
    };
    if (type === "limit" || type === "stop_limit") payload.price = Number(els.limitPrice.value);
    if (type === "stop" || type === "stop_limit") payload.stop_price = Number(els.stopPrice.value);
    const result = await request("/api/order", { method: "POST", body: JSON.stringify(payload) });
    setResult(`${result.status}: ${side} ${num(result.filled_quantity, 4)} filled, ${num(result.remaining_quantity, 4)} remaining at ${num(result.average_price, 2)}`);
    await refresh();
  } catch (error) {
    setResult(error.message, true);
  }
}

async function fundUser() {
  try {
    const payload = {
      user: els.userName.value.trim() || "Dashboard User",
      amount: Number(els.fundingAmount.value),
    };
    const result = await request("/api/accounts/fund", { method: "POST", body: JSON.stringify(payload) });
    setResult(`${result.owner} funded. Buying power ${num(result.buying_power, 2)}`);
    await refresh();
  } catch (error) {
    setResult(error.message, true);
  }
}

async function toggleRunning() {
  if (!currentState) return;
  try {
    await request("/api/simulation", { method: "POST", body: JSON.stringify({ running: !currentState.running }) });
    await refresh();
  } catch (error) {
    setResult(error.message, true);
  }
}

async function resetMarket() {
  try {
    await request("/api/reset", { method: "POST", body: JSON.stringify({}) });
    setResult("Market reset");
    await refresh();
  } catch (error) {
    setResult(error.message, true);
  }
}

async function applyRegime() {
  try {
    const payload = {
      scenario: els.scenarioSelect.value,
      seed: els.seedInput.value.trim() === "" ? null : Number(els.seedInput.value),
    };
    await request("/api/regime", { method: "POST", body: JSON.stringify(payload) });
    setResult(`Regime applied: ${payload.scenario}${payload.seed === null ? "" : ` #${payload.seed}`}`);
    await loadConfig();
    await refresh();
  } catch (error) {
    setResult(error.message, true);
  }
}

async function applyChaos() {
  try {
    const level = Number(els.chaosLevel.value);
    const result = await request("/api/chaos", {
      method: "POST",
      body: JSON.stringify({ level, source: "dashboard override" }),
    });
    updateChaosControls(result.chaos);
    setResult(`Chaos set to ${level}. The controller may adjust it on its next cycle.`);
  } catch (error) {
    setResult(error.message, true);
  }
}

async function cancelOrder(orderId, owner) {
  try {
    await request("/api/cancel", { method: "POST", body: JSON.stringify({ order_id: orderId, user: owner }) });
    setResult(`Canceled ${orderId}`);
    await refresh();
  } catch (error) {
    setResult(error.message, true);
  }
}

function setRefreshCadence() {
  refreshMs = Number(els.refreshSelect.value) || 1000;
  if (refreshTimer) window.clearInterval(refreshTimer);
  refreshTimer = window.setInterval(refresh, refreshMs);
}

els.orderType.addEventListener("change", updateOrderInputs);
els.buyButton.addEventListener("click", () => submitOrder("buy"));
els.sellButton.addEventListener("click", () => submitOrder("sell"));
els.fundButton.addEventListener("click", fundUser);
els.toggleRun.addEventListener("click", toggleRunning);
els.resetMarket.addEventListener("click", resetMarket);
els.applyRegime.addEventListener("click", applyRegime);
els.applyChaos.addEventListener("click", applyChaos);
els.chaosLevel.addEventListener("input", () => {
  els.chaosReadout.value = els.chaosLevel.value;
  els.chaosReadout.textContent = els.chaosLevel.value;
});
els.refreshSelect.addEventListener("change", setRefreshCadence);
els.ordersTable.addEventListener("click", (event) => {
  const button = event.target.closest("[data-cancel-order]");
  if (!button) return;
  cancelOrder(button.dataset.cancelOrder, button.dataset.owner);
});
window.addEventListener("resize", () => {
  if (lastDrawState) {
    drawPriceChart(lastDrawState);
    drawDepth(lastDrawState);
  }
});

updateOrderInputs();
loadConfig().then(refresh);
setRefreshCadence();
