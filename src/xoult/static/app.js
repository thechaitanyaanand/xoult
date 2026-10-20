"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const view = $("#view");

// Display preferences belong to the device, not the account, so they live in the browser.
const prefs = {
  get(key) { try { return localStorage.getItem(`xoult-${key}`); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(`xoult-${key}`, value); } catch {} },
};
const applyLayout = () => document.documentElement.classList.toggle("wide", prefs.get("layout") === "wide");
applyLayout();

let config = { categories: [], google_client_id: null };
let me = null;  // null until there is a session
let lastAction = null;  // { node, p, kind } for undo

// Skips null children so optional parts can be written inline as `cond ? el(...) : null`.
function append(node, ...children) {
  node.append(...children.filter((c) => c !== null && c !== undefined));
  return node;
}

function el(tag, props = {}, ...children) {
  return append(Object.assign(document.createElement(tag), props), ...children);
}

function say(message) { $("#announce").textContent = message; }

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "content-type": "application/json" }, ...options });
  if (res.status === 401) {
    me = null;
    route();
  }
  if (!res.ok) {
    const err = new Error(`${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

function track(paperId, kind) {
  // keepalive lets a click survive the tab navigating to arXiv.
  return fetch("/events", {
    method: "POST", keepalive: true, headers: { "content-type": "application/json" },
    body: JSON.stringify({ paper_id: paperId, kind }),
  });
}

function untrack(paperId, kind) {
  return fetch(`/events?${new URLSearchParams({ paper_id: paperId, kind })}`, { method: "DELETE" });
}

function setSavedCount(n) {
  if (me) me.saved = Math.max(0, n);
  $("#saved-count").textContent = me && me.saved ? `(${me.saved})` : "";
}

function authors(list) {
  if (list.length <= 3) return list.join(", ");
  return `${list.slice(0, 3).join(", ")} and ${list.length - 3} more`;
}

function failure(retry) {
  return el("p", { className: "status" },
    "Couldn't reach the server. On the free plan it sleeps when idle and takes up to a minute to start.",
    el("button", { className: "btn", textContent: "Try again", onclick: retry }));
}

/* ---------- Google sign-in ---------- */

let gisReady;
function loadGis() {
  gisReady ??= new Promise((resolve, reject) => {
    const s = el("script", { src: "https://accounts.google.com/gsi/client", async: true });
    s.onload = resolve;
    s.onerror = reject;
    document.head.append(s);
  });
  return gisReady;
}

// Returns a container that fills with Google's own button, or null when sign-in is off.
function googleButton() {
  if (!config.google_client_id) return null;
  const box = el("div", { className: "gsi" });
  loadGis().then(() => {
    google.accounts.id.initialize({ client_id: config.google_client_id, callback: onGoogleCredential });
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    google.accounts.id.renderButton(box, { theme: dark ? "filled_black" : "outline", size: "large", text: "signin_with" });
  }).catch(() => box.replaceChildren(el("p", { className: "status", textContent: "Google sign-in didn't load. Check your connection or ad blocker." })));
  return box;
}

async function onGoogleCredential(response) {
  try {
    await api("/auth/google", { method: "POST", body: JSON.stringify({ credential: response.credential }) });
  } catch {
    return say("Google sign-in failed. Try again.");
  }
  await loadMe();
  say(`Signed in as ${me.email}`);
  go(me.categories.length ? "#today" : "#settings");
}

/* ---------- Paper entries ---------- */

function entry(p, rank, { saved = false } = {}) {
  const url = `https://arxiv.org/abs/${p.id}`;
  const node = el("li", { className: "paper", tabIndex: -1 });

  const title = el("a", { href: url, target: "_blank", rel: "noopener", textContent: p.title });
  title.addEventListener("click", () => track(p.id, "click"));

  const summary = p.tldr ?? `${p.abstract.slice(0, 260).trimEnd()}…`;
  const actions = el("div", { className: "actions" });
  // head and body stack in the default layout and sit side by side in wide mode.
  append(node,
    el("span", { className: "mark", textContent: saved ? "✓" : String(rank), ariaHidden: "true" }),
    el("div", { className: "head" },
      el("h2", {}, title),
      el("p", { className: "byline", textContent: authors(p.authors) })),
    append(el("div", { className: "body" }),
      // An LLM wrote the TLDR from the abstract; say so, since it can be wrong.
      el("p", { className: "summary" }, p.tldr ? el("span", { className: "ai", textContent: "AI summary" }) : null, summary),
      p.because ? el("p", { className: "because" }, "Because you saved ", el("cite", { textContent: p.because })) : null,
      el("details", {}, el("summary", { textContent: "Abstract" }), el("p", { textContent: p.abstract }))),
    actions,
  );

  const links = () => [
    el("a", { className: "link", href: `https://arxiv.org/pdf/${p.id}`, target: "_blank", rel: "noopener", textContent: "PDF" }),
    el("span", { className: "cats", textContent: p.categories.slice(0, 3).join("  ") }),
  ];
  node._idle = () => {
    node.dataset.state = "";
    $(".mark", node).textContent = String(rank);
    actions.replaceChildren(
      el("button", { className: "btn primary", textContent: "Save", onclick: () => act(node, p, "save") }),
      el("button", { className: "btn", textContent: "Not for me", onclick: () => act(node, p, "dismiss") }),
      ...links(),
    );
  };
  node._done = (kind) => {
    node.dataset.state = kind === "save" ? "saved" : "dismissed";
    if (kind === "save") $(".mark", node).textContent = "✓";
    actions.replaceChildren(
      el("span", { className: "note", textContent: kind === "save" ? "Saved" : "Hidden from your feed" }),
      el("button", { className: "link", textContent: "Undo", onclick: () => undo(node, p, kind) }),
      ...links(),
    );
  };

  if (!saved) {
    node._idle();
    return node;
  }
  node.dataset.state = "saved";
  actions.replaceChildren(
    el("a", { className: "link", href: `https://arxiv.org/bibtex/${p.id}`, target: "_blank", rel: "noopener", textContent: "BibTeX" }),
    el("button", { className: "link", textContent: "Remove", onclick: async () => {
      await untrack(p.id, "save");
      node.remove();
      setSavedCount(me.saved - 1);
      $(".dateline").textContent = savedLine(me.saved);
      say("Removed from saved");
    } }),
    ...links(),
  );
  return node;
}

async function act(node, p, kind) {
  node._done(kind);
  lastAction = { node, p, kind };
  if (kind === "save") setSavedCount(me.saved + 1);
  say(kind === "save" ? "Saved" : "Hidden");
  const res = await track(p.id, kind);
  if (!res.ok) {
    node._idle();
    if (kind === "save") setSavedCount(me.saved - 1);
    say(res.status === 429 ? "Slow down a little, then try again." : "That didn't save. Try again.");
  }
}

async function undo(node, p, kind) {
  node._idle();
  if (kind === "save") setSavedCount(me.saved - 1);
  lastAction = null;
  say("Undone");
  await untrack(p.id, kind);
  node.focus();
}

/* ---------- Today ---------- */

function dateline(feed) {
  if (!feed.latest_date) return "No papers yet. The first batch arrives with the next weekday ingest.";
  const day = new Date(feed.latest_date).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
  const ranked = feed.items.some((i) => i.because)
    ? "Ranked by what you've saved."
    : "Ranked by your categories until you save a few.";
  return `${feed.latest_count.toLocaleString()} new papers announced ${day}. ${ranked}`;
}

function keyHints() {
  const k = (key) => el("kbd", { textContent: key });
  return el("p", { className: "hint" },
    k("j"), " ", k("k"), " move   ", k("s"), " save   ", k("x"), " not for me   ",
    k("o"), " open   ", k("a"), " abstract   ", k("u"), " undo");
}

// The ranked list from the last visit to Today, so switching tabs doesn't re-rank (and
// re-log every impression). It is rebuilt on "Re-rank", after an hour in case a new
// day's papers arrived, and when the account or its categories change.
let todayCache = null;  // { key, at, nodes }
const TODAY_TTL = 60 * 60 * 1000;

async function renderToday({ fresh = false } = {}) {
  const key = `${me.id} ${me.categories.join()}`;
  if (!fresh && todayCache && todayCache.key === key && Date.now() - todayCache.at < TODAY_TTL) {
    return view.replaceChildren(...todayCache.nodes);
  }
  view.replaceChildren(el("p", { className: "status", textContent: "Ranking today's papers…" }));
  let feed;
  try { feed = await api("/feed?limit=20"); } catch (e) {
    if (e.status !== 401) view.replaceChildren(failure(renderToday));
    return;
  }
  lastAction = null;
  view.replaceChildren(
    el("p", { className: "dateline", textContent: dateline(feed) }),
    feed.items.length
      ? el("ol", { className: "papers" }, ...feed.items.map((p, i) => entry(p, i + 1)))
      : el("p", { className: "status", textContent: "You've been through everything from the last week. New papers arrive each weekday." }),
    feed.items.length ? el("button", { className: "btn more", textContent: "Re-rank with what you've saved", onclick: () => { renderToday({ fresh: true }); scrollTo(0, 0); } }) : null,
    keyHints(),
  );
  todayCache = { key, at: Date.now(), nodes: [...view.childNodes] };
}

/* ---------- Saved ---------- */

const savedLine = (n) => (n
  ? `${n} saved, most recent first. Each one shapes your ranking.`
  : "Nothing saved yet. Save papers from Today and they collect here.");

async function renderSaved() {
  view.replaceChildren(el("p", { className: "status", textContent: "Loading saved papers…" }));
  let papers;
  try { papers = await api("/saved"); } catch (e) {
    if (e.status !== 401) view.replaceChildren(failure(renderSaved));
    return;
  }
  setSavedCount(papers.length);
  const nudge = !me.signed_in && config.google_client_id && papers.length >= 3
    ? el("p", { className: "nudge" }, "These live only in this browser. ",
        el("a", { href: "#settings", textContent: "Sign in" }), " to keep them on every device.")
    : null;
  view.replaceChildren(
    el("p", { className: "dateline", textContent: savedLine(papers.length) }),
    nudge,
    el("ol", { className: "papers" }, ...papers.map((p, i) => entry(p, i + 1, { saved: true }))),
  );
}

/* ---------- Stats ---------- */

async function renderStats() {
  view.replaceChildren(el("p", { className: "status", textContent: "Loading…" }));
  let rows;
  try { rows = await api("/stats"); } catch { return view.replaceChildren(failure(renderStats)); }
  const pct = (v) => (v === null ? "–" : `${(v * 100).toFixed(1)}%`);
  view.replaceChildren(
    el("p", { className: "dateline", textContent: "How each version of the ranking performs for all readers over the last 14 days." }),
    rows.length
      ? el("table", {},
          el("thead", {}, el("tr", {}, ...["Ranking", "Readers", "Shown", "Saved", "Not for me", "Opened"].map((h) => el("th", { textContent: h })))),
          el("tbody", {}, ...rows.map((r) => el("tr", {},
            el("td", { textContent: r.ranker }),
            el("td", { textContent: r.users.toLocaleString() }),
            el("td", { textContent: r.impressions.toLocaleString() }),
            el("td", { textContent: pct(r.save_rate) }),
            el("td", { textContent: pct(r.dismiss_rate) }),
            el("td", { textContent: pct(r.ctr) }),
          ))))
      : el("p", { className: "status", textContent: "No activity in the last 14 days." }),
    el("p", { className: "explainer" },
      "Save rate is the share of shown papers that readers saved. It is the number a new ranking has to beat before it replaces the current one."),
  );
}

/* ---------- Categories (onboarding and settings share this) ---------- */

function categoryForm(selected, label, onSubmit) {
  const form = el("form", {},
    el("fieldset", {},
      el("legend", { textContent: "Categories you follow" }),
      ...config.categories.map((c) => el("label", { className: "choice" },
        el("input", { type: "checkbox", name: "c", value: c.id, checked: selected.includes(c.id) }),
        c.id, el("span", { textContent: c.daily ? `${c.name}, about ${c.daily} new a day` : c.name })))),
    el("button", { className: "btn primary", type: "submit", textContent: label }),
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const picked = [...form.querySelectorAll("input:checked")].map((i) => i.value);
    if (!picked.length) return say("Pick at least one category.");
    try { await onSubmit(picked); } catch (err) {
      if (err.status !== 401) say(err.status === 429 ? "Too many attempts. Wait a minute." : "Couldn't save that. Try again.");
    }
  });
  return form;
}

