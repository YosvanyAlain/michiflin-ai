# Validación de la interfaz simplificada

Validación local previa a publicación y al envío expresamente solicitado por el dueño. Las cifras son por tanda, con solapamientos; no representan un total de pruebas únicas.

| Área | Resultado de cierre |
|---|---|
| Lógica, seguridad, mantenimiento, copias e idiomas | 400 casos unittest; tandas pytest de 212 y 627 aprobadas. Un test opcional omitido. |
| Navegador WebXDC completo | 28 unittest y 131 pytest aprobados. |
| Móvil, diálogos, compatibilidad y entrega | 137 aprobados, 1 omitido. |
| Aceptación, entrega, temas, manual y compatibilidad voluntaria | 76 aprobados en la tanda focalizada. |
| Interfaz nueva y regresión de avisos/destinos/hojas | 84 aprobados, incluida la matriz ampliada. |
| Campana y navegación, tanda amplia anterior | 187 aprobados; los 9 fallidos por directorio temporal ausente se repitieron exactamente y aprobaron. |
| Navegadores aislados, TLS, proxy y almacenamiento | 72 aprobados, 2 omitidos, 134 subpruebas aprobadas. |
| Archivos: lógica, entrega y cifrado | 38 aprobados, 24 subpruebas aprobadas. |
| Archivos: ZIP/7z/RAR, aislamiento, cifrado, UI y unión remota | 39 unittest, final OK; 1 omitido (streaming opcional de 600 MiB). |
| Comprobaciones estáticas | Ruff F/E9, sintaxis de todos los JS, compilación Python, YAML, historial del Programador, arranque Live y git diff --check aprobados. |

## Vistas y revisión visual

- Chromium: 320×568, 412×915 y 1024×768.
- WebKit: 390×844, 768×1024 y 1440×900.
- Español/inglés y temas claro/oscuro distribuidos en esas seis combinaciones.
- 43 capturas por combinación: 20 de inicio, chats, navegación, ajustes y avisos, más 23 de las demás rutas principales y las secciones de kit/enlaces. 258 capturas de vistas; otras dos del arranque con app.js retenido.
- Se revisaron visualmente las doce hojas de contacto correspondientes. También se inspeccionaron por separado vistas representativas de Inicio estrecho, campana móvil oscura y biblioteca de escritorio.
- Las comprobaciones programáticas verifican contenido, ausencia de fallo de renderizado, errores JS y desbordamiento horizontal. Las pruebas especializadas ejercitan interacción, foco, gestos, movimiento reducido, almacenamiento y respuestas tardías.
- Son navegadores emulados con respuestas sintéticas, no pruebas físicas de todos los teléfonos ni de cada cliente Delta Chat/Android. No certifican la velocidad de la red real.

## Incidencias encontradas y cierre

- Se retiraron selectores y expectativas del saludo antiguo, conservando las comprobaciones de movimiento reducido y verificando ahora la continuación del último resultado.
- Para los procesos aislados no sirve un socket temporal debajo de `/home/user`, cuyo directorio privado no puede atravesar el UID aislado. La tanda completa pasó usando temporales accesibles. No se cambiaron permisos de la carpeta privada, validación TLS ni filtros de red.
- El tmpfs de `/tmp` es menor que la reserva de disco del servicio: la lógica de entrega pasó al usar temporales sobre disco. La reserva de producción no se redujo.
- En una primera tanda de archivos hubo un timeout del selector de extracción RAR. La repetición completa, incluyendo todos sus escenarios Chromium/WebKit y ambos idiomas, terminó OK. La repetición no prueba por sí sola la causa de ese timeout.
- Actualizar elTOQUE manualmente mientras hay una consulta automática activa adopta la misma solicitud y la hace visible; no duplica la operación. Hay prueba específica del solapamiento.
- Se corrigió el diagnóstico agregado para que reconozca las mismas compilaciones previas fijadas que el servidor. Un recuento anterior basado solo en el paquete actual no demuestra que todas las instancias antiguas fueran incompatibles.

## Rendimiento y entrega

La huella del paquete y comprobaciones de compatibilidad usan cachés acotadas e invalidables. En una medición local de 250 llamadas, la primera huella tardó 11,934 ms y la mediana en caché 0,563 ms. Es una medición de esa operación local, no una mejora medida de latencia del bot en producción.

La entrega nueva requiere un commit exacto en `OWNER_REQUESTED_APP_REVISION`, vinculación existente del dueño verificada y registro de revisión que impide repetirla. Las instancias previas y sus datos se conservan. La entrega registra además un checkpoint cifrado sin rotar copias previas; un fallo de ese respaldo no provoca reenvío automático en el mismo estado. Dos pruebas adicionales cubren respaldo correcto/fallido e idempotencia. La retirada de los archivos activos de la función eliminada es una acción independiente: requiere autorizar expresamente `UI_RETIRE_STORE_REVISION` con el commit exacto, además de respaldo y recuperación verificados antes del arranque. Una solicitud de actualización no la activa. Los respaldos históricos no se eliminan por esa retirada.

Los resultados del despliegue, la integración continua remota y el acuse de entrega deben verificarse por separado; este documento registra las pruebas locales y no los declara completados anticipadamente.
