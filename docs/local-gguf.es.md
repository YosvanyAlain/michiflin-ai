# IA local opcional para el agente sin cuota

**Estado:** integración optativa, desactivada por defecto. Se requiere un equipo o
servidor autorizado para alojar la aplicación y el modelo. No se habilita en
GitHub Actions: sus términos permiten Actions para desarrollo y pruebas y
ponen restricciones al uso como aplicación o servicio. No se cambian ni arrancan
otras instancias del bot.

## Qué hace

- Usa únicamente un servidor de inferencia `llama-server` en `127.0.0.1`, sin
  credenciales, proveedor de IA externo ni sus cuotas; si falla, no cambia a una API.
- Las preguntas de funciones, cálculo y otras herramientas existentes se resuelven
  primero por rutas conocidas. La IA puede redactar, conversar y proponer código;
  **no ejecuta código ni acciones privadas** desde este modo.
- Para una búsqueda explícita o una pregunta actual, consulta el buscador gratuito
  con **la pregunta actual**, no con el historial. Lee hasta dos páginas públicas y
  muestra extractos textuales y añade los enlaces consultados. Los textos web no
  se envían al modelo. El buscador y las páginas pueden fallar, aplicar límites,
  filtrar resultados o contener información falsa; los extractos no garantizan
  exactitud ni neutralizan todos los riesgos de un sitio malicioso.
- Se intenta bloquear búsquedas que contengan datos privados evidentes, pero la
  detección no es infalible. No envíes contraseñas ni tokens en consultas web.
- Historial breve en memoria del proceso, separado por chat y remitente; al
  reiniciar se pierde. El modelo solo recibe ese historial generado por el servidor;
  no recibe el historial no autenticado de la miniapp ni extractos web anteriores.
  No se registra el contenido desde el adaptador, pero el servidor y el sistema
  anfitrión deben configurarse para proteger sus registros y el acceso local.
- Una inferencia a la vez, sin cola ilimitada; si está ocupado, responde que se
  intente de nuevo. No es capacidad para miles de solicitudes simultáneas.

## Candidato medido

Se probó `Huihui-Qwen3-4B-Instruct-2507-abliterated.Q4_K_M.gguf`, declarado
«uncensored» por su autor; esa etiqueta no garantiza ausencia de rechazos ni
exactitud. Archivo: 2.497.281.312 bytes; revisión y SHA-256 fijos:

- Revisión GGUF: `c9e90669eeb205d5af35c28a3e9983fc9293c2ec`
- SHA-256: `b1324e2f1bd2e0610d591f54f93e38d90b187f69d1f215f4cace349eade678d7`
- Fuente: <https://huggingface.co/mradermacher/Huihui-Qwen3-4B-Instruct-2507-abliterated-GGUF>
- Motor probado: `llama.cpp` v0.8.0, commit `8642541b255bfb5dc39cd351a178be09013a55d6`.

En una prueba sintética aislada con dos hilos de CPU y contexto 3072, el modelo
ocupó aproximadamente 4,7 GB de memoria residente y generó unos 13,5 tokens/s;
el rendimiento real depende de la máquina y de la longitud de las preguntas.
Esos resultados no prueban capacidad de producción ni funcionamiento simultáneo
con el bot y las descargas. Un modelo de 1,7B fue más rápido pero falló una resta
verbal sencilla en esa misma prueba. En una prueba inicial el modelo obedeció
una instrucción maliciosa dentro de una fuente web; por eso la nueva ruta entrega
extractos sin pasar el texto de las páginas al modelo. Las fuentes aún pueden
contener afirmaciones falsas y deben revisarse.

## Activación en un servidor compatible

Solo después de escoger un anfitrión que permita este uso y verificar recursos,
instalar **manualmente** el modelo verificado y `llama-server`. No guardar los
pesos dentro del repositorio, de los respaldos de identidad ni de la miniapp. El
operador debe comprobar el SHA-256, limitar el acceso del equipo y el uso de CPU,
y supervisar el proceso. Por ejemplo, ajustando las rutas al servidor autorizado:

```sh
# El operador debe haber verificado antes el SHA-256 del archivo GGUF.
llama-server --model /ruta/segura/modelo.Q4_K_M.gguf \
  --host 127.0.0.1 --port 18971 --ctx-size 3072 --threads 2 \
  --threads-batch 2 --parallel 1 --no-webui
```

En el entorno del bot en ese mismo equipo, establecer `LOCAL_GGUF_ENABLED=1`
y, si se cambió el puerto, `LOCAL_GGUF_PORT`. **No exponer el puerto a Internet:**
`llama-server` no autentica al adaptador en esta configuración; otros procesos
locales podrían acceder. Proteger y revisar los registros del servidor, aislarlo
por usuario/servicio y controlar memoria/CPU. Si se reinicia o falla, el agente
comunica que la IA local no está disponible. Las búsquedas que pida el usuario
siguen saliendo al buscador; la inferencia y su contexto se quedan en el equipo.

La integración adapta instrucciones, herramientas y contexto; **no reentrena ni
modifica los pesos** del modelo. El código de la aplicación es distinto del
modelo, cuyos términos de licencia y procedencia deben verificarse por separado.

**Miniapps existentes:** no se modifican sus archivos ni su identificador de
compilación, y no se envía otra app automáticamente. Las miniapps antiguas
pueden seguir mostrando el texto anterior «sin IA» aunque el anfitrión habilite
un modelo local. El agente indica expresamente «IA local» en sus respuestas y
al preguntarle por sus funciones. Antes de activar esta opción para otras
personas, corregir esa descripción con un plan de compatibilidad aprobado;
no sustituir ni invalidar las instancias existentes.
