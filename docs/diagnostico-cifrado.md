# Diagnóstico cifrado de respaldos

El diagnóstico es de solo lectura: restaura una copia aislada, no inicia el
correo y no modifica las instantáneas. Sus resultados son contadores y valores
booleanos, pero incluso esos datos pueden ser sensibles en un repositorio
público. Por eso **ya no se suben en texto claro**.

## Uso

1. Genera localmente una pareja de claves X25519. Conserva la **clave privada**
   fuera de GitHub, cifrada con una clave que solo controles tú. No publiques
   ni pegues la clave privada en conversaciones, logs o variables de Actions.
2. Codifica los 32 bytes de la **clave pública** en Base64 URL con relleno y
   proporciona ese valor como `diagnostic_public_key` al ejecutar el workflow
   `Validacion y mantenimiento`, opción `diagnostic`. Si falta o no es válida,
   el trabajo termina antes de restaurar ninguna copia.
3. Descarga los artefactos `sync-diagnostic-encrypted` y
   `backup-inventory-encrypted`. Cada uno contiene un archivo `.enc` autenticado.
   Están disponibles durante un día y no son copias de los respaldos.
4. Localmente, usa `tools.diagnostic_artifact_crypto.decrypt_blob(blob,
   private_key_base64url)` en un proceso de confianza. El parámetro es la clave
   privada de 32 bytes codificada en Base64 URL con relleno. Analiza el JSON en
   memoria; no publiques los resultados ni los escribas en un directorio
   compartido. Un cambio de bytes o una clave equivocada produce un error de
   autenticación.

El formato emplea X25519 efímero, HKDF-SHA256 y ChaCha20-Poly1305. La clave
privada permanece fuera de GitHub y los resultados no se suben en texto claro.
La copia aislada y los JSON sí existen temporalmente en el runner de GitHub; el
workflow intenta eliminarlos al terminar. Esto requiere confiar en ese entorno
y no garantiza un borrado físico. La etiqueta y el tamaño de los artefactos
son visibles en Actions.

Las ramas y ejecuciones históricas que todavía contengan el workflow anterior
no se corrigen automáticamente: no uses sus tareas de diagnóstico hasta
verificar que tengan esta protección o emplear un repositorio privado.
