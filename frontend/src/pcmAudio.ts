// The recorder writes signed little-endian PCM16 samples. Read bounded ranges
// and copy those samples directly into Web Audio buffers; no media decoder.
export const MAX_PCM_RANGE_BYTES = 1024 * 1024;
const MAX_QUEUED_BUFFERS = 3;
const CHUNK_SECONDS = 2;

export type WaveInfo = {
  channels: number;
  sampleRate: number;
  frameBytes: number;
  frames: number;
  dataOffset: number;
  duration: number;
};

export type PlaybackState = {
  loaded: boolean;
  loading: boolean;
  playing: boolean;
  buffering: boolean;
  position: number;
  duration: number;
  error: string;
};

const formatError = () =>
  new Error("この録音は対応するPCM16 WAV形式ではありません。");
const ascii = (bytes: Uint8Array, offset: number, length: number) =>
  String.fromCharCode(...bytes.subarray(offset, offset + length));

export class RangePcmSource {
  private total: number | null = null;
  private etag: string | null = null;
  private url: string;
  info: WaveInfo | null = null;

  constructor(url: string) {
    const parsed = new URL(url, location.origin);
    if (
      parsed.origin !== location.origin ||
      !/^\/api\/sessions\/[a-f0-9]{32}\/audio\/(microphone|system)$/.test(
        parsed.pathname,
      )
    ) {
      throw new Error("録音データのURLを確認してください。");
    }
    this.url = parsed.href;
  }

  private async range(start: number, count: number, signal: AbortSignal) {
    if (
      !Number.isSafeInteger(start) ||
      count < 1 ||
      count > MAX_PCM_RANGE_BYTES
    ) {
      throw formatError();
    }
    const end = start + count - 1;
    const response = await fetch(this.url, {
      headers: { Range: `bytes=${start}-${end}` },
      cache: "no-store",
      credentials: "same-origin",
      signal,
    });
    const rejectResponse = async (message: string): Promise<never> => {
      await response.body?.cancel().catch(() => {});
      throw new Error(message);
    };
    // Never consume a full-file response when a server ignores Range.
    if (response.status !== 206) {
      return rejectResponse(
        "録音を分割して読み込めません。アプリを再起動して、録音を開き直してください。",
      );
    }
    const match = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(
      response.headers.get("Content-Range") ?? "",
    );
    if (!match) return rejectResponse("録音データの取得範囲を確認できません。");
    const first = Number(match[1]),
      last = Number(match[2]),
      total = Number(match[3]);
    if (
      !Number.isSafeInteger(total) ||
      total < 12 ||
      total > 0x1_0000_0007 ||
      first !== start ||
      last !== Math.min(end, total - 1) ||
      last < first ||
      (this.total !== null && this.total !== total)
    ) {
      return rejectResponse(
        "録音データの範囲またはサイズが変わりました。開き直してください。",
      );
    }
    const etag = response.headers.get("ETag");
    if (this.etag !== null && etag !== this.etag) {
      return rejectResponse("録音データが変更されました。開き直してください。");
    }
    this.total = total;
    if (etag) this.etag = etag;
    const expected = last - first + 1;
    const length = response.headers.get("Content-Length");
    if (length !== null && Number(length) !== expected) {
      return rejectResponse("録音データのサイズが一致しません。");
    }
    const reader = response.body?.getReader();
    if (!reader) throw new Error("録音データを読み込めません。");
    const bytes = new Uint8Array(expected);
    let offset = 0;
    try {
      while (true) {
        const part = await reader.read();
        if (part.done) break;
        if (offset + part.value.byteLength > expected) {
          await reader.cancel();
          throw new Error("録音データの取得範囲が大きすぎます。");
        }
        bytes.set(part.value, offset);
        offset += part.value.byteLength;
      }
    } finally {
      reader.releaseLock();
    }
    if (offset !== expected || expected !== count)
      throw new Error("録音データが途中で途切れています。");
    return bytes;
  }

