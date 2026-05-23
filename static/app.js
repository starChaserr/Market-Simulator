const els = {
  lastPrice: document.querySelector("#lastPrice"),
  bidAsk: document.querySelector("#bidAsk"),
  spread: document.querySelector("#spread"),
  volume: document.querySelector("#volume"),
  volatility: document.querySelector("#volatility"),
  chartSubhead: document.querySelector("#chartSubhead"),
  marketState: document.querySelector("#marketState"),
  currencySelect: document.querySelector("#currencySelect"),
  currencySource: document.querySelector("#currencySource"),
  chartRefreshSelect: document.querySelector("#chartRefreshSelect"),
  chartRefreshApply: document.querySelector("#chartRefreshApply"),
  chartRefreshStatus: document.querySelector("#chartRefreshStatus"),
  priceCanvas: document.querySelector("#priceCanvas"),
  bookCanvas: document.querySelector("#bookCanvas"),
  userName: document.querySelector("#userName"),
  quantity: document.querySelector("#quantity"),
  orderType: document.querySelector("#orderType"),
  price: document.querySelector("#price"),
  priceCurrencyHint: document.querySelector("#priceCurrencyHint"),
  fundCurrencyHint: document.querySelector("#fundCurrencyHint"),
  fundingAmount: document.querySelector("#fundingAmount"),
  fundButton: document.querySelector("#fundButton"),
  buyButton: document.querySelector("#buyButton"),
  sellButton: document.querySelector("#sellButton"),
  orderResult: document.querySelector("#orderResult"),
  toggleRun: document.querySelector("#toggleRun"),
  reset: document.querySelector("#reset"),
  agentsTable: document.querySelector("#agentsTable"),
  usersTable: document.querySelector("#usersTable"),
  tradesTable: document.querySelector("#tradesTable"),
  agentCounts: document.querySelector("#agentCounts"),
};

let currentState = null;
let refreshTimer = null;
let currentRefreshMs = 1000;
let chartRefreshSaveTimer = null;
let chartRefreshSaveToken = 0;
let displayLocale = navigator.language || "en-US";
let displayCurrency = "USD";
const currencyStorageKey = "market-simulator-display-currency";
const currencyModeStorageKey = "market-simulator-display-currency-mode";
const dashboardHeaders = { "X-API-User": "Dashboard UI" };

async function loadCurrencyPreference() {
  const savedCurrency = localStorage.getItem(currencyStorageKey);
  const savedMode = localStorage.getItem(currencyModeStorageKey);
  const locale = browserLocale();
  const timezone = browserTimezone();

  try {
    const params = new URLSearchParams({ locale, timezone });
    const response = await fetch(`/api/currency?${params.toString()}`, { cache: "no-store", headers: dashboardHeaders });
    if (!response.ok) throw new Error(`Currency request failed: ${response.status}`);
    const preference = await response.json();
    if (savedCurrency && savedMode === "manual") {
      setCurrency(savedCurrency, preference.locale || locale || displayLocale, "manual");
      return;
    }
    if (savedCurrency) {
      localStorage.removeItem(currencyStorageKey);
      localStorage.removeItem(currencyModeStorageKey);
    }
    setCurrency(
      preference.currency || displayCurrency,
      preference.locale || locale || displayLocale,
      currencySourceLabel(preference),
    );
  } catch (_error) {
    if (savedCurrency && savedMode === "manual") {
      setCurrency(savedCurrency, locale || displayLocale, "manual");
      return;
    }
    setCurrency(displayCurrency, displayLocale, "default");
  }
}

function browserLocale() {
  if (navigator.languages?.length) return navigator.languages[0];
  return navigator.language || "";
}

function browserTimezone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch (_error) {
    return "";
  }
}

function currencySourceLabel(preference) {
  if (!preference?.source || preference.source === "default") return "default";
  return preference.region ? `auto ${preference.region}` : `auto ${preference.source}`;
}

