const PAGE_SIZE = 30;
const ALL = "all";
const MAX_AUTHORS = 6;
const VENUE_CHIP_LIMIT = 12;
const TRACK_LABELS = { workshop: "Workshop", findings: "Findings" };
const SORTS = ["published", "importance", "citations", "influential", "hf"];
// 중요도 등급 (scoring.py 점수 0~100)
const TIERS = [
  { min: 60, key: "top", label: "매우 높음" },
  { min: 35, key: "high", label: "높음" },
  { min: 15, key: "mid", label: "보통" },
  { min: 1, key: "low", label: "낮음" },
  { min: 0, key: "none", label: "" },
];
const PART_LABELS = { citations: "인용", influential: "주요 인용", hf_upvotes: "HF 추천", venue: "학회 채택" };
// 제출처 탭: "" 전체 논문, major 주요 학회(config.MAJOR_VENUES), any 제출처가 확인된 논문
const SCOPES = ["", "major", "any"];
const SCOPE_LABELS = { major: "주요 학회", any: "학회 표기" };

const state = {
  topic: "llm",
  q: "",
  scope: "",
  venue: "",
  majorVenues: [],
  accepted: false,
  sort: "published",
  offset: 0,
  total: 0,
  topics: [],
  totalCount: 0,
  metricsUpdatedAt: null,
  venues: [],
  venuesExpanded: false,
  fetching: false,
  updatingMetrics: false,
  requestId: 0,
  venueFetchOpen: false,
  venueFetchStatus: null, // { range, count } — 선택한 토픽·학회·연도 기준
  venueFetchRequestId: 0,
};

const $ = (id) => document.getElementById(id);
const rtf = new Intl.RelativeTimeFormat("ko", { numeric: "auto" });

async function api(path, options) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof body.detail === "string" ? body.detail : `요청 실패 (HTTP ${res.status})`;
    throw new Error(detail);
  }
  return body;
}

function relativeTime(iso) {
  const seconds = (new Date(iso) - Date.now()) / 1000;
  const units = [["day", 86400], ["hour", 3600], ["minute", 60]];
  for (const [unit, size] of units) {
    if (Math.abs(seconds) >= size) return rtf.format(Math.round(seconds / size), unit);
  }
  return "방금";
}

function currentTopic() {
  return state.topics.find((t) => t.key === state.topic);
}

/* ---------- URL state ---------- */
function readUrl() {
  const params = new URLSearchParams(location.search);
  state.topic = params.get("topic") || state.topic;
  state.q = params.get("q") || "";
  state.venue = params.get("venue") || "";
  state.scope = SCOPES.includes(params.get("scope")) ? params.get("scope") : "";
  if (state.venue && !state.scope) state.scope = "any";
  state.accepted = params.get("accepted") === "1";
  if (SORTS.includes(params.get("sort"))) state.sort = params.get("sort");
  $("search").value = state.q;
  $("accepted-only").checked = state.accepted;
  $("sort").value = state.sort;
}

function writeUrl() {
  const params = new URLSearchParams();
  params.set("topic", state.topic);
  if (state.q) params.set("q", state.q);
  if (state.scope) params.set("scope", state.scope);
  if (state.venue) params.set("venue", state.venue);
  if (state.accepted) params.set("accepted", "1");
  if (state.sort !== "published") params.set("sort", state.sort);
  history.replaceState(null, "", `?${params}`);
}

/** API 필터 파라미터. 제출처 칩·탭 개수는 제출처 탭과 학회 선택을 뺀 조건으로 센다. */
function filterParams({ withVenue = true } = {}) {
  const params = new URLSearchParams();
  if (state.topic !== ALL) params.set("topic", state.topic);
  if (state.q) params.set("q", state.q);
  if (state.accepted) params.set("accepted", "true");
  if (withVenue && state.scope) params.set("scope", state.scope);
  if (withVenue && state.venue) params.set("venue", state.venue);
  return params;
}

