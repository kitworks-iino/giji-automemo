import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(
  new URL("../src/pcmAudio.ts", import.meta.url),
  "utf8",
);
const javascript = ts.transpileModule(source, {
  compilerOptions: {
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.ES2022,
  },
}).outputText;
const { RangePcmSource, pcm16Channels, MAX_PCM_RANGE_BYTES } = await import(
  `data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}`
);
globalThis.location = { origin: "http://127.0.0.1:8768" };
const url =
  "/api/sessions/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/audio/microphone?playback=test";
const signal = () => new AbortController().signal;

function wave({
  channels = 2,
  rate = 16000,
  frames = 2,
  codec = 1,
  bits = 16,
  junk = false,
} = {}) {
  const prefix = junk ? 12 : 0;
  const bytes = new Uint8Array(44 + prefix + frames * channels * 2);
  const view = new DataView(bytes.buffer);
  const text = (at, value) =>
    [...value].forEach(
      (letter, index) => (bytes[at + index] = letter.charCodeAt(0)),
    );
  text(0, "RIFF");
  view.setUint32(4, bytes.length - 8, true);
  text(8, "WAVE");
  if (junk) {
    text(12, "JUNK");
    view.setUint32(16, 3, true);
    bytes.set([1, 2, 3], 20);
  }
  const fmt = 12 + prefix;
  text(fmt, "fmt ");
  view.setUint32(fmt + 4, 16, true);
  view.setUint16(fmt + 8, codec, true);
  view.setUint16(fmt + 10, channels, true);
  view.setUint32(fmt + 12, rate, true);
  view.setUint32(fmt + 16, rate * channels * 2, true);
  view.setUint16(fmt + 20, channels * 2, true);
  view.setUint16(fmt + 22, bits, true);
  text(fmt + 24, "data");
  view.setUint32(fmt + 28, frames * channels * 2, true);
  return bytes;
}

function ranged(
  t,
  bytes,
  { total = bytes.length, etag = '"fixture"', alter } = {},
) {
  const requests = [];
  const original = globalThis.fetch;
  t.after(() => (globalThis.fetch = original));
  globalThis.fetch = async (_url, options) => {
    const match = /^bytes=(\d+)-(\d+)$/.exec(options.headers.Range);
    assert.ok(match);
    const start = Number(match[1]),
      end = Math.min(Number(match[2]), total - 1);
    assert.ok(end - start + 1 <= MAX_PCM_RANGE_BYTES);
    requests.push({ start, end, signal: options.signal });
    const body = new Uint8Array(end - start + 1);
    if (start < bytes.length)
      body.set(bytes.subarray(start, Math.min(end + 1, bytes.length)));
    const headers = {
      "Content-Range": `bytes ${start}-${end}/${total}`,
      "Content-Length": String(body.length),
      ETag: etag,
    };
    return (
      alter?.({ start, end, body, headers, requests }) ??
      new Response(body, { status: 206, headers })
    );
  };
  return requests;
}

test("PCM16 deinterleaves channels, signed little-endian boundaries and negative samples", () => {
  const data = new Uint8Array([0, 128, 255, 127, 0, 0, 0, 192]);
  const result = pcm16Channels(data, 2);
  assert.deepEqual([...result[0]], [-1, 0]);
  assert.deepEqual([...result[1]], [32767 / 32768, -0.5]);
  assert.throws(() => pcm16Channels(data.subarray(0, 7), 2));
  assert.throws(() => pcm16Channels(data, 0));
});

test("parses PCM16 and skips padded unknown RIFF chunks without decoding them", async (t) => {
  const bytes = wave({ junk: true });
  const requests = ranged(t, bytes);
  const reader = new RangePcmSource(url);
  const info = await reader.load(signal());
  assert.deepEqual(info, {
    channels: 2,
    sampleRate: 16000,
    frameBytes: 4,
    frames: 2,
    dataOffset: 56,
    duration: 2 / 16000,
  });
  assert.equal((await reader.samples(0, 2, signal())).length, 8);
  assert.ok(requests.every((request) => request.signal instanceof AbortSignal));
});

