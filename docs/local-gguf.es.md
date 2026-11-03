# IA local opcional: prueba acotada

El dueño solicitó probar IA local en el bot que funciona actualmente en GitHub.
**Esto no convierte GitHub Actions en un hospedaje recomendado ni garantiza
compatibilidad con sus condiciones:** GitHub limita Actions a desarrollo y pruebas
y puede restringir usos como servicio continuo. La migración a un proveedor que
permita el uso previsto queda para más adelante, sin arrancar otra instancia ahora.
[Condiciones de Actions](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features#actions).

## Cómo se habilita esta prueba

La configuración general `LOCAL_GGUF_ENABLED` permanece desactivada por defecto.
En este proyecto, el flujo de GitHub prueba iniciar el modelo en cada sesión y
solo habilita la función después de verificar la salud y la autenticación. El
indicador explícito `LOCAL_GGUF_ACTIONS_TEST=1` es obligatorio dentro de Actions.
El dueño puede poner la variable de repositorio `LOCAL_GGUF_TEST_REQUESTED=0`
para no prepararlo en el siguiente relevo. Una prueba fallida mantiene el bot y
el agente anterior; no cambia a un proveedor de IA ni arranca otra identidad.

El modelo y el motor se descargan de fuentes públicas en **cada runner efímero**:
el archivo GGUF pesa 2.497.281.312 bytes. Ambos se verifican por SHA-256 y se
borran del runner al finalizar. No se suben a cachés, backups, miniapps ni al
repositorio. La descarga y el arranque pueden retrasar la siguiente sesión y
están sujetos a disponibilidad, transferencia y límites de los proveedores.

El motor se ejecuta con un usuario distinto, sin acceso a `/home` ni a Internet,
con sistema de archivos protegido y únicamente un socket Unix local autorizado.
Usa una clave temporal, no impresa ni incluida en argumentos; no se guardan sus
registros de entrada/salida. Se limita a dos hilos, una inferencia, 7 GiB de
memoria máxima y prioridad baja. Un fallo de preparación deja el agente sin
modelo; un fallo posterior se comunica y no redirige la pregunta a una API.
Estos controles reducen riesgos, pero no garantizan disponibilidad ni seguridad
absoluta. La cuenta, el estado y las copias no se montan en el servidor de IA.

## Reparto entre usuarios

- Cada remitente autenticado puede tener **una** petición de modelo activa o
  esperando, con la misma regla para dueños y demás usuarios.
- Una sola inferencia funciona a la vez; hasta cuatro personas adicionales
  esperan por orden de llegada. El plazo máximo de espera es 90 segundos. Si la
  cola está llena, el turno caduca o la persona ya tiene una petición, se le
  informa sin reproducir la tarea automáticamente.
- Mantenimiento, revocación y kill switch prevalecen. Las búsquedas tienen dos
  puestos separados y no ocupan un turno del modelo. Son límites de protección,
  no una promesa de miles de respuestas simultáneas ni de espera garantizada.
- El historial corto de IA vive solo en el proceso, separado por chat y remitente.
  La miniapp no puede aportar un historial arbitrario para el modelo.

## Qué hace y qué no hace

Las herramientas conocidas atienden primero cálculo, clima y otras funciones.
La IA local puede conversar, redactar y proponer código; **no ejecuta código ni
acciones privadas**. La búsqueda explícita o de información actual consulta un
buscador con la pregunta actual y muestra extractos y enlaces. No pasa páginas
externas al modelo; las fuentes aún pueden equivocarse o ser engañosas. No envíes
contraseñas a consultas web: el filtrado preventivo no es infalible.

Las miniapps existentes se conservan **sin cambiar sus archivos ni su build** y
no se distribuye otra automáticamente. Sus textos antiguos pueden seguir diciendo
«sin IA»: al interactuar reciben un aviso y cada respuesta del agente indica que
el modo puede usar IA local. Esto corrige la interpretación, pero no modifica la
etiqueta estática antigua. Cuando la prueba se desactiva, no aparece ese aviso.

## Modelo y mediciones

Candidato: `Huihui-Qwen3-4B-Instruct-2507-abliterated.Q4_K_M.gguf`, anunciado por
su autor como «uncensored»; esa etiqueta no garantiza ausencia de rechazos ni
exactitud. No se modifican ni se reentrenan los pesos.

- [Modelo GGUF](https://huggingface.co/mradermacher/Huihui-Qwen3-4B-Instruct-2507-abliterated-GGUF)
- Revisión: `c9e90669eeb205d5af35c28a3e9983fc9293c2ec`
- SHA-256: `b1324e2f1bd2e0610d591f54f93e38d90b187f69d1f215f4cace349eade678d7`
- `llama.cpp` 0.8.0, commit `8642541b255bfb5dc39cd351a178be09013a55d6`,
  binario oficial `b11960` Ubuntu x64 SHA-256
  `31cc3a6bc638ac6c48dc4b1b74888f1eaadf3d14227c1e5f67daf9a49541d97a`.

Con dos hilos y contexto 3072, una medición aislada anterior dio aproximadamente
4,7 GB de RAM y 13,5 tokens/s. Es una referencia, no una garantía bajo carga de
usuarios y tareas reales. Un 1,7B fue más rápido pero falló una resta sencilla.
Las respuestas deben revisarse.

## Uso en otro anfitrión, más adelante

El operador debe verificar la licencia del modelo, los hashes, los recursos y que
el proveedor permita el uso. Puede iniciar un `llama-server` confiable en un
socket local protegido o `127.0.0.1`, establecer `LOCAL_GGUF_ENABLED=1` y, si usa
una clave local, `LOCAL_GGUF_KEY_FILE`. No exponer el servidor a Internet ni
guardar pesos con el estado privado. Limitar CPU, memoria, logs y acceso local.
