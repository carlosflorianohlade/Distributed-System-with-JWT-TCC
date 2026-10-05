const H = location.hostname && location.protocol !== "file:" ? location.hostname : "localhost";
const URL_ = {
  auth:`http://${H}:8000`, order:`http://${H}:8001`, inventory:`http://${H}:8002`,
  payment:`http://${H}:8003`, shipping:`http://${H}:8004`
};
const $ = id => document.getElementById(id);
let token = null, user = null, polling = null;

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
    `<tr><td>${k}</td><td class="${v.try}">${v.try}</td><td class="${v.confirm}">${v.confirm}</td><td class="${v.cancel}">${v.cancel}</td></tr>`).join("");
  $("st").innerHTML =
    `<p>Transazione <code>${o.transaction_id}</code><br>Stato: <b>${o.status}</b> · Decisione: <b>${o.decision}</b>` +
    (o.payment_intent_id ? ` · PaymentIntent: <code>${o.payment_intent_id}</code>` : "") + `</p>` +
    `<table><tr><th>Servizio</th><th>Try</th><th>Confirm</th><th>Cancel</th></tr>${rows}</table>`;
}

async function pollOrders() {
  try {
    const all = (await api("order", "/orders")).body || {};
    const ids = Object.keys(all);
    if (ids.length) renderOrder(all[ids[ids.length - 1]]);
  } catch {}
}

$("bh").onclick = () => { health(); states(); };

$("bl").onclick = async () => {
  try {
    const r = await api("auth", "/login", { method: "POST", body: JSON.stringify({ username: $("u").value, password: $("p").value }) });
    if (!r.ok) return say("m1", `Errore ${r.status}: ${r.body?.detail || "login fallito"}`, "err");
    token = r.body.access_token; user = $("u").value.trim();
    say("m1", `Login riuscito come ${user}. Token JWT ricevuto.`, "good");
    $("bc").disabled = false;
  } catch { say("m1", "auth-service non raggiungibile (porta 8000).", "err"); }
};

$("bc").onclick = async () => {
  try {
    const r = await api("payment", `/users/${user}`, { method: "PUT", body: JSON.stringify({ payment_method: $("pm").value }) });
    if (!r.ok) return say("m2", `Errore ${r.status}: ${JSON.stringify(r.body?.detail)}`, "err");
    say("m2", `Carta ${r.body.payment_method} associata al Customer Stripe ${r.body.customer_id}.`, "good");
    $("bo").disabled = false;
  } catch { say("m2", "payment-service non raggiungibile (porta 8003).", "err"); }
};

$("bo").onclick = async () => {
  $("bo").disabled = true; $("out").hidden = true; say("m3", "Ordine in corso…");
  polling = setInterval(() => { pollOrders(); states(); }, 400);
  try {
    const r = await api("order", "/orders", {
      method: "POST",
      headers: { Authorization: "Bearer " + token },
      body: JSON.stringify({
        product_id: $("pr").value, quantity: +$("q").value, address: $("ad").value,
        fail_payment: $("fp").checked, fail_shipping: $("fs").checked,
        payment_method: $("pm").value
      })
    });
    $("out").hidden = false; $("out").textContent = `HTTP ${r.status}\n` + JSON.stringify(r.body, null, 2);
    say("m3", r.ok ? "Ordine confermato." : `Ordine non completato (HTTP ${r.status}).`, r.ok ? "good" : "err");
  } catch { say("m3", "order-service non raggiungibile (porta 8001).", "err"); }
  clearInterval(polling); await pollOrders(); await states(); $("bo").disabled = false;
};

health(); states();