/* ---------- topics / tabs ---------- */
async function loadTopics() {
  const data = await api("/api/topics");
  state.topics = data.topics;
  state.totalCount = data.total;
  state.metricsUpdatedAt = data.metrics_updated_at;
  $("db-total").textContent = data.total.toLocaleString();
  if (state.topic !== ALL && !currentTopic()) state.topic = state.topics[0]?.key ?? ALL;
  renderTabs(data.total);
  renderFetchControls();
}

function renderTabs(total) {
  const tabs = [...state.topics, { key: ALL, label: "전체", count: total }];
  $("tabs").replaceChildren(
    ...tabs.map((t) => {
      const btn = document.createElement("button");
      btn.className = "tab";
      btn.type = "button";
      btn.role = "tab";
      btn.setAttribute("aria-selected", String(t.key === state.topic));
      btn.title = t.description || "";
      btn.append(t.label);
      const count = document.createElement("span");
      count.className = "count";
      count.textContent = t.count.toLocaleString();
      btn.append(count);
      btn.addEventListener("click", () => selectTopic(t.key));
      return btn;
    }),
  );
}

function renderFetchControls() {
  const topic = currentTopic(); // "전체" 탭이면 없음: arXiv 버튼은 숨기고 인용 업데이트만 둔다
  const busy = state.fetching || state.updatingMetrics;
  const count = topic ? topic.count : state.totalCount;
  const metricsAt = topic ? topic.metrics_updated_at : state.metricsUpdatedAt;

  const status = [];
  if (topic) {
    status.push(topic.last_fetched_at ? `가져오기 ${relativeTime(topic.last_fetched_at)}` : "아직 가져온 적 없음");
  }
  if (count) status.push(metricsAt ? `인용 ${relativeTime(metricsAt)}` : "인용 미수집");
  $("fetch-status").textContent = status.join(" · ");

  $("btn-new").hidden = $("btn-older").hidden = $("btn-venue-fetch").hidden = !topic;
  $("btn-new").disabled = busy;
  $("btn-older").disabled = busy || !count;
  $("btn-metrics").disabled = busy || !count;
  renderVenueFetch();
}

function selectTopic(key) {
  if (key === state.topic) return;
  state.topic = key;
  state.venueFetchStatus = null;
  loadTopics().then(loadVenueFetchStatus).catch(showError);
  loadPapers(true);
}

/* ---------- fetching by venue ---------- */
function venueFetchSelection() {
  const year = $("vf-year").value;
  return { venue: $("vf-venue").value, year: year ? Number(year) : null };
}

function renderVenueFetch() {
  const topic = currentTopic();
  const open = state.venueFetchOpen && Boolean(topic);
  $("venue-fetch").hidden = !open;
  $("btn-venue-fetch").setAttribute("aria-expanded", String(open));
  if (!open) return;

  const { venue, year } = venueFetchSelection();
  const label = year ? `${venue} ${year}` : venue;
  const status = state.venueFetchStatus;
  const range = status?.range;
  const busy = state.fetching || state.updatingMetrics;
  $("vf-new").disabled = busy || !venue;
  $("vf-older").disabled = busy || !range?.oldest || Boolean(range?.exhausted);
  $("vf-older").title = range?.exhausted ? "더 이전 검색 결과가 없습니다" : "";

  const parts = [];
  if (status) {
    parts.push(`저장된 ${topic.label} · ${label} 논문 ${status.count.toLocaleString()}편`);
    if (range?.oldest) {
      parts.push(`${range.oldest.slice(0, 10)} ~ ${range.newest.slice(0, 10)} 구간 검색함 (${relativeTime(range.updated_at)})`);
    } else if (range) {
      parts.push(`검색 결과 없음 (${relativeTime(range.updated_at)})`);
    } else {
      parts.push("이 조건으로는 아직 가져온 적 없음");
    }
    if (range?.exhausted) parts.push("더 이전 결과 없음");
  }
  $("vf-status").textContent = parts.join(" · ");
  $("vf-hint").textContent = venue
    ? `arXiv에서 comment·journal_ref에 “${label}”이(가) 적힌 ${topic.label} 논문을 최신순으로 검색하고, ` +
      `제출처가 ${label}(으)로 확인된 논문만 저장합니다. 이미 검색한 구간은 다시 요청하지 않습니다.`
    : "";
}