function setCurrency(currency, locale, source) {
  displayCurrency = String(currency || "USD").toUpperCase();
  displayLocale = locale || displayLocale || "en-US";
  ensureCurrencyOption(displayCurrency);
  els.currencySelect.value = displayCurrency;
  els.currencySource.textContent = source;
  els.priceCurrencyHint.textContent = `(${displayCurrency})`;
  els.fundCurrencyHint.textContent = `(${displayCurrency})`;
  if (currentState) render(currentState);
}

function setRefreshCadence(refreshMs, source = "broker feed") {
  const nextMs = Math.max(250, Math.min(Number(refreshMs) || 1000, 60000));
  currentRefreshMs = nextMs;
  ensureChartRefreshOption(nextMs);
  els.chartRefreshSelect.value = String(nextMs);
  els.chartRefreshStatus.textContent = source;
  if (refreshTimer) window.clearInterval(refreshTimer);
  refreshTimer = window.setInterval(refresh, currentRefreshMs);
}

function ensureChartRefreshOption(refreshMs) {
  const value = String(refreshMs);
  if ([...els.chartRefreshSelect.options].some((option) => option.value === value)) return;
  const option = document.createElement("option");
  option.value = value;
  option.textContent = refreshMs >= 60000 ? `${money(refreshMs / 60000, 1)}m` : `${money(refreshMs / 1000, 2)}s`;
  els.chartRefreshSelect.appendChild(option);
}

function ensureCurrencyOption(currency) {
  if ([...els.currencySelect.options].some((option) => option.value === currency)) return;
  const option = document.createElement("option");
  option.value = currency;
  option.textContent = currency;
  els.currencySelect.appendChild(option);
}

