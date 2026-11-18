/**
 * Stock Signal — Current 4-Rule Playbook Depot & Automated Email Alerts UI
 * Renders exact 4-Year / multi-horizon profit, equity curve vs. SPY,
 * live 6-slot holdings with exit-rule proximity, closed trade ledger, and email preview.
 */
(() => {
  "use strict";

  const depotState = {
    data: window.__FOUR_RULE_DEPOT__ || null,
    alerts: window.__ALERT_DATA__ || null,
    windowKey: "4y",
    capital: 10000,
  };

  function esc(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtMoney(val, showPlus = false) {
    if (val === null || val === undefined || Number.isNaN(val)) return "–";
    const abs = Math.abs(val);
    const formatted =
      "$" +
      Math.round(abs).toLocaleString("en-US", {
        maximumFractionDigits: 0,
      });
    if (val < 0) return "-" + formatted;
    return (showPlus && val > 0 ? "+" : "") + formatted;
  }

  function fmtPrice(val, sym = "$") {
    if (val === null || val === undefined || Number.isNaN(val)) return "–";
    return (
      sym +
      Number(val).toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })
    );
  }

  function fmtSignedPct(val, digits = 1) {
    if (val === null || val === undefined || Number.isNaN(val)) return "–";
    return `${val > 0 ? "+" : ""}${Number(val).toFixed(digits)}%`;
  }

  function getActiveWindow() {
    if (!depotState.data || !depotState.data.windows) return null;
    return (
      depotState.data.windows[depotState.windowKey] ||
      depotState.data.windows["4y"] ||
      Object.values(depotState.data.windows)[0]
    );
  }

  function renderDepotKpis() {
    const grid = document.getElementById("depot-kpi-grid");
    const win = getActiveWindow();
    if (!grid || !win) return;

    const scale = depotState.capital / (depotState.data.initialCapital || 10000);
    const finalVal = win.finalValue10k * scale;
    const profitVal = win.profit10k * scale;
    const spyFinal = win.spyFinal10k * scale;
    const spyProfit = win.spyProfit10k * scale;
    const extraProfit = win.extraProfitVsSpy10k * scale;
    const cagrEdge = win.cagrPct - win.spyCagrPct;

    const activeSlots =
      (depotState.alerts &&
        Array.isArray(depotState.alerts.activeSlots) &&
        depotState.alerts.activeSlots.length > 0
        ? depotState.alerts.activeSlots
        : win.activeSlots) || [];

    const winLabel = depotState.windowKey.toUpperCase();

    grid.innerHTML = `
      <div class="bt-kpi-card featured">
        <div class="bt-kpi-title">
          <span>⚡ 4-Rule Depot (${esc(winLabel)})</span>
          <span class="dip-pill dip-prime_dip">${fmtSignedPct(win.totalReturnPct)}</span>
        </div>
        <div class="bt-kpi-value">${fmtMoney(finalVal)}</div>
        <div class="bt-kpi-sub">${fmtSignedPct(win.cagrPct, 2)}/yr CAGR · ${esc(win.startDate.slice(0, 7))} → Today</div>
      </div>

      <div class="bt-kpi-card">
        <div class="bt-kpi-title">
          <span>Net Profit Earned</span>
          <span style="font-size:0.71rem;color:var(--text-muted)">from ${fmtMoney(depotState.capital)}</span>
        </div>
        <div class="bt-kpi-value ${profitVal >= 0 ? "val-good" : "val-bad"}">${fmtMoney(profitVal, true)}</div>
        <div class="bt-kpi-sub">SPY made ${fmtMoney(spyProfit, true)} (${fmtSignedPct(win.spyReturnPct)})</div>
      </div>

      <div class="bt-kpi-card">
        <div class="bt-kpi-title">
          <span>Extra Profit vs. SPY</span>
          <span style="font-size:0.71rem;color:var(--text-muted)">0.10% fee incl.</span>
        </div>
        <div class="bt-kpi-value ${extraProfit >= 0 ? "val-good" : "val-bad"}">${fmtMoney(extraProfit, true)}</div>
        <div class="bt-kpi-sub">SPY ended at ${fmtMoney(spyFinal)} (${fmtSignedPct(cagrEdge, 2)}/yr edge)</div>
      </div>

      <div class="bt-kpi-card">
        <div class="bt-kpi-title">
          <span>Closed Trade Win Rate</span>
          <span class="dip-pill dip-pullback">${win.winningTradesCount}/${win.closedTradesCount} won</span>
        </div>
        <div class="bt-kpi-value">${win.winRatePct.toFixed(1)}%</div>
        <div class="bt-kpi-sub">Avg ${fmtSignedPct(win.avgTradeReturnPct, 2)}/trade · ${Math.round(win.avgDaysHeld)}d hold</div>
      </div>

      <div class="bt-kpi-card">
        <div class="bt-kpi-title">
          <span>Max Drawdown &amp; Slots</span>
          <span class="dip-pill dip-neutral">${activeSlots.length}/6 Active</span>
        </div>
        <div class="bt-kpi-value">${win.maxDrawdownPct.toFixed(1)}%</div>
        <div class="bt-kpi-sub">SPY DD ${win.spyMaxDrawdownPct.toFixed(1)}% · Sharpe ${win.sharpe.toFixed(2)}</div>
      </div>
    `;
  }

  function drawDepotEquityChart() {
    const canvas = document.getElementById("depot-equity-canvas");
    const readout = document.getElementById("depot-chart-readout");
    const win = getActiveWindow();
    if (!canvas || !win || !Array.isArray(win.curve) || win.curve.length < 2) return;

    const curve = win.curve;
    const scale = depotState.capital / (depotState.data.initialCapital || 10000);
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.parentElement.getBoundingClientRect();
    const width = Math.max(300, Math.floor(rect.width - 40));
    const height = 285;

    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);

    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const isDark = document.documentElement.getAttribute("data-theme") === "dark";
    const colDepot = isDark ? "#a78bfa" : "#7c3aed";
    const colSpy = isDark ? "#94a3b8" : "#64748b";
    const colGrid = isDark ? "rgba(148, 163, 184, 0.14)" : "rgba(15, 23, 42, 0.08)";
    const colText = isDark ? "#94a3b8" : "#64748b";

    const pad = { top: 16, right: 16, bottom: 28, left: 68 };
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;

    let minY = Infinity;
    let maxY = -Infinity;
    for (const pt of curve) {
      const dv = pt.depot * scale;
      const sv = pt.spy * scale;
      if (dv < minY) minY = dv;
      if (sv < minY) minY = sv;
      if (dv > maxY) maxY = dv;
      if (sv > maxY) maxY = sv;
    }
    const span = Math.max(1, maxY - minY);
    minY = Math.max(0, minY - span * 0.07);
    maxY = maxY + span * 0.07;

    const xAt = (i) => pad.left + (i / (curve.length - 1)) * plotW;
    const yAt = (v) => pad.top + (1 - (v - minY) / (maxY - minY)) * plotH;

    function paint(hoverIdx) {
      ctx.clearRect(0, 0, width, height);

      // Gridlines & Y labels
      ctx.font = "600 11px 'JetBrains Mono', monospace";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      const steps = 4;
      for (let s = 0; s <= steps; s++) {
        const val = minY + (s / steps) * (maxY - minY);
        const y = yAt(val);
        ctx.strokeStyle = colGrid;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(pad.left, y);
        ctx.lineTo(width - pad.right, y);
        ctx.stroke();

        ctx.fillStyle = colText;
        const lbl =
          val >= 100000
            ? "$" + (val / 1000).toFixed(0) + "k"
            : "$" + Math.round(val).toLocaleString("en-US");
        ctx.fillText(lbl, pad.left - 8, y);
      }

      // X-axis date ticks
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      const xTicks = Math.min(6, curve.length - 1);
      for (let t = 0; t <= xTicks; t++) {
        const idx = Math.round((t / xTicks) * (curve.length - 1));
        const x = xAt(idx);
        ctx.fillStyle = colText;
        ctx.fillText(curve[idx].d.slice(0, 7), x, height - pad.bottom + 8);
      }

      // SPY line (dashed subtle)
      ctx.save();
      ctx.strokeStyle = colSpy;
      ctx.lineWidth = 1.8;
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      curve.forEach((pt, i) => {
        const x = xAt(i);
        const y = yAt(pt.spy * scale);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
      ctx.restore();

      // 4-Rule Depot area fill + bold line
      const grad = ctx.createLinearGradient(0, pad.top, 0, pad.top + plotH);
      grad.addColorStop(0, isDark ? "rgba(167, 139, 250, 0.24)" : "rgba(124, 58, 237, 0.16)");
      grad.addColorStop(1, "rgba(124, 58, 237, 0.0)");
      ctx.fillStyle = grad;
      ctx.beginPath();
      curve.forEach((pt, i) => {
        const x = xAt(i);
        const y = yAt(pt.depot * scale);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.lineTo(xAt(curve.length - 1), pad.top + plotH);
      ctx.lineTo(xAt(0), pad.top + plotH);
      ctx.closePath();
      ctx.fill();

      ctx.strokeStyle = colDepot;
      ctx.lineWidth = 2.6;
      ctx.beginPath();
      curve.forEach((pt, i) => {
        const x = xAt(i);
        const y = yAt(pt.depot * scale);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();

      // Hover crosshair
      if (hoverIdx !== null && hoverIdx >= 0 && hoverIdx < curve.length) {
        const pt = curve[hoverIdx];
        const x = xAt(hoverIdx);
        const yd = yAt(pt.depot * scale);
        const ys = yAt(pt.spy * scale);

        ctx.strokeStyle = isDark ? "rgba(226,232,240,0.28)" : "rgba(15,23,42,0.22)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, pad.top);
        ctx.lineTo(x, pad.top + plotH);
        ctx.stroke();

        ctx.fillStyle = colSpy;
        ctx.beginPath();
        ctx.arc(x, ys, 4, 0, Math.PI * 2);
        ctx.fill();

        ctx.fillStyle = colDepot;
        ctx.beginPath();
        ctx.arc(x, yd, 5, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    paint(null);

    canvas.onmousemove = (ev) => {
      const b = canvas.getBoundingClientRect();
      const mx = ev.clientX - b.left;
      const ratio = Math.max(0, Math.min(1, (mx - pad.left) / plotW));
      const idx = Math.round(ratio * (curve.length - 1));
      const pt = curve[idx];
      if (pt && readout) {
        const dv = pt.depot * scale;
        const sv = pt.spy * scale;
        const prof = dv - depotState.capital;
        readout.textContent = `${pt.d} · 4-Rule Depot: ${fmtMoney(dv)} (Profit ${fmtMoney(prof, true)}) · SPY: ${fmtMoney(sv)}`;
      }
      paint(idx);
    };

    canvas.onmouseleave = () => {
      if (readout) readout.textContent = "Hover curve to inspect depot value & profit";
      paint(null);
    };
  }

  function renderActiveSlots() {
    const tbody = document.getElementById("depot-slots-tbody");
    const subtitle = document.getElementById("depot-slots-subtitle");
    const win = getActiveWindow();
    if (!tbody || !win) return;

    const scale = depotState.capital / (depotState.data.initialCapital || 10000);
    const currentDepotVal = win.finalValue10k * scale;
    const slotVal = currentDepotVal / 6;

    const activeSlots =
      (depotState.alerts &&
        Array.isArray(depotState.alerts.activeSlots) &&
        depotState.alerts.activeSlots.length > 0
        ? depotState.alerts.activeSlots
        : depotState.data.currentDepot?.activeSlots || win.activeSlots) || [];

    const asOf =
      depotState.alerts?.lastTradingDate ||
      depotState.data.currentDepot?.asOfDate ||
      win.endDate;

    if (subtitle) {
      subtitle.textContent = `Marked to market as of ${asOf} · Each equal slot = ${fmtMoney(slotVal)} (16.7% of ${fmtMoney(currentDepotVal)} live depot). Click any stock row for full valuation & RSI chart.`;
    }

    const rowsHtml = [];
    activeSlots.forEach((s, i) => {
      const cur = s.currencySymbol || "$";
      const pnlCls = s.pnlPct > 0 ? "val-good" : s.pnlPct < 0 ? "val-bad" : "";
      const verdictBadge =
        s.verdict === "Strong Buy"
          ? `<span class="dip-pill dip-pullback">Strong Buy</span>`
          : `<span class="dip-pill dip-prime_dip">${esc(s.verdict || "Buy")}</span>`;

      const holdEligible = (s.tradingDaysHeld || 0) >= 10;
      const holdTag = holdEligible
        ? `<span style="display:block;font-size:0.69rem;color:var(--text-muted)">Exit-ready</span>`
        : `<span style="display:block;font-size:0.69rem;color:var(--sig-hold-fg)">Min ${10 - (s.tradingDaysHeld || 0)}d left</span>`;

      const rsi14 = Number(s.currentRsi14 ?? 50);
      const rsi5 = Number(s.currentRsi5 ?? 50);
      const sc = Number(s.currentScore ?? 70);

      const r14Cls = rsi14 >= 65 ? "val-warn" : rsi14 <= 38 ? "val-good" : "";
      const r5Cls = rsi5 >= 70 ? "val-warn" : rsi5 <= 30 ? "val-good" : "";
      const scCls = sc <= 55 ? "val-bad" : sc >= 75 ? "val-good" : "";

      rowsHtml.push(`
        <tr data-slot-sym="${esc(s.symbol)}" style="cursor:pointer;" title="Click to inspect ${esc(s.symbol)} deep-dive chart &amp; multiples">
          <td class="tabular" style="font-weight:700;color:var(--text-muted);">#${i + 1}</td>
          <td>
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
              <strong class="tabular" style="font-size:0.9rem;">${esc(s.symbol)}</strong>
              ${verdictBadge}
            </div>
            <div style="font-size:0.75rem;color:var(--text-muted);">${esc(s.name)} · ${esc(s.sector)}</div>
          </td>
          <td class="num tabular">
            <strong>${fmtMoney(slotVal)}</strong>
            <span style="display:block;font-size:0.7rem;color:var(--text-muted);">16.7% weight</span>
          </td>
          <td class="num tabular">
            <strong>${fmtPrice(s.entryPrice, cur)}</strong>
            <span style="display:block;font-size:0.71rem;color:var(--text-muted);">${esc(s.entryDate)} (RSI ${Number(s.entryRsi14 || 0).toFixed(0)})</span>
          </td>
          <td class="num tabular">
            <strong>${fmtPrice(s.currentPrice, cur)}</strong>
            <span style="display:block;font-size:0.71rem;color:var(--text-muted);">${s.forwardPE ? `Fwd P/E ${Number(s.forwardPE).toFixed(1)}x` : "S&P 100"}</span>
          </td>
          <td class="num tabular ${pnlCls}" style="font-weight:700;">
            ${fmtSignedPct(s.pnlPct, 2)}
          </td>
          <td class="num tabular">
            <strong>${s.tradingDaysHeld}d</strong>
            ${holdTag}
          </td>
          <td>
            <div class="exit-meter-wrap">
              <span class="exit-meter-pill ${r14Cls}">14d RSI <b>${rsi14.toFixed(1)}</b> / 72</span>
              <span class="exit-meter-pill ${r5Cls}">5d RSI <b>${rsi5.toFixed(1)}</b> / 78</span>
              <span class="exit-meter-pill ${scCls}">Score <b>${sc}</b> / 50</span>
            </div>
          </td>
        </tr>
      `);
    });

    for (let k = activeSlots.length; k < 6; k++) {
      rowsHtml.push(`
        <tr>
          <td class="tabular" style="font-weight:700;color:var(--text-muted);">#${k + 1}</td>
          <td>
            <strong class="tabular">SPY (Parked)</strong>
            <div style="font-size:0.75rem;color:var(--text-muted);">S&amp;P 500 Index ETF — awaiting next S&amp;P 100 ⚡ Prime Dip</div>
          </td>
          <td class="num tabular"><strong>${fmtMoney(slotVal)}</strong><span style="display:block;font-size:0.7rem;color:var(--text-muted);">16.7% weight</span></td>
          <td class="num tabular">–</td>
          <td class="num tabular">–</td>
          <td class="num tabular">Market</td>
          <td class="num tabular">–</td>
          <td><span class="exit-meter-pill">Auto-rotates into next qualifying S&amp;P 100 Prime Dip</span></td>
        </tr>
      `);
    }

    tbody.innerHTML = rowsHtml.join("");

    tbody.querySelectorAll("tr[data-slot-sym]").forEach((row) => {
      row.addEventListener("click", () => {
        const sym = row.getAttribute("data-slot-sym");
        if (sym && typeof window.openStockDrawer === "function") {
          window.openStockDrawer(sym);
        }
      });
    });
  }

  function renderTradesAndYearly() {
    const strip = document.getElementById("depot-yearly-strip");
    const tbody = document.getElementById("depot-trades-tbody");
    const win = getActiveWindow();
    if (!win) return;

    if (strip && Array.isArray(win.yearly)) {
      strip.innerHTML = win.yearly
        .map((y) => {
          const cls = y.depotPct >= y.spyPct ? "val-good" : "val-muted";
          return `<div class="exit-meter-pill" style="padding:5px 9px;">
            <strong>${esc(y.year)}:</strong>
            <span class="${cls}" style="font-weight:700;">${fmtSignedPct(y.depotPct, 1)}</span>
            <span style="color:var(--text-muted);">vs SPY ${fmtSignedPct(y.spyPct, 1)}</span>
          </div>`;
        })
        .join("");
    }

    if (tbody) {
      const trades = win.recentClosedTrades || depotState.data.currentDepot?.recentClosedTrades || [];
      tbody.innerHTML = trades
        .map((t) => {
          const cls = t.returnPct > 0 ? "val-good" : t.returnPct < 0 ? "val-bad" : "";
          return `<tr>
            <td>
              <strong class="tabular">${esc(t.symbol)}</strong>
              <span style="display:block;font-size:0.71rem;color:var(--text-muted);">${esc(t.sector || "")}</span>
            </td>
            <td class="tabular" style="font-size:0.76rem;">
              <div>${esc(t.entryDate)} (${fmtPrice(t.entryPrice)})</div>
              <div style="color:var(--text-muted);">&rarr; ${esc(t.exitDate)} (${fmtPrice(t.exitPrice)})</div>
            </td>
            <td class="num tabular">${t.daysHeld}d</td>
            <td class="num tabular ${cls}" style="font-weight:700;">${fmtSignedPct(t.returnPct, 2)}</td>
            <td style="font-size:0.74rem;color:var(--text-secondary);">${esc(t.reason)}</td>
          </tr>`;
        })
        .join("");
    }
  }

  function renderEmailPreview() {
    const frame = document.getElementById("depot-email-preview");
    if (!frame) return;
    if (depotState.alerts && depotState.alerts.latestEmailPreviewHtml) {
      frame.innerHTML = depotState.alerts.latestEmailPreviewHtml;
    } else {
      frame.innerHTML = `<p style="margin:0;font-size:0.8rem;color:#475569;">Alert preview generated on each scheduled GitHub Actions check.</p>`;
    }
  }

  function renderAllDepot() {
    if (!depotState.data) return;
    renderDepotKpis();
    renderActiveSlots();
    renderTradesAndYearly();
    renderEmailPreview();
    const depotRoot = document.getElementById("depot-view-root");
    if (depotRoot && !depotRoot.classList.contains("hidden-panel")) {
      requestAnimationFrame(drawDepotEquityChart);
    }
  }

  window.renderFourRuleDepot = renderAllDepot;
  window.drawFourRuleChart = drawDepotEquityChart;

  async function initDepotUi() {
    const capInput = document.getElementById("depot-capital-input");
    if (capInput) {
      capInput.addEventListener("input", () => {
        const val = parseFloat(capInput.value);
        if (val >= 100 && val <= 1e9) {
          depotState.capital = val;
          document.querySelectorAll("#depot-cap-pills .pill").forEach((b) => {
            b.classList.toggle("active", Number(b.dataset.cap) === val);
          });
          renderDepotKpis();
          renderActiveSlots();
          drawDepotEquityChart();
        }
      });
    }

    document.querySelectorAll("#depot-cap-pills .pill").forEach((btn) => {
      btn.addEventListener("click", () => {
        const val = Number(btn.dataset.cap || 10000);
        depotState.capital = val;
        if (capInput) capInput.value = String(val);
        document
          .querySelectorAll("#depot-cap-pills .pill")
          .forEach((b) => b.classList.toggle("active", b === btn));
        renderDepotKpis();
        renderActiveSlots();
        drawDepotEquityChart();
      });
    });

    document.querySelectorAll("#depot-window-pills .pill").forEach((btn) => {
      btn.addEventListener("click", () => {
        depotState.windowKey = btn.dataset.win || "4y";
        document
          .querySelectorAll("#depot-window-pills .pill")
          .forEach((b) => b.classList.toggle("active", b === btn));
        renderAllDepot();
      });
    });

    // Fetch fresh JSON snapshots when served over HTTP/HTTPS
    try {
      const [dRes, aRes] = await Promise.all([
        fetch(`./data/four_rule_depot.json?t=${Date.now()}`, { cache: "no-store" }),
        fetch(`./data/alert_state.json?t=${Date.now()}`, { cache: "no-store" }),
      ]);
      if (dRes.ok) depotState.data = await dRes.json();
      if (aRes.ok) depotState.alerts = await aRes.json();
    } catch (_) {}

    renderAllDepot();
  }

  initDepotUi();
})();
