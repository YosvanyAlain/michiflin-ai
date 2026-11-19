"use strict";
/* Offline QR encoder and SVG renderer.
 *
 * Delta Chat can hand back its own QR image, but it arrives with its own
 * styling baked in. Drawing it here instead gives the panel a code that
 * matches the rest of the interface: rounded modules, a soft gradient and
 * carved finder eyes, with a quiet zone wide enough for any scanner.
 *
 * Byte mode only, which is what invitation links need. Error correction
 * defaults to Q so the centre badge can cover part of the code safely.
 */

const QR_EC = { L: 0, M: 1, Q: 2, H: 3 };

// Total codewords per version (1-40), index 0 unused.
const QR_TOTAL = [0, 26, 44, 70, 100, 134, 172, 196, 242, 292, 346, 404, 466, 532, 581, 655,
  733, 815, 901, 991, 1085, 1156, 1258, 1364, 1474, 1588, 1706, 1828, 1921, 2051, 2185,
  2323, 2465, 2611, 2761, 2876, 3034, 3196, 3362, 3532, 3706];

// [ec codewords per block, group1 blocks, group2 blocks] for L,M,Q,H per version.
const QR_ECB = [null,
  [[7,1,0],[10,1,0],[13,1,0],[17,1,0]], [[10,1,0],[16,1,0],[22,1,0],[28,1,0]],
  [[15,1,0],[26,1,0],[18,2,0],[22,2,0]], [[20,1,0],[18,2,0],[26,2,0],[16,4,0]],
  [[26,1,0],[24,2,0],[18,2,2],[22,2,2]], [[18,2,0],[16,4,0],[24,4,0],[28,4,0]],
  [[20,2,0],[18,4,0],[18,2,4],[26,4,1]], [[24,2,0],[22,2,2],[22,4,2],[26,4,2]],
  [[30,2,0],[22,3,2],[20,4,4],[24,4,4]], [[18,2,2],[26,4,1],[24,6,2],[28,6,2]],
  [[20,4,0],[30,1,4],[28,4,4],[24,3,8]], [[24,2,2],[22,6,2],[26,4,6],[28,7,4]],
  [[26,4,0],[22,8,1],[24,8,4],[22,12,4]], [[30,3,1],[24,4,5],[20,11,5],[24,11,5]],
  [[22,5,1],[24,5,5],[30,5,7],[24,11,7]], [[24,5,1],[28,7,3],[24,15,2],[30,3,13]],
  [[28,1,5],[28,10,1],[28,1,15],[28,2,17]], [[30,5,1],[26,9,4],[28,17,1],[28,2,19]],
  [[28,3,4],[26,3,11],[26,17,4],[26,9,16]], [[28,3,5],[26,3,13],[30,15,5],[28,15,10]],
  [[28,4,4],[26,17,0],[28,17,6],[30,19,6]], [[28,2,7],[28,17,0],[30,7,16],[24,34,0]],
  [[30,4,5],[28,4,14],[30,11,14],[30,16,14]], [[30,6,4],[28,6,14],[30,11,16],[30,30,2]],
  [[26,8,4],[28,8,13],[30,7,22],[30,22,13]], [[28,10,2],[28,19,4],[28,28,6],[30,33,4]],
  [[30,8,4],[28,22,3],[30,8,26],[30,12,28]], [[30,3,10],[28,3,23],[30,4,31],[30,11,31]],
  [[30,7,7],[28,21,7],[30,1,37],[30,19,26]], [[30,5,10],[28,19,10],[30,15,25],[30,23,25]],
  [[30,13,3],[28,2,29],[30,42,1],[30,23,28]], [[30,17,0],[28,10,23],[30,10,35],[30,19,35]],
  [[30,17,1],[28,14,21],[30,29,19],[30,11,46]], [[30,13,6],[28,14,23],[30,44,7],[30,59,1]],
  [[30,12,7],[28,12,26],[30,39,14],[30,22,41]], [[30,6,14],[28,6,34],[30,46,10],[30,2,64]],
  [[30,17,4],[28,29,14],[30,49,10],[30,24,46]], [[30,4,18],[28,13,32],[30,48,14],[30,42,32]],
  [[30,20,4],[28,40,7],[30,43,22],[30,10,67]], [[30,19,6],[28,18,31],[30,34,34],[30,20,61]]];

