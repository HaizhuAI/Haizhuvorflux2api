/* ═══════════ Vorflux Gateway Console ═══════════ */
(() => {
"use strict";

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
let TOKEN = sessionStorage.getItem("vgw_admin") || "";
let evtSrc = null;

/* ---------------- API ---------------- */
async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json",
               "Authorization": "Bearer " + TOKEN,
               ...(opts.headers || {}) },
  });
  if (r.status === 401) { logout(); throw new Error("unauthorized"); }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(data?.error?.message || data?.detail || data?.message || `HTTP ${r.status}`);
  return data;
}
const post = (p, b) => api(p, { method: "POST", body: JSON.stringify(b || {}) });
const patch = (p, b) => api(p, { method: "PATCH", body: JSON.stringify(b || {}) });
const put = (p, b) => api(p, { method: "PUT", body: JSON.stringify(b || {}) });
const del = p => api(p, { method: "DELETE" });

/* ---------------- toast ---------------- */
function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("#toast-wrap").appendChild(el);
  gsap.fromTo(el, { x: 60, opacity: 0 }, { x: 0, opacity: 1, duration: .45, ease: "power3.out" });
  setTimeout(() => gsap.to(el, { x: 60, opacity: 0, duration: .35, onComplete: () => el.remove() }), 3600);
}

/* ---------------- login ---------------- */
async function tryLogin() {
  const tok = $("#login-token").value.trim();
  if (!tok) return;
  try {
    const r = await api("/admin/api/login", { method: "POST", body: JSON.stringify({ token: tok }) });
    TOKEN = r.session;
    sessionStorage.setItem("vgw_admin", TOKEN);
    enterApp();
  } catch (e) {
    $("#login-err").textContent = "Token 无效";
    gsap.fromTo(".login-card", { x: -8 }, { x: 0, duration: .4, ease: "elastic.out(1,.3)" });
  }
}
function logout() {
  TOKEN = ""; sessionStorage.removeItem("vgw_admin");
  if (evtSrc) { evtSrc.close(); evtSrc = null; }
  $("#app").classList.add("hidden");
  $("#login-view").classList.remove("hidden");
}
$("#login-btn").onclick = tryLogin;
$("#login-token").addEventListener("keydown", e => e.key === "Enter" && tryLogin());
$("#logout-btn").onclick = async () => { try { await post("/admin/api/logout"); } catch {} logout(); };

/* ---------------- enter app ---------------- */
async function enterApp() {
  $("#login-view").classList.add("hidden");
  $("#app").classList.remove("hidden");
  gsap.fromTo(".sidebar", { x: -40, opacity: 0 }, { x: 0, opacity: 1, duration: .6, ease: "power3.out" });
  gsap.fromTo(".nav-item", { x: -18, opacity: 0 }, { x: 0, opacity: 1, stagger: .06, duration: .45, ease: "power2.out", delay: .15 });
  connectStream();
  loadAll();
}

/* ---------------- router ---------------- */
const pages = ["dashboard", "accounts", "keys", "playground", "logs", "settings"];
function go(page) {
  $$(".nav-item").forEach(n => n.classList.toggle("active", n.dataset.page === page));
  pages.forEach(p => {
    const el = $("#page-" + p);
    if (p === page) {
      el.classList.remove("hidden");
      gsap.fromTo(el, { y: 18, opacity: 0 }, { y: 0, opacity: 1, duration: .45, ease: "power2.out" });
      gsap.fromTo(el.querySelectorAll(".kpi,.panel,.acard"), { y: 14, opacity: 0 },
        { y: 0, opacity: 1, stagger: .04, duration: .4, ease: "power2.out", clearProps: "transform" });
    } else el.classList.add("hidden");
  });
  if (page === "keys") loadKeys();
  if (page === "logs") loadLogs();
  if (page === "settings") loadSettings();
  if (page === "playground") loadModels();
  if (page === "accounts" || page === "dashboard") refreshAccounts();
}
$$(".nav-item").forEach(n => n.onclick = () => go(n.dataset.page));

