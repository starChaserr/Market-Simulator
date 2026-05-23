const els = {
  symbolName: document.querySelector("#symbolName"),
  instrumentKey: document.querySelector("#instrumentKey"),
  feedStatus: document.querySelector("#feedStatus"),
  refreshSelect: document.querySelector("#refreshSelect"),
  refreshApply: document.querySelector("#refreshApply"),
  resetButton: document.querySelector("#resetButton"),
  lastPrice: document.querySelector("#lastPrice"),
  bidAsk: document.querySelector("#bidAsk"),
  spread: document.querySelector("#spread"),
  volume: document.querySelector("#volume"),
  quoteAge: document.querySelector("#quoteAge"),
  chartMeta: document.querySelector("#chartMeta"),
  sessionLow: document.querySelector("#sessionLow"),
  sessionHigh: document.querySelector("#sessionHigh"),
  priceCanvas: document.querySelector("#priceCanvas"),
  depthCanvas: document.querySelector("#depthCanvas"),
  userName: document.querySelector("#userName"),
  quantity: document.querySelector("#quantity"),
  price: document.querySelector("#price"),
  orderType: document.querySelector("#orderType"),
  timeInForce: document.querySelector("#timeInForce"),
  stopPriceWrap: document.querySelector("#stopPriceWrap"),
  stopPrice: document.querySelector("#stopPrice"),
  postOnly: document.querySelector("#postOnly"),
  buyButton: document.querySelector("#buyButton"),
  sellButton: document.querySelector("#sellButton"),
  fundAmount: document.querySelector("#fundAmount"),
  fundButton: document.querySelector("#fundButton"),
  orderResult: document.querySelector("#orderResult"),
  accountsBody: document.querySelector("#accountsBody"),
  ordersBody: document.querySelector("#ordersBody"),
  tradesBody: document.querySelector("#tradesBody"),
  eventsList: document.querySelector("#eventsList"),
};

const headers = { "Content-Type": "application/json", "X-API-User": "Paper Dashboard" };
let refreshMs = 1000;
let refreshTimer = null;
let currentState = null;

function inr(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    currencyDisplay: "narrowSymbol",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(Number(value));
}

function num(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function compact(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toLocaleString("en-IN", { notation: "compact", maximumFractionDigits: 2 });
}

function signedClass(value) {
  const number = Number(value);
  if (number > 0) return "positive";
  if (number < 0) return "negative";
  return "";
}

function sideClass(side) {
  return side === "buy" ? "side-buy" : "side-sell";
}

function setStatus(text, online) {
  els.feedStatus.textContent = text;
  els.feedStatus.classList.toggle("offline", !online);
}

async function request(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `${path} failed with ${response.status}`);
  return payload;
}

async function loadRefresh() {
  const payload = await request("/api/chart-refresh");
  const nextMs = Number(payload.chart_refresh_ms || payload.chart_refresh_interval * 1000 || 1000);
  setRefresh(nextMs);
}

function setRefresh(nextMs) {
  refreshMs = Math.max(500, Math.min(Number(nextMs) || 1000, 60000));
  ensureRefreshOption(refreshMs);
  els.refreshSelect.value = String(refreshMs);
  if (refreshTimer) window.clearInterval(refreshTimer);
  refreshTimer = window.setInterval(refresh, refreshMs);
}

function ensureRefreshOption(value) {
  const text = value >= 1000 ? `${num(value / 1000, value % 1000 === 0 ? 0 : 1)}s` : `${value}ms`;
  if ([...els.refreshSelect.options].some((option) => option.value === String(value))) return;
  const option = document.createElement("option");
  option.value = String(value);
  option.textContent = text;
  els.refreshSelect.appendChild(option);
}

async function applyRefresh() {
  const value = Number(els.refreshSelect.value);
  const payload = await request("/api/chart-refresh", {
    method: "POST",
    headers,
    body: JSON.stringify({ chart_refresh_ms: value }),
  });
  setRefresh(Number(payload.chart_refresh_ms));
  els.orderResult.textContent = `Feed refresh set to ${num(Number(payload.chart_refresh_ms) / 1000, 1)}s`;
}

async function refresh() {
  try {
    const state = await request("/api/state");
    currentState = state;
    render(state);
    setStatus("Live Quote", true);
  } catch (error) {
    setStatus("Offline", false);
    els.orderResult.textContent = error.message;
  }
}