  async load(signal: AbortSignal): Promise<WaveInfo> {
    const header = await this.range(0, 12, signal);
    if (ascii(header, 0, 4) !== "RIFF" || ascii(header, 8, 4) !== "WAVE")
      throw formatError();
    const riffEnd = new DataView(header.buffer).getUint32(4, true) + 8;
    if (riffEnd < 44 || riffEnd > (this.total ?? 0)) throw formatError();
    let position = 12;
    let format: {
      channels: number;
      sampleRate: number;
      frameBytes: number;
    } | null = null;
    for (let chunks = 0; chunks < 128 && position + 8 <= riffEnd; chunks++) {
      const chunk = await this.range(position, 8, signal);
      const id = ascii(chunk, 0, 4);
      const size = new DataView(chunk.buffer).getUint32(4, true);
      const body = position + 8;
      if (body + size > riffEnd) throw formatError();
      if (id === "fmt ") {
        if (format || size < 16) throw formatError();
        const raw = await this.range(body, 16, signal);
        const view = new DataView(raw.buffer);
        const codec = view.getUint16(0, true),
          channels = view.getUint16(2, true);
        const sampleRate = view.getUint32(4, true),
          byteRate = view.getUint32(8, true);
        const frameBytes = view.getUint16(12, true),
          bits = view.getUint16(14, true);
        if (
          codec !== 1 ||
          bits !== 16 ||
          channels < 1 ||
          channels > 8 ||
          sampleRate < 8000 ||
          sampleRate > 192000 ||
          frameBytes !== channels * 2 ||
          byteRate !== sampleRate * frameBytes
        )
          throw formatError();
        format = { channels, sampleRate, frameBytes };
      } else if (id === "data") {
        if (!format || size % format.frameBytes !== 0) throw formatError();
        if (size === 0) throw new Error("この録音には音声データがありません。");
        this.info = {
          ...format,
          frames: size / format.frameBytes,
          dataOffset: body,
          duration: size / format.frameBytes / format.sampleRate,
        };
        return this.info;
      }
      position = body + size + (size % 2);
    }
    throw formatError();
  }

  async samples(frame: number, frames: number, signal: AbortSignal) {
    if (
      !this.info ||
      !Number.isInteger(frame) ||
      !Number.isInteger(frames) ||
      frame < 0 ||
      frames < 1 ||
      frame + frames > this.info.frames
    )
      throw formatError();
    return this.range(
      this.info.dataOffset + frame * this.info.frameBytes,
      frames * this.info.frameBytes,
      signal,
    );
  }
}

export function pcm16Channels(
  bytes: Uint8Array,
  channels: number,
): Float32Array[] {
  if (
    !Number.isInteger(channels) ||
    channels < 1 ||
    channels > 8 ||
    bytes.length % (channels * 2)
  ) {
    throw formatError();
  }
  const frames = bytes.length / (channels * 2);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  return Array.from({ length: channels }, (_, channel) => {
    const values = new Float32Array(frames);
    for (let frame = 0; frame < frames; frame++) {
      values[frame] =
        view.getInt16((frame * channels + channel) * 2, true) / 32768;
    }
    return values;
  });
}

type Scheduled = {
  source: AudioBufferSourceNode;
  time: number;
  frame: number;
  frames: number;
};

export class PcmPlayback {
  private source: RangePcmSource;
  private context: AudioContext | null = null;
  private gain: GainNode | null = null;
  private volume = 1;
  private controller: AbortController | null = null;
  private generation = 0;
  private pumping = -1;
  private queue: Scheduled[] = [];
  private nextFrame = 0;
  private positionFrame = 0;
  private scheduledUntil = 0;
  private playing = false;
  private loading = false;
  private disposed = false;
  private error = "";
  private timer: ReturnType<typeof setInterval>;

  constructor(
    url: string,
    private changed: (state: PlaybackState) => void,
  ) {
    this.source = new RangePcmSource(url);
    this.timer = setInterval(() => {
      if (this.playing) this.emit();
    }, 150);
  }

  private currentFrame() {
    const info = this.source.info;
    if (!this.context || !info || !this.playing) return this.positionFrame;
    const now = this.context.currentTime;
    let frame = this.positionFrame;
    for (const item of this.queue) {
      if (now < item.time) return item.frame;
      if (now < item.time + item.frames / info.sampleRate) {
        return item.frame + (now - item.time) * info.sampleRate;
      }
      frame = item.frame + item.frames;
    }
    return Math.min(info.frames, frame);
  }

  private emit() {
    if (this.disposed) return;
    const info = this.source.info;
    this.changed({
      loaded: !!info,
      loading: this.loading,
      playing: this.playing,
      buffering: this.playing && this.queue.length === 0,
      position: info ? this.currentFrame() / info.sampleRate : 0,
      duration: info?.duration ?? 0,
      error: this.error,
    });
  }