/* ---------------- live stream ---------------- */
function connectStream() {
  if (evtSrc) evtSrc.close();
  // EventSource can't set headers -> pass token via query is not supported by backend;
  // fall back to polling overview every 2.5s
  const tick = async () => {
    if (!TOKEN) return;
    try {
      const d = await api("/admin/api/overview");
      renderKpis(d);
      $("#conn-txt").textContent = "live";
      $("#conn-pill .pulse").classList.remove("down");
    } catch {
      $("#conn-txt").textContent = "disconnected";
      $("#conn-pill .pulse").classList.add("down");
    }
    setTimeout(tick, 2500);
  };
  tick();
  setInterval(refreshAccounts, 4000);
}

/* ---------------- dashboard ---------------- */
function renderKpis(d) {
  animNum("#k-healthy", d.accounts_healthy);
  $("#k-total").textContent = "/ " + d.accounts_total + " accounts";
  animNum("#k-inflight", d.inflight);
  animNum("#k-req", d.requests);
  const okr = d.requests ? Math.round(100 * d.ok / d.requests) : 100;
  $("#k-okrate").textContent = okr + "% ok";
  animNum("#k-lat", d.avg_latency_ms);
  $("#k-up").textContent = fmtUptime(d.uptime_s);
  $("#dash-upstream").textContent = "upstream: " + (d.upstream || "") + (d.global_proxy ? " · global proxy: " + d.global_proxy : "");
}
function animNum(sel, val) {
  const el = $(sel); if (!el) return;
  const from = parseInt(el.dataset.v || "0", 10);
  el.dataset.v = val;
  const o = { v: from };
  gsap.to(o, { v: val, duration: .6, ease: "power1.out",
    onUpdate: () => el.textContent = Math.round(o.v).toLocaleString() });
}
function fmtUptime(s) {
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h " + Math.floor(s % 3600 / 60) + "m";
  return Math.floor(s / 86400) + "d";
}
function badge(st, healthy, cooling) {
  if (st === "disabled") return `<span class="badge dim">disabled</span>`;
  if (st === "no_credits") return `<span class="badge warn">余额不足</span>`;
  if (cooling) return `<span class="badge warn">cooldown</span>`;
  if (healthy) return `<span class="badge ok">healthy</span>`;
  return `<span class="badge bad">failing</span>`;
}

