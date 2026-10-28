"use strict";
const $ = (id) => document.getElementById(id);
let files = [];
let busy = false;
let ready = false;
const codes = new Map();
const certificateTypes = new Map();
const layouts = new Map();
const formatFooters = new Map();
const batchLinks = new Map();
let batchPositions = new WeakMap();
let nextBatchPosition = 0;
let inspectionQueue = Promise.resolve();
let backgrounds = [];
let certificateCodePattern = "";
let maxCodeLength = 40;

function needsBackground(file) {
  return /\.pdf$/i.test(file.name) || /\.xls[xm]$/i.test(file.name) && layouts.get(file)?.data?.needsBackground === true;
}

function hasBackground(file) {
  if (/\.xls[xm]$/i.test(file.name) && !layouts.get(file)?.data) return false;
  return !needsBackground(file) || backgrounds.some((option) =>
    option.available && option.value === certificateTypes.get(file));
}

function updateSubmitState() {
  $("submit").disabled = busy || !ready || files.length === 0 || files.some((file) => !hasBackground(file));
}

function message(text) {
  $("load-error").textContent = text;
  $("load-error").hidden = !text;
}

function renderFiles() {
  $("file-list").replaceChildren();
  $("selection-summary").hidden = files.length === 0;
  $("file-count").textContent = `${files.length} ${files.length === 1 ? "archivo seleccionado" : "archivos seleccionados"}`;
  const pdfOnly = files.length > 0 && files.every((file) => /\.pdf$/i.test(file.name));
  $("signature-options").hidden = pdfOnly;
  $("signature-hint").hidden = pdfOnly;
  files.forEach((file, index) => {
    const isPdf = /\.pdf$/i.test(file.name);
    const row = document.createElement("div");
    row.className = "file-row";
    const top = document.createElement("div");
    top.className = "file-top";
    const name = document.createElement("span");
    name.className = "filename";
    name.textContent = file.name;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove";
    remove.textContent = "×";
    remove.disabled = busy;
    remove.setAttribute("aria-label", `Quitar ${file.name}`);
    remove.addEventListener("click", () => { codes.delete(file); certificateTypes.delete(file); layouts.delete(file); formatFooters.delete(file); files.splice(index, 1); renderFiles(); });
    top.append(name, remove);
    row.append(top);
    if (/\.xls[xm]$/i.test(file.name)) {
      const layout = layouts.get(file);
      const status = document.createElement("small");
      status.className = "background-hint";
      status.textContent = layout?.error || (layout?.data ?
        `${layout.data.selectionReason ? layout.data.selectionReason + " " : ""}Hoja: ${layout.data.sheetName}. ${layout.data.preserveFormat ? "Conservaremos el fondo, encabezado y pie existentes." : "Esta hoja no incluye fondo, encabezado ni pie. Elige el fondo oficial."}` :
        "Revisando la hoja y el formato del Excel…");
      row.append(status);
      if (layout?.error) {
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "remove";
        retry.textContent = "Revisar de nuevo";
        retry.disabled = busy;
        retry.addEventListener("click", () => inspectFile(file));
        row.append(retry);
      }
    }
    if (needsBackground(file)) {
      const typeLabel = document.createElement("label");
      typeLabel.className = "code-label type-label";
      typeLabel.textContent = isPdf ? "Fondo del PDF · obligatorio" : "Fondo para las hojas sin formato · obligatorio";
      const select = document.createElement("select");
      select.id = `type-${index}`;
      select.className = "code-input type-select";
      select.required = true;
      select.disabled = busy;
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "Selecciona el tipo de certificado";
      select.append(placeholder);
      for (const background of backgrounds) {
        const option = document.createElement("option");
        option.value = background.value;
        option.textContent = background.label + (background.available ? "" : " · no disponible");
        option.disabled = !background.available;
        select.append(option);
      }
      select.value = certificateTypes.get(file) || "";
      select.addEventListener("change", () => {
        certificateTypes.set(file, select.value);
        updateSubmitState();
      });
      typeLabel.htmlFor = select.id;
      row.append(typeLabel, select);
      const hint = document.createElement("small");
      hint.className = "background-hint";
      hint.textContent = isPdf ? "Añadiremos el fondo a sus páginas y alinearemos el QR junto a «Escanee este QR». Sus textos, tablas y firma se conservan." :
        "Conservaremos los formatos existentes. Este fondo solo se añadirá a las hojas que no tengan uno.";
      row.append(hint);
    }
    if (isPdf) {
      $("file-list").append(row);
      return;
    }
    const label = document.createElement("label");
    label.className = "code-label";
    label.textContent = "Número del certificado (LP / QCP) · déjalo vacío para conservar el actual";
    const input = document.createElement("input");
    input.className = "code-input";
    input.id = `code-${index}`;
    input.type = "text";
    input.maxLength = maxCodeLength;
    input.pattern = certificateCodePattern;
    input.placeholder = "QCP-2026-0001 o LP-ALT0125-PC001";
    input.title = "QCP-0000-0000 o LP-ALT0125-PC001";
    input.value = codes.get(file) || "";
    input.disabled = busy;
    input.addEventListener("input", () => {
      input.value = input.value.toUpperCase();
      codes.set(file, input.value);
    });
    label.htmlFor = input.id;
    row.append(label, input);
    if (/\.xls[xm]$/i.test(file.name) && layouts.get(file)?.data) {
      const detected = layouts.get(file).data.formatFooter;
      const note = document.createElement("small");
      note.className = "background-hint";
      note.textContent = detected ? `Formato del certificado: ${detected.code} · Versión ${detected.version}. Numeración automática.` :
        "Falta el código F-GCA y la versión del formato del certificado. Puedes completarlos abajo; son distintos del número LP/QCP y del código del equipo.";
      row.append(note);
      if (!detected) {
        const details = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = "Pie del certificado · código F-GCA y versión del formato";
        details.append(summary);
        const values = formatFooters.get(file) || { code: "", version: "" };
        for (const [key, title, placeholder, limit] of [["code", "Código del formato del certificado", "F-GCA-10.04", 40], ["version", "Versión del formato", "Por ejemplo 00 o 01, según tu formato", 16]]) {
          const footerLabel = document.createElement("label");
          footerLabel.className = "code-label";
          footerLabel.textContent = title;
          const field = document.createElement("input");
          field.className = `code-input format-${key}`;
          field.id = `format-${key}-${index}`;
          field.value = values[key];
          field.placeholder = placeholder;
          field.maxLength = limit;
          field.disabled = busy;
          field.addEventListener("input", () => { values[key] = field.value; formatFooters.set(file, values); });
          footerLabel.htmlFor = field.id;
          details.append(footerLabel, field);
        }
        row.append(details);
      }
    }
    $("file-list").append(row);
  });
  updateSubmitState();
}

