# Agentes aceptados y pruebas eficientes

La referencia aceptada por el usuario es la etiqueta `agentes-estables-20261119`, revisión `5da5d2b`. La publicación de referencia no garantiza disponibilidad de alojamiento: conserva el código que el usuario aprobó.

`stable-agents-profile.json` recoge el perfil y las sumas del código aceptado. `tests/test_stable_agents_contract.py` comprueba que los archivos del agente, la cola, el colaborador propietario y el lanzador no cambian mientras se completan otros trabajos. También protege las funciones de autorización/cuota, selección exacta y entrada de la app. No garantiza respuestas idénticas de un modelo generativo ni constituye una firma criptográfica de seguridad. Cualquier cambio futuro deliberado exige revisión y una nueva referencia explícita.

## Aprovechar las pruebas sin desperdiciar recursos

- El ensayo de modelo ejecuta **solo Qwen3.5 por defecto**. El modelo anterior se añade únicamente si se pide una comparación; ambas variantes se ejecutan en serie.
- No se descargan navegadores para la comprobación de inferencia. Solo se instalan las dependencias de la aplicación.
- Se reutiliza la caché de dependencias, no se guardan pesos GGUF, identidades, prompts privados ni credenciales en caché.
- Una misma descarga verificada sirve para las comprobaciones del código híbrido y las preguntas sintéticas de referencia.
- Los informes distinguen inferencia local real de transporte API simulado. Retienen las propuestas de calculadora y las respuestas sintéticas que permitan investigar rechazos de formato.
- El diagnóstico detallado de síntesis se limita a la prueba sintética. No añade registros de consultas reales al bot.
- Las sesiones de aceptación manuales pueden durar **1, 2 o 3 horas**, con cierre, copia cifrada y vuelta a la configuración habitual. No hay un bucle de activación local indefinida ni se cambia la cuota de una persona para superar un límite del proveedor.

No se eliminan controles de CI para ahorrar minutos. No se abren cuentas adicionales, cambian identidades, compran créditos ni evaden límites o verificaciones.

## Qué sigue pendiente y cómo se protege lo aceptado

1. Diagnosticar por qué la síntesis de fuentes no superó el formato. Hasta tener evidencia, mantener exactamente la alternativa extractiva que el usuario aprobó.
2. Medir propuestas repetidas de herramientas. No cambiar el agente aceptado a escondidas para deduplicarlas.
3. Añadir proveedores gratuitos nuevos solo después de verificar elegibilidad, cuotas y coste. Una integración sin clave disponible debe declararse como no activada/no probada con autenticación.
4. Conservar la limpieza ya verificada y las seis partes del respaldo remoto; no borrar credenciales ni fuentes actuales.

El bot permanece en GitHub Actions para estas pruebas de desarrollo, como pidió el usuario. No se ha creado un alojamiento externo ni se promete continuidad permanente del servicio. Una futura migración se decidirá cuando el usuario lo indique.
