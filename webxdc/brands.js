"use strict";
// Simplified marks of the model makers, drawn inline (the mini app has no network access).

const BRANDS = {
  gemini: {
    color: "#4f7cf7",
    svg: '<defs><linearGradient id="bg-gemini" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#4285f4"/><stop offset=".55" stop-color="#9b72cb"/><stop offset="1" stop-color="#d96570"/></linearGradient></defs>' +
      '<path fill="url(#bg-gemini)" d="M12 2c.7 5.3 4.7 9.3 10 10-5.3.7-9.3 4.7-10 10-.7-5.3-4.7-9.3-10-10 5.3-.7 9.3-4.7 10-10z"/>',
  },
  gemma: {
    color: "#3186ff",
    svg: '<path fill="#3186ff" d="M12 2c.7 5.3 4.7 9.3 10 10-5.3.7-9.3 4.7-10 10-.7-5.3-4.7-9.3-10-10 5.3-.7 9.3-4.7 10-10z"/><circle cx="12" cy="12" r="2.4" fill="#fff"/>',
  },
  openai: {
    color: "currentColor",
    svg: '<g fill="none" stroke="currentColor" stroke-width="1.7">' +
      [0, 60, 120, 180, 240, 300].map((angle) => `<rect x="9.2" y="3" width="5.6" height="10.5" rx="2.8" transform="rotate(${angle} 12 12)"/>`).join("") + "</g>",
  },
  mistral: {
    color: "#fa500f",
    svg: (() => {
      const rows = [["#ffd800", [0, 4]], ["#ffaf00", [0, 1, 3, 4]], ["#ff8205", [0, 1, 2, 3, 4]], ["#fa500f", [0, 2, 4]], ["#e10500", [0, 4]]];
      return rows.map(([color, cols], row) => cols.map((col) => `<rect x="${2 + col * 4}" y="${2.5 + row * 3.8}" width="4" height="3.8" fill="${color}"/>`).join("")).join("");
    })(),
  },
  qwen: {
    color: "#615ced",
    svg: '<path fill="none" stroke="#615ced" stroke-width="2.6" stroke-linejoin="round" d="M12 3.2l7.6 4.4v8.8L12 20.8l-7.6-4.4V7.6z"/><path stroke="#615ced" stroke-width="2.6" stroke-linecap="round" d="M14.2 14.2l3.4 3.4"/>',
  },
  deepseek: {
    color: "#4d6bfe",
    svg: '<path fill="#4d6bfe" d="M2.5 12.8c0-3.9 3.7-6.8 8.6-6.8 2.7 0 4.9.9 6.4 2.3l2.6-2.1c.5-.4 1.3 0 1.2.7l-.5 3.6c.3.7.5 1.5.5 2.3 0 3.9-3.8 6.9-9 6.9-5.4 0-9.8-2.6-9.8-6.9z"/>' +
      '<circle cx="15.6" cy="11.2" r="1.15" fill="#fff"/><path fill="none" stroke="#fff" stroke-width="1.3" stroke-linecap="round" d="M6 14.2c2.4 1.6 5.6 1.8 8.4.6"/>',
  },
  meta: {
    color: "#0668e1",
    svg: '<path fill="none" stroke="#0668e1" stroke-width="2.4" stroke-linecap="round" d="M3.2 15.2c0-4.2 1.9-8.2 4.3-8.2 3.6 0 5.6 10 9.4 10 2.3 0 3.9-1.9 3.9-4.6 0-3-1.6-5.4-3.9-5.4-3.8 0-5.8 10-9.4 10-2.4 0-4.3-.6-4.3-1.8z"/>',
  },
  zai: {
    color: "currentColor",
    svg: '<rect x="2.5" y="2.5" width="19" height="19" rx="5" fill="currentColor"/><path fill="none" stroke="var(--surface, #fff)" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round" d="M7.5 8h9l-9 8h9"/>',
  },
  nvidia: {
    color: "#76b900",
    svg: '<rect x="2.5" y="2.5" width="19" height="19" rx="4" fill="#76b900"/><path fill="#fff" d="M5 12c2.2-3.3 5-5 8.2-5 2.4 0 4.4.9 5.8 2.2v5.6C17.6 16.1 15.6 17 13.2 17 10 17 7.2 15.3 5 12zm8.2 2.8a2.8 2.8 0 1 0 0-5.6 2.8 2.8 0 0 0 0 5.6z"/>',
  },
  openrouter: {
    color: "#6467f2",
    svg: '<g fill="none" stroke="#6467f2" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 12h5.5c2.5 0 3.5-5 6-5h5"/><path d="M8 12c2.5 0 3.5 5 6 5h5"/><path d="M17 4.5 19.5 7 17 9.5"/><path d="M17 14.5 19.5 17 17 19.5"/></g>',
  },
  cloudflare: {
    color: "#f38020",
    svg: '<path fill="#f38020" d="M17.2 18H5.6A3.6 3.6 0 0 1 5 10.9a5.6 5.6 0 0 1 10.5-2.2A4.7 4.7 0 0 1 21.5 13a4.6 4.6 0 0 1-4.3 5z"/><path fill="#faae40" d="M17.2 18H12c1.6-2.8 3.5-4.4 6.4-4.6.9-.1 2 .1 3 .6A4.6 4.6 0 0 1 17.2 18z"/>',
  },
  tencent: {
    color: "#0052d9",
    svg: '<circle cx="12" cy="12" r="9.5" fill="none" stroke="#0052d9" stroke-width="2.4"/><path fill="#00a3ff" d="M12 4.5a7.5 7.5 0 0 1 0 15 3.75 3.75 0 0 1 0-7.5 3.75 3.75 0 0 0 0-7.5z"/>',
  },
  microsoft: {
    color: "#00a4ef",
    svg: '<rect x="3" y="3" width="8.5" height="8.5" fill="#f25022"/><rect x="12.5" y="3" width="8.5" height="8.5" fill="#7fba00"/><rect x="3" y="12.5" width="8.5" height="8.5" fill="#00a4ef"/><rect x="12.5" y="12.5" width="8.5" height="8.5" fill="#ffb900"/>',
  },
  moonshot: {
    color: "currentColor",
    svg: '<circle cx="12" cy="12" r="9.5" fill="currentColor"/><path fill="var(--surface, #fff)" d="M14.5 6.2a6.2 6.2 0 1 0 3.3 11.1 7 7 0 0 1-3.3-11.1z"/>',
  },
};