function inspectFile(file) {
  layouts.set(file, { pending: true });
  renderFiles();
  inspectionQueue = inspectionQueue.then(async () => {
    if (!files.includes(file)) return;
    try {
      const form = new FormData();
      form.append("excel", file);
      const response = await fetch("/api/excel/inspect", { method: "POST", body: form });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "No se pudo revisar el Excel.");
      if (files.includes(file)) layouts.set(file, { data: result });
    } catch (error) {
      if (files.includes(file)) layouts.set(file, { error: error instanceof TypeError ? "No se pudo conectar. Revisa tu conexión y vuelve a intentar." : error.message });
    }
    renderFiles();
  });
}

function addFiles(incoming) {
  if (busy || !ready) return;
  const errors = [];
  for (const file of incoming) {
    if (!/\.(xlsx|xlsm|pdf)$/i.test(file.name)) { errors.push(`${file.name}: selecciona un Excel .xlsx, .xlsm o un PDF.`); continue; }
    if (file.size > 15 * 1024 * 1024) { errors.push(`${file.name}: supera los 15 MB.`); continue; }
    if (files.length >= 20) { errors.push("El lote admite hasta 20 archivos."); break; }
    if (files.some((other) => other.name === file.name && other.size === file.size && other.lastModified === file.lastModified)) continue;
    files.push(file);
    if (/\.xls[xm]$/i.test(file.name)) inspectFile(file);
  }
  message(errors.join(" "));
  renderFiles();
}

$("document-files").addEventListener("change", (event) => { addFiles(event.target.files); event.target.value = ""; });
const dropzone = $("dropzone");
for (const eventName of ["dragenter", "dragover"]) {
  dropzone.addEventListener(eventName, (event) => { event.preventDefault(); if (!busy) dropzone.classList.add("dragover"); });
}
for (const eventName of ["dragleave", "drop"]) {
  dropzone.addEventListener(eventName, (event) => { event.preventDefault(); dropzone.classList.remove("dragover"); });
}
dropzone.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));

function updateBatchExport() {
  if (!$("batch-export")) return;
  $("batch-export").hidden = batchLinks.size === 0;
  $("download-links").disabled = busy || batchLinks.size === 0;
  $("batch-export-summary").textContent = `${batchLinks.size} ${batchLinks.size === 1 ? "PDF con enlace de Drive" : "PDF con enlaces de Drive"}. Solo incluye documentos preparados correctamente.`;
}

$("download-links")?.addEventListener("click", () => {
  if (busy || !batchLinks.size) return;
  try {
    const rows = [...batchLinks].sort(([a], [b]) => a - b).map(([, row]) => row);
    const url = URL.createObjectURL(window.QCPLinksExcel.create(rows));
    const download = document.createElement("a");
    download.href = url;
    download.download = "Enlaces_certificados.xlsx";
    document.body.append(download);
    download.click();
    download.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  } catch {
    message("No se pudo generar el Excel de enlaces. Vuelve a pulsar Descargar Excel de enlaces.");
  }
});

function addResult(file, result, error) {
  const card = document.createElement("article");
  card.className = `result-card${error ? " failed" : ""}`;
  const label = document.createElement("div");
  label.className = `result-label${error ? " failed" : ""}`;
  label.textContent = error ? "NO SE PUDO PROCESAR" : "CERTIFICADO PREPARADO";
  const title = document.createElement("strong");
  title.textContent = file.name;
  card.append(label, title);
  if (error) {
    const detail = document.createElement("p");
    detail.textContent = error;
    card.append(detail);
  } else {
    const detail = document.createElement("p");
    detail.textContent = result.sourceType === "pdf" && result.backgroundVerified ? `${result.finalCode || "PDF"} · QR y fondo` :
      `${result.finalCode || "Sin código único detectado"} · ${result.signatureIncluded ? "Con imagen de firma" : "Sin firma añadida"}`;
    card.append(detail);
    if (result.formatPreserved) {
      const preserved = document.createElement("p");
      preserved.className = "background-confirmation";
      preserved.textContent = result.sourceType === "pdf" ? "Contenido y páginas del PDF conservados" : "Formato del Excel conservado · sin añadir otro fondo";
      card.append(preserved);
    }
    if (result.backgroundVerified) {
      const background = document.createElement("p");
      background.className = "background-confirmation";
      const label = backgrounds.find((option) => option.value === result.certificateType)?.label || "Fondo oficial";
      const count = result.backgroundPages.length;
      background.textContent = `${label} · Fondo verificado en ${count} ${count === 1 ? "página" : "páginas"}`;
      card.append(background);
    }
    if (result.formatFooterVerified) {
      const footer = document.createElement("p");
      footer.className = "background-confirmation footer-confirmation";
      footer.textContent = "Pie verificado · código del formato, versión y numeración de todas las páginas";
      card.append(footer);
    }
    for (const warning of result.warnings) {
      const text = document.createElement("p");
      text.className = "warning";
      text.textContent = warning;
      card.append(text);
    }
    const actions = document.createElement("div");
    actions.className = "actions";
    const download = document.createElement("a");
    download.href = result.downloadUrl;
    download.textContent = "Descargar PDF ↓";
    const verify = document.createElement("a");
    // Relative URL keeps local development usable if localhost and 127.0.0.1 differ.
    verify.href = result.driveUrl || `/certificados/${result.id}`;
    verify.target = "_blank";
    verify.rel = "noopener";
    verify.textContent = result.driveUrl ? "Abrir en Drive" : "Ver consulta";
    const externalIcon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    externalIcon.setAttribute("viewBox", "0 0 24 24");
    externalIcon.setAttribute("width", "13");
    externalIcon.setAttribute("height", "13");
    externalIcon.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M7 17 17 7M7 7h10v10");
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.8");
    externalIcon.append(path);
    verify.append(externalIcon);
    actions.append(download, verify);
    if (result.qrMode === "portable-v1") {
      const qrLink = document.createElement("a");
      qrLink.href = result.verificationUrl;
      qrLink.target = "_blank";
      qrLink.rel = "noopener";
      qrLink.textContent = "Probar QR";
      actions.append(qrLink);
    }
    card.append(actions);
    if (result.qrMode === "portable-v1") $("portable-file-help").hidden = false;
    else if (result.driveUrl) $("drive-file-help").hidden = false;
  }
  $("result-list").append(card);
}