async function loadVenueCatalog() {
  const { groups } = await api("/api/venue-catalog");
  const select = $("vf-venue");
  const current = select.value || state.venue;
  select.replaceChildren(
    ...groups.map((group) => {
      const optgroup = document.createElement("optgroup");
      optgroup.label = group.label;
      optgroup.append(...group.venues.map((name) => new Option(name, name)));
      return optgroup;
    }),
  );
  const names = groups.flatMap((group) => group.venues);
  select.value = names.includes(current) ? current : names[0];
}

async function loadVenueFetchStatus() {
  const topic = currentTopic();
  const { venue, year } = venueFetchSelection();
  if (!state.venueFetchOpen || !topic || !venue) return;
  const requestId = ++state.venueFetchRequestId;
  const params = new URLSearchParams({ venue });
  if (year) params.set("year", year);
  try {
    const status = await api(`/api/venue-fetch/${topic.key}?${params}`);
    if (requestId !== state.venueFetchRequestId) return;
    state.venueFetchStatus = status;
    renderVenueFetch();
  } catch (err) {
    showError(err);
  }
}

async function toggleVenueFetch() {
  state.venueFetchOpen = !state.venueFetchOpen;
  renderVenueFetch();
  if (!state.venueFetchOpen) return;
  try {
    await loadVenueCatalog(); // 저장된 논문에서 새로 발견된 학회가 있을 수 있어 열 때마다 갱신
  } catch (err) {
    showError(err);
    return;
  }
  renderVenueFetch();
  await loadVenueFetchStatus();
}

function onVenueFetchSelectionChange() {
  state.venueFetchStatus = null;
  renderVenueFetch();
  loadVenueFetchStatus();
}

function initYearOptions() {
  const thisYear = new Date().getFullYear();
  const years = Array.from({ length: 7 }, (_, i) => thisYear + 1 - i);
  $("vf-year").replaceChildren(new Option("전체 연도", ""), ...years.map((y) => new Option(y, y)));
  $("vf-year").value = thisYear;
}

/* ---------- papers ---------- */
async function loadPapers(reset) {
  if (reset) state.offset = 0;
  const requestId = ++state.requestId;
  writeUrl();

  const params = filterParams();
  params.set("sort", state.sort);
  params.set("limit", PAGE_SIZE);
  params.set("offset", state.offset);

  $("btn-more").disabled = true;
  try {
    const [data, venueData] = await Promise.all([
      api(`/api/papers?${params}`),
      reset ? api(`/api/venues?${filterParams({ withVenue: false })}`) : null,
    ]);
    if (requestId !== state.requestId) return; // 더 최근 요청이 있으면 버린다

    if (venueData) {
      state.venues = venueData.venues;
      state.majorVenues = venueData.major;
      renderVenues();
    }
    if (reset) $("papers").replaceChildren();
    const nodes = data.items.map(renderPaper);
    $("papers").append(...nodes);
    nodes.forEach(hideToggleIfShort);

    state.total = data.total;
    state.offset += data.items.length;
    renderSummary();
  } catch (err) {
    showError(err);
  } finally {
    $("btn-more").disabled = false;
  }
}

/* ---------- venues ---------- */
function isMajor(venue) {
  return state.majorVenues.includes(venue);
}

