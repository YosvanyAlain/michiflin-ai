"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const PROPS = new Set(["value", "checked", "disabled", "selected", "hidden", "multiple", "readOnly"]);

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function add(node, ...children) {
  return append(node, children);
}

function fill(node, ...children) {
  node.replaceChildren();
  return append(node, children);
}

function h(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (PROPS.has(key)) node[key] = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  return append(node, children);
}

const ICONS = {
  code: '<path d="m8 8-4 4 4 4"/><path d="m16 8 4 4-4 4"/><path d="m14 5-4 14"/>',
  home: '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M10 21v-6h4v6"/>',
  ai: '<path d="M12 3l1.8 4.7 4.7 1.8-4.7 1.8L12 16l-1.8-4.7-4.7-1.8 4.7-1.8z"/><path d="M19 15l.8 2.2 2.2.8-2.2.8L19 21l-.8-2.2-2.2-.8 2.2-.8z"/>',
  web: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14 14 0 0 1 0 18 14 14 0 0 1 0-18"/>',
  apps: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  downloads: '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  back: '<path d="m15 18-6-6 6-6"/>',
  next: '<path d="m9 18 6-6-6-6"/>',
  up: '<path d="m18 15-6-6-6 6"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
  send: '<path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4z"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>',
  youtube: '<rect x="2" y="5" width="20" height="14" rx="4"/><path d="m10 9 5 3-5 3z"/>',
  rates: '<circle cx="8" cy="8" r="6"/><path d="M18.1 10.4A6 6 0 1 1 10.3 18"/><path d="M7 6h1v4"/><path d="m16.7 13.9.7.7-2.8 2.8"/>',
  book: '<path d="M4 4h6a3 3 0 0 1 3 3v13a3 3 0 0 0-3-3H4z"/><path d="M20 4h-4a3 3 0 0 0-3 3v13a3 3 0 0 1 3-3h4z"/>',
  cloudrain: '<path d="M6 15a4 4 0 0 1-.4-8 5.5 5.5 0 0 1 10.6 1.3A3.5 3.5 0 0 1 18 15z"/><path d="m8 18-1 2m5-2-1 2m5-2-1 2"/>',
  cloudsnow: '<path d="M6 14a4 4 0 0 1-.4-8 5.5 5.5 0 0 1 10.6 1.3A3.5 3.5 0 0 1 18 14z"/><path d="M8 17v4m-2-2h4m4-2v4m-2-2h4"/>',
  cloudstorm: '<path d="M6 14a4 4 0 0 1-.4-8 5.5 5.5 0 0 1 10.6 1.3A3.5 3.5 0 0 1 18 14z"/><path d="m13 14-3 5h3l-2 3"/>',
  cloudfog: '<path d="M6 13a4 4 0 0 1-.4-8 5.5 5.5 0 0 1 10.6 1.3A3.5 3.5 0 0 1 18 13z"/><path d="M5 17h14M8 20h11"/>',
  moon: '<path d="M20 15A8 8 0 0 1 9 4a8 8 0 1 0 11 11z"/>',
  wind: '<path d="M3 8h12c3 0 3-4 1-4-1.5 0-2 1-2 2M2 12h17c3 0 3 4 1 4-1.5 0-2-1-2-2M3 16h9c3 0 3 4 1 4-1.5 0-2-1-2-2"/>',
  drop: '<path d="M12 3s-6 7-6 11a6 6 0 0 0 12 0c0-4-6-11-6-11z"/>',
  weather: '<path d="M7 19a4.5 4.5 0 0 1-.6-9 6 6 0 0 1 11.4 1.6A3.8 3.8 0 0 1 17.5 19z"/>',
  news: '<path d="M4 4h13a1 1 0 0 1 1 1v14a2 2 0 0 0 2 2H6a2 2 0 0 1-2-2z"/><path d="M18 8h2a1 1 0 0 1 1 1v10a2 2 0 0 1-2 2"/><path d="M8 8h6M8 12h6M8 16h4"/>',
  translate: '<path d="m5 8 6 6"/><path d="m4 14 6-6 2-3"/><path d="M2 5h12"/><path d="M7 2h1"/><path d="m22 22-5-10-5 10"/><path d="M14 18h6"/>',
  tools: '<rect x="5" y="2" width="14" height="20" rx="2"/><path d="M8 6h8M8 11h.01M12 11h.01M16 11h.01M8 15h.01M12 15h.01M16 15h.01M8 19h.01M12 19h.01M16 19h.01"/>',
  join: '<path d="m12 2 10 5-10 5L2 7z"/><path d="m2 12 10 5 10-5"/><path d="m2 17 10 5 10-5"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  qr: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><path d="M14 14h3v3h-3zM20 14h1M14 20h1M19 17v4h2"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>',
  trash: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  share: '<path d="M4 12v7a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7"/><path d="m16 6-4-4-4 4"/><path d="M12 2v13"/>',
  chat: '<path d="M21 11.5a8.4 8.4 0 0 1-12.5 7.4L3 21l2.1-5.5A8.5 8.5 0 1 1 21 11.5z"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  pdf: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M8 13h8M8 17h6"/>',
  camera: '<path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/>',
  audio: '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
  video: '<rect x="2" y="2" width="20" height="20" rx="2.5"/><path d="M7 2v20M17 2v20M2 12h20M2 7h5M2 17h5M17 17h5M17 7h5"/>',
  pause: '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
  play: '<path d="m6 3 14 9-14 9z"/>',
  close: '<path d="M18 6 6 18M6 6l12 12"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  alert: '<circle cx="12" cy="12" r="9"/><path d="M12 8v4M12 16h.01"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  swap: '<path d="m17 3 4 4-4 4"/><path d="M3 7h18"/><path d="m7 21-4-4 4-4"/><path d="M21 17H3"/>',
  bolt: '<path d="M13 2 3 14h9l-1 8 10-12h-9z"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.9 1.9 0 0 0 3.4 0"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>',
  health: '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
  help: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5z"/><path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  open: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  subtitles: '<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M7 15h4M15 15h2M7 11h2M13 11h4"/>',
  pin: '<path d="M12 17v5"/><path d="M9 10.8V4h6v6.8l3 3.2v2H6v-2z"/>',
  ban: '<circle cx="12" cy="12" r="9"/><path d="m5.6 5.6 12.8 12.8"/>',
  clip: '<path d="m21 11.5-8.6 8.6a5.5 5.5 0 0 1-7.8-7.8l8.6-8.6a3.7 3.7 0 0 1 5.2 5.2l-8.6 8.6a1.8 1.8 0 0 1-2.6-2.6l7.9-7.9"/>',
  more: '<circle cx="5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="19" cy="12" r="1.2"/>',
  cat: '<path d="M5.6 10.4 L6.9 5.4 L10.5 7.6 Q12 7.2 13.5 7.6 L17.1 5.4 L18.4 10.4 Q19.2 14.9 16.2 17.7 Q14.4 19.4 12 19.4 Q9.6 19.4 7.8 17.7 Q4.8 14.9 5.6 10.4 Z"/><circle cx="9.7" cy="12" r="1.5"/><circle cx="14.3" cy="12" r="1.5"/><path d="M11.35 14 L12.65 14 L12 14.75 Z" fill="currentColor" stroke="none"/><path d="M10.7 15.1 Q12 16.4 13.3 15.1"/>',
  catfull: '<path d="M8.5 6.8 L8.1 3.3 L11 5.2 Q12 5 13 5.2 L15.9 3.3 L15.5 6.8 Q16.9 8.1 16.9 9.7 Q16.9 12.5 12 12.5 Q7.1 12.5 7.1 9.7 Q7.1 8.1 8.5 6.8 Z"/><path d="M9 12.4 Q7.3 15.3 7.5 18.6 Q7.6 20.6 9.5 20.6 L14.6 20.6 Q16.5 20.6 16.4 18.6 Q16.2 15.3 15 12.4"/><path d="M16.3 20.4 Q20.7 20.2 20.3 16.4 Q20.1 14.6 18.6 14.5"/><path d="M10.5 20.6 L10.5 18.7 M13.6 20.6 L13.6 18.7"/><circle cx="10.3" cy="9.5" r="0.95"/><circle cx="13.7" cy="9.5" r="0.95"/><path d="M11.4 10.9 L12.6 10.9 L12 11.6 Z" fill="currentColor" stroke="none"/><path d="M10.9 11.9 Q12 12.8 13.1 11.9"/>',
  notes: '<rect x="4" y="3" width="16" height="19" rx="3"/><path d="M8 2v3M16 2v3M8 10h8M8 14h8M8 18h5"/>',
  links: '<path d="M10.5 13.5a4.5 4.5 0 0 0 6.4 0l2.6-2.6a4.5 4.5 0 0 0-6.4-6.4l-1.5 1.5"/><path d="M13.5 10.5a4.5 4.5 0 0 0-6.4 0l-2.6 2.6a4.5 4.5 0 0 0 6.4 6.4l1.5-1.5"/>',
  nauta: '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="m4 7 8 6 8-6M4 17l5-4M20 17l-5-4"/>',
  killswitch: '<path d="M12 3v9"/><path d="M7.1 5.8a8 8 0 1 0 9.8 0"/><circle cx="12" cy="12" r="1"/>',
  status: '<circle cx="12" cy="12" r="9"/><path d="M5 12h3l2-4 3 8 2-4h4"/>',
};

