# Acabado visual · familia Creator Studio

Segundo lote de la auditoría del 19 de noviembre de 2026. **Alcance parcial**, no certificación integral ni validación de producción.

## Referencia y cambios

Referencia solicitada por el propietario: su repositorio público `YosvanyAlain/creator-studio`, diseño Prism. Michiflin ya compartía buena parte de sus tokens; este lote evita sustituir el producto o añadir dependencias y corrige la mezcla de tratamientos visuales:

- portada con saludo, título jerarquizado, mascota local y dos acciones bilingües reales: preguntar al agente y explorar la web;
- marca compacta en la cabecera de inicio, sin afectar títulos largos de otras vistas;
- búsqueda inmediata, con selectores avanzados dentro de un `<details>` nativo solo en inicio; las demás vistas conservan opciones desplegadas;
- sombras más contenidas y bordes coherentes con índigo/violeta, sin el tinte verde residual;
- tarjetas de accesos adaptables, sin desbordar a 320 px; tamaños mínimos de 44 px para las nuevas acciones;
- conservación de las paletas existentes, modo oscuro/sistema, aspecto pixel y preferencia de movimiento reducido;
- altura de carrusel conservada al rotar hacia banners más cortos para no desplazar la búsqueda mientras se lee.

Todo funciona con CSS, SVG y fuentes locales: no se agregan solicitudes de red, paquetes JS ni imágenes remotas.

## Compatibilidad

Se añade la copia **exacta** de `i18n.js` del paquete `ec7e67e` al archivo de compatibilidad y se incluye en su conjunto de recursos reemplazados. El manifiesto existente no cambia. Se conserva el hash `c8a5f423ba` para la época sintética `test-epoch`, con rechazo de alteraciones y de otras épocas. Las correcciones del lote 1 permanecen.

## Pruebas

Nuevo archivo `tests/test_creator_finish.py`: 10 casos parametrizados Chromium/WebKit, español/inglés, temas claro/oscuro/sistema y anchos 320/390/768/1280. Comprueba navegación de ambas acciones, controles de búsqueda plegados/desplegados, selección de modo/fuente, reflujo y estabilidad del carrusel. Esas pruebas usan un **bridge simulado**, no producción.

```sh
python -m pytest -q tests/test_creator_finish.py tests/test_slow_add_forms.py \
  tests/test_sheet_accessibility.py tests/test_audit_base_compat.py
```

El informe externo de este lote contiene los resultados de la regresión ampliada y distingue cada ejecución por correo real de las pruebas simuladas. No sumar reruns como casos únicos.

## Producción y límites

Se creó una identidad nueva con el núcleo real de Delta Chat y se intentó SecureJoin con la invitación suministrada. No se completó en 90 segundos y no se envió el saludo. La conectividad local estaba operativa; la causa del handshake pendiente **no está confirmada**. La configuración consultada del repositorio mantenía `OWNER_ONLY=1` e `INVITE_AUTO_ACCESS=0`; no se cambiaron estos controles ni se abrió el bot a terceros.

Las pruebas funcionales por correo de este lote se ejecutan contra un **bot de QA aislado**, no contra datos de producción. El host Chromium con adaptador RPC no equivale al cliente nativo Android/iOS/desktop ni valida realtime.

Publicar a `main` activa las pruebas de CI. El workflow del bot continúa sin disparador `push`: una sesión programada posterior puede cargar los cambios. No se fuerza el reinicio de la sesión en curso.

## Resultado local cerrado

- Regresión ampliada: **374 aprobadas, 0 fallos, 0 errores, 1 omitida** (375 casos recogidos), 1468,25 s. Las **41 subpruebas** aprobadas se informan aparte; el XML suma esos eventos y no debe interpretarse como 416 casos únicos.
- Comprobación final focalizada: 56 aprobadas y 1 omitida, 161,07 s; es un rerun con solapamiento, no se suma al total anterior. La omisión corresponde a la unión de vídeo: falta ffmpeg en este entorno.
- Recorrido final por correo real de QA: 12 comprobaciones, 33,4 s, sin errores JavaScript, borrador conservado. El paquete recibido coincide byte a byte con los recursos locales. En el primer recorrido el clima agotó su tiempo de espera; el reintento explícito pasó y también pasó el recorrido final completo.
- Ruff F/E9 y sintaxis de todos los scripts JavaScript aprobados.
