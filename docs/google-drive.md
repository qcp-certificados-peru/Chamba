# Guardar certificados en Google Drive desde Render Free

Las emisiones nuevas usan un **QR portable del portal QCP**. El enlace identifica la emisión y una clave pública, y busca el PDF original o una copia íntegra en la misma cuenta de Drive. Por eso se puede copiar el PDF a otra carpeta de esa cuenta y eliminar el original, siempre que se conserve una copia nativa de Drive sin modificar.

El código se guarda en GitHub; Render ejecuta el portal; Drive guarda los PDF. Cada emisión sigue creando **un PDF suelto con su nombre**, sin subcarpetas ni JSON. El registro completo firmado con Ed25519 se conserva en una carpeta técnica privada, **QCP - Registros del portal (no eliminar)**, situada aparte en la raíz de Mi unidad. Solo hay una carpeta técnica por cuenta, con registros JSON privados; la carpeta configurada para certificados sigue recibiendo únicamente los PDF sueltos. Los registros no contienen credenciales ni la clave privada de firma. La clave privada se genera por emisión, se usa una vez y no se almacena. El QR fija la clave pública para comprobar el registro y el SHA-256 del PDF impide reemplazarlo por un documento diferente. Esta comprobación técnica no certifica la identidad del emisor ni sustituye una firma digital del certificado.

El portal prefiere el último archivo válido conocido y, si desaparece, busca una copia por las huellas binarias de Drive, sin depender del nombre, carpeta ni propiedades personalizadas de la copia. El SHA-256 siempre se comprueba antes de entregar el PDF; MD5 solo sirve como localizador alternativo cuando Drive no devuelve SHA-256. La copia debe ser propiedad de la cuenta conectada. Solo se actualiza el localizador en el registro técnico privado; no se restaura, mueve, elimina ni cambia permisos de certificados. El PDF se entrega mediante el portal después de comprobar su hash, incluso si la copia tiene permisos privados. Quien posea el QR puede seguir descargando ese certificado publicado; poner una copia en una carpeta privada no retira por sí solo su consulta pública.

**Los QR anteriores que abren directamente Google Drive no cambian.** Sus carpetas, JSON y propiedades privadas se conservan sin migración ni escritura. Para esos QR, copiar y borrar el original sigue rompiendo el destino; se debe mover el mismo archivo o restaurarlo de la papelera.

El registro técnico JSON es privado y está separado del archivo original; se conserva aunque se borre ese PDF. El formato comprimido de registro respeta el límite de 64 KB y las emisiones siguen llevando propiedades privadas para compatibilidad. Si no caben, se rechaza la emisión antes de publicar, sin truncar datos. El JSON descomprimido se limita a 64 KB.

**Disponibilidad:** los QR portables requieren conservar el dominio del portal, Render y la autorización de la cuenta de Drive. Render Free puede suspender el servidor y tardar en responder al primer acceso. Los registros y PDF no dependen del disco local, por lo que sobreviven a un reinicio. Los QR antiguos directos de Drive mantienen su comportamiento anterior y no pasan por Render.

## 1. Preparar la carpeta

Usa la cuenta personal únicamente para pruebas con documentos ficticios. En Drive, abre **Compartir** en la carpeta y deja **Acceso general → Restringido**. No hace falta que los trabajadores tengan acceso a esa carpeta.

El identificador de una carpeta está después de `/folders/` en su enlace. Por ejemplo, en `https://drive.google.com/drive/folders/IDENTIFICADOR`, configura `GOOGLE_DRIVE_FOLDER_ID=IDENTIFICADOR`. No incluyas `?usp=drive_link` ni el enlace completo en esa variable.

## 2. Crear el proyecto y habilitar la API

Una API permite que el servidor se comunique con Drive. No hay que instalar nada en la computadora de los trabajadores.

