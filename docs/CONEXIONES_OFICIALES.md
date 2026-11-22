# Guía para activar las conexiones oficiales

## Antes de empezar

Estas conexiones son **opcionales y de lectura limitada**: YouTube consulta el canal y suscripciones del usuario; Facebook, su perfil y, con permiso adicional, sus publicaciones; TikTok, su perfil y, con permiso adicional, sus vídeos. No son una conexión a Google Drive, no publican contenido ni permiten descargar contenido privado. La autorización real de los tres servicios sigue pendiente y depende de las condiciones de cada proveedor.

Cada persona concede o retira su propio permiso desde la plataforma. No escribas contraseñas, códigos temporales, QR, cookies, tokens ni secretos en este chat, en el repositorio ni en capturas. Los códigos y resultados viajan por el chat privado cifrado y pueden permanecer en su historial o en las copias; los tokens se guardan cifrados en el servidor. Retirar una conexión local no borra necesariamente las copias antiguas: revoca también el acceso en el proveedor si la app indica que no pudo hacerlo.

El repositorio ya tiene un secreto llamado `WEB_SESSION_KEY`. **No lo sustituyas ni lo regeneres:** hacerlo puede impedir abrir las bóvedas y sesiones existentes. No es necesario conocer su valor para seguir esta guía.

## Dónde guardar los valores

