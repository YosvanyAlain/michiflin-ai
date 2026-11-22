# Notificaciones y conservación de instancias

## Actualizaciones voluntarias

El workflow no ejecuta automáticamente `deliver_owner_refresh`. No se envía un archivo `.xdc` por actualizar el servidor, salvo una solicitud explícita del dueño fijada a un commit exacto mediante `OWNER_REQUESTED_APP_REVISION`. Esa entrega sucede dentro de la única instancia del bot y solo a una vinculación autorizada existente. Antes del envío se guarda una reserva durable y, si el respaldo es obligatorio, se verifica una copia cifrada sin rotar las anteriores. Un fallo de la reserva impide enviar; una reserva existente o un resultado incierto impide reintentar a ciegas. Restaurar una copia anterior podría perder la reserva: verifica la entrega y desarma la variable antes de cualquier recuperación.

Con `MINIAPP_CURRENT_ONLY=1`, una app anterior no puede operar el backend, aunque antes figurara como compatible. Solo si esa instancia autorizada envía un evento nuevo puede recibir un aviso orientativo; el barrido de arranque no genera un envío masivo. La reserva del aviso se guarda antes del envío, por lo que una caída posterior o la saturación pueden impedirlo. No se garantiza la entrega, y restaurar un respaldo anterior a la reserva puede permitir que se repita.

Una app actual puede ofrecer **Ajustes → Sincronización → Pedir app nueva**. Para una app retirada, solicita `/app` en el chat privado cifrado y autorizado. El bot no reemplaza automáticamente una instancia anterior conocida, ni por abrirla ni por repetir la unión. Los mensajes anteriores y sus datos locales no se borran; cada instancia tiene almacenamiento independiente y no se trasladan los proyectos automáticamente.

## Respaldo principal

Esta versión no ofrece copias automáticas en Google Drive. El respaldo principal continúa cifrado en el repositorio privado configurado. La retirada de esta opción no borra ni modifica archivos o copias históricas que ya se encuentren en Drive. Conserva sus claves y herramientas de recuperación por separado.

En la nueva sesión `STATE_BACKUP_RETAIN_ALL=1` impide la rotación automática de instantáneas. Una sesión anterior ya iniciada conserva su propia configuración hasta terminar; no se puede cambiar en caliente. Las copias acumuladas requieren espacio y GitHub limita los archivos de cada publicación: vigila su capacidad. Si no se puede guardar una copia nueva, el relevo automático debe detenerse; no resuelvas el problema borrando copias sin una decisión expresa.

La configuración ligera existente excluye del respaldo archivos de audio y vídeo dentro de los adjuntos y puede descartar copias locales de descargas completadas; las carpetas de la cola y los espacios temporales tampoco se incluyen. Una instantánea del estado **no es una copia completa de todos los archivos**. Conserva y verifica por separado los originales y adjuntos importantes, así como los datos locales de cada miniapp.

## YouTube: alcance real del diagnóstico

El propietario confirmó que su nueva prueba descargó vídeos y ya no mostró el aviso de verificación. Eso confirma su caso observado, no garantiza todos los enlaces, clientes o sesiones futuras.

Se corrigió un defecto comprobable: `proxy_unavailable` / `isolation_unavailable` podían confundirse con contenido no disponible por la búsqueda genérica de la palabra `unavailable`. Ahora se distinguen proxy, DNS/conexión/TLS, HTTP 403, HTTP 429, formato, FFmpeg, intérprete JavaScript y verificación humana, tanto en el proceso normal como en el aislado. Se declaran Deno y Node como intérpretes compatibles, igual que en el worker aislado. Los mensajes no publican URLs firmadas, cookies o texto bruto del proveedor.

Una verificación humana explícita detiene los cambios de ruta/cliente; un límite 429 también detiene los cambios de ruta. No se resuelven CAPTCHA, se transfieren identidades ni se prometen accesos al servidor por verificar desde otro dispositivo. Si reaparece el fallo, el diagnóstico requiere el enlace concreto y el mensaje/código devuelto en esa solicitud. Las pruebas con errores sintéticos no equivalen a una descarga real desde el runner.
