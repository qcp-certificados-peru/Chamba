# Permisos de los PDF generados

Con `CERTIFICATE_PDF_OWNER_PASSWORD` configurada, los PDF nuevos usan AES-256 y
restringen la edición y la copia de texto e imágenes. La contraseña de propietario
se configura únicamente como secreto del servidor; no se incluye en el código,
los metadatos públicos ni este documento.

| Operación | Permiso del lector |
| --- | --- |
| Abrir y consultar el PDF | Libre, sin contraseña |
| Imprimir, incluida alta calidad | Permitido |
| Copiar texto e imágenes | Restringido |
| Editar contenido, anotaciones, formularios o páginas | Restringido |
| Acceso con tecnologías de asistencia | Permitido |

El propietario puede autorizar cambios de permisos con su contraseña. Estas
restricciones dependen del lector PDF: no impiden capturas de pantalla ni OCR y
no constituyen una firma digital. El contenido, los fondos y los QR se conservan.

Con la variable vacía o ausente, los PDF se generan sin esas restricciones.
El cambio solo afecta a nuevas emisiones; no reescribe documentos anteriores.
