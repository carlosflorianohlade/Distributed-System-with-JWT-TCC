const H = location.hostname && location.protocol !== "file:" ? location.hostname : "localhost";
const URL_ = {
  auth:`http://${H}:8000`, order:`http://${H}:8001`, inventory:`http://${H}:8002`,
  payment:`http://${H}:8003`, shipping:`http://${H}:8004`
};
const $ = id => document.getElementById(id);
let token = null, user = null, polling = null, knownIds = new Set();
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function api(base, path, opts = {}) {
  const r = await fetch(URL_[base] + path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) }
  });
  let body; try { body = await r.json(); } catch { body = null; }
  return { ok: r.ok, status: r.status, body };
}
const say = (id, txt, cls) => { $(id).textContent = txt; $(id).className = "msg " + (cls || ""); };

async function health() {
  $("health").innerHTML = (await Promise.all(Object.keys(URL_).map(async k => {
    let up = false;
    try { up = (await api(k, "/health")).ok; } catch {}
    return `<span class="dot ${up ? "up" : "down"}"></span>${k}`;
  }))).join("");
}

async function states() {
  for (const [k, id] of [["inventory","s-inv"],["payment","s-pay"],["shipping","s-shp"]]) {
    try { $(id).textContent = JSON.stringify((await api(k, "/state")).body, null, 2); }
    catch { $(id).textContent = "non raggiungibile"; }
  }
}

function renderOrder(o) {
  if (!o) return;
  const rows = Object.entries(o.participants).map(([k, v]) =>
    `<tr><td>${esc(k)}</td><td class="${esc(v.try)}">${esc(v.try)}</td><td class="${esc(v.confirm)}">${esc(v.confirm)}</td><td class="${esc(v.cancel)}">${esc(v.cancel)}</td></tr>`).join("");
  $("st").innerHTML =
    `<p>Transazione <code>${esc(o.transaction_id)}</code><br>Stato: <b>${esc(o.status)}</b> · Decisione: <b>${esc(o.decision)}</b>` +
    (o.payment_intent_id ? ` · PaymentIntent: <code>${esc(o.payment_intent_id)}</code>` : "") + `</p>` +
    `<table><tr><th>Servizio</th><th>Try</th><th>Confirm</th><th>Cancel</th></tr>${rows}</table>`;
}

async function pollOrders() {
  try {
    const all = (await api("order", "/orders")).body || {};
    const fresh = Object.keys(all).filter(id => !knownIds.has(id));
    if (fresh.length) renderOrder(all[fresh[fresh.length - 1]]);
  } catch {}
}

$("bh").onclick = () => { health(); states(); };

$("bl").onclick = async () => {
  try {
    const r = await api("auth", "/login", { method: "POST", body: JSON.stringify({ username: $("u").value, password: $("p").value }) });
    if (!r.ok) return say("m1", `Errore ${r.status}: ${r.body?.detail || "login fallito"}`, "err");
    token = r.body.access_token; user = $("u").value.trim();
    $("bo").disabled = true; say("m2", "");
    say("m1", `Login riuscito come ${user}. Token JWT ricevuto.`, "good");
    $("bc").disabled = false;
  } catch { say("m1", "auth-service non raggiungibile (porta 8000).", "err"); }
};

$("bc").onclick = async () => {
  try {
    const r = await api("payment", `/users/${encodeURIComponent(user)}`, { method: "PUT", body: JSON.stringify({ payment_method: $("pm").value }) });
    if (!r.ok) return say("m2", `Errore ${r.status}: ${JSON.stringify(r.body?.detail)}`, "err");
    say("m2", `Carta ${r.body.payment_method} associata al Customer Stripe ${r.body.customer_id}.`, "good");
    $("bo").disabled = false;
  } catch { say("m2", "payment-service non raggiungibile (porta 8003).", "err"); }
};

// Cambiando carta bisogna riassociarla: l'ordine usa quella registrata per l'utente
$("pm").onchange = () => {
  $("bo").disabled = true;
  if (token) say("m2", "Carta cambiata: premi di nuovo «Associa la carta all'utente».");
};

$("bo").onclick = async () => {
  const qty = parseInt($("q").value, 10);
  if (!Number.isInteger(qty) || qty < 1) return say("m3", "Quantità non valida (minimo 1).", "err");
  $("bo").disabled = true; $("out").hidden = true; say("m3", "Ordine in corso…");
  try { knownIds = new Set(Object.keys((await api("order", "/orders")).body || {})); } catch { knownIds = new Set(); }
  polling = setInterval(() => { pollOrders(); states(); }, 400);
  try {
    const r = await api("order", "/orders", {
      method: "POST",
      headers: { Authorization: "Bearer " + token },
      body: JSON.stringify({
        product_id: $("pr").value, quantity: qty, address: $("ad").value,
        fail_payment: $("fp").checked, fail_shipping: $("fs").checked
      })
    });
    $("out").hidden = false; $("out").textContent = `HTTP ${r.status}\n` + JSON.stringify(r.body, null, 2);
    if (r.ok && r.body?.status === "ORDER_CONFIRMED") say("m3", "Ordine confermato.", "good");
    else if (r.ok) say("m3", "Ordine in COMMIT ma Confirm ancora in sospeso (CONFIRM_PENDING): serve il recovery.", "err");
    else if (r.status === 401) { say("m3", "Sessione scaduta: rifai il login.", "err"); token = null; $("bc").disabled = true; }
    else {
      const d = r.body?.detail;
      const why = d && typeof d === "object" ? ` – ${d.message || ""}${d.failed_participant ? ` [${d.failed_participant}]` : ""}${d.reason ? `: ${d.reason}` : ""}` : (d ? ` – ${d}` : "");
      say("m3", `Ordine non completato (HTTP ${r.status})${why}`, "err");
    }
  } catch { say("m3", "order-service non raggiungibile (porta 8001).", "err"); }
  clearInterval(polling); await pollOrders(); await states(); $("bo").disabled = !token;
};

health(); states();