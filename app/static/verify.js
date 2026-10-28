"use strict";
(async () => {
  const $ = (id) => document.getElementById(id);
  const id = window.location.pathname.split("/").filter(Boolean).pop();
  try {
    const key = new URLSearchParams(window.location.search).get("key");
    const response = await fetch(`/api/certificates/${encodeURIComponent(id)}${key ? `?key=${encodeURIComponent(key)}` : ""}`);
    if (!response.ok) throw new Error("No se encontró este certificado en el portal.");
    const info = await response.json();
    $("verify-name").textContent = info.filename;
    $("verify-code").textContent = info.finalCode || (info.detectedCodes.length ? info.detectedCodes.join(", ") : "No detectado en el texto");
    $("verify-date").textContent = new Intl.DateTimeFormat("es-PE", { dateStyle: "long", timeStyle: "short" }).format(new Date(info.createdAt));
    $("verify-signature").textContent = info.signatureIncluded ? "Imagen de firma incluida" : "Sin imagen de firma";
    $("verify-hash").textContent = info.sha256;
    $("verify-download").href = info.downloadUrl;
    $("verify-status").textContent = "Este documento está disponible para consulta y descarga.";
    $("verify-content").hidden = false;
  } catch (error) {
    $("verify-status").textContent = "No se pudo consultar el documento.";
    $("verify-error").textContent = error.message;
    $("verify-error").hidden = false;
  }
})();
