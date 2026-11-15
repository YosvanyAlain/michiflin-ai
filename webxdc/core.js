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
  cat: '<path d="M5 9.5 6.5 3l4 3.6a6 6 0 0 1 3 0L17.5 3 19 9.5c1 4.5-1.6 10.5-7 10.5S4 14 5 9.5z"/><circle cx="9.5" cy="12" r="1.6"/><circle cx="14.5" cy="12" r="1.6"/>',
  catpixel: '<path fill="#ebfbf4" stroke="none" shape-rendering="crispEdges" d="M4 2h3v2h2v2h6V4h2V2h3v6h1v8h-2v2h-2v2H7v-2H5v-2H3V8h1z"/><path fill="#0a6257" stroke="none" shape-rendering="crispEdges" d="M6 6h2v2h8V6h2v4h1v5h-2v2H7v-2H5v-5h1z"/><path fill="#f3ba42" stroke="none" shape-rendering="crispEdges" d="M7 11h2v2H7zm8 0h2v2h-2z"/>',
  notes: '<rect x="4" y="3" width="16" height="19" rx="3"/><path d="M8 2v3M16 2v3M8 10h8M8 14h8M8 18h5"/>',
  links: '<path d="M10.5 13.5a4.5 4.5 0 0 0 6.4 0l2.6-2.6a4.5 4.5 0 0 0-6.4-6.4l-1.5 1.5"/><path d="M13.5 10.5a4.5 4.5 0 0 0-6.4 0l-2.6 2.6a4.5 4.5 0 0 0 6.4 6.4l1.5-1.5"/>',
  nauta: '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="m4 7 8 6 8-6M4 17l5-4M20 17l-5-4"/>',
  killswitch: '<path d="M12 3v9"/><path d="M7.1 5.8a8 8 0 1 0 9.8 0"/><circle cx="12" cy="12" r="1"/>',
  status: '<circle cx="12" cy="12" r="9"/><path d="M5 12h3l2-4 3 8 2-4h4"/>',
};