1. Entra en [Secrets and variables → Actions del repositorio](https://github.com/YosvanyAlain/michiflin-ai/settings/secrets/actions), inicia sesión en GitHub y abre la pestaña **Secrets**.
2. Usa **New repository secret** para cada nombre indicado abajo. Pega allí el valor correspondiente del proveedor; no lo envíes por chat. GitHub permite actualizar secretos, pero no muestra sus valores después de guardarlos. [1](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)
3. Las dos opciones `FACEBOOK_READ_POSTS` y `TIKTOK_READ_VIDEOS` son **variables**, no secretos. Déjalas sin crear o en `0` hasta que el proveedor apruebe esos permisos. Para activarlas después, ve a la pestaña **Variables** de la misma pantalla y crea la correspondiente con valor `1`.
4. Una sesión del bot que ya estaba iniciada no recibe cambios de secretos o variables en caliente. Se necesita una sesión posterior, con relevo seguro y una sola instancia de la cuenta. Guardar las credenciales no es lo mismo que probar el inicio de sesión.

Si algún proveedor pide facturación, pagos, verificación empresarial o documentos que no deseas proporcionar, **detente y consúltalo antes de aceptar**. Las pantallas y los requisitos pueden cambiar.

## 1. YouTube: primer servicio para probar

1. Abre [Google Cloud Console](https://console.cloud.google.com/) y selecciona o crea un proyecto. No actives facturación ni compras sin decidirlo.
2. En **APIs y servicios → Biblioteca**, busca **YouTube Data API v3** y pulsa **Habilitar**. [1](https://developers.google.com/youtube/v3/guides/authentication)
3. En **Google Auth Platform** (o **Pantalla de consentimiento OAuth**), configura los datos de la app y el público que corresponda. Para una prueba con cuenta personal, si está en modo externo de pruebas añade tu propia cuenta como usuario de prueba. Google puede exigir verificación antes de autorizar usuarios generales y las autorizaciones de prueba pueden caducar. [5](https://developers.google.com/identity/protocols/oauth2/production-readiness/overview)
4. En **Clientes → Crear cliente**, elige **Televisores y dispositivos con entrada limitada**. No elijas una clave de API ni un cliente web: Michiflin usa el flujo de dispositivo. Guarda la credencial OAuth creada. [1](https://developers.google.com/identity/protocols/oauth2/limited-input-device)
5. En los secretos de GitHub crea por separado:
   - `GOOGLE_OAUTH_CLIENT_ID`: ID de cliente OAuth de Google.
   - `GOOGLE_OAUTH_CLIENT_SECRET`: secreto de ese mismo cliente.
6. El permiso solicitado es `https://www.googleapis.com/auth/youtube.readonly`; habilita o declara solo ese alcance si la consola lo solicita. No concede edición ni publicación. Si Google muestra advertencias o requiere revisión, no las interpretes como una aprobación y no las evadas. [1](https://developers.google.com/youtube/v3/guides/authentication)
7. Avísame solo **«ya guardé los dos secretos de YouTube»**. Tras verificar que la versión correspondiente esté funcionando y se haya iniciado una sesión posterior, abre tu miniapp en tu chat privado autorizado: **Ajustes → Privacidad y controles → Conexiones oficiales → YouTube → Conectar con Google**. Introduce el código solo en el sitio oficial indicado, revisa los permisos y vuelve a la miniapp para pulsar **Ya autoricé**. Comprueba después canal o suscripciones; si hay error, comparte únicamente el mensaje sin códigos ni datos privados.

## 2. Facebook: perfil y, solo con aprobación, publicaciones

1. Abre [Meta for Developers](https://developers.facebook.com/apps/) y crea o selecciona una app adecuada a tu caso. Sigue los requisitos reales de Meta, incluido cualquier modo de desarrollo, usuarios de prueba y revisión.
2. Añade **Facebook Login** y en sus ajustes habilita **Login from Devices / Inicio de sesión desde dispositivos** si la consola lo ofrece. El flujo oficial muestra un código para [facebook.com/device](https://www.facebook.com/device). [1](https://developers.facebook.com/documentation/facebook-login/for-devices)
3. En GitHub guarda estos secretos, separados:
   - `FACEBOOK_APP_ID`: ID de la app de Meta.
   - `FACEBOOK_CLIENT_TOKEN`: **client token**, no contraseña, token de una persona ni app secret. Meta lo sitúa en los ajustes avanzados de la app; la ubicación puede cambiar. [1](https://developers.facebook.com/documentation/facebook-login/for-devices)
4. El perfil usa `public_profile`. El permiso `user_posts` es adicional y está sujeto a revisión y a usos permitidos de Meta. No lo actives si Meta no ha aprobado el uso concreto. Si aprueban explícitamente el acceso a publicaciones para esta app, configura la variable `FACEBOOK_READ_POSTS=1` y, tras el siguiente relevo seguro, vuelve a autorizar para concederlo. [1](https://developers.facebook.com/docs/permissions/reference/user_posts)
5. En la miniapp, selecciona **Facebook → Conectar con Facebook**, escribe el código solo en el sitio oficial, concede los permisos que desees y vuelve para pulsar **Ya autoricé**. Comprueba primero **Mi perfil**. Una app en desarrollo puede limitar qué cuentas pueden probarla; la aprobación no está garantizada.

## 3. TikTok: QR y Display API

1. Abre [TikTok for Developers](https://developers.tiktok.com/) y registra una app. Agrega **Login Kit** y configura el producto y los permisos de **Display API** que permitan el perfil. Si corresponde, utiliza el entorno de pruebas del proveedor; acceso general requiere la revisión que este indique. [5](https://developers.tiktok.com/doc/getting-started-create-an-app?enter_method=left_navigation)
2. TikTok requiere configurar una **URL de redirección** en Login Kit. Esta integración exige una URL `https://` exacta, sin parámetros ni fragmento, en un dominio que controles; no admite `localhost`. El módulo verifica esa URL en el resultado del sondeo del QR, pero **no crea por sí mismo un sitio público ni un servidor de callback**. Antes de registrar una dirección, confirma qué dominio, página y verificación te exige TikTok. No inventes una URL ni uses una dirección que no controles. Las políticas, los términos y la propiedad de URL pueden ser requisitos para producción. [5](https://developers.tiktok.com/doc/getting-started-create-an-app?enter_method=left_navigation)
3. En GitHub guarda por separado:
   - `TIKTOK_CLIENT_KEY`: client key de la app.
   - `TIKTOK_CLIENT_SECRET`: client secret de la app.
   - `TIKTOK_OAUTH_REDIRECT_URI`: la URL HTTPS exacta registrada en TikTok.
4. El alcance base es `user.info.basic`. La lista de vídeos requiere `video.list`, aprobación del proveedor y consentimiento de la persona. Si TikTok aprueba ese uso concreto, configura la variable `TIKTOK_READ_VIDEOS=1`; tras el siguiente relevo seguro, vuelve a autorizar. [4](https://developers.tiktok.com/docs/en/display-api-get-started)
5. En la miniapp, selecciona **TikTok → Conectar con TikTok**, escanea el QR temporal con TikTok, revisa los permisos y confirma. Vuelve a la miniapp y pulsa **Ya autoricé**. Prueba **Mi perfil** y, solo si fue aprobado, **Mis vídeos**. El QR y sus códigos no se deben compartir. El flujo usa la autorización oficial por QR; su compatibilidad con tu app y tu cuenta debe comprobarse realmente. [1](https://developers.tiktok.com/docs/en/login-kit-qr-code-authorization?enter_method=left_navigation)

## Si algo no funciona

- **«El servidor aún no tiene una app autorizada»**: revisa los nombres de secretos, que los valores correspondan al mismo cliente y que se haya iniciado una nueva sesión. La presencia de nombres no demuestra que los valores sean válidos.
- **Permiso denegado, app no verificada o solicitud caducada**: revisa el estado de pruebas/revisión y los permisos en la consola oficial; inicia otra solicitud solo si corresponde. No envíes el código ni el QR para diagnóstico.
- **Perfil vacío o publicaciones/vídeos deshabilitados**: comprueba el consentimiento, el alcance aprobado y la variable correspondiente. Estos servicios no ofrecen aquí el feed completo.
- **Desconectar**: usa el botón de la miniapp. Si el proveedor no confirma la revocación, entra en los ajustes de seguridad de esa plataforma y retira allí el acceso.

Las pruebas automatizadas existentes usan respuestas simuladas. Se considera conexión real verificada únicamente después de completar la autorización con una cuenta autorizada y consultar un dato permitido sin exponerlo en registros ni en el chat de soporte.
