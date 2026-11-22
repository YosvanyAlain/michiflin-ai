# Michiflin: un solo camino de trabajo

## Operación ordinaria

- **Rama de trabajo y despliegue: `main`.** No crear ramas de ensayo por defecto.
- **Bot** (`michiflin.yml`): única entrada de ejecución de la identidad existente. Conserva exclusión mutua, cierre ordenado y respaldo cifrado antes del relevo. No lanzar otra identidad ni cancelar una sesión activa para actualizar.
- **Validación y mantenimiento** (`tests.yml`): única entrada de comprobaciones. En push/PR a main ejecuta las suites existentes; no ejecuta la identidad ni recibe su configuración para los ensayos de modelo.
- En ejecución manual, `all` ejecuta suites y ensayo aislado de modelo en una sola ejecución. `tests`, `model`, `diagnostic` y `maintenance` permiten repetir solo una parte. Los cinco trabajos de suites son comprobaciones, no cinco bots.
- El mantenimiento de registros conserva su horario y permisos mínimos. El diagnóstico de respaldo sigue siendo una restauración aislada sin correo.
- Los siete workflows separados de comparación, mantenimiento, diagnóstico y preparación/ejecución/recuperación de next se retiran del árbol activo. Los de next no se sustituyen por otro despliegue.

Las ramas históricas se conservan mediante etiquetas de archivo verificadas antes de retirarlas. El inventario de referencia permite recuperarlas sin mantener docenas de caminos activos. No se borra el historial de ejecuciones ni los respaldos privados para ocultar fallos.

## Mantenimiento autorizado de los agentes

El usuario autorizó explícitamente estas dos correcciones, después de pedir conservar la versión anterior. La referencia original `agentes-estables-20261119` sigue recuperable.

1. **Herramientas repetidas:** las acciones de solo lectura idénticas reutilizan la observación dentro de la misma petición. Una propuesta local repetida delega en la API ya autorizada en vez de ejecutar otra vez. No hay caché entre peticiones o personas; no se altera la ruta de modelo exacto, cuota, permisos o límites de pasos.
2. **Formato de síntesis:** además del objeto original `answer` con citas inline, se admite la variante observada con una lista `citations` solo si cada referencia contiene una cita literal del extracto seleccionado y una etiqueta real. No se aceptan URLs generadas, etiquetas inventadas, citas fabricadas, objetos extra ni respuestas truncadas. Si el formato falla, sigue la alternativa extractiva. Esto valida formato/referencias, no demuestra semánticamente todas las afirmaciones de un modelo.

`stable-maintenance-profile.json` identifica exclusivamente los dos archivos modificados. Los restantes agentes y las funciones de autorización, cuota y enrutamiento se comprueban contra la referencia original.

Se corrigió también un fallo de **la prueba de regresión**, no de cuotas: los hashes de `ast.dump` producidos con Python 3.13 difieren en Python 3.12 aunque el código sea idéntico. Se conservan los hashes originales como procedencia y se añaden hashes del texto exacto de las funciones obtenidas de la revisión aceptada para comprobarlas de forma portable.

## Criterio de cierre

- Tests locales y controles estáticos aprobados.
- Suites remotas y ensayo aislado revisados, incluyendo síntesis aceptada en el caso sintético y una sola ejecución de calculadora.
- Integración por avance rápido en main, sin forzar historial.
- Archivo verificado de ramas históricas antes de retirarlas.
- Relevo del bot con respaldo, sin duplicar identidad. Un job `in_progress` por sí solo no demuestra entrega en teléfono.

La investigación de proveedores termina esta entrega **sin integraciones nuevas**: no se ha demostrado elegibilidad gratuita autenticada de ModelScope/Vercel para esta cuenta. No añadir créditos, cuentas ni fallbacks potencialmente de pago. Una integración futura requerirá un proveedor elegible y credenciales autorizadas; no bloquea al bot con sus proveedores actuales.

La comprobación en un teléfono físico depende del usuario y no puede certificarse desde CI. GitHub Actions sigue siendo el entorno de desarrollo/pruebas solicitado, no una garantía de alojamiento permanente.
