# Prueba acotada del modelo local en GitHub Actions

Esta modalidad es una prueba de desarrollo autorizada, no una promesa de alojamiento permanente o disponibilidad continua. Mantiene el bot en GitHub Actions sin crear una máquina externa.

## Activación de una única sesión

El workflow `michiflin.yml` admite `local_ai_test=true` por ejecución manual. También permite reservar **un número exacto de ejecución** con la variable `LOCAL_AI_TEST_RUN_NUMBER`. Esta segunda vía permite aprovechar el relevo normal tras publicar una revisión sin poner dos bots en marcha ni cancelar forzosamente la sesión activa.

La reserva solo coincide con ese `github.run_number`, en el primer intento. El número siguiente no hereda la prueba. No se cambia la variable recurrente `LOCAL_GGUF_TEST_REQUESTED`, cuyo valor de despliegue se mantiene en cero.

Para la sesión seleccionada se activa:

- Qwen3.5-4B Q4_K_M con pesos y motor fijados por SHA-256.
- Contexto del servidor y del cliente sincronizados a 6144 tokens.
- Perfil de entrada y salida ampliado.
- Síntesis local de extractos web públicos, con comprobaciones de formato y alternativa extractiva.
- Planificador local para el agente Con IA/API. Las consultas externas de las personas conservan el consentimiento y la cuota de cada persona. Una selección explícita de modelo evita el planificador.
- Duración de servicio del bot acotada a **1, 2 o 3 horas**, elegidas expresamente para la prueba, además de la preparación y el cierre. El valor inicial sigue siendo una hora. `local_ai_test_hours` o la variable `LOCAL_AI_TEST_HOURS` permiten escoger uno de esos tres valores fijos; cualquier otro valor vuelve a una hora. La disponibilidad real puede ser menor por fallos o cierre anticipado. Descarga, preparación y pruebas previas no forman parte de esa hora.

No se habilitan shell, escrituras, sesiones web privadas ni facultades de administrador para el modelo.

## Comprobación previa con código real

Tras arrancar el servidor, `tools/smoke_local_hybrid.py` ejercita las rutas reales de conversación, fuentes y coordinación. Usa un árbol de configuración temporal separado del estado restaurado del bot y consultas completamente sintéticas.

La inferencia GGUF es real. El transporte de API se simula expresamente: **esta prueba no acredita una respuesta real de un proveedor externo**, pero mantiene los envoltorios reales de autorización y liquidación de cuota. Verifica que la petición consume una unidad de la persona sintética, que otra consulta agotada no llega al transporte y que la conversación local no consume cuota de API.

El informe distingue propuestas locales reales, llamadas reales de calculadora, síntesis aceptada y vuelta a extracción. Que una propuesta local falle y delegue no se presentará como uso exitoso de herramientas. Las solicitudes HTTP externas están bloqueadas en la comprobación; las llamadas del modelo usan el socket Unix local autenticado.

Solo el informe sintético se guarda durante tres días. No se adjuntan conversaciones, estado de cuenta, credenciales o logs arbitrarios.

Si falla la preparación o el arranque, el bot conserva su ruta habitual. Si falla esta comprobación previa híbrida, se detiene el GGUF y se desactivan las funciones híbridas para que el bot normal pueda arrancar.

## Final y reversión

El proceso de inferencia se detiene antes de la copia cifrada final. El relevo ordinario no transmite `local_ai_test`; el siguiente número de ejecución tampoco coincide con la reserva. Los indicadores ordinarios de despliegue siguen aplicándose, sin alterarlos.

No ejecutar una segunda sesión de bot en paralelo. Mantener `cancel-in-progress: false`, el vaciado de trabajos en curso, el guardado cifrado final y el arranque normal de la sesión sucesora. Una prueba reservada que termine cancelada antes de comenzar debe inspeccionarse antes de reservar otro número; no usar cancelación forzada para arrancarla.
