# Biblioteca: originales y conservación de datos

Este documento describe la candidata; no demuestra por sí solo un despliegue ni una migración. La política sigue siendo **solo la miniapp actual**: `MINIAPP_CURRENT_ONLY=1` y `REVOKE_LEGACY_MINIAPPS=1`.

## Antes de cambiar de miniapp

Cada mensaje WebXDC tiene almacenamiento independiente. Al cambiar el paquete, el bot puede dejar de atender las solicitudes de la miniapp anterior; el mensaje anterior no se borra por ello, pero sus datos locales tampoco se copian automáticamente. Lo mismo se aplica a cada dispositivo o instancia.

1. En la miniapp anterior, abre **Biblioteca → Mi biblioteca**, abre cada libro y usa la opción de exportación. Si solo tienes una copia de lectura TXT, esa copia incluye texto, título y autor; **no recupera los bytes, las imágenes ni la maquetación del EPUB/PDF original**. Comprueba que el archivo exportado se guardó y abre.
2. Anota o captura el progreso si la versión anterior no permite respaldarlo. Importar un TXT aislado empieza desde el principio.
3. Exporta por separado archivos del kit, medios y otros datos locales que quieras conservar. Una copia de la biblioteca no es una migración integral de la miniapp.
4. Conserva el mensaje y los datos de la miniapp anterior. Pide `/app` en el chat privado cuando la nueva versión esté desplegada. En la nueva usa **Biblioteca → Mi biblioteca → Importar libros o copia**. Comprueba los archivos y su lectura antes de borrar nada.

## Archivos y copia de biblioteca

- Al descargar de una ficha, selecciona un formato público ofrecido (EPUB, PDF o TXT). No se cambia silenciosamente por otro formato si falla. El archivo y la comprobación de la ficha de Internet Archive se solicitan sin reutilizar cookies o credenciales del navegador. La transferencia por el bot puede tardar; mantén la miniapp abierta hasta que aparezca «Guardado». El archivo se comprueba por SHA-256 y se conserva byte por byte en esa instancia.
- **Esta versión no ofrece lectura interna de libros**. En **Mi biblioteca**, usa **Exportar original** y ábrelo en una aplicación compatible. El número de descargas nuevas junto a esa pestaña se limpia al abrirla, no al borrar libros. Un EPUB cifrado o con fuentes ofuscadas se conserva sin descifrar y puede no abrirse en otra aplicación. No se eluden DRM, préstamos ni restricciones.
- **Exportar original** prepara exactamente los bytes recibidos, sin cabecera ni conversión. Los libros antiguos que solo contienen texto se identifican como **copia de texto**, y se exportan como tal; no se les atribuye un original inexistente. El cliente ofrece sus opciones para compartir o guardar; la miniapp no puede comprobar si la persona terminó de guardar, compartir o cancelar. Verifica el archivo en su destino.
- **Copia de biblioteca** crea un JSON local versión 2 que incluye los originales disponibles, los libros de solo texto, posiciones y marcadores. La persona elige guardarlo o enviarlo. **No está cifrado** y puede contener libros y hábitos de lectura privados. Se aceptan también copias JSON versión 1; estas solo contienen lo que ya guardaba esa versión, sin recuperar originales perdidos.
- Se admiten archivos EPUB, PDF o TXT de hasta 8.000.000 bytes y un máximo de 40 libros por instancia. Una selección o copia JSON tiene un límite de 40 MiB; si se supera, exporta archivos originales individualmente. La importación valida antes de escribir y añade en una transacción, sin sobrescribir libros existentes; restaura el progreso solo en los añadidos y avisa si el guardado local falla.
- Dentro de la misma instancia, la actualización de la base local separa los metadatos y los archivos originales mediante una transacción; los libros de texto anteriores se conservan. Al eliminar una copia, metadatos y original también se eliminan juntos; un fallo de la transacción revierte ambos. Esto no copia datos entre mensajes WebXDC. El almacenamiento local puede agotarse o ser eliminado por el cliente. Un archivo o JSON exportado y comprobado es más seguro que confiar únicamente en el almacenamiento de la miniapp. La copia no incluye contraseñas, archivos del kit, otros datos ni ajustes globales.

## Tamaño y compatibilidad

Los motores locales aumentan el paquete `.xdc` de aproximadamente 3,72 MB a aproximadamente 4,4 MB, antes de la codificación del correo (que añade aproximadamente un tercio). El presupuesto de regresión pasa explícitamente de 3.800.000 a 4.600.000 bytes; no se han eliminado las fuentes ni licencias del motor 7-Zip para compensar el tamaño. La entrega puede fallar o tardar con conexiones lentas, límites del servidor de correo o clientes antiguos. Si no llega, conserva la versión anterior y consulta el estado antes de repetir.

## Alcance de las pruebas

Las pruebas automatizadas usan libros y cuentas sintéticas, un puente WebXDC simulado y Chromium/WebKit. También prueban el paquete y fragmentos en Delta Chat Core con un perfil artificial, local y sin correo; esto no es una prueba de entrega. No prueban por sí solas la recepción en un cliente Delta Chat físico, ni que datos de otra instancia se migren automáticamente. La disponibilidad de cada fuente, los permisos para compartir y la posibilidad de exportar dependen del proveedor y del cliente. Una copia de texto no se convierte en el original por el hecho de importarla.