function money(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function compact(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toLocaleString(undefined, {
    notation: "compact",
    maximumFractionDigits: 2,
  });
}

function formatCurrency(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  try {
    return new Intl.NumberFormat(displayLocale, {
      style: "currency",
      currency: displayCurrency,
      currencyDisplay: "narrowSymbol",
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(Number(value));
  } catch (_error) {
    return `${displayCurrency} ${money(value, digits)}`;
  }
}

function signedClass(side) {
  return side === "buy" ? "side-buy" : "side-sell";
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

async function fetchState() {
  const response = await fetch("/api/state", { cache: "no-store", headers: dashboardHeaders });
  if (!response.ok) throw new Error(`State request failed: ${response.status}`);
  return response.json();
}

async function loadChartRefreshPreference() {
  try {
    const response = await fetch("/api/chart-refresh", { cache: "no-store", headers: dashboardHeaders });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 404) {
        els.chartRefreshStatus.textContent = "local";
        return;
      }
      const retryAfter = response.headers.get("Retry-After");
      const suffix = retryAfter ? ` Retry after ${retryAfter}s.` : "";
      throw new Error(`${result.error || `Chart refresh request failed (${response.status})`}.${suffix}`);
    }
    const confirmedMs = Number(result.chart_refresh_ms);
    if (Number.isFinite(confirmedMs) && confirmedMs > 0) {
      setRefreshCadence(confirmedMs, "broker feed");
    }
  } catch (error) {
    els.chartRefreshStatus.textContent = "default";
    els.orderResult.textContent = error.message;
  }
}

async function refresh() {
  try {
    currentState = await fetchState();
    render(currentState);
  } catch (error) {
    els.marketState.textContent = "Offline";
    els.marketState.classList.add("paused");
    els.orderResult.textContent = error.message;
  }
}

function render(state) {
  if (state.chart_refresh_ms && Number(state.chart_refresh_ms) !== currentRefreshMs) {
    setRefreshCadence(Number(state.chart_refresh_ms));
  }
  els.lastPrice.textContent = formatCurrency(state.last_price, 2);
  els.bidAsk.textContent = `${formatCurrency(state.best_bid, 2)} / ${formatCurrency(state.best_ask, 2)}`;
  els.spread.textContent = formatCurrency(state.spread, 4);
  els.volume.textContent = compact(state.total_volume);
  els.volatility.textContent = `${money(state.volatility * 100, 2)}%`;
  els.chartSubhead.textContent = `${state.symbol} tick ${state.tick} | chart ${money((state.chart_refresh_ms || currentRefreshMs) / 1000, 2)}s | high ${formatCurrency(state.session_high, 2)} | low ${formatCurrency(state.session_low, 2)}`;
  els.marketState.textContent = state.running ? "Live" : "Paused";
  els.marketState.classList.toggle("paused", !state.running);
  els.toggleRun.textContent = state.running ? "Pause" : "Resume";
  renderAgentCounts(state.agent_counts);
  renderApiUsers(state.api_users || []);
  renderAgents(state.agents);
  renderTrades(state.trades);
  drawPriceChart(state);
  drawBook(state.order_book);
}

function renderAgentCounts(counts) {
  const parts = Object.entries(counts || {}).map(([key, value]) => `${key}: ${value}`);
  els.agentCounts.textContent = parts.join(" | ");
}

function renderApiUsers(users) {
  els.usersTable.replaceChildren();
  if (!users.length) {
    const row = document.createElement("tr");
    const cell = appendCell(row, "No API users yet. Submit orders with user, api_user, client_id, model, or X-API-User.");
    cell.colSpan = 7;
    els.usersTable.appendChild(row);
    return;
  }
  for (const user of users.slice(0, 40)) {
    const row = document.createElement("tr");
    appendCell(row, user.user);
    appendCell(row, formatCurrency(user.cash, 2));
    appendCell(row, signedMoney(user.profit_loss), pnlClass(user.profit_loss));
    appendCell(row, money(user.inventory, 2));
    appendCell(row, compact(user.volume));
    appendCell(row, String(user.orders));
    appendCell(row, user.average_trade_price ? formatCurrency(user.average_trade_price, 2) : "--");
    els.usersTable.appendChild(row);
  }
}

function renderAgents(agents) {
  els.agentsTable.replaceChildren();
  for (const agent of agents.slice(0, 60)) {
    const row = document.createElement("tr");
    appendCell(row, agent.owner);
    const typeCell = document.createElement("td");
    const chip = document.createElement("span");
    chip.className = "type-chip";
    chip.textContent = agent.agent_type;
    typeCell.appendChild(chip);
    row.appendChild(typeCell);
    const action = agent.extra?.last_action || "hold";
    const actionCell = document.createElement("td");
    const actionChip = document.createElement("span");
    actionChip.className = `action-chip ${actionClass(action)}`;
    actionChip.textContent = action.toUpperCase();
    actionCell.appendChild(actionChip);
    row.appendChild(actionCell);
    appendCell(row, money(agent.inventory, 2));
    appendCell(row, compact(agent.volume));
    appendCell(row, signedMoney(agent.profit_loss), pnlClass(agent.profit_loss));
    els.agentsTable.appendChild(row);
  }
}

function actionClass(action) {
  const normalized = String(action || "").toLowerCase();
  if (normalized.includes("buy") && normalized.includes("sell")) return "action-mixed";
  if (normalized.includes("buy")) return "action-buy";
  if (normalized.includes("sell")) return "action-sell";
  return "action-hold";
}

function renderTrades(trades) {
  els.tradesTable.replaceChildren();
  for (const trade of trades.slice(0, 80)) {
    const row = document.createElement("tr");
    const side = document.createElement("td");
    side.className = signedClass(trade.aggressor_side);
    side.textContent = trade.aggressor_side.toUpperCase();
    row.appendChild(side);
    appendCell(row, formatCurrency(trade.price, 2));
    appendCell(row, money(trade.quantity, 2));
    appendCell(row, shortName(trade.buyer));
    appendCell(row, shortName(trade.seller));
    els.tradesTable.appendChild(row);
  }
}

function appendCell(row, text, className = "") {
  const cell = document.createElement("td");
  cell.textContent = text;
  if (className) cell.className = className;
  row.appendChild(cell);
  return cell;
}

function signedMoney(value) {
  const number = Number(value || 0);
  const prefix = number > 0 ? "+" : number < 0 ? "-" : "";
  return `${prefix}${formatCurrency(Math.abs(number), 2)}`;
}

function pnlClass(value) {
  const number = Number(value || 0);
  if (number > 0) return "pnl-positive";
  if (number < 0) return "pnl-negative";
  return "";
}

function shortName(value) {
  if (!value) return "--";
  return String(value)
    .replace("institution-", "inst-")
    .replace("market-maker-", "mm-")
    .replace("external-liquidity", "latent");
}

function drawPriceChart(state) {
  const { ctx, width, height } = resizeCanvas(els.priceCanvas);
  const history = (state.history || []).slice(-170);
  ctx.clearRect(0, 0, width, height);
  drawTradingBackground(ctx, width, height);

  if (history.length < 2) {
    drawCentered(ctx, width, height, "Waiting for trades");
    return;
  }

  const pad = { left: 14, right: 74, top: 18, bottom: 74 };
  const plotW = width - pad.left - pad.right;
  const priceH = height - pad.top - pad.bottom;
  const volumeTop = height - 54;
  const volumeH = 38;
  const prices = history.flatMap((point) => [
    point.high ?? point.last,
    point.low ?? point.last,
    point.fundamental,
  ]);
  const minPrice = Math.min(...prices);
  const maxPrice = Math.max(...prices);
  const padding = Math.max(0.02, (maxPrice - minPrice) * 0.08);
  const lowBound = minPrice - padding;
  const highBound = maxPrice + padding;
  const range = Math.max(0.01, highBound - lowBound);
  const y = (price) => pad.top + (highBound - price) / range * priceH;
  const step = plotW / history.length;
  const candleW = Math.max(3, Math.min(12, step * 0.62));
  const x = (index) => pad.left + index * step + step / 2;

  ctx.strokeStyle = "#e8ebe3";
  ctx.lineWidth = 1;
  ctx.fillStyle = "#5f686d";
  ctx.font = "11px system-ui, sans-serif";
  ctx.textAlign = "left";
  for (let i = 0; i <= 6; i += 1) {
    const yy = pad.top + priceH * i / 6;
    ctx.beginPath();
    ctx.moveTo(pad.left, yy);
    ctx.lineTo(width - pad.right, yy);
    ctx.stroke();
    const label = highBound - range * i / 6;
    ctx.fillText(formatCurrency(label, 2), width - pad.right + 10, yy + 4);
  }
  for (let i = 0; i < history.length; i += 24) {
    const xx = x(i);
    ctx.beginPath();
    ctx.moveTo(xx, pad.top);
    ctx.lineTo(xx, volumeTop + volumeH);
    ctx.stroke();
  }

  const maxVolume = Math.max(1, ...history.map((point) => point.volume || 0));
  for (let i = 0; i < history.length; i += 1) {
    const point = history[i];
    const open = point.open ?? history[i - 1]?.close ?? point.last;
    const close = point.close ?? point.last;
    const high = point.high ?? Math.max(open, close);
    const low = point.low ?? Math.min(open, close);
    const up = close >= open;
    const color = up ? "#0f8f69" : "#d23f31";
    const xx = x(i);
    const bodyTop = y(Math.max(open, close));
    const bodyBottom = y(Math.min(open, close));
    const bodyHeight = Math.max(2, bodyBottom - bodyTop);

    ctx.strokeStyle = color;
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.moveTo(xx, y(high));
    ctx.lineTo(xx, y(low));
    ctx.stroke();

    ctx.fillStyle = up ? "rgba(15, 143, 105, 0.88)" : "rgba(210, 63, 49, 0.9)";
    ctx.fillRect(xx - candleW / 2, bodyTop, candleW, bodyHeight);

    const barHeight = (point.volume || 0) / maxVolume * volumeH;
    ctx.fillStyle = up ? "rgba(15, 143, 105, 0.24)" : "rgba(210, 63, 49, 0.22)";
    ctx.fillRect(xx - candleW / 2, volumeTop + volumeH - barHeight, candleW, barHeight);
  }

  drawLine(ctx, history, x, y, "fundamental", "#b87503", 1.3, [5, 5]);
  drawMovingAverage(ctx, history, x, y, 12, "#2563eb", 1.6);
  drawMovingAverage(ctx, history, x, y, 30, "#7c3aed", 1.4);

  const last = history[history.length - 1];
  const lastY = y(last.close ?? last.last);
  ctx.strokeStyle = "rgba(37, 99, 235, 0.45)";
  ctx.setLineDash([4, 5]);
  ctx.beginPath();
  ctx.moveTo(pad.left, lastY);
  ctx.lineTo(width - pad.right, lastY);
  ctx.stroke();
  ctx.setLineDash([]);
  const lastLabel = formatCurrency(last.close ?? last.last, 2);
  const lastLabelWidth = Math.max(64, ctx.measureText(lastLabel).width + 14);
  ctx.fillStyle = "#2563eb";
  roundRect(ctx, width - pad.right + 5, lastY - 11, lastLabelWidth, 22, 5);
  ctx.fill();
  ctx.fillStyle = "#ffffff";
  ctx.font = "700 11px system-ui, sans-serif";
  ctx.fillText(lastLabel, width - pad.right + 11, lastY + 4);

  ctx.fillStyle = "#20272a";
  ctx.font = "700 12px system-ui, sans-serif";
  ctx.fillText("MA 12", pad.left + 8, pad.top + 16);
  ctx.fillStyle = "#7c3aed";
  ctx.fillText("MA 30", pad.left + 56, pad.top + 16);
  ctx.fillStyle = "#b87503";
  ctx.fillText("Fundamental", pad.left + 104, pad.top + 16);
  ctx.textAlign = "left";
}

function drawLine(ctx, points, x, y, key, color, width, dash) {
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.setLineDash(dash);
  ctx.beginPath();
  points.forEach((point, index) => {
    const xx = x(index);
    const yy = y(point[key]);
    if (index === 0) ctx.moveTo(xx, yy);
    else ctx.lineTo(xx, yy);
  });
  ctx.stroke();
  ctx.restore();
}

function drawMovingAverage(ctx, points, x, y, windowSize, color, width) {
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.beginPath();
  let started = false;
  for (let index = 0; index < points.length; index += 1) {
    const start = Math.max(0, index - windowSize + 1);
    const slice = points.slice(start, index + 1);
    if (slice.length < Math.min(4, windowSize)) continue;
    const average = slice.reduce((sum, point) => sum + (point.close ?? point.last), 0) / slice.length;
    if (!started) {
      ctx.moveTo(x(index), y(average));
      started = true;
    } else {
      ctx.lineTo(x(index), y(average));
    }
  }
  ctx.stroke();
  ctx.restore();
}

function drawTradingBackground(ctx, width, height) {
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, width, height);
  ctx.fillStyle = "#f8faf6";
  ctx.fillRect(0, height - 62, width, 62);
}

function roundRect(ctx, x, y, width, height, radius) {
  const r = Math.min(radius, width / 2, height / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + width, y, x + width, y + height, r);
  ctx.arcTo(x + width, y + height, x, y + height, r);
  ctx.arcTo(x, y + height, x, y, r);
  ctx.arcTo(x, y, x + width, y, r);
  ctx.closePath();
}

function drawBook(book) {
  const { ctx, width, height } = resizeCanvas(els.bookCanvas);
  ctx.clearRect(0, 0, width, height);
  drawPanelBackground(ctx, width, height);

  const bids = (book?.bids || []).slice(0, 12);
  const asks = (book?.asks || []).slice(0, 12);
  if (!bids.length && !asks.length) {
    drawCentered(ctx, width, height, "No depth");
    return;
  }

  const pad = { left: 12, right: 12, top: 20, bottom: 20 };
  const mid = width / 2;
  const maxQty = Math.max(1, ...bids.map((level) => level.cumulative), ...asks.map((level) => level.cumulative));
  const rows = Math.max(bids.length, asks.length, 1);
  const rowH = (height - pad.top - pad.bottom) / rows;

  ctx.strokeStyle = "#d8dbd2";
  ctx.beginPath();
  ctx.moveTo(mid, pad.top);
  ctx.lineTo(mid, height - pad.bottom);
  ctx.stroke();

  drawBookSide(ctx, bids, {
    x0: pad.left,
    x1: mid - 8,
    y0: pad.top,
    rowH,
    maxQty,
    color: "rgba(8, 127, 91, 0.34)",
    textColor: "#087f5b",
    align: "left",
  });

  drawBookSide(ctx, asks, {
    x0: mid + 8,
    x1: width - pad.right,
    y0: pad.top,
    rowH,
    maxQty,
    color: "rgba(201, 42, 42, 0.28)",
    textColor: "#c92a2a",
    align: "right",
  });
}

function drawBookSide(ctx, levels, opts) {
  ctx.font = "12px system-ui, sans-serif";
  levels.forEach((level, index) => {
    const y = opts.y0 + index * opts.rowH + 3;
    const fullWidth = opts.x1 - opts.x0;
    const barWidth = level.cumulative / opts.maxQty * fullWidth;
    ctx.fillStyle = opts.color;
    if (opts.align === "left") {
      ctx.fillRect(opts.x1 - barWidth, y, barWidth, Math.max(7, opts.rowH - 5));
      ctx.fillStyle = opts.textColor;
      ctx.fillText(formatCurrency(level.price, 2), opts.x0 + 2, y + Math.min(18, opts.rowH - 2));
      ctx.fillStyle = "#20272a";
      ctx.fillText(compact(level.quantity), opts.x1 - 74, y + Math.min(18, opts.rowH - 2));
    } else {
      ctx.fillRect(opts.x0, y, barWidth, Math.max(7, opts.rowH - 5));
      ctx.fillStyle = opts.textColor;
      ctx.fillText(formatCurrency(level.price, 2), opts.x0 + 6, y + Math.min(18, opts.rowH - 2));
      ctx.fillStyle = "#20272a";
      ctx.fillText(compact(level.quantity), opts.x1 - 76, y + Math.min(18, opts.rowH - 2));
    }
  });
}

function drawPanelBackground(ctx, width, height) {
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, width, height);
  ctx.fillStyle = "#fbfcf9";
  ctx.fillRect(0, height * 0.72, width, height * 0.28);
}

