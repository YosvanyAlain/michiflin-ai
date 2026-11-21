"use strict";
// Deliberately local: no AI, API keys, mail, uploads or persistent input history.
window.LocalUtilities = {
  mount() {
    const es = lang() !== "en";
    const items = [
      ["text", es ? "Texto: contar y limpiar" : "Text: count and clean", es ? "Palabras, caracteres y espacios" : "Words, characters and whitespace"],
      ["json", es ? "Formatear JSON" : "Format JSON", "JSON"],
      ["hash", "SHA-256", es ? "Huella de texto; no cifra contraseñas" : "Text fingerprint; not password encryption"],
      ["password", es ? "Contraseña aleatoria" : "Random password", es ? "Se genera en este dispositivo" : "Generated on this device"],
      ["base64", "Base64", es ? "Codificar texto UTF-8 (no cifrado)" : "Encode UTF-8 text (not encryption)"],
      ["url", es ? "Limpiar enlace" : "Clean URL", es ? "Quita rastreadores; no abre el enlace" : "Remove trackers; does not open the link"],
      ["percent", es ? "Porcentajes y descuentos" : "Percentages and discounts", es ? "Ejemplo: 250 15" : "Example: 250 15"],
    ];
    const root = h("div", { class: "stack local-utilities" });
    add(root, h("p", { class: "note" }, icon("shield"), es ? "Estas herramientas funcionan sin correo ni IA. El texto no sale del dispositivo." : "These tools work without mail or AI. Text stays on this device."));
    for (const [key, title, hint] of items) {
      const input = h("textarea", { class: "input", rows: 2, maxlength: 100000, placeholder: hint });
      const result = h("pre", { class: "local-tool-result", tabindex: "0" });
      const run = h("button", { class: "btn primary small", onclick: async () => {
        run.disabled = true;
        try {
          const text = input.value;
          let output = "";
          if (key === "text") output = `${(text.trim().match(/\S+/gu) || []).length} ${es ? "palabras" : "words"} · ${[...text].length} ${es ? "caracteres" : "characters"}\n\n${text.trim().replace(/[^\S\n]+/g, " ")}`;
          if (key === "json") output = JSON.stringify(JSON.parse(text), null, 2);
          if (key === "hash") {
            const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
            output = [...new Uint8Array(bytes)].map(n => n.toString(16).padStart(2, "0")).join("");
          }
          if (key === "password") {
            const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%*-_";
            const chosen = []; const bound = 256 - (256 % chars.length);
            while (chosen.length < 24) for (const n of crypto.getRandomValues(new Uint8Array(32))) {
              if (n < bound && chosen.length < 24) chosen.push(chars[n % chars.length]);
            }
            output = chosen.join("");
          }
          if (key === "base64") {
            const bytes = new TextEncoder().encode(text); let binary = "";
            for (let offset = 0; offset < bytes.length; offset += 8192)
              binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
            output = btoa(binary);
          }
          if (key === "url") {
            const url = new URL(text.trim());
            if (!["https:", "http:"].includes(url.protocol) || url.username || url.password) throw new Error("URL");
            for (const name of [...url.searchParams.keys()]) if (/^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$)/i.test(name)) url.searchParams.delete(name);
            output = url.href;
          }
          if (key === "percent") {
            const values = text.trim().split(/[\s;]+/).map(n => Number(n.replace(",", ".")));
            if (values.length !== 2 || !values.every(Number.isFinite)) throw new Error("input");
            const [amount, percent] = values, part = amount * percent / 100;
            output = `${percent}% = ${part}\n${es ? "Con descuento" : "Discounted"}: ${amount - part}\n${es ? "Con incremento" : "Increased"}: ${amount + part}`;
          }
          result.textContent = output;
        } catch (_) { result.textContent = es ? "Revisa el formato de entrada o la disponibilidad de criptografía del cliente." : "Check the input format or the client's cryptography support."; }
        finally { run.disabled = false; }
      } }, es ? "Usar herramienta" : "Run tool");
      add(root, h("details", { class: "card local-tool-card" }, h("summary", {}, icon("tools"), h("b", { class: "grow" }, title), icon("down", "caret")),
        h("div", { class: "stack utility-body" }, h("p", { class: "muted small" }, hint), key === "password" ? null : input, run, result,
          h("button", { class: "btn ghost small", onclick: () => exportSheet(new Blob([result.textContent], { type: "text/plain" }), key + ".txt") }, icon("share"), t("export")))));
    }
    return root;
  }
};
