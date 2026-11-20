# Incidente: miniapp sin respuestas · 19 noviembre 2026

Prioridad acordada: recuperar el servicio antes de reconstruir la interfaz. El propietario autoriza la administración del bot y una identidad visual completamente nueva conservando solo el nombre Michiflin.

## Evidencia comprobada

Se inspeccionaron los registros de la sesión anterior y una copia aislada del respaldo cifrado más reciente, sin iniciar correo en esa copia ni publicar estado privado.

- Delta Chat registró actualizaciones WebXDC que no pudo asociar a su mensaje original. También se vio un rechazo de SecureJoin por código de autorización inválido. No demostrar una causa universal a partir de estos avisos.
- Respaldo: SQLite íntegro; 16 vínculos de miniapp, todos con mensaje original presente, tipo correcto, chat correspondiente y archivo local; ninguna época diferente.
- De ellos, 14 dirigidos al propietario; los 14 pasan la comprobación criptográfica real mediante RPC **offline sobre la copia**, ninguno rechazado.
- Cinco paquetes no coinciden con los hashes actualmente admitidos. Los demás conservan compatibilidad. No se debilitó la validación para admitir cualquier paquete.
- Sin correo saliente fallido o pendiente en esa instantánea, y sin actualizaciones más allá de los cursores guardados. La última actividad registrada de miniapp era de unas 12,5 horas antes. La instantánea tenía unos 22 minutos de antigüedad; no representa un monitor en tiempo real.

**No se atribuye todavía la causa a la interfaz, a un borrado de mensajes ni a una caída general del bot.** Una instancia ajena al mensaje/chat del bot o una copia histórica puede no recibir respuestas. La confirmación final requiere abrir una instancia nueva recibida directamente del bot y comprobar un intercambio completo.

## Recuperación limitada y explícita

`OWNER_APP_RECOVERY_REVISION` debe ser un SHA completo idéntico a `RUNNING_SHA`. Solo esa revisión puede emitir una miniapp de recuperación:

- únicamente al propietario configurado, con marcador confirmado, época y huella coincidentes y chat criptográficamente autorizado;
- sin cambiar acceso, invitados, claves, identidad, historial, cola o proyectos;
- la nueva instancia se registra con `recovery_revision` en el mismo guardado normal; arranques posteriores no repiten una entrega ya registrada;
- las miniapps anteriores permanecen, con su almacenamiento independiente. No se promete migrar automáticamente sus datos;
- ante fallo o comprobación incierta se difiere sin imprimir identidad, marcador ni contenido de excepción.

La revisión exacta es una autorización operativa, no una apertura permanente. Se debe retirar la variable después de comprobar la entrega. No cancelar una sesión activa sin permitir su respaldo final: el mecanismo existente de relevo observa la publicación y termina con guardado antes de iniciar la siguiente.

## Validación

89 casos locales aprobados, sin fallos, en la batería de recuperación, diagnóstico, entrega de paquetes, guardas de identidad, identidad limpia y compatibilidad del lote anterior. Las identidades y transportes de esas pruebas son sintéticos. El diagnóstico remoto usa datos reales solo para producir contadores/booleanos agregados; no es una interacción real del propietario ni valida la interfaz nativa.

La reconstrucción visual completa (logos normal/pixel, animación inicial, navegación y estados de conexión) sigue pendiente hasta recuperar y confirmar las respuestas.
