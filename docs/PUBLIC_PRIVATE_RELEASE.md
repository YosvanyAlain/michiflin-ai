# Lanzamiento: enlace abierto, chats privados y miniapps retiradas

## Decisión del dueño

Cualquiera que reciba el enlace de contacto de Delta Chat del bot puede completar la unión segura y recibir su propia miniapp, sin aprobación manual. No se crea un sitio web público ni una miniapp compartida entre personas. El acceso administrativo y las APIs personales continúan separados por identidad.

Este lanzamiento incorpora también la revisión móvil `614b31e` y **sustituye su decisión anterior de mantener operativas las miniapps históricas**. Se conservan sus mensajes y almacenamiento local, pero no sus permisos para comunicarse con el backend.

## Política del lanzamiento

El workflow principal fija explícitamente:

```
OWNER_ONLY=0
INVITE_AUTO_ACCESS=1
REVOKE_LEGACY_MINIAPPS=1
MINIAPP_RELEASE=public-private-20261119-v1
PUBLIC_ACCESS_RELEASE=private-invites-20261119-v1
```

- La unión segura verifica automáticamente la clave del contacto y su chat individual cifrado. No exige conocer la identidad civil de la persona.
- Una dirección de correo o una antigua lista de permitidos no bastan. Los bloqueos explícitos y las comprobaciones contra cambios de clave siguen vigentes.
- Cada persona tiene su concesión, chat, miniapp, datos y APIs personales separados. Entrar mediante el enlace no concede administración.
- Grupos: se mantiene únicamente la activación explícita de búsqueda sin IA, tasas y clima. No se abre IA, archivos o datos privados en grupos.
- Se conservan los límites de concurrencia, reparto y proveedor, las pausas personales y el mantenimiento. Abrir el contacto no significa aceptar tráfico ilimitado ni gastar las APIs personales del dueño.

## Revocación de miniapps

Con la revocación activada, solo se acepta un registro del servidor cuya generación/build y época coincidan exactamente con los actuales. `MINIAPP_RELEASE` cambia el hash del paquete independientemente de la época de la cuenta: no se rota la identidad del bot ni se invalidan por accidente los identificadores de sus trabajos y copias.

Se bloquean los registros antiguos antes de:

- leer sus actualizaciones de estado;
- admitir o reproducir solicitudes, incluidas multipartes;
- ejecutar operaciones, incluso `hello`, estado, reinstalación o controles del dueño;
- emitir acuses, respuestas, fragmentos y notificaciones;
- iniciar o continuar sesiones de navegador remoto.

También se vuelve a comprobar el permiso al empezar el trabajo y en la entrega diferida. El build indicado por el cliente no sustituye el registro del servidor. Una copia reenviada no es una miniapp nueva autorizada: debe existir una entrega del bot en el chat individual cifrado y a la identidad correcta.

Los mensajes históricos siguen protegidos contra poda. Un archivo guardado en el dispositivo puede abrirse y ejecutar su código local: no se puede borrar o impedir remotamente que intente transmitir. **Lo que se revoca es su acceso al servicio; el servidor no ejecuta sus órdenes ni le devuelve datos.**

## Apertura inicial y cierre de emergencia

`tools/open_private_invite_access.py` se ejecuta después de verificar la recuperación del backup y antes de iniciar el runtime. Requiere el estado existente y la configuración exacta de este lanzamiento. Guarda y checkpointa cifrada la apertura inicial con una marca de versión.

La marca impide que los siguientes arranques vuelvan a abrir un servicio que el dueño haya cerrado posteriormente. Un fallo del checkpoint aborta el lanzamiento y restaura la política local anterior. La herramienta no modifica claves, cuentas, mensajes, invitaciones o registros de datos personales.

## Entrega de la nueva miniapp

El lanzamiento manual ofrece `deliver_owner_refresh`, activado por defecto. La herramienta solo entrega al chat privado previamente verificado del dueño, una vez por commit solicitado. No crea cuentas ni publica el enlace de invitación. Los nuevos visitantes reciben su propia app mediante la unión segura; una miniapp retirada no impide entregar la nueva en ese chat. `/app` sigue siendo la recuperación desde el chat, no desde una miniapp antigua.

Si la entrega automática falla, las miniapps retiradas **siguen bloqueadas**: no hay una excepción silenciosa de compatibilidad para el dueño.

## Validación y publicación

- Suite local de regresión: 271 pruebas aprobadas.
- Modo real de revocación y acceso mediante enlace, más revisión móvil Chromium/WebKit: 78 pruebas aprobadas. Hay solapamiento entre suites; no son 349 casos únicos.
- Las pruebas recorren todas las operaciones registradas y comprueban silencio de los transportes antiguos, rechazo de copias ajenas, continuidad de la huella, concesiones privadas, checkpoint y respeto a cierres posteriores.
- La CI incluye la nueva suite. La compatibilidad histórica permanece disponible en código solo para configuraciones que no hayan activado esta revocación; el workflow de este lanzamiento la activa explícitamente.

Estas comprobaciones no equivalen a publicar. Antes de ejecutar el runtime debe publicarse el commit, completar las cinco suites de CI, verificar el backup cifrado y efectuar una transición controlada del runtime existente. No se activa el bot independiente ni sus arranques programados.
