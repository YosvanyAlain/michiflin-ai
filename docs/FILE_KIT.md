# Kit de archivos / File kit

## Español

Abre **Descargas → Kit de archivos**. Elige **En este dispositivo** para trabajar sin conexión con copias guardadas dentro de esa instancia de la miniapp, o **Servidor temporal** para trabajar en el espacio del usuario autorizado. Los originales elegidos en el teléfono no se modifican ni se borran.

### Organizar y editar

- Toca la flecha de una carpeta para expandirla o contraerla; toca su nombre para convertirla en destino. El árbol tiene su propio desplazamiento. Marca las casillas de uno o varios elementos para operar sobre ellos.
- Puedes crear carpetas, renombrar, copiar o preparar un movimiento y pegar en la carpeta elegida. Eliminar pide confirmación y solo afecta a las copias del espacio activo. Deshacer y rehacer recuperan operaciones mientras se conserve el historial. **Vaciar historial** elimina la posibilidad de recuperar versiones anteriores, no los archivos visibles.
- Toca un archivo para previsualizar texto UTF-8, imágenes o medios compatibles con el cliente. Guarda el texto con el botón correspondiente. Si el archivo cambió en otra ventana, la escritura se rechaza en lugar de sobrescribir la versión nueva. Otros binarios se pueden exportar y abrir con otra aplicación; no hay editor hexadecimal, recorte ni conversión multimedia.
- El editor local admite hasta 4 MiB de texto. Las previsualizaciones remotas admiten hasta 200.000 bytes y la edición remota hasta 70.000 bytes por solicitud. Para archivos mayores, envía una copia al chat o expórtala para abrirla fuera.

### Comprimidos y partes

Selecciona archivos o carpetas y elige **Crear ZIP o 7z**. Puedes establecer una contraseña y un tamaño de volumen; `0` significa un único archivo. Para abrir ZIP, 7z o RAR, selecciona todas las partes y señala la primera. La extracción crea una carpeta nueva y no reemplaza archivos existentes. RAR es solo de lectura. No se garantiza que todas las variantes de RAR, contraseñas, compresión o multipartes funcionen en todos los clientes.

En el móvil, el motor opera en un Worker con recursos locales y un presupuesto ajustable de 16–128 MiB para entrada y salida; el bloqueo de red externa depende del cliente WebXDC. El consumo real de memoria puede ser mayor; aumenta el presupuesto con cautela porque el teléfono puede cerrar la app. En el servidor, el presupuesto depende de la memoria libre; se usa un proceso sin red y con permisos reducidos, y la operación se rechaza si no se puede crear el aislamiento. Ni el sandbox ni la revisión previa garantizan ausencia de fallos de seguridad.

**Unir partes** permanece dentro del kit. Puede cargar archivos seleccionados en el espacio local o pedirlos al teléfono, ordenar partes `.001` y unir segmentos MP4/M4A/MP3 compatibles. Si se conoce un número de parte faltante, bloquea la unión. Verifica huellas solo cuando están disponibles y el cliente las puede calcular; sin huella no se puede afirmar que el resultado esté completo o sea idéntico al original. Se puede guardar el resultado como una nueva copia en el kit o exportarlo.

### Mover copias entre teléfono, servidor y chat

