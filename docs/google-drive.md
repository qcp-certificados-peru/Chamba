# Guardar certificados en Google Drive desde Render Free

El QR del modo Drive apunta directamente al PDF final en Google Drive, no al disco de Render. La aplicación reserva el identificador de Drive antes de generar el PDF, coloca ese enlace en el QR y publica el archivo con ese mismo identificador. Cada emisión recibe un identificador distinto. No existe una fecha de caducidad programada para el QR: seguirá abriendo el archivo mientras este exista, conserve su permiso de lectura y Google mantenga disponible la cuenta y el servicio.

El código se guarda en GitHub; Render ejecuta la aplicación; Drive guarda los certificados. El Excel original y la imagen de firma separada no se guardan en Drive. **Cada emisión nueva crea únicamente un PDF con su nombre, directamente en la carpeta configurada.** No crea una subcarpeta ni un archivo JSON. El registro completo (código, fecha, avisos y hash SHA-256) se guarda como propiedades privadas de la aplicación asociadas a ese mismo archivo; no modifica el contenido ni los bytes del PDF. Solo el PDF recibe acceso «cualquiera con el enlace», sin permiso de edición. La carpeta y las propiedades privadas no se publican; la aplicación rechaza carpetas con acceso general público o de dominio porque sus permisos se heredarían.

Los certificados emitidos con la estructura anterior conservan sus carpetas y `metadata.json`. Esos JSON siguen sirviendo para la consulta y descarga desde el portal: **no deben borrarse**. El cambio no migra, modifica, mueve ni elimina archivos anteriores; solo cambia cómo se guardan las emisiones nuevas.

El registro nuevo se comprime sin pérdidas y se distribuye dentro de los límites de Drive: hasta 30 propiedades privadas y 124 bytes por pareja clave/valor. Si un registro excepcional supera esa capacidad o está incompleto, se informa del problema sin truncar información ni publicar un certificado sin registro. Los datos técnicos siguen sujetos al límite de 64 KB del lector anterior.

Las propiedades privadas se leen con el cliente OAuth que las creó. Renovar su token o secreto no cambia esa identidad; sustituir el cliente OAuth por otro puede impedir que el portal lea esos registros nuevos. El enlace público del QR sigue dependiendo únicamente del archivo y sus permisos.

Render Free sigue pudiendo suspender el portal y perder sus archivos locales. Esto ya no afecta a los QR nuevos del modo Drive: la consulta mediante el QR va directamente a Google. La consulta con información adicional y la descarga desde el portal leen Drive, de modo que no necesitan registros locales. Debe mantenerse la autorización para que el portal pueda emitir y consultar registros. Revocar la autorización impide nuevas operaciones del portal, pero no elimina los PDFs existentes ni modifica sus enlaces públicos.

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
2. Pulsa **Abrir en Drive** o escanea el QR. Debe abrir una dirección `https://drive.google.com/file/d/.../view`, sin un dominio de Render.
3. Abre ese mismo enlace en una ventana de incógnito, sin iniciar sesión. Debes poder consultar o descargar el PDF. Google decide cómo muestra la vista previa; comprueba también el PDF descargado.
4. Mira la carpeta: debe aparecer directamente el PDF con su nombre, sin una nueva subcarpeta ni JSON. Solo el PDF debe tener acceso mediante enlace.
5. Reinicia/despliega el servicio Render y vuelve a escanear el mismo QR. Después, comprueba también la consulta y la descarga desde el portal.
6. Con un documento ficticio, usa **Organizar → Mover** en Drive para trasladar ese mismo PDF a otra carpeta de la misma cuenta. Conserva su permiso de lectura mediante enlace y comprueba el QR, la consulta y la descarga nuevamente. La búsqueda del registro nuevo utiliza su identificador de emisión, no su nombre ni su carpeta actual.

### Mover y copiar son operaciones diferentes

Para reorganizar certificados conservando su QR, usa **Mover** (o cortar y pegar dentro de Drive). El archivo mantiene su identificador y el enlace del QR. La cuenta autorizada del portal debe conservar acceso al archivo, y sus destinatarios deben conservar el permiso de lectura mediante enlace.

**Hacer una copia**, copiar y pegar o descargar y volver a subir puede crear otro archivo con un identificador diferente. El QR impreso en esa copia sigue apuntando al PDF original; no se redirige automáticamente. Conserva el original si necesitas ese QR. Aunque Drive copie también las propiedades técnicas, el portal reconoce el identificador del archivo emitido y evita confundir la copia con el original. Mover solo el PDF de una emisión antigua también conserva el enlace; deja su carpeta y JSON anteriores intactos para la consulta del portal.

En un entorno de desarrollo con las variables configuradas de forma segura:

```sh
cd /workspace/Chamba
.venv/bin/python scripts/check_drive.py
```

Esta comprobación renueva la autorización y verifica carpeta, permisos y acceso de escritura sin subir archivos. Para comprobar una emisión real, ejecuta `scripts/smoke.py` contra el portal configurado: guarda un PDF ficticio SIN VALIDEZ. Las pruebas automáticas de Drive usan respuestas simuladas; no reemplazan la comprobación real de tu cuenta.

## Cambiar a la cuenta de la empresa

Antes de emitir documentos reales, autoriza la cuenta de la empresa, crea una carpeta restringida de la empresa y reemplaza las variables de cuenta/carpeta en Render. Si se cambia el cliente OAuth, reemplaza también su ID y secreto. Repite la prueba.

Cambiar la carpeta o la autorización afecta a las **emisiones nuevas**. No migra archivos ni modifica los QR anteriores. Los certificados de prueba seguirán dependiendo de sus archivos en tu cuenta personal; mantener su mismo QR al trasladarlos requiere una migración que preserve los identificadores de Drive y los permisos. No se incluye recuperación de certificados antiguos en este cambio, por indicación del usuario.

Conserva copias de seguridad y no borres, reemplaces ni restrinjas los PDFs que deban seguir consultándose. Renombrar un PDF o moverlo dentro de la misma cuenta no cambia su identificador, pero hay que revisar los permisos heredados después de moverlo. Un QR no garantiza autenticidad ni permanencia por sí solo; su duración depende del archivo y del servicio al que apunta.

Referencias oficiales: [OAuth para aplicaciones de servidor](https://developers.google.com/identity/protocols/oauth2/web-server), [caducidad de refresh tokens](https://developers.google.com/identity/protocols/oauth2#expiration), [crear archivos con IDs reservados](https://developers.google.com/workspace/drive/api/guides/create-file), [propiedades privadas y sus límites](https://developers.google.com/workspace/drive/api/guides/properties), [cuentas de servicio y cuota](https://developers.google.com/workspace/drive/api/guides/handle-errors#storageQuotaExceeded).
