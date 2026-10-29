# Recuperar la copia independiente del historial del dueño

La copia `michiflin-owner-history-only-….bin`, cuando existe, se guarda en la release privada de respaldos, cifrada con `STATE_BACKUP_PASSPHRASE`. Es una exportación independiente: **no contiene la base de la cuenta ni restaura la identidad, sus claves, su correo o las instancias locales de WebXDC**. Incluye los mensajes y actualizaciones retenidos, sus metadatos, el estado conservado y los adjuntos que se encontraron; descarta los chats individuales inequívocamente ajenos. Los registros ambiguos se conservan.

Antes de retirar copias antiguas se comparan las revisiones disponibles con la exportación y se verifica una descarga cifrada. El archivo también conserva versiones distintas de los documentos y adjuntos recuperables de esas revisiones: `indice-historico.json` relaciona cada revisión con los JSON deduplicados en `revisiones/` y los archivos identificados por SHA-256 en `archivos/`. `adjuntos.json` indica los nombres originales, huellas y ausencias de la revisión principal; las demás revisiones tienen sus propios documentos de adjuntos. Algunos adjuntos pueden haber faltado ya en todas las copias disponibles. Las actualizaciones WebXDC exportadas no garantizan reconstruir el almacenamiento local de una app.

Para comprobar y extraer una copia que hayas descargado por un canal privado, desde este repositorio y con las dependencias instaladas:

```sh
# Proporciona STATE_BACKUP_PASSPHRASE al entorno de forma privada.
# Nunca pegues su valor en el chat, en el repositorio ni en una orden guardada en el historial.
python tools/restore_owner_history.py /ruta/privada/copia.enc --check
python tools/restore_owner_history.py /ruta/privada/copia.enc --destino /ruta/privada/nueva
```

La extracción exige una carpeta que no exista, valida los nombres y las huellas antes de escribir y aplica permisos privados. Para interpretar el contenido, abre `LEEME.txt`, `conversaciones.json`, `mensajes.json` e `indice-historico.json`. El exportador elimina coincidencias exactas de claves y credenciales que conoce, y se detiene si detecta alguna dentro de un adjunto; no puede demostrar que no queden secretos desconocidos, transformados o comprimidos en los datos personales. Trata el archivo como información confidencial incluso después de sanearlo.

El sello anti-downgrade y el respaldo de la **identidad nueva** son independientes de esta copia y deben conservarse. Borrar una referencia o reescribir un historial Git no garantiza el borrado inmediato de objetos inaccesibles, cachés, relays, destinatarios ni dispositivos externos.

Si no conoces la frase porque solo está guardada como secreto de GitHub, la recuperación puede ejecutarse en un corredor privado autorizado con `STATE_BACKUP_PASSPHRASE`, `GITHUB_TOKEN` y `STATE_BACKUP_REPO` disponibles, tras restaurar el estado de la identidad nueva: omite la ruta del archivo y usa `python tools/restore_owner_history.py --check` o `--destino` hacia una carpeta privada temporal. No publiques ni subas el contenido extraído sin protegerlo; para trasladarlo, cífralo con una clave que controle el dueño.