1. Para importar al espacio local, elige uno o varios archivos. Para enviarlos al servidor, usa **Servidor temporal → Importar desde el móvil** o selecciona un archivo local y usa **Enviar copia al servidor**.
2. Elige el archivo y espera el borrador que prepara la app. **Envíalo tú al chat del bot**, conservando la marca temporal en su texto. La app no recibe una confirmación fiable de que el usuario haya enviado el borrador; espera la confirmación del bot y actualiza el árbol. La autorización de una sola vez caduca y está ligada al remitente y a la sesión. El bot comprueba tamaño y SHA-256 antes de publicar la copia; un adjunto diferente se rechaza y no se elimina el mensaje original.
3. Cada adjunto al servidor debe caber en el límite efectivo por parte indicado. Para archivos mayores, crea primero volúmenes locales del tamaño adecuado y envía cada uno por separado. Los límites reales del proveedor y la codificación del correo pueden ser más estrictos.
4. Desde un trabajo de descarga **listo o terminado**, usa **Copiar descarga al kit**. Se copian las partes sin borrar los originales. Se exige una huella verificable: los segmentos multimedia y volúmenes comprimidos usan sus huellas individuales; las partes crudas pueden usar la huella del conjunto. Si una descarga antigua carece de huellas suficientes, no se copia.
5. Para recibir un archivo remoto en el chat, ábrelo y elige **Enviar copia al chat**. La cola respeta el ajuste de tamaño y los límites del buzón; una copia exportada por el kit no se remultiplexa como vídeo. El seguimiento de entregas y reintentos conserva las limitaciones normales de la cola; ante una respuesta incierta, revisa Descargas y el chat antes de repetir.

### Seguridad y duración

Las contraseñas de comprimidos y el contenido de lectura/escritura remota viajan en sobres cifrados con claves efímeras; no se guardan en claro en las actualizaciones WebXDC. Los nombres, rutas virtuales y metadatos sí se encuentran en las actualizaciones del chat cifrado. Los datos se procesan en memoria en los extremos y, al crear comprimidos remotos, en un sandbox temporal; no se promete borrado criptográfico completo de toda la memoria.

El almacenamiento local pertenece a una sola instancia y el cliente puede recuperarlo. Conservar la app anterior no migra automáticamente sus datos. El espacio del servidor está en el directorio temporal del trabajo, no forma parte de las copias de seguridad y puede desaparecer al reiniciar o cambiar de corredor. Exporta las copias importantes y verifica su recepción. Las pruebas de Chromium y WebKit son sintéticas: la compatibilidad con dispositivos físicos y versiones reales de WebXDC debe comprobarse por separado.

## English

Open **Downloads → File kit**. **On this device** keeps offline copies inside this mini-app instance; **Temporary server** uses the authorized person's temporary workspace. Selecting a phone file never changes or removes the original.

Expand the scrollable tree, choose the destination folder and check entries to rename, copy, move, delete or undo/redo. Clearing history removes recoverable previous versions. Text editing accepts UTF-8; local text is limited to 4 MiB, remote edits to 70,000 bytes and remote previews to 200,000 bytes. Image and media previews depend on client support; export other binary files to open them elsewhere. There is no hex editor or media trimming/conversion.

Create ZIP/7z archives with optional passwords and volume sizes; open ZIP/7z and read RAR by selecting every volume and the first volume. Existing files are never overwritten. The mobile worker has a user-adjustable 16–128 MiB input/output budget but can use more actual memory. Server processing requires a network-free, restricted sandbox and refuses the operation if it cannot be created. Not every archive variant or device has been verified.

The existing **Join parts** tool is nested in the kit, accepts selected local copies, and can save the result back. It supports `.001` and compatible MP4/M4A/MP3 segments. Missing known parts are blocked; hashes are checked only when available and supported by the client.

To copy a phone file to the server, the app prepares a one-use attachment draft. You must send it to the bot's chat with the marker unchanged; wait for the bot's confirmation. The server checks the chosen file's SHA-256 and size. Each attachment must fit the effective per-part limit; divide larger files first. Finished or ready downloads can be copied when enough hashes are available. To send a server copy to the chat, use the existing queue, which applies the configured mailbox ceilings; check the queue before retrying an uncertain operation.

Archive passwords, remote text and preview bytes use ephemeral encrypted envelopes; file names and operation metadata remain in encrypted chat updates. Processing still occurs in endpoint memory and temporary sandbox storage. Local storage is per instance and may be reclaimed. Server storage is temporary, is not backed up, and can disappear when the runner changes. Export and verify important copies. Synthetic Chromium/WebKit tests do not establish compatibility with physical Android or iOS devices.
