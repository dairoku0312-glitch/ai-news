"use strict";

const CATEGORIES = [
  "介護・福祉",
  "外国人材・特定技能・在留資格",
  "AI・IT",
  "経済・ビジネス",
  "国内政治",
  "海外ニュース",
];

let allDays = [];
let activeCategory = "すべて";
let keyword = "";
let selectedDate = null;
let calYear = 0;
let calMonth = 0; // 0-based

function dateStrOf(y, m, d) {
  const pad = (n) => String(n).padStart(2, "0");
  return y + "-" + pad(m + 1) + "-" + pad(d);
}

function renderCalendar() {
  const title = document.getElementById("cal-title");
  const grid = document.getElementById("calendar");
  if (!title || !grid) return;
  title.textContent = calYear + "年" + (calMonth + 1) + "月";
  const has = {};
  allDays.forEach((day) => { has[day.date] = true; });
  const todayStr = todayJstString();
  const startDay = new Date(calYear, calMonth, 1).getDay();
  const daysInMonth = new Date(calYear, calMonth + 1, 0).getDate();
  grid.innerHTML = "";
  for (let i = 0; i < startDay; i++) {
    const blank = document.createElement("span");
    blank.className = "cal-cell cal-blank";
    grid.appendChild(blank);
  }
  for (let d = 1; d <= daysInMonth; d++) {
    const ds = dateStrOf(calYear, calMonth, d);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "cal-cell cal-day";
    btn.innerHTML = "<span class=\"cal-num\">" + d + "</span>" +
      (has[ds] ? "<span class=\"cal-dot\">●</span>" : "<span class=\"cal-dot cal-dot-none\">・</span>");
    if (ds === todayStr) btn.classList.add("is-today");
    if (ds === selectedDate) btn.classList.add("is-selected");
    if (!has[ds]) {
      btn.disabled = true;
      btn.classList.add("is-empty");
    } else {
      btn.addEventListener("click", () => {
        selectedDate = ds;
        renderCalendar();
        render();
      });
    }
    grid.appendChild(btn);
  }
}

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatDateTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return (
    d.getFullYear() + "年" + (d.getMonth() + 1) + "月" + d.getDate() + "日 " +
    pad(d.getHours()) + ":" + pad(d.getMinutes())
  );
}

function formatDate(isoDate) {
  // isoDate: "YYYY-MM-DD" or full ISO string
  if (!isoDate) return "";
  const d = new Date(isoDate.length <= 10 ? isoDate + "T00:00:00+09:00" : isoDate);
  if (isNaN(d.getTime())) return isoDate;
  const weekdays = ["日", "月", "火", "水", "木", "金", "土"];
  return (
    d.getFullYear() + "年" + (d.getMonth() + 1) + "月" + d.getDate() + "日（" +
    weekdays[d.getDay()] + "）"
  );
}

function todayJstString() {
  const now = new Date();
  const jst = new Date(now.getTime() + (9 * 60 + now.getTimezoneOffset()) * 60000);
  const pad = (n) => String(n).padStart(2, "0");
  return jst.getFullYear() + "-" + pad(jst.getMonth() + 1) + "-" + pad(jst.getDate());
}

function itemMatches(item) {
  if (activeCategory !== "すべて" && item.category !== activeCategory) return false;
  if (keyword) {
    const hay = [
      item.title || "",
      item.summary || "",
      (item.points || []).join(" "),
      item.impact || "",
      item.source || "",
    ].join(" ");
    if (!hay.toLowerCase().includes(keyword.toLowerCase())) return false;
  }
  return true;
}

