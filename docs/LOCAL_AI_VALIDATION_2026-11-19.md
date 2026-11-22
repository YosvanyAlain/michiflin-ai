# Validación de IA local e integración por persona — 2026-11-19

## Estado y límites del despliegue

El código de esta mejora se desarrolla sin restaurar la identidad del bot en las pruebas de modelos. No se utilizan chats reales, credenciales de proveedores ni consultas de usuarios. Las preguntas y respuestas conservadas son sintéticas.

La sesión de producción inspeccionada (`44941455393`, revisión `aa65f15`) tiene `LOCAL_GGUF_TEST_REQUESTED=0`: **no hay un GGUF opcional activo en esa sesión**. Existe un lanzador temporal; no constituye alojamiento permanente de pesos o inferencia. Actualizar este código no activa un servidor ni reinicia el bot.

GitHub Actions se utiliza aquí para desarrollo y comparación de software. No debe convertirse en alojamiento permanente de inferencia para el bot. Un despliegue continuo requiere un servidor autorizado; no se han creado máquinas externas ni prometido disponibilidad continua de servicios gratuitos.

## Cambios implementados

- `LOCAL_AGENT_COORDINATOR=1`: permite hasta dos propuestas locales de herramientas de solo lectura en el agente **Con IA/API**, elegido expresamente. El programa mantiene el control de herramientas, destinos públicos y consultas. La API existente produce la respuesta final. Elegir un modelo concreto evita el planificador local.
- La cuota de la persona se comprueba antes del trabajo híbrido. Las llamadas de API siguen pasando por `ai.complete`/`complete_exact`, `_allowance` y `_settle`: una petición se liquida una vez según las reglas existentes. No se crea una bolsa compartida alternativa ni se amplían permisos del colaborador reservado al propietario.
- El planificador no recibe historial privado ni claves. Solo ve la consulta actual y observaciones acotadas. La detección de consultas privadas es una precaución, **no una garantía de identificar todo dato sensible**.
- `LOCAL_WEB_SYNTHESIS=1`: permite redactar sobre extractos públicos ya seleccionados. Los enlaces finales pertenecen al programa, no al modelo. JSON inválido, truncamiento, etiquetas desconocidas o enlaces generados hacen volver a citas extractivas. Validar etiquetas **no demuestra que cada afirmación esté respaldada**, ni elimina toda inyección de instrucciones. La interfaz identifica el texto como síntesis de IA y pide comprobar enlaces.
- `LOCAL_GGUF_EXTENDED=1`: contexto del lanzador de 6144 tokens, entrada de conversación hasta 12000 caracteres, historial más amplio y salida normal de hasta 1024 tokens / código hasta 1536. Se cuenta la plantilla completa antes de inferir en el perfil ampliado y se rechaza el exceso sin cortar silenciosamente la pregunta. El plazo de generación solicitado es 90 segundos; no garantiza un tiempo total de respuesta de 90 segundos, pues preparación y evaluación de entrada también consumen tiempo.
- La cola compartida conserva su funcionamiento y reduce la salida a 192 tokens cuando hay personas esperando. Permanecen los controles de pausa, aislamiento por identidad, SSRF y restricciones de acciones de escritura.

Los tres indicadores nuevos están desactivados por defecto. Para un servidor externo compatible deben configurarse tanto servidor como cliente; no basta con aumentar el presupuesto en Python. El lanzador sincroniza su contexto con el mismo indicador de perfil.

## Primera comparación real: contexto 3072

Ejecución: https://github.com/YosvanyAlain/michiflin-ai/actions/runs/44963052162

| Modelo cuantizado Q4_K_M | Comprobaciones | Tiempo acumulado de 8 preguntas |
|---|---:|---:|
| Huihui-Qwen3-4B-Instruct-2507-abliterated (anterior) | 7/8 | 67,64 s |
| Qwen3.5-4B (candidato) | 8/8 | 51,15 s |

