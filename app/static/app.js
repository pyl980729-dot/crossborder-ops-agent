/* CrossBorder Ops Agent · 前端 */
(() => {
  "use strict";

  const state = { run: null, active: null, running: false };

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, html) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html !== undefined) node.innerHTML = html;
    return node;
  };

  /* ---------------- 工具 ---------------- */
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const pct = (v, d = 1) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(d)}%`);
  const money = (v, cur = "R$") =>
    v === null || v === undefined ? "—" : `${cur} ${Number(v).toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  const int = (v) => (v === null || v === undefined ? "—" : Number(v).toLocaleString("en-US"));
  const num = (v, d = 2) => (v === null || v === undefined ? "—" : Number(v).toFixed(d));

  async function api(path, opts = {}) {
    const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = (await res.json()).detail || msg; } catch (_) {}
      throw new Error(msg);
    }
    return res.json();
  }

  /* ---------------- 启动 ---------------- */
  async function boot() {
    try {
      const health = await api("/api/health");
      const badge = $("#llm-badge");
      const online = health.llm.mode === "online";
      badge.textContent = online ? `LLM 在线 · ${health.llm.name}` : "离线规则模式";
      badge.classList.toggle("online", online);
    } catch (e) {
      $("#llm-badge").textContent = "服务未就绪";
    }
    loadHistory();

    $("#run-btn").addEventListener("click", runPipeline);
    bindTabs();
    $("#add-task").addEventListener("click", createTask);
    document.querySelectorAll(".chip-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        $("#task-cron").value = btn.dataset.cron;
      });
    });
  }

  /* ---------------- 运行 ---------------- */
  async function runPipeline() {
    if (state.running) return;
    state.running = true;
    const btn = $("#run-btn");
    btn.disabled = true;
    btn.querySelector(".btn-label").textContent = "运行中…";
    btn.querySelector(".spinner").hidden = false;

    try {
      const run = await api("/api/runs", { method: "POST", body: JSON.stringify({}) });
      state.run = run;
      state.active = run.stages[0]?.stage ?? null;
      renderAll();
      loadHistory();
    } catch (e) {
      alert(`运行失败：${e.message}`);
    } finally {
      state.running = false;
      btn.disabled = false;
      btn.querySelector(".btn-label").textContent = "运行全链路";
      btn.querySelector(".spinner").hidden = true;
    }
  }

  function renderAll() {
    $("#summary").innerHTML = summaryHTML(state.run);
    $("#pipeline-wrap").hidden = false;
    $("#workspace").hidden = false;
    renderPipeline();
    renderDetail();
    renderBackflow();
  }

  /* ---------------- 摘要 ---------------- */
  function summaryHTML(run) {
    const s = run.summary;
    const statusText = { ok: "全链路通过", partial: "部分达标", blocked: "已阻塞" }[run.status] || run.status;
    const cards = [
      { k: "运行状态", v: statusText, s: `${run.elapsed_ms} ms · ${s.stage_count} 个 Stage`, cls: "accent" },
      { k: "动作清单", v: s.action_total, s: `其中 P0 ${s.p0_total} 条` },
      { k: "诊断结论", v: s.insight_total, s: "全部可溯源" },
      { k: "闭环回流", v: s.backflow_count, s: "条改法指回上游" },
      { k: "Stage 通过", v: `${s.ok}/${s.stage_count}`, s: s.blocked ? `${s.blocked} 个阻塞` : "全部完成" },
    ];
    return `<div class="stat-grid">${cards
      .map((c) => `<div class="stat ${c.cls || ""}"><div class="k">${c.k}</div><div class="v">${esc(c.v)}</div><div class="s">${esc(c.s)}</div></div>`)
      .join("")}</div>`;
  }

  /* ---------------- 流水线 ---------------- */
  function renderPipeline() {
    const wrap = $("#pipeline");
    wrap.innerHTML = "";
    state.run.stages.forEach((st, i) => {
      const node = el("div", `node ${st.status} ${st.stage === state.active ? "active" : ""}`);
      node.innerHTML = `
        <div class="idx">${String(i + 1).padStart(2, "0")}</div>
        <div>
          <div class="nm">${esc(st.title.replace(/^\d+\s*/, ""))}</div>
          <div class="sub">${st.elapsed_ms}ms · ${st.status}</div>
        </div>`;
      node.addEventListener("click", () => {
        state.active = st.stage;
        renderPipeline();
        renderDetail();
      });
      wrap.appendChild(node);
    });
  }

  /* ---------------- 详情 ---------------- */
  function renderDetail() {
    const stage = state.run.stages.find((s) => s.stage === state.active);
    if (!stage) return;
    const box = $("#detail");

    const insights = stage.insights
      .map(
        (i) => `<div class="insight ${i.level}">
            <span class="tag">${{ good: "优势", info: "解读", warn: "注意", risk: "风险" }[i.level] || i.level}</span>
            <div>${esc(i.text)}${i.evidence ? `<span class="ev">↳ ${esc(i.evidence)}</span>` : ""}</div>
          </div>`
      )
      .join("");

    const actions = stage.actions
      .map(
        (a) => `<tr>
            <td><span class="pri ${a.priority}">${a.priority}</span></td>
            <td>${esc(a.target)}</td>
            <td>${esc(a.change)}<div class="ev" style="color:#9ca3af;font-size:11px;margin-top:2px">${esc(a.reason)}</div></td>
            <td class="mono">${esc(a.verify || "—")}</td>
            <td>${esc(a.effort)}</td>
          </tr>`
      )
      .join("");

    const missing = stage.missing?.length
      ? `<div class="block"><div class="block-title">缺失字段</div><div class="chips">${stage.missing
          .map((m) => `<span class="chip miss">${esc(m)}</span>`)
          .join("")}</div></div>`
      : "";

    box.innerHTML = `
      <div class="detail-head">
        <div class="eyebrow">Stage ${stage.stage} · ${stage.status}</div>
        <h2>${esc(stage.title)}</h2>
        <div class="headline">${esc(stage.headline)}</div>
      </div>
      <div class="detail-body">
        ${missing}
        <div class="block"><div class="block-title">关键指标</div>${renderMetrics(stage)}</div>
        ${insights ? `<div class="block"><div class="block-title">诊断结论</div>${insights}</div>` : ""}
        ${
          actions
            ? `<div class="block"><div class="block-title">动作清单</div>
                <table><thead><tr><th>优先级</th><th>对象</th><th>动作</th><th>验证方式</th><th>成本</th></tr></thead>
                <tbody>${actions}</tbody></table></div>`
            : ""
        }
        <div class="block"><div class="block-title">溯源</div><div class="chips">${(stage.evidence || [])
          .map((e) => `<span class="chip">${esc(e)}</span>`)
          .join("")}</div></div>
      </div>`;
  }

  /* ---------------- 各 Stage 指标渲染 ---------------- */
  function renderMetrics(stage) {
    const m = stage.metrics || {};
    const fn = {
      market: renderMarket,
      strategy: renderStrategy,
      listing: renderListing,
      image: renderImage,
      video: renderVideo,
      ads: renderAds,
      review: renderReview,
    }[stage.stage];
    return fn ? fn(m) : `<pre class="mono">${esc(JSON.stringify(m, null, 2))}</pre>`;
  }

  const metricCard = (k, v, d = "") =>
    `<div class="metric"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="d">${esc(d)}</div></div>`;

  function renderMarket(m) {
    const s = m.summary || {};
    const c = m.concentration || {};
    const bands = m.price_bands || [];

    const cards = `<div class="metric-grid">${[
      metricCard("月销量", int(s.total_monthly_sales), "件"),
      metricCard("月 GMV", money(s.monthly_gmv), "按售价×销量"),
      metricCard("类目均价", money(s.avg_price), `${s.product_count} 个在榜商品`),
      metricCard(c.cr10_meaningful ? "CR10" : "CR3", pct(c.cr10_meaningful ? c.cr10 : c.cr3), c.verdict || ""),
    ].join("")}</div>`;

    const max = Math.max(...bands.map((b) => b.sales_share), 0.01);
    const bars = bands
      .map(
        (b) => `<div class="bar-row ${b.tag === "blue_ocean" ? "blue" : b.tag === "red_ocean" ? "red" : ""}">
          <span>${esc(b.label)}</span>
          <div class="bar-track"><div class="bar-fill" style="width:${Math.max((b.sales_share / max) * 100, 2)}%"></div></div>
          <span class="bar-num">${pct(b.sales_share, 0)} · ×${b.opportunity}</span>
        </div>`
      )
      .join("");

    const verdict = m.verdict || {};
    const chips = (verdict.reasons || []).map((r) => `<span class="chip">${esc(r)}</span>`).join("");

    return `${cards}
      <div style="margin-top:14px"><div class="block-title" style="margin-bottom:8px">价格带供需（绿=机会，红=红海，×N=机会指数）</div>
      <div class="bars">${bars}</div></div>
      <div style="margin-top:12px"><div class="chips">${chips}</div></div>`;
  }

  function renderStrategy(m) {
    const p = m.pricing || {};
    const cards = `<div class="metric-grid">${[
      metricCard("净利率", pct(p.net_margin_rate), `单件贡献 ${money(p.contribution)}`),
      metricCard("盈亏线 ROAS", num(p.breakeven_roas), `ACoS ${pct(p.breakeven_acos)}`),
      metricCard("可承受 CPC", money(p.affordable_cpc), `按 CVR ${pct(p.params?.cvr)}`),
      metricCard("达标最低价", money(p.required_price), `当前 ${money(p.price)}`),
    ].join("")}</div>`;

    const rows = (m.skus || [])
      .map(
        (s) => `<tr><td><b>${esc(s.role)}</b></td><td class="num">${money(s.price)}</td><td class="num">${money(s.cost)}</td>
        <td class="num">${pct(s.net_margin_rate)}</td><td class="num">${num(s.breakeven_roas)}</td>
        <td style="color:#6b7280;font-size:12px">${esc(s.note)}</td></tr>`
      )
      .join("");

    return `${cards}
      <div style="margin-top:14px">
        <table><thead><tr><th>角色</th><th>售价</th><th>成本</th><th>净利率</th><th>盈亏线 ROAS</th><th>职责</th></tr></thead>
        <tbody>${rows}</tbody></table>
      </div>`;
  }

  function renderListing(m) {
    const titles = m.titles || [];
    const cards = titles
      .map((t) => {
        const l = t.lint || {};
        const badge = l.passed ? `<span class="chip good">校验通过</span>` : `<span class="chip miss">未通过</span>`;
        return `<div class="cell" style="margin-bottom:9px">
            <div class="n">${esc(t.variant)} ${badge}</div>
            <div class="mono" style="line-height:1.6;color:#111827">${esc(t.text)}</div>
            <div class="p" style="margin-top:6px">${l.chars}/${l.title_max} 字符 · 关键词覆盖 ${pct(l.keyword_coverage, 0)}${l.stuffed_words?.length ? " · 堆砌: " + esc(l.stuffed_words.join(",")) : ""}</div>
          </div>`;
      })
      .join("");

    const sections = (m.detail_sections || [])
      .map((s, i) => `<div class="cell"><div class="n"><span>${i + 1}</span>${esc(s.title)}</div><div class="p">${esc(s.guide)}</div></div>`)
      .join("");

    return `${cards}<div style="margin-top:12px"><div class="block-title" style="margin-bottom:8px">详情页七段结构</div><div class="grid9">${sections}</div></div>`;
  }

  function renderImage(m) {
    const plan = m.plan || [];
    const cells = plan
      .map(
        (p) => `<div class="cell"><div class="n"><span>${String(plan.indexOf(p) + 1).padStart(2, "0")}</span>${esc(p.slot)}</div>
          <div class="p" style="margin-bottom:6px">${esc(p.purpose)}</div>
          <div class="mono" style="font-size:11px;color:#4b5563;line-height:1.5">${esc(p.prompt.slice(0, 120))}…</div>
          <div class="p" style="margin-top:6px;color:#b45309">${esc(p.compliance)}</div></div>`
      )
      .join("");
    return `<div class="metric-grid">${metricCard("规划图数", plan.length, `覆盖 ${pct(m.check?.coverage, 0)}`)}
      ${metricCard("完整度", m.check?.complete ? "完整" : "缺图", (m.check?.missing || []).join("、") || "无缺失")}
      ${metricCard("主卖点", (m.main_point || "").slice(0, 12), "贯穿图/视频/标题")}
      ${metricCard("主图规格", `${m.platform?.image_min_px}px`, m.platform?.image_bg || "")}</div>
      <div style="margin-top:14px"><div class="grid9">${cells}</div></div>`;
  }

  function renderVideo(m) {
    const t = m.timing || {};
    const sb = m.storyboard || [];
    const total = sb.reduce((a, s) => a + s.seconds, 0) || 1;
    const segs = sb
      .map((s) => `<div class="tl-seg" style="flex:${s.seconds / total}">${esc(s.scene)} ${s.seconds}s</div>`)
      .join("");
    const rows = sb
      .map(
        (s) => `<tr><td class="num">${s.start}-${s.end}s</td><td><b>${esc(s.scene)}</b></td>
        <td>${esc(s.narration)}</td><td style="color:#6b7280;font-size:12px">${esc(s.visual)}</td></tr>`
      )
      .join("");
    return `<div class="metric-grid">
        ${metricCard("目标时长", `${t.target_seconds}s`, `分镜合计 ${t.storyboard_seconds}s`)}
        ${metricCard("口播测算", `${t.needed_seconds}s`, `${t.cjk_chars} 汉字 + ${t.latin_chars} 拉丁字符`)}
        ${metricCard("时长校验", t.too_fast || t.over_duration ? "需压缩" : "匹配", t.too_fast ? "语速偏快" : "语速正常")}
        ${metricCard("主卖点", (m.main_point || "").slice(0, 12), "只讲这一件事")}
      </div>
      <div style="margin-top:16px"><div class="timeline">${segs}</div>
      <table><thead><tr><th>时间</th><th>段落</th><th>口播</th><th>画面</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  function renderAds(m) {
    const s = m.summary || {};
    const t = m.thresholds || {};
    const cards = `<div class="metric-grid">${[
      metricCard("广告花费", money(s.total_cost), `${s.campaign_count} 个广告组`),
      metricCard("整体 ROAS", num(s.overall_roas), `盈亏线 ${num(t.breakeven_roas)}`),
      metricCard("广告后净额", money(s.net_after_ads), s.net_after_ads >= 0 ? "净赚" : "净亏"),
      metricCard("P0 失血组", s.p0_count, `目标 ROAS ${num(t.target_roas)}`),
    ].join("")}</div>`;

    const rows = (m.campaigns || [])
      .map((c) => {
        const g = c.grade || {};
        return `<tr>
          <td><span class="pri ${g.level === "P0" ? "P0" : g.level === "P1" ? "P1" : g.level === "P2" ? "P2" : "P1"}">${esc(g.level)}</span></td>
          <td><b>${esc(c.campaign)}</b><div class="ev" style="color:#9ca3af;font-size:11px">${esc(g.reason || "")}</div></td>
          <td class="num">${money(c.cost)}</td>
          <td class="num">${num(c.roas)}</td>
          <td class="num">${pct(c.ctr, 2)}</td>
          <td class="num">${c.orders}</td>
          <td class="num">${money(c.cpa)}</td>
        </tr>`;
      })
      .join("");

    return `${cards}
      <div style="margin-top:14px">
      <table><thead><tr><th>分级</th><th>广告组 / 诊断</th><th>花费</th><th>ROAS</th><th>CTR</th><th>订单</th><th>CPA</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }

  function renderReview(m) {
    const a = m.aggregate || {};
    const cards = `<div class="metric-grid">${[
      metricCard("评论总数", int(a.total), `平均分 ${a.avg_rating}`),
      metricCard("好评率", pct(a.good_rate), `差评 ${a.bad_count} 条`),
      metricCard("可修复率", pct(a.fixable_rate), `${a.fixable_count} 条改内容可消`),
      metricCard("带图率", pct(a.with_image_rate), "影响转化"),
    ].join("")}</div>`;

    const rows = (a.bad_by_category || [])
      .map(
        (c) => `<tr>
          <td><b>${esc(c.category)}</b></td>
          <td class="num">${c.count}</td>
          <td class="num">${pct(c.share)}</td>
          <td>${c.fixable ? `<span class="chip good">内容可修复</span>` : `<span class="chip miss">需供应链</span>`}</td>
          <td style="color:#6b7280;font-size:12px">${esc((m.playbook?.[c.category] || ["", ""])[1] || "")}</td>
        </tr>`
      )
      .join("");

    return `${cards}
      <div style="margin-top:14px">
      <table><thead><tr><th>归因</th><th>条数</th><th>占差评</th><th>类型</th><th>修补动作</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }

  /* ---------------- 回流与历史 ---------------- */
  function renderBackflow() {
    const box = $("#backflow");
    const fb = state.run.backflow || [];
    if (!fb.length) {
      box.innerHTML = `<p class="muted">本次运行无回流指令</p>`;
      return;
    }
    box.innerHTML =
      fb
        .map(
          (f) => `<div class="flow-item rerun" data-stage="${esc(f.to_stage)}" title="点击重跑该环节">
          <span class="mono">${esc(f.from_stage)}</span>
          <span class="arrow">→</span>
          <span class="mono">${esc(f.to_stage)}</span>
          <span class="cnt">${f.count}</span>
        </div>
        <div class="muted" style="font-size:11.5px;padding:0 0 8px 0">${esc(f.scope)}</div>`
        )
        .join("") + `<p class="muted" style="margin-top:8px;font-size:11px">点击任一回流项 → 只重跑下游环节，不重跑全链路</p>`;

    box.querySelectorAll(".rerun").forEach((node) => {
      node.addEventListener("click", () => rerunStage(node.dataset.stage));
    });
  }

  async function rerunStage(stage) {
    if (!state.run?.run_id) return;
    try {
      const saved = await api(`/api/runs/${state.run.run_id}`);
      const res = await api(`/api/stages/${stage}`, { method: "POST", body: JSON.stringify(saved.input) });
      const idx = state.run.stages.findIndex((s) => s.stage === stage);
      if (idx >= 0) state.run.stages[idx] = res;
      state.active = stage;
      renderPipeline();
      renderDetail();
    } catch (e) {
      alert(`重跑失败：${e.message}`);
    }
  }

  async function loadHistory() {
    try {
      const data = await api("/api/runs?limit=8");
      const box = $("#history");
      $("#history-count").textContent = `共 ${data.stats.total_runs} 次`;
      if (!data.runs.length) {
        box.innerHTML = `<p class="muted">暂无记录</p>`;
        return;
      }
      box.innerHTML = data.runs
        .map(
          (r) => `<div class="hist-item" data-id="${esc(r.id)}">
            <div class="h-title"><span class="dot ${r.status}"></span> ${esc(r.title)}</div>
            <div class="h-meta">${esc(r.created_at.replace("T", " ").slice(5, 16))} · ${r.elapsed_ms}ms · ${esc(r.llm_mode)}</div>
          </div>`
        )
        .join("");
      box.querySelectorAll(".hist-item").forEach((node) => {
        node.addEventListener("click", async () => {
          const run = await api(`/api/runs/${node.dataset.id}`);
          state.run = run.result;
          state.active = run.result.stages[0]?.stage;
          renderAll();
        });
      });
    } catch (_) {
      /* 历史加载失败不影响主流程 */
    }
  }

  /* ================= 视图切换 ================= */
  function bindTabs() {
    document.querySelectorAll("#tabs .tab").forEach((tab) => {
      tab.addEventListener("click", () => switchView(tab.dataset.view));
    });
  }

  function switchView(name) {
    document.querySelectorAll("#tabs .tab").forEach((t) => {
      t.classList.toggle("active", t.dataset.view === name);
    });
    $("#view-flow").hidden = name !== "flow";
    $("#view-auto").hidden = name !== "auto";
    if (name === "auto") loadAutomation();
  }

  /* ================= 自动化 ================= */
  function fmtCountdown(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    if (seconds <= 0) return "即将执行";
    if (seconds < 60) return `${seconds} 秒后`;
    const m = Math.floor(seconds / 60);
    if (m < 60) return `${m} 分钟后`;
    const h = Math.floor(m / 60);
    const rm = m % 60;
    if (h < 24) return `${h} 小时${rm ? ` ${rm} 分` : ""}后`;
    return `${Math.floor(h / 24)} 天${h % 24 ? ` ${h % 24} 小时` : ""}后`;
  }

  function fmtTime(iso) {
    if (!iso) return "—";
    return iso.replace("T", " ").slice(5, 16);
  }

  async function loadAutomation() {
    try {
      const data = await api("/api/automation/status");
      renderAutoStats(data);
      renderTasks(data.tasks || []);
      loadLatestCycle();
    } catch (e) {
      $("#task-list").innerHTML = `<p class="muted">加载失败：${esc(e.message)}</p>`;
    }
  }

  function renderAutoStats(d) {
    const running = d.state === "running";
    const enabled = (d.tasks || []).filter((t) => t.enabled).length;
    const next = (d.tasks || [])
      .filter((t) => t.enabled && t.seconds_to_next !== null)
      .sort((a, b) => a.seconds_to_next - b.seconds_to_next)[0];

    const cards = [
      {
        k: "调度器",
        v: running ? "运行中" : "已停止",
        s: `每 ${d.tick_seconds} 秒检查一次`,
        cls: running ? "accent" : "",
      },
      { k: "已配置任务", v: `${enabled} / ${(d.tasks || []).length}`, s: "启用 / 总数" },
      { k: "下次执行", v: next ? fmtCountdown(next.seconds_to_next) : "—", s: next ? next.name : "没有启用中的任务" },
      { k: "自动触发", v: d.stats?.scheduled_runs ?? 0, s: `累计运行 ${d.stats?.total_runs ?? 0} 次` },
      { k: "服务器时间", v: fmtTime(d.server_time), s: d.last_error ? "有错误" : "正常" },
    ];

    $("#auto-stats").innerHTML = cards
      .map(
        (c) => `<div class="stat ${c.cls || ""}"><div class="k">${c.k}</div>
        <div class="v" style="font-size:${String(c.v).length > 6 ? "18px" : "24px"}">${esc(c.v)}</div>
        <div class="s">${esc(c.s)}</div></div>`
      )
      .join("");
  }

  function renderTasks(tasks) {
    const box = $("#task-list");
    if (!tasks.length) {
      box.innerHTML = `<p class="muted">还没有定时任务。下面填个名字选个时间就能建。</p>`;
      return;
    }
    box.innerHTML = tasks
      .map(
        (t) => `<div class="task-row ${t.enabled ? "" : "off"}">
          <div class="task-main">
            <div class="task-name">${esc(t.name)}
              ${t.valid ? "" : `<span class="chip miss">cron 非法</span>`}
            </div>
            <div class="task-meta">
              <code>${esc(t.cron)}</code> · ${esc(t.describe || "")}
              · 上次 ${fmtTime(t.last_run_at)}
              · 下次 ${
                t.enabled && t.next_run_at
                  ? `${fmtTime(t.next_run_at)}（${fmtCountdown(t.seconds_to_next)}）`
                  : "—"
              }
            </div>
          </div>
          <div class="task-ops">
            <button class="btn btn-sm" data-act="run" data-id="${esc(t.id)}">立即执行</button>
            <button class="btn btn-sm" data-act="toggle" data-id="${esc(t.id)}" data-on="${t.enabled}">
              ${t.enabled ? "暂停" : "启用"}
            </button>
            <button class="btn btn-sm danger" data-act="del" data-id="${esc(t.id)}">删除</button>
          </div>
        </div>`
      )
      .join("");

    box.querySelectorAll("button[data-act]").forEach((btn) => {
      btn.addEventListener("click", () => handleTaskAction(btn.dataset.act, btn.dataset.id, btn));
    });
  }

  async function handleTaskAction(act, id, btn) {
    try {
      if (act === "run") {
        btn.disabled = true;
        btn.textContent = "执行中…";
        const run = await api(`/api/automation/schedules/${id}/run`, { method: "POST" });
        state.run = run;
        state.active = run.stages[0]?.stage;
        renderAll();
        loadAutomation();
        return;
      }
      if (act === "toggle") {
        const enable = btn.dataset.on !== "true";
        await api(`/api/automation/schedules/${id}?enabled=${enable}`, { method: "PATCH" });
      }
      if (act === "del") {
        if (!confirm("删除这个定时任务？历史运行记录会保留。")) return;
        await api(`/api/automation/schedules/${id}`, { method: "DELETE" });
      }
      loadAutomation();
    } catch (e) {
      alert(`操作失败：${e.message}`);
      btn.disabled = false;
    }
  }

  async function createTask() {
    const name = $("#task-name").value.trim();
    const cron = $("#task-cron").value.trim();
    if (!name) return alert("请填写任务名称");
    if (!cron) return alert("请填写 cron 表达式");
    try {
      await api("/api/automation/schedules", {
        method: "POST",
        body: JSON.stringify({ name, cron }),
      });
      $("#task-name").value = "";
      loadAutomation();
    } catch (e) {
      alert(`创建失败：${e.message}`);
    }
  }

  /* ---- 闭环对比 ---- */
  async function loadLatestCycle() {
    const box = $("#cycle-panel");
    try {
      const list = await api("/api/runs?limit=1");
      if (!list.runs?.length) {
        box.innerHTML = `<p class="muted">还没有运行记录。跑一次全链路就能看到对比。</p>`;
        return;
      }
      const detail = await api(`/api/runs/${list.runs[0].id}`);
      const cycle = detail.result?.cycle;
      if (!cycle) {
        box.innerHTML = `<p class="muted">这次运行没有闭环数据。</p>`;
        return;
      }
      renderCycle(cycle, detail);
    } catch (e) {
      box.innerHTML = `<p class="muted">加载失败：${esc(e.message)}</p>`;
    }
  }

  function renderCycle(cycle, detail) {
    const tone = { 改善: "good", 恶化: "bad", 持平: "flat", 首次运行: "first" }[cycle.verdict] || "flat";
    // 箭头只表数值方向（↑升 ↓降），颜色只表好坏（绿好红坏）。
    // 混在一起会读反：P0 组从 0 涨到 2 是「↑ +2 红色」，不是「↓」。
    const arrowOf = (r) => (r.delta > 0 ? "↑" : r.delta < 0 ? "↓" : "=");
    const cls = { better: "better", worse: "worse", same: "same" };

    const rows = (cycle.rows || [])
      .map(
        (r) => `<div class="cyc-row">
          <span class="cyc-label">${esc(r.label)}</span>
          <span class="cyc-val">${esc(r.prev)}</span>
          <span class="cyc-arrow">→</span>
          <span class="cyc-val strong">${esc(r.curr)}</span>
          <span class="cyc-trend ${cls[r.trend]}">${arrowOf(r)} ${r.delta > 0 ? "+" : ""}${r.delta}</span>
        </div>`
      )
      .join("");

    const pending = cycle.pending || {};
    const staleAll = pending.stale || [];
    const stale = staleAll
      .slice(0, 4)
      .map(
        (a) => `<div class="pend-item stale">
          <span class="pri ${a.priority}">${a.priority}</span>
          <div><b>${esc(a.target)}</b><div class="pend-sub">${esc(a.change)}</div></div>
        </div>`
      )
      .join("");

    const resolved = (pending.resolved || [])
      .map(
        (a) => `<div class="pend-item done">
          <span class="done-mark">✓</span>
          <div><b>${esc(a.target)}</b><div class="pend-sub">已消除</div></div>
        </div>`
      )
      .join("");

    $("#cycle-panel").innerHTML = `
      <div class="verdict ${tone}">
        <div class="verdict-label">${esc(cycle.verdict)}</div>
        <div class="verdict-reason">${esc(cycle.reason || "")}</div>
        ${
          cycle.baseline_at
            ? `<div class="verdict-base">对比基线：${fmtTime(cycle.baseline_at)}（${esc(cycle.baseline_run_id || "")}）</div>`
            : ""
        }
      </div>
      ${rows ? `<div class="cyc-table">${rows}</div>` : ""}
      ${
        stale
          ? `<div class="block-title" style="margin-top:16px">上次的 P0 仍未消除（${staleAll.length}）</div>
             <div class="pend-list">${stale}</div>
             ${staleAll.length > 4 ? `<p class="preview-note" style="margin-top:8px">另有 ${staleAll.length - 4} 条，去「工作流」页看完整清单。</p>` : ""}`
          : ""
      }
      ${resolved ? `<div class="block-title" style="margin-top:14px">已消除</div><div class="pend-list">${resolved}</div>` : ""}
      ${
        pending.new?.length
          ? `<p class="preview-note" style="margin-top:12px">另有 ${pending.new.length} 条新增 P0，去「工作流」页看完整动作清单。</p>`
          : ""
      }`;
  }

  boot();
})();
