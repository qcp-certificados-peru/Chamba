"use strict";
(() => {
  const encoder = new TextEncoder();
  const driveLink = /^https:\/\/drive\.google\.com\/file\/d\/[A-Za-z0-9_-]{10,200}\/view$/;
  const xml = (value) => String(value).replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\ufffe\uffff]/g, "")
    .replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;" })[char]);
  const declaration = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>';
  const spreadsheet = "http://schemas.openxmlformats.org/spreadsheetml/2006/main";
  const relationships = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
  const packageRelationships = "http://schemas.openxmlformats.org/package/2006/relationships";

  function crc32(bytes) {
    let crc = 0xffffffff;
    for (const byte of bytes) {
      crc ^= byte;
      for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
    }
    return (crc ^ 0xffffffff) >>> 0;
  }

  // XLSX is a ZIP of XML files. These small lists use uncompressed entries:
  // no spreadsheet library, external service or compression dependency needed.
  function zip(parts) {
    const chunks = [], directory = [];
    let offset = 0, directorySize = 0;
    for (const [path, content] of parts) {
      const name = encoder.encode(path), data = encoder.encode(declaration + content), crc = crc32(data);
      const header = new Uint8Array(30 + name.length), local = new DataView(header.buffer);
      local.setUint32(0, 0x04034b50, true);
      local.setUint16(4, 20, true);
      local.setUint16(6, 0x0800, true);
      local.setUint16(12, 0x0021, true); // 1980-01-01, valid DOS date.
      local.setUint32(14, crc, true);
      local.setUint32(18, data.length, true);
      local.setUint32(22, data.length, true);
      local.setUint16(26, name.length, true);
      header.set(name, 30);
      chunks.push(header, data);
      const entry = new Uint8Array(46 + name.length), central = new DataView(entry.buffer);
      central.setUint32(0, 0x02014b50, true);
      central.setUint16(4, 20, true);
      central.setUint16(6, 20, true);
      central.setUint16(8, 0x0800, true);
      central.setUint16(14, 0x0021, true);
      central.setUint32(16, crc, true);
      central.setUint32(20, data.length, true);
      central.setUint32(24, data.length, true);
      central.setUint16(28, name.length, true);
      central.setUint32(42, offset, true);
      entry.set(name, 46);
      directory.push(entry);
      directorySize += entry.length;
      offset += header.length + data.length;
    }
    const end = new Uint8Array(22), view = new DataView(end.buffer);
    view.setUint32(0, 0x06054b50, true);
    view.setUint16(8, parts.length, true);
    view.setUint16(10, parts.length, true);
    view.setUint32(12, directorySize, true);
    view.setUint32(16, offset, true);
    return new Blob([...chunks, ...directory, end], { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
  }

  function create(rows) {
    if (!Array.isArray(rows) || !rows.length || rows.some((row) =>
      typeof row.filename !== "string" || !row.filename || !driveLink.test(row.driveUrl))) {
      throw new Error("El listado requiere nombres de PDF y enlaces válidos de Google Drive.");
    }
    // Inline strings preserve filenames as text, including ones beginning '='.
    // Links use OOXML relationships, never executable HYPERLINK formulas.
    const cell = (ref, value, style) => `<c r="${ref}" t="inlineStr" s="${style}"><is><t xml:space="preserve">${xml(value)}</t></is></c>`;
    const header = `<row r="1" ht="24" customHeight="1">${cell("A1", "Nombre del PDF", 1)}${cell("B1", "Enlace de Google Drive", 1)}</row>`;
    const data = rows.map((row, index) => `<row r="${index + 2}">${cell(`A${index + 2}`, row.filename, 0)}${cell(`B${index + 2}`, row.driveUrl, 2)}</row>`).join("");
    const links = rows.map((_, index) => `<hyperlink ref="B${index + 2}" r:id="link${index + 1}"/>`).join("");
    const targets = rows.map((row, index) => `<Relationship Id="link${index + 1}" Type="${relationships}/hyperlink" Target="${xml(row.driveUrl)}" TargetMode="External"/>`).join("");
    return zip([
      ["[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>'],
      ["_rels/.rels", `<Relationships xmlns="${packageRelationships}"><Relationship Id="workbook" Type="${relationships}/officeDocument" Target="xl/workbook.xml"/></Relationships>`],
      ["xl/workbook.xml", `<workbook xmlns="${spreadsheet}" xmlns:r="${relationships}"><bookViews><workbookView/></bookViews><sheets><sheet name="Certificados" sheetId="1" r:id="sheet1"/></sheets></workbook>`],
      ["xl/_rels/workbook.xml.rels", `<Relationships xmlns="${packageRelationships}"><Relationship Id="sheet1" Type="${relationships}/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="styles" Type="${relationships}/styles" Target="styles.xml"/></Relationships>`],
      ["xl/worksheets/sheet1.xml", `<worksheet xmlns="${spreadsheet}" xmlns:r="${relationships}"><dimension ref="A1:B${rows.length + 1}"/><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols><col min="1" max="1" width="72" customWidth="1"/><col min="2" max="2" width="85" customWidth="1"/></cols><sheetData>${header}${data}</sheetData><autoFilter ref="A1:B${rows.length + 1}"/><hyperlinks>${links}</hyperlinks></worksheet>`],
      ["xl/worksheets/_rels/sheet1.xml.rels", `<Relationships xmlns="${packageRelationships}">${targets}</Relationships>`],
      ["xl/styles.xml", `<styleSheet xmlns="${spreadsheet}"><fonts count="3"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font><font><u/><sz val="11"/><color rgb="FF0563C1"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF163C40"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/><xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>`]
    ]);
  }

  window.QCPLinksExcel = Object.freeze({ create, isDriveLink: (url) => typeof url === "string" && driveLink.test(url) });
})();