// Pixel variants for the interface icons. The multicolour cat is already
// pixel art. Both paths stay in the SVG so appearance changes are immediate
// and work offline.
const PIXEL_ICONS = {
  home: '<path d="M3 10h2V8h2V6h2V4h6v2h2v2h2v2h2M5 10v10h5v-6h4v6h5V10"/>',
  ai: '<path d="M10 3h4v3h3v3h3v4h-3v3h-3v3h-4v-3H7v-3H4V9h3V6h3z"/><path d="M19 17h2v2h-2z"/>',
  web: '<path d="M8 3h8v2h3v3h2v8h-2v3h-3v2H8v-2H5v-3H3V8h2V5h3zM3 12h18M10 4 8 8v8l2 4M14 4l2 4v8l-2 4"/>',
  apps: '<path d="M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z"/>',
  downloads: '<path d="M10 3h4v9h4v2h-2v2h-2v2h-4v-2H8v-2H6v-2h4zM4 20h16"/>',
  settings: '<path d="M9 3h6v2h3v2h2v3h2v4h-2v3h-2v2h-3v2H9v-2H6v-2H4v-3H2v-4h2V7h2V5h3zM9 9h6v6H9z"/>',
  back: '<path d="M15 4h-3v3H9v3H6v4h3v3h3v3h3M7 12h12"/>',
  next: '<path d="M9 4h3v3h3v3h3v4h-3v3h-3v3H9M17 12H5"/>',
  search: '<path d="M7 3h7v2h3v3h1v6h-1v3h-3v1H7v-1H4v-3H3V8h1V5h3zM17 17h2v2h2v2"/>',
  image: '<path d="M3 3h18v18H3zM6 17h2v-2h2v-2h3v2h2v2h2v2M8 7h3v3H8z"/>',
  youtube: '<path d="M4 5h16v2h2v10h-2v2H4v-2H2V7h2zM10 8v8h2v-1h2v-2h2v-2h-2V9h-2V8z"/>',
  audio: '<path d="M10 5h3V4h7v12h-3v-2h-4V8h4M10 5v13H8v2H4v-4h6"/>',
  news: '<path d="M4 3h15v15h2v3H6v-2H4zM8 7h7v3H8zM8 13h7M8 17h7"/>',
  rates: '<path d="M4 4h8v2h2v7h-2v2H4v-2H2V6h2zM10 16h2v2h2v2h7v-2h1v-7h-5M7 7v5M5 9h4"/>',
  weather: '<path d="M5 11V9h2V7h3V5h5v2h2v3h2v2h2v5h-2v2H5v-2H3v-4h2z"/>',
  translate: '<path d="M3 5h10M8 3v2M5 8h2v2h2v2h2v2h2M12 8h-2v2H8v2H6v2H4M14 20l3-9h2l3 9M15 17h6"/>',
  tools: '<path d="M5 3h14v18H5zM8 6h8v3H8zM8 12h1M12 12h1M16 12h1M8 16h1M12 16h1M16 16h1"/>',
  chat: '<path d="M5 4h14v2h2v10h-2v2h-8v2H6v2H3v-4h2v-2H3V6h2z"/>',
  file: '<path d="M5 3h9v2h2v2h2v2h2v12H5zM14 3v6h6"/>',
  folder: '<path d="M3 5h7v2h11v13H3z"/>',
  health: '<path d="M3 12h4v-2h2V6h2v12h2v-4h2v-2h6"/>',
  notes: '<path d="M4 4h16v17H4zM8 2v4M16 2v4M8 10h8M8 14h8M8 18h5"/>',
  bolt: '<path d="M12 3h4v5h4v3h-3v3h-3v3h-3v3H8v-7H4v-3h3V7h3V4h2z"/>',
  bell: '<path d="M9 4h6v2h3v8h2v3H4v-3h2V6h3zM10 20h4"/>',
  clock: '<path d="M8 3h8v2h3v3h2v8h-2v3h-3v2H8v-2H5v-3H3V8h2V5h3zM12 7v5h4"/>',
  close: '<path d="M5 5h2v2h2v2h2v2h2V9h2V7h2V5h2M5 19h2v-2h2v-2h2v-2h2v2h2v2h2v2h2"/>',
  refresh: '<path d="M5 10V7h2V5h9v2h2v3h3V4M19 14v3h-2v2H8v-2H6v-3H3v6"/>',
  send: '<path d="M3 10h3V8h4V6h4V4h4V2h3v6h-2v4h-2v4h-2v4h-3v2h-2v-7H7v-2H3zM10 15l8-9"/>',
  cat: '<path d="M4 4h3v2h3v2h4V6h3V4h3v6h1v6h-2v3h-3v2H8v-2H5v-3H3v-6h1zM7 11h3v3H7zM14 11h3v3h-3z"/>',
  alert: '<path d="M8 3h8v2h3v3h2v8h-2v3h-3v2H8v-2H5v-3H3V8h2V5h3zM12 7v6M12 17h1"/>',
  ban: '<path d="M8 3h8v2h3v3h2v8h-2v3h-3v2H8v-2H5v-3H3V8h2V5h3zM6 6l12 12"/>',
  book: '<path d="M3 4h7v2h2v14h-2v-2H3zM21 4h-7v2h-2v14h2v-2h7z"/>',
  camera: '<path d="M3 7h5V5h2V3h4v2h2v2h5v13H3zM9 10h6v6H9z"/>',
  check: '<path d="M4 12h3v3h3v2h2v-3h2v-2h2v-2h2V8h2V6h1"/>',
  clip: '<path d="M16 6V4H8v2H6v12h2v2h8v-2h2V8h-2v9H9V8h4v7"/>',
  cloudfog: '<path d="M5 11V9h2V7h3V5h5v2h2v3h2v2h2v2H3v-3zM5 17h14M7 20h12"/>',
  cloudrain: '<path d="M5 10V8h2V6h3V4h5v2h2v3h2v2h2v3H3v-4zM7 17v3M12 17v3M17 17v3"/>',
  cloudsnow: '<path d="M5 10V8h2V6h3V4h5v2h2v3h2v2h2v3H3v-4zM7 17v4M5 19h4M16 17v4M14 19h4"/>',
  cloudstorm: '<path d="M5 10V8h2V6h3V4h5v2h2v3h2v2h2v3H3v-4zM12 15h3l-2 3h2l-4 4v-3H9z"/>',
  code: '<path d="M8 5H6v2H4v2H2v6h2v2h2v2h2M16 5h2v2h2v2h2v6h-2v2h-2v2h-2M14 3l-4 18"/>',
  copy: '<path d="M8 8h12v13H8zM5 16H3V3h12v2"/>',
  down: '<path d="M5 9h2v2h2v2h2v2h2v-2h2v-2h2V9h2"/>',
  drop: '<path d="M11 3h2v3h2v3h2v3h2v5h-2v3H7v-3H5v-5h2V9h2V6h2z"/>',
  help: '<path d="M8 3h8v2h3v3h2v8h-2v3h-3v2H8v-2H5v-3H3V8h2V5h3zM9 8V7h6v2h-2v2h-1v2M12 17h1"/>',
  info: '<path d="M8 3h8v2h3v3h2v8h-2v3h-3v2H8v-2H5v-3H3V8h2V5h3zM12 11v6M12 7h1"/>',
  join: '<path d="M12 3 3 7l9 4 9-4zM3 12l9 4 9-4M3 17l9 4 9-4"/>',
  killswitch: '<path d="M11 3h2v9h-2zM7 5H5v2H3v10h2v2h3v2h8v-2h3v-2h2V7h-2V5h-2"/>',
  link: '<path d="M10 14h4v-2h2v-2h2V8h-2V6h-4v2h-2M14 10h-4v2H8v2H6v2h2v2h4v-2h2"/>',
  links: '<path d="M10 14h4v-2h2v-2h2V8h-2V6h-4v2h-2M14 10h-4v2H8v2H6v2h2v2h4v-2h2"/>',
  moon: '<path d="M15 3h-4v2H8v2H6v3H4v6h2v3h3v2h7v-2h3v-2h2v-3h-5v-2h-2V8h1z"/>',
  more: '<path d="M4 11h2v2H4zM11 11h2v2h-2zM18 11h2v2h-2z"/>',
  nauta: '<path d="M3 5h18v14H3zM4 7h2v2h2v2h2v2h4v-2h2V9h2V7h2"/>',
  open: '<path d="M13 4h7v7M20 4l-9 9M18 13v7H4V6h7"/>',
  pause: '<path d="M6 4h4v16H6zM14 4h4v16h-4z"/>',
  pdf: '<path d="M5 3h9v2h2v2h2v2h2v12H5zM14 3v6h6M8 13h8M8 17h6"/>',
  pin: '<path d="M9 3h6v2h2v5h2v2H5v-2h2V5h2zM12 12v9"/>',
  play: '<path d="M7 4h3v2h3v2h3v2h3v4h-3v2h-3v2h-3v2H7z"/>',
  plus: '<path d="M11 4h2v7h7v2h-7v7h-2v-7H4v-2h7z"/>',
  share: '<path d="M4 12v8h16v-8M8 7h2V5h2V3h2v2h2v2M13 4v12"/>',
  shield: '<path d="M11 3h2v2h4v2h3v7h-2v3h-2v2h-3v2h-2v-2H8v-2H6v-3H4V7h3V5h4z"/>',
  status: '<path d="M8 3h8v2h3v3h2v8h-2v3h-3v2H8v-2H5v-3H3V8h2V5h3zM5 12h3v-2h2V8h2v8h2v-4h5"/>',
  subtitles: '<path d="M3 5h18v14H3zM6 9h5M6 13h5M13 9h5M13 13h5"/>',
  sun: '<path d="M9 8h6v2h2v4h-2v2H9v-2H7v-4h2zM12 2v3M12 19v3M2 12h3M19 12h3M4 4l2 2M18 18l2 2M20 4l-2 2M6 18l-2 2"/>',
  swap: '<path d="M4 7h15M16 4h3v3h2v2h-2v2h-3M20 17H5M8 14H5v3H3v2h2v2h3"/>',
  trash: '<path d="M5 6h14M9 6V3h6v3M7 7v13h10V7M10 10v7M14 10v7"/>',
  up: '<path d="M5 15h2v-2h2v-2h2V9h2v2h2v2h2v2h2"/>',
  user: '<path d="M9 3h6v2h2v5h-2v2H9v-2H7V5h2zM6 20v-3h2v-2h8v2h2v3z"/>',
  video: '<path d="M3 3h18v18H3zM7 3v18M17 3v18M3 8h4M3 16h4M17 8h4M17 16h4"/>',
  wind: '<path d="M3 7h12V5h3v2h-2M2 12h17v2h-2M3 17h9v2h-2"/>',
};