/* ---------------- accounts ---------------- */
async function refreshAccounts() {
  let d;
  try { d = await api("/admin/api/accounts"); } catch { return; }
  renderDashAccounts(d.accounts);
  renderAccountList(d.accounts);
}
function renderDashAccounts(accs) {
  const el = $("#dash-accounts");
  if (!accs.length) { el.innerHTML = `<p class="muted">暂无账号 — 到「账号池」添加第一个 Vorflux 账号</p>`; return; }
  el.innerHTML = accs.map(a => `
    <div class="acard">
      <div class="email">${esc(a.email)}</div>
      <div class="meta">${esc(a.account_name || a.account_id || "—")}</div>
      <div class="rowline">
        ${badge(a.status, a.healthy, a.cooldown_until > Date.now()/1000)}
        <span>并发 ${a.inflight}/${a.max_concurrent}</span>
        <span>${a.token_valid ? "token ✓" : "token ✗"}</span>
      </div>
      <div class="inflight-bar"><i style="width:${Math.min(100, 100*a.inflight/a.max_concurrent)}%"></i></div>
    </div>`).join("");
}
function renderAccountList(accs) {
  const el = $("#accounts-list");
  if (!accs.length) { el.innerHTML = `<div class="panel"><p class="muted">还没有账号。点击「+ 添加账号」用邮箱验证码登录。</p></div>`; return; }
  el.innerHTML = accs.map(a => {
    const cool = a.cooldown_until > Date.now()/1000;
    return `
    <div class="acard" data-id="${a.id}">
      <div style="display:flex;justify-content:space-between;align-items:start">
        <div>
          <div class="email">${esc(a.email)}${a.auth_kind === "oauth" ? ' <span class="tag oauth">OAuth</span>' : ""}</div>
          <div class="meta">acct: ${esc(a.account_id || "pending…")} ${a.proxy ? "· proxy: " + esc(a.proxy) : ""}</div>
        </div>
        ${badge(a.status, a.healthy, cool)}
      </div>
      <div class="rowline">
        <span>并发 <b>${a.inflight}</b>/${a.max_concurrent}</span>
        <span>失败计数 ${a.fail_count}</span>
        ${cool ? `<span>冷却至 ${new Date(a.cooldown_until*1000).toLocaleTimeString()}</span>` : ""}
        <span>${a.token_valid ? "token 有效" : "token 失效"}</span>
        <span>requests ${a.stats?.requests || 0}</span>
      </div>
      <div class="inflight-bar"><i style="width:${Math.min(100, 100*a.inflight/a.max_concurrent)}%"></i></div>
      <div class="actions">
        <button class="btn sm" data-act="test">测试</button>
        <button class="btn sm" data-act="refresh">刷新Token</button>
        <button class="btn sm" data-act="reset">重置熔断</button>
        <button class="btn sm" data-act="edit">编辑</button>
        <button class="btn sm ${a.status==="active"?"":"primary"}" data-act="toggle">${a.status==="active"?"禁用":"启用"}</button>
        <button class="btn sm danger" data-act="del">删除</button>
      </div>
    </div>`;
  }).join("");
  $$("#accounts-list .acard .actions button").forEach(b => {
    b.onclick = () => acctAction(+b.closest(".acard").dataset.id, b.dataset.act, b);
  });
}
async function acctAction(id, act, btn) {
  try {
    if (act === "test") {
      btn.disabled = true; btn.textContent = "…";
      const r = await post(`/admin/api/accounts/${id}/test`);
      btn.disabled = false; btn.textContent = "测试";
      if (r.ok) toast(`OK · ${r.latency_ms}ms · 默认模型 ${r.default_model || "?"} · 余额 $${(r.credit_balance?.balanceUsd ?? "?")}`, "ok");
      else toast("测试失败: " + (r.error || ""), "err");
      return;
    }
    if (act === "refresh") { await post(`/admin/api/accounts/${id}/refresh`); toast("Token 已刷新", "ok"); }
    if (act === "reset") { await post(`/admin/api/accounts/${id}/reset-cb`); toast("熔断已重置", "ok"); }
    if (act === "toggle") {
      const cur = btn.textContent === "禁用" ? "disabled" : "active";
      await patch(`/admin/api/accounts/${id}`, { status: cur });
      toast(cur === "active" ? "已启用" : "已禁用");
    }
    if (act === "del") {
      if (!confirm("确认删除该账号？")) return;
      await del(`/admin/api/accounts/${id}`); toast("已删除");
    }
    if (act === "edit") { openEdit(id); return; }
    refreshAccounts();
  } catch (e) { toast(e.message, "err"); if (act==="test"){btn.disabled=false;btn.textContent="测试";} }
}

/* ---------------- add account modal ---------------- */
function openModal(sel) {
  const m = $(sel); m.classList.remove("hidden");
  gsap.fromTo(m.querySelector(".modal"), { y: 26, opacity: 0, scale: .97 },
    { y: 0, opacity: 1, scale: 1, duration: .35, ease: "power3.out" });
}
function closeModal(sel) { $(sel).classList.add("hidden"); }
$("#add-account-btn").onclick = () => { $("#acct-err").textContent = ""; openModal("#acct-modal"); };
$("#acct-close").onclick = () => closeModal("#acct-modal");
$$("#acct-modal .tab").forEach(t => t.onclick = () => {
  $$("#acct-modal .tab").forEach(x => x.classList.toggle("active", x === t));
  $("#tab-otp").classList.toggle("hidden", t.dataset.tab !== "otp");
  $("#tab-token").classList.toggle("hidden", t.dataset.tab !== "token");
});
$("#otp-send").onclick = async () => {
  const email = $("#otp-email").value.trim(), proxy = $("#otp-proxy").value.trim();
  if (!email) return $("#acct-err").textContent = "请输入邮箱";
  $("#otp-send").disabled = true;
  try {
    await post("/admin/api/accounts/otp/start", { email, proxy });
    $("#otp-email-echo").textContent = email;
    $("#otp-step1").classList.add("hidden");
    $("#otp-step2").classList.remove("hidden");
    $("#acct-err").textContent = "";
    gsap.fromTo("#otp-step2", { x: 20, opacity: 0 }, { x: 0, opacity: 1, duration: .35 });
  } catch (e) { $("#acct-err").textContent = e.message; }
  $("#otp-send").disabled = false;
};
$("#otp-back").onclick = () => { $("#otp-step2").classList.add("hidden"); $("#otp-step1").classList.remove("hidden"); };
$("#otp-verify").onclick = async () => {
  const email = $("#otp-email").value.trim(), code = $("#otp-code").value.trim(),
        proxy = $("#otp-proxy").value.trim();
  if (code.length !== 6) return $("#acct-err").textContent = "请输入 6 位验证码";
  $("#otp-verify").disabled = true;
  try {
    await post("/admin/api/accounts/otp/verify", { email, code, proxy });
    toast("账号已添加", "ok");
    closeModal("#acct-modal");
    $("#otp-step2").classList.add("hidden"); $("#otp-step1").classList.remove("hidden");
    refreshAccounts();
  } catch (e) { $("#acct-err").textContent = e.message; }
  $("#otp-verify").disabled = false;
};
$("#imp-add").onclick = async () => {
  const b = { email: $("#imp-email").value.trim(), refresh_token: $("#imp-token").value.trim(),
              proxy: $("#imp-proxy").value.trim(), account_id: $("#imp-accid").value.trim() };
  if (!b.refresh_token) return $("#acct-err").textContent = "需要 refresh_token";
  $("#imp-add").disabled = true;
  try {
    await post("/admin/api/accounts/import", b);
    toast("账号已导入", "ok"); closeModal("#acct-modal"); refreshAccounts();
  } catch (e) { $("#acct-err").textContent = e.message; }
  $("#imp-add").disabled = false;
};

