// Remembers the music files you opened and your last practice for each (IndexedDB, with an in-memory fallback).
const mem = { scores: new Map(), runs: new Map() };
let dbp = null;

function db() {
  if (!dbp) {
    dbp = new Promise((resolve) => {
      try {
        const req = indexedDB.open("pianocheck", 1);
        req.onupgradeneeded = () => {
          req.result.createObjectStore("scores", { keyPath: "name" });
          req.result.createObjectStore("runs", { keyPath: "name" });
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => resolve(null);
      } catch { resolve(null); }
    });
  }
  return dbp;
}

async function tx(store, mode, fn) {
  const d = await db();
  if (!d) return null;
  return new Promise((resolve) => {
    try {
      const t = d.transaction(store, mode);
      const r = fn(t.objectStore(store));
      t.oncomplete = () => resolve(r && "result" in r ? r.result : null);
      t.onerror = () => resolve(null);
      t.onabort = () => resolve(null);
    } catch { resolve(null); }
  });
}

export async function putScore(name, xml) {
  const rec = { name, xml, ts: Date.now() };
  mem.scores.set(name, rec);
  await tx("scores", "readwrite", (s) => s.put(rec));
}

export async function touchScore(name) {
  const rec = await getScore(name);
  if (rec) await putScore(name, rec.xml);
}

export async function getScore(name) {
  const r = await tx("scores", "readonly", (s) => s.get(name));
  return r || mem.scores.get(name) || null;
}

export async function listScores() {
  const r = await tx("scores", "readonly", (s) => s.getAll());
  const all = new Map();
  for (const x of mem.scores.values()) all.set(x.name, x);
  for (const x of r || []) all.set(x.name, x);
  return [...all.values()].sort((a, b) => b.ts - a.ts).map((x) => x.name);
}

export async function putRun(name, snap) {
  const rec = { name, snap, ts: Date.now() };
  mem.runs.set(name, rec);
  await tx("runs", "readwrite", (s) => s.put(rec));
}

export async function getRun(name) {
  const r = await tx("runs", "readonly", (s) => s.get(name));
  return (r || mem.runs.get(name) || {}).snap || null;
}