function renderCard(item) {
  const points = (item.points || [])
    .map((p) => "<li>" + escapeHtml(p) + "</li>")
    .join("");
  const statusBadge = item.policy_status
    ? '<span class="status-badge status-' + escapeHtml(item.policy_status) + '">' +
      escapeHtml(item.policy_status) + "</span>"
    : "";
  const impact = item.impact
    ? '<p class="card-impact"><strong>業務への影響：</strong>' + escapeHtml(item.impact) + "</p>"
    : "";
  const published = item.published_at ? formatDate(item.published_at) : "";
  return (
    '<article class="card">' +
    '<h3 class="card-title"><a href="' + escapeHtml(item.url) +
    '" target="_blank" rel="noopener noreferrer">' + escapeHtml(item.title) + "</a></h3>" +
    '<div class="card-badges"><span class="category-chip">' + escapeHtml(item.category) +
    "</span>" + statusBadge + "</div>" +
    '<p class="card-summary">' + escapeHtml(item.summary) + "</p>" +
    (points ? '<ul class="card-points">' + points + "</ul>" : "") +
    impact +
    '<div class="card-meta"><span>情報源：' + escapeHtml(item.source || "-") + "</span>" +
    (published ? "<span>元記事の公開日：" + escapeHtml(published) + "</span>" : "") +
    "</div>" +
    '<div class="card-actions"><a class="source-link" href="' + escapeHtml(item.url) +
    '" target="_blank" rel="noopener noreferrer">元記事を読む →</a></div>' +
    "</article>"
  );
}

function render() {
  const container = document.getElementById("news-container");
  const emptyMsg = document.getElementById("empty-message");
  const todayStr = todayJstString();
  let html = "";
  let anyVisible = false;

  // キーワード検索中は全ての日から探す。それ以外はカレンダーで選んだ日だけ表示する。
  const targetDays = keyword
    ? allDays
    : allDays.filter((day) => day.date === selectedDate);

  targetDays.forEach((day) => {
    const items = (day.items || []).filter(itemMatches);
    if (items.length === 0) return;
    anyVisible = true;
    const label = day.date === todayStr ? "本日のニュース（" + formatDate(day.date) + "）" : formatDate(day.date);
    html += '<section class="day-section"><h2 class="day-heading">' + escapeHtml(label) + "</h2>";
    html += items.map(renderCard).join("");
    html += "</section>";
  });

  container.innerHTML = html;
  emptyMsg.hidden = anyVisible;
}

function renderFilters() {
  const wrap = document.getElementById("filters");
  const names = ["すべて"].concat(CATEGORIES);
  wrap.innerHTML = "";
  names.forEach((name) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "filter-chip" + (name === activeCategory ? " active" : "");
    btn.textContent = name;
    btn.addEventListener("click", () => {
      activeCategory = name;
      renderFilters();
      render();
    });
    wrap.appendChild(btn);
  });
}

async function init() {
  document.getElementById("today-date").textContent = formatDate(todayJstString());

  let data;
  try {
    const res = await fetch("data/news.json", { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    data = await res.json();
  } catch (e) {
    document.getElementById("last-updated").textContent = "最終更新日時：取得できませんでした";
    document.getElementById("empty-message").hidden = false;
    return;
  }

  document.getElementById("last-updated").textContent =
    "最終更新日時：" + formatDateTime(data.last_success_at);

  if (data.status === "error") {
    const banner = document.getElementById("error-banner");
    const attempt = data.last_attempt_at ? formatDateTime(data.last_attempt_at) : "-";
    banner.textContent =
      "最新の自動更新（" + attempt + "）に失敗しました" +
      (data.error ? "：" + data.error : "") +
      "。表示しているのは前回成功時の内容です。";
    banner.hidden = false;
  }

  allDays = Array.isArray(data.days) ? data.days : [];
  selectedDate = allDays.length ? allDays[0].date : todayJstString();
  const sel = new Date(selectedDate + "T00:00:00+09:00");
  calYear = sel.getFullYear();
  calMonth = sel.getMonth();
  renderFilters();
  renderCalendar();
  render();

  document.getElementById("cal-prev").addEventListener("click", () => {
    calMonth -= 1;
    if (calMonth < 0) { calMonth = 11; calYear -= 1; }
    renderCalendar();
  });
  document.getElementById("cal-next").addEventListener("click", () => {
    calMonth += 1;
    if (calMonth > 11) { calMonth = 0; calYear += 1; }
    renderCalendar();
  });

  document.getElementById("search").addEventListener("input", (ev) => {
    keyword = ev.target.value.trim();
    render();
  });
}

document.addEventListener("DOMContentLoaded", init);
