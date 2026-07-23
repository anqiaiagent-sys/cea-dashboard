#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CEA Trading - 第6步:读 equity.csv + flows.csv，算业绩指标，生成只露业绩(不露金额)的静态网页。
只用 Python 标准库（无需额外安装）。
- 净值(NAV)以首个数据点 = 1.00，出入金按 flows.csv 扣除后计算(time-weighted / 分段收益连乘)。
- 页面只写:归一化净值、收益率%、回撤%、时间戳、点数。绝不写美元金额或每家余额。
可选:若存在 github.json + token 文件，则把生成的 index.html 通过 GitHub API 推送到 Pages 仓库。

用法:
  python build_site.py                      # 生成 site/index.html
  python build_site.py --push               # 生成并推送到 GitHub(需 github.json)
  python build_site.py --equity path --flows path --out path
"""

import argparse
import base64
import csv
import json
import math
import os
import sys
from datetime import datetime, timezone, timedelta

BJ = timezone(timedelta(hours=8))  # 北京时间

# ----------------------- 解析工具 -----------------------

_TS_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
]


def parse_ts(s):
    """把时间字符串解析成带北京时区的 datetime。解析不了返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    # 先试 ISO
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BJ)
        return dt
    except ValueError:
        pass
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=BJ)
        except ValueError:
            continue
    return None


def to_float(s):
    try:
        return float(str(s).strip().replace(",", ""))
    except (ValueError, AttributeError):
        return None


# ----------------------- 读数据 -----------------------

def read_equity(path):
    """返回按时间升序的 [(dt, total_equity_usd), ...]。
    容错:自动跳过表头行(第2列非数字的行)。只取前两列:时间、总权益。"""
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, newline="", encoding="utf-8-sig") as f:
        for parts in csv.reader(f):
            if len(parts) < 2:
                continue
            dt = parse_ts(parts[0])
            total = to_float(parts[1])
            if dt is None or total is None:
                continue  # 表头 / 空行 / 坏行,跳过
            rows.append((dt, total))
    rows.sort(key=lambda r: r[0])
    return rows


def read_flows(path):
    """返回 [(dt, signed_amount_usd), ...]。存入=+，取出=-。
    列: 日期时间, 方向(存入/取出), 金额USD, 哪个所, 备注"""
    flows = []
    if not os.path.exists(path):
        return flows
    with open(path, newline="", encoding="utf-8-sig") as f:
        for parts in csv.reader(f):
            if len(parts) < 3:
                continue
            dt = parse_ts(parts[0])
            amt = to_float(parts[2])
            if dt is None or amt is None:
                continue  # 表头 / 空行
            direction = (parts[1] or "").strip()
            sign = 1.0
            if any(k in direction for k in ("取出", "提取", "转出", "out", "withdraw")):
                sign = -1.0
            elif any(k in direction for k in ("存入", "转入", "充值", "in", "deposit")):
                sign = 1.0
            else:
                # 方向不认识:按金额正负,并记一个提示
                sign = 1.0
            flows.append((dt, sign * abs(amt)))
    flows.sort(key=lambda r: r[0])
    return flows


# ----------------------- 指标计算 -----------------------

def flow_between(flows, t_prev, t_cur):
    """区间 (t_prev, t_cur] 内的净流入(存入正、取出负)之和。"""
    s = 0.0
    for (t, amt) in flows:
        if t_prev < t <= t_cur:
            s += amt
    return s


