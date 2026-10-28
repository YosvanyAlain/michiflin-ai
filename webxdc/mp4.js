// MP4 segment joiner: rebuilds one sample table for consecutive stream-copied pieces and reuses their media data as is.
// Works on lazy Blob slices, so even long videos never sit fully in memory. Also used by the Node tests.
"use strict";

const Mp4Join = (() => {
  const text = (bytes, at) => String.fromCharCode(bytes[at], bytes[at + 1], bytes[at + 2], bytes[at + 3]);
  const u32 = (bytes, at) => ((bytes[at] << 24) >>> 0) + (bytes[at + 1] << 16) + (bytes[at + 2] << 8) + bytes[at + 3];
  const u64 = (bytes, at) => u32(bytes, at) * 4294967296 + u32(bytes, at + 4);
  const i32 = (bytes, at) => u32(bytes, at) | 0;

  class JoinError extends Error {
    constructor(code) {
      super(code);
      this.code = code;
    }
  }

  // Children of a box payload: [{ type, start, end, body }] where body is the payload start.
  function children(bytes, start, end) {
    const list = [];
    let at = start;
    while (at + 8 <= end) {
      let size = u32(bytes, at);
      let body = at + 8;
      if (size === 1) {
        size = u64(bytes, at + 8);
        body = at + 16;
      } else if (size === 0) {
        size = end - at;
      }
      if (size < 8 || at + size > end) break;
      list.push({ type: text(bytes, at + 4), start: at, end: at + size, body });
      at += size;
    }
    return list;
  }

  const find = (bytes, box, type) => children(bytes, box.body, box.end).find((child) => child.type === type);
  const findAll = (bytes, box, type) => children(bytes, box.body, box.end).filter((child) => child.type === type);

  // Top-level layout of one file, reading headers only.
  async function layout(source) {
    const boxes = [];
    let at = 0;
    while (at + 8 <= source.size) {
      const head = await source.read(at, 16);
      let size = u32(head, 0);
      let header = 8;
      if (size === 1) {
        size = u64(head, 8);
        header = 16;
      } else if (size === 0) {
        size = source.size - at;
      }
      if (size < 8) throw new JoinError("broken");
      boxes.push({ type: text(head, 4), start: at, size, header });
      at += size;
    }
    if (boxes.some((box) => box.type === "moof")) throw new JoinError("fragmented");
    const moov = boxes.find((box) => box.type === "moov");
    const ftyp = boxes.find((box) => box.type === "ftyp");
    if (!moov || !ftyp) throw new JoinError("not_mp4");
    return { moov, ftyp, mdats: boxes.filter((box) => box.type === "mdat") };
  }

  function track(bytes, trak) {
    const tkhd = find(bytes, trak, "tkhd");
    const mdia = find(bytes, trak, "mdia");
    const mdhd = find(bytes, mdia, "mdhd");
    const hdlr = find(bytes, mdia, "hdlr");
    const minf = find(bytes, mdia, "minf");
    const stbl = find(bytes, minf, "stbl");
    if (!tkhd || !mdhd || !hdlr || !stbl) throw new JoinError("broken");
    const box = (type) => find(bytes, stbl, type);
    const stsd = box("stsd");
    const stts = box("stts");
    const stsc = box("stsc");
    const stsz = box("stsz");
    const stco = box("stco") || box("co64");
    if (box("stz2")) throw new JoinError("unsupported");
    if (!stsd || !stts || !stsc || !stsz || !stco) throw new JoinError("broken");
    const mdhdVersion = bytes[mdhd.body];
    const timescale = u32(bytes, mdhd.body + (mdhdVersion === 1 ? 20 : 12));

    const sizeUniform = u32(bytes, stsz.body + 4);
    const count = u32(bytes, stsz.body + 8);
    const sizes = new Uint32Array(count);
    for (let index = 0; index < count; index += 1) sizes[index] = sizeUniform || u32(bytes, stsz.body + 12 + index * 4);

    const durations = new Uint32Array(count);
    let filled = 0;
    for (let entry = 0, total = u32(bytes, stts.body + 4); entry < total; entry += 1) {
      const run = u32(bytes, stts.body + 8 + entry * 8);
      const delta = u32(bytes, stts.body + 12 + entry * 8);
      for (let step = 0; step < run && filled < count; step += 1) durations[filled++] = delta;
    }

    let offsets = null;
    const ctts = box("ctts");
    if (ctts) {
      offsets = new Int32Array(count);
      const signed = bytes[ctts.body] === 1;
      filled = 0;
      for (let entry = 0, total = u32(bytes, ctts.body + 4); entry < total; entry += 1) {
        const run = u32(bytes, ctts.body + 8 + entry * 8);
        const value = signed ? i32(bytes, ctts.body + 12 + entry * 8) : u32(bytes, ctts.body + 12 + entry * 8);
        for (let step = 0; step < run && filled < count; step += 1) offsets[filled++] = value;
      }
    }

    let sync = null;
    const stss = box("stss");
    if (stss) {
      sync = new Uint8Array(count);
      for (let entry = 0, total = u32(bytes, stss.body + 4); entry < total; entry += 1) sync[u32(bytes, stss.body + 8 + entry * 4) - 1] = 1;
    }

    const wide = stco.type === "co64";
    const chunkCount = u32(bytes, stco.body + 4);
    const chunkAt = (index) => (wide ? u64(bytes, stco.body + 8 + index * 8) : u32(bytes, stco.body + 8 + index * 4));
    const runs = [];
    for (let entry = 0, total = u32(bytes, stsc.body + 4); entry < total; entry += 1) {
      const at = stsc.body + 8 + entry * 12;
      runs.push({ first: u32(bytes, at) - 1, perChunk: u32(bytes, at + 4), description: u32(bytes, at + 8) });
    }
    const positions = new Float64Array(count);
    let sample = 0;
    for (let entry = 0; entry < runs.length; entry += 1) {
      if (runs[entry].description !== 1) throw new JoinError("unsupported");
      const last = entry + 1 < runs.length ? runs[entry + 1].first : chunkCount;
      for (let chunk = runs[entry].first; chunk < last; chunk += 1) {
        let at = chunkAt(chunk);
        for (let step = 0; step < runs[entry].perChunk && sample < count; step += 1) {
          positions[sample] = at;
          at += sizes[sample];
          sample += 1;
        }
      }
    }

    let edit = null;
    const edts = find(bytes, trak, "edts");
    const elst = edts && find(bytes, edts, "elst");
    if (elst) {
      const version = bytes[elst.body];
      const total = u32(bytes, elst.body + 4);
      const width = version === 1 ? 20 : 12;
      for (let entry = 0; entry < total; entry += 1) {
        const at = elst.body + 8 + entry * width;
        const mediaTime = version === 1 ? u64(bytes, at + 8) : i32(bytes, at + 4);
        if (mediaTime !== -1 && mediaTime < 2 ** 53) {
          edit = mediaTime;
          break;
        }
      }
    }

    return {
      handler: text(bytes, hdlr.body + 8), timescale, sizes, durations, offsets, sync, positions, edit,
      stsd: bytes.slice(stsd.start, stsd.end), tkhd: bytes.slice(tkhd.start, tkhd.end), mdhd: bytes.slice(mdhd.start, mdhd.end),
      hdlr: bytes.slice(hdlr.start, hdlr.end),
      media: children(bytes, minf.body, minf.end).filter((child) => child.type !== "stbl").map((child) => bytes.slice(child.start, child.end)),
    };
  }

  async function parse(source) {
    const map = await layout(source);
    const bytes = await source.read(map.moov.start, map.moov.size);
    const moov = { type: "moov", start: 0, end: bytes.length, body: map.moov.header };
    const mvhd = find(bytes, moov, "mvhd");
    if (!mvhd) throw new JoinError("broken");
    const traks = findAll(bytes, moov, "trak").map((trak) => track(bytes, trak));
    if (!traks.length) throw new JoinError("broken");
    const ftyp = await source.read(map.ftyp.start, map.ftyp.size);
    return { source, ftyp, mvhd: bytes.slice(mvhd.start, mvhd.end), traks, mdats: map.mdats };
  }

  // --- Writing -------------------------------------------------------------------------------------------------------------
  class Writer {
    constructor(size) {
      this.bytes = new Uint8Array(size);
      this.at = 0;
    }

    u8(value) {
      this.bytes[this.at++] = value;
    }

    u32(value) {
      this.bytes[this.at++] = (value >>> 24) & 255;
      this.bytes[this.at++] = (value >>> 16) & 255;
      this.bytes[this.at++] = (value >>> 8) & 255;
      this.bytes[this.at++] = value & 255;
    }

    u64(value) {
      this.u32(Math.floor(value / 4294967296));
      this.u32(value % 4294967296);
    }

    raw(bytes) {
      this.bytes.set(bytes, this.at);
      this.at += bytes.length;
    }
  }

  function box(type, ...parts) {
    const size = 8 + parts.reduce((sum, part) => sum + part.length, 0);
    const out = new Writer(size);
    out.u32(size);
    for (let index = 0; index < 4; index += 1) out.u8(type.charCodeAt(index));
    parts.forEach((part) => out.raw(part));
    return out.bytes;
  }

  function full(type, version, flags, fill, size) {
    const out = new Writer(4 + size);
    out.u32(((version & 255) << 24) | (flags & 0xffffff));
    fill(out);
    return box(type, out.bytes);
  }

  function runLength(values, count) {
    const runs = [];
    for (let index = 0; index < count; index += 1) {
      const last = runs[runs.length - 1];
      if (last && last[1] === values[index]) last[0] += 1;
      else runs.push([1, values[index]]);
    }
    return runs;
  }

  function withDuration(original, duration, durationAt32, durationAt64) {
    const copy = original.slice();
    const view = new Writer(0);
    view.bytes = copy;
    const version = copy[8];
    view.at = version === 1 ? durationAt64 : durationAt32;
    if (version === 1) view.u64(duration);
    else view.u32(Math.min(duration, 0xffffffff));
    return copy;
  }

  function sampleTable(merged, base) {
    const count = merged.sizes.length;
    const stts = runLength(merged.durations, count);
    const parts = [merged.stsd,
      full("stts", 0, 0, (out) => { out.u32(stts.length); stts.forEach(([run, value]) => { out.u32(run); out.u32(value); }); }, 4 + stts.length * 8)];
    if (merged.offsets) {
      const ctts = runLength(merged.offsets, count);
      const signed = merged.offsets.some((value) => value < 0);
      parts.push(full("ctts", signed ? 1 : 0, 0, (out) => {
        out.u32(ctts.length);
        ctts.forEach(([run, value]) => { out.u32(run); out.u32(value >>> 0); });
      }, 4 + ctts.length * 8));
    }
    if (merged.sync) {
      const keys = [];
      for (let index = 0; index < count; index += 1) if (merged.sync[index]) keys.push(index + 1);
      parts.push(full("stss", 0, 0, (out) => { out.u32(keys.length); keys.forEach((key) => out.u32(key)); }, 4 + keys.length * 4));
    }
    // Consecutive samples that stay contiguous in the output form one chunk.
    const chunks = [];
    const perChunk = [];
    for (let index = 0; index < count; index += 1) {
      const position = merged.positions[index] + base;
      if (index && merged.positions[index] === merged.positions[index - 1] + merged.sizes[index - 1]) perChunk[perChunk.length - 1] += 1;
      else {
        chunks.push(position);
        perChunk.push(1);
      }
    }
    const stsc = [];
    perChunk.forEach((samples, index) => {
      if (!stsc.length || stsc[stsc.length - 1][1] !== samples) stsc.push([index + 1, samples]);
    });
    parts.push(full("stsc", 0, 0, (out) => { out.u32(stsc.length); stsc.forEach(([first, samples]) => { out.u32(first); out.u32(samples); out.u32(1); }); }, 4 + stsc.length * 12));
    parts.push(full("stsz", 0, 0, (out) => { out.u32(0); out.u32(count); merged.sizes.forEach((size) => out.u32(size)); }, 8 + count * 4));
    parts.push(full("co64", 0, 0, (out) => { out.u32(chunks.length); chunks.forEach((position) => out.u64(position)); }, 4 + chunks.length * 8));
    return box("stbl", ...parts);
  }

  // Codec setup without the bitrate hints that differ from piece to piece (btrt box, esds max and average bitrate).
  function essence(stsd) {
    const copy = stsd.slice();
    for (let at = 4; at + 4 <= copy.length; at += 1) {
      const type = text(copy, at);
      if (type === "btrt") copy.fill(0, at + 4, Math.min(copy.length, at + 16));
      if (type === "esds") {
        for (let scan = at + 8; scan < Math.min(copy.length, at + 64); scan += 1) {
          if (copy[scan] !== 4) continue;
          let content = scan + 1;
          while (content < copy.length && copy[content] & 128 && content - scan < 4) content += 1;
          content += 1;
          copy.fill(0, content + 2, Math.min(copy.length, content + 13));
          break;
        }
      }
    }
    return copy;
  }

  function equal(first, second) {
    if (first.length !== second.length) return false;
    for (let index = 0; index < first.length; index += 1) if (first[index] !== second[index]) return false;
    return true;
  }

  // Pieces in order -> { parts, size, duration } where parts are Uint8Array headers and lazy source ranges.
  async function join(sources) {
    if (sources.length < 2) throw new JoinError("few");
    const files = [];
    for (const source of sources) files.push(await parse(source));
    const first = files[0];
    for (const file of files.slice(1)) {
      if (file.traks.length !== first.traks.length) throw new JoinError("mismatch");
      file.traks.forEach((trak, index) => {
        const model = first.traks[index];
        if (trak.handler !== model.handler || trak.timescale !== model.timescale || !equal(essence(trak.stsd), essence(model.stsd))) throw new JoinError("mismatch");
      });
    }

    // Media data ranges in output order; a sample position maps through the range that holds it.
    const ranges = [];
    let payload = 0;
    files.forEach((file, fileIndex) => file.mdats.forEach((mdat) => {
      const start = mdat.start + mdat.header;
      const end = mdat.start + mdat.size;
      ranges.push({ fileIndex, start, end, out: payload });
      payload += end - start;
    }));
    const place = (fileIndex, position) => {
      const range = ranges.find((item) => item.fileIndex === fileIndex && position >= item.start && position < item.end);
      if (!range) throw new JoinError("broken");
      return range.out + position - range.start;
    };

    const movieScale = u32(first.mvhd, first.mvhd[8] === 1 ? 28 : 20);
    const merged = first.traks.map((model, index) => {
      const parts = files.map((file) => file.traks[index]);
      const count = parts.reduce((sum, part) => sum + part.sizes.length, 0);
      const out = {
        ...model, sizes: new Uint32Array(count), durations: new Uint32Array(count), positions: new Float64Array(count),
        offsets: parts.some((part) => part.offsets) ? new Int32Array(count) : null,
        sync: parts.some((part) => part.sync) ? new Uint8Array(count) : null,
      };
      let at = 0;
      parts.forEach((part, fileIndex) => {
        out.sizes.set(part.sizes, at);
        out.durations.set(part.durations, at);
        if (out.offsets && part.offsets) out.offsets.set(part.offsets, at);
        if (out.sync) {
          if (part.sync) out.sync.set(part.sync, at);
          else out.sync.fill(1, at, at + part.sizes.length);
        }
        for (let sample = 0; sample < part.sizes.length; sample += 1) out.positions[at + sample] = place(fileIndex, part.positions[sample]);
        at += part.sizes.length;
      });
      out.mediaDuration = out.durations.reduce((sum, value) => sum + value, 0);
      // The visible span runs from the edit point to the latest composition end (decode time + offset + duration).
      let clock = 0;
      let last = 0;
      for (let sample = 0; sample < count; sample += 1) {
        last = Math.max(last, clock + (out.offsets ? out.offsets[sample] : 0) + out.durations[sample]);
        clock += out.durations[sample];
      }
      out.movieDuration = Math.round(((last - (model.edit || 0)) * movieScale) / model.timescale);
      return out;
    });
    const movieDuration = Math.max(...merged.map((trak) => trak.movieDuration));

    const build = (base) => {
      const traks = merged.map((trak) => {
        const tkhd = withDuration(trak.tkhd, trak.movieDuration, 28, 36);
        const mdhd = withDuration(trak.mdhd, trak.mediaDuration, 24, 32);
        const edts = trak.edit ? [box("edts", full("elst", 1, 0, (out) => { out.u32(1); out.u64(trak.movieDuration); out.u64(trak.edit); out.u32(0x00010000); }, 24))] : [];
        return box("trak", tkhd, ...edts, box("mdia", mdhd, trak.hdlr, box("minf", ...trak.media, sampleTable(trak, base))));
      });
      return box("moov", withDuration(first.mvhd, movieDuration, 24, 32), ...traks);
    };
    const wide = payload + 16 > 0xffffffff;
    const header = wide ? 16 : 8;
    const probe = build(0);
    const base = first.ftyp.length + probe.length + header;
    const moov = build(base);
    const mdatHead = new Writer(header);
    if (wide) {
      mdatHead.u32(1);
      [..."mdat"].forEach((char) => mdatHead.u8(char.charCodeAt(0)));
      mdatHead.u64(payload + 16);
    } else {
      mdatHead.u32(payload + 8);
      [..."mdat"].forEach((char) => mdatHead.u8(char.charCodeAt(0)));
    }
    const parts = [first.ftyp, moov, mdatHead.bytes, ...ranges.map((range) => ({ source: files[range.fileIndex].source, start: range.start, end: range.end }))];
    return { parts, size: base + payload, duration: movieDuration / movieScale };
  }

  // Part number embedded by the bot ("michiflin:<job>:<i>/<n>[:<encoded name>]"), found near the start of a file.
  async function marker(source) {
    const bytes = await source.read(0, Math.min(source.size, 512 * 1024));
    let found = null;
    for (let at = 0; at + 10 < bytes.length && !found; at += 1) {
      if (bytes[at] === 109 && text(bytes, at) === "mich" && text(bytes, at + 4) === "ifli" && bytes[at + 8] === 110 && bytes[at + 9] === 58) {
        let end = at + 10;
        while (end < bytes.length && end - at < 220 && /[\w:/%.~-]/.test(String.fromCharCode(bytes[end]))) end += 1;
        const match = String.fromCharCode(...bytes.slice(at, end)).match(/^michiflin:(\d+):(\d+)\/(\d+)(?::([\w%.~-]+))?/);
        if (match) {
          let name = "";
          try { name = match[4] ? decodeURIComponent(match[4]) : ""; } catch (error) { name = ""; }
          found = { job: Number(match[1]), index: Number(match[2]), total: Number(match[3]), name };
        }
      }
    }
    return found;
  }

  // MP3 pieces join byte by byte once the tag block at the start of each later piece is dropped.
  async function id3Length(source) {
    const head = await source.read(0, 10);
    if (head.length < 10 || head[0] !== 73 || head[1] !== 68 || head[2] !== 51) return 0;
    return 10 + ((head[6] & 127) << 21) + ((head[7] & 127) << 14) + ((head[8] & 127) << 7) + (head[9] & 127) + (head[5] & 16 ? 10 : 0);
  }

  async function joinMp3(sources) {
    const parts = [];
    let size = 0;
    for (const [index, source] of sources.entries()) {
      const start = index ? await id3Length(source) : 0;
      parts.push({ source, start, end: source.size });
      size += source.size - start;
    }
    return { parts, size, duration: 0 };
  }

  return { join, joinMp3, marker, JoinError };
})();

if (typeof module !== "undefined") module.exports = Mp4Join;