function renderWelcome() {
  // Signed in with Google but never picked categories: finish setup instead of starting over.
  const finishing = me && !me.categories.length;
  const form = categoryForm(config.categories.map((c) => c.id), "Start reading", async (picked) => {
    if (finishing) await api("/me", { method: "PATCH", body: JSON.stringify({ categories: picked }) });
    else await api("/users", { method: "POST", body: JSON.stringify({ categories: picked }) });
    await loadMe();
    go("#today");
  });
  const signIn = !finishing ? googleButton() : null;
  view.replaceChildren(el("section", { className: "welcome" },
    el("h1", { textContent: finishing ? "Pick what you follow" : "A short list from today's arXiv" }),
    el("p", { textContent: finishing
      ? "Choose the categories to rank. You can change them any time in Settings."
      : "Over a thousand papers are announced each weekday. Save the ones worth reading and mark the rest as not for you; the list reorders around what you save. No account needed to start." }),
    form,
    signIn ? el("div", { className: "alt" }, el("p", { className: "status", textContent: "Used xoult before? Sign in to get your saved papers back." }), signIn) : null,
  ));
}

/* ---------- Settings ---------- */

function renderSettings() {
  const account = el("section", {}, el("h2", { textContent: "Account" }));
  if (me.signed_in) {
    account.append(
      el("p", { textContent: `Signed in with Google as ${me.email}. Your saves follow you to any device where you sign in.` }),
      el("button", { className: "btn", textContent: "Sign out", onclick: async () => {
        await api("/auth/logout", { method: "POST" });
        me = null;
        go("#today");
      } }),
    );
  } else {
    const button = googleButton();
    account.append(
      el("p", { textContent: "You're using xoult without an account. Your saves live only in this browser, and clearing its data loses them." }),
      button ?? el("p", { className: "status", textContent: "Sign-in isn't set up on this server." }),
    );
  }

  const categories = el("section", {},
    el("h2", { textContent: "Categories" }),
    categoryForm(me.categories, "Save categories", async (picked) => {
      await api("/me", { method: "PATCH", body: JSON.stringify({ categories: picked }) });
      me.categories = picked;
      say("Categories saved");
    }),
  );

  const current = prefs.get("layout") === "wide" ? "wide" : "column";
  const option = (value, label, hint) => el("label", { className: "choice" },
    el("input", { type: "radio", name: "layout", value, checked: current === value,
      onchange: () => { prefs.set("layout", value); applyLayout(); say(`${label} layout`); } }),
    label, el("span", { textContent: hint }));
  const layout = el("section", {},
    el("h2", { textContent: "Layout" }),
    el("fieldset", {},
      el("legend", { textContent: "Layout" }),
      option("column", "Single column", "One paper at a time, easiest to read"),
      option("wide", "Wide", "Title and actions beside the summary on large screens, about twice as many papers per screen")),
  );

  const data = el("section", {},
    el("h2", { textContent: "Your data" }),
    el("p", {}, "xoult stores the categories you pick and which papers you were shown, opened, saved or hid. That is used to rank your feed and, as totals across readers, to improve the ranking. If you sign in, it also stores your Google account id and email. There are no ads or tracking scripts. ",
      el("a", { href: "/privacy", textContent: "Privacy details" })),
    el("div", { className: "row" },
      el("a", { className: "btn", href: "/me/export", download: "xoult-data.json", textContent: "Download my data" }),
      el("button", { className: "btn danger", textContent: "Delete everything", onclick: async () => {
        if (!confirm("Delete your account, saved papers and history? This can't be undone.")) return;
        await api("/me", { method: "DELETE" });
        me = null;
        say("Everything deleted");
        go("#today");
      } }),
    ),
  );

  view.replaceChildren(el("div", { className: "settings" }, account, categories, layout, data));
}