const QR_ALIGN = [null, [], [6,18], [6,22], [6,26], [6,30], [6,34], [6,22,38], [6,24,42],
  [6,26,46], [6,28,50], [6,30,54], [6,32,58], [6,34,62], [6,26,46,66], [6,26,48,70],
  [6,26,50,74], [6,30,54,78], [6,30,56,82], [6,30,58,86], [6,34,62,90], [6,28,50,72,94],
  [6,26,50,74,98], [6,30,54,78,102], [6,28,54,80,106], [6,32,58,84,110], [6,30,58,86,114],
  [6,34,62,90,118], [6,26,50,74,98,122], [6,30,54,78,102,126], [6,26,52,78,104,130],
  [6,30,56,82,108,134], [6,34,60,86,112,138], [6,30,58,86,114,142], [6,34,62,90,118,146],
  [6,30,54,78,102,126,150], [6,24,50,76,102,128,154], [6,28,54,80,106,132,158],
  [6,32,58,84,110,136,162], [6,26,54,82,110,138,166], [6,30,58,86,114,142,170]];

const QR_EXP = new Uint8Array(512);
const QR_LOG = new Uint8Array(256);
(function initGaloisField() {
  let x = 1;
  for (let i = 0; i < 255; i++) {
    QR_EXP[i] = x;
    QR_LOG[x] = i;
    x <<= 1;
    if (x & 0x100) x ^= 0x11d;
  }
  for (let i = 255; i < 512; i++) QR_EXP[i] = QR_EXP[i - 255];
})();

function qrMul(a, b) { return a === 0 || b === 0 ? 0 : QR_EXP[QR_LOG[a] + QR_LOG[b]]; }

// Builds the product of (x - a^0)...(x - a^(degree-1)), highest power first,
// so poly[0] is the leading 1 that the division below is allowed to skip.
function qrGenerator(degree) {
  let poly = [1];
  for (let i = 0; i < degree; i++) {
    const next = new Array(poly.length + 1).fill(0);
    for (let j = 0; j < poly.length; j++) {
      next[j] ^= poly[j];                              // multiply by x
      next[j + 1] ^= qrMul(poly[j], QR_EXP[i]);        // multiply by a^i
    }
    poly = next;
  }
  return poly;
}

function qrRemainder(data, degree) {
  const out = new Array(degree).fill(0);
  const gen = qrGenerator(degree);
  for (const byte of data) {
    const factor = byte ^ out[0];
    out.shift();
    out.push(0);
    for (let i = 0; i < degree; i++) out[i] ^= qrMul(gen[i + 1], factor);
  }
  return out;
}

function qrCapacityBits(version, ec) {
  const [ecLen, g1, g2] = QR_ECB[version][ec];
  return (QR_TOTAL[version] - ecLen * (g1 + g2)) * 8;
}

function qrCharCountBits(version) { return version < 10 ? 8 : 16; }

