// Tiny XML reader (enough for MusicXML). Same code runs in the browser and in Node tests.
function decode(s) {
  return s.replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos);/gi, (m, e) => {
    if (e[0] === "#") return String.fromCodePoint(e[1] === "x" || e[1] === "X" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10));
    return { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" }[e.toLowerCase()];
  });
}

export function parseXML(text) {
  let i = 0;
  const n = text.length;
  const top = { tag: "#top", attrs: {}, children: [], text: "" };
  const stack = [top];
  while (i < n) {
    const lt = text.indexOf("<", i);
    if (lt < 0) break;
    if (lt > i) stack[stack.length - 1].text += decode(text.slice(i, lt));
    if (text.startsWith("<!--", lt)) { i = text.indexOf("-->", lt) + 3; continue; }
    if (text.startsWith("<![CDATA[", lt)) {
      const e = text.indexOf("]]>", lt);
      stack[stack.length - 1].text += text.slice(lt + 9, e);
      i = e + 3; continue;
    }
    if (text.startsWith("<?", lt)) { i = text.indexOf("?>", lt) + 2; continue; }
    if (text.startsWith("<!", lt)) {
      let depth = 0, j = lt + 2;
      for (; j < n; j++) {
        const c = text[j];
        if (c === "[") depth++; else if (c === "]") depth--; else if (c === ">" && depth <= 0) break;
      }
      i = j + 1; continue;
    }
    let j = lt + 1, q = null;
    for (; j < n; j++) {
      const c = text[j];
      if (q) { if (c === q) q = null; } else if (c === '"' || c === "'") q = c; else if (c === ">") break;
    }
    let inner = text.slice(lt + 1, j);
    i = j + 1;
    if (inner[0] === "/") { if (stack.length > 1) stack.pop(); continue; }
    const selfClose = inner.endsWith("/");
    if (selfClose) inner = inner.slice(0, -1);
    const tag = /^[^\s/>]+/.exec(inner)[0];
    const attrs = {};
    const re = /([^\s=]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g;
    const rest = inner.slice(tag.length);
    let a;
    while ((a = re.exec(rest))) attrs[a[1]] = decode(a[2] ?? a[3]);
    const node = { tag, attrs, children: [], text: "" };
    stack[stack.length - 1].children.push(node);
    if (!selfClose) stack.push(node);
  }
  return top.children[0];
}

export const child = (n, tag) => n.children.find((c) => c.tag === tag) || null;
export const kids = (n, tag) => n.children.filter((c) => c.tag === tag);
export function* iter(n, tag) {
  for (const c of n.children) {
    if (c.tag === tag) yield c;
    yield* iter(c, tag);
  }
}
export const intText = (n) => parseInt(n.text.trim(), 10);
