# Actualización de biblioteca y conservación de datos

La candidata se desarrolla y prueba por separado. **No supone por sí sola un despliegue ni una migración.** No se ha cambiado la política de seguridad: únicamente se autoriza la miniapp de la versión actual.

## Antes de instalar una miniapp nueva

Cada mensaje WebXDC tiene su propio almacenamiento aislado. Una miniapp enviada en otro mensaje no puede leer automáticamente IndexedDB ni localStorage de la anterior. Cambiar el paquete cambia su huella; con `MINIAPP_CURRENT_ONLY=1` y `REVOKE_LEGACY_MINIAPPS=1`, cuando el bot pase a la nueva versión, la miniapp anterior dejará de poder pedirle servicios. El mensaje anterior y sus datos locales no se borran por este cambio, pero que sigan accesibles depende del cliente y de que no se borren los datos; no deben considerarse un respaldo independiente.

1. Abre **la miniapp anterior**, entra en **Biblioteca → Mi biblioteca**, abre cada libro que quieras conservar y pulsa **Exportar al lector del sistema**. Elige **Guardar o compartir** y guarda cada TXT en Archivos, o usa **Enviar a un chat** que controles. El TXT contiene texto, título y autor, no el EPUB/PDF original ni ilustraciones. Comprueba que el archivo realmente se guardó y se abre.
2. El lector anterior no ofrece exportación completa de progreso. Si te importa la posición, anota el número de parte y el total o haz una captura. El porcentaje de la miniapp nueva permite volver aproximadamente a ese punto; el TXT incluye una cabecera y la paginación podría variar.
3. Revisa por separado cualquier otro dato local importante de la miniapp anterior (por ejemplo, archivos locales del kit, imágenes o ajustes) y expórtalo si la función correspondiente lo permite. La migración de biblioteca **no** copia todo el almacenamiento de la aplicación.
4. Conserva el mensaje y el almacenamiento de la miniapp anterior. No los borres ni limpies sus datos hasta verificar los archivos y la importación. La exportación TXT se hace manualmente, un libro a la vez.
5. Solo después de decidir el cambio y una vez desplegada la versión nueva, pide `/app` en el chat privado. En la miniapp nueva, usa **Biblioteca → Mi biblioteca → Importar TXT o copia** y selecciona los TXT. La importación no restaura su progreso anterior automáticamente. Comprueba que cada libro se abre.

El aviso se aplica a cada instancia/dispositivo con datos locales propios. No presupone que todos los usuarios tengan los mismos archivos ni permite inspeccionar sus libros.

## Copias posteriores de la biblioteca nueva

- **Copia de biblioteca** crea un JSON local con los libros guardados en esa instancia y sus posiciones y marcadores. La persona elige expresamente guardarlo o enviarlo; el archivo **no está cifrado** y puede contener libros y hábitos de lectura privados. Elige un destino seguro.
- Se pueden importar TXT o copias JSON de esta biblioteca; el límite es 40 libros guardados, 3 MB de texto por libro y 40 MiB por selección de archivos o copia. Si una copia sobrepasa 40 MiB, exporta los libros individualmente como TXT; este formato individual no conserva marcadores ni progreso.
- La importación valida todos los archivos antes de escribir y añade los libros en una transacción; nunca sustituye uno que tenga el mismo identificador. El progreso se restaura solo para libros añadidos y después de guardar el libro; si el almacenamiento del progreso falla, se informa. Un TXT importado comienza al inicio.
- No se exportan contraseñas, otros datos de la miniapp, ajustes de lectura ni archivos originales EPUB/PDF. Estas copias no sustituyen un respaldo completo del dispositivo.

## Lectura y fuentes

- El lector funciona sin conexión una vez guardado el libro. Controles en flujo normal, tipos y tamaños de letra, interlineado, ancho, tres fondos, búsqueda local, índice cuando se detectan encabezados, marcadores, porcentaje, voz declarada local por el sistema si está disponible y exportación TXT. La posición se estima mientras se desplaza; no es un marcador exacto de cada palabra.
- Si el proveedor ofrece EPUB, se intenta primero una copia adaptada con títulos, énfasis e imágenes raster locales dentro de límites. Se eliminan HTML activo, estilos, enlaces y recursos remotos; algunas imágenes o la maquetación original pueden faltar y se avisa. Un libro que ya se guardó como texto en la app antigua no recupera ilustraciones al importarse. Los PDF siguen como texto extraído; un PDF escaneado sin texto puede no ser legible. Para el formato y maquetación originales, abre la ficha del proveedor y comprueba el acceso y los derechos aplicables.
- Las búsquedas y descargas viajan por el bot y pueden tardar. Mantén abierta la miniapp hasta que aparezca «Abrir»; si se cierra antes de guardar una respuesta, puede hacer falta descargar otra vez. Se muestran resultados parciales y avisos de fuentes; un fallo de entrega no se atribuye automáticamente a un catálogo. Los cursores siguen aislados y limitados. No se eluden DRM, préstamos, desafíos, cuotas ni restricciones.

## Alcance de las pruebas

Las pruebas automatizadas usan libros y cuentas sintéticas, un puente WebXDC simulado y Chromium/WebKit. No demuestran recepción en Delta Chat real ni recuperación automática de los datos de una instancia anterior. La disponibilidad real de una fuente, la voz y la exportación dependen del proveedor y del cliente utilizado.