function icon(name, extra = "") {
  const holder = document.createElement("span");
  const key = Object.prototype.hasOwnProperty.call(ICONS, name) ? name : "info";
  const normal = ICONS[key], pixel = PIXEL_ICONS[key];
  const art = pixel ? `<g class="icon-normal">${normal}</g><g class="icon-pixel">${pixel}</g>` : normal;
  holder.innerHTML = `<svg class="ic ${extra}" viewBox="0 0 24 24" aria-hidden="true">${art}</svg>`;
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
  document.documentElement.dataset.theme = ["system", "light", "dark"].includes(prefs.theme) ? prefs.theme : "system";
  document.documentElement.dataset.style = ["michiflin", "material", "pixel", "ocean"].includes(prefs.style) ? prefs.style : "michiflin";
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
function closeSheet() {
  const sheet = $("#sheet"), revision = ++sheetRevision;
  sheet.classList.remove("open");
  setTimeout(() => {
    // A newer sheet may already have replaced this one during its animation.
    if (revision !== sheetRevision) return;
    sheet.hidden = true;
    fill(sheet);
    // Only acknowledge the current result after the sheet has really closed.
    if (typeof revealVisibleNotices === "function") revealVisibleNotices();
    else if (typeof markVisibleNoticesRead === "function") markVisibleNoticesRead();
  }, 220);
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("#sheet").hidden) closeSheet();
});

function openSheet(title, body) {
  const sheet = $("#sheet"), revision = ++sheetRevision;
  const panel = h("div", { class: "sheet-panel", role: "dialog" },
    h("div", { class: "sheet-head" }, h("h2", {}, title), h("button", { class: "icon-btn", "aria-label": t("close"), onclick: closeSheet }, icon("close"))),
    h("div", { class: "sheet-body" }, body));
  fill(sheet, h("div", { class: "sheet-scrim", onclick: closeSheet }), panel);
  sheet.hidden = false;
  requestAnimationFrame(() => { if (revision === sheetRevision) sheet.classList.add("open"); });
  return panel;
}

function confirmBox(title, text, okLabel) {
  return new Promise((resolve) => {
    const done = (value) => {
      closeSheet();
      resolve(value);
    };
    openSheet(title, [
      h("p", { class: "muted" }, text),
      h("div", { class: "row end" },
        h("button", { class: "btn ghost", onclick: () => done(false) }, t("cancel")),
        h("button", { class: "btn danger", onclick: () => done(true) }, okLabel || t("confirm"))),
    ]);
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