def compute_metrics(equity, flows):
    """
    分段收益连乘算净值(NAV),出入金按 flows 扣除:
      每段收益 r_i = (E_i - 本段净流入) / E_{i-1} - 1
      NAV_i = NAV_{i-1} * (1 + r_i)，NAV_0 = 1.0
    这个口径的好处:用户可手算复核任一段
      (例:期初9994、期间充2000、期末12100 → 交易收益=(12100-2000)/9994-1)。
    返回 dict。
    """
    n = len(equity)
    result = {
        "points": [],          # [{"t": iso, "nav": float}]
        "n": n,
        "start": None,
        "end": None,
        "cum_return": None,     # 累计收益率(小数)
        "max_drawdown": None,   # 最大回撤(正数小数)
        "annualized": None,     # 年化(小数),数据不足返回 None
        "days": None,
        "flow_warning": False,
    }
    if n == 0:
        return result

    times = [e[0] for e in equity]
    result["start"] = times[0].strftime("%Y-%m-%d %H:%M")
    result["end"] = times[-1].strftime("%Y-%m-%d %H:%M")

    nav = [1.0]
    for i in range(1, n):
        e_prev = equity[i - 1][1]
        e_cur = equity[i][1]
        net_flow = flow_between(flows, times[i - 1], times[i])
        if e_prev <= 0:
            # 无法算收益,保持上一段净值(避免除0污染)
            nav.append(nav[-1])
            continue
        r = (e_cur - net_flow) / e_prev - 1.0
        nav.append(nav[-1] * (1.0 + r))

    result["points"] = [
        {"t": times[i].strftime("%Y-%m-%d %H:%M"), "nav": round(nav[i], 6)}
        for i in range(n)
    ]

    # 累计收益率
    result["cum_return"] = nav[-1] - 1.0

    # 最大回撤(基于净值)
    peak = nav[0]
    mdd = 0.0
    for v in nav:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > mdd:
                mdd = dd
    result["max_drawdown"] = mdd

    # 年化(数据不足则 None)
    span_days = (times[-1] - times[0]).total_seconds() / 86400.0
    result["days"] = span_days
    if span_days >= 1.0 and nav[-1] > 0:
        result["annualized"] = nav[-1] ** (365.0 / span_days) - 1.0

    return result


# ----------------------- 生成 HTML -----------------------

def fmt_pct(x, signed=True):
    if x is None:
        return "—"
    v = x * 100.0
    s = "%+.2f%%" % v if signed else "%.2f%%" % v
    return s


def build_html(m, now_bj):
    data_json = json.dumps(m["points"], ensure_ascii=False)

    cum = m["cum_return"]
    if cum is None:
        cum_str, cum_dir = "—", "flat"
    elif abs(cum) < 1e-9:
        cum_str, cum_dir = "0.00%", "flat"
    else:
        cum_str = fmt_pct(cum)
        cum_dir = "up" if cum > 0 else "down"

    mdd = m["max_drawdown"]
    if mdd is None:
        mdd_str = "—"
    elif mdd < 1e-9:
        mdd_str = "0.00%"
    else:
        mdd_str = "-%.2f%%" % (mdd * 100.0)

    ann = m["annualized"]
    if ann is None:
        ann_str, ann_dir = "—", "flat"
    elif abs(ann) < 1e-9:
        ann_str, ann_dir = "0.00%", "flat"
    else:
        ann_str = fmt_pct(ann)
        ann_dir = "up" if ann > 0 else "down"

    # 年化稳定性提示
    days = m["days"] or 0
    if m["n"] < 2:
        ann_note = "数据不足,暂无法计算"
    elif days < 7:
        ann_note = "测试期参考值 · 数据点少,会随时间大幅波动"
    elif days < 30:
        ann_note = "测试期参考值 · 数据尚少,仅供参考"
    else:
        ann_note = "测试期参考值"

    start = m["start"] or "—"
    end = m["end"] or "—"
    n = m["n"]
    updated = now_bj.strftime("%Y-%m-%d %H:%M")

    html = _TEMPLATE
    replacements = {
        "__DATA_JSON__": data_json,
        "__CUM_STR__": cum_str,
        "__CUM_DIR__": cum_dir,
        "__MDD_STR__": mdd_str,
        "__ANN_STR__": ann_str,
        "__ANN_DIR__": ann_dir,
        "__ANN_NOTE__": ann_note,
        "__N__": str(n),
        "__START__": start,
        "__END__": end,
        "__UPDATED__": updated,
    }
    for k, v in replacements.items():
        html = html.replace(k, v)
    return html


