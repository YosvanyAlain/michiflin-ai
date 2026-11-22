# Comunidad, notificaciones y actualizaciones voluntarias

## Uso

- **Campana, arriba a la derecha:** centro de avisos adaptable a móvil, tableta y escritorio. Inicio ya no contiene la lista de avisos ni una insignia inferior. Las otras secciones conservan sus insignias.
- Filtra por sección, oculta un aviso, activa **Mostrar ocultas** para recuperarlo o usa **Limpiar visibles**. Limpiar requiere confirmación y solo retira los avisos seleccionados de este dispositivo: no borra los resultados, conversaciones o trabajos.
- Los avisos generales se marcan leídos únicamente después de mostrarse durante dos fotogramas, con al menos la mitad visible. Los resultados de otras secciones solo se reconocen al abrir y mostrar su destino. Cerrar o cubrir la campana antes de pintarse no cuenta como lectura.
- **Apps → Comunidad de texto / La Plaza:** comunidad de los contactos admitidos al bot. Alias sin publicar el correo de la cuenta; perfil y biografía; publicaciones de hasta 3.000 caracteres; respuestas en hilos; edición con control de versiones; eliminación propia; me gusta, guardados, seguimiento, silencios personales y reportes.
- Filtros de todo, fijados, siguiendo, guardados y mis textos; búsqueda de texto/etiquetas; paginación manual. No hay sondeo automático ni reproducción multimedia.
- **Mi perfil:** paleta, espaciado, tamaño de letra y etiquetas silenciadas. También permite restablecer los textos y las personas ocultos. El borrador se guarda localmente antes de enviar; una clave de idempotencia evita duplicar una publicación al repetir exactamente una solicitud de resultado incierto. No se repiten publicaciones automáticamente.
- El propietario puede fijar, ocultar/restaurar publicaciones, resolver reportes y suspender/readmitir perfiles. La admisión al bot continúa siendo una autorización independiente. Los perfiles están vinculados a la concesión de acceso y, cuando se usa, a la época estricta.

### Privacidad y límites

Solo texto: no se interpreta HTML de publicaciones, perfiles o reportes; no hay imágenes, adjuntos, embeds ni correos de cuenta en respuestas públicas. Una persona puede escribir datos personales en su propio texto: no debe publicar secretos.

**No es cifrado extremo a extremo entre miembros.** Los contactos admitidos y el servidor pueden leer las publicaciones. El chat privado existente protege el transporte al bot; el respaldo protege la copia almacenada en la nube. Las copias anteriores pueden conservar textos posteriormente eliminados.

Máximo 20 publicaciones por persona cada 10 minutos, con separación mínima de 8 segundos. No cambia las cuotas de IA, los modelos, los agentes ni los límites de descarga. Capacidad inicial de 20.000 publicaciones/respuestas; al alcanzarla se rechazan nuevas publicaciones, nunca se purga el historial silenciosamente.

## Actualizaciones voluntarias

El workflow ya no ejecuta la entrega automática `deliver_owner_refresh`. No se añade otro workflow ni una rama de despliegue. En el primer saludo de una app anterior admitida se envía un aviso dentro de esa misma instancia, con marca por compilación después del envío. No se envía un archivo `.xdc` por actualizar el servidor.

El paquete exacto anterior a esta modificación sigue admitido en la misma época y release, aun con la revocación de versiones históricas activa. Se verifica contra un manifiesto fijado y sus fuentes archivadas. Esto **no reactiva** identidades, concesiones ni versiones antiguas retiradas.

La persona solicita la nueva instancia desde **Ajustes → Sincronización → Pedir app nueva**, o mediante `/app`. La anterior y sus datos locales se conservan. Las instancias tienen almacenamiento local independiente; el perfil y las publicaciones de comunidad permanecen en el servidor y son accesibles desde la nueva app con la misma autorización. Un respaldo restaurado puede repetir un aviso cuya marca aún no se había respaldado.

