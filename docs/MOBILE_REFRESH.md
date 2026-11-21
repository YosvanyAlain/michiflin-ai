# Michiflin: revisión móvil, identidad y biblioteca

## Alcance

La referencia visual es `assets/michiflin-avatar.png`: gato negro pequeño de ojos dorados, gafas redondas y placa roja. Las poses vectoriales comparten esos rasgos; el retrato real se usa en la cabecera. La animación respeta movimiento reducido.

| Pedido | Implementación |
|---|---|
| 1. Identidad visual | Retrato original, dos poses negras coherentes, ojos y placa sin recoloreado heredado del antiguo icono. |
| 2. Opciones ocultas | Indicador persistente «Más opciones abajo», pulsable y oculto al llegar al final; no depende de un toast. |
| 3. Imágenes | Selector plegado, marcas reales de familia/proveedor cuando existe un recurso verificable, nombre de familia cuando no; resultado al lado del prompt. Elegir un modelo pliega el selector. |
| 4. Solicitudes | Seis filas iniciales, expansión por bloques, altura acotada y diagnóstico secundario plegado. No se pierde la cola. |
| 5. Submenús | Solo cambia la flecha del `summary` que realmente se abre, no la de sus descendientes. |
| 6. Utilidades | Texto, JSON, SHA-256, contraseñas seguras, Base64, limpieza de enlaces y porcentajes locales, más conversión, hora y cálculo existentes. Unidades de tiempo/binarias y rechazo de temperaturas físicamente imposibles, recuperando ideas del repo nuevo. |
| 7. APIs propias | Asistente con trece opciones de proveedor/manual, tres protocolos, normalización de endpoints copiados, descubrimiento sin generaciones de prueba y edición manual. Donación separada, opcional, cifrada y revocable. |
| 8. Libros | Gutenberg con el catálogo y espejo oficial adaptados del repo nuevo, Open Library, Internet Archive, Google Books, Standard Ebooks, Wikisource y búsqueda web. Estantería, detalles, copia de lectura local, apertura sin conexión y exportación accesible. |
| 9. Agente y Coder | Identidad Michiflin independiente del proveedor, preservación de límites de herramientas, aislamiento del programador y descarte de prefijos de razonamiento truncados. Se conserva selección explícita y rotación compatible, no se cambia el modelo por una promesa no medida. |
| 10. YouTube | Miniaturas de tres calidades, baja por defecto, carga visible y agrupada; cambiar calidad no repite la búsqueda ni pierde páginas. |
| 11. Grupos | Solo búsqueda pública extractiva **sin ningún modelo de IA**, tasas y clima; dueño verificado, grupo cifrado y activación explícita. Sin APIs, archivos, Coder, descargas ni datos privados. |
| 12. Comandos | Alias experimental retirado; auditoría de aliases y eliminación de un duplicado inocuo de imagen. |
| 13. Web/Live | Identidad genérica por hostname, sin promoción o clasificación particular de servicios. Migración cifrada de sesiones antiguas; la lista de proveedores de IA no se confunde con sitios de navegación. |

## Límites que se muestran al usuario

- **Catálogo no equivale a descarga.** Préstamos, DRM y restricciones no se eluden. Una copia de lectura EPUB/PDF se convierte a texto; la ficha enlaza los formatos originales. Los libros demasiado grandes se rechazan expresamente en lugar de truncarlos. Wikisource puede ser una página enlazada a otros capítulos, no una obra entera.
- **Fuentes reales:** las pruebas públicas verificaron resultados de Gutenberg, Open Library, Internet Archive y Wikisource. Google Books y Standard Ebooks rechazaron la consulta en el entorno de prueba; la interfaz ofrece otras fuentes y no inventa resultados.
- **Voz:** usa `speechSynthesis` y voces locales cuando el cliente/sistema las ofrece. No se promete que todos los WebViews tengan voces. La exportación de texto permite usar un lector accesible del sistema. No se envía el libro a un servicio TTS de pago.
- **Almacenamiento:** los libros descargados pertenecen a esta instancia de WebXDC; el cliente puede borrar su almacenamiento. No se afirma una descarga hasta que termina la transacción local.
- **APIs:** la donación no es necesaria para usar una API personal. La copia se guarda como un recurso cifrado en un repositorio verificado privado, nunca en código o logs. Se descarga y verifica antes de confirmar recepción. Queda pendiente de revisión, no se activa ni gasta automáticamente. El dueño puede descifrarla. El límite solicitado no sustituye un presupuesto del proveedor. Retirar el recurso no puede invalidar copias ya vistas: hay que rotar/revocar en el proveedor para ello.
- **Modelos:** una prueba pública pequeña, sin claves de pago ni datos privados, comprobó identidad, suma y código Python parseable en GPT-OSS 120B. Qwen respondió HTTP 200 pero no produjo el JSON solicitado con ese presupuesto. Esto prueba compatibilidad limitada, no calidad general ni «ausencia de censura». Las reglas y disponibilidad del proveedor siguen aplicando.

## Activar un grupo

El dueño que ya tiene una clave verificada debe estar dentro de un grupo cifrado y escribir:

```
/michiflin activar
```

Después los miembros pueden usar `/buscar consulta`, `/tasas` y `/clima ciudad`. No consumen cuotas de modelos de IA; las fuentes web pueden limitar tráfico. Hay control de concurrencia/antiflood, no una cuota diaria de IA. `/michiflin desactivar` apaga esas herramientas en ese grupo. Si el dueño sale, se deja de responder. El mantenimiento y la pausa del dueño también se respetan.

## Compatibilidad

`compat/mobile-before` preserva las huellas de la miniapp que ya estaba enviada. No se incluye en paquetes nuevos ni restaura el antiguo tema retirado. Las instancias anteriores y sus datos no se eliminan al entregar la nueva. Los nuevos nombres genéricos de Web/Live migran las sesiones cifradas antes de retirar su alias antiguo.

## Pruebas

Las pruebas usan correo sintético, claves ficticias y almacenamiento aislado. Los sondeos externos solo usan consultas públicas y dos llamadas anónimas de compatibilidad: no usan credenciales donadas ni privadas. La CI se divide en lógica, navegadores y archivos para que su tramo final no se cancele por acumular todo en un único trabajo de 40 minutos.