function render(state) {
  els.symbolName.textContent = state.symbol || "--";
  els.instrumentKey.textContent = state.instrument_key || "--";
  els.lastPrice.textContent = inr(state.last_price);
  els.bidAsk.textContent = `${inr(state.best_bid)} / ${inr(state.best_ask)}`;
  els.spread.textContent = inr(state.spread, 4);
  els.volume.textContent = compact(state.total_volume);
  els.quoteAge.textContent = `${num(state.quote_age_seconds || 0, 1)}s`;
  els.chartMeta.textContent = `Mark ${inr(state.mark_price)} | Fundamental proxy ${inr(state.fundamental_price)} | Vol ${num((state.volatility || 0) * 100, 2)}%`;
  els.sessionLow.textContent = `Low ${inr(state.session_low)}`;
  els.sessionHigh.textContent = `High ${inr(state.session_high)}`;
  renderAccounts(state.api_users || []);
  renderOrders(state.open_orders || []);
  renderTrades(state.trades || []);
  renderEvents(state.events || []);
  drawPriceChart(state.history || []);
  drawDepth(state.order_book || { bids: [], asks: [] });
}

function appendCell(row, text, className = "") {
  const cell = document.createElement("td");
  cell.textContent = text;
  if (className) cell.className = className;
  row.appendChild(cell);
  return cell;
}

function renderAccounts(accounts) {
  els.accountsBody.replaceChildren();
  if (!accounts.length) {
    const row = document.createElement("tr");
    const cell = appendCell(row, "No paper accounts yet. Start an agent or fund a dashboard user.");
    cell.colSpan = 7;
    els.accountsBody.appendChild(row);
    return;
  }
  for (const account of accounts.slice(0, 30)) {
    const row = document.createElement("tr");
    appendCell(row, account.owner || account.user || "--");
    appendCell(row, inr(account.profit_loss), `num ${signedClass(account.profit_loss)}`);
    appendCell(row, inr(account.equity), "num");
    appendCell(row, inr(account.max_drawdown), "num negative");
    appendCell(row, num(account.inventory, 2), "num");
    appendCell(row, String(account.orders || 0), "num");
    appendCell(row, String(account.fills || 0), "num");
    els.accountsBody.appendChild(row);
  }
}

function renderOrders(orders) {
  els.ordersBody.replaceChildren();
  if (!orders.length) {
    const row = document.createElement("tr");
    const cell = appendCell(row, "No open paper orders.");
    cell.colSpan = 6;
    els.ordersBody.appendChild(row);
    return;
  }
  for (const order of orders.slice(0, 40)) {
    const row = document.createElement("tr");
    appendCell(row, order.side || "--", sideClass(order.side));
    appendCell(row, order.owner || order.user || "--");
    appendCell(row, num(order.remaining_quantity ?? order.quantity, 2), "num");
    appendCell(row, order.price ? inr(order.price) : "--", "num");
    appendCell(row, order.status || "--");
    const action = document.createElement("td");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "cancel-button";
    button.textContent = "Cancel";
    button.addEventListener("click", () => cancelOrder(order));
    action.appendChild(button);
    row.appendChild(action);
    els.ordersBody.appendChild(row);
  }
}

function renderTrades(trades) {
  els.tradesBody.replaceChildren();
  if (!trades.length) {
    const row = document.createElement("tr");
    const cell = appendCell(row, "No paper fills yet.");
    cell.colSpan = 5;
    els.tradesBody.appendChild(row);
    return;
  }
  for (const trade of trades.slice(0, 50)) {
    const row = document.createElement("tr");
    appendCell(row, trade.side || "--", sideClass(trade.side));
    appendCell(row, trade.owner || "--");
    appendCell(row, inr(trade.price), "num");
    appendCell(row, num(trade.quantity, 2), "num");
    appendCell(row, timeLabel(trade.timestamp));
    els.tradesBody.appendChild(row);
  }
}

