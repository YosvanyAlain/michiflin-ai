"use strict";
// One top-right center; section/result badges retain their existing semantics.
let notificationCenterDraw = null;
function notificationCenterPaint() {
  const button = $("#notifications-toggle");
  if (!button) return;
  const count = notificationCounts().home.unread;
  fill(button, icon("bell"), h("i", { class: "notification-count", hidden: !count }, count > 99 ? "99+" : String(count)));
  button.setAttribute("aria-label", t("notification_center") + (count ? ` · ${count}` : ""));
  button.title = t("notification_center");
  if (notificationCenterDraw) notificationCenterDraw();
}
function notificationCenter() {
  const button = $("#notifications-toggle"), body = h("div", { class: "notification-center-content" });
  let showHidden = false, category = "all", revision = 0, lastKey = "";
  let readFrame = 0;
  const canRead = () => body.isConnected && document.visibilityState !== "hidden" && $("#sheet").classList.contains("open");
  function paintedNodes() {
    const panel = $("#sheet .sheet-body"), bounds = panel.getBoundingClientRect();
    return [...body.querySelectorAll("[data-center-notice-id]")].filter(node => {
      const r = node.getBoundingClientRect();
      const top = Math.max(bounds.top, 0), bottom = Math.min(bounds.bottom, window.innerHeight);
      const visible = Math.max(0, Math.min(bottom, r.bottom) - Math.max(top, r.top));
      return bottom > top && r.right > Math.max(bounds.left, 0) && r.left < Math.min(bounds.right, window.innerWidth) && r.height > 0 && r.width > 0 && visible >= Math.min(r.height, bottom - top) / 2;
    });
  }
  function scheduleRead() {
    cancelAnimationFrame(readFrame);
    if (!canRead()) return;
    const ticket = revision, sheet = sheetRevision, visibility = visibilityRevision;
    readFrame = requestAnimationFrame(() => {
      if (!canRead() || ticket !== revision || sheet !== sheetRevision || visibility !== visibilityRevision) return;
      const candidates = new Set(paintedNodes());
      readFrame = requestAnimationFrame(() => {
        if (!canRead() || ticket !== revision || sheet !== sheetRevision || visibility !== visibilityRevision) return;
        const ids = new Set(paintedNodes().filter(node => candidates.has(node)).map(node => node.dataset.centerNoticeId));
        const records = store.get("notices", []); let changed = false;
        for (const record of records) if (!record.target && !record.hidden && record.unread && ids.has(record.id)) { record.unread = false; changed = true; }
        if (changed && store.set("notices", records)) { jobBadge(); bus.emit("notice-paint"); }
      });
    });
  }
  const visibleRows = () => store.get("notices", []).filter(n => (showHidden ? !!n.hidden : !n.hidden) && (category === "all" || (n.category || "home") === category));
  const draw = () => {
    const key = JSON.stringify([showHidden, category, store.get("notices", []).map(n => [n.id, n.text, !!n.hidden]), cache.maintenance_notice]);
    if (key === lastKey) { scheduleRead(); return; }
    lastKey = key; revision += 1;
    const scroll = $("#sheet .sheet-body")?.scrollTop || 0;
    const focused = body.contains(document.activeElement) ? document.activeElement.dataset.centerFocus : "";
    const list = h("div", { class: "notification-center-list" });
    const hiddenToggle = h("button", { class: "btn ghost small", "data-center-focus": "hidden", "aria-pressed": String(showHidden),
      onclick: () => { showHidden = !showHidden; draw(); } }, icon("eye"), t(showHidden ? "notification_visible" : "notification_hidden_tab"));
    const filter = select(category, [["all", t("notification_all")], ...TABS.map(tab => [tab.id, t("tab_" + tab.id)])], value => { category = value; draw(); });
    filter.setAttribute("aria-label", t("notification_all"));
    filter.dataset.centerFocus = "category";
    const rows = visibleRows();
    const change = (id, remove = false) => {
      const records = store.get("notices", []), selected = records.find(n => n.id === id);
      if (!selected) return;
      const next = remove ? records.filter(n => n.id !== id) : records.map(n => n.id === id ? { ...n, hidden: !n.hidden } : n);
      if (store.set("notices", next)) bus.emit("notices");
    };
    for (const item of rows) {
      let swiped = false, start = null;
      const actions = h("div", { class: "notice-inline-actions", role: "group", "aria-label": t("notification_actions") },
        h("button", { class: "icon-btn notice-visibility", "data-center-focus": "hide-" + item.id,
          "aria-label": t(item.hidden ? "notification_show" : "notification_hide"), title: t(item.hidden ? "notification_show" : "notification_hide"), onclick: () => change(item.id) }, icon(item.hidden ? "eye" : "eyeOff")),
        h("button", { class: "icon-btn notice-delete", "data-center-focus": "delete-" + item.id,
          "aria-label": t("notification_delete_one"), title: t("notification_delete_one"), onclick: () => change(item.id, true) }, icon("trash")));
      const open = () => {
        if (swiped) { swiped = false; return; }
        if (item.target) { closeSheet(); openNotice(item); }
        else row.classList.toggle("actions-open");
      };
      const row = h("div", { class: `notice-center-row ${item.hidden ? "notice-hidden" : ""}`, "data-center-notice-id": item.id },
        h("button", { class: "notice-center-main notice", "data-notice-id": item.id, "data-center-focus": "open-" + item.id, onclick: open },
          icon(item.kind === "bad" ? "alert" : item.unread ? "bell" : "check"),
          h("span", { class: "grow" }, h("span", { class: "notice-center-text" }, item.text), h("small", { class: "muted" }, fmtTime(item.ts)))), actions);
      row.style.touchAction = "pan-y";
      row.addEventListener("pointerdown", event => { if (event.isPrimary !== false) start = { x: event.clientX, y: event.clientY }; });
      row.addEventListener("pointerup", event => {
        if (!start) return;
        const dx = event.clientX - start.x, dy = event.clientY - start.y; start = null;
        if (Math.abs(dx) > 48 && Math.abs(dx) > Math.abs(dy) * 1.5) {
          swiped = true; row.classList.add("actions-open");
          row.dataset.swipe = dx < 0 ? "delete" : "visibility";
          actions.querySelector(dx < 0 ? ".notice-delete" : ".notice-visibility").focus({ preventScroll: true });
        }
      });
      row.addEventListener("pointercancel", () => { start = null; });
      list.append(row);
    }
    if (!rows.length) list.append(h("p", { class: "empty" }, t("notices_none")));
    const clearAll = h("button", { class: "icon-btn", "data-center-focus": "clear", disabled: !store.get("notices", []).length,
      "aria-label": t("notification_delete_all"), title: t("notification_delete_all"), onclick: async () => {
        // Capture all current IDs, including hidden rows. New arrivals during
        // confirmation survive; deleting notices never deletes their results.
        const ids = new Set(store.get("notices", []).map(n => n.id));
        const yes = await confirmBox(t("notification_delete_all"), t("notification_delete_all_confirm"), t("delete"));
        if (yes && store.set("notices", store.get("notices", []).filter(n => !ids.has(n.id)))) bus.emit("notices");
        notificationCenter();
      } }, icon("trash"));
    fill(body, h("div", { class: "row notification-center-tools" }, filter, hiddenToggle, clearAll),
      cache.maintenance_notice?.active ? h("p", { class: "note warn" }, lang() === "en" ? cache.maintenance_notice.en : cache.maintenance_notice.es) : null, list);
    if (focused) {
      const next = [...body.querySelectorAll("[data-center-focus]")].find(n => n.dataset.centerFocus === focused);
      (next || hiddenToggle).focus({ preventScroll: true });
    }
    if ($("#sheet .sheet-body")) $("#sheet .sheet-body").scrollTop = scroll;
    scheduleRead();
  };
  openSheet(t("notification_center"), body, () => {
    revision += 1; notificationCenterDraw = null; cancelAnimationFrame(readFrame);
    document.removeEventListener("visibilitychange", scheduleRead);
    window.removeEventListener("resize", scheduleRead);
    $("#sheet").classList.remove("notification-center");
    button.setAttribute("aria-expanded", "false");
  });
  button.setAttribute("aria-expanded", "true");
  notificationCenterDraw = draw;
  $("#sheet").classList.add("notification-center");
  $("#sheet .sheet-body").addEventListener("scroll", scheduleRead, { passive: true });
  $("#sheet .sheet-panel").addEventListener("transitionend", scheduleRead);
  document.addEventListener("visibilitychange", scheduleRead);
  window.addEventListener("resize", scheduleRead);
  draw();
  requestAnimationFrame(scheduleRead);
}
$("#notifications-toggle").onclick = notificationCenter;
["notices", "notice-paint", "badges", "hello", "maintenance"].forEach(kind => bus.always(kind, notificationCenterPaint));
notificationCenterPaint();