# HTML 模板(自包含,无外部依赖,手绘 SVG 折线 + hover;深浅色;表格视图)
_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>套利组合 · 业绩看板</title>
<style>
  :root{
    color-scheme: light;
    --page:#f9f9f7; --surface:#fcfcfb;
    --text-primary:#0b0b0b; --text-secondary:#52514e; --muted:#898781;
    --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,0.10);
    --series:#2a78d6; --series-soft:rgba(42,120,214,0.10);
    --good:#006300; --bad:#d03b3b;
  }
  :root[data-theme="dark"]{
    color-scheme: dark;
    --page:#0d0d0d; --surface:#1a1a19;
    --text-primary:#ffffff; --text-secondary:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
    --series:#3987e5; --series-soft:rgba(57,135,229,0.14);
    --good:#0ca30c; --bad:#e34948;
  }
  @media (prefers-color-scheme: dark){
    :root:not([data-theme="light"]){
      color-scheme: dark;
      --page:#0d0d0d; --surface:#1a1a19;
      --text-primary:#ffffff; --text-secondary:#c3c2b7; --muted:#898781;
      --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
      --series:#3987e5; --series-soft:rgba(57,135,229,0.14);
      --good:#0ca30c; --bad:#e34948;
    }
  }
  *{box-sizing:border-box}
  html,body{margin:0}
  body{
    background:var(--page); color:var(--text-primary);
    font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    line-height:1.5; -webkit-font-smoothing:antialiased;
  }
  .wrap{max-width:880px; margin:0 auto; padding:24px 20px 48px}
  header{display:flex; align-items:baseline; justify-content:space-between; gap:12px; flex-wrap:wrap}
  h1{font-size:20px; font-weight:600; margin:0}
  .sub{color:var(--text-secondary); font-size:13px; margin-top:2px}
  .toggle{
    border:1px solid var(--border); background:var(--surface); color:var(--text-secondary);
    border-radius:999px; padding:5px 12px; font-size:12px; cursor:pointer;
  }
  .toggle:hover{color:var(--text-primary)}
  .tiles{display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin:20px 0}
  @media (max-width:620px){.tiles{grid-template-columns:1fr}}
  .tile{
    background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:14px 16px;
  }
  .tile .label{font-size:12px; color:var(--text-secondary)}
  .tile .value{font-size:30px; font-weight:600; margin-top:4px; letter-spacing:-0.5px}
  .tile .note{font-size:11px; color:var(--muted); margin-top:4px}
  .value.up{color:var(--good)} .value.down{color:var(--bad)}
  .card{background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:16px}
  .card h2{font-size:14px; font-weight:600; margin:0 0 2px}
  .card .cap{font-size:12px; color:var(--text-secondary); margin-bottom:8px}
  .chartbox{position:relative; width:100%}
  svg{display:block; width:100%; height:auto; touch-action:pan-y}
  .tip{
    position:absolute; pointer-events:none; background:var(--surface);
    border:1px solid var(--border); border-radius:8px; padding:6px 9px; font-size:12px;
    color:var(--text-primary); box-shadow:0 4px 14px rgba(0,0,0,0.12); opacity:0; transition:opacity .08s;
    white-space:nowrap; transform:translate(-50%,-115%);
  }
  .tip .t{color:var(--muted); font-size:11px}
  .metarow{display:flex; gap:20px; flex-wrap:wrap; margin-top:14px; color:var(--text-secondary); font-size:13px}
  .metarow b{color:var(--text-primary); font-weight:600}
  footer{margin-top:22px; color:var(--muted); font-size:12px; line-height:1.7}
  .tablewrap{margin-top:14px; overflow:auto; display:none}
  table{border-collapse:collapse; width:100%; font-size:12px; font-variant-numeric:tabular-nums}
  th,td{text-align:right; padding:5px 10px; border-bottom:1px solid var(--grid)}
  th:first-child,td:first-child{text-align:left}
  .linkrow{margin-top:8px}
  .linkrow button{background:none;border:none;color:var(--series);cursor:pointer;font-size:12px;padding:0}
  .empty{color:var(--muted); font-size:13px; padding:30px 0; text-align:center}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>套利组合 · 业绩看板</h1>
      <div class="sub">净值以起始日 = 1.00 · 仅展示业绩(不含金额)</div>
    </div>
    <button class="toggle" id="themeBtn">切换深/浅色</button>
  </header>

  <div class="tiles">
    <div class="tile">
      <div class="label">累计收益率</div>
      <div class="value __CUM_DIR__">__CUM_STR__</div>
      <div class="note">起始日至今,已扣除出入金</div>
    </div>
    <div class="tile">
      <div class="label">最大回撤</div>
      <div class="value">__MDD_STR__</div>
      <div class="note">净值从高点回落的最大幅度</div>
    </div>
    <div class="tile">
      <div class="label">年化收益率</div>
      <div class="value __ANN_DIR__">__ANN_STR__</div>
      <div class="note">__ANN_NOTE__</div>
    </div>
  </div>

  <div class="card">
    <h2>净值曲线</h2>
    <div class="cap">起始点归一化为 1.00;曲线只反映业绩涨跌,不代表金额。</div>
    <div class="chartbox" id="chartbox">
      <div class="tip" id="tip"></div>
    </div>
    <div class="metarow">
      <span>数据点 <b>__N__</b> 个</span>
      <span>区间 <b>__START__</b> → <b>__END__</b></span>
      <span>最后更新 <b>__UPDATED__</b>(北京时间)</span>
    </div>
    <div class="linkrow"><button id="tblBtn">显示/隐藏数据表</button></div>
    <div class="tablewrap" id="tablewrap">
      <table id="tbl"><thead><tr><th>时间(北京)</th><th>净值</th><th>较起始</th></tr></thead><tbody></tbody></table>
    </div>
  </div>

  <footer>
    <div>说明:净值 = 组合业绩指数,起始点记为 1.00。收益率已用出入金记录扣除充值/提取,只反映交易盈亏。</div>
    <div>数据每 8 小时更新一次(北京 00:00 / 08:00 / 16:00);最大回撤只基于这些时点,盘中短时波动可能未被捕捉。</div>
    <div>本页仅供了解业绩表现,不含任何资金金额、账户余额或密钥,不构成投资建议。</div>
  </footer>