function icon(name, extra = "") {
  const holder = document.createElement("span");
  const key = Object.prototype.hasOwnProperty.call(ICONS, name) ? name : "info";
  const art = ICONS[key];
  holder.innerHTML = `<svg class="ic ${extra}" viewBox="0 0 24 24" aria-hidden="true">${art}</svg>`;
  return holder.firstChild;
}

// Original local vector artwork: the same ginger cat, mint collar and cream
// markings in both poses. No external images, fonts or animation runtime.
function mascot(pose = "seated") {
  const seated = `<ellipse cx="81" cy="183" rx="53" ry="9" fill="#082e2a" opacity=".16"/>
    <g stroke="#653e32" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
      <path d="M110 161c41 12 41-38 21-38-14 0-15 13-8 18" fill="none" stroke="#df8c4c" stroke-width="17"/>
      <path d="M56 91c-15 20-23 50-19 73 3 15 19 18 43 18s39-5 41-18c4-26-8-56-22-73" fill="#eaaa68"/>
      <path d="M72 102c-13 16-14 35-12 64h39c2-30-1-48-14-65" fill="#fff0d9" stroke="none"/>
      <path d="M55 144l1 32c-9 1-12 10 0 10h14c5 0 7-6 7-11v-24M100 144l-1 32c10 1 12 10 0 10H85c-5 0-7-6-7-11v-24" fill="#fff0d9"/>
      <path d="M46 53 42 14c13 1 24 13 30 22m16 0c10-17 19-22 31-22l-5 40" fill="#eaaa68"/>
      <path d="m49 26 3 25 17-10m23-1 20-14-4 25" fill="#e8a4a0" stroke="none"/>
      <path d="M42 52c9-17 61-20 76 0 8 11 7 31-2 42-16 18-56 18-73-1-9-10-11-30-1-41Z" fill="#efb879"/>
      <path d="m72 39 8 17 9-17" fill="#cc7f43" stroke="none"/>
      <path d="M45 77c7-6 15-5 22 1 8-11 21-11 29 0 6-6 16-7 22-1-1 22-22 29-38 29S47 99 45 77" fill="#fff2df" stroke="none"/>
      <path class="mascot-eyes" d="M54 68q6-7 12 0m28 0q6-7 12 0" fill="none" stroke-width="4"/>
      <path d="m74 80 6 6 6-6Z" fill="#b76c6b" stroke="none"/>
      <path d="M80 86v5m-8 0q8 7 16 0M49 81l-18-3m18 10-18 4m81-11 18-3m-18 10 18 4" fill="none" stroke-width="2"/>
      <path d="M52 106q27 11 54-1l-2 8q-24 10-50 0Z" fill="#4cbfa8" stroke-width="2"/>
      <circle cx="80" cy="116" r="6" fill="#f8ce6d" stroke-width="2"/>
      <path d="M42 130l9 4m-11 8 9 3m60-15-9 4m12 8-9 3" stroke="#c37b43"/>
    </g>`;
  const running = `<g class="cat-tail" fill="none" stroke="#df8c4c" stroke-width="14" stroke-linecap="round"><path d="M56 82C10 92 4 43 26 42"/></g>
    <g stroke="#653e32" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
      <path class="cat-back-leg rear" d="M78 90 66 112 39 121q-8 10 7 10l34-10 19-26" fill="#d79254"/>
      <path class="cat-front-leg rear" d="m141 92 7 24 23 10q10 9 14 0l-22-24-4-25" fill="#fff0d9"/>
      <path d="M57 62q31-19 75-6c23 8 34 32 18 48-18 13-75 9-95-4-13-11-10-25 2-38Z" fill="#eaaa68"/>
      <path d="M103 92q33 8 49-6l-2 16c-12 14-37 10-52 8Z" fill="#fff0d9" stroke="none"/>
      <path class="cat-back-leg" d="M75 90 91 113 73 127q-4 9 9 9l29-19-16-29" fill="#efb879"/>
      <path class="cat-front-leg" d="m140 90-9 23 10 19q11 8 13-2l-5-17 17-22" fill="#fff0d9"/>
      <g class="cat-running-head">
        <path d="m135 42 2-32q15 3 22 20m16 0 24-13-5 32" fill="#efb879"/>
        <path d="m143 22 1 21 13-9m23 1 13-10-3 18" fill="#e8a4a0" stroke="none"/>
        <path d="M134 44c15-17 48-15 62 0 10 10 11 29 3 40-14 19-48 20-64 2-10-11-11-29-1-42Z" fill="#efb879"/>
        <path d="m159 32 8 14 8-13" fill="#cc7f43" stroke="none"/>
        <path d="M138 69c10-5 18-1 22 3q12-12 24-1c10-5 16-3 18 2-1 20-21 25-35 25-17 0-28-11-29-19" fill="#fff2df" stroke="none"/>
        <path d="m146 58 7 1m29-1 7-1" stroke-width="4"/>
        <path d="m164 73 6 5 6-5Z" fill="#b76c6b" stroke="none"/>
        <path d="m170 79 0 5m-6 0q6 6 12 0m-33-10-17-2m19 10-17 3m66-11 18-2m-19 10 18 3" fill="none" stroke-width="2"/>
        <path d="M141 94q20 10 44-1l-4 8q-17 8-37 0Z" fill="#4cbfa8" stroke-width="2"/>
        <circle cx="167" cy="104" r="5" fill="#f8ce6d" stroke-width="2"/>
      </g>
      <path d="m69 64 8 12m6-16 7 11m6-12 5 10" stroke="#c37b43"/>
    </g>`;
  const holder = document.createElement("span"), moving = pose === "running";
  holder.innerHTML = `<svg class="mascot mascot-${moving ? "running" : "seated"}" viewBox="${moving ? "0 0 230 150" : "0 0 160 200"}" aria-hidden="true">${moving ? running : seated}</svg>`;
  return holder.firstChild;
}

