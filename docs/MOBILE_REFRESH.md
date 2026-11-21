# Michiflin: segunda revisión móvil

> Actualización posterior del dueño: el lanzamiento abre los chats privados mediante el enlace de contacto y revoca las miniapps anteriores. Véase `PUBLIC_PRIVATE_RELEASE.md`; esa política sustituye la compatibilidad operativa descrita en esta revisión.

Esta revisión sustituye los requisitos anteriores de mascota al arrancar y selector de calidad de miniaturas. El código local parte de `ffc62f2ae43d7730e44de9c97da08db07d7bbbf3`. Preparar estos archivos no equivale a desplegarlos.

## Cambios

1. **Apertura directa:** sin splash, animación de gato, retrato en cabecera ni presentación automática. La ayuda guiada sigue disponible bajo petición.
2. **Descubrimiento de contenido:** flecha discreta en el borde derecho, con etiqueta accesible, pulsable y oculta al alcanzar el final; no ocupa el centro inferior.
3. **Identidad interior:** iconografía temática genérica, sin mascota o retrato de Michiflin. Se conserva el nombre del producto y el icono externo de la aplicación, no se modifica la identidad del bot.
4. **Banners:** base compacta de 170 px en móvil y 180 px en escritorio; el contenido puede crecer con reflujo o aumento de texto. Acciones principales de al menos 44 px de alto.
5. **Biblioteca:** icono de libro propio; cuadrícula de portadas compactas y acciones pequeñas. Solo se muestran registros con un formato de lectura admitido. Internet Archive resuelve metadatos públicos con un máximo de cuatro consultas paralelas, omitiendo préstamos y restricciones.
6. **Carga silenciosa:** catálogo y portadas no inflan el contador de tareas deliberadas. El catálogo muestra su propio indicador discreto. Dos portadas simultáneas, peticiones deduplicadas, caché de hasta 24 portadas durante un día y seis búsquedas durante diez minutos. Volver a entrar reutiliza esa caché; hay actualización explícita.
7. **Libros guardados:** siguen en IndexedDB y se abren sin red. Limpiar catálogo/portadas desde Ajustes no borra los libros. La lectura en voz alta depende del cliente y del sistema; exportar texto permite usar un lector externo. No se eluden DRM, préstamos ni límites de recursos.
8. **YouTube:** miniaturas siempre de calidad baja, carga visible y agrupada, sin selector. No se descarga el video para generar una miniatura y no se conserva multimedia descargada por el bot.
9. **Modelos:** el aviso invalida y actualiza el catálogo seleccionable; si falla, queda marcado para reintento al entrar en IA. También se invalida el catálogo de otras instancias privadas autorizadas, sin notificación de sistema para invitados. No se inventan modelos ni disponibilidad. Se usan marcas verificadas ya integradas; una identidad desconocida se muestra con iniciales honestas, no con un logo ajeno.
10. **Ajustes:** nueve categorías: apariencia/idioma; IA/modelos/APIs; búsqueda/clima; bot/descargas; privacidad/controles; biblioteca/caché; grupos públicos; conexión/sincronización; ayuda/administración. Idioma del chat va con apariencia, controles de imagen/cuotas IA con IA y lector/capturas con navegación. Se conservan las autorizaciones del backend y los valores actuales.
11. **Utilidades:** mosaicos abiertos y compactos. Cada herramienta local o remota abre su propio espacio adaptable; no hay una pila de formularios plegados. Los resultados, errores y avisos mantienen su destino específico y no se vuelven a solicitar al abrir una notificación.
12. **Grupos:** activación explícita y comprensible; véase abajo. Solo búsqueda pública sin IA, tasas y clima. Sin modelos, cuotas IA, APIs personales, agente, Coder, archivos, descargas o historial privado.

## Grupos: diagnóstico y activación

La ausencia de respuesta puede ser deliberada: falta de activación, herramientas deshabilitadas en el servidor, grupo sin cifrar, dueño ausente, identidad sin verificación global o pausa/mantenimiento. Esta revisión no afirma haber observado el grupo real del dueño.

- El servidor debe tener `GROUP_PUBLIC_TOOLS=1`.
- Dentro del grupo cifrado, el dueño envía `/michiflin activar`.
- Si ya tiene una clave global verificada, se conserva la activación directa.
- En una identidad antigua sin esa fijación, el bot crea una solicitud de 24 horas e indica al dueño que abra **su miniapp en el chat individual cifrado → Ajustes → Grupos públicos**.
- La confirmación exige dueño autorizado, chat individual cifrado y el mismo contacto/huella presente en el grupo. Se fija esa huella para las comprobaciones de pertenencia posteriores. Una dirección por sí sola no habilita herramientas.
- Se admiten hasta veinte grupos pendientes/registrados por esta vía, con limitación de repetición. La interfaz avisa si la función está deshabilitada en el servidor y bloquea la activación.
- Comandos públicos: `/buscar consulta`, `/tasas`, `/clima ciudad`, `/ayuda`. `/michiflin desactivar` funciona para el dueño globalmente verificado; el dueño de una identidad antigua puede desactivar desde los Ajustes privados.
- Si el dueño sale o cambia la huella aprobada, las herramientas dejan de responder. Pausa del dueño, mantenimiento y controles de concurrencia siguen aplicándose.

## Privacidad y conservación

- La donación de API continúa separada, voluntaria, explícita y cifrada; guardar una API personal no implica donarla.
- No se toca el historial, las instancias existentes, sus temas, sus notificaciones ni las copias de libros del cliente.
- No se reactiva el bot independiente ni sus arranques programados.
- No se guarda multimedia descargada del bot ni se incrustan claves en archivos, logs o previsualizaciones.
- El catálogo indica fallos de fuentes; no se fabrican libros, modelos o disponibilidad. Una URL de formato público es evidencia de descarga ofrecida, no garantía de que una fuente externa nunca falle.

## Compatibilidad y entrega

`compat/polish-before` verifica por contenido el paquete exacto previo a esta revisión. Se conserva solo para reconocer instancias ya entregadas; no se incluyen esos archivos en el nuevo `.xdc`. No se pide al dueño borrar chats o datos. La compatibilidad anterior sigue comprobándose con sus huellas y manifiestos.

Antes de publicar: ejecutar las cinco suites de CI, verificar backup cifrado y acceso de escritura, y usar la entrega controlada existente. No arrancar otra identidad. Un `.xdc` aislado no incorpora los cambios de backend: ambos deben desplegarse juntos.

## Pruebas

Las pruebas de esta revisión usan correo simulado, identidades sintéticas y almacenamiento aislado; no demuestran entrega real por Delta Chat ni disponibilidad de APIs de terceros. `tests/test_mobile_polish.py` cubre arranque sin bloqueo, 320/390/1280 px, Chromium/WebKit, categorías, herramientas independientes, caché, descargas admitidas, miniaturas bajas, catálogo seleccionable y aprobación privada de grupos. Se conservan las regresiones de consentimiento, privacidad, compatibilidad y notificaciones visibles.
