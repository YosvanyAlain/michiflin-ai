"use strict";
// This worker never receives app storage or network URLs. Repack untrusted ZIP
// entries only after bounded streaming inflation and CRC verification. EPUB.js
// consumes the repacked *preview*; the untouched original stays in IndexedDB.
importScripts("vendor/books/fflate.min.js");

self.onmessage = event => {
  try {
    const input = new Uint8Array(event.data), view = new DataView(input.buffer, input.byteOffset, input.byteLength);
    const maxSize = 24000000, maxFiles = 3000;
    const fail = code => { throw new Error(code); };
    const bounds = (offset, length) => {
      if (!Number.isSafeInteger(offset) || !Number.isSafeInteger(length) || offset < 0 || length < 0 || offset + length > input.length) fail("structure");
    };
    const u16 = p => { bounds(p, 2); return view.getUint16(p, true); };
    const u32 = p => { bounds(p, 4); return view.getUint32(p, true); };
    if (input.length < 22 || input.length > 8000000) fail("size");
    let end = -1;
    for (let p = input.length - 22, first = Math.max(0, input.length - 65557); p >= first; p--) {
      if (u32(p) === 0x06054b50 && p + 22 + u16(p + 20) === input.length) { end = p; break; }
    }
    if (end < 0 || u16(end + 4) !== 0 || u16(end + 6) !== 0 || u16(end + 8) !== u16(end + 10)) fail("structure");
    const count = u16(end + 10), centralSize = u32(end + 12), centralStart = u32(end + 16);
    if (!count || count > maxFiles || count === 0xffff || centralStart + centralSize !== end) fail("structure");
    const entries = [], seen = new Set(), ranges = [];
    let cursor = centralStart, total = 0;
    for (let i = 0; i < count; i++) {
      bounds(cursor, 46);
      if (u32(cursor) !== 0x02014b50) fail("structure");
      const flags = u16(cursor + 8), method = u16(cursor + 10), crc = u32(cursor + 16), compressed = u32(cursor + 20),
        expanded = u32(cursor + 24), nameLen = u16(cursor + 28), extraLen = u16(cursor + 30), commentLen = u16(cursor + 32),
        disk = u16(cursor + 34), local = u32(cursor + 42);
      bounds(cursor + 46, nameLen + extraLen + commentLen);
      if (!nameLen || nameLen > 1000 || disk || compressed === 0xffffffff || expanded === 0xffffffff || local === 0xffffffff ||
          ![0, 8].includes(method)) fail("structure");
      if (flags & 0x41) fail("protected");
      const nameBytes = input.subarray(cursor + 46, cursor + 46 + nameLen);
      const name = fflate.strFromU8(nameBytes, !(flags & 0x800));
      const parts = name.split("/");
      if (name.startsWith("/") || /[\\\u0000-\u001f]/.test(name) ||
          parts.some(part => [".", "..", "__proto__", "constructor", "prototype"].includes(part)) || seen.has(name)) fail("structure");
      if (name.toLowerCase() === "meta-inf/encryption.xml") fail("protected");
      seen.add(name); total += expanded;
      if (total > maxSize || expanded > maxSize || compressed > input.length) fail("size");
      bounds(local, 30);
      if (u32(local) !== 0x04034b50 || u16(local + 6) !== flags || u16(local + 8) !== method || u16(local + 26) !== nameLen) fail("structure");
      const localExtra = u16(local + 28), data = local + 30 + nameLen + localExtra;
      bounds(local + 30, nameLen + localExtra);
      for (let j = 0; j < nameLen; j++) if (input[local + 30 + j] !== nameBytes[j]) fail("structure");
      if (!(flags & 8) && (u32(local + 14) !== crc || u32(local + 18) !== compressed || u32(local + 22) !== expanded)) fail("structure");
      if (data + compressed > centralStart) fail("structure");
      ranges.push({ start: local, end: data + compressed });
      entries.push({ name, method, data, compressed, expanded, crc });
      cursor += 46 + nameLen + extraLen + commentLen;
    }
    if (cursor !== end || !seen.has("META-INF/container.xml")) fail("structure");
    ranges.sort((a, b) => a.start - b.start);
    for (let i = 1; i < ranges.length; i++) if (ranges[i - 1].end > ranges[i].start) fail("structure");

    const table = new Uint32Array(256);
    for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; table[n] = c >>> 0; }
    const files = Object.create(null);
    let inflatedTotal = 0;
    for (const entry of entries) {
      let length = 0, checksum = 0xffffffff, finished = false;
      const chunks = [];
      const receive = (part, final) => {
        length += part.length; inflatedTotal += part.length;
        if (length > entry.expanded || inflatedTotal > maxSize) fail("size");
        for (let j = 0; j < part.length; j++) checksum = table[(checksum ^ part[j]) & 255] ^ (checksum >>> 8);
        chunks.push(part); if (final) finished = true;
      };
      if (entry.method === 0) {
        if (entry.compressed !== entry.expanded) fail("structure");
        receive(input.slice(entry.data, entry.data + entry.compressed), true);
      } else {
        if (!entry.compressed) fail("structure");
        const inflater = new fflate.Inflate(receive);
        for (let p = 0; p < entry.compressed; p += 4096) {
          const next = Math.min(p + 4096, entry.compressed);
          inflater.push(input.subarray(entry.data + p, entry.data + next), next === entry.compressed);
        }
      }
      if (!finished || length !== entry.expanded || ((checksum ^ 0xffffffff) >>> 0) !== entry.crc) fail("structure");
      const content = new Uint8Array(length); let at = 0;
      for (const chunk of chunks) { content.set(chunk, at); at += chunk.length; }
      files[entry.name] = content;
    }
    // STORE rather than recompress: all output sizes and CRCs now describe the
    // real extracted bytes, so JSZip cannot encounter a forged size or bomb.
    const safe = fflate.zipSync(files, { level: 0 });
    self.postMessage({ ok: true, buffer: safe.buffer }, [safe.buffer]);
  } catch (error) {
    const code = ["protected", "size", "structure"].includes(error && error.message) ? error.message : "structure";
    self.postMessage({ ok: false, code });
  }
};
