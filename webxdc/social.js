"use strict";
// Text is always inserted as textContent; no remote embeds, HTML or media.
window.TextCommunity = (() => {
  const tx = (es, en) => lang() === "en" ? en : es;
  const button = (label, action, extra = "") => h("button", { class: "btn small " + extra, type: "button", onclick: action }, label);
  let revision = 0;
  function mount(main, params = {}) {
    const ticket = ++revision;
    const root = h("div", { class: "social-space", "data-theme": "auto" });
    const head = h("div", { class: "social-hero" }, h("span", { class: "eyebrow" }, tx("SOLO PALABRAS · TU COMUNIDAD", "JUST WORDS · YOUR COMMUNITY")),
      h("h2", {}, t("social_title")), h("p", {}, tx("Comparte ideas. Encuentra conversaciones. A tu ritmo.", "Share ideas. Find conversations. At your own pace.")));
    const status = h("p", { class: "muted small", role: "status", "aria-live": "polite" });
    const toolbar = h("div", { class: "row wrap social-toolbar" });
    const composer = h("div", { class: "social-composer" });
    const list = h("div", { class: "social-feed", "aria-label": tx("Publicaciones", "Posts") });
    const more = button(tx("Cargar más", "Load more"), () => load(true), "ghost"); more.hidden = true;
    root.append(head, toolbar, status, composer, list, more);
    main.append(root);
    let data = null, busy = false, cursor = 0, feed = params.feed || "all", query = "";
    const parent = Number(params.parent) || 0;
    const active = () => ticket === revision && root.isConnected && current.view === "social";
    const draftKey = "social.draft." + (parent || "root");
    const currentDraft = () => {
      const value = store.get(draftKey, { text: "", nonce: "" });
      return { text: typeof value.text === "string" ? value.text.slice(0, 3000) : "",
        nonce: typeof value.nonce === "string" && /^[a-zA-Z0-9_-]{8,80}$/.test(value.nonce) ? value.nonce : "" };
    };
    const applyPreferences = prefs => {
      root.dataset.theme = prefs.theme || "auto";
      root.dataset.density = prefs.density || "comfortable";
      root.dataset.font = prefs.font || "normal";
    };
    async function mutation(args) {
      if (busy) return false;
      busy = true; root.setAttribute("aria-busy", "true");
      status.textContent = tx("Guardando…", "Saving…");
      try { return await bridge.call("social", args); }
      catch (error) { if (active()) { status.textContent = error.message; toast(error.message, "bad"); } return false; }
      finally { busy = false; root.removeAttribute("aria-busy"); }
    }
    async function act(args) { if (await mutation(args)) await load(); }
    function editText(title, value, maximum, done) {
      const area = h("textarea", { class: "input social-editor", rows: 7, maxlength: maximum, value, "aria-label": title });
      const save = button(tx("Guardar", "Save"), async () => {
        if (save.disabled) return; save.disabled = true;
        try { if (await done(area.value)) closeSheet(); } finally { save.disabled = false; }
      }, "primary");
      openSheet(title, h("div", {}, area, h("p", { class: "muted small" }, tx("Solo texto; sin imágenes, vídeos ni adjuntos.", "Text only; no images, videos or attachments.")), save));
    }
    function profile() {
      if (!data) return;
      const alias = field({ value: data.me.alias, maxlength: 36, "aria-label": tx("Alias público (no correo)", "Public alias (not email)") });
      const bio = h("textarea", { class: "input", rows: 3, maxlength: 240, value: data.me.bio, "aria-label": tx("Biografía", "Bio") });
      const prefs = { theme: "auto", density: "comfortable", font: "normal", muted_tags: [], ...data.prefs };
      const tags = field({ value: prefs.muted_tags.join(", "), placeholder: "spoiler, política", "aria-label": tx("Etiquetas silenciadas", "Muted tags") });
      const form = h("div", { class: "social-profile-form" },
        h("label", {}, tx("Alias público (no correo)", "Public alias (not email)"), alias),
        h("label", {}, tx("Sobre ti", "About you"), bio),
        h("label", {}, tx("Paleta", "Palette"), select(prefs.theme, [["auto", tx("Como la app", "Match app")], ["violet", "Violeta"], ["ocean", "Ocean"], ["forest", "Forest"], ["rose", "Rose"], ["amber", "Amber"]], v => { prefs.theme = v; })),
        h("label", {}, tx("Espaciado", "Spacing"), select(prefs.density, [["comfortable", tx("Cómodo", "Comfortable")], ["compact", tx("Compacto", "Compact")]], v => { prefs.density = v; })),
        h("label", {}, tx("Letra", "Text size"), select(prefs.font, [["normal", tx("Normal", "Normal")], ["large", tx("Grande", "Large")]], v => { prefs.font = v; })),
        h("label", {}, tx("Silenciar etiquetas (separadas por comas)", "Mute tags (comma separated)"), tags),
        h("p", { class: "muted small" }, t("social_privacy")),
        button(tx("Guardar perfil y aspecto", "Save profile and appearance"), async () => {
          prefs.muted_tags = tags.value.split(",").map(s => s.trim().replace(/^#/, "")).filter(Boolean);
          const confirmed = await mutation({ do: "profile", alias: alias.value, bio: bio.value, prefs });
          if (confirmed?.me && active()) {
            // The mutation response is the committed profile. Do not make its
            // display depend on a second feed request reaching this device.
            data = { ...data, me: confirmed.me, prefs: confirmed.prefs || {} };
            applyPreferences(data.prefs); drawComposer();
            closeSheet(); load();
          }
        }, "primary"),
        button(tx("Volver a mostrar personas y textos ocultos", "Restore muted people and hidden posts"), async () => {
          if (await mutation({ do: "restore_hidden" })) { closeSheet(); load(); }
        }, "ghost"));
      openSheet(tx("Tu perfil y preferencias", "Your profile and preferences"), form);
    }
    async function moderation() {
      try {
        const response = await bridge.call("social", { do: "moderation" });
        if (!active()) return;
        const body = h("div", { class: "social-profile-form" }, h("p", {}, tx("Moderación del propietario. Sin correos públicos.", "Owner moderation. No public emails.")));
        for (const report of response.reports || []) body.append(h("div", { class: "card" },
          h("strong", {}, "#" + report.post), h("p", { class: "social-text" }, report.reason),
          h("small", {}, report.resolved ? tx("Resuelto", "Resolved") : tx("Pendiente", "Pending")),
          button(tx("Ocultar y resolver", "Hide and resolve"), async () => { if (await mutation({ do: "moderate", id: report.post, on: true })) { closeSheet(); load(); } }),
          button(tx("Aprobar y resolver", "Approve and resolve"), async () => { if (await mutation({ do: "moderate", id: report.post, on: false })) { closeSheet(); load(); } })));
        for (const person of response.people || []) if (!person.moderator) body.append(h("div", { class: "row wrap" }, h("b", {}, person.alias),
          button(person.banned ? tx("Readmitir", "Readmit") : tx("Suspender", "Suspend"), async () => {
            if (await confirmBox(person.alias, tx("¿Cambiar su acceso a la comunidad?", "Change community access?"), t("ok")) && await mutation({ do: "ban", person: person.id, on: !person.banned })) { closeSheet(); load(); }
          })));
        openSheet(tx("Administrar comunidad", "Manage community"), body);
      } catch (error) { toast(error.message, "bad"); }
    }
    function postCard(post) {
      const commands = h("div", { class: "row wrap social-actions" });
      const item = h("article", { class: "social-post", "data-post-id": post.id },
        h("div", { class: "social-post-head" }, h("span", { class: "social-avatar", "aria-hidden": "true" }, post.author.alias.slice(0, 2).toUpperCase()),
          h("div", { class: "grow" }, h("b", {}, post.author.alias), post.author.moderator ? h("span", { class: "pill" }, tx("Propietario", "Owner")) : null,
            h("small", { class: "muted" }, fmtTime(post.created) + (post.revision > 1 ? tx(" · editado", " · edited") : ""))),
          post.pinned ? h("span", { class: "pill" }, tx("Fijado", "Pinned")) : null,
          post.hidden ? h("span", { class: "pill" }, tx("Oculto por moderación", "Hidden by moderation")) : null),
        h("p", { class: "social-text" }, post.text), commands);
      commands.append(button((post.liked ? "♥ " : "♡ ") + post.likes, () => act({ do: "like", id: post.id, on: !post.liked }), post.liked ? "on" : "ghost"),
        button(post.saved ? tx("Guardado", "Saved") : tx("Guardar", "Save"), () => act({ do: "save", id: post.id, on: !post.saved }), "ghost"));
      if (!post.parent) commands.append(button(tx("Conversación", "Conversation") + ` · ${post.replies}`, () => go("social", { parent: post.id }), "ghost"));
      if (post.parent) commands.append(button(tx("Ver hilo original", "Open original thread"), () => go("social", { parent: post.parent }), "ghost"));
      const menu = h("details", { class: "social-post-menu" }, h("summary", {}, tx("Opciones", "Options")));
      const options = h("div", { class: "row wrap" });
      if (post.mine) options.append(button(tx("Editar", "Edit"), () => editText(tx("Editar publicación", "Edit post"), post.text, 3000, async text => {
        if (await mutation({ do: "edit", id: post.id, revision: post.revision, text })) { load(); return true; } return false;
      })), button(tx("Eliminar", "Delete"), async () => {
        if (await confirmBox(tx("Eliminar texto", "Delete text"), tx("Se retirará el texto de la comunidad. Las copias de seguridad previas pueden conservarlo.", "The text will be removed from the community. Earlier backups may retain it."), t("delete"))) act({ do: "delete", id: post.id, revision: post.revision });
      }));
      else options.append(button(post.following ? tx("Dejar de seguir", "Unfollow") : tx("Seguir", "Follow"), () => act({ do: "follow", person: post.author.id, on: !post.following })),
        button(tx("Silenciar persona", "Mute person"), () => act({ do: "mute", person: post.author.id, on: true })),
        button(tx("Reportar", "Report"), () => editText(tx("Motivo del reporte", "Report reason"), "", 500, async reason => {
          if (await mutation({ do: "report", id: post.id, reason })) { toast(tx("Reporte enviado al propietario", "Report sent to owner")); return true; } return false;
        })));
      options.append(button(tx("Ocultar para mí", "Hide for me"), () => act({ do: "hide", id: post.id, on: true })));
      if (post.can_moderate) options.append(button(post.pinned ? tx("Desfijar", "Unpin") : tx("Fijar", "Pin"), () => act({ do: "pin", id: post.id, on: !post.pinned })),
        button(post.hidden ? tx("Restaurar publicación", "Restore post") : tx("Moderar: ocultar", "Moderate: hide"), () => act({ do: "moderate", id: post.id, on: !post.hidden })));
      menu.append(options); item.append(menu); return item;
    }
    function drawComposer() {
      if (composer.childElementCount) {
        composer.querySelector(".social-posting-as").textContent = tx("Publicas como ", "Posting as ") + data.me.alias;
        return; // refresh must never erase a draft
      }
      let draft = currentDraft();
      const area = h("textarea", { class: "input social-editor", rows: 4, maxlength: 3000, value: draft.text,
        placeholder: parent ? tx("Escribe una respuesta…", "Write a reply…") : tx("¿Qué quieres compartir? Usa #etiquetas para organizar.", "What would you like to share? Organize with #tags."),
        "aria-label": tx("Nuevo texto", "New text"), oninput: () => {
          draft = { text: area.value, nonce: "" }; store.set(draftKey, draft); counter.textContent = `${area.value.length}/3000`;
        } });
      const counter = h("small", { class: "muted" }, `${draft.text.length}/3000`);
      const send = button(parent ? tx("Responder", "Reply") : tx("Publicar", "Post"), async () => {
        if (busy || !area.value.trim()) return;
        draft = currentDraft();
        const nonce = draft.nonce || (window.crypto?.randomUUID ? crypto.randomUUID() : `post_${Date.now()}_${Math.random().toString(36).slice(2)}`);
        const text = area.value; if (!store.set(draftKey, { text, nonce })) return;
        area.disabled = send.disabled = true;
        try { if (await mutation({ do: "post", text, nonce, parent })) {
          area.value = ""; counter.textContent = "0/3000";
          const latest = currentDraft();
          if (latest.nonce === nonce && latest.text === text) store.del(draftKey);
          await load();
        } } finally { area.disabled = send.disabled = false; }
      }, "primary");
      composer.append(h("p", { class: "small muted social-posting-as" }, tx("Publicas como ", "Posting as ") + data.me.alias), area, h("div", { class: "row wrap" }, counter, send),
        h("details", { class: "context-help" }, h("summary", {}, tx("Privacidad y respaldo", "Privacy and backup")), h("p", {}, t("social_privacy"))));
    }
    function drawToolbar() {
      const search = field({ type: "search", value: query, maxlength: 120, placeholder: tx("Buscar texto o #etiqueta", "Search text or #tag"), "aria-label": tx("Buscar publicaciones", "Search posts") });
      fill(toolbar, parent ? button(tx("Volver a la plaza", "Back to the square"), () => go("social", {}), "ghost") :
        select(feed, [["all", tx("Todo", "All")], ["pinned", tx("Fijados", "Pinned")], ["following", tx("Siguiendo", "Following")], ["saved", tx("Guardados", "Saved")], ["mine", tx("Mis textos", "My posts")]], value => { feed = value; load(); }),
        button(tx("Actualizar", "Refresh"), () => load(), "ghost"), button(tx("Mi perfil", "My profile"), profile, "ghost"),
        data.me.moderator ? button(tx("Administrar", "Manage"), moderation, "ghost") : null,
        h("form", { class: "row social-search", onsubmit: event => { event.preventDefault(); query = search.value; load(); } }, search, button(tx("Buscar", "Search"), () => { query = search.value; load(); }, "ghost")));
      const sel = toolbar.querySelector("select"); if (sel) sel.setAttribute("aria-label", tx("Filtrar comunidad", "Filter community"));
    }
    async function load(append = false) {
      if (!active() || busy) return; busy = true; status.textContent = tx("Consultando comunidad…", "Loading community…"); more.disabled = true;
      try {
        const response = await bridge.call("social", { do: parent ? "thread" : "feed", parent, feed, q: query, ...(append && cursor ? { before: cursor } : {}) });
        if (!active()) return;
        data = response; cursor = response.next || 0; applyPreferences(response.prefs || {}); drawToolbar(); drawComposer();
        if (!append) { list.replaceChildren(); if (response.root) list.append(postCard(response.root), h("h3", {}, tx("Respuestas", "Replies"))); }
        for (const post of response.posts || []) list.append(postCard(post));
        if (!list.childElementCount) list.append(h("p", { class: "empty" }, tx("Aún no hay textos en esta vista. Inicia una conversación.", "No posts in this view yet. Start a conversation.")));
        more.hidden = !cursor;
        status.textContent = tx("Sin actualización automática: tú decides cuándo consultar.", "No automatic polling: refresh when you choose.");
      } catch (error) { if (active()) { status.textContent = error.message; if (!data) fill(list, failure(error.message), button(tx("Reintentar consulta", "Retry loading"), () => load(), "ghost")); } }
      finally { busy = false; more.disabled = false; }
    }
    load();
  }
  return { mount };
})();
VIEWS.social = { tab: "apps", title: () => t("social_title"), render: (main, params) => window.TextCommunity.mount(main, params) };
// The app's remembered start view may already have been rendered by app.js.
if (current.view === "social") render();