  async load() {
    const generation = ++this.generation;
    this.controller?.abort();
    this.controller = new AbortController();
    this.loading = true;
    this.error = "";
    this.emit();
    try {
      await this.source.load(this.controller.signal);
    } catch (error) {
      if (generation === this.generation && !this.disposed)
        this.error = (error as Error).message;
    } finally {
      if (generation === this.generation) {
        this.loading = false;
        this.emit();
      }
    }
  }

  async play() {
    const info = this.source.info;
    if (!info || this.playing || this.disposed) return;
    if (this.positionFrame >= info.frames) this.positionFrame = 0;
    this.error = "";
    const generation = ++this.generation;
    this.playing = true;
    this.controller?.abort();
    this.controller = new AbortController();
    this.nextFrame = Math.floor(this.positionFrame);
    this.scheduledUntil = 0;
    this.emit();
    try {
      if (!this.context) {
        this.context = new AudioContext();
        this.gain = this.context.createGain();
        this.gain.gain.value = this.volume;
        this.gain.connect(this.context.destination);
      }
      await this.context.resume();
      if (generation === this.generation) await this.pump(generation);
    } catch (error) {
      if (generation === this.generation) this.fail(error);
    }
  }

  private async pump(generation: number) {
    if (this.pumping === generation) return;
    const info = this.source.info,
      context = this.context,
      signal = this.controller?.signal;
    if (!info || !context || !signal || !this.gain) return;
    this.pumping = generation;
    try {
      while (
        this.playing &&
        generation === this.generation &&
        this.queue.length < MAX_QUEUED_BUFFERS &&
        this.nextFrame < info.frames
      ) {
        const frame = this.nextFrame;
        const frames = Math.min(
          info.frames - frame,
          info.sampleRate * CHUNK_SECONDS,
          Math.floor(MAX_PCM_RANGE_BYTES / info.frameBytes),
        );
        const bytes = await this.source.samples(frame, frames, signal);
        if (!this.playing || generation !== this.generation) return;
        const channels = pcm16Channels(bytes, info.channels);
        const buffer = context.createBuffer(
          info.channels,
          frames,
          info.sampleRate,
        );
        channels.forEach((values, channel) =>
          buffer.copyToChannel(values, channel),
        );
        const node = context.createBufferSource();
        node.buffer = buffer;
        node.connect(this.gain);
        const time = Math.max(context.currentTime + 0.025, this.scheduledUntil);
        const item: Scheduled = { source: node, time, frame, frames };
        this.queue.push(item);
        this.nextFrame += frames;
        this.scheduledUntil = time + frames / info.sampleRate;
        node.onended = () => {
          node.disconnect();
          if (generation !== this.generation || this.disposed) return;
          this.positionFrame = frame + frames;
          this.queue = this.queue.filter((queued) => queued !== item);
          if (this.queue.length === 0 && this.nextFrame >= info.frames) {
            this.playing = false;
            void context.suspend().catch(() => {});
          } else {
            void this.pump(generation);
          }
          this.emit();
        };
        node.start(time);
        this.emit();
      }
    } catch (error) {
      if (generation === this.generation) this.fail(error);
    } finally {
      if (this.pumping === generation) this.pumping = -1;
    }
  }

  private fail(error: unknown) {
    this.pause();
    this.error =
      error instanceof Error ? error.message : "録音を再生できませんでした。";
    this.emit();
  }

  pause() {
    this.positionFrame = Math.floor(this.currentFrame());
    this.playing = false;
    this.generation++;
    this.controller?.abort();
    this.controller = null;
    for (const item of this.queue) {
      item.source.onended = null;
      try {
        item.source.stop();
      } catch {
        /* A finished source may already be stopped. */
      }
      item.source.disconnect();
    }
    this.queue = [];
    this.scheduledUntil = 0;
    if (this.context) void this.context.suspend().catch(() => {});
    this.emit();
  }

  seek(seconds: number) {
    const info = this.source.info;
    if (!info || !Number.isFinite(seconds)) return;
    const resume = this.playing;
    this.pause();
    this.positionFrame = Math.round(
      Math.max(0, Math.min(info.duration, seconds)) * info.sampleRate,
    );
    this.error = "";
    this.emit();
    if (resume && this.positionFrame < info.frames) void this.play();
  }

  setVolume(value: number) {
    this.volume = Math.max(0, Math.min(1, value));
    if (this.gain) this.gain.gain.value = this.volume;
  }

  dispose() {
    this.disposed = true;
    this.pause();
    clearInterval(this.timer);
    if (this.context) void this.context.close().catch(() => {});
  }
}