for (const [name, options] of [
  ["compressed codec", { codec: 6 }],
  ["floating-point codec", { codec: 3 }],
  ["non-16-bit samples", { bits: 24 }],
  ["unsupported channel count", { channels: 9 }],
  ["unsupported sample rate", { rate: 4000 }],
]) {
  test(`rejects ${name}`, async (t) => {
    ranged(t, wave(options));
    await assert.rejects(new RangePcmSource(url).load(signal()), /PCM16 WAV/);
  });
}

test("rejects inconsistent frame alignment and truncated RIFF data", async (t) => {
  const bytes = wave();
  new DataView(bytes.buffer).setUint16(32, 6, true);
  ranged(t, bytes);
  await assert.rejects(new RangePcmSource(url).load(signal()), /PCM16 WAV/);
});

test("rejects data chunk past declared RIFF boundary", async (t) => {
  const bytes = wave();
  new DataView(bytes.buffer).setUint32(40, 100000, true);
  ranged(t, bytes);
  await assert.rejects(new RangePcmSource(url).load(signal()), /PCM16 WAV/);
});

test("a 2GB recording loads only metadata and a bounded seek range", async (t) => {
  const bytes = wave({ channels: 1, frames: 1 });
  const total = 2_000_000_044;
  const view = new DataView(bytes.buffer);
  view.setUint32(4, total - 8, true);
  view.setUint32(40, total - 44, true);
  const requests = ranged(t, bytes, { total });
  const reader = new RangePcmSource(url);
  const info = await reader.load(signal());
  assert.equal(info.frames, 1_000_000_000);
  assert.equal(
    requests.reduce((sum, request) => sum + request.end - request.start + 1, 0),
    44,
  );
  await reader.samples(info.frames - 1000, 1000, signal());
  assert.equal(requests.at(-1).start, total - 2000);
  const before = requests.length;
  await assert.rejects(reader.samples(0, MAX_PCM_RANGE_BYTES, signal()));
  assert.equal(
    requests.length,
    before,
    "Oversized request must fail before fetching",
  );
});

test("rejects 200 full-file fallback without reading the response body", async (t) => {
  const original = globalThis.fetch;
  t.after(() => (globalThis.fetch = original));
  let cancelled = false;
  globalThis.fetch = async () =>
    new Response(
      new ReadableStream({
        cancel() {
          cancelled = true;
        },
      }),
      { status: 200 },
    );
  await assert.rejects(new RangePcmSource(url).load(signal()), /分割/);
  assert.ok(cancelled);
});

test("rejects malformed Content-Range and oversized streaming body", async (t) => {
  ranged(t, wave(), {
    alter: ({ body, headers }) =>
      new Response(body, {
        status: 206,
        headers: { ...headers, "Content-Range": "bytes 1-12/52" },
      }),
  });
  await assert.rejects(new RangePcmSource(url).load(signal()), /範囲/);
});

test("stream length is bounded even when the body exceeds its valid range header", async (t) => {
  ranged(t, wave(), {
    alter: ({ body, headers }) =>
      new Response(new Uint8Array(body.length + 1), { status: 206, headers }),
  });
  await assert.rejects(new RangePcmSource(url).load(signal()), /大きすぎ/);
});

test("rejects a changed recording between metadata and playback", async (t) => {
  ranged(t, wave(), {
    alter: ({ body, headers, requests }) =>
      new Response(body, {
        status: 206,
        headers: {
          ...headers,
          ETag: requests.length > 4 ? '"changed"' : '"fixture"',
        },
      }),
  });
  const reader = new RangePcmSource(url);
  await reader.load(signal());
  await assert.rejects(reader.samples(0, 1, signal()), /変更/);
});

test("permits only scoped local recording URLs", () => {
  assert.throws(() => new RangePcmSource("https://example.com" + url));
  assert.throws(() => new RangePcmSource("/api/settings"));
  assert.throws(() => new RangePcmSource("/api/sessions/../settings"));
});