El modelo anterior respondió `19` donde correspondía `17`. Son comprobaciones pequeñas y una sola generación por pregunta; **no equivalen a un benchmark general de inteligencia**. El tiempo acumulado excluye descarga y arranque, depende del runner y no prueba una ventaja universal de velocidad.

## Comparación ampliada: contexto 6144

Ejecución: https://github.com/YosvanyAlain/michiflin-ai/actions/runs/44963259023

El candidato arrancó con el motor fijado `llama.cpp b11960` y contexto 6144. En las doce preguntas sintéticas obtuvo 9/12 comprobaciones estrictas:

- Una segunda operación de descuento fue incorrecta (`20` en lugar de `22`).
- Una respuesta de fuentes expresó correctamente la ausencia de horario confirmado, pero no respetó el formato de cita pedido.
- El extracto con una instrucción maliciosa no consiguió que produjera la palabra solicitada por el atacante, pero faltó la etiqueta de fuente exigida. Eso falla la comprobación estricta; no demuestra resistencia general a inyecciones.
- Recuperó el código inicial de una entrada de 3811 tokens. Esa petición tardó aproximadamente 95 segundos, incluida la evaluación de entrada y las llamadas de plantilla/tokenización.
- Pico del grupo de servicio: 3613315072 bytes (aprox. 3,36 GiB), con máximo configurado de 7 GiB, dos hilos y CPU limitada al 200 %. No incluye todo el consumo del sistema, descargas ni del bot con sus navegadores.

La misma ejecución terminó correctamente para el modelo anterior: 8/12 comprobaciones, 199,42 segundos acumulados y 3804016640 bytes de pico (aprox. 3,54 GiB). El candidato obtuvo 9/12, 171,08 segundos acumulados y aprox. 3,36 GiB. Los tiempos de esta tanda incluyen tokenización y preparación de cada petición; no deben mezclarse directamente con los de la primera tanda.

**Decisión:** seleccionar `qwen35` como variante predeterminada del lanzador temporal, conservando `legacy` para reversión. Es una mejora modesta respaldada por estas pruebas, no una garantía universal. Ambos fallaron la segunda operación; el anterior además afirmó que la biblioteca no abrió el sábado cuando el dato solo decía que no había horario confirmado. El candidato expresó la incertidumbre correctamente aunque incumplió el formato.

No se han cambiado las variables remotas para activar inferencia en producción. El workflow de comparación queda bajo ejecución manual para no repetir descargas y pruebas de modelos en cada cambio de código. Estos resultados exigen conservar cálculo determinista, formatos comprobables y respuestas de reserva. No justifican prometer que razona correctamente en cualquier tarea.

## Integridad y recuperación

Las variantes permitidas usan revisión, tamaño y SHA-256 fijos. La descarga del modelo usa cuatro rangos HTTPS exactos, cada uno una sola vez; rechaza respuestas parciales incorrectas y errores de cuota, sin espejos ni intentos de eludir límites. El motor se verifica por SHA-256. El proceso de inferencia se ejecuta con UID separado, sin red y sin acceso a `/home`, mediante socket Unix autenticado.

La variante `legacy` permanece disponible para revertir una selección de prueba. Los indicadores pueden volver a cero sin cambiar identidad, claves, historial, invitaciones o aplicaciones existentes. Si el servidor no arranca, el bot conserva su ruta habitual: no se sustituye por un proveedor oculto.

## Comprobaciones de software

La revisión `d264262` superó las cinco suites de CI (lógica, navegador, archivos, móvil y notificaciones):
https://github.com/YosvanyAlain/michiflin-ai/actions/runs/44963430185

La ejecución local seleccionada final superó 331 pruebas. Incluye pruebas de liquidación real de cuota por persona, sin transporte externo, que verifican que varios pasos se cobran una sola vez, los errores no gastan cuota y otra persona agotada no hereda la autorización.

No se ha confirmado esta mejora en un teléfono físico ni se ha activado el GGUF en la sesión de producción.