## Google Drive: copia adicional opcional

La base `text-social.sqlite3` reside en `CONFIG_DIR`. El archivador existente hace una instantánea consistente de SQLite (incluyendo transacciones confirmadas en WAL) y la incorpora al respaldo cifrado principal. No se cambia su clave ni el procedimiento de recuperación principal.

`drive_backup.py` puede duplicar el **mismo sobre cifrado**, nunca el estado en claro, después de que GitHub haya verificado su copia. Para activarlo en el runner hacen falta:

| Configuración de GitHub | Nombre |
|---|---|
| Variable, valor `1` | `DRIVE_BACKUP_ENABLED` |
| Secreto OAuth del propietario | `GOOGLE_DRIVE_CLIENT_ID` |
| Secreto OAuth | `GOOGLE_DRIVE_CLIENT_SECRET` |
| Concesión de renovación de acceso | `GOOGLE_DRIVE_REFRESH_TOKEN` |
| ID de una carpeta privada no compartida en Mi unidad | `GOOGLE_DRIVE_FOLDER_ID` |

La conexión de Google Drive de **Arena** no transfiere estas credenciales a **GitHub Actions**. Sin ellas, no se debe anunciar automatización activa. Usar una autorización de archivos apropiada, no credenciales de Gmail o la contraseña de Google. Mantenerse dentro del almacenamiento gratuito: no hay ampliación, compra ni activación de facturación automática. Revocar el refresh token en Google desactiva el acceso del runner.

Protocolo de la copia adicional:

1. Validar el sobre cifrado y su recuperación local.
2. Comprobar que la carpeta de destino no está compartida.
3. Crear un archivo nuevo marcado pendiente, sin reemplazar ni borrar copias anteriores.
4. Descargarlo con límite de tamaño; comparar SHA-256; descifrar y validar extracción, estado y bases SQLite.
5. Marcarlo verificado solo después de superar la recuperación de prueba.

Si falla Drive, la copia principal ya verificada continúa válida y se pospone su rotación para conservar todas las anteriores. No se rota ni elimina ningún archivo de Drive desde este módulo. Se debe revisar su espacio periódicamente; al agotarse la copia adicional falla sin comprar almacenamiento. Los errores no registran credenciales, URLs privadas ni contenidos.

Para una recuperación excepcional, `drive_backup.recovery_archive(file_id, passphrase)` descarga, descifra y verifica un archivo explícitamente elegido y devuelve un archivo TAR en memoria. **No restaura sobre el bot en funcionamiento.** Un operador debe inspeccionar la identidad en un directorio aislado, guardar con permisos privados y seguir el procedimiento de recuperación y exclusión de doble bot existente. La clave de cifrado debe conservarse fuera de la carpeta de respaldos.

## YouTube: alcance real del diagnóstico

Los registros disponibles de la sesión 44974344172 confirman la verificación de WARP, pero no contienen un error de descarga. Eso no demuestra que YouTube funcione ni que WARP sea el culpable.

Se corrigió un defecto comprobable: `proxy_unavailable` / `isolation_unavailable` podían confundirse con contenido no disponible por la búsqueda genérica de la palabra `unavailable`. Ahora se distinguen proxy, DNS/conexión/TLS, HTTP 403, HTTP 429, formato, FFmpeg, intérprete JavaScript y verificación humana, tanto en el proceso normal como en el aislado. Se declaran Deno y Node como intérpretes compatibles, igual que en el worker aislado. Los mensajes no publican URLs firmadas, cookies o texto bruto del proveedor.

Una verificación humana explícita detiene los cambios de ruta/cliente; un límite 429 también detiene los cambios de ruta. No se resuelven CAPTCHA, se transfieren identidades ni se prometen accesos al servidor por verificar desde otro dispositivo. Para identificar el fallo reportado hace falta el enlace concreto y el mensaje/código devuelto por el bot en esa solicitud. Las pruebas con errores sintéticos no equivalen a una descarga real desde el runner.