function renderVenues() {
  const sum = (list) => list.reduce((total, v) => total + v.count, 0);
  const counts = { major: sum(state.venues.filter((v) => v.major)), any: sum(state.venues) };
  for (const tab of document.querySelectorAll(".scope-tab")) {
    tab.setAttribute("aria-selected", String(tab.dataset.scope === state.scope));
    const count = tab.querySelector(".count");
    if (count) count.textContent = counts[tab.dataset.scope].toLocaleString();
  }

  // "전체 논문" 탭에서는 학회 칩을 숨긴다.
  $("venue-chips").hidden = !state.scope;
  if (!state.scope) return;

  let venues = state.scope === "major" ? state.venues.filter((v) => v.major) : state.venues;
  // 선택한 제출처가 현재 조건에서 0편이어도 칩은 남겨 둔다 (해제할 수 있도록).
  if (state.venue && !venues.some((v) => v.name === state.venue)) {
    venues = [...venues, { name: state.venue, count: 0, major: isMajor(state.venue) }];
  }
  const collapsible = venues.length > VENUE_CHIP_LIMIT;
  let shown = collapsible && !state.venuesExpanded ? venues.slice(0, VENUE_CHIP_LIMIT) : venues;
  if (state.venue && !shown.some((v) => v.name === state.venue)) {
    shown = [...shown, venues.find((v) => v.name === state.venue)];
  }

  const chips = [
    venueChip("전체", ""),
    ...shown.map((v) => venueChip(v.name, v.name, v.count, state.scope === "any" && v.major)),
  ];
  if (collapsible) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "venue-chip venue-more";
    more.textContent = state.venuesExpanded ? "접기" : `+${venues.length - shown.length}곳 더`;
    more.addEventListener("click", () => {
      state.venuesExpanded = !state.venuesExpanded;
      renderVenues();
    });
    chips.push(more);
  }
  if (!venues.length) {
    const none = document.createElement("span");
    none.className = "venue-none";
    none.textContent = state.scope === "major"
      ? "주요 학회 논문이 없습니다 — ‘학회별 가져오기’로 가져올 수 있습니다"
      : "제출처가 표기된 논문이 없습니다";
    chips.push(none);
  }
  $("venue-chips").replaceChildren(...chips);
}

function venueChip(label, value, count, major = false) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = major ? "venue-chip major" : "venue-chip";
  if (major) btn.title = "주요 학회";
  btn.setAttribute("aria-pressed", String(value === state.venue));
  btn.append(label);
  if (count !== undefined) {
    const span = document.createElement("span");
    span.className = "count";
    span.textContent = count.toLocaleString();
    btn.append(span);
  }
  btn.addEventListener("click", () => selectVenue(value));
  return btn;
}

/** 학회를 선택하고, 지금 탭에서 그 학회가 안 보이면 보이는 탭으로 바꾼다 (카드 배지·학회별 가져오기). */
function applyVenue(venue) {
  state.venue = venue;
  if (venue && (!state.scope || (state.scope === "major" && !isMajor(venue)))) {
    state.scope = isMajor(venue) ? "major" : "any";
  }
}

function selectVenue(venue) {
  if (venue === state.venue) return;
  applyVenue(venue);
  renderVenues();
  loadPapers(true);
}

function selectScope(scope) {
  if (scope === state.scope) return;
  state.scope = scope;
  // 새 탭에 속하지 않는 학회 선택은 푼다.
  if (!scope || (scope === "major" && !isMajor(state.venue))) state.venue = "";
  state.venuesExpanded = false;
  renderVenues();
  loadPapers(true);
}

function venueLabel(p) {
  const parts = [p.venue_year ? `${p.venue} ${p.venue_year}` : p.venue];
  if (p.venue_track) parts.push(TRACK_LABELS[p.venue_track] ?? p.venue_track);
  if (p.venue_status === "review") parts.push("심사 중");
  return parts.join(" · ");
}

