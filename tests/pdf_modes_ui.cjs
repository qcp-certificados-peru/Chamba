"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.listeners = new Map();
    this.textContent = "";
    this.value = "";
    this.files = [];
    this.hidden = false;
    this.disabled = false;
    this.className = "";
    this.classList = { add() {}, remove() {} };
  }
  append(...nodes) {
    for (const node of nodes) {
      node.parent = this;
      this.children.push(node);
    }
  }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  setAttribute(key, value) { this[key] = value; }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  async fire(name) {
    return this.listeners.get(name)?.({ target: this, preventDefault() {} });
  }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this);
    this.parent = null;
  }
  querySelectorAll(selector) {
    return walk(this).filter(node => selector.startsWith(".")
      && node.className.split(" ").includes(selector.slice(1)));
  }
  click() {}
}
function walk(root) {
  return root.children.flatMap(child => [child, ...walk(child)]);
}
const tick = () => new Promise(resolve => setImmediate(resolve));
const source = fs.readFileSync(path.join(__dirname, "../app/static/app.js"), "utf8");

async function scenario(retry) {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const requests = [];
  const blobs = new Map();
  let reportAttempts = 0;
  const context = {
    document: {
      getElementById: get,
      createElement: tag => new Element(tag),
      createElementNS: (_, tag) => new Element(tag),
      createTextNode: text => Object.assign(new Element("#text"), { textContent: text }),
      body: new Element("body"),
    },
    window: { QCPLinksExcel: {
      isDriveLink: value => typeof value === "string" && value.startsWith("https://drive.google.com/file/d/"),
    } },
    FormData, Date, Map, WeakMap, console,
    setInterval: () => 1, clearInterval() {}, setTimeout, clearTimeout,
    URL: {
      createObjectURL(blob) { const url = "blob:test-" + blobs.size; blobs.set(url, blob); return url; },
      revokeObjectURL(url) { blobs.delete(url); },
    },
    fetch: async (url, options) => {
      if (url === "/api/config") return new Response(JSON.stringify({
        uploadsEnabled: true,
        localMode: true,
        certificateCodePattern: "QCP-[0-9]{4}-[0-9]{4}",
        maxCodeLength: 40,
        certificateBackgrounds: [
          { value: "acreditado", label: "Acreditado", available: true },
          { value: "no_acreditado", label: "No acreditado", available: true },
        ],
      }), { headers: { "Content-Type": "application/json" } });
      assert.equal(url, "/api/certificates");
      const form = options.body;
      const mode = form.get("modoPdf");
      requests.push({ mode, background: form.get("tipoCertificado"), filename: form.get("pdf").name });
      if (mode === "background") {
        reportAttempts++;
        if (retry && reportAttempts === 1) {
          return new Response(JSON.stringify({ detail: "Reintentar descarga" }),
            { status: 503, headers: { "Content-Type": "application/json" } });
        }
        const response = new Response("%PDF-1.7\nSYNTHETIC REPORT - SIN VALIDEZ",
          { headers: { "Content-Type": "application/pdf", "X-Processing-Mode": "background-only" } });
        response.json = () => { throw new Error("A report download must not be parsed as JSON"); };
        return response;
      }
      assert.equal(mode, "certificate");
      return new Response(JSON.stringify({
        id: "synthetic-certificate", filename: "certificate.pdf", sourceType: "pdf",
        finalCode: "QCP-2026-0001", warnings: [], formatPreserved: true,
        downloadUrl: "/api/certificates/synthetic-certificate/download",
        driveUrl: "https://drive.google.com/file/d/SIN_VALIDEZ/view",
        qrMode: "drive-direct",
      }), { status: 201, headers: { "Content-Type": "application/json" } });
    },
  };
  vm.runInNewContext(source, context, { filename: "app.js" });
  for (let i = 0; i < 4; i++) await tick();
  const report = new File(["%PDF-"], "informe-sin-titulo-exacto.pdf", { type: "application/pdf" });
  const certificate = new File(["%PDF-"], "certificate.pdf", { type: "application/pdf" });
  get("document-files").files = retry ? [report] : [report, certificate];
  await get("document-files").fire("change");
  const modeElements = () => walk(get("file-list")).filter(node => node.className.includes("pdf-mode-select"));
  const backgroundElements = () => walk(get("file-list")).filter(node => node.className.includes("type-select"));
  assert.equal(modeElements().length, retry ? 1 : 2);
  modeElements()[0].value = "background";
  await modeElements()[0].fire("change");
  if (!retry) {
    modeElements()[1].value = "certificate";
    await modeElements()[1].fire("change");
  }
  for (let i = 0; i < backgroundElements().length; i++) {
    const element = backgroundElements()[i];
    element.value = i ? "acreditado" : "no_acreditado";
    await element.fire("change");
  }
  assert.equal(get("submit").disabled, false);
  await get("certificate-form").fire("submit");
  if (retry) {
    assert.equal(modeElements()[0].value, "background");
    assert.equal(backgroundElements()[0].value, "no_acreditado");
    await get("certificate-form").fire("submit");
  }
  assert.equal(requests[0].mode, "background");
  assert.equal(requests[0].background, "no_acreditado");
  const links = walk(get("result-list")).filter(node => node.tagName === "A");
  const reportLink = links.find(link => link.href?.startsWith("blob:"));
  assert.ok(reportLink, "A direct PDF download must be rendered");
  assert.equal(reportLink.download, report.name);
  assert.ok((await blobs.get(reportLink.href).text()).startsWith("%PDF-"));
  const driveLinks = links.filter(link => link.href?.startsWith("https://drive.google.com/"));
  assert.equal(driveLinks.length, retry ? 0 : 1);
  assert.equal(get("batch-export").hidden, retry);
  if (retry) assert.deepEqual(requests.map(row => row.mode), ["background", "background"]);
  else assert.deepEqual(requests.map(row => row.mode), ["background", "certificate"]);
}
(async () => {
  await scenario(false);
  await scenario(true);
  console.log("PDF modes: mixed batch, direct download, Drive isolation and retry passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