/* ---------- Routing and keyboard ---------- */

// Navigate and render exactly once: changing the hash already fires hashchange → route(),
// so calling route() as well would render twice and log every impression twice.
function go(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

async function loadMe() {
  try { me = await (await fetch("/me")).json(); } catch { me = null; }
  if (!me || !me.id) me = null;
  setSavedCount(me ? me.saved : 0);
}

function route() {
  const page = location.hash.slice(1) || "today";
  const ready = me && me.categories.length;
  for (const a of document.querySelectorAll("nav a")) {
    if (ready && a.hash === `#${page}`) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  if (!ready) return renderWelcome();
  if (page === "saved") return renderSaved();
  if (page === "stats") return renderStats();
  if (page === "settings") return renderSettings();
  return renderToday();
}

document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey || /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
  const items = [...document.querySelectorAll(".paper")];
  if (!items.length) return;
  const current = items.indexOf(document.activeElement.closest?.(".paper"));
  const focus = (i) => { const n = items[Math.max(0, Math.min(items.length - 1, i))]; n.focus(); n.scrollIntoView({ block: "nearest" }); };
  const node = items[current];
  const click = (sel) => { const b = node && $(sel, node); if (b) b.click(); };
  switch (e.key) {
    case "j": focus(current + 1); break;
    case "k": focus(current < 0 ? 0 : current - 1); break;
    case "s": if (node && !node.dataset.state) { click(".btn.primary"); focus(current + 1); } break;
    case "x": if (node && !node.dataset.state) { click(".btn:not(.primary)"); focus(current + 1); } break;
    case "o": click("h2 a"); break;
    case "a": if (node) { const d = $("details", node); d.open = !d.open; } break;
    case "u": if (lastAction) undo(lastAction.node, lastAction.p, lastAction.kind); break;
    default: return;
  }
  e.preventDefault();
});

window.addEventListener("hashchange", route);

(async () => {
  try { config = await (await fetch("/config")).json(); } catch { return view.replaceChildren(failure(() => location.reload())); }
  if (config.has_session) await loadMe();
  route();
})();