</div>

<script>
const DATA = __DATA_JSON__;

// ---------- 主题切换 ----------
const themeBtn = document.getElementById('themeBtn');
themeBtn.addEventListener('click', ()=>{
  const cur = document.documentElement.getAttribute('data-theme');
  const next = cur === 'dark' ? 'light' : (cur === 'light' ? 'dark'
      : (matchMedia('(prefers-color-scheme: dark)').matches ? 'light' : 'dark'));
  document.documentElement.setAttribute('data-theme', next);
  draw();
});

// ---------- 数据表 ----------
(function(){
  const tb = document.querySelector('#tbl tbody');
  DATA.forEach(d=>{
    const tr = document.createElement('tr');
    const chg = (d.nav-1)*100;
    tr.innerHTML = `<td>${d.t}</td><td>${d.nav.toFixed(4)}</td><td>${(chg>=0?'+':'')+chg.toFixed(2)}%</td>`;
    tb.appendChild(tr);
  });
  document.getElementById('tblBtn').addEventListener('click', ()=>{
    const w = document.getElementById('tablewrap');
    w.style.display = w.style.display === 'block' ? 'none' : 'block';
  });
})();

// ---------- 画图 ----------
const box = document.getElementById('chartbox');
const tip = document.getElementById('tip');
let pts = [];   // 屏幕坐标缓存