const store = {
  get(key, fallback) {
    try {
      const raw = localStorage.getItem("mf." + key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch (error) {
      return fallback;
    }
  },
  set(key, value, quiet = false) {
    try {
      localStorage.setItem("mf." + key, JSON.stringify(value));
      return true;
    } catch (error) {
      if (!quiet) toast(t("storage_full"), "bad");
      return false;
    }
  },
  del(key) {
    try { localStorage.removeItem("mf." + key); return true; }
    catch (error) { toast(t("storage_full"), "bad"); return false; }
  },
};

// One shared catalogue keeps the saved preference and the accessible selector in sync.
const VISUAL_STYLES = Object.freeze([
  "michiflin", "material", "ocean", "pearl", "onyx", "emerald", "sapphire",
  "amethyst", "ruby", "copper", "arctic", "mocha", "aurora"
]);
const prefs = Object.assign({ lang: "auto", theme: "system", style: "michiflin", accent: "style", engine: "web" }, store.get("prefs", {}));

function savePrefs() {
  store.set("prefs", prefs);
}

function systemLang() {
  const first = (navigator.languages && navigator.languages[0]) || navigator.language || "";
  return first.toLowerCase().split("-")[0] === "es" ? "es" : "en";
}

function lang() {
  return prefs.lang === "es" || prefs.lang === "en" ? prefs.lang : systemLang();
}

function has(key) {
  return Object.prototype.hasOwnProperty.call(I18N[lang()], key);
}

function t(key, vars) {
  const text = has(key) ? I18N[lang()][key] : key;
  return vars ? text.replace(/\{(\w+)\}/g, (match, name) => (name in vars ? vars[name] : match)) : text;
}

function applyLook() {
  if (!VISUAL_STYLES.includes(prefs.style)) { prefs.style = "michiflin"; savePrefs(); }
  document.documentElement.dataset.theme = ["system", "light", "dark"].includes(prefs.theme) ? prefs.theme : "system";
  document.documentElement.dataset.style = VISUAL_STYLES.includes(prefs.style) ? prefs.style : "michiflin";
  document.documentElement.dataset.accent = ["style", "teal", "violet", "blue", "rose"].includes(prefs.accent) ? prefs.accent : "style";
  document.documentElement.lang = lang();
}

function fmtNumber(value, digits = 0) {
  return Number(value).toLocaleString(lang(), { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

function fmtSize(bytes) {
  const units = ["B", "KB", "MB", "GB"];
  let value = Number(bytes) || 0;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${fmtNumber(value, index > 1 ? 1 : 0)} ${units[index]}`;
}

function fmtDuration(seconds) {
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total} s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} h ${String(minutes % 60).padStart(2, "0")} min`;
  return `${Math.floor(hours / 24)} ${t("days_short")}`;
}

function fmtTime(seconds) {
  const date = new Date(seconds * 1000);
  const sameDay = date.toDateString() === new Date().toDateString();
  const options = sameDay ? { hour: "2-digit", minute: "2-digit" } : { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" };
  return date.toLocaleString(lang(), options);
}

function now() {
  return Date.now() / 1000;
}

const EMOJI = /(?:[\p{Extended_Pictographic}\u{1F1E6}-\u{1F1FF}][\u{FE0F}\u{200D}\u{20E3}\u{1F3FB}-\u{1F3FF}\p{Extended_Pictographic}]*|[\u{FE0F}\u{200D}\u{20E3}]) ?/gu;

function plain(text) {
  return String(text || "").replace(EMOJI, "").replace(/[ \t]+$/gm, "").trim();
}

// Countdowns and elapsed counters refresh themselves once per second.
function until(seconds, doneText = "") {
  return h("span", { class: "until", "data-until": String(seconds), "data-done": doneText }, fmtDuration(seconds - now()));
}

function since(seconds) {
  return h("span", { class: "since", "data-since": String(seconds) }, fmtDuration(now() - seconds));
}

setInterval(() => {
  const moment = now();
  document.querySelectorAll("[data-until]").forEach((node) => {
    const left = Number(node.dataset.until) - moment;
    node.textContent = left > 0 ? fmtDuration(left) : node.dataset.done || fmtDuration(0);
  });
  document.querySelectorAll("[data-since]").forEach((node) => {
    node.textContent = fmtDuration(moment - Number(node.dataset.since));
  });
}, 1000);

function toast(text, kind = "") {
  const box = $("#toasts");
  const node = h("div", { class: `toast ${kind}` }, icon(kind === "bad" ? "alert" : kind === "ok" ? "check" : "info"), h("span", {}, plain(text)));
  box.append(node);
  setTimeout(() => node.classList.add("out"), 3600);
  setTimeout(() => node.remove(), 4000);
  return node;
}

let sheetRevision = 0;
let sheetReturnFocus = null;
let sheetOnClose = null;
let sheetBackgroundInert = false;

function sheetFocusable() {
  return [...$("#sheet").querySelectorAll('button, input, select, textarea, a[href], [tabindex]')]
    .filter((node) => !node.disabled && node.tabIndex >= 0 && !node.closest('[inert]')
      && node.getClientRects().length && getComputedStyle(node).visibility !== "hidden");
}

function closeSheet() {
  const sheet = $("#sheet");
  if (sheet.hidden) return;
  const revision = ++sheetRevision;
  const onClose = sheetOnClose;
  sheetOnClose = null;
  sheet.classList.remove("open");
  setTimeout(() => {
    // A newer sheet may already have replaced this one during its animation.
    if (revision !== sheetRevision) return;
    sheet.hidden = true;
    fill(sheet);
    $("#app").inert = sheetBackgroundInert;
    const target = sheetReturnFocus;
    sheetReturnFocus = null;
    if (target && target.isConnected && !target.closest('[inert]')) target.focus({ preventScroll: true });
    // Only acknowledge the current result after the sheet has really closed.
    if (typeof revealVisibleNotices === "function") revealVisibleNotices();
    else if (typeof markVisibleNoticesRead === "function") markVisibleNoticesRead();
  }, 220);
  // Escape, scrim and close must settle an awaiting confirmation too.
  if (onClose) onClose();
}

document.addEventListener("keydown", (event) => {
  if ($("#sheet").hidden) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeSheet();
  } else if (event.key === "Tab") {
    const nodes = sheetFocusable(), first = nodes[0], last = nodes[nodes.length - 1];
    if (!first) {
      event.preventDefault();
      $("#sheet .sheet-panel").focus({ preventScroll: true });
    } else if (!$("#sheet").contains(document.activeElement) ||
               (event.shiftKey ? document.activeElement === first : document.activeElement === last)) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus({ preventScroll: true });
    }
  }
});

function openSheet(title, body, onClose = null) {
  const sheet = $("#sheet"), revision = ++sheetRevision;
  const previousClose = sheetOnClose;
  sheetOnClose = null;
  if (previousClose) previousClose();
  if (sheet.hidden) {
    sheetReturnFocus = document.activeElement;
    sheetBackgroundInert = $("#app").inert;
  }
  sheetOnClose = onClose;
  const titleId = `sheet-title-${revision}`;
  const panel = h("div", { class: "sheet-panel", role: "dialog", "aria-modal": "true", "aria-labelledby": titleId, tabindex: "-1" },
    h("div", { class: "sheet-head" }, h("h2", { id: titleId }, title), h("button", { class: "icon-btn", "aria-label": t("close"), onclick: closeSheet }, icon("close"))),
    h("div", { class: "sheet-body" }, body));
  fill(sheet, h("div", { class: "sheet-scrim", onclick: closeSheet }), panel);
  sheet.hidden = false;
  $("#app").inert = true;
  requestAnimationFrame(() => {
    if (revision !== sheetRevision) return;
    sheet.classList.add("open");
    (sheetFocusable()[0] || panel).focus({ preventScroll: true });
  });
  return panel;
}

function confirmBox(title, text, okLabel) {
  return new Promise((resolve) => {
    const done = (value) => {
      // Settle the explicit choice before close invokes the dismissal callback.
      resolve(value);
      closeSheet();
    };
    openSheet(title, [
      h("p", { class: "muted" }, text),
      h("div", { class: "row end" },
        h("button", { class: "btn ghost", onclick: () => done(false) }, t("cancel")),
        h("button", { class: "btn danger", onclick: () => done(true) }, okLabel || t("confirm"))),
    ], () => resolve(false));
  });
}

async function copyText(text) {
  let copied = false;
  try {
    await navigator.clipboard.writeText(text);
    copied = true;
  } catch (error) {
    const area = h("textarea", { value: text, style: "position:fixed;opacity:0" });
    document.body.append(area);
    try { area.select(); copied = document.execCommand("copy"); } catch (fallbackError) { /* client refused clipboard */ }
    finally { area.remove(); }
  }
  toast(t(copied ? "copied" : "copy_failed"), copied ? "ok" : "bad");
}

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

function base64ToBlob(data, type) {
  const binary = atob(data);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return new Blob([bytes], { type });
}

// Readable file name: keeps letters (accents included), digits and spaces; drops characters file systems reject.
function niceName(text, fallback = "file") {
  const clean = String(text || "").replace(/[\u0000-\u001f\/\\:*?"<>|#%{}^~\[\]`]+/g, " ").replace(/\s+/g, " ").trim().slice(0, 60).trim();
  return clean.replace(/^\.+/, "") || fallback;
}

async function sendToChat(blob, name, text = "") {
  try {
    await window.webxdc.sendToChat({ file: { name, blob }, text });
  } catch (error) {
    if (blob.size > 40 * 1024 * 1024) throw error;
    await window.webxdc.sendToChat({ file: { name, base64: await blobToBase64(blob) }, text });
  }
}

async function saveFile(blob, name) {
  const file = new File([blob], name, { type: blob.type || "application/octet-stream" });
  if (navigator.canShare && navigator.canShare({ files: [file] })) {
    try {
      await navigator.share({ files: [file] });
      return;
    } catch (error) {
      if (error.name === "AbortError") return;
    }
  }
  const url = URL.createObjectURL(blob);
  const anchor = h("a", { href: url, download: name });
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

function exportSheet(blob, name, text = "") {
  const run = (job) => async () => {
    closeSheet();
    try {
      await job();
    } catch (error) {
      toast(t("export_failed"), "bad");
    }
  };
  openSheet(t("export"), [
    h("p", { class: "muted small" }, `${name} · ${fmtSize(blob.size)}`),
    h("div", { class: "choice-list" },
      h("button", { class: "choice", onclick: run(() => sendToChat(blob, name, text)) }, icon("chat"),
        h("span", {}, h("b", {}, t("export_chat")), h("small", {}, t("export_chat_hint")))),
      h("button", { class: "choice", onclick: run(() => saveFile(blob, name)) }, icon("share"),
        h("span", {}, h("b", {}, t("export_device")), h("small", {}, t("export_device_hint"))))),
  ]);
}

const media = {
  db: null,
  memory: new Map(),
  open() {
    if (!this.db) {
      this.db = new Promise((resolve) => {
        try {
          const request = indexedDB.open("michiflin", 1);
          request.onupgradeneeded = () => request.result.createObjectStore("images", { keyPath: "id" });
          request.onsuccess = () => resolve(request.result);
          request.onerror = () => resolve(null);
        } catch (error) {
          resolve(null);
        }
      });
    }
    return this.db;
  },
  async run(mode, work) {
    const db = await this.open();
    if (!db) return work(null);
    return new Promise((resolve, reject) => {
      const transaction = db.transaction("images", mode);
      const request = work(transaction.objectStore("images"));
      transaction.oncomplete = () => resolve(request && "result" in request ? request.result : undefined);
      transaction.onerror = () => reject(transaction.error);
    });
  },
  async put(item) {
    try {
      if (await this.open()) {
        await this.run("readwrite", (os) => os.put(item));
        return true;
      }
    } catch (error) { /* storage full or unavailable: keep a temporary copy */ }
    this.memory.set(item.id, item);
    return false;
  },
  async get(id) {
    let item = null;
    try { item = await this.run("readonly", (os) => (os ? os.get(id) : null)); } catch (error) { /* memory still works */ }
    return item || this.memory.get(id) || null;
  },
  async all() {
    let stored = [];
    try { stored = (await this.run("readonly", (os) => (os ? os.getAll() : null))) || []; } catch (error) { /* memory still works */ }
    const items = new Map(stored.map((item) => [item.id, item]));
    for (const item of this.memory.values()) items.set(item.id, item);
    return [...items.values()].sort((a, b) => b.ts - a.ts);
  },
  remove(id) {
    this.memory.delete(id);
    return this.run("readwrite", (os) => (os ? os.delete(id) : undefined));
  },
  clear() {
    this.memory.clear();
    return this.run("readwrite", (os) => (os ? os.clear() : undefined));
  },
};

// Requests travel as webxdc updates (e-mails); answers can arrive minutes later or after the app was closed.
const STALE_MS = 45 * 60 * 1000;
const UPDATE_MAX = 126000; // webxdc caps one update at 128000 bytes
const CODE_REQUEST_MAX = 600000; // large coding projects are split into bounded request updates
const REQUEST_CHUNK = 74000;
// Unanswered requests are resent with the same id (the bot answers a resend from its cache or says it is working).
const RESEND_AFTER = [45000, 90000, 180000, 300000];
const RESENDABLE = 24000; // bytes; bigger requests (attachments) are not stored for a resend
// Past this without any sign from the bot, a request is given up; an acknowledged one gets ACK_WAIT more.
const ANSWER_MS = 12 * 60 * 1000;
const ACK_WAIT = 16 * 60 * 1000;

const bridge = {
  serial: store.get("serial", 0),
  waiting: new Map(),
  chunks: new Map(),
  // Keep just identifiers for this opening. A failed cleanup can leave even
  // an acknowledged entry in storage indefinitely, including past STALE_MS.
  // Forgetting its tombstone would revive and possibly resend completed work.
  finished: new Set(),
  handlers: {},
  pushes: {},
  watchers: new Set(),
  // In-memory only: end-to-end observations for this opening, never content.
  timings: { sync: null, request: null },
  lastSentAt: 0,
  pending() {
    const pending = store.get("pending", {});
    const limit = Date.now() - STALE_MS;
    for (const [id, meta] of Object.entries(pending))
      if (this.finished.has(id) || (meta.ts < limit && !meta.ack)) delete pending[id];
    return pending;
  },
  save(pending, reportFailure = true) {
    // A caller may hold an older snapshot while another reply is settled.
    for (const id of Object.keys(pending)) if (this.finished.has(id)) delete pending[id];
    if (store.set("pending", pending, true)) return true;
    // store.set reports failure rather than throwing. Preserve the minimal
    // request metadata, dropping optional resend copies to save space.
    let removed = false;
    for (const meta of Object.values(pending)) { if (meta.p) removed = true; delete meta.p; }
    if (store.set("pending", pending, true)) {
      if (removed) toast(t("storage_resend_limited"), "bad");
      return true;
    }
    if (reportFailure) toast(t("storage_full"), "bad");
    return false;
  },
  // Lowest serial the listener must start from: chunks of an unfinished answer are replayed after a reopen.
  startSerial() {
    const floors = Object.values(store.get("floors", {})).map((item) => item.s);
    return Math.max(0, Math.min(this.serial, ...floors));
  },
  request(op, args = {}, ctx = null) {
    const id = Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
    // The bot rotates this value after every restart. Requests queued under an
    // older session are refused rather than silently repeating provider calls.
    const payload = { t: "req", id, op, args, lang: lang(), session: store.get("botSession", "") };
    const size = new Blob([JSON.stringify({ payload })]).size;
    const reported = Number(window.webxdc.sendUpdateMaxSize) || 128000;
    const maxUpdate = Math.min(UPDATE_MAX, reported);
    const chunkSize = Math.min(REQUEST_CHUNK, Math.floor((maxUpdate - 500) / 4) * 4);
    // Bound the number of stored fragments and do not send an unusable partial request.
    const canSplit = chunkSize >= 1024 && Math.ceil(size * 4 / 3 / chunkSize) <= 12;
    if (size > maxUpdate && (op !== "code" || size > CODE_REQUEST_MAX || !canSplit)) {
      const error = t("request_big");
      toast(error, "bad");
      const promise = Promise.reject(new Error(error));
      promise.catch(() => {});
      return { id: null, promise, error };
    }
    const pending = this.pending();
    pending[id] = { op, ctx, ts: Date.now(), sent: Date.now(), tries: 0, ack: 0, p: size <= RESENDABLE ? payload : null };
    // Never send a potentially chargeable operation if the app cannot retain
    // even its minimal tracking metadata for a later response or reopen.
    if (!this.save(pending, false)) {
      const error = t("storage_request_unsent"), promise = Promise.reject(new Error(error));
      toast(error, "bad");
      promise.catch(() => {});
      return { id: null, promise, error };
    }
    const promise = new Promise((resolve, reject) => this.waiting.set(id, { resolve, reject }));
    promise.catch(() => {});
    if (size > maxUpdate) {
      // Encode UTF-8 first so splitting never divides a Unicode surrogate pair.
      const bytes = new TextEncoder().encode(JSON.stringify(payload));
      let binary = "";
      for (let offset = 0; offset < bytes.length; offset += 8192)
        binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
      const encoded = btoa(binary);
      const count = Math.ceil(encoded.length / chunkSize);
      for (let i = 0; i < count; i += 1) {
        this.lastSentAt = Date.now();
        window.webxdc.sendUpdate({ payload: { t: "req_chunk", id, i, n: count,
          s: encoded.slice(i * chunkSize, (i + 1) * chunkSize) } }, "");
      }
    } else {
      this.lastSentAt = Date.now();
      window.webxdc.sendUpdate({ payload }, "");
    }
    this.changed();
    return { id, promise };
  },
  call(op, args = {}) {
    return this.request(op, args).promise;
  },
  resend(id, pending = this.pending()) {
    const meta = pending[id];
    if (!meta || !meta.p || this.finished.has(id)) return false;
    const payload = meta.p; // save() may remove the optional persisted copy
    meta.tries = (meta.tries || 0) + 1;
    meta.sent = Date.now();
    if (!this.save(pending, false)) { toast(t("storage_retry_unsent"), "bad"); return false; }
    this.lastSentAt = Date.now();
    window.webxdc.sendUpdate({ payload: Object.assign({}, payload, { retry: meta.tries }) }, "");
    this.changed();
    return true;
  },
  forget(id) {
    const pending = this.pending();
    this.finished.add(id);
    delete pending[id];
    this.save(pending);
    const waiter = this.waiting.get(id);
    if (waiter) waiter.reject(Object.assign(new Error(t("cancelled")), { cancelled: true }));
    this.waiting.delete(id);
    this.changed();
  },
  receive(update) {
    const replay = update.serial <= this.serial;
    if (!replay) {
      this.serial = update.serial;
      store.set("serial", this.serial);
    }
    const payload = update.payload;
    if (!payload || typeof payload !== "object") return;
    if (payload.t === "res") this.settle(payload, true);
    else if (payload.t === "chunk") this.piece(payload, update.serial);
    else if (payload.t === "ack") this.acked(payload.re);
    else if (payload.t === "push" && !replay && this.pushes[payload.kind]) this.pushes[payload.kind](payload.data);
  },
  acked(id) {
    const pending = this.pending();
    if (!pending[id]) return;
    pending[id].ack = Date.now();
    this.save(pending);
    this.changed();
  },
  piece(payload, serial) {
    if (!this.pending()[payload.re] && !this.waiting.has(payload.re)) return;
    if (!Number.isInteger(payload.n) || payload.n < 1 || payload.n > 1000 || !Number.isInteger(payload.i)
        || payload.i < 0 || payload.i >= payload.n || typeof payload.s !== "string" || payload.s.length > 45000) return;
    const parts = this.chunks.get(payload.re) || [];
    if (!parts.length && this.pending()[payload.re]) {
      const floors = store.get("floors", {});
      if (!floors[payload.re]) {
        floors[payload.re] = { s: Math.max(0, serial - 1), ts: Date.now() };
        store.set("floors", floors);
      }
    }
    parts[payload.i] = payload.s;
    this.chunks.set(payload.re, parts);
    if (parts.filter((part) => part !== undefined).length === payload.n) {
      this.chunks.delete(payload.re);
      this.dropFloor(payload.re);
      try {
        this.settle(JSON.parse(parts.join("")), true);
      } catch (error) {
        this.settle({ re: payload.re, ok: false, error: t("bad_answer") }, true);
      }
    }
  },
  dropFloor(id) {
    const floors = store.get("floors", {});
    const limit = Date.now() - STALE_MS;
    let changed = false;
    for (const [key, item] of Object.entries(floors)) {
      if (key === id || item.ts < limit || !this.pending()[key]) {
        delete floors[key];
        changed = true;
      }
    }
    if (changed) store.set("floors", floors);
  },
  settle(answer, received = false) {
    const pending = this.pending();
    const meta = pending[answer.re];
    const waiter = this.waiting.get(answer.re);
    if (!meta && !waiter) return;
    const observed = meta && Number(meta.ts) > 0 ? Date.now() - Number(meta.ts) : -1;
    const group = meta && meta.op === "hello" ? "sync" : meta &&
      ["ask", "image", "code", "read", "search", "youtube_search", "music_search", "music_item", "action", "tool", "translate", "weather"].includes(meta.op) ? "request" : "";
    // Timeouts and locally abandoned requests are not responses. Ignore
    // negative or very old intervals, which are not useful measurements.
    if (received && group && Number.isFinite(observed) && observed >= 0 && observed <= STALE_MS)
      this.timings[group] = { op: meta.op, ms: observed, ok: !!answer.ok };
    this.finished.add(answer.re);
    delete pending[answer.re];
    this.save(pending);
    this.chunks.delete(answer.re);
    this.dropFloor(answer.re);
    const error = answer.ok ? "" : plain(answer.error) || t("failed");
    const data = answer.ok ? answer.data : null;
    // Refresh the session but never repeat the uncertain work automatically.
    if (answer.code === "session_changed" && typeof hello === "function") setTimeout(() => hello(true), 0);
    if (data && typeof data === "object" && !Array.isArray(data) && "quota" in data && typeof onQuota === "function") onQuota(data.quota);
    if (meta && this.handlers[meta.op]) this.handlers[meta.op](answer.ok ? answer.data : null, error, meta.ctx, answer.re, received);
    if (waiter) {
      this.waiting.delete(answer.re);
      if (answer.ok) waiter.resolve(answer.data);
      else waiter.reject(new Error(error));
    }
    this.changed();
  },
  changed() {
    this.watchers.forEach((watch) => watch());
  },
  // Every few seconds: resend what got no sign of life, give up on what is clearly lost.
  tick(eager = false) {
    const pending = this.pending();
    const nowMs = Date.now();
    for (const [id, meta] of Object.entries(pending)) {
      const tries = meta.tries || 0;
      if (meta.ack) {
        if (nowMs - meta.ack > ACK_WAIT) this.settle({ re: id, ok: false, error: t("no_answer") });
        else if (nowMs - meta.sent > ACK_WAIT / 2 && tries < RESEND_AFTER.length) this.resend(id, pending);
        continue;
      }
      const wait = RESEND_AFTER[Math.min(tries, RESEND_AFTER.length - 1)] / (eager ? 3 : 1);
      if (meta.p && tries < RESEND_AFTER.length && nowMs - meta.sent > wait) this.resend(id, pending);
      else if (nowMs - meta.ts > ANSWER_MS && (!meta.p || tries >= RESEND_AFTER.length)) this.settle({ re: id, ok: false, error: t("no_answer") });
    }
  },
  // Manual repair: drop half-received answers, resend everything resendable now, refuse the rest.
  repair() {
    this.chunks.clear();
    store.set("floors", {});
    const pending = this.pending();
    for (const [id, meta] of Object.entries(pending)) {
      if (meta.p) this.resend(id, pending);
      else this.settle({ re: id, ok: false, error: t("no_answer") });
    }
    this.changed();
  },
  watchExpiry() {
    this.tick(true);
    setInterval(() => this.tick(), 5000);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") this.tick(true);
    });
    window.addEventListener("focus", () => this.tick(true));
  },
};

// Rich text: fenced code, headings, lists, bold, inline code, links and tap commands, built as DOM nodes.
const INLINE = /(\*\*[^*\n]+\*\*|\*[^*\s](?:[^*\n]*[^*\s])?\*(?![\w*]))|(`[^`\n]+`)|(https?:\/\/[^\s<>"']+[^\s<>"'.,;:!?)\]])|(^|[\s(])(\/[a-zA-Z][\w.@-]*)/g;

function inline(text, hooks) {
  const out = [];
  let last = 0;
  INLINE.lastIndex = 0;
  for (let match = INLINE.exec(text); match; match = INLINE.exec(text)) {
    if (match[1] && match[1][1] !== "*" && /[\w*]/.test(text[match.index - 1] || "")) continue;
    const start = match.index + (match[4] ? match[4].length : 0);
    out.push(text.slice(last, start));
    if (match[1]) out.push(h("strong", {}, match[1].replace(/^\*+|\*+$/g, "")));
    else if (match[2]) out.push(h("code", {}, match[2].slice(1, -1)));
    else if (match[3]) out.push(h("a", { href: "#", class: "link", onclick: (event) => { event.preventDefault(); hooks.link(match[3]); } }, match[3]));
    else out.push(hooks.command(match[5]));
    last = match.index + match[0].length;
  }
  out.push(text.slice(last));
  return out;
}

function rich(text, hooks) {
  const root = h("div", { class: "rich" });
  const blocks = plain(text).split(/```[\w-]*\n?/);
  blocks.forEach((block, index) => {
    if (index % 2 === 1) {
      root.append(h("pre", {}, h("code", {}, block.replace(/\n$/, ""))));
      return;
    }
    let list = null;
    let paragraph = null;
    for (const line of block.split("\n")) {
      const heading = line.match(/^#{1,4}\s+(.*)/);
      const bullet = line.match(/^\s*(?:[-*•·]|\d+[.)])\s+(.*)/);
      if (!line.trim()) {
        list = paragraph = null;
      } else if (heading) {
        list = paragraph = null;
        root.append(h("h4", {}, inline(heading[1], hooks)));
      } else if (bullet) {
        paragraph = null;
        const ordered = /^\s*\d/.test(line);
        if (!list || list.tagName !== (ordered ? "OL" : "UL")) {
          list = h(ordered ? "ol" : "ul");
          root.append(list);
        }
        list.append(h("li", {}, inline(bullet[1], hooks)));
      } else {
        list = null;
        if (paragraph) paragraph.append(h("br"));
        else root.append((paragraph = h("p")));
        append(paragraph, inline(line, hooks));
      }
    }
  });
  return root;
}

// Reader pages arrive as HTML: keep the structure, drop anything active.
const DROP = "script,style,iframe,object,embed,form,input,button,link,meta,base,svg,noscript,template";

function sanitize(html, hooks) {
  const doc = new DOMParser().parseFromString(`<div>${html}</div>`, "text/html");
  const root = doc.body.firstChild;
  root.querySelectorAll(DROP).forEach((node) => node.remove());
  root.querySelectorAll("*").forEach((node) => {
    for (const attribute of [...node.attributes]) {
      const name = attribute.name.toLowerCase();
      const keep = ["href", "src", "alt", "title", "colspan", "rowspan"].includes(name);
      if (!keep || /^\s*javascript:/i.test(attribute.value)) node.removeAttribute(attribute.name);
    }
    if (node.tagName === "IMG" && !/^data:image\//.test(node.getAttribute("src") || "")) node.remove();
  });
  const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) node.nodeValue = node.nodeValue.replace(EMOJI, "");
  const fragment = document.importNode(root, true);
  fragment.className = "page-body";
  fragment.querySelectorAll("a[href]").forEach((anchor) => {
    const href = anchor.getAttribute("href");
    anchor.removeAttribute("href");
    anchor.classList.add("link");
    anchor.addEventListener("click", (event) => {
      event.preventDefault();
      hooks.href(href);
    });
  });
  return fragment;
}

function mailBody(href) {
  const match = /[?&]body=([^&]*)/.exec(href);
  return match ? decodeURIComponent(match[1]) : "";
}
