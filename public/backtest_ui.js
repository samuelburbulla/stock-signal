/**
 * Stock Signal — Fictitious Depot Historical Backtest Renderer
 */
(function () {
  "use strict";

  const btState = {
    data: window.__BACKTEST_DATA__ || null,
    universe: "sp100_2021", // 'sp100_2021' | 'nyse_2021' | 'watchlist_2026'
    horizon: "5y", // '5y' | '3y' | '1y'
    capital: 10000,
    activeTab: "screener",
  };

  function getActiveUniv() {
    if (!btState.data) return null;
    if (btState.data.universes && btState.data.universes[btState.universe]) {
      return btState.data.universes[btState.universe];
    }
    return btState.data;
  }

  function fmtCurrency(val) {
    return "$" + Math.round(val).toLocaleString("en-US");
  }

  function fmtSignedPct(val, digits = 1) {
    if (val === null || val === undefined || Number.isNaN(val)) return "–";
    const n = Number(val);
    return `${n > 0 ? "+" : ""}${n.toFixed(digits)}%`;
  }

  function esc(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function getSlicedSeries() {
    const u = getActiveUniv();
    const all = (u && u.series) || [];
    if (!all.length) return [];
    const monthsBack = btState.horizon === "1y" ? 12 : btState.horizon === "3y" ? 36 : 60;
    const startIdx = Math.max(0, all.length - 1 - monthsBack);
    const base = all[startIdx];
    const cap = btState.capital;
    return all.slice(startIdx).map((pt) => ({
      date: pt.date,
      dailyDip:
        pt.dailyDip && base.dailyDip
          ? (pt.dailyDip / base.dailyDip) * cap
          : (pt.allBuys / base.allBuys) * cap,
      allBuys: (pt.allBuys / base.allBuys) * cap,
      strongBuy: (pt.strongBuy / base.strongBuy) * cap,
      equalUniv: (pt.equalUniv / base.equalUniv) * cap,
      spy: (pt.spy / base.spy) * cap,
      avoid: (pt.avoid / base.avoid) * cap,
    }));
  }

  function renderKpis() {
    const grid = document.getElementById("bt-kpi-grid");
    const u = getActiveUniv();
    if (!grid || !u) return;
    const h = u.horizons[btState.horizon];
    if (!h) return;
    const scale = btState.capital / 10000.0;

    const metaEl = document.getElementById("bt-universe-meta");
    if (metaEl) {
      metaEl.textContent = `${u.label || "Historical Cohort"} (${u.universeCount || "–"} stocks) · ${u.badge || ""} · Dividends reinvested · 0.10% trading fee`;
    }

    const bestSignalVal = Math.max(
      h.dailyDip ? h.dailyDip.finalValue10k : 0,
      h.allBuys.finalValue10k,
      h.strongBuy.finalValue10k
    );

    const cards = [];
    if (h.dailyDip) {
      cards.push({
        title: "⚡ Daily Dip-Buyer Depot",
        featured: h.dailyDip.finalValue10k === bestSignalVal,
        st: h.dailyDip,
        dot: "var(--sig-dip-dot)",
      });
    }
    cards.push(
      {
        title: "All Buys Depot (Monthly)",
        featured: h.allBuys.finalValue10k === bestSignalVal && (!h.dailyDip || h.dailyDip.finalValue10k < bestSignalVal),
        st: h.allBuys,
        dot: "var(--accent)",
      },
      {
        title: "Strong Buy Only Depot",
        featured: h.strongBuy.finalValue10k === bestSignalVal && h.allBuys.finalValue10k < bestSignalVal && (!h.dailyDip || h.dailyDip.finalValue10k < bestSignalVal),
        st: h.strongBuy,
        dot: "var(--sig-strong-dot)",
      },
      {
        title: `Equal-Weight Bucket (${u.universeCount || ""} Stocks)`,
        featured: false,
        st: h.equalUniverse,
        dot: "var(--sig-hold-dot)",
      },
      {
        title: "S&P 500 (SPY Total Return)",
        featured: false,
        st: h.spy,
        dot: "var(--text-muted)",
      }
    );

    grid.innerHTML = cards
      .map((c) => {
        const finalVal = c.st.finalValue10k * scale;
        const profit = finalVal - btState.capital;
        const retCls = c.st.totalReturnPct >= 0 ? "val-good" : "val-bad";
        return `<div class="bt-kpi-card ${c.featured ? "featured" : ""}">
          <div class="bt-kpi-title">
            <span style="display:inline-flex;align-items:center;gap:6px;">
              <span class="bucket-dot" style="background:${c.dot}"></span>${esc(c.title)}
            </span>
          </div>
          <div class="bt-kpi-value tabular">${fmtCurrency(finalVal)}</div>
          <div class="bt-kpi-sub tabular">
            <span class="${retCls}">${fmtSignedPct(c.st.totalReturnPct)}</span> (${profit >= 0 ? "+" : "-"}${fmtCurrency(Math.abs(profit))})
          </div>
          <div class="bt-kpi-sub tabular">
            CAGR ${fmtSignedPct(c.st.cagrPct, 2)}/yr · Sharpe ${c.st.sharpe.toFixed(2)} · MaxDD ${c.st.maxDrawdownPct.toFixed(1)}%
          </div>
        </div>`;
      })
      .join("");
  }

  function drawEquityChart() {
    const canvas = document.getElementById("bt-equity-canvas");
    const readout = document.getElementById("bt-chart-readout");
    if (!canvas || !getActiveUniv()) return;

    const pts = getSlicedSeries();
    if (pts.length < 2) return;

    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.parentElement.getBoundingClientRect();
    const width = Math.max(300, Math.floor(rect.width - 40));
    const height = 270;

    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;

    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);

    const styles = getComputedStyle(document.documentElement);
    const textMuted = styles.getPropertyValue("--text-muted").trim() || "#667085";
    const borderCol = styles.getPropertyValue("--border-subtle").trim() || "#e4e7ec";
    const colDip = styles.getPropertyValue("--sig-dip-dot").trim() || "#7c3aed";
    const colAllBuys = styles.getPropertyValue("--accent").trim() || "#0f766e";
    const colStrong = styles.getPropertyValue("--sig-strong-dot").trim() || "#16a34a";
    const colUniv = styles.getPropertyValue("--sig-hold-dot").trim() || "#d97706";
    const colSpy = textMuted;

    const allVals = [];
    pts.forEach((p) => {
      allVals.push(p.dailyDip, p.allBuys, p.strongBuy, p.equalUniv, p.spy);
    });
    const minV = Math.min(...allVals) * 0.94;
    const maxV = Math.max(...allVals) * 1.04;
    const span = maxV - minV || 1;

    const padL = 8;
    const padR = 68;
    const padT = 12;
    const padB = 24;
    const plotW = width - padL - padR;
    const plotH = height - padT - padB;

    const xOf = (i) => padL + (i / (pts.length - 1)) * plotW;
    const yOf = (v) => padT + plotH - ((v - minV) / span) * plotH;

    function drawLine(key, color, lineW, dashed = false) {
      ctx.save();
      if (dashed) ctx.setLineDash([4, 4]);
      ctx.strokeStyle = color;
      ctx.lineWidth = lineW;
      ctx.beginPath();
      pts.forEach((p, idx) => {
        if (idx === 0) ctx.moveTo(xOf(idx), yOf(p[key]));
        else ctx.lineTo(xOf(idx), yOf(p[key]));
      });
      ctx.stroke();
      ctx.restore();
    }

    function paint(hoverIdx = null) {
      ctx.clearRect(0, 0, width, height);

      // Grid lines + right-axis dollar labels
      ctx.strokeStyle = borderCol;
      ctx.lineWidth = 1;
      ctx.fillStyle = textMuted;
      ctx.font = "11px 'JetBrains Mono', monospace";
      ctx.textAlign = "left";

      for (let g = 0; g <= 4; g++) {
        const val = minV + (g / 4) * span;
        const y = Math.round(yOf(val)) + 0.5;
        ctx.beginPath();
        ctx.moveTo(padL, y);
        ctx.lineTo(padL + plotW, y);
        ctx.stroke();
        ctx.fillText(fmtCurrency(val), padL + plotW + 6, y + 3);
      }

      // X-axis dates
      const step = Math.max(1, Math.floor((pts.length - 1) / 5));
      for (let i = 0; i < pts.length; i += step) {
        ctx.textAlign = i === 0 ? "left" : "center";
        ctx.fillText(pts[i].date.slice(0, 7), xOf(i), height - 6);
      }
      ctx.textAlign = "right";
      ctx.fillText(pts[pts.length - 1].date.slice(0, 7), padL + plotW, height - 6);

      // Draw benchmark & strategy lines
      drawLine("spy", colSpy, 1.6, true);
      drawLine("equalUniv", colUniv, 1.6, false);
      drawLine("strongBuy", colStrong, 2.0, false);
      drawLine("allBuys", colAllBuys, 2.4, false);
      drawLine("dailyDip", colDip, 2.8, false);

      if (hoverIdx !== null && pts[hoverIdx]) {
        const hp = pts[hoverIdx];
        const hx = xOf(hoverIdx);
        ctx.save();
        ctx.strokeStyle = textMuted;
        ctx.setLineDash([2, 2]);
        ctx.beginPath();
        ctx.moveTo(hx, padT);
        ctx.lineTo(hx, padT + plotH);
        ctx.stroke();
        ctx.restore();

        [
          ["dailyDip", colDip],
          ["allBuys", colAllBuys],
          ["strongBuy", colStrong],
          ["equalUniv", colUniv],
          ["spy", colSpy],
        ].forEach(([k, col]) => {
          ctx.beginPath();
          ctx.arc(hx, yOf(hp[k]), 3.8, 0, Math.PI * 2);
          ctx.fillStyle = col;
          ctx.fill();
        });
      }
    }

    paint();

    canvas.onmousemove = (ev) => {
      const r = canvas.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (ev.clientX - r.left - padL) / plotW));
      const idx = Math.round(ratio * (pts.length - 1));
      const pt = pts[idx];
      if (pt && readout) {
        readout.textContent = `${pt.date} · ⚡ Daily Dip ${fmtCurrency(pt.dailyDip)} · All Buys ${fmtCurrency(
          pt.allBuys
        )} · Eq. Bucket ${fmtCurrency(pt.equalUniv)} · SPY ${fmtCurrency(pt.spy)}`;
      }
      paint(idx);
    };

    canvas.onmouseleave = () => {
      if (readout) readout.textContent = "Hover chart to inspect monthly depot value";
      paint(null);
    };
  }

  function renderYearlyAndCheckpoints() {
    const u = getActiveUniv();
    if (!u) return;
    const tbody = document.getElementById("bt-yearly-tbody");
    if (tbody) {
      tbody.innerHTML = (u.yearly || [])
        .map((y) => {
          const cls = (v) => (v > 0 ? "val-good" : v < 0 ? "val-bad" : "");
          return `<tr>
            <td><strong>${esc(y.year)}</strong></td>
            <td class="num tabular ${cls(y.dailyDipPct)}"><strong>${fmtSignedPct(y.dailyDipPct)}</strong></td>
            <td class="num tabular ${cls(y.allBuysPct)}">${fmtSignedPct(y.allBuysPct)}</td>
            <td class="num tabular ${cls(y.strongBuyPct)}">${fmtSignedPct(y.strongBuyPct)}</td>
            <td class="num tabular ${cls(y.equalUnivPct)}">${fmtSignedPct(y.equalUnivPct)}</td>
            <td class="num tabular ${cls(y.spyPct)}">${fmtSignedPct(y.spyPct)}</td>
            <td class="num tabular ${cls(y.avoidBasketPct)}">${fmtSignedPct(y.avoidBasketPct)}</td>
          </tr>`;
        })
        .join("");
    }

    const noteEl = document.getElementById("bt-methodology-note");
    if (noteEl) {
      noteEl.textContent = u.methodology || "";
    }

    const cpList = document.getElementById("bt-checkpoints-list");
    if (cpList) {
      cpList.innerHTML = (u.checkpoints || [])
        .map((cp) => {
          const diff = cp.depotReturnPct - cp.spyReturnPct;
          const cls = diff >= 0 ? "val-good" : "val-bad";
          const chips = (cp.topHoldings || [])
            .map(
              (h) =>
                `<span class="bt-hold-chip">${esc(h.symbol)} · Sc ${h.score}${
                  h.pe !== null ? ` · P/E ${h.pe}x` : ""
                }</span>`
            )
            .join("");
          const avoided = (cp.avoidedExamples || []).map((a) => a.symbol).join(", ");
          return `<div class="bt-cp-item">
            <div class="bt-cp-head tabular">
              <span>Rebalance ${esc(cp.date)} (${cp.holdingsCount} stocks)</span>
              <span class="${cls}">Next 1M: ${fmtSignedPct(cp.depotReturnPct)} vs SPY ${fmtSignedPct(cp.spyReturnPct)}</span>
            </div>
            <div class="bt-chip-row">${chips}</div>
            <div style="margin-top:5px;font-size:0.72rem;color:var(--text-muted);">Avoided on valuation: ${esc(avoided)}</div>
          </div>`;
        })
        .join("");
    }
  }

  function renderBacktestAll() {
    renderKpis();
    renderYearlyAndCheckpoints();
    if (btState.activeTab === "backtest") {
      requestAnimationFrame(drawEquityChart);
    }
  }

  function resolveTabFromHash(hash) {
    if (hash === "#backtest") return "backtest";
    if (hash === "#depot" || hash === "#alerts" || hash === "#email-alerts-box") return "depot";
    return "screener";
  }

  function switchMainTab(tab) {
    btState.activeTab = tab;
    const isScreener = tab === "screener";
    const isDepot = tab === "depot";
    const isBt = tab === "backtest";

    const screenerEl = document.getElementById("screener-view-root");
    const depotEl = document.getElementById("depot-view-root");
    const btEl = document.getElementById("backtest-view-root");
    if (screenerEl) screenerEl.classList.toggle("hidden-panel", !isScreener);
    if (depotEl) depotEl.classList.toggle("hidden-panel", !isDepot);
    if (btEl) btEl.classList.toggle("hidden-panel", !isBt);

    const navScreener = document.getElementById("nav-tab-screener");
    const navDepot = document.getElementById("nav-tab-depot");
    const navBt = document.getElementById("nav-tab-backtest");
    if (navScreener) navScreener.classList.toggle("active", isScreener);
    if (navDepot) navDepot.classList.toggle("active", isDepot);
    if (navBt) navBt.classList.toggle("active", isBt);

    if (isBt) {
      requestAnimationFrame(drawEquityChart);
    } else if (isDepot && typeof window.renderFourRuleDepot === "function") {
      requestAnimationFrame(() => window.renderFourRuleDepot());
    }
  }
  window.switchMainTab = switchMainTab;

  async function initBacktest() {
    document.getElementById("nav-tab-screener").addEventListener("click", () => {
      history.replaceState(null, "", "#screener");
      switchMainTab("screener");
    });
    const navDepotBtn = document.getElementById("nav-tab-depot");
    if (navDepotBtn) {
      navDepotBtn.addEventListener("click", () => {
        history.replaceState(null, "", "#depot");
        switchMainTab("depot");
      });
    }
    document.getElementById("nav-tab-backtest").addEventListener("click", () => {
      history.replaceState(null, "", "#backtest");
      switchMainTab("backtest");
    });

    document.querySelectorAll("#bt-universe-pills .pill").forEach((btn) => {
      btn.addEventListener("click", () => {
        btState.universe = btn.dataset.universe || "sp100_2021";
        document
          .querySelectorAll("#bt-universe-pills .pill")
          .forEach((b) => b.classList.toggle("active", b === btn));
        renderBacktestAll();
      });
    });

    document.querySelectorAll("#bt-horizon-pills .pill").forEach((btn) => {
      btn.addEventListener("click", () => {
        btState.horizon = btn.dataset.horizon || "5y";
        document
          .querySelectorAll("#bt-horizon-pills .pill")
          .forEach((b) => b.classList.toggle("active", b === btn));
        renderBacktestAll();
      });
    });

    const capInput = document.getElementById("bt-capital-input");
    if (capInput) {
      capInput.addEventListener("input", () => {
        const val = parseFloat(capInput.value);
        if (val >= 100 && val <= 1e9) {
          btState.capital = val;
          renderKpis();
          drawEquityChart();
        }
      });
    }

    document.getElementById("theme-toggle-btn").addEventListener("click", () => {
      if (btState.activeTab === "backtest") {
        requestAnimationFrame(drawEquityChart);
      } else if (btState.activeTab === "depot" && typeof window.drawFourRuleChart === "function") {
        requestAnimationFrame(() => window.drawFourRuleChart());
      }
    });

    window.addEventListener("resize", () => {
      if (btState.activeTab === "backtest") drawEquityChart();
      if (btState.activeTab === "depot" && typeof window.drawFourRuleChart === "function") {
        window.drawFourRuleChart();
      }
    });

    // Always fetch fresh backtest.json when available so latest multi-universe payload loads
    try {
      const r = await fetch(`./data/backtest.json?t=${Date.now()}`, { cache: "no-store" });
      if (r.ok) btState.data = await r.json();
    } catch (_) {}

    renderBacktestAll();
    const initialTab = resolveTabFromHash(window.location.hash);
    if (initialTab !== "screener") {
      switchMainTab(initialTab);
      if (window.location.hash === "#email-alerts-box" || window.location.hash === "#alerts") {
        setTimeout(() => {
          const box = document.getElementById("email-alerts-box");
          if (box) box.scrollIntoView({ behavior: "smooth", block: "start" });
        }, 120);
      }
    }
    window.addEventListener("hashchange", () => {
      const nextTab = resolveTabFromHash(window.location.hash);
      switchMainTab(nextTab);
      if (window.location.hash === "#email-alerts-box" || window.location.hash === "#alerts") {
        const box = document.getElementById("email-alerts-box");
        if (box) box.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  }

  initBacktest();
})();
