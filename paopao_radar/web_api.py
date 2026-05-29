from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from .config import Settings
from .launch_score import (
    build_launch_radar_payload,
    build_mock_payload,
    filter_items,
    launch_stats,
    load_latest_payload,
    now_iso,
)
from .storage import JsonStore


VALID_MODES = {"auto", "mock", "real"}


def _normalize_mode(mode: str | None, default_mode: str) -> str:
    value = (mode or default_mode or "mock").strip().lower()
    if value not in VALID_MODES:
        raise HTTPException(status_code=400, detail="mode must be auto, mock or real")
    return value


def _with_stats(payload: dict[str, Any]) -> dict[str, Any]:
    items = list(payload.get("items") or [])
    return {**payload, "stats": launch_stats(items)}


def _payload_for_mode(settings: Settings, store: JsonStore, mode: str) -> dict[str, Any]:
    try:
        payload = build_launch_radar_payload(settings, store, mode=mode)
    except Exception as exc:
        cached = load_latest_payload(settings, store)
        if cached.get("items"):
            payload = {
                **cached,
                "stale": True,
                "error": f"{type(exc).__name__}: {exc}",
            }
        else:
            payload = {
                "updated_at": now_iso(),
                "stale": True,
                "mock": False,
                "mode": mode,
                "items": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
    return _with_stats(payload)


def create_app(
    settings: Settings | None = None,
    store: JsonStore | None = None,
    default_mode: str | None = None,
) -> FastAPI:
    settings = settings or Settings.load()
    store = store or JsonStore(settings.data_dir)
    default_mode = (default_mode or settings.launch_web_mode or "mock").lower()

    app = FastAPI(title="泡泡抓币 Launch Radar API", version="1.0")

    @app.get("/api/launch-radar")
    def launch_radar(
        mode: str | None = Query(default=None),
        timeframe: str = Query(default=""),
        level: str = Query(default=""),
        signal_type: str = Query(default=""),
        wash_risk_level: str = Query(default=""),
        min_score: float = Query(default=0.0, ge=0, le=100),
    ) -> dict[str, Any]:
        normalized = _normalize_mode(mode, default_mode)
        payload = _payload_for_mode(settings, store, normalized)
        filtered = filter_items(
            payload,
            timeframe=timeframe,
            level=level,
            signal_type=signal_type,
            wash_risk_level=wash_risk_level,
            min_score=min_score,
        )
        return _with_stats(filtered)

    @app.get("/api/oi-divergence")
    def oi_divergence(mode: str | None = Query(default=None)) -> dict[str, Any]:
        data = store.load(settings.oi_divergence_latest_path, {})
        if isinstance(data, dict) and data.get("items"):
            return data
        normalized = _normalize_mode(mode, default_mode)
        payload = _payload_for_mode(settings, store, normalized)
        return {
            "updated_at": payload.get("updated_at"),
            "stale": payload.get("stale", False),
            "items": [
                {
                    "rank": item.get("rank"),
                    "symbol": item.get("symbol"),
                    "timeframe": item.get("timeframe"),
                    "level": item.get("level"),
                    "score": item.get("score"),
                    "oi_change_pct": item.get("oi_change_pct"),
                    "price_change_pct": item.get("price_change_pct"),
                    "divergence_ratio": item.get("divergence_ratio"),
                    "updated_at": item.get("updated_at"),
                }
                for item in payload.get("items", [])
            ],
        }

    @app.get("/api/wash-risk")
    def wash_risk(mode: str | None = Query(default=None)) -> dict[str, Any]:
        data = store.load(settings.wash_risk_latest_path, {})
        if isinstance(data, dict) and data.get("items"):
            return data
        normalized = _normalize_mode(mode, default_mode)
        payload = _payload_for_mode(settings, store, normalized)
        return {
            "updated_at": payload.get("updated_at"),
            "stale": payload.get("stale", False),
            "items": [
                {
                    "rank": item.get("rank"),
                    "symbol": item.get("symbol"),
                    "wash_risk_score": item.get("wash_risk_score"),
                    "wash_risk_level": item.get("wash_risk_level"),
                    "risk_reasons": item.get("risk_reasons", []),
                    "updated_at": item.get("updated_at"),
                }
                for item in payload.get("items", [])
            ],
        }

    @app.get("/api/symbol/{symbol}")
    def symbol_detail(symbol: str, mode: str | None = Query(default=None)) -> dict[str, Any]:
        normalized_symbol = symbol.upper()
        if not normalized_symbol.endswith("USDT"):
            normalized_symbol = f"{normalized_symbol}USDT"
        latest = load_latest_payload(settings, store)
        if not latest.get("items"):
            latest = _payload_for_mode(settings, store, _normalize_mode(mode, default_mode))
        item = next(
            (row for row in latest.get("items", []) if str(row.get("symbol", "")).upper() == normalized_symbol),
            None,
        )
        history = []
        records = store.load(settings.signal_history_path, [])
        if isinstance(records, list):
            for record in records[-50:]:
                for row in record.get("items", []) if isinstance(record, dict) else []:
                    if str(row.get("symbol", "")).upper() == normalized_symbol:
                        history.append({
                            "updated_at": record.get("updated_at"),
                            "level": row.get("level"),
                            "score": row.get("score"),
                            "signal_type": row.get("signal_type"),
                            "wash_risk_level": row.get("wash_risk_level"),
                        })
        return {
            "symbol": normalized_symbol,
            "updated_at": latest.get("updated_at"),
            "stale": latest.get("stale", False),
            "item": item,
            "history": history[-20:],
        }

    @app.get("/launch-radar", response_class=HTMLResponse)
    def launch_radar_page() -> HTMLResponse:
        return HTMLResponse(LAUNCH_RADAR_HTML)

    return app


def app_from_env() -> FastAPI:
    return create_app()


LAUNCH_RADAR_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>泡泡抓币 - 山寨币启动雷达</title>
  <style>
    :root { color-scheme: dark; --bg:#050607; --panel:#0c0d10; --line:#1e232b; --text:#eef4ff; --muted:#7f8da3; --green:#2ef2a2; --red:#ff6370; --yellow:#f3c04d; --blue:#6ea8ff; }
    * { box-sizing: border-box; }
    body { margin:0; background:var(--bg); color:var(--text); font:14px/1.45 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body::before { content:""; position:fixed; inset:0; pointer-events:none; background-image:linear-gradient(rgba(255,255,255,.035) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.035) 1px, transparent 1px); background-size:40px 40px; mask-image:linear-gradient(to bottom, #000, transparent 70%); }
    header { position:sticky; top:0; z-index:4; display:flex; align-items:center; justify-content:space-between; gap:16px; min-height:56px; padding:0 24px; border-bottom:1px solid var(--line); background:rgba(5,6,7,.9); backdrop-filter:blur(16px); }
    .brand { font-weight:800; letter-spacing:0; }
    .brand span { color:var(--green); }
    .status { display:flex; gap:10px; color:var(--muted); font-size:12px; align-items:center; }
    main { position:relative; max-width:1500px; margin:0 auto; padding:24px; }
    .toolbar { display:grid; grid-template-columns:repeat(6, minmax(120px, 1fr)); gap:10px; margin-bottom:16px; }
    select, input, button { width:100%; height:38px; border:1px solid var(--line); background:#080a0d; color:var(--text); border-radius:6px; padding:0 10px; }
    button { cursor:pointer; font-weight:700; }
    button.primary { background:#06291d; border-color:#11553c; color:#9affd3; }
    .cards { display:grid; grid-template-columns:repeat(5, minmax(130px, 1fr)); gap:10px; margin-bottom:16px; }
    .card { border:1px solid var(--line); background:linear-gradient(180deg, rgba(255,255,255,.04), rgba(255,255,255,.01)); border-radius:8px; padding:14px; }
    .card .label { color:var(--muted); font-size:12px; }
    .card .value { font-size:28px; font-weight:800; margin-top:6px; }
    .table-wrap { border:1px solid var(--line); border-radius:8px; overflow:auto; background:rgba(8,10,13,.88); }
    table { width:100%; border-collapse:collapse; min-width:1320px; }
    th, td { padding:12px 10px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; }
    th { color:var(--muted); font-size:12px; font-weight:700; background:#090b0e; position:sticky; top:0; z-index:1; }
    tr:hover td { background:#0e1217; }
    .symbol { color:#fff; font-weight:800; cursor:pointer; }
    .pill { display:inline-flex; align-items:center; min-width:34px; justify-content:center; height:22px; border-radius:999px; border:1px solid var(--line); padding:0 8px; font-size:12px; font-weight:800; }
    .s { color:#fff; border-color:#7a53ff; background:#241457; }
    .a { color:#111; border-color:#ffd36f; background:#ffd36f; }
    .b { color:#9affd3; border-color:#14583d; background:#06291d; }
    .high { color:#ffccd1; border-color:#69222c; background:#2b0b10; }
    .medium { color:#ffe8ad; border-color:#6b4b12; background:#261806; }
    .low { color:#9affd3; border-color:#14583d; background:#06291d; }
    .pos { color:var(--green); }
    .neg { color:var(--red); }
    .empty { padding:64px 16px; text-align:center; color:var(--muted); }
    dialog { width:min(760px, calc(100vw - 32px)); border:1px solid var(--line); border-radius:8px; background:#080a0d; color:var(--text); padding:0; }
    dialog::backdrop { background:rgba(0,0,0,.65); }
    .modal-head { display:flex; justify-content:space-between; align-items:center; padding:16px; border-bottom:1px solid var(--line); }
    .modal-body { padding:16px; display:grid; grid-template-columns:repeat(2, 1fr); gap:10px; }
    .detail { border:1px solid var(--line); border-radius:8px; padding:12px; background:#050607; min-height:74px; }
    .detail .label { color:var(--muted); font-size:12px; }
    .detail .value { margin-top:6px; font-size:18px; font-weight:800; }
    .reasons { grid-column:1 / -1; color:#c9d5e6; }
    @media (max-width:900px) { header { padding:0 14px; } main { padding:16px 12px; } .toolbar, .cards { grid-template-columns:repeat(2, 1fr); } .modal-body { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <header>
    <div class="brand">泡泡抓币 <span>启动雷达</span></div>
    <div class="status"><span id="modeText">读取中</span><span id="updatedAt">--</span><span id="staleText"></span></div>
  </header>
  <main>
    <section class="toolbar">
      <select id="timeframe"><option value="">全部周期</option><option value="15m">15m</option></select>
      <select id="level"><option value="">全部等级</option><option>S</option><option>A</option><option>B</option><option>C</option></select>
      <select id="signalType">
        <option value="">全部信号</option>
        <option value="SHORT_SQUEEZE_FUEL">空头燃料</option>
        <option value="ACCUMULATION_BUILDUP">建仓候选</option>
        <option value="MOMENTUM_BREAKOUT">动量突破</option>
        <option value="LONG_CROWDED_RISK">多头拥挤风险</option>
        <option value="WATCH">观察</option>
      </select>
      <select id="washRisk"><option value="">全部刷量风险</option><option value="LOW">低</option><option value="MEDIUM">中</option><option value="HIGH">高</option></select>
      <input id="minScore" type="number" min="0" max="100" step="1" placeholder="最低分" />
      <button class="primary" id="refresh">刷新</button>
    </section>
    <section class="cards">
      <div class="card"><div class="label">S级信号</div><div class="value" id="sCount">0</div></div>
      <div class="card"><div class="label">A级信号</div><div class="value" id="aCount">0</div></div>
      <div class="card"><div class="label">高刷量风险</div><div class="value" id="highRiskCount">0</div></div>
      <div class="card"><div class="label">空头燃料候选</div><div class="value" id="shortFuelCount">0</div></div>
      <div class="card"><div class="label">建仓候选</div><div class="value" id="accumulationCount">0</div></div>
    </section>
    <section class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th><th>币种</th><th>周期</th><th>信号</th><th>等级</th><th>评分</th><th>OI变化</th><th>价格变化</th><th>背离度</th><th>Funding</th><th>主动买卖比</th><th>多空比</th><th>OI/市值</th><th>刷量风险</th><th>跨所确认</th><th>更新时间</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
      <div class="empty" id="empty" hidden>暂无数据</div>
    </section>
  </main>
  <dialog id="detailDialog">
    <div class="modal-head"><strong id="detailTitle">详情</strong><button id="closeDialog" style="width:80px">关闭</button></div>
    <div class="modal-body" id="detailBody"></div>
  </dialog>
  <script>
    const signalNames = {
      SHORT_SQUEEZE_FUEL: '空头燃料',
      ACCUMULATION_BUILDUP: '建仓候选',
      MOMENTUM_BREAKOUT: '动量突破',
      LONG_CROWDED_RISK: '多头拥挤风险',
      WATCH: '观察'
    };
    const riskNames = { LOW: '低', MEDIUM: '中', HIGH: '高' };
    const pct = (v, d = 2) => Number.isFinite(Number(v)) ? `${Number(v).toFixed(d)}%` : '--';
    const num = (v, d = 2) => Number.isFinite(Number(v)) ? Number(v).toFixed(d) : '--';
    const cls = v => Number(v) >= 0 ? 'pos' : 'neg';
    async function load() {
      const params = new URLSearchParams();
      params.set('timeframe', document.querySelector('#timeframe').value);
      params.set('level', document.querySelector('#level').value);
      params.set('signal_type', document.querySelector('#signalType').value);
      params.set('wash_risk_level', document.querySelector('#washRisk').value);
      params.set('min_score', document.querySelector('#minScore').value || '0');
      const res = await fetch(`/api/launch-radar?${params.toString()}`);
      const data = await res.json();
      render(data);
    }
    function render(data) {
      document.querySelector('#modeText').textContent = data.mock ? 'Mock模式' : '真实数据';
      document.querySelector('#updatedAt').textContent = data.updated_at || '--';
      document.querySelector('#staleText').textContent = data.stale ? '已使用上次成功结果' : '';
      const stats = data.stats || {};
      document.querySelector('#sCount').textContent = stats.s_count || 0;
      document.querySelector('#aCount').textContent = stats.a_count || 0;
      document.querySelector('#highRiskCount').textContent = stats.high_wash_risk_count || 0;
      document.querySelector('#shortFuelCount').textContent = stats.short_squeeze_fuel_count || 0;
      document.querySelector('#accumulationCount').textContent = stats.accumulation_count || 0;
      const tbody = document.querySelector('#rows');
      tbody.innerHTML = '';
      const items = data.items || [];
      document.querySelector('#empty').hidden = items.length > 0;
      for (const item of items) {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td>${item.rank}</td>
          <td><span class="symbol" data-symbol="${item.symbol}">${item.symbol}</span></td>
          <td>${item.timeframe}</td>
          <td>${signalNames[item.signal_type] || item.signal_type}</td>
          <td><span class="pill ${String(item.level).toLowerCase()}">${item.level}</span></td>
          <td>${num(item.score, 1)}</td>
          <td class="${cls(item.oi_change_pct)}">${pct(item.oi_change_pct)}</td>
          <td class="${cls(item.price_change_pct)}">${pct(item.price_change_pct)}</td>
          <td>${num(item.divergence_ratio, 2)}</td>
          <td class="${cls(item.funding_rate)}">${pct(Number(item.funding_rate) * 100, 4)}</td>
          <td>${num(item.taker_buy_sell_ratio, 2)}</td>
          <td>${num(item.long_short_ratio, 2)}</td>
          <td>${pct(Number(item.oi_marketcap_ratio) * 100, 2)}</td>
          <td><span class="pill ${String(item.wash_risk_level).toLowerCase()}">${riskNames[item.wash_risk_level] || item.wash_risk_level} ${num(item.wash_risk_score, 0)}</span></td>
          <td>${item.cross_exchange_confirmed ? '是' : '否'}</td>
          <td>${item.updated_at || '--'}</td>`;
        tbody.appendChild(tr);
      }
      document.querySelectorAll('.symbol').forEach(el => el.addEventListener('click', () => showDetail(el.dataset.symbol)));
    }
    async function showDetail(symbol) {
      const res = await fetch(`/api/symbol/${encodeURIComponent(symbol)}`);
      const data = await res.json();
      const item = data.item;
      document.querySelector('#detailTitle').textContent = `${symbol} 启动详情`;
      const body = document.querySelector('#detailBody');
      if (!item) {
        body.innerHTML = '<div class="empty">暂无数据</div>';
      } else {
        const reasons = (item.risk_reasons || []).map(x => `<li>${x}</li>`).join('') || '<li>暂无明显刷量风险原因</li>';
        const history = (data.history || []).map(x => `${x.updated_at || '--'} ${x.level || '-'} ${x.score || '-'}`).join('<br>') || '暂无历史记录';
        body.innerHTML = `
          <div class="detail"><div class="label">多周期OI变化</div><div class="value">${pct(item.oi_change_pct)}</div></div>
          <div class="detail"><div class="label">Funding</div><div class="value ${cls(item.funding_rate)}">${pct(Number(item.funding_rate) * 100, 4)}</div></div>
          <div class="detail"><div class="label">主动买卖比</div><div class="value">${num(item.taker_buy_sell_ratio, 2)}</div></div>
          <div class="detail"><div class="label">多空比</div><div class="value">${num(item.long_short_ratio, 2)}</div></div>
          <div class="detail"><div class="label">信号生命周期</div><div class="value">${item.lifecycle || '--'}</div></div>
          <div class="detail"><div class="label">刷量风险</div><div class="value">${riskNames[item.wash_risk_level] || item.wash_risk_level} ${num(item.wash_risk_score, 0)}</div></div>
          <div class="detail reasons"><div class="label">刷量风险原因</div><ul>${reasons}</ul></div>
          <div class="detail reasons"><div class="label">信号历史</div><div>${history}</div></div>`;
      }
      document.querySelector('#detailDialog').showModal();
    }
    document.querySelector('#refresh').addEventListener('click', load);
    document.querySelector('#closeDialog').addEventListener('click', () => document.querySelector('#detailDialog').close());
    document.querySelectorAll('select,input').forEach(el => el.addEventListener('change', load));
    load().catch(err => {
      document.querySelector('#empty').hidden = false;
      document.querySelector('#empty').textContent = `加载失败：${err.message}`;
    });
  </script>
</body>
</html>"""


app = app_from_env()