/* ---------------- edit modal ---------------- */
let editId = null;
async function openEdit(id) {
  editId = id;
  const d = await api("/admin/api/accounts");
  const a = d.accounts.find(x => x.id === id);
  if (!a) return;
  $("#edit-email").textContent = a.email;
  $("#edit-proxy").value = a.proxy || "";
  $("#edit-maxc").value = a.max_concurrent;
  $("#edit-err").textContent = "";
  openModal("#edit-modal");
}
$("#edit-close").onclick = () => closeModal("#edit-modal");
$("#edit-save").onclick = async () => {
  try {
    await patch(`/admin/api/accounts/${editId}`, {
      proxy: $("#edit-proxy").value.trim(),
      max_concurrent: +$("#edit-maxc").value || 0,
    });
    toast("已保存", "ok"); closeModal("#edit-modal"); refreshAccounts();
  } catch (e) { $("#edit-err").textContent = e.message; }
};

/* ---------------- keys ---------------- */
async function loadKeys() {
  const d = await api("/admin/api/keys");
  const tb = $("#keys-tbl tbody");
  tb.innerHTML = d.keys.map(k => `
    <tr><td class="mono">${esc(k.key.slice(0,14))}…<button class="btn sm ghost" onclick="navigator.clipboard.writeText('${k.key}')">复制</button></td>
    <td>${esc(k.name||"—")}</td><td>${new Date(k.created_at*1000).toLocaleString()}</td>
    <td><button class="btn sm danger" data-kid="${k.id}">删除</button></td></tr>`).join("")
    || `<tr><td colspan="4" class="muted">暂无 key${d.master_key_set ? "" : "（提示：可设置环境变量 API_KEY 作为主 key）"}</td></tr>`;
  $$("#keys-tbl [data-kid]").forEach(b => b.onclick = async () => {
    await del("/admin/api/keys/" + b.dataset.kid); loadKeys(); toast("已删除");
  });
}
$("#add-key-btn").onclick = async () => {
  const name = prompt("Key 名称 (可选)") || "";
  const r = await post("/admin/api/keys", { name });
  toast("已创建: " + r.key, "ok"); loadKeys();
};

/* ---------------- logs ---------------- */
async function loadLogs() {
  const d = await api("/admin/api/logs?limit=200");
  $("#logs-tbl tbody").innerHTML = d.logs.map(l => `
    <tr><td class="mono">${new Date(l.ts*1000).toLocaleTimeString()}</td>
    <td>${esc(l.account_email||"—")}</td><td class="mono">${esc(l.model||"")}</td>
    <td>${esc(l.kind||"")}</td><td class="${l.status==="ok"?"st-ok":"st-err"}">${l.status}</td>
    <td class="mono">${l.latency_ms}ms</td><td class="mono">${esc((l.session_id||"").slice(0,10))}</td>
    <td class="muted">${esc((l.error||"").slice(0,80))}</td></tr>`).join("")
    || `<tr><td colspan="8" class="muted">暂无日志</td></tr>`;
}
$("#logs-refresh").onclick = loadLogs;