function renderSummary() {
  const shown = state.offset;
  const filters = [
    state.q && `"${state.q}" 검색`,
    state.venue || SCOPE_LABELS[state.scope],
    state.accepted && "채택",
  ].filter(Boolean);
  $("summary").textContent = state.total
    ? `${filters.length ? `${filters.join(" · ")} — ` : ""}${state.total.toLocaleString()}편 중 ${shown.toLocaleString()}편 표시`
    : "";
  $("btn-more").hidden = shown >= state.total;

  const empty = $("empty");
  empty.hidden = state.total > 0;
  if (state.total > 0) return;

  const title = document.createElement("strong");
  const desc = document.createElement("span");
  if (state.scope === "major" && !state.venue && !state.q && !state.accepted) {
    title.textContent = "저장된 주요 학회 논문이 없습니다.";
    desc.textContent = "‘학회별 가져오기’에서 주요 학회를 골라 arXiv에서 가져올 수 있습니다.";
  } else if (state.scope || state.accepted) {
    title.textContent = "조건에 맞는 논문이 없습니다.";
    desc.textContent = "제출처 탭을 ‘전체 논문’으로 바꾸거나 ‘채택된 논문만’을 해제해 보세요.";
  } else if (state.q) {
    title.textContent = "검색 결과가 없습니다.";
    desc.textContent = "저장된 논문 안에서만 검색합니다. 다른 검색어를 입력해 보세요.";
  } else if (state.topic === ALL) {
    title.textContent = "아직 저장된 논문이 없습니다.";
    desc.textContent = "토픽 탭을 선택한 뒤 ‘새 논문 가져오기’를 눌러 주세요.";
  } else {
    title.textContent = `아직 저장된 ${currentTopic()?.label ?? ""} 논문이 없습니다.`;
    desc.textContent = "‘새 논문 가져오기’를 누르면 arXiv에서 가져와 DB에 저장합니다.";
  }
  empty.replaceChildren(title, desc);
}

function highlight(el, text) {
  const terms = state.q.split(/\s+/).filter(Boolean);
  if (!terms.length) {
    el.textContent = text;
    return;
  }
  const escaped = terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const re = new RegExp(`(${escaped.join("|")})`, "gi");
  el.replaceChildren(
    ...text.split(re).map((part, i) => {
      if (i % 2 === 0) return document.createTextNode(part);
      const mark = document.createElement("mark");
      mark.textContent = part;
      return mark;
    }),
  );
}

function linkify(el, text) {
  el.replaceChildren(
    ...text.split(/(https?:\/\/[^\s,;)]*[^\s,;).])/g).map((part, i) => {
      if (i % 2 === 0) return document.createTextNode(part);
      const a = document.createElement("a");
      a.href = part;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = part;
      return a;
    }),
  );
}