$("certificate-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy || !ready || !files.length) return;
  if (files.some((file) => !hasBackground(file))) {
    return message("Elige el fondo de cada PDF y espera la revisión del formato de los Excel.");
  }
  const signature = files.some((file) => /\.xls[xm]$/i.test(file.name)) ? $("signature").files[0] : undefined;
  if (files.some((file) => { const footer = formatFooters.get(file); return footer && Boolean(footer.code.trim()) !== Boolean(footer.version.trim()); })) {
    return message("Completa el código y la versión del formato, o deja ambos vacíos para conservar lo disponible en el Excel.");
  }
  if (signature && (signature.size > 2 * 1024 * 1024 || !/\.(png|jpe?g)$/i.test(signature.name))) {
    return message("La firma debe ser PNG o JPG de hasta 2 MB.");
  }
  message("");
  busy = true;
  renderFiles();
  $("document-files").disabled = true;
  $("signature").disabled = true;
  $("submit").textContent = "Preparando…";
  const continuingBatch = files.some((file) => batchPositions.has(file));
  if (continuingBatch) {
    $("result-list").querySelectorAll(".failed").forEach((card) => card.remove());
  } else {
    batchLinks.clear();
    batchPositions = new WeakMap();
    nextBatchPosition = 0;
    $("result-list").replaceChildren();
    $("drive-file-help").hidden = true;
    $("portable-file-help").hidden = true;
  }
  for (const file of files) {
    if (!batchPositions.has(file)) batchPositions.set(file, nextBatchPosition++);
  }
  updateBatchExport();
  $("results").hidden = false;
  const failed = [];
  const batch = [...files];
  let completed = 0;
  try {
    for (const [index, file] of batch.entries()) {
      const started = Date.now();
      const updateProgress = () => {
        const seconds = Math.floor((Date.now() - started) / 1000);
        $("progress").textContent = `Preparando ${index + 1} de ${batch.length}: ${file.name}${seconds ? ` · ${seconds} s` : ""}${seconds >= 30 ? " · En proceso o esperando turno; mantén esta pestaña abierta." : ""}`;
      };
      updateProgress();
      const timer = setInterval(updateProgress, 1000);
      const form = new FormData();
      const isExcel = /\.xls[xm]$/i.test(file.name);
      const uploadToken = layouts.get(file)?.data?.uploadToken;
      if (uploadToken) form.append("excelToken", uploadToken);
      else form.append(/\.xls[xm]$/i.test(file.name) ? "excel" : "pdf", file);
      if (isExcel) form.append("codigoCorrecto", (codes.get(file) || "").trim());
      if (needsBackground(file)) form.append("tipoCertificado", certificateTypes.get(file));
      const footer = formatFooters.get(file);
      if (footer?.code.trim() && footer?.version.trim()) {
        form.append("codigoFormato", footer.code.trim());
        form.append("versionFormato", footer.version.trim());
      }
      if (signature && isExcel) form.append("firma", signature);
      try {
        let response = await fetch("/api/certificates", { method: "POST", body: form });
        if (response.status === 410 && uploadToken) {
          // Eviction, expiry or a deployment can remove the private temporary copy.
          // This response occurs before publication, so resending cannot duplicate it.
          form.delete("excelToken");
          form.append("excel", file);
          response = await fetch("/api/certificates", { method: "POST", body: form });
        }
        const result = await response.json();
        if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "La carga no es válida.");
        addResult(file, result, null);
        if (result.filename && window.QCPLinksExcel?.isDriveLink(result.driveUrl)) {
          batchLinks.set(batchPositions.get(file), { filename: result.filename, driveUrl: result.driveUrl });
        }
        codes.delete(file);
        certificateTypes.delete(file);
        layouts.delete(file);
        formatFooters.delete(file);
        completed++;
      } catch (error) {
        failed.push(file);
        addResult(file, null, error instanceof TypeError ? "No se pudo conectar con el portal. Revisa tu conexión e inténtalo de nuevo." : error.message);
      } finally {
        clearInterval(timer);
      }
    }
  } finally {
    files = failed;
    busy = false;
    if (!failed.length) batchPositions = new WeakMap();
    updateBatchExport();
    $("document-files").disabled = false;
    $("signature").disabled = false;
    const arrow = document.createElement("span");
    arrow.textContent = "→";
    arrow.setAttribute("aria-hidden", "true");
    $("submit").replaceChildren(document.createTextNode(failed.length ? "Reintentar archivos pendientes" : "Preparar certificados"), arrow);
    $("progress").textContent = `${completed} de ${batch.length} certificados preparados.${failed.length ? " Los archivos pendientes siguen seleccionados." : " Ya puedes descargarlos."}`;
    renderFiles();
  }
});

async function initialize() {
  try {
    const response = await fetch("/api/config");
    if (!response.ok) throw new Error("El portal no está disponible.");
    const config = await response.json();
    backgrounds = Array.isArray(config.certificateBackgrounds) ? config.certificateBackgrounds : [];
    certificateCodePattern = config.certificateCodePattern;
    maxCodeLength = config.maxCodeLength;
    $("local-notice").hidden = !config.localMode;
    ready = config.uploadsEnabled === true;
    $("setup-notice").hidden = ready;
    if (!ready && config.storageBackend === "drive") {
      $("setup-notice").textContent = "La conexión con Google Drive está pendiente. Contacta al administrador para habilitar la preparación de certificados.";
    }
    $("document-files").disabled = !ready;
    $("signature").disabled = !ready;
    renderFiles();
  } catch {
    message("No se pudo iniciar el portal. Recarga la página para volver a intentarlo.");
  }
}
initialize();
