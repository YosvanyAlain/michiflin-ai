// Node harness: node tests/mp4join.js out.mp4 part1 part2 ... (mp3 pieces use the byte joiner)
const fs = require("fs");
const Mp4Join = require("../webxdc/mp4.js");

const fileSource = (path) => {
  const handle = fs.openSync(path, "r");
  const size = fs.fstatSync(handle).size;
  return { size, path, read: async (offset, length) => {
    const buffer = Buffer.alloc(Math.max(0, Math.min(length, size - offset)));
    fs.readSync(handle, buffer, 0, buffer.length, offset);
    return new Uint8Array(buffer);
  } };
};

(async () => {
  const [target, ...paths] = process.argv.slice(2);
  const sources = paths.map(fileSource);
  const markers = [];
  for (const source of sources) markers.push(await Mp4Join.marker(source));
  const result = target.endsWith(".mp3") ? await Mp4Join.joinMp3(sources) : await Mp4Join.join(sources);
  const out = fs.openSync(target, "w");
  let written = 0;
  for (const part of result.parts) {
    const bytes = part instanceof Uint8Array ? part : await part.source.read(part.start, part.end - part.start);
    fs.writeSync(out, bytes);
    written += bytes.length;
  }
  fs.closeSync(out);
  console.log(JSON.stringify({ size: result.size, written, duration: result.duration, markers }));
})().catch((error) => {
  console.log(JSON.stringify({ error: error.code || error.message }));
  process.exit(1);
});
