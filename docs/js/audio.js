// Microphone capture (and replay of a recording file for testing), decimated to ~22 kHz.
import { Decimator } from "./dsp.js";

const TARGET = 22050;

function pipeline(rate) {
  const factor = Math.max(1, Math.round(rate / TARGET));
  return { factor, sr: rate / factor, dec: new Decimator(factor) };
}

export async function startMic(onChunk) {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 },
  });
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  await ctx.resume();
  await ctx.audioWorklet.addModule(new URL("./worklet.js", import.meta.url).href);
  const { sr, dec } = pipeline(ctx.sampleRate);
  const src = ctx.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(ctx, "capture");
  node.port.onmessage = (e) => onChunk(dec.push(e.data));
  const mute = ctx.createGain();
  mute.gain.value = 0; // keep the graph running without playing the microphone back
  src.connect(node);
  node.connect(mute);
  mute.connect(ctx.destination);
  return {
    sr,
    stop() {
      stream.getTracks().forEach((t) => t.stop());
      node.disconnect();
      ctx.close();
    },
  };
}

/** Plays an audio file through the same pipeline, in real time, as if it were the microphone. */
export async function replayFile(file, onChunk, speed = 1) {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const audio = await ctx.decodeAudioData(await file.arrayBuffer());
  const rate = ctx.sampleRate;
  ctx.close();
  const { sr, dec } = pipeline(rate);
  const x = audio.getChannelData(0);
  let pos = 0;
  const t0 = performance.now();
  let timer = null;
  const done = new Promise((resolve) => {
    timer = setInterval(() => {
      const want = Math.floor(((performance.now() - t0) / 1000) * rate * speed);
      while (pos < Math.min(want, x.length)) {
        const e = Math.min(pos + 2048, x.length);
        onChunk(dec.push(x.subarray(pos, e)));
        pos = e;
      }
      if (pos >= x.length) { clearInterval(timer); resolve(); }
    }, 20);
  });
  return { sr, done, stop() { clearInterval(timer); } };
}
