# Arranque local resistente en WebViews

## Alcance y límite de la investigación

La foto recibida muestra navegación y engranaje, pero título y contenido vacíos. No proporciona la versión de Android, Delta Chat ni Android System WebView. **No se atribuye una causa definitiva a ese teléfono sin sus datos/diagnóstico.**

Con la revisión base `667fe47b180d16d4fbe5bf37300462784051b9b3`, una API de voz opcional que lanza una excepción reproduce el mismo estado visual: barra inferior y engranaje presentes, título vacío y cero caracteres de contenido. También se reproduce con un snapshot de trabajos `jobs: null`. El fallo no depende de que llegue una respuesta del bot: ocurre durante el arranque/renderizado local.

## Cambios

- La limpieza opcional de voz no bloquea cada navegación si el cliente no permite esa API. El lector conserva texto/exportación aunque no tenga voz.
- Los registros JSON con forma incorrecta no se toman como arrays/objetos válidos. La cola en caché se valida antes de dibujar la barra. No se borra el almacenamiento ni el historial.
- Un fallo de una vista muestra una recuperación localizada, con Inicio y Ajustes, en lugar de vaciar toda la app. No muestra el error privado ni vuelve a ejecutar la operación.
- El contenido no comienza con opacidad cero: una excepción o callback de animación que no llegue ya no lo deja invisible.
- Una recuperación HTML/ES5 independiente se muestra si el script principal ni siquiera puede arrancar/parsearse. Indica pedir `/app` en el chat privado sin borrar el chat ni reinstalar.
- Un `setUpdateListener` del cliente que lanza sincrónicamente no interrumpe la interfaz local. No se hace ningún reintento automático de una operación de pago.

No se modificaron credenciales, invitación, admisión, identidad, cifrado, comandos de seguridad ni consentimiento de APIs. No se reactivó la ejecución independiente.

## Verificación local

`tests/test_android_startup.py`: **31 pruebas aprobadas** en 55,89 s. Se comprueban clics sobre las cinco pestañas, acceso a Ajustes según diseño móvil/escritorio, sus ocho subapartados, una utilidad local, contenido visible y ausencia de recuperación inesperada. Son 10 tamaños/orientaciones × español/inglés, siete escenarios de API/caché averiada, error de vista recuperable sin reejecución/borrado, script principal inválido dentro del paquete real, fallo del listener del host y motor Chromium antiguo real.

- Chromium 159: móvil 320×640, 360×800, 412×915, horizontal 800×360, tablet 768×1024, escritorio 1280×720 y 1920×1080.
- WebKit 27.2: móvil 390×844, tablet 1024×768, escritorio 1440×900.
- Chromium **98.0.4695.0**, ejecutable antiguo real: paquete `.xdc` construido por `miniapp.build`, móvil 360×800 y navegación mediante entrada de ratón en coordenadas. No se ha simulado un motor antiguo cambiando solo el user-agent ni se ha añadido un polyfill para ocultar errores de la app.

Regresiones de interfaz, biblioteca, compatibilidad, modo por chat, entrega silenciosa y aislamiento: **149 aprobadas**. Comprobaciones WebXDC estáticas: 13 aprobadas y 1 omitida por condición del entorno; Ruff y `git diff --check` correctos.

**Limitación:** las pruebas usan motores de navegador y emulación de tamaño/táctil, con puente Delta Chat sintético. No constituyen prueba en un teléfono Android físico ni validación de todos los WebViews, ni verifican proveedores externos por correo. Un paquete recién construido tiene nueva huella y no debe anunciarse conectado al bot hasta desplegar el servidor correspondiente.

## Publicación pendiente

Los cambios están preparados localmente, **no publicados en GitHub ni en el bot**. La credencial vigente de la sesión anterior no está disponible aquí. Se recuperó el respaldo cifrado autorizado y se comprobó sin imprimir secretos: su token devuelve HTTP 401. Hace falta acceso válido del propietario para publicar, ejecutar las cinco suites remotas y realizar el relevo con respaldo cifrado verificado.

Después del despliegue habrá que solicitar `/app` en el chat privado y abrir el adjunto nuevo: reabrir un paquete ya recibido no actualiza su JavaScript. Se conserva el historial; no se debe pedir borrar el chat, los datos de la app o reinstalar Delta Chat como supuesto arreglo.