// Model id or title -> maker. Providers that only route traffic (Groq, OrcaRouter, BazaarLink) show the maker of the model itself.
const BRAND_RULES = [
  [/gemma/i, "gemma"],
  [/gemini/i, "gemini"],
  [/gpt|openai|\bo[134]\b/i, "openai"],
  [/mistral|ministral|codestral|magistral|pixtral|devstral/i, "mistral"],
  [/qwen|qwq/i, "qwen"],
  [/deepseek/i, "deepseek"],
  [/llama|meta-|\bmeta\b/i, "meta"],
  [/glm|z-ai|zai|zhipu/i, "zai"],
  [/nemotron|nvidia/i, "nvidia"],
  [/hunyuan|tencent|\bhy\d/i, "tencent"],
  [/\bphi-?\d|microsoft/i, "microsoft"],
  [/kimi|moonshot/i, "moonshot"],
  [/openrouter/i, "openrouter"],
];
const PROVIDER_BRANDS = { gemini: "gemini", mistral: "mistral", zai: "zai", cloudflare: "cloudflare", openrouter: "openrouter", ghmodels: "openai" };

function brandOf(model, provider) {
  for (const text of [(model && model.title) || "", (model && model.id) || ""]) {
    for (const [pattern, name] of BRAND_RULES) if (pattern.test(text)) return name;
  }
  return PROVIDER_BRANDS[provider && provider.key] || "";
}

function brandMark(name, extra = "") {
  const brand = BRANDS[name];
  const holder = document.createElement("span");
  holder.className = `brand ${extra}`;
  holder.innerHTML = brand
    ? `<svg viewBox="0 0 24 24" aria-hidden="true" style="color:${brand.color === "currentColor" ? "inherit" : brand.color}">${brand.svg}</svg>`
    : `<svg class="ic" viewBox="0 0 24 24" aria-hidden="true">${ICONS.ai}</svg>`;
  return holder;
}