1. Entra en [Google Cloud Console](https://console.cloud.google.com/) con la cuenta de la carpeta.
2. Abre el selector de proyectos de la barra superior y elige **Nuevo proyecto**.
3. Escribe **Portal QCP pruebas**, crea el proyecto y selecciónalo.
4. Ve a **APIs y servicios → Biblioteca**. Busca **Google Drive API**, entra en ella y pulsa **Habilitar**.

Esta integración usa Google Drive API y el espacio de tu cuenta de Drive. No necesita activar facturación ni contratar recursos de pago en Google Cloud. Drive gratuito tiene una cuota compartida con Gmail y Fotos; cuando se llene, las nuevas emisiones fallarán con un mensaje, sin borrar certificados antiguos. Render también conserva sus propios límites gratuitos.

## 3. Configurar el consentimiento de Google

1. Ve a **Google Auth Platform**. También puedes llegar desde **APIs y servicios → Pantalla de consentimiento de OAuth**.
2. Si aparece **Comenzar/Get started**, pulsa ese botón.
3. Pon **Portal QCP** como nombre de la aplicación y tu correo como correo de asistencia y de contacto.
4. Para una cuenta Gmail personal, selecciona público **Externo/External**. Completa los datos requeridos y guarda. En **Información de la marca**, configura la página principal del portal, su política de privacidad (`/privacidad`) y, opcionalmente, sus condiciones (`/condiciones`). Añade el dominio del servicio sin esquema ni ruta en **Dominios autorizados**, conserva los correos de asistencia/contacto y guarda los cambios. En el portal de pruebas actual, las direcciones son `https://chamba-v5n2.onrender.com`, `https://chamba-v5n2.onrender.com/privacidad` y `https://chamba-v5n2.onrender.com/condiciones`, con dominio autorizado `chamba-v5n2.onrender.com`. Comprueba que los enlaces abran antes de añadirlos a Google. Si el botón de publicación está deshabilitado, revisa la configuración de marca y los mensajes de validación; no presupongas que el problema es de facturación.
5. En **Acceso a los datos/Data access**, añade el permiso `https://www.googleapis.com/auth/drive`. Es un permiso amplio sobre la cuenta; esta implementación utiliza una carpeta fija, pero la credencial debe guardarse como un secreto. No se usa `drive.file` porque no concede por sí solo acceso a una carpeta existente elegida mediante un enlace.
6. En **Público/Audience**, cambia el estado de publicación a **En producción/In production** antes de autorizar la cuenta. Este estado de Google es distinto del despliegue del portal en Render.

**No dejes el consentimiento en «Testing/Pruebas».** Con permisos de Drive, los refresh tokens de una aplicación externa en Testing normalmente caducan a los siete días. «En producción» evita ese vencimiento específico, pero no garantiza una autorización eterna: se puede revocar, quedar invalidada por cambios de cuenta o alcanzar límites de tokens.

Una aplicación privada usada por su propio propietario puede mostrar el aviso de Google de aplicación no verificada. Esto no es una aplicación OAuth pública ofrecida a todos los trabajadores: solo la cuenta propietaria autoriza al servidor. Google puede aplicar límites de uso, verificación o políticas de la empresa. Si bloquea la autorización, revisa el motivo y las políticas de la cuenta; no cambies de permiso ni publiques secretos para intentar resolverlo.

## 4. Crear las credenciales de la aplicación

1. En **Google Auth Platform → Clientes/Clients**, pulsa **Crear cliente**. En la interfaz anterior: **APIs y servicios → Credenciales → Crear credenciales → ID de cliente OAuth**.
2. Selecciona **Aplicación web/Web application** y llámala **Portal QCP servidor**.
3. En **URIs de redirección autorizados**, agrega exactamente:

   ```text
   https://developers.google.com/oauthplayground
   ```

4. Pulsa **Crear**. Conserva el **ID de cliente** y el **secreto de cliente** para introducirlos en las herramientas oficiales y en Render. No los incluyas en GitHub, en el chat ni en capturas.

Se usa autorización OAuth de tu propia cuenta. Una cuenta de servicio no tiene cuota personal de almacenamiento y no puede ser propietaria de los archivos de «Mi unidad»; compartirle una carpeta personal no resuelve esa limitación. Por eso no se usa una cuenta de servicio para esta configuración gratuita.

## 5. Autorizar la cuenta y obtener el refresh token

El refresh token permite que el servidor renueve su acceso automáticamente sin pedirte iniciar sesión en cada carga.

1. Abre el [OAuth Playground oficial de Google](https://developers.google.com/oauthplayground).
2. Pulsa el engranaje superior derecho. Activa **Use your own OAuth credentials**.
3. Introduce allí tu **OAuth Client ID** y **OAuth Client secret**. Conserva **Access type: Offline** y **Force prompt: Consent**, si aparecen esas opciones. Cierra ese panel.
4. En **Step 1**, escribe o selecciona el permiso `https://www.googleapis.com/auth/drive` y pulsa **Authorize APIs**.
5. Inicia sesión con la misma cuenta de tu carpeta y acepta el consentimiento de tu propia aplicación. Si aparece un aviso de aplicación no verificada, verifica el nombre y el proyecto que acabas de crear; continúa solo si Google ofrece el acceso para tu aplicación personal.
6. En **Step 2**, pulsa **Exchange authorization code for tokens**.
7. Copia únicamente el **Refresh token** al campo secreto de Render indicado abajo. El **Access token** es de corta duración y no se configura en el portal.

Usa tus propias credenciales en el Playground: sus credenciales predeterminadas no son apropiadas para un servidor permanente y sus refresh tokens se revocan automáticamente después de 24 horas. Si autorizaste cuando el consentimiento estaba en Testing, cámbialo a producción y genera una autorización nueva.

## 6. Configurar Render

Abre **Render → servicio Chamba → Environment → Edit**. Añade estas variables con los nombres exactos:

| KEY | VALUE |
| --- | --- |
| `CERTIFICATE_STORAGE_BACKEND` | `drive` |
| `GOOGLE_DRIVE_FOLDER_ID` | Solo el identificador de la carpeta |
| `GOOGLE_DRIVE_CLIENT_ID` | El ID de cliente OAuth creado en Google |
| `GOOGLE_DRIVE_CLIENT_SECRET` | El secreto de ese cliente, en el campo de Render |
| `GOOGLE_DRIVE_REFRESH_TOKEN` | El refresh token obtenido en el Playground |

No uses una variable llamada `clave`, no configures `PORTAL_ACCESS_KEY` y no pongas una contraseña de Gmail. El portal continúa permitiendo a los trabajadores subir archivos sin contraseña. Usa el origen HTTPS público; Render aporta `RENDER_EXTERNAL_URL` automáticamente. Si definiste `PUBLIC_BASE_URL`, debe coincidir con el dominio del servicio.

Pulsa **Save, rebuild, and deploy** o la opción equivalente para guardar y desplegar. Espera a que el nuevo despliegue esté **Live**. Un commit en `main` activa el Auto-Deploy si lo tienes habilitado; cambiar variables requiere que Render reinicie/despliegue con esa configuración. Los ajustes de Render no modifican GitHub ni los ajustes del entorno Codex.

Si eliges `drive` y faltan variables, la página abre pero deshabilita las emisiones. Si Google rechaza una carga, el portal muestra un error y no cambia silenciosamente al almacenamiento temporal de Render. El estado de salud HTTP no prueba los permisos reales de Drive: hay que completar la comprobación siguiente.

## 7. Probar antes de emitir certificados reales

1. Sube un Excel ficticio y descarga el PDF final. Comprueba el formato, la firma y que el QR aparezca en la ubicación de siempre.
2. Pulsa **Probar QR** o escanea el QR nuevo. Debe abrir el PDF desde `/q/IDENTIFICADOR/CLAVE` en el dominio del portal. **Abrir en Drive** continúa abriendo directamente el archivo guardado.
3. Abre ese mismo enlace en una ventana de incógnito, sin iniciar sesión. Debes poder consultar o descargar el PDF. Google decide cómo muestra la vista previa; comprueba también el PDF descargado.
4. Mira la carpeta: debe aparecer directamente el PDF con su nombre, sin una nueva subcarpeta ni JSON. Solo el PDF debe tener acceso mediante enlace.
5. Reinicia/despliega el servicio Render y vuelve a escanear el mismo QR. Después, comprueba también la consulta y la descarga desde el portal.
6. Con un documento ficticio, usa **Organizar → Mover** en Drive para trasladar ese mismo PDF a otra carpeta de la misma cuenta. Conserva su permiso de lectura mediante enlace y comprueba el QR, la consulta y la descarga nuevamente. La consulta utiliza el registro privado y el contenido exacto del PDF, no su nombre ni su carpeta actual.

### Copias portables y QR anteriores

Para reorganizar certificados conservando su QR, usa **Mover** (o cortar y pegar dentro de Drive). El archivo mantiene su identificador y el enlace del QR. La cuenta autorizada del portal debe conservar acceso al archivo, y sus destinatarios deben conservar el permiso de lectura mediante enlace.

Para el QR portable nuevo, usa **Hacer una copia** o copiar y pegar **dentro de Drive**, en la misma cuenta. Conserva la copia sin modificar y prueba su QR después de eliminar el origen. La búsqueda utiliza el identificador de emisión; no basta con el nombre del archivo. Una copia sin propiedades personalizadas también funciona: se localiza por su contenido. Copiar a otra cuenta, cambiar de servicio o modificar el PDF no forma parte de esta recuperación automática.

Los QR anteriores directos de Google Drive siguen necesitando el archivo original. Mover solo el PDF de una emisión antigua conserva su enlace; deja su carpeta y JSON anteriores intactos para la consulta del portal.

### Si ya se copió el PDF y se borró el original

En los certificados anteriores, el enlace impreso en el QR es una dirección de Google Drive. Una actualización del portal no puede interceptar esa dirección ni reasignarla a un archivo diferente por tener el mismo nombre o contenido. Detectar la copia en el portal tampoco cambiaría el QR que ya se entregó.

Si el original sigue en la papelera, usa **Restaurar** sobre ese archivo y luego **Organizar → Mover** para cambiarlo de carpeta conservando su identificador. No elimines otros certificados ni sus registros para intentar repararlo. Google indica que la papelera elimina los archivos tras 30 días; comprueba el caso en su [guía de recuperación](https://support.google.com/drive/answer/1716222?hl=es). Si se eliminó definitivamente, puede ser necesario solicitar recuperación a Google; si no se recupera el archivo original, hay que emitir un PDF con un QR nuevo.

En un entorno de desarrollo con las variables configuradas de forma segura:

```sh
cd /workspace/Chamba
.venv/bin/python scripts/check_drive.py
```

Esta comprobación renueva la autorización y verifica carpeta, permisos y acceso de escritura sin subir archivos. Para comprobar una emisión real, ejecuta `scripts/smoke.py` contra el portal configurado: guarda un PDF ficticio SIN VALIDEZ. Las pruebas automáticas de Drive usan respuestas simuladas; no reemplazan la comprobación real de tu cuenta.

## Cambiar a la cuenta de la empresa

Antes de emitir documentos reales, autoriza la cuenta de la empresa, crea una carpeta restringida de la empresa y reemplaza las variables de cuenta/carpeta en Render. Si se cambia el cliente OAuth, reemplaza también su ID y secreto. Repite la prueba.

Cambiar la carpeta o la autorización afecta a las **emisiones nuevas**. No migra archivos ni modifica los QR anteriores. Los QR antiguos directos seguirán dependiendo de sus archivos en la cuenta anterior. Los nuevos portables buscan copias en la cuenta autorizada, por lo que cambiar de cuenta no migra automáticamente sus documentos ni sus registros. No se incluye recuperación de certificados antiguos en este cambio, por indicación del usuario.

Conserva copias de seguridad y al menos una copia nativa íntegra de cada PDF portable. Para los QR anteriores directos, conserva su archivo original y permiso de lectura. Renombrar un PDF o moverlo dentro de la misma cuenta no cambia su identificador, pero hay que revisar los permisos heredados después de moverlo. Un QR no garantiza autenticidad ni permanencia por sí solo; su duración depende del archivo y del servicio al que apunta.

Referencias oficiales: [OAuth para aplicaciones de servidor](https://developers.google.com/identity/protocols/oauth2/web-server), [caducidad de refresh tokens](https://developers.google.com/identity/protocols/oauth2#expiration), [crear archivos con IDs reservados](https://developers.google.com/workspace/drive/api/guides/create-file), [propiedades privadas y sus límites](https://developers.google.com/workspace/drive/api/guides/properties), [cuentas de servicio y cuota](https://developers.google.com/workspace/drive/api/guides/handle-errors#storageQuotaExceeded).

## Prueba automática de copia y eliminación

`GET /api/pruebas/qr-portable.pdf` proporciona un PDF fijo **SIN VALIDEZ**. Solo ese contenido exacto puede enviarse a `/api/certificates` con `pdf`, `tipoCertificado=no_acreditado` y `pruebaCopia=true`. El servidor emite ese documento ficticio, hace una copia nativa en la raíz de la misma cuenta, comprueba que la copia no necesita propiedades personalizadas y verifica sus bytes contra el registro firmado antes de eliminar definitivamente **solo el origen ficticio recién creado**. Después resuelve el mismo QR hacia la copia y devuelve `copyTest`. No acepta identificadores de archivos existentes ni permite usar esa opción con documentos de clientes. La copia ficticia queda en Drive con un nombre SIN VALIDEZ para revisar el resultado.