/** Encode text into a module matrix. Returns {size, modules} with booleans. */
function qrEncode(text, level = "Q") {
  const ec = QR_EC[level] ?? QR_EC.Q;
  const bytes = new TextEncoder().encode(String(text));
  let version = 0;
  for (let v = 1; v <= 40; v++) {
    if (4 + qrCharCountBits(v) + bytes.length * 8 <= qrCapacityBits(v, ec)) { version = v; break; }
  }
  if (!version) throw new Error("qr: text too long");

  // --- bit stream -------------------------------------------------------
  const bits = [];
  const push = (value, length) => { for (let i = length - 1; i >= 0; i--) bits.push((value >> i) & 1); };
  push(4, 4);                                   // byte mode
  push(bytes.length, qrCharCountBits(version));
  for (const byte of bytes) push(byte, 8);
  const capacity = qrCapacityBits(version, ec);
  for (let i = 0; i < 4 && bits.length < capacity; i++) bits.push(0);
  while (bits.length % 8) bits.push(0);
  const data = [];
  for (let i = 0; i < bits.length; i += 8) {
    let byte = 0;
    for (let j = 0; j < 8; j++) byte = (byte << 1) | bits[i + j];
    data.push(byte);
  }
  for (let pad = 0; data.length * 8 < capacity; pad++) data.push(pad % 2 ? 0x11 : 0xec);

  // --- interleave blocks ------------------------------------------------
  const [ecLen, g1, g2] = QR_ECB[version][ec];
  const blocks = g1 + g2;
  const shortLen = Math.floor((QR_TOTAL[version] - ecLen * blocks) / blocks);
  const dataBlocks = [];
  const ecBlocks = [];
  let at = 0;
  for (let b = 0; b < blocks; b++) {
    const length = shortLen + (b >= g1 ? 1 : 0);
    const chunk = data.slice(at, at + length);
    at += length;
    dataBlocks.push(chunk);
    ecBlocks.push(qrRemainder(chunk, ecLen));
  }
  const stream = [];
  for (let i = 0; i < shortLen + 1; i++) {
    for (const block of dataBlocks) if (i < block.length) stream.push(block[i]);
  }
  for (let i = 0; i < ecLen; i++) for (const block of ecBlocks) stream.push(block[i]);

  // --- place modules ----------------------------------------------------
  const size = version * 4 + 17;
  const grid = Array.from({ length: size }, () => new Array(size).fill(null));
  const fixed = Array.from({ length: size }, () => new Array(size).fill(false));
  const setFixed = (x, y, value) => {
    if (x < 0 || y < 0 || x >= size || y >= size) return;
    grid[y][x] = value;
    fixed[y][x] = true;
  };
  const finder = (ox, oy) => {
    for (let y = -1; y <= 7; y++) {
      for (let x = -1; x <= 7; x++) {
        const edge = Math.max(Math.abs(x - 3), Math.abs(y - 3));
        setFixed(ox + x, oy + y, edge !== 2 && edge <= 3);
      }
    }
  };
  finder(0, 0); finder(size - 7, 0); finder(0, size - 7);
  for (let i = 8; i < size - 8; i++) {
    const on = i % 2 === 0;
    setFixed(i, 6, on);
    setFixed(6, i, on);
  }
  for (const cy of QR_ALIGN[version]) {
    for (const cx of QR_ALIGN[version]) {
      if ((cx < 8 && cy < 8) || (cx < 8 && cy > size - 9) || (cx > size - 9 && cy < 8)) continue;
      for (let y = -2; y <= 2; y++) {
        for (let x = -2; x <= 2; x++) setFixed(cx + x, cy + y, Math.max(Math.abs(x), Math.abs(y)) !== 1);
      }
    }
  }
  setFixed(8, size - 8, true);                 // dark module
  // Reserve the format areas. The top-left copy spans nine cells along each
  // axis, but the other two span only eight: letting them run to nine steals
  // two data modules and shifts every bit placed afterwards.
  for (let i = 0; i <= 8; i++) {
    if (grid[i][8] === null) setFixed(8, i, false);
    if (grid[8][i] === null) setFixed(i, 8, false);
  }
  for (let i = 0; i <= 7; i++) {
    if (grid[size - 1 - i][8] === null) setFixed(8, size - 1 - i, false);
    if (grid[8][size - 1 - i] === null) setFixed(size - 1 - i, 8, false);
  }
  if (version >= 7) {
    for (let i = 0; i < 18; i++) {
      const x = i % 3, y = Math.floor(i / 3);
      setFixed(size - 11 + x, y, false);
      setFixed(y, size - 11 + x, false);
    }
  }

  let index = 0;
  let upward = true;
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    for (let step = 0; step < size; step++) {
      const y = upward ? size - 1 - step : step;
      for (let col = 0; col < 2; col++) {
        const x = right - col;
        if (fixed[y][x]) continue;
        const bit = index < stream.length * 8 ? (stream[index >> 3] >> (7 - (index & 7))) & 1 : 0;
        grid[y][x] = bit === 1;
        index++;
      }
    }
    upward = !upward;
  }

  // --- masking ----------------------------------------------------------
  const maskAt = (pattern, x, y) => {
    switch (pattern) {
      case 0: return (x + y) % 2 === 0;
      case 1: return y % 2 === 0;
      case 2: return x % 3 === 0;
      case 3: return (x + y) % 3 === 0;
      case 4: return (Math.floor(y / 2) + Math.floor(x / 3)) % 2 === 0;
      case 5: return ((x * y) % 2) + ((x * y) % 3) === 0;
      case 6: return (((x * y) % 2) + ((x * y) % 3)) % 2 === 0;
      default: return (((x + y) % 2) + ((x * y) % 3)) % 2 === 0;
    }
  };
  const penalty = (matrix) => {
    let score = 0;
    const runs = (get) => {
      for (let a = 0; a < size; a++) {
        let run = 1;
        for (let b = 1; b < size; b++) {
          if (get(a, b) === get(a, b - 1)) { run++; continue; }
          if (run >= 5) score += run - 2;
          run = 1;
        }
        if (run >= 5) score += run - 2;
      }
    };
    runs((a, b) => matrix[a][b]);
    runs((a, b) => matrix[b][a]);
    for (let y = 0; y < size - 1; y++) {
      for (let x = 0; x < size - 1; x++) {
        const v = matrix[y][x];
        if (v === matrix[y][x + 1] && v === matrix[y + 1][x] && v === matrix[y + 1][x + 1]) score += 3;
      }
    }
    const bad = [true, false, true, true, true, false, true];
    const hasPattern = (line, at) => {
      for (let i = 0; i < 7; i++) if (line[at + i] !== bad[i]) return false;
      const before = line.slice(Math.max(0, at - 4), at);
      const after = line.slice(at + 7, at + 11);
      return (before.length === 4 && before.every((v) => !v)) || (after.length === 4 && after.every((v) => !v));
    };
    for (let a = 0; a < size; a++) {
      const row = matrix[a];
      const col = matrix.map((line) => line[a]);
      for (let at = 0; at + 7 <= size; at++) {
        if (hasPattern(row, at)) score += 40;
        if (hasPattern(col, at)) score += 40;
      }
    }
    let dark = 0;
    for (const row of matrix) for (const cell of row) if (cell) dark++;
    score += Math.floor(Math.abs((dark * 100) / (size * size) - 50) / 5) * 10;
    return score;
  };

  let best = null;
  let bestScore = Infinity;
  for (let pattern = 0; pattern < 8; pattern++) {
    const matrix = grid.map((row, y) => row.map((cell, x) => (fixed[y][x] ? cell : cell !== maskAt(pattern, x, y))));
    // Format information. The level is not the raw index: the specification
    // encodes L=01, M=00, Q=11, H=10.
    const formatBits = (() => {
      const value = ([1, 0, 3, 2][ec] << 3) | pattern;
      let rem = value << 10;
      for (let i = 14; i >= 10; i--) if ((rem >> i) & 1) rem ^= 0x537 << (i - 10);
      return ((value << 10) | rem) ^ 0x5412;
    })();
    for (let i = 0; i <= 5; i++) matrix[i][8] = ((formatBits >> i) & 1) === 1;
    matrix[7][8] = ((formatBits >> 6) & 1) === 1;
    matrix[8][8] = ((formatBits >> 7) & 1) === 1;
    matrix[8][7] = ((formatBits >> 8) & 1) === 1;
    for (let i = 9; i < 15; i++) matrix[8][14 - i] = ((formatBits >> i) & 1) === 1;
    for (let i = 0; i < 8; i++) matrix[8][size - 1 - i] = ((formatBits >> i) & 1) === 1;
    for (let i = 8; i < 15; i++) matrix[size - 15 + i][8] = ((formatBits >> i) & 1) === 1;
    matrix[size - 8][8] = true;
    if (version >= 7) {
      let rem = version << 12;
      for (let i = 17; i >= 12; i--) if ((rem >> i) & 1) rem ^= 0x1f25 << (i - 12);
      const bits18 = (version << 12) | rem;
      for (let i = 0; i < 18; i++) {
        const on = ((bits18 >> i) & 1) === 1;
        const x = i % 3, y = Math.floor(i / 3);
        matrix[y][size - 11 + x] = on;
        matrix[size - 11 + x][y] = on;
      }
    }
    const score = penalty(matrix);
    if (score < bestScore) { bestScore = score; best = matrix; }
  }
  return { size, modules: best, version };
}