function cssVar(name){ return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function draw(){
  box.querySelectorAll('svg').forEach(s=>s.remove());
  const W = box.clientWidth || 800;
  const H = Math.max(240, Math.min(360, Math.round(W*0.42)));
  const padL = 46, padR = 16, padT = 14, padB = 26;
  const iw = W - padL - padR, ih = H - padT - padB;

  const svgNS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNS,'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('width', W); svg.setAttribute('height', H);
  box.appendChild(svg);

  if(!DATA.length){
    const t = document.createElementNS(svgNS,'text');
    t.setAttribute('x', W/2); t.setAttribute('y', H/2);
    t.setAttribute('text-anchor','middle'); t.setAttribute('fill', cssVar('--muted'));
    t.setAttribute('font-size','13'); t.textContent = '暂无数据,取数攒够后自动显示曲线';
    svg.appendChild(t); return;
  }

  // y 轴范围(净值),留 8% 余量;单点时给个对称小区间
  let lo = Math.min(...DATA.map(d=>d.nav));
  let hi = Math.max(...DATA.map(d=>d.nav));
  if(hi-lo < 1e-6){ lo -= 0.01; hi += 0.01; }
  const padY = (hi-lo)*0.10; lo -= padY; hi += padY;
  // 让 1.00 基准尽量落在范围内
  lo = Math.min(lo, 1.0 - (hi-lo)*0.02);
  hi = Math.max(hi, 1.0 + (hi-lo)*0.02);

  const n = DATA.length;
  const X = i => padL + (n===1 ? iw/2 : iw * i/(n-1));
  const Y = v => padT + ih * (1 - (v-lo)/(hi-lo));

  const grid = cssVar('--grid'), axis = cssVar('--axis'), muted = cssVar('--muted');
  const series = cssVar('--series'), surface = cssVar('--surface');

  // y 网格线 + 刻度(含 1.00 基准)
  const ticks = niceTicks(lo, hi, 4);
  ticks.forEach(tv=>{
    const y = Y(tv);
    const ln = document.createElementNS(svgNS,'line');
    ln.setAttribute('x1',padL); ln.setAttribute('x2',W-padR);
    ln.setAttribute('y1',y); ln.setAttribute('y2',y);
    ln.setAttribute('stroke', Math.abs(tv-1)<1e-9 ? axis : grid);
    ln.setAttribute('stroke-width','1');
    svg.appendChild(ln);
    const tx = document.createElementNS(svgNS,'text');
    tx.setAttribute('x', padL-8); tx.setAttribute('y', y+3.5);
    tx.setAttribute('text-anchor','end'); tx.setAttribute('fill', muted);
    tx.setAttribute('font-size','11'); tx.setAttribute('font-variant-numeric','tabular-nums');
    tx.textContent = tv.toFixed(2);
    svg.appendChild(tx);
  });

  // x 轴端点日期
  [0, n-1].forEach(i=>{
    if(n===1 && i===n-1) return;
    const tx = document.createElementNS(svgNS,'text');
    tx.setAttribute('x', X(i)); tx.setAttribute('y', H-8);
    tx.setAttribute('text-anchor', i===0?'start':'end'); tx.setAttribute('fill', muted);
    tx.setAttribute('font-size','11');
    tx.textContent = DATA[i].t.slice(5,10);
    svg.appendChild(tx);
  });

  pts = DATA.map((d,i)=>({x:X(i), y:Y(d.nav), d}));

  // 面积填充
  if(n>1){
    let ap = `M ${pts[0].x} ${Y(lo)} `;
    pts.forEach(p=> ap += `L ${p.x} ${p.y} `);
    ap += `L ${pts[n-1].x} ${Y(lo)} Z`;
    const area = document.createElementNS(svgNS,'path');
    area.setAttribute('d', ap); area.setAttribute('fill', 'var(--series-soft)');
    svg.appendChild(area);
  }

  // 折线
  if(n>1){
    let lp = `M ${pts[0].x} ${pts[0].y} `;
    pts.slice(1).forEach(p=> lp += `L ${p.x} ${p.y} `);
    const line = document.createElementNS(svgNS,'path');
    line.setAttribute('d', lp); line.setAttribute('fill','none');
    line.setAttribute('stroke', series); line.setAttribute('stroke-width','2');
    line.setAttribute('stroke-linejoin','round'); line.setAttribute('stroke-linecap','round');
    svg.appendChild(line);
  }

  // 末端点(带 surface 描边环)
  const last = pts[n-1];
  const dot = document.createElementNS(svgNS,'circle');
  dot.setAttribute('cx',last.x); dot.setAttribute('cy',last.y); dot.setAttribute('r','4.5');
  dot.setAttribute('fill',series); dot.setAttribute('stroke',surface); dot.setAttribute('stroke-width','2');
  svg.appendChild(dot);

  // hover 十字线 + 竖线
  const vline = document.createElementNS(svgNS,'line');
  vline.setAttribute('stroke', axis); vline.setAttribute('stroke-width','1'); vline.setAttribute('opacity','0');
  svg.appendChild(vline);
  const hdot = document.createElementNS(svgNS,'circle');
  hdot.setAttribute('r','4.5'); hdot.setAttribute('fill',series);
  hdot.setAttribute('stroke',surface); hdot.setAttribute('stroke-width','2'); hdot.setAttribute('opacity','0');
  svg.appendChild(hdot);

  function move(clientX){
    const r = svg.getBoundingClientRect();
    const mx = (clientX - r.left) * (W / r.width);
    let best=0, bd=1e9;
    pts.forEach((p,i)=>{ const dd=Math.abs(p.x-mx); if(dd<bd){bd=dd;best=i;} });
    const p = pts[best];
    vline.setAttribute('x1',p.x); vline.setAttribute('x2',p.x);
    vline.setAttribute('y1',padT); vline.setAttribute('y2',padT+ih); vline.setAttribute('opacity','1');
    hdot.setAttribute('cx',p.x); hdot.setAttribute('cy',p.y); hdot.setAttribute('opacity','1');
    const chg=(p.d.nav-1)*100;
    tip.innerHTML = `<div class="t">${p.d.t}</div>净值 ${p.d.nav.toFixed(4)} · ${(chg>=0?'+':'')+chg.toFixed(2)}%`;
    tip.style.left = (p.x / W * (svg.getBoundingClientRect().width)) + 'px';
    tip.style.top = (p.y / H * (svg.getBoundingClientRect().height)) + 'px';
    tip.style.opacity = 1;
  }
  function leave(){ vline.setAttribute('opacity','0'); hdot.setAttribute('opacity','0'); tip.style.opacity=0; }
  svg.addEventListener('mousemove', e=>move(e.clientX));
  svg.addEventListener('mouseleave', leave);
  svg.addEventListener('touchstart', e=>{ if(e.touches[0]) move(e.touches[0].clientX); }, {passive:true});
  svg.addEventListener('touchmove', e=>{ if(e.touches[0]) move(e.touches[0].clientX); }, {passive:true});
  svg.addEventListener('touchend', leave);
}

function niceTicks(lo, hi, count){
  const span = hi-lo; if(span<=0) return [lo];
  const raw = span/count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw/mag;
  let step = mag * (norm<1.5?1: norm<3?2: norm<7?5:10);
  const out=[]; let t=Math.ceil(lo/step)*step;
  for(; t<=hi+1e-9; t+=step) out.push(Math.round(t/step)*step);
  return out;
}

draw();
let rt; addEventListener('resize', ()=>{ clearTimeout(rt); rt=setTimeout(draw,120); });
</script>
</body>
</html>
"""


# ----------------------- GitHub 推送(可选) -----------------------

def github_push(html, cfg_path):
    """通过 GitHub Contents API 把 html 推送为仓库里的文件。
    github.json 结构:
      {"owner":"你的用户名","repo":"仓库名","path":"index.html",
       "branch":"main","token_file":"/root/cea/github_token"}
    token 单独存文件(chmod 600),不写进 github.json,也绝不进聊天。"""
    import urllib.request
    import urllib.error

    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    owner = cfg["owner"]; repo = cfg["repo"]
    path = cfg.get("path", "index.html")
    branch = cfg.get("branch", "main")
    token_file = cfg.get("token_file", os.path.join(os.path.dirname(cfg_path), "github_token"))
    with open(token_file, encoding="utf-8") as f:
        token = f.read().strip()

    api = "https://api.github.com/repos/%s/%s/contents/%s" % (owner, repo, path)
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "cea-build-site",
    }

    # 取现有文件 sha(存在才需要)
    sha = None
    req = urllib.request.Request(api + "?ref=" + branch, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            sha = json.loads(resp.read().decode()).get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise

    body = {
        "message": "update dashboard",
        "content": base64.b64encode(html.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha:
        body["sha"] = sha
    data = json.dumps(body).encode("utf-8")
    put = urllib.request.Request(api, data=data, headers=headers, method="PUT")
    with urllib.request.urlopen(put, timeout=30) as resp:
        code = resp.getcode()
    return code


# ----------------------- main -----------------------

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--equity", default=os.path.join(here, "equity.csv"))
    ap.add_argument("--flows", default=os.path.join(here, "flows.csv"))
    ap.add_argument("--out", default=os.path.join(here, "site", "index.html"))
    ap.add_argument("--push", action="store_true", help="生成后推送到 GitHub(需 github.json)")
    ap.add_argument("--github", default=os.path.join(here, "github.json"))
    ap.add_argument("--now", default=None, help="覆盖'最后更新'时间(测试用,格式 YYYY-MM-DD HH:MM)")
    args = ap.parse_args()

    equity = read_equity(args.equity)
    flows = read_flows(args.flows)
    m = compute_metrics(equity, flows)

    if args.now:
        now_bj = parse_ts(args.now) or datetime.now(BJ)
    else:
        now_bj = datetime.now(BJ)

    html = build_html(m, now_bj)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print("[build] 写入 %s (数据点 %d)" % (args.out, m["n"]))

    if args.push:
        if not os.path.exists(args.github):
            print("[push] 未找到 %s,跳过推送。" % args.github, file=sys.stderr)
            sys.exit(2)
        code = github_push(html, args.github)
        print("[push] GitHub 返回 %s" % code)


if __name__ == "__main__":
    main()