function renderPaper(p) {
  const node = $("paper-tpl").content.firstElementChild.cloneNode(true);
  const q = (sel) => node.querySelector(sel);

  if (p.venue) {
    const badge = q(".venue-badge");
    badge.hidden = false;
    badge.textContent = venueLabel(p);
    badge.classList.toggle("review", p.venue_status === "review");
    badge.title = `${p.venue} 논문만 보기`;
    badge.addEventListener("click", () => {
      selectVenue(p.venue);
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  }

  const note = [p.journal_ref, p.comment].filter(Boolean).join(" · ");
  if (note) {
    const comment = q(".comment");
    comment.hidden = false;
    comment.title = note;
    linkify(comment, note);
  }

  const date = q(".date");
  date.dateTime = p.published;
  date.textContent = p.published.slice(0, 10);
  q(".primary-cat").textContent = p.primary_category;
  if (!p.version.endsWith("v1")) {
    q(".revised").textContent = `${p.version.match(/v\d+$/)[0]} · ${p.updated.slice(0, 10)} 수정`;
  }
  q(".topics").append(
    ...p.topics.map((key) => {
      const badge = document.createElement("span");
      badge.className = "topic-badge";
      badge.dataset.topic = key;
      badge.textContent = state.topics.find((t) => t.key === key)?.label ?? key;
      return badge;
    }),
  );

  const link = q(".paper-title a");
  link.href = p.abs_url;
  highlight(link, p.title);

  const authors = q(".authors");
  // "외 1명"보다는 이름 하나를 더 보여주는 편이 낫다.
  const shown = p.authors.length > MAX_AUTHORS + 1 ? MAX_AUTHORS : p.authors.length;
  const extra = p.authors.length - shown;
  highlight(authors, p.authors.slice(0, shown).join(", ") + (extra > 0 ? ` 외 ${extra}명` : ""));
  authors.title = p.authors.join(", ");

  const abstract = q(".abstract");
  highlight(abstract, p.summary);
  const toggle = q(".toggle-abstract");
  toggle.addEventListener("click", () => {
    const expanded = abstract.classList.toggle("expanded");
    toggle.textContent = expanded ? "초록 접기" : "초록 펼치기";
  });

  q(".abs-link").href = p.abs_url;
  q(".pdf-link").href = p.pdf_url;
  q(".categories").append(
    ...p.categories.map((c) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = c;
      return chip;
    }),
  );
  renderStats(node, p);
  return node;
}

/* ---------- citations / importance ---------- */
function renderStats(node, p) {
  const q = (sel) => node.querySelector(sel);
  const score = p.importance ?? 0;
  const tier = TIERS.find((t) => score >= t.min);
  node.dataset.tier = tier.key;

  q(".score-value").textContent = score;
  q(".score-label").textContent = tier.label ? `중요도 · ${tier.label}` : "중요도";
  q(".score-fill").style.width = `${score}%`;
  const parts = Object.entries(p.importance_parts).map(([key, value]) => `${PART_LABELS[key] ?? key} +${value}`);
  q(".score").title = parts.length
    ? `중요도 ${score}점 = ${parts.join(", ")}`
    : "아직 반영된 지표가 없습니다 (인용·HF 추천·학회 채택)";

  const s2 = p.s2_paper_id ? `https://www.semanticscholar.org/paper/${p.s2_paper_id}` : null;
  const citationNote = p.citations_updated_at
    ? `Semantic Scholar · ${relativeTime(p.citations_updated_at)} 확인`
    : "아직 가져오지 않음 — ‘인용·추천 업데이트’를 누르세요";
  setStat(node, "citations", p.citation_count, s2,
    p.citations_updated_at && p.citation_count == null ? "Semantic Scholar에 아직 없는 논문" : citationNote);
  setStat(node, "influential", p.influential_citation_count, s2,
    `Highly Influential Citations: 이 논문의 방법·결과를 비중 있게 다룬 인용 (${citationNote})`);
  setStat(node, "hf", p.hf_upvotes, `https://huggingface.co/papers/${p.id}`,
    p.hf_upvotes == null ? "Hugging Face Daily Papers에 선정되지 않음" : "Hugging Face Daily Papers 추천 수");
}

function setStat(node, key, value, href, title) {
  const stat = node.querySelector(`[data-stat="${key}"]`);
  const link = stat.querySelector("a");
  link.textContent = value == null ? "–" : value.toLocaleString();
  if (value == null || !href) link.removeAttribute("href");
  else link.href = href;
  stat.title = title;
  stat.classList.toggle("zero", !value);
}

async function runMetrics({ auto = false } = {}) {
  if (state.updatingMetrics) return;
  const topic = currentTopic();
  state.updatingMetrics = true;
  $("btn-metrics").classList.add("loading");
  renderFetchControls();
  $("fetch-status").textContent = "인용·추천 수 가져오는 중…";

  try {
    const r = await api(`/api/metrics${topic ? `?topic=${topic.key}` : ""}`, { method: "POST" });
    if (r.errors.length) {
      toast(r.errors.join(" / "), true);
    } else if (!auto) {
      toast(r.checked
        ? `${r.checked.toLocaleString()}편의 인용 수를 확인했습니다. HF 추천 ${r.hf_matched}편 반영.`
        : "모든 논문의 인용·추천 수가 최신입니다 (24시간 이내에 확인함).");
    }
    await loadPapers(true);
  } catch (err) {
    showError(err);
  } finally {
    state.updatingMetrics = false;
    $("btn-metrics").classList.remove("loading");
    await loadTopics().catch(showError);
  }
}

function hideToggleIfShort(node) {
  const abstract = node.querySelector(".abstract");
  if (abstract.scrollHeight <= abstract.clientHeight + 1) {
    node.querySelector(".toggle-abstract").hidden = true;
  }
}

/* ---------- fetching from arXiv ---------- */
async function runFetch(mode, { venue = null, year = null } = {}) {
  const topic = currentTopic();
  if (!topic || state.fetching || state.updatingMetrics) return;

  state.fetching = true;
  const btn = venue
    ? (mode === "new" ? $("vf-new") : $("vf-older"))
    : (mode === "new" ? $("btn-new") : $("btn-older"));
  btn.classList.add("loading");
  renderFetchControls();
  $("fetch-status").textContent = "arXiv에서 가져오는 중… (요청 간 3초 대기)";

  let inserted = 0;
  try {
    const params = new URLSearchParams({ mode });
    if (venue) params.set("venue", venue);
    if (year) params.set("year", year);
    const r = await api(`/api/fetch/${topic.key}?${params}`, { method: "POST" });
    inserted = r.inserted;
    let msg;
    if (venue) {
      const label = `${topic.label} · ${year ? `${venue} ${year}` : venue}`;
      msg = `${label}: arXiv 검색 ${r.received.toLocaleString()}편 중 학회 확인 ${r.matched.toLocaleString()}편, ` +
        `새로 저장 ${r.inserted.toLocaleString()}편.`;
      if (r.exhausted) msg += " 더 이전 검색 결과는 없습니다.";
    } else {
      msg = r.inserted
        ? `${topic.label}: ${r.inserted.toLocaleString()}편을 새로 저장했습니다.`
        : `${topic.label}: 새로 저장할 논문이 없습니다.`;
    }
    if (r.truncated) msg += " 가져오기 상한에 도달해 중간에 빠진 논문이 있을 수 있습니다.";
    toast(msg);
    if (venue) applyVenue(venue); // 가져온 학회의 논문을 바로 보여준다
    if (state.topic === topic.key) await loadPapers(true);
  } catch (err) {
    showError(err);
  } finally {
    state.fetching = false;
    btn.classList.remove("loading");
    await loadTopics().catch(showError);
    if (venue) await loadVenueFetchStatus();
  }
  // 새로 받은 논문의 인용·추천 수도 이어서 가져온다.
  if (inserted) await runMetrics({ auto: true });
}

/* ---------- toast ---------- */
let toastTimer;
function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), isError ? 7000 : 4000);
}