/** Render the matrix as an elegant standalone SVG string. */
function qrSvg(text, options = {}) {
  const { size, modules } = qrEncode(text, options.level || "Q");
  const quiet = options.quiet ?? 4;
  const span = size + quiet * 2;
  const unit = 10;
  const box = span * unit;
  const id = "q" + Math.random().toString(36).slice(2, 8);
  const dark = options.dark || `url(#${id}g)`;
  // Rounding is what makes the code feel designed rather than printed, but
  // scanners find the eyes by their 1:1:3:1:1 ratio and lose it when the
  // corners melt. These defaults are the roundest that still decode.
  const dotR = options.dotRadius ?? 4.6;
  const eyeR = options.eyeRadius ?? 3;
  const pupilR = options.pupilRadius ?? 2;
  const parts = [];

  const isFinder = (x, y) => (x < 7 && y < 7) || (x >= size - 7 && y < 7) || (x < 7 && y >= size - 7);
  const on = (x, y) => x >= 0 && y >= 0 && x < size && y < size && modules[y][x] && !isFinder(x, y);

  // Data modules as rounded dots that merge with their neighbours.
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      if (!on(x, y)) continue;
      const px = (x + quiet) * unit, py = (y + quiet) * unit;
      const tl = !on(x - 1, y) && !on(x, y - 1) ? dotR : 0;
      const tr = !on(x + 1, y) && !on(x, y - 1) ? dotR : 0;
      const br = !on(x + 1, y) && !on(x, y + 1) ? dotR : 0;
      const bl = !on(x - 1, y) && !on(x, y + 1) ? dotR : 0;
      parts.push(`<path d="M${px + tl} ${py}h${unit - tl - tr}a${tr} ${tr} 0 0 1 ${tr} ${tr}`
        + `v${unit - tr - br}a${br} ${br} 0 0 1 ${-br} ${br}h${-(unit - br - bl)}`
        + `a${bl} ${bl} 0 0 1 ${-bl} ${-bl}v${-(unit - bl - tl)}a${tl} ${tl} 0 0 1 ${tl} ${-tl}z"/>`);
    }
  }
  // Carved finder eyes.
  for (const [fx, fy] of [[0, 0], [size - 7, 0], [0, size - 7]]) {
    const px = (fx + quiet) * unit, py = (fy + quiet) * unit;
    parts.push(`<rect x="${px + 5}" y="${py + 5}" width="${unit * 7 - 10}" height="${unit * 7 - 10}" rx="${eyeR}" `
      + `fill="none" stroke="${dark}" stroke-width="10"/>`);
    parts.push(`<rect x="${px + unit * 2}" y="${py + unit * 2}" width="${unit * 3}" height="${unit * 3}" rx="${pupilR}"/>`);
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${box} ${box}" width="${box}" height="${box}" role="img">`
    + `<defs><linearGradient id="${id}g" x1="0" y1="0" x2="1" y2="1">`
    + `<stop offset="0" stop-color="${options.from || "#4c3fd6"}"/>`
    + `<stop offset="1" stop-color="${options.to || "#8257ff"}"/></linearGradient></defs>`
    + `<rect width="${box}" height="${box}" rx="${unit * 3}" fill="${options.bg || "#ffffff"}"/>`
    + `<g fill="${dark}">${parts.join("")}</g></svg>`;
}

if (typeof module === "object" && module.exports) module.exports = { qrEncode, qrSvg };
