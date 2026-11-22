# Notificaciones y respaldo adicional

## Actualizaciones voluntarias

El workflow ya no ejecuta la entrega automática `deliver_owner_refresh`. No se añade otro workflow ni una rama de despliegue. En el primer saludo de una app anterior admitida se envía un aviso dentro de esa misma instancia, con marca por compilación después del envío. No se envía un archivo `.xdc` por actualizar el servidor, salvo una solicitud explícita del dueño fijada a un commit exacto mediante `OWNER_REQUESTED_APP_REVISION`. Esa entrega sucede dentro de la única instancia del bot, solo a una vinculación verificada existente y una sola vez; el mismo ajuste es inerte para cualquier otro commit.

El paquete exacto anterior a esta modificación sigue admitido en la misma época y release, aun con la revocación de versiones históricas activa. Los paquetes de referencia se verifican contra sus manifiestos; las compilaciones recientes publicadas se reconocen mediante un registro fijado de huellas vinculado a la misma época y release. Esto **no reactiva** identidades, concesiones ni versiones antiguas retiradas.

La persona solicita la nueva instancia desde **Ajustes → Sincronización → Pedir app nueva**, o mediante `/app`. La anterior y sus datos locales se conservan. Las instancias tienen almacenamiento local independiente. Un respaldo restaurado puede repetir un aviso cuya marca aún no se había respaldado.

## Google Drive: copia adicional opcional

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

El propietario confirmó que su nueva prueba descargó vídeos y ya no mostró el aviso de verificación. Eso confirma su caso observado, no garantiza todos los enlaces, clientes o sesiones futuras.

Se corrigió un defecto comprobable: `proxy_unavailable` / `isolation_unavailable` podían confundirse con contenido no disponible por la búsqueda genérica de la palabra `unavailable`. Ahora se distinguen proxy, DNS/conexión/TLS, HTTP 403, HTTP 429, formato, FFmpeg, intérprete JavaScript y verificación humana, tanto en el proceso normal como en el aislado. Se declaran Deno y Node como intérpretes compatibles, igual que en el worker aislado. Los mensajes no publican URLs firmadas, cookies o texto bruto del proveedor.

Una verificación humana explícita detiene los cambios de ruta/cliente; un límite 429 también detiene los cambios de ruta. No se resuelven CAPTCHA, se transfieren identidades ni se prometen accesos al servidor por verificar desde otro dispositivo. Si reaparece el fallo, el diagnóstico requiere el enlace concreto y el mensaje/código devuelto en esa solicitud. Las pruebas con errores sintéticos no equivalen a una descarga real desde el runner.
