/**
 * Stock Signal — Frontend Screener, Reasonable Price Range Renderer & Deep-Dive Valuation Inspector
 */
(function () {
  "use strict";

  const state = {
    payload: window.__INITIAL_STOCK_DATA__ || { counts: {}, stocks: [] },
    verdictFilter: "all",
    preset: "all",
    sectorFilter: "all",
    searchQuery: "",
    sortKey: "score",
    sortDir: "desc",
    viewMode: "table", // 'table' | 'cards'
    selectedSymbol: null,
    highlightedSymbol: null,
    hasLiveBackend: false,
  };

  // --- Formatting Helpers ---
  function fmtMoneyCompact(val, sym = "$") {
    if (val === null || val === undefined || Number.isNaN(val)) return "–";
    const sign = val < 0 ? "-" : "";
    const abs = Math.abs(val);
    if (abs >= 1e12) return `${sign}${sym}${(abs / 1e12).toFixed(2)}T`;
    if (abs >= 1e9) return `${sign}${sym}${(abs / 1e9).toFixed(1)}B`;
    if (abs >= 1e6) return `${sign}${sym}${(abs / 1e6).toFixed(0)}M`;
    return `${sign}${sym}${abs.toFixed(0)}`;
  }

  function fmtPrice(val, sym = "$") {
    if (val === null || val === undefined || Number.isNaN(val)) return "–";
    return `${sym}${Number(val).toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
  }

  function fmtRangePrice(val, sym = "$") {
    if (val === null || val === undefined || Number.isNaN(val)) return "–";
    const n = Number(val);
    if (n >= 50) {
      return `${sym}${Math.round(n).toLocaleString("en-US")}`;
    }
    return `${sym}${n.toFixed(2)}`;
  }

  function fmtMult(val) {
    if (val === null || val === undefined || Number.isNaN(val)) return "–";
    if (val > 250) return ">250x";
    if (val < -99) return "Neg";
    return `${Number(val).toFixed(1)}x`;
  }

  function fmtPeg(val) {
    if (val === null || val === undefined || Number.isNaN(val)) return "–";
    if (val <= 0) return "Neg";
    if (val > 15) return ">15x";
    return `${Number(val).toFixed(2)}x`;
  }

  function fmtPct(val, signed = false, digits = 1) {
    if (val === null || val === undefined || Number.isNaN(val)) return "–";
    const num = Number(val);
    const prefix = signed && num > 0 ? "+" : "";
    return `${prefix}${num.toFixed(digits)}%`;
  }

  function esc(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // --- Semantic Color Classifiers for Quick Scanning ---
  function clsForwardPE(pe, sector) {
    if (pe === null || pe === undefined) return "val-muted";
    if (pe <= 0) return "val-bad";
    const isFin = sector === "Financial Services" || sector === "Real Estate";
    if (pe <= (isFin ? 13.0 : 17.0)) return "val-good";
    if (pe >= (isFin ? 23.0 : 33.0)) return "val-bad";
    return "";
  }

  function clsPeg(peg) {
    if (peg === null || peg === undefined) return "val-muted";
    if (peg <= 0 || peg >= 2.4) return "val-bad";
    if (peg <= 1.15) return "val-good";
    return "";
  }

  function clsFcf(fcf) {
    if (fcf === null || fcf === undefined) return "val-muted";
    if (fcf >= 3.8) return "val-good";
    if (fcf < 1.0) return "val-bad";
    return "";
  }

  function clsRsi(rsi) {
    if (rsi === null || rsi === undefined) return "val-muted";
    if (rsi <= 37) return "val-good";
    if (rsi >= 70) return "val-bad";
    return "";
  }

  function rsiTag(rsi) {
    if (rsi === null || rsi === undefined) return "";
    if (rsi <= 35) return "Oversold";
    if (rsi <= 42) return "Pullback";
    if (rsi >= 70) return "Overbought";
    return "Neutral";
  }

  function clsUpside(up) {
    if (up === null || up === undefined) return "val-muted";
    if (up >= 18) return "val-good";
    if (up < 0) return "val-bad";
    return "";
  }

  function shortVerdictLabel(verdictKey) {
    if (verdictKey === "strong_buy") return "Strong Buy";
    if (verdictKey === "buy") return "Good Buy";
    if (verdictKey === "hold") return "Hold";
    return "Avoid";
  }

  // --- SVG Sparkline Generator ---
  function buildSparklineSvg(chart, width = 76, height = 24) {
    if (!Array.isArray(chart) || chart.length < 3) {
      return `<span class="val-muted">–</span>`;
    }
    const vals = chart.map((p) => p.c);
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const span = max - min || 1;
    const pts = vals
      .map((v, idx) => {
        const x = ((idx / (vals.length - 1)) * (width - 4) + 2).toFixed(1);
        const y = (height - 3 - ((v - min) / span) * (height - 6)).toFixed(1);
        return `${x},${y}`;
      })
      .join(" ");
    const isUp = vals[vals.length - 1] >= vals[0];
    const stroke = isUp ? "var(--sig-strong-dot)" : "var(--sig-avoid-dot)";
    return `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" aria-hidden="true">
      <polyline fill="none" stroke="${stroke}" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" points="${pts}" />
    </svg>`;
  }

  const CLIENT_ALIASES = {
    SPX: "SPY",
    "^SPX": "SPY",
    "^GSPC": "SPY",
    GSPC: "SPY",
    SP500: "SPY",
    "S&P500": "SPY",
    SPACEX: "SPCX",
    ANTHROPIC: "ANT",
  };

  function renderDipBadge(s) {
    const dk = s.dipKey || "neutral";
    const lbl = s.dipLabel || "Neutral";
    const rsn = s.dipReason || "";
    return `<span class="dip-pill dip-${esc(dk)}" title="${esc(rsn)}">${esc(lbl)}</span>`;
  }

  function renderPriceRangeCell(s) {
    const cur = s.currencySymbol || "$";
    if (s.cheapPrice == null || s.fairPrice == null || s.expensivePrice == null) {
      return `<span class="val-muted">–</span>`;
    }
    const zone = s.valuationZone || "fair";
    const vs = Number(s.vsFairPct || 0);
    const vsTxt = `${vs > 0 ? "+" : ""}${Math.round(vs)}%`;
    const zoneLbl = zone === "cheap" ? `Cheap (${vsTxt})` : zone === "expensive" ? `Expensive (${vsTxt})` : `Fair (${vsTxt})`;
    const pos = Math.max(3, Math.min(97, Number(s.rangePosValuation ?? 50)));
    const tip = `${s.rangeBasis || ""} · Cheap ≤ ${fmtRangePrice(s.cheapPrice, cur)} · Fair ~${fmtRangePrice(s.fairPrice, cur)} · Expensive ≥ ${fmtRangePrice(s.expensivePrice, cur)}`;

    return `<div class="price-range-cell" title="${esc(tip)}">
      <div class="price-range-top tabular">
        <span><b class="val-good">&le;${fmtRangePrice(s.cheapPrice, cur)}</b> <span class="val-muted">&ndash;</span> <b class="val-bad">&ge;${fmtRangePrice(s.expensivePrice, cur)}</b></span>
        <span class="zone-pill zone-${esc(zone)}">${esc(zoneLbl)}</span>
      </div>
      <div class="val-zone-bar">
        <span class="val-zone-seg seg-cheap"></span>
        <span class="val-zone-seg seg-fair"></span>
        <span class="val-zone-seg seg-exp"></span>
        <span class="val-zone-fair-tick"></span>
        <span class="val-zone-dot dot-${esc(zone)}" style="left:${pos}%"></span>
      </div>
      <div class="price-range-sub tabular">
        <span>Fair ~${fmtRangePrice(s.fairPrice, cur)}</span>
        ${s.shockAlert && s.shockAlert.active ? `<span class="val-bad">shock haircut</span>` : ""}
      </div>
    </div>`;
  }

  // --- Filtering & Sorting ---
  function getFilteredStocks() {
    const all = state.payload.stocks || [];
    const q = state.searchQuery.trim().toLowerCase();
    const aliasSym = q ? CLIENT_ALIASES[q.toUpperCase()] || null : null;

    const filtered = all.filter((s) => {
      if (state.verdictFilter === "prime_dip") {
        if (s.dipKey !== "prime_dip") return false;
      } else if (state.verdictFilter !== "all" && s.verdictKey !== state.verdictFilter) {
        return false;
      }
      if (state.sectorFilter !== "all" && s.sector !== state.sectorFilter) {
        return false;
      }
      if (state.preset === "top_buys" && !(s.verdictKey === "strong_buy" || s.verdictKey === "buy")) {
        return false;
      }
      if (state.preset === "cheap_zone" && s.valuationZone !== "cheap") {
        return false;
      }
      if (state.preset === "oversold_dips" && !((s.dipKey === "prime_dip" || s.dipKey === "pullback") && !s.isFallingKnife)) {
        return false;
      }
      if (state.preset === "knives_and_expensive" && !(s.isFallingKnife || s.valuationZone === "expensive" || s.verdictKey === "avoid")) {
        return false;
      }
      if (q) {
        if (aliasSym && s.symbol === aliasSym) return true;
        const hay = `${s.symbol} ${s.name} ${s.longName || ""} ${s.sector || ""} ${s.industry || ""} ${s.dipLabel || ""} ${s.valuationZoneLabel || ""}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    const key = state.sortKey;
    const dir = state.sortDir === "asc" ? 1 : -1;

    filtered.sort((a, b) => {
      const va = a[key];
      const vb = b[key];
      if (va === null || va === undefined) return 1;
      if (vb === null || vb === undefined) return -1;
      if (typeof va === "string") {
        return va.localeCompare(vb) * dir;
      }
      return (va - vb) * dir;
    });

    return filtered;
  }

  // --- UI Banner ---
  let bannerTimer = null;
  function showBanner(msg, isError = false, autoHideMs = 5000) {
    const banner = document.getElementById("status-banner");
    const msgEl = document.getElementById("status-banner-msg");
    msgEl.textContent = msg;
    banner.classList.toggle("error", Boolean(isError));
    banner.classList.add("visible");
    if (bannerTimer) clearTimeout(bannerTimer);
    if (autoHideMs > 0) {
      bannerTimer = setTimeout(() => banner.classList.remove("visible"), autoHideMs);
    }
  }

  // --- Render Header & Counts ---
  function renderHeaderAndCounts() {
    const p = state.payload;
    const stocks = p.stocks || [];
    const counts = {
      total: stocks.length,
      strong_buy: stocks.filter((s) => s.verdictKey === "strong_buy").length,
      buy: stocks.filter((s) => s.verdictKey === "buy").length,
      prime_dip: stocks.filter((s) => s.dipKey === "prime_dip").length,
      hold: stocks.filter((s) => s.verdictKey === "hold").length,
      avoid: stocks.filter((s) => s.verdictKey === "avoid").length,
    };

    document.getElementById("count-all").textContent = counts.total;
    document.getElementById("count-strong_buy").textContent = counts.strong_buy;
    document.getElementById("count-buy").textContent = counts.buy;
    const dipCntEl = document.getElementById("count-prime_dip");
    if (dipCntEl) dipCntEl.textContent = counts.prime_dip;
    document.getElementById("count-hold").textContent = counts.hold;
    document.getElementById("count-avoid").textContent = counts.avoid;

    const updatedEl = document.getElementById("last-updated-text");
    if (p.updatedAt) {
      const dt = new Date(p.updatedAt);
      const formatted = dt.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
      updatedEl.textContent = `Daily Auto-Updated · Last synced ${formatted} (${counts.total} stocks)`;
    }

    const sectorSelect = document.getElementById("sector-filter-select");
    const currentSector = state.sectorFilter;
    const sectors = Array.from(new Set(stocks.map((s) => s.sector).filter(Boolean))).sort();
    sectorSelect.innerHTML =
      `<option value="all">All Sectors</option>` +
      sectors
        .map(
          (sec) =>
            `<option value="${esc(sec)}" ${sec === currentSector ? "selected" : ""}>${esc(sec)}</option>`
        )
        .join("");

    document.querySelectorAll(".signal-bucket").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.verdict === state.verdictFilter);
    });
  }

  function renderPillarChips(pillars) {
    if (!pillars) return "";
    const items = [
      ["V", pillars.valuation],
      ["Q", pillars.quality],
      ["G", pillars.growth],
      ["U", pillars.upside],
      ["T", pillars.timing],
    ];
    return `<div class="pillar-strip">
      ${items
        .map(([code, obj]) => {
          const sc = obj ? obj.score : 50;
          const st = obj ? obj.status : "neutral";
          const lbl = obj ? obj.label : code;
          return `<span class="pillar-chip" title="${esc(lbl)}: ${sc}/100">
            <span class="pillar-bar ${esc(st)}"></span>
            <span class="pillar-lbl">${code}</span>
          </span>`;
        })
        .join("")}
    </div>`;
  }

  // --- Render Table & Cards ---
  function renderList() {
    const stocks = getFilteredStocks();
    const tbody = document.getElementById("stock-table-body");
    const cardsGrid = document.getElementById("cards-view-container");
    const tableCard = document.getElementById("table-view-container");

    tableCard.classList.toggle("hidden", state.viewMode !== "table");
    cardsGrid.classList.toggle("active", state.viewMode === "cards");

    document.querySelectorAll("#stock-table thead th.sortable").forEach((th) => {
      const key = th.dataset.sort;
      const baseLabel = th.textContent.replace(/[ ↑↓]+$/, "");
      if (key === state.sortKey) {
        th.classList.add("sorted");
        th.textContent = `${baseLabel} ${state.sortDir === "asc" ? "↑" : "↓"}`;
      } else {
        th.classList.remove("sorted");
        th.textContent = baseLabel;
      }
    });

    if (stocks.length === 0) {
      const emptyHtml = `<div class="empty-state">No stocks match this filter. Try clicking <b>All Tracked</b> or clearing the search box above.</div>`;
      tbody.innerHTML = `<tr><td colspan="12">${emptyHtml}</td></tr>`;
      cardsGrid.innerHTML = emptyHtml;
      return;
    }

    tbody.innerHTML = stocks
      .map((s) => {
        const cur = s.currencySymbol || "$";
        const chgCls = s.changePct > 0.05 ? "val-good" : s.changePct < -0.05 ? "val-bad" : "val-muted";
        const hasFcf = s.fcfYield !== null && s.fcfYield !== undefined;
        const cashCell = hasFcf
          ? `<span class="${clsFcf(s.fcfYield)}">${fmtPct(s.fcfYield)}</span><span class="sub-val">EV/EB ${fmtMult(s.evToEbitda)}</span>`
          : `<span class="val-muted">${fmtMult(s.priceToBook)}</span><span class="sub-val">P/Book</span>`;

        const rowHighlight = state.highlightedSymbol === s.symbol ? "highlighted" : "";

        return `<tr data-symbol="${esc(s.symbol)}" class="${rowHighlight}">
          <td>
            <div class="verdict-cell sig-${esc(s.verdictKey)}">
              <span class="score-badge tabular">${s.score}</span>
              <span class="verdict-pill badge-${esc(s.verdictKey)}">${esc(shortVerdictLabel(s.verdictKey))}</span>
            </div>
          </td>
          <td class="stock-id">
            <div class="stock-id-top">
              <span class="ticker-sym">${esc(s.symbol)}</span>
              <span class="company-name">${esc(s.name)}</span>
            </div>
            <div class="stock-takeaway" title="${esc(s.summary)}">${esc(s.summary)}</div>
          </td>
          <td>${renderPillarChips(s.pillars)}</td>
          <td class="num tabular">
            <strong>${fmtPrice(s.price, cur)}</strong>
            <span class="sub-val ${chgCls}">${fmtPct(s.changePct, true, 2)}</span>
          </td>
          <td>${renderPriceRangeCell(s)}</td>
          <td class="num tabular">
            <span class="${clsForwardPE(s.forwardPE, s.sector)}">${fmtMult(s.forwardPE)}</span>
            <span class="sub-val">TTM ${fmtMult(s.trailingPE)}</span>
          </td>
          <td class="num tabular">
            <span class="${clsPeg(s.pegRatio)}">${fmtPeg(s.pegRatio)}</span>
          </td>
          <td class="num tabular">${cashCell}</td>
          <td class="num tabular">
            <span class="${s.revenueGrowth >= 12 ? "val-good" : s.revenueGrowth < 0 ? "val-bad" : ""}">${fmtPct(s.revenueGrowth, true)}</span>
            <span class="sub-val">EPS ${fmtPct(s.epsGrowth, true)}</span>
          </td>
          <td class="num tabular">
            <span class="${s.profitMargin >= 20 ? "val-good" : s.profitMargin < 5 ? "val-bad" : ""}">${fmtPct(s.profitMargin)}</span>
            <span class="sub-val">ROE ${fmtPct(s.roe, false, 0)}</span>
          </td>
          <td class="num tabular">
            <div>${renderDipBadge(s)}</div>
            <span class="sub-val">RSI ${s.rsi14 !== null && s.rsi14 !== undefined ? s.rsi14.toFixed(0) : "–"}${
              s.dip20dPct !== null && s.dip20dPct !== undefined && s.dip20dPct <= -1.0
                ? ` · ${fmtPct(s.dip20dPct, true, 0)} 20d`
                : ""
            }</span>
          </td>
          <td class="num">
            <div class="range-wrap" title="52W Low ${fmtPrice(s.low52w, cur)} — High ${fmtPrice(s.high52w, cur)}">
              ${buildSparklineSvg(s.chart)}
              <div class="range-track">
                <span class="range-dot" style="left:${Math.round(s.rangePos52w ?? 50)}%"></span>
              </div>
            </div>
          </td>
        </tr>`;
      })
      .join("");

    cardsGrid.innerHTML = stocks
      .map((s) => {
        const cur = s.currencySymbol || "$";
        return `<article class="stock-card" data-symbol="${esc(s.symbol)}">
          <div class="card-head">
            <div>
              <div class="stock-id-top">
                <span class="ticker-sym">${esc(s.symbol)}</span>
                <span class="company-name">${esc(s.name)}</span>
              </div>
              <span class="sub-val">${esc(s.sector)} · ${fmtMoneyCompact(s.marketCap, cur)}</span>
            </div>
            <div class="verdict-cell sig-${esc(s.verdictKey)}">
              <span class="score-badge tabular">${s.score}</span>
              <span class="verdict-pill badge-${esc(s.verdictKey)}">${esc(shortVerdictLabel(s.verdictKey))}</span>
            </div>
          </div>
          <div class="card-summary">${esc(s.summary)}</div>
          <div class="card-range-box">
            ${renderPriceRangeCell(s)}
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;">
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
              ${renderPillarChips(s.pillars)}
              ${renderDipBadge(s)}
            </div>
            ${buildSparklineSvg(s.chart, 84, 24)}
          </div>
          <div class="card-metrics tabular">
            <div>
              <div class="card-metric-lbl">Price</div>
              <div class="card-metric-val">${fmtPrice(s.price, cur)}</div>
            </div>
            <div>
              <div class="card-metric-lbl">Fair Value</div>
              <div class="card-metric-val">${fmtRangePrice(s.fairPrice, cur)}</div>
            </div>
            <div>
              <div class="card-metric-lbl">Fwd P/E</div>
              <div class="card-metric-val ${clsForwardPE(s.forwardPE, s.sector)}">${fmtMult(s.forwardPE)}</div>
            </div>
            <div>
              <div class="card-metric-lbl">PEG</div>
              <div class="card-metric-val ${clsPeg(s.pegRatio)}">${fmtPeg(s.pegRatio)}</div>
            </div>
          </div>
        </article>`;
      })
      .join("");
  }

  // --- Interactive 1-Year Canvas Chart inside Drawer ---
  function drawDetailChart(stock) {
    const canvas = document.getElementById("detail-price-canvas");
    const readout = document.getElementById("chart-hover-readout");
    if (!canvas || !Array.isArray(stock.chart) || stock.chart.length < 3) return;

    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.parentElement.getBoundingClientRect();
    const width = Math.max(280, Math.floor(rect.width - 28));
    const height = 195;

    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;

    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);

    const styles = getComputedStyle(document.documentElement);
    const textMuted = styles.getPropertyValue("--text-muted").trim() || "#667085";
    const borderCol = styles.getPropertyValue("--border-subtle").trim() || "#e4e7ec";
    const accentCol = styles.getPropertyValue("--accent").trim() || "#1570ef";
    const holdCol = styles.getPropertyValue("--sig-hold-dot").trim() || "#dc6803";
    const goodCol = styles.getPropertyValue("--sig-strong-dot").trim() || "#12b76a";

    const pts = stock.chart;
    const allVals = [];
    pts.forEach((p) => {
      if (p.c != null) allVals.push(p.c);
      if (p.ma50 != null) allVals.push(p.ma50);
      if (p.ma200 != null) allVals.push(p.ma200);
    });
    const minV = Math.min(...allVals) * 0.97;
    const maxV = Math.max(...allVals) * 1.03;
    const span = maxV - minV || 1;

    const padL = 8;
    const padR = 52;
    const padT = 10;
    const padB = 22;
    const plotW = width - padL - padR;
    const plotH = height - padT - padB;

    const xOf = (i) => padL + (i / (pts.length - 1)) * plotW;
    const yOf = (v) => padT + plotH - ((v - minV) / span) * plotH;

    function paint(hoverIdx = null) {
      ctx.clearRect(0, 0, width, height);

      ctx.strokeStyle = borderCol;
      ctx.lineWidth = 1;
      ctx.fillStyle = textMuted;
      ctx.font = "11px 'JetBrains Mono', monospace";
      ctx.textAlign = "left";
      for (let g = 0; g <= 3; g++) {
        const val = minV + (g / 3) * span;
        const y = Math.round(yOf(val)) + 0.5;
        ctx.beginPath();
        ctx.moveTo(padL, y);
        ctx.lineTo(padL + plotW, y);
        ctx.stroke();
        ctx.fillText(`${stock.currencySymbol || "$"}${val.toFixed(0)}`, padL + plotW + 6, y + 3);
      }

      ctx.textAlign = "left";
      ctx.fillText(pts[0].d.slice(0, 7), padL, height - 5);
      ctx.textAlign = "center";
      ctx.fillText(pts[Math.floor(pts.length / 2)].d.slice(0, 7), padL + plotW / 2, height - 5);
      ctx.textAlign = "right";
      ctx.fillText(pts[pts.length - 1].d.slice(5), padL + plotW, height - 5);

      ctx.save();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = holdCol;
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      let started200 = false;
      pts.forEach((p, idx) => {
        if (p.ma200 == null) return;
        if (!started200) {
          ctx.moveTo(xOf(idx), yOf(p.ma200));
          started200 = true;
        } else {
          ctx.lineTo(xOf(idx), yOf(p.ma200));
        }
      });
      ctx.stroke();
      ctx.restore();

      ctx.save();
      ctx.strokeStyle = goodCol;
      ctx.globalAlpha = 0.7;
      ctx.lineWidth = 1.25;
      ctx.beginPath();
      let started50 = false;
      pts.forEach((p, idx) => {
        if (p.ma50 == null) return;
        if (!started50) {
          ctx.moveTo(xOf(idx), yOf(p.ma50));
          started50 = true;
        } else {
          ctx.lineTo(xOf(idx), yOf(p.ma50));
        }
      });
      ctx.stroke();
      ctx.restore();

      const grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
      grad.addColorStop(0, "rgba(20, 128, 150, 0.18)");
      grad.addColorStop(1, "rgba(20, 128, 150, 0.0)");

      ctx.beginPath();
      pts.forEach((p, idx) => {
        if (idx === 0) ctx.moveTo(xOf(idx), yOf(p.c));
        else ctx.lineTo(xOf(idx), yOf(p.c));
      });
      ctx.strokeStyle = accentCol;
      ctx.lineWidth = 2.1;
      ctx.stroke();

      ctx.lineTo(xOf(pts.length - 1), padT + plotH);
      ctx.lineTo(xOf(0), padT + plotH);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();

      if (hoverIdx !== null && pts[hoverIdx]) {
        const hp = pts[hoverIdx];
        const hx = xOf(hoverIdx);
        const hy = yOf(hp.c);
        ctx.save();
        ctx.strokeStyle = textMuted;
        ctx.setLineDash([2, 2]);
        ctx.beginPath();
        ctx.moveTo(hx, padT);
        ctx.lineTo(hx, padT + plotH);
        ctx.stroke();
        ctx.restore();

        ctx.beginPath();
        ctx.arc(hx, hy, 4, 0, Math.PI * 2);
        ctx.fillStyle = accentCol;
        ctx.fill();
      }
    }

    paint();

    canvas.onmousemove = (ev) => {
      const r = canvas.getBoundingClientRect();
      const mx = ev.clientX - r.left;
      const ratio = Math.max(0, Math.min(1, (mx - padL) / plotW));
      const idx = Math.round(ratio * (pts.length - 1));
      const pt = pts[idx];
      if (pt && readout) {
        const cur = stock.currencySymbol || "$";
        readout.textContent = `${pt.d}: ${fmtPrice(pt.c, cur)}${
          pt.ma200 ? ` · 200d ${fmtPrice(pt.ma200, cur)}` : ""
        }`;
      }
      paint(idx);
    };

    canvas.onmouseleave = () => {
      if (readout) readout.textContent = "Hover chart to inspect dates";
      paint(null);
    };
  }

  // --- Open & Render Stock Deep-Dive Drawer ---
  window.openStockDrawer = openDrawer;
  function openDrawer(symbol) {
    const stock = (state.payload.stocks || []).find((s) => s.symbol === symbol);
    if (!stock) return;
    state.selectedSymbol = symbol;

    const cur = stock.currencySymbol || "$";
    const p = stock.pillars || {};
    const drawer = document.getElementById("detail-drawer");
    const backdrop = document.getElementById("drawer-backdrop");
    const content = document.getElementById("drawer-content");

    const greenItems =
      (stock.greenFlags || []).length > 0
        ? stock.greenFlags.map((f) => `<li>${esc(f)}</li>`).join("")
        : `<li>Steady market position with no extreme bargain triggers today.</li>`;

    const redItems =
      (stock.redFlags || []).length > 0
        ? stock.redFlags.map((f) => `<li>${esc(f)}</li>`).join("")
        : `<li>No major valuation or balance-sheet red flags detected.</li>`;

    const factorRows = [
      ["Valuation (35%)", p.valuation],
      ["Business Quality (20%)", p.quality],
      ["Revenue & EPS Growth (20%)", p.growth],
      ["Wall St. Upside (15%)", p.upside],
      ["Entry Timing / RSI (10%)", p.timing],
    ]
      .map(([label, obj]) => {
        const sc = obj ? obj.score : 50;
        const st = obj ? obj.status : "neutral";
        return `<div class="factor-row">
          <span>${esc(label)}</span>
          <div class="factor-track"><div class="factor-fill ${esc(st)}" style="width:${sc}%"></div></div>
          <strong class="tabular" style="text-align:right">${sc}/100</strong>
        </div>`;
      })
      .join("");

    const shock = stock.shockAlert && stock.shockAlert.active ? stock.shockAlert : null;
    const shockBannerHtml = shock
      ? `<div class="shock-alert-box">
          <div class="shock-alert-head">
            <strong>${esc(shock.badge || "🚨 Governance / Leadership Shock")}</strong>
            <span>Buy Signal Capped at Hold / Watch</span>
          </div>
          <p>${esc(shock.reason)} — trailing valuation multiples do not yet reflect leadership/probe uncertainty or potential guidance resets.</p>
          ${
            Array.isArray(shock.headlines) && shock.headlines.length > 0
              ? `<ul class="shock-headlines">${shock.headlines.map((h) => `<li>${esc(h)}</li>`).join("")}</ul>`
              : ""
          }
        </div>`
      : "";

    const zone = stock.valuationZone || "fair";
    const vsNum = Number(stock.vsFairPct || 0);
    const vsSigned = `${vsNum > 0 ? "+" : ""}${vsNum.toFixed(1)}% vs. Fair Value`;
    const pos = Math.max(3, Math.min(97, Number(stock.rangePosValuation ?? 50)));

    content.innerHTML = `
      <div class="drawer-header">
        <div class="drawer-title-group">
          <div class="verdict-cell sig-${esc(stock.verdictKey)}">
            <span class="score-badge tabular">${stock.score}</span>
            <span class="verdict-pill badge-${esc(stock.verdictKey)}">${esc(stock.verdict)}</span>
            <span class="drawer-sub">${esc(stock.sector)} · ${esc(stock.industry)}</span>
          </div>
          <h2>${esc(stock.longName || stock.name)} (${esc(stock.symbol)})</h2>
          <div class="drawer-sub tabular">
            <strong>${fmtPrice(stock.price, cur)}</strong> (${fmtPct(stock.changePct, true, 2)} today) · Market Cap ${fmtMoneyCompact(stock.marketCap, cur)} · EV ${fmtMoneyCompact(stock.enterpriseValue, cur)}
          </div>
        </div>
        <div style="display:flex;gap:6px;">
          ${state.hasLiveBackend ? `<button id="drawer-remove-btn" type="button" class="btn" title="Remove ticker from daily watchlist">Remove</button>` : ""}
          <button id="drawer-close-btn" type="button" class="btn">✕ Close</button>
        </div>
      </div>

      ${shockBannerHtml}

      <p style="margin:14px 0 0;font-size:0.88rem;color:var(--text-secondary);">
        <strong>Bottom Line:</strong> ${esc(stock.summary)}
      </p>

      <div class="drawer-section">
        <h3>Reasonable Price Range (What Is Cheap vs. Expensive?)</h3>
        <div class="drawer-range-card">
          <div class="drawer-range-tiers tabular">
            <div class="range-tier tier-cheap ${zone === "cheap" ? "active" : ""}">
              <span class="tier-lbl">🟢 Cheap Zone</span>
              <strong class="tier-val">&le; ${fmtPrice(stock.cheapPrice, cur)}</strong>
              <span class="tier-hint">18%+ margin of safety</span>
            </div>
            <div class="range-tier tier-fair ${zone === "fair" ? "active" : ""}">
              <span class="tier-lbl">⚖ Fair Value</span>
              <strong class="tier-val">~${fmtPrice(stock.fairPrice, cur)}</strong>
              <span class="tier-hint">Reasonable intrinsic value</span>
            </div>
            <div class="range-tier tier-exp ${zone === "expensive" ? "active" : ""}">
              <span class="tier-lbl">🔴 Expensive Zone</span>
              <strong class="tier-val">&ge; ${fmtPrice(stock.expensivePrice, cur)}</strong>
              <span class="tier-hint">20%+ valuation premium</span>
            </div>
          </div>

          <div class="drawer-range-meter">
            <div class="val-zone-bar large">
              <span class="val-zone-seg seg-cheap"></span>
              <span class="val-zone-seg seg-fair"></span>
              <span class="val-zone-seg seg-exp"></span>
              <span class="val-zone-fair-tick"></span>
              <span class="val-zone-dot dot-${esc(zone)}" style="left:${pos}%"></span>
            </div>
            <div class="drawer-range-caption tabular">
              <span>Current: <strong>${fmtPrice(stock.price, cur)}</strong> (<span class="zone-pill zone-${esc(zone)}">${esc(stock.valuationZoneLabel || "Fair")} · ${esc(vsSigned)}</span>)</span>
            </div>
          </div>

          <div class="drawer-range-basis">${esc(stock.rangeBasis || "")}</div>
        </div>
      </div>

      <div class="dip-callout-box ${stock.isFallingKnife ? "knife-callout" : ""}">
        <div class="dip-callout-head">
          <span style="font-size:0.82rem;font-weight:700;color:var(--text-primary);">Dip vs. Falling-Knife Check — Is Today a Good Entry?</span>
          ${renderDipBadge(stock)}
        </div>
        <p style="margin:0 0 8px;font-size:0.81rem;color:var(--text-secondary);line-height:1.45;">
          ${esc(stock.dipReason || "Normal daily momentum range.")}
        </p>
        <div class="dip-callout-stats">
          <span>14d RSI: <strong>${stock.rsi14 !== null && stock.rsi14 !== undefined ? stock.rsi14.toFixed(1) : "–"}</strong></span>
          <span>20d High Drop: <strong>${fmtPct(stock.dip20dPct, true, 1)}</strong></span>
          <span>vs. 50d MA: <strong>${fmtPct(stock.distFrom50dPct, true, 1)}</strong></span>
          <span>vs. 200d MA: <strong>${fmtPct(stock.distFrom200dPct, true, 1)}</strong></span>
        </div>
      </div>

      <div class="drawer-section">
        <h3>Why Buy vs. What To Watch</h3>
        <div class="flags-grid">
          <div class="flag-box">
            <h4 class="val-good">✓ Bullish Indicators</h4>
            <ul class="flag-list">${greenItems}</ul>
          </div>
          <div class="flag-box">
            <h4 class="val-bad">! Risks &amp; Valuation Checks</h4>
            <ul class="flag-list">${redItems}</ul>
          </div>
        </div>
      </div>

      <div class="drawer-section">
        <h3>5-Pillar Buy Score Breakdown</h3>
        <div class="factor-rows">${factorRows}</div>
      </div>

      <div class="drawer-section">
        <h3>1-Year Price &amp; Moving Averages</h3>
        <div class="chart-container">
          <div class="chart-legend">
            <div class="legend-items">
              <span><i class="legend-swatch" style="background:var(--accent)"></i>Price</span>
              <span><i class="legend-swatch" style="background:var(--sig-strong-dot)"></i>50d MA (${fmtPrice(stock.ma50, cur)})</span>
              <span><i class="legend-swatch" style="background:var(--sig-hold-dot)"></i>200d MA (${fmtPrice(stock.ma200, cur)})</span>
            </div>
            <span id="chart-hover-readout" class="tabular">Hover chart to inspect dates</span>
          </div>
          <canvas id="detail-price-canvas"></canvas>
        </div>
      </div>

      <div class="drawer-section">
        <h3>Valuation Multiples &amp; Consensus</h3>
        <div class="metrics-grid">
          <div class="metric-tile">
            <div class="metric-tile-name">Forward P/E</div>
            <div class="metric-tile-val ${clsForwardPE(stock.forwardPE, stock.sector)}">${fmtMult(stock.forwardPE)}</div>
            <div class="metric-tile-hint">Next 12m earnings (&lt;17x cheap)</div>
          </div>
          <div class="metric-tile">
            <div class="metric-tile-name">Trailing P/E</div>
            <div class="metric-tile-val">${fmtMult(stock.trailingPE)}</div>
            <div class="metric-tile-hint">Past 12m GAAP earnings</div>
          </div>
          <div class="metric-tile">
            <div class="metric-tile-name">PEG Ratio</div>
            <div class="metric-tile-val ${clsPeg(stock.pegRatio)}">${fmtPeg(stock.pegRatio)}</div>
            <div class="metric-tile-hint">P/E vs Growth (&lt;1.0x bargain)</div>
          </div>
          <div class="metric-tile">
            <div class="metric-tile-name">Free Cash Flow Yield</div>
            <div class="metric-tile-val ${clsFcf(stock.fcfYield)}">${fmtPct(stock.fcfYield)}</div>
            <div class="metric-tile-hint">FCF ${fmtMoneyCompact(stock.freeCashflow, cur)} / yr</div>
          </div>
          <div class="metric-tile">
            <div class="metric-tile-name">EV / EBITDA</div>
            <div class="metric-tile-val">${fmtMult(stock.evToEbitda)}</div>
            <div class="metric-tile-hint">Enterprise multiple (&lt;14x low)</div>
          </div>
          <div class="metric-tile">
            <div class="metric-tile-name">Wall St. Target</div>
            <div class="metric-tile-val ${clsUpside(stock.targetUpsidePct)}">${fmtPrice(stock.targetMeanPrice, cur)} (${fmtPct(stock.targetUpsidePct, true, 0)})</div>
            <div class="metric-tile-hint">${stock.analystCount || 0} analysts (${fmtRangePrice(stock.targetLowPrice, cur)}–${fmtRangePrice(stock.targetHighPrice, cur)})</div>
          </div>
        </div>
      </div>

      <div class="drawer-section">
        <h3>Quality, Growth &amp; Balance Sheet</h3>
        <div class="metrics-grid">
          <div class="metric-tile">
            <div class="metric-tile-name">Revenue Growth (YoY)</div>
            <div class="metric-tile-val ${stock.revenueGrowth >= 12 ? "val-good" : stock.revenueGrowth < 0 ? "val-bad" : ""}">${fmtPct(stock.revenueGrowth, true)}</div>
            <div class="metric-tile-hint">Sales ${fmtMoneyCompact(stock.totalRevenue, cur)}</div>
          </div>
          <div class="metric-tile">
            <div class="metric-tile-name">Earnings Growth</div>
            <div class="metric-tile-val ${stock.epsGrowth >= 15 ? "val-good" : stock.epsGrowth < 0 ? "val-bad" : ""}">${fmtPct(stock.epsGrowth, true)}</div>
            <div class="metric-tile-hint">Blended YoY &amp; Fwd EPS</div>
          </div>
          <div class="metric-tile">
            <div class="metric-tile-name">Operating / Net Margin</div>
            <div class="metric-tile-val">${fmtPct(stock.operatingMargin)} / ${fmtPct(stock.profitMargin)}</div>
            <div class="metric-tile-hint">Gross margin ${fmtPct(stock.grossMargin)}</div>
          </div>
          <div class="metric-tile">
            <div class="metric-tile-name">Return on Equity (ROE)</div>
            <div class="metric-tile-val">${fmtPct(stock.roe)}</div>
            <div class="metric-tile-hint">ROA ${fmtPct(stock.roa)}</div>
          </div>
          <div class="metric-tile">
            <div class="metric-tile-name">Net Cash / (Debt)</div>
            <div class="metric-tile-val ${stock.netCash > 0 ? "val-good" : ""}">${fmtMoneyCompact(stock.netCash, cur)}</div>
            <div class="metric-tile-hint">Debt/Equity ${stock.debtToEquity !== null ? stock.debtToEquity.toFixed(2) + "x" : "–"}</div>
          </div>
          <div class="metric-tile">
            <div class="metric-tile-name">Dividend Yield</div>
            <div class="metric-tile-val">${fmtPct(stock.dividendYield, false, 2)}</div>
            <div class="metric-tile-hint">52W ${fmtRangePrice(stock.low52w, cur)} – ${fmtRangePrice(stock.high52w, cur)}</div>
          </div>
        </div>
      </div>
    `;

    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    backdrop.classList.add("open");

    document.getElementById("drawer-close-btn").onclick = closeDrawer;
    const remBtn = document.getElementById("drawer-remove-btn");
    if (remBtn) remBtn.onclick = () => removeTicker(stock.symbol);

    requestAnimationFrame(() => drawDetailChart(stock));
  }

  function closeDrawer() {
    state.selectedSymbol = null;
    const drawer = document.getElementById("detail-drawer");
    const backdrop = document.getElementById("drawer-backdrop");
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    backdrop.classList.remove("open");
  }

  // --- Cloud Static Snapshot + Live API Actions ---
  async function syncFromServer() {
    try {
      const health = await fetch("./api/health", { cache: "no-store" });
      const ct = health.headers.get("content-type") || "";
      if (health.ok && ct.includes("application/json")) {
        state.hasLiveBackend = true;
        const resp = await fetch("./api/stocks", { cache: "no-store" });
        if (resp.ok) {
          const data = await resp.json();
          if (data && Array.isArray(data.stocks)) {
            state.payload = data;
            renderAll();
          }
        }
        return;
      }
    } catch (_) {
      state.hasLiveBackend = false;
    }

    const addInput = document.getElementById("add-ticker-input");
    const addBtn = document.getElementById("add-ticker-btn");
    if (addInput && addBtn) {
      addInput.placeholder = "Jump to ticker (NVDA, ANT, SPY)";
      addBtn.textContent = "Inspect";
    }
    try {
      const resp = await fetch(`./data/stocks.json?t=${Date.now()}`, { cache: "no-store" });
      if (resp.ok) {
        const data = await resp.json();
        if (data && Array.isArray(data.stocks)) {
          state.payload = data;
          renderAll();
        }
      }
    } catch (_) {
      // Embedded snapshot fallback remains active
    }
  }

  async function refreshLiveNow() {
    const btn = document.getElementById("refresh-live-btn");
    const prevText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "↻ Checking…";

    if (!state.hasLiveBackend) {
      try {
        const resp = await fetch(`./data/stocks.json?t=${Date.now()}`, { cache: "no-store" });
        if (!resp.ok) throw new Error("Snapshot fetch failed");
        const data = await resp.json();
        state.payload = data;
        renderAll();
        if (state.selectedSymbol) openDrawer(state.selectedSymbol);
        const ts = data.updatedAt ? new Date(data.updatedAt).toLocaleString() : "today";
        showBanner(
          `Synced latest GitHub Pages snapshot (${data.stocks.length} stocks · updated ${ts}). Auto-updates twice every trading day.`,
          false,
          5500
        );
      } catch (_) {
        showBanner("Showing latest embedded daily snapshot.", false, 4000);
      } finally {
        btn.disabled = false;
        btn.textContent = prevText;
      }
      return;
    }

    showBanner("Fetching live quotes, price ranges, and corporate-shock headlines…", false, 0);
    try {
      const resp = await fetch("./api/refresh", { method: "POST" });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "Refresh failed");
      state.payload = data;
      renderAll();
      if (state.selectedSymbol) openDrawer(state.selectedSymbol);
      showBanner(`Updated ${data.stocks.length} stocks with live market data and shock checks.`, false, 4000);
    } catch (err) {
      showBanner(`Live refresh unavailable (${err.message}). Showing latest daily snapshot.`, true, 6000);
    } finally {
      btn.disabled = false;
      btn.textContent = prevText;
    }
  }

  async function addTicker(rawTicker) {
    const cleaned = rawTicker.trim().toUpperCase().replace(/\./g, "-");
    const ticker = CLIENT_ALIASES[cleaned] || CLIENT_ALIASES[rawTicker.trim().toUpperCase()] || cleaned;
    if (!ticker) return;
    const btn = document.getElementById("add-ticker-btn");
    const input = document.getElementById("add-ticker-input");

    const existing = (state.payload.stocks || []).find(
      (s) =>
        s.symbol === ticker ||
        (s.name || "").toUpperCase().includes(ticker) ||
        (s.longName || "").toUpperCase().includes(ticker)
    );
    if (!state.hasLiveBackend) {
      if (existing) {
        state.verdictFilter = "all";
        state.preset = "all";
        state.sectorFilter = "all";
        state.searchQuery = "";
        document.getElementById("filter-search-input").value = "";
        document.querySelectorAll("#preset-pills .pill").forEach((p) => p.classList.toggle("active", p.dataset.preset === "all"));
        state.highlightedSymbol = existing.symbol;
        input.value = "";
        renderAll();
        openDrawer(existing.symbol);
      } else {
        showBanner(
          `'${ticker}' isn't in the ${(state.payload.stocks || []).length}-stock default list yet — add it to public/data/watchlist.json in GitHub and the daily Action will score it automatically!`,
          false,
          7000
        );
      }
      return;
    }

    btn.disabled = true;
    btn.textContent = "Loading…";
    try {
      const resp = await fetch("./api/add", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ticker }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || `Could not load ${ticker}`);
      state.payload = data.payload;
      state.verdictFilter = "all";
      state.preset = "all";
      state.sectorFilter = "all";
      state.searchQuery = "";
      document.getElementById("filter-search-input").value = "";
      document.querySelectorAll("#preset-pills .pill").forEach((p) => p.classList.toggle("active", p.dataset.preset === "all"));

      const added = data.added;
      state.highlightedSymbol = added.symbol;
      input.value = "";
      renderAll();
      openDrawer(added.symbol);
      showBanner(`Added ${added.symbol} (${added.name}) — Rated ${added.verdict} (Score ${added.score}/99).`, false, 5000);
    } catch (err) {
      showBanner(err.message, true, 6500);
    } finally {
      btn.disabled = false;
      btn.textContent = "+ Track";
    }
  }

  async function removeTicker(symbol) {
    if (!state.hasLiveBackend) return;
    try {
      const resp = await fetch("./api/remove", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ticker: symbol }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "Failed to remove");
      state.payload = data.payload;
      closeDrawer();
      renderAll();
      showBanner(`Removed ${symbol} from your daily watchlist.`, false, 3500);
    } catch (err) {
      showBanner(err.message, true, 5000);
    }
  }

  function renderAll() {
    renderHeaderAndCounts();
    renderList();
  }

  // --- Event Listeners & Init ---
  function bindEvents() {
    const savedTheme = localStorage.getItem("stock-signal-theme") || "light";
    document.documentElement.setAttribute("data-theme", savedTheme);
    const themeBtn = document.getElementById("theme-toggle-btn");
    themeBtn.textContent = savedTheme === "dark" ? "Light Mode" : "Dark Mode";

    themeBtn.addEventListener("click", () => {
      const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      localStorage.setItem("stock-signal-theme", next);
      themeBtn.textContent = next === "dark" ? "Light Mode" : "Dark Mode";
      if (state.selectedSymbol) {
        const s = (state.payload.stocks || []).find((x) => x.symbol === state.selectedSymbol);
        if (s) drawDetailChart(s);
      }
    });

    const guideBtn = document.getElementById("guide-toggle-btn");
    const guideBar = document.getElementById("guide-bar");
    guideBtn.addEventListener("click", () => {
      const open = guideBar.classList.toggle("open");
      guideBtn.setAttribute("aria-expanded", String(open));
    });

    document.getElementById("status-banner-close").addEventListener("click", () => {
      document.getElementById("status-banner").classList.remove("visible");
    });

    document.querySelectorAll(".signal-bucket").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.verdictFilter = btn.dataset.verdict || "all";
        renderAll();
      });
    });

    document.querySelectorAll("#preset-pills .pill").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.preset = btn.dataset.preset || "all";
        document.querySelectorAll("#preset-pills .pill").forEach((b) => b.classList.toggle("active", b === btn));
        renderList();
      });
    });

    document.getElementById("filter-search-input").addEventListener("input", (e) => {
      state.searchQuery = e.target.value;
      renderList();
    });

    document.getElementById("sector-filter-select").addEventListener("change", (e) => {
      state.sectorFilter = e.target.value;
      renderList();
    });

    const tblBtn = document.getElementById("view-table-btn");
    const crdBtn = document.getElementById("view-cards-btn");
    tblBtn.addEventListener("click", () => {
      state.viewMode = "table";
      tblBtn.classList.add("active");
      crdBtn.classList.remove("active");
      renderList();
    });
    crdBtn.addEventListener("click", () => {
      state.viewMode = "cards";
      crdBtn.classList.add("active");
      tblBtn.classList.remove("active");
      renderList();
    });

    document.querySelectorAll("#stock-table thead th.sortable").forEach((th) => {
      th.addEventListener("click", () => {
        const key = th.dataset.sort;
        if (state.sortKey === key) {
          state.sortDir = state.sortDir === "desc" ? "asc" : "desc";
        } else {
          state.sortKey = key;
          const ascDefault = ["symbol", "vsFairPct", "forwardPE", "pegRatio", "rsi14"].includes(key);
          state.sortDir = ascDefault ? "asc" : "desc";
        }
        renderList();
      });
    });

    document.getElementById("stock-table-body").addEventListener("click", (e) => {
      const tr = e.target.closest("tr[data-symbol]");
      if (tr) openDrawer(tr.dataset.symbol);
    });
    document.getElementById("cards-view-container").addEventListener("click", (e) => {
      const card = e.target.closest("[data-symbol]");
      if (card) openDrawer(card.dataset.symbol);
    });

    document.getElementById("drawer-backdrop").addEventListener("click", closeDrawer);
    window.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && state.selectedSymbol) closeDrawer();
    });

    document.getElementById("refresh-live-btn").addEventListener("click", refreshLiveNow);
    document.getElementById("add-ticker-form").addEventListener("submit", (e) => {
      e.preventDefault();
      addTicker(document.getElementById("add-ticker-input").value);
    });
  }

  bindEvents();
  renderAll();
  syncFromServer();
})();