function showError(err) {
  console.error(err);
  toast(err.message || String(err), true);
}

/* ---------- init ---------- */
let searchTimer;
$("search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.q = e.target.value.trim();
    loadPapers(true);
  }, 250);
});

document.addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement !== $("search")) {
    e.preventDefault();
    $("search").focus();
  }
});

for (const tab of document.querySelectorAll(".scope-tab")) {
  tab.addEventListener("click", () => selectScope(tab.dataset.scope));
}

$("accepted-only").addEventListener("change", (e) => {
  state.accepted = e.target.checked;
  loadPapers(true);
});

$("sort").addEventListener("change", (e) => {
  state.sort = e.target.value;
  loadPapers(true);
});

$("btn-metrics").addEventListener("click", () => runMetrics());
$("btn-venue-fetch").addEventListener("click", toggleVenueFetch);
$("vf-venue").addEventListener("change", onVenueFetchSelectionChange);
$("vf-year").addEventListener("change", onVenueFetchSelectionChange);
$("vf-new").addEventListener("click", () => runFetch("new", venueFetchSelection()));
$("vf-older").addEventListener("click", () => runFetch("older", venueFetchSelection()));
$("btn-new").addEventListener("click", () => runFetch("new"));
$("btn-older").addEventListener("click", () => runFetch("older"));
$("btn-more").addEventListener("click", () => loadPapers(false));

readUrl();
initYearOptions();
loadTopics()
  .then(() => loadPapers(true))
  .catch(showError);