function drawCentered(ctx, width, height, text) {
  ctx.fillStyle = "#687176";
  ctx.font = "700 14px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.fillText(text, width / 2, height / 2);
  ctx.textAlign = "left";
}

async function submitManualOrder(side) {
  const quantity = Number(els.quantity.value);
  const orderType = els.orderType.value;
  const price = els.price.value ? Number(els.price.value) : null;
  if (!Number.isFinite(quantity) || quantity <= 0) {
    els.orderResult.textContent = "Quantity must be positive.";
    return;
  }
  if (orderType === "limit" && (!Number.isFinite(price) || price <= 0)) {
    els.orderResult.textContent = "Limit price must be positive.";
    return;
  }

  const payload = { quantity, order_type: orderType, user: els.userName.value || "Dashboard User" };
  if (orderType === "limit") payload.price = price;
  els.orderResult.textContent = "Submitting...";
  try {
    const response = await fetch(`/api/${side}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Order rejected");
    els.orderResult.textContent = `${side.toUpperCase()} ${money(result.filled_quantity, 2)} / ${money(result.requested_quantity, 2)} @ ${result.average_price ? formatCurrency(result.average_price, 2) : "resting"} (${result.status})`;
    await refresh();
  } catch (error) {
    els.orderResult.textContent = error.message;
  }
}

async function fundUser() {
  const amount = Number(els.fundingAmount.value);
  const user = els.userName.value || "Dashboard User";
  if (!Number.isFinite(amount) || amount <= 0) {
    els.orderResult.textContent = "Funding amount must be positive.";
    return;
  }

  els.orderResult.textContent = "Funding...";
  try {
    const response = await fetch("/api/accounts/fund", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user, amount }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Funding failed");
    els.orderResult.textContent = `${result.user} funded. Cash ${formatCurrency(result.cash, 2)}.`;
    await refresh();
  } catch (error) {
    els.orderResult.textContent = error.message;
  }
}

async function setRunning(nextRunning) {
  await fetch("/api/simulation", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...dashboardHeaders },
    body: JSON.stringify({ running: nextRunning }),
  });
  await refresh();
}

async function resetSimulation() {
  els.orderResult.textContent = "Resetting...";
  await fetch("/api/reset", { method: "POST", headers: dashboardHeaders });
  els.orderResult.textContent = "Simulation reset.";
  await refresh();
}

async function setChartRefresh() {
  const chartRefreshMs = Number(els.chartRefreshSelect.value);
  const previousMs = currentRefreshMs;
  const saveToken = ++chartRefreshSaveToken;
  if (!Number.isFinite(chartRefreshMs) || chartRefreshMs <= 0) {
    els.orderResult.textContent = "Chart refresh must be positive.";
    els.chartRefreshSelect.value = String(previousMs);
    return;
  }
  els.chartRefreshApply.disabled = true;
  els.chartRefreshStatus.textContent = "updating...";
  try {
    const response = await fetch("/api/chart-refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...dashboardHeaders },
      body: JSON.stringify({ chart_refresh_ms: chartRefreshMs }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      const retryAfter = response.headers.get("Retry-After");
      const suffix = retryAfter ? ` Retry after ${retryAfter}s.` : "";
      const error = new Error(`${result.error || `Chart refresh update failed (${response.status})`}.${suffix}`);
      error.status = response.status;
      throw error;
    }
    const confirmedMs = Number(result.chart_refresh_ms);
    if (!Number.isFinite(confirmedMs) || confirmedMs <= 0) {
      throw new Error("Chart refresh update returned an invalid interval.");
    }
    if (saveToken !== chartRefreshSaveToken) return;
    setRefreshCadence(confirmedMs, "broker feed");
    els.orderResult.textContent = `Chart refresh set to ${money(confirmedMs / 1000, 2)}s.`;
    refresh();
  } catch (error) {
    if (error.status === 404) {
      setRefreshCadence(chartRefreshMs, "local");
      els.orderResult.textContent = `Chart refresh set locally to ${money(chartRefreshMs / 1000, 2)}s. Restart the server to enable broker API sync.`;
      return;
    }
    if (saveToken !== chartRefreshSaveToken) return;
    setRefreshCadence(previousMs, "failed");
    els.orderResult.textContent = error.message;
  } finally {
    if (saveToken === chartRefreshSaveToken) {
      els.chartRefreshApply.disabled = false;
    }
  }
}

function queueChartRefreshSave() {
  if (chartRefreshSaveTimer) window.clearTimeout(chartRefreshSaveTimer);
  chartRefreshSaveTimer = window.setTimeout(setChartRefresh, 80);
}

els.orderType.addEventListener("change", () => {
  const isLimit = els.orderType.value === "limit";
  els.price.disabled = !isLimit;
  if (!isLimit) els.price.value = "";
  if (isLimit && currentState?.last_price) els.price.placeholder = `${displayCurrency} ${money(currentState.last_price, 2)}`;
});
els.buyButton.addEventListener("click", () => submitManualOrder("buy"));
els.sellButton.addEventListener("click", () => submitManualOrder("sell"));
els.fundButton.addEventListener("click", fundUser);
els.toggleRun.addEventListener("click", () => setRunning(!currentState?.running));
els.reset.addEventListener("click", resetSimulation);
els.chartRefreshSelect.addEventListener("input", queueChartRefreshSave);
els.chartRefreshSelect.addEventListener("change", queueChartRefreshSave);
els.chartRefreshApply.addEventListener("click", setChartRefresh);
els.currencySelect.addEventListener("change", () => {
  localStorage.setItem(currencyStorageKey, els.currencySelect.value);
  localStorage.setItem(currencyModeStorageKey, "manual");
  setCurrency(els.currencySelect.value, navigator.language || displayLocale, "manual");
});
window.addEventListener("resize", () => {
  if (currentState) render(currentState);
});

setRefreshCadence(currentRefreshMs);
loadChartRefreshPreference().then(loadCurrencyPreference).then(refresh);
window.addEventListener("beforeunload", () => window.clearInterval(refreshTimer));