/* ---------------- settings ---------------- */
async function loadSettings() {
  const d = await api("/admin/api/settings");
  $("#set-proxy").value = d.global_proxy || "";
  $("#set-modelmap").value = JSON.stringify(d.model_map || {}, null, 2);
  $("#set-maxwait").value = d.max_turn_wait || 600;
  $("#cfg-base").textContent = location.origin;
}
$("#set-save").onclick = async () => {
  let mm = {};
  try { mm = JSON.parse($("#set-modelmap").value || "{}"); }
  catch { $("#set-msg").textContent = "模型映射 JSON 无效"; return; }
  try {
    await put("/admin/api/settings", {
      global_proxy: $("#set-proxy").value.trim(),
      model_map: mm,
      max_turn_wait: +$("#set-maxwait").value || 600,
    });
    $("#set-msg").textContent = "已保存"; toast("设置已保存", "ok");
  } catch (e) { $("#set-msg").textContent = e.message; }
};

/* ---------------- playground ---------------- */
let pgModels = [];
async function loadModels() {
  const sel = $("#pg-model");
  try {
    const d = await api("/v1/models", {});
    pgModels = d.data || [];
    sel.innerHTML = pgModels.map(m => `<option value="${esc(m.id)}">${esc(m.id)}${m.display_name ? " · " + esc(m.display_name) : ""}</option>`).join("");
  } catch {}
}
$("#pg-send").onclick = async () => {
  const out = $("#pg-out"); out.textContent = "";
  const model = $("#pg-model").value;
  const session = $("#pg-session").value.trim();
  const sys = $("#pg-system").value.trim();
  const msg = $("#pg-msg").value.trim();
  const stream = $("#pg-stream").checked;
  if (!msg) return;
  const messages = [];
  if (sys) messages.push({ role: "system", content: sys });
  messages.push({ role: "user", content: msg });
  const body = { model, messages, stream };
  if (session) body.session_id = session;
  $("#pg-send").disabled = true;
  const t0 = performance.now();
  $("#pg-meta").textContent = "running…";
  try {
    if (!stream) {
      const r = await fetch("/v1/chat/completions", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": "Bearer " + TOKEN },
        body: JSON.stringify(body) });
      const d = await r.json();
      if (d.error) { out.textContent = "ERROR: " + d.error.message; }
      else {
        out.textContent = d.choices?.[0]?.message?.content || "(empty)";
        $("#pg-meta").textContent = `${((performance.now()-t0)/1000).toFixed(1)}s · ${d.model} · ${d.vorflux?.account || ""} · session ${d.vorflux?.session_id || ""}`;
      }
    } else {
      const r = await fetch("/v1/chat/completions", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": "Bearer " + TOKEN },
        body: JSON.stringify(body) });
      const rd = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { value, done } = await rd.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split("\n"); buf = lines.pop();
        for (const ln of lines) {
          if (!ln.startsWith("data:")) continue;
          const pay = ln.slice(5).trim();
          if (pay === "[DONE]") continue;
          try {
            const j = JSON.parse(pay);
            const delta = j.choices?.[0]?.delta?.content;
            if (delta) out.textContent += delta;
            if (j.usage) $("#pg-meta").textContent = `stream · ${((performance.now()-t0)/1000).toFixed(1)}s · tokens ${j.usage.total_tokens}`;
          } catch {}
        }
      }
    }
  } catch (e) { out.textContent = "ERROR: " + e.message; }
  $("#pg-send").disabled = false;
};

/* ---------------- boot ---------------- */
(async () => {
  gsap.fromTo(".login-card", { y: 30, opacity: 0 }, { y: 0, opacity: 1, duration: .7, ease: "power3.out" });
  gsap.to(".o1", { x: 60, y: 40, duration: 14, yoyo: true, repeat: -1, ease: "sine.inOut" });
  gsap.to(".o2", { x: -50, y: -60, duration: 17, yoyo: true, repeat: -1, ease: "sine.inOut" });
  gsap.to(".o3", { x: 40, y: -30, duration: 12, yoyo: true, repeat: -1, ease: "sine.inOut" });
  if (TOKEN) {
    try { await api("/admin/api/overview"); enterApp(); }
    catch { $("#login-view").classList.remove("hidden"); }
  } else $("#login-view").classList.remove("hidden");
})();

function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

})();