function renderEvents(events) {
  els.eventsList.replaceChildren();
  const visible = [...events].slice(-12).reverse();
  if (!visible.length) {
    const item = document.createElement("div");
    item.className = "event-item";
    item.innerHTML = '<span class="event-time">Now</span><span class="event-message">Bridge is waiting for activity.</span>';
    els.eventsList.appendChild(item);
    return;
  }
  for (const event of visible) {
    const item = document.createElement("div");
    item.className = "event-item";
    const when = document.createElement("span");
    when.className = "event-time";
    when.textContent = `${timeLabel(event.time)} ${event.level || "info"}`;
    const message = document.createElement("span");
    message.className = "event-message";
    message.textContent = event.message || "";
    item.append(when, message);
    els.eventsList.appendChild(item);
  }
}

function timeLabel(epoch) {
  if (!epoch) return "--";
  return new Date(Number(epoch) * 1000).toLocaleTimeString("en-IN", { hour12: false });
}

function resizeCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(1, Math.floor(rect.width * dpr));
  const height = Math.max(1, Math.floor(rect.height * dpr));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, width: rect.width, height: rect.height };
}

function drawPriceChart(history) {
  const { ctx, width, height } = resizeCanvas(els.priceCanvas);
  ctx.clearRect(0, 0, width, height);
  const pad = { top: 18, right: 68, bottom: 28, left: 54 };
  const areaW = width - pad.left - pad.right;
  const areaH = height - pad.top - pad.bottom;
  ctx.strokeStyle = "#d7dde3";
  ctx.lineWidth = 1;
  ctx.strokeRect(pad.left, pad.top, areaW, areaH);

  const points = history.map((point) => Number(point.mid || point.close || point.mark)).filter((value) => Number.isFinite(value) && value > 0);
  if (points.length < 2) {
    ctx.fillStyle = "#687681";
    ctx.font = "700 14px system-ui";
    ctx.fillText("Waiting for live quote history", pad.left + 18, pad.top + 38);
    return;
  }
  const rawMin = Math.min(...points);
  const rawMax = Math.max(...points);
  const center = (rawMin + rawMax) / 2;
  const visibleRange = Math.max(rawMax - rawMin, Math.abs(center) * 0.0004, 0.05);
  const min = center - visibleRange / 2;
  const max = center + visibleRange / 2;
  const span = Math.max(max - min, 0.01);
  const xFor = (index) => pad.left + (index / Math.max(points.length - 1, 1)) * areaW;
  const yFor = (value) => pad.top + areaH - ((value - min) / span) * areaH;

  ctx.strokeStyle = "#edf1f4";
  for (let i = 1; i < 4; i += 1) {
    const y = pad.top + (areaH / 4) * i;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(pad.left + areaW, y);
    ctx.stroke();
  }

  ctx.strokeStyle = "#2458a6";
  ctx.lineWidth = 2;
  ctx.beginPath();
  points.forEach((point, index) => {
    const x = xFor(index);
    const y = yFor(point);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  const last = points[points.length - 1];
  ctx.fillStyle = last >= points[0] ? "#087f5b" : "#b4232a";
  ctx.beginPath();
  ctx.arc(xFor(points.length - 1), yFor(last), 4, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = "#687681";
  ctx.font = "700 12px system-ui";
  ctx.fillText(inr(max), pad.left + areaW + 8, pad.top + 5);
  ctx.fillText(inr(min), pad.left + areaW + 8, pad.top + areaH);
  ctx.fillText(`${points.length} ticks`, pad.left, height - 8);
}

function drawDepth(book) {
  const { ctx, width, height } = resizeCanvas(els.depthCanvas);
  ctx.clearRect(0, 0, width, height);
  const bids = (book.bids || []).slice(0, 8);
  const asks = (book.asks || []).slice(0, 8);
  const rows = Math.max(bids.length, asks.length, 1);
  const rowH = Math.max(24, (height - 34) / rows);
  const maxQty = Math.max(...bids.map((level) => Number(level.quantity || 0)), ...asks.map((level) => Number(level.quantity || 0)), 1);
  ctx.font = "700 12px system-ui";
  ctx.fillStyle = "#687681";
  ctx.fillText("Bid", 10, 18);
  ctx.fillText("Ask", width / 2 + 10, 18);
  for (let i = 0; i < rows; i += 1) {
    const y = 28 + i * rowH;
    drawLevel(ctx, bids[i], 10, y, width / 2 - 18, rowH - 5, maxQty, "#dff3ec", "#087f5b", "right");
    drawLevel(ctx, asks[i], width / 2 + 8, y, width / 2 - 18, rowH - 5, maxQty, "#fae4e6", "#b4232a", "left");
  }
}

function drawLevel(ctx, level, x, y, w, h, maxQty, fill, text, align) {
  if (!level) return;
  const qty = Number(level.quantity || 0);
  const barW = Math.max(3, (qty / maxQty) * w);
  ctx.fillStyle = fill;
  if (align === "right") ctx.fillRect(x + w - barW, y, barW, h);
  else ctx.fillRect(x, y, barW, h);
  ctx.fillStyle = text;
  ctx.textAlign = align;
  const labelX = align === "right" ? x + w - 6 : x + 6;
  ctx.fillText(`${inr(level.price)}  ${compact(qty)}`, labelX, y + h / 2 + 4);
  ctx.textAlign = "left";
}

function orderPayload(side) {
  const payload = {
    user: els.userName.value.trim() || "DashboardPaper",
    side,
    quantity: Number(els.quantity.value),
    order_type: els.orderType.value,
    time_in_force: els.timeInForce.value,
    post_only: els.postOnly.checked,
  };
  const price = Number(els.price.value);
  const stopPrice = Number(els.stopPrice.value);
  if (Number.isFinite(price) && price > 0) payload.price = price;
  if (Number.isFinite(stopPrice) && stopPrice > 0) payload.stop_price = stopPrice;
  return payload;
}

async function submitOrder(side) {
  try {
    const payload = orderPayload(side);
    const response = await request("/api/order", { method: "POST", headers, body: JSON.stringify(payload) });
    els.orderResult.textContent = `${response.side} ${response.status}: ${num(response.filled_quantity || 0, 2)} / ${num(response.quantity || 0, 2)} at ${response.average_price ? inr(response.average_price) : response.price ? inr(response.price) : "market"}`;
    await refresh();
  } catch (error) {
    els.orderResult.textContent = error.message;
  }
}

async function fundUser() {
  try {
    const payload = { user: els.userName.value.trim() || "DashboardPaper", amount: Number(els.fundAmount.value) };
    const response = await request("/api/accounts/fund", { method: "POST", headers, body: JSON.stringify(payload) });
    els.orderResult.textContent = `Funded ${response.owner}: ${inr(response.cash)} cash`;
    await refresh();
  } catch (error) {
    els.orderResult.textContent = error.message;
  }
}

async function cancelOrder(order) {
  try {
    const user = encodeURIComponent(order.owner || order.user || "");
    await request(`/api/orders/${encodeURIComponent(order.order_id || order.id)}?user=${user}`, { method: "DELETE", headers });
    els.orderResult.textContent = `Canceled ${order.order_id || order.id}`;
    await refresh();
  } catch (error) {
    els.orderResult.textContent = error.message;
  }
}

async function resetPaper() {
  try {
    await request("/api/reset", { method: "POST", headers, body: "{}" });
    els.orderResult.textContent = "Paper accounts reset.";
    await refresh();
  } catch (error) {
    els.orderResult.textContent = error.message;
  }
}

function updateOrderFields() {
  const type = els.orderType.value;
  const needsLimit = type === "limit" || type === "stop_limit";
  const needsStop = type === "stop" || type === "stop_limit";
  els.price.disabled = !needsLimit;
  els.price.placeholder = needsLimit ? "limit" : "market";
  els.stopPriceWrap.classList.toggle("hidden", !needsStop);
  els.postOnly.disabled = type !== "limit";
  if (type !== "limit") els.postOnly.checked = false;
}

els.refreshApply.addEventListener("click", () => applyRefresh().catch((error) => { els.orderResult.textContent = error.message; }));
els.buyButton.addEventListener("click", () => submitOrder("buy"));
els.sellButton.addEventListener("click", () => submitOrder("sell"));
els.fundButton.addEventListener("click", fundUser);
els.resetButton.addEventListener("click", resetPaper);
els.orderType.addEventListener("change", updateOrderFields);
window.addEventListener("resize", () => {
  if (currentState) {
    drawPriceChart(currentState.history || []);
    drawDepth(currentState.order_book || { bids: [], asks: [] });
  }
});

updateOrderFields();
loadRefresh()
  .catch((error) => {
    els.orderResult.textContent = error.message;
    setRefresh(1000);
  })
  .finally(refresh);
