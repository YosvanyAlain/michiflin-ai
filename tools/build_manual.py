#!/usr/bin/env python3
"""Build the Spanish owner/developer manual from this checkout.

Requires docs/requirements-manual.txt and DejaVu fonts. The manual is guidance,
not a replacement for reviewing the full, independently delivered source code.
"""
from __future__ import annotations

import ast
import html
import os
import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from PIL import Image as PillowImage, ImageDraw
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (BaseDocTemplate, Flowable, Frame, Image, LongTable,
                                PageBreak, PageTemplate, Paragraph, Preformatted, Spacer, Table,
                                TableStyle)
from reportlab.platypus.tableofcontents import TableOfContents

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path(os.environ.get("MICHIFLIN_MANUAL_OUTPUT", ROOT / "docs" / "Manual_Michiflin.pdf"))
FONTS = Path(os.environ.get("DEJAVU_FONT_DIR", "/usr/share/fonts/truetype/dejavu"))
for name, filename in (("Body", "DejaVuSans.ttf"), ("BodyBold", "DejaVuSans-Bold.ttf"),
                       ("Serif", "DejaVuSerif.ttf"), ("SerifBold", "DejaVuSerif-Bold.ttf"),
                       ("Mono", "DejaVuSansMono.ttf")):
    pdfmetrics.registerFont(TTFont(name, str(FONTS / filename)))
pdfmetrics.registerFontFamily("Body", normal="Body", bold="BodyBold", italic="Body", boldItalic="BodyBold")

NAVY = colors.HexColor("#11282e")
INK = colors.HexColor("#1b3034")
MUTED = colors.HexColor("#5c7073")
TEAL = colors.HexColor("#0d8578")
PALE = colors.HexColor("#e9f3f0")
SAND = colors.HexColor("#faf1e3")
GOLD = colors.HexColor("#da9c3d")
LINE = colors.HexColor("#d9e5e1")
WHITE = colors.white
PAGE_W, PAGE_H = A4
MARGIN_X = 48
BODY_W = PAGE_W - 2 * MARGIN_X

styles = getSampleStyleSheet()
styles.add(ParagraphStyle(name="BodyM", fontName="Body", fontSize=10.1, leading=15.3,
                          textColor=INK, spaceAfter=8.5, allowWidows=0, allowOrphans=0))
styles.add(ParagraphStyle(name="SmallM", parent=styles["BodyM"], fontSize=8.7, leading=12.7, spaceAfter=5))
styles.add(ParagraphStyle(name="TinyM", parent=styles["BodyM"], fontSize=7.8, leading=11.0, spaceAfter=4))
styles.add(ParagraphStyle(name="ChapterM", fontName="SerifBold", fontSize=19, leading=25,
                          textColor=NAVY, spaceBefore=9, spaceAfter=14, keepWithNext=True))
styles.add(ParagraphStyle(name="SubM", fontName="BodyBold", fontSize=12, leading=17,
                          textColor=TEAL, spaceBefore=11, spaceAfter=6, keepWithNext=True))
styles.add(ParagraphStyle(name="LabelM", fontName="BodyBold", fontSize=8.5, leading=12,
                          textColor=TEAL, spaceBefore=4, spaceAfter=6, keepWithNext=True))
styles.add(ParagraphStyle(name="BulletM", parent=styles["BodyM"], leftIndent=15, firstLineIndent=-11,
                          bulletIndent=0, spaceAfter=5))
styles.add(ParagraphStyle(name="CodeM", fontName="Mono", fontSize=8.4, leading=12.4,
                          textColor=INK, leftIndent=9, rightIndent=6, spaceBefore=2, spaceAfter=2))
styles.add(ParagraphStyle(name="TableHeadM", fontName="BodyBold", fontSize=8.5, leading=12.2,
                          textColor=WHITE, spaceAfter=0))
styles.add(ParagraphStyle(name="TableM", fontName="Body", fontSize=8.35, leading=12.3,
                          textColor=INK, spaceAfter=0, wordWrap="CJK"))
styles.add(ParagraphStyle(name="CaptionM", fontName="Body", fontSize=8, leading=11,
                          textColor=MUTED, alignment=TA_CENTER, spaceAfter=6))
styles.add(ParagraphStyle(name="TocM", fontName="Body", fontSize=10.4, leading=16,
                          textColor=INK, leftIndent=0, firstLineIndent=0, spaceBefore=4))


def escape(text: object) -> str:
    return html.escape(str(text), quote=False)


def p(text: str, style: str = "BodyM") -> Paragraph:
    return Paragraph(escape(text).replace("\n", "<br/>"), styles[style])


def bullet(text: str) -> Paragraph:
    return Paragraph("• " + escape(text), styles["BulletM"])


def sub(text: str) -> Paragraph:
    return p(text, "SubM")


def note(title: str, text: str, warning: bool = False) -> Table:
    content = [Paragraph(f'<font name="BodyBold">{escape(title)}</font><br/>{escape(text)}', styles["SmallM"])]
    box = Table([[content]], colWidths=[BODY_W])
    box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), SAND if warning else PALE),
                             ("BOX", (0, 0), (-1, -1), 0.4, GOLD if warning else LINE),
                             ("LEFTPADDING", (0, 0), (-1, -1), 12),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                             ("TOPPADDING", (0, 0), (-1, -1), 9),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    box.spaceAfter = 10
    return box


def code(text: str) -> Table:
    block = Preformatted(text.strip("\n"), styles["CodeM"])
    box = Table([[block]], colWidths=[BODY_W])
    box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f1f5f3")),
                             ("BOX", (0, 0), (-1, -1), 0.4, LINE),
                             ("LEFTPADDING", (0, 0), (-1, -1), 4),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                             ("TOPPADDING", (0, 0), (-1, -1), 7),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    box.spaceAfter = 10
    return box


def table(headers: list[str], rows: list[list[str]], widths: list[float]) -> LongTable:
    data = [[p(label, "TableHeadM") for label in headers]]
    data += [[p(cell, "TableM") for cell in row] for row in rows]
    result = LongTable(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    result.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), NAVY),
                                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, colors.HexColor("#f6f9f7")]),
                                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                ("LINEBELOW", (0, 0), (-1, 0), 0.7, TEAL),
                                ("LINEBELOW", (0, 1), (-1, -1), 0.25, LINE),
                                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                                ("TOPPADDING", (0, 0), (-1, -1), 7),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    result.spaceAfter = 12
    return result


class FlowDiagram(Flowable):
    def __init__(self):
        super().__init__()
        self.width, self.height = BODY_W, 86

    def draw(self):
        c = self.canv
        labels = [("TÚ", "Delta Chat"), ("VISTA", "miniapp"), ("MENSAJES", "correo E2E"),
                  ("CEREBRO", "bot Python"), ("FUENTES", "web / IA")]
        width = 82
        gap = (self.width - width * len(labels)) / (len(labels) - 1)
        for i, (eyebrow, label) in enumerate(labels):
            x = i * (width + gap)
            c.setFillColor(PALE if i % 2 == 0 else colors.HexColor("#f1f5f3"))
            c.roundRect(x, 18, width, 52, 9, fill=1, stroke=0)
            c.setFillColor(TEAL)
            c.setFont("BodyBold", 7)
            c.drawCentredString(x + width / 2, 52, eyebrow)
            c.setFillColor(INK)
            c.setFont("BodyBold", 8.3)
            c.drawCentredString(x + width / 2, 34, label)
            if i < len(labels) - 1:
                c.setStrokeColor(TEAL)
                c.setLineWidth(1)
                c.line(x + width + 3, 44, x + width + gap - 3, 44)
                c.line(x + width + gap - 6, 47, x + width + gap - 3, 44)
                c.line(x + width + gap - 6, 41, x + width + gap - 3, 44)


class Manual(BaseDocTemplate):
    def __init__(self, filename: Path):
        super().__init__(str(filename), pagesize=A4, leftMargin=MARGIN_X, rightMargin=MARGIN_X,
                         topMargin=56, bottomMargin=49, title="Michiflin AI - Manual integral",
                         author="Michiflin AI", subject="Guía de uso, arquitectura, mantenimiento y migración")
        frame = Frame(MARGIN_X, 49, BODY_W, PAGE_H - 105, leftPadding=0, rightPadding=0,
                      topPadding=0, bottomPadding=0, id="body")
        self.addPageTemplates(PageTemplate(id="manual", frames=frame, onPage=self.page_art))
        self._anchors: dict[str, int] = {}

    def page_art(self, canvas, doc):
        canvas.saveState()
        if doc.page == 1:
            canvas.setFillColor(NAVY)
            canvas.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
            canvas.setFillColor(colors.HexColor("#173d42"))
            canvas.roundRect(36, 374, PAGE_W - 72, 320, 25, fill=1, stroke=0)
            canvas.setStrokeColor(colors.HexColor("#2ca497"))
            canvas.setLineWidth(1)
            canvas.line(58, 325, PAGE_W - 58, 325)
            avatar = PillowImage.open(ROOT / "assets" / "michiflin-avatar.png").convert("RGBA")
            mask = PillowImage.new("L", avatar.size, 0)
            ImageDraw.Draw(mask).ellipse((0, 0, avatar.width - 1, avatar.height - 1), fill=255)
            avatar.putalpha(mask)
            canvas.drawImage(ImageReader(avatar), PAGE_W - 211, 522, width=145, height=145, mask="auto")
            canvas.setFont("BodyBold", 10)
            canvas.setFillColor(colors.HexColor("#80cdbd"))
            canvas.drawString(59, 743, "GUÍA INTEGRAL · EDICIÓN DEL PROPIETARIO")
            canvas.setFillColor(WHITE)
            canvas.setFont("SerifBold", 34)
            canvas.drawString(59, 594, "Michiflin")
            canvas.drawString(59, 550, "AI")
            canvas.setFont("Body", 13)
            canvas.setFillColor(colors.HexColor("#d0e9e1"))
            canvas.drawString(60, 478, "Cómo usarlo, entenderlo,")
            canvas.drawString(60, 456, "mantenerlo y migrarlo.")
            canvas.setFont("BodyBold", 9)
            canvas.setFillColor(colors.HexColor("#80cdbd"))
            canvas.drawString(59, 286, "DE LA IDEA AL CÓDIGO · DEL CHAT AL SERVIDOR")
            canvas.setFont("Body", 9)
            canvas.setFillColor(colors.HexColor("#bfd5d0"))
            canvas.drawString(59, 255, "Guía técnica y práctica en español")
            canvas.drawString(59, 237, "29 de octubre de 2026 · versión de referencia")
            canvas.setFont("Body", 8)
            canvas.drawString(59, 77, "El ZIP de código y este PDF son entregas separadas.")
        else:
            canvas.setStrokeColor(LINE)
            canvas.line(MARGIN_X, PAGE_H - 39, PAGE_W - MARGIN_X, PAGE_H - 39)
            canvas.setFont("BodyBold", 7.5)
            canvas.setFillColor(TEAL)
            canvas.drawString(MARGIN_X, PAGE_H - 30, "MICHIFLIN AI")
            canvas.setFont("Body", 7.4)
            canvas.setFillColor(MUTED)
            canvas.drawRightString(PAGE_W - MARGIN_X, PAGE_H - 30, "MANUAL DEL PROPIETARIO")
            canvas.line(MARGIN_X, 36, PAGE_W - MARGIN_X, 36)
            canvas.setFont("Body", 7.5)
            canvas.drawString(MARGIN_X, 23, "Uso · arquitectura · mantenimiento · migración")
            canvas.drawRightString(PAGE_W - MARGIN_X, 23, str(doc.page - 1))
        canvas.restoreState()

    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name == "ChapterM":
            title = flowable.getPlainText()
            key = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "chapter"
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(title, key, level=0, closed=False)
            self.notify("TOCEntry", (0, title, self.page - 1, key))


def chapter(title: str) -> Paragraph:
    return p(title, "ChapterM")


def screenshot_pair() -> Table:
    paths = (ROOT / "docs/manual-media/maintenance-real-es.png", ROOT / "docs/manual-media/queue-simulated-es.png")
    left = Image(str(paths[0]), width=5.3 * cm, height=8.35 * cm)
    right = Image(str(paths[1]), width=4.1 * cm, height=8.88 * cm)
    t = Table([[left, right], [p("Mantenimiento · Delta Chat Desktop real, cuenta sintética", "CaptionM"),
                               p("Cola · captura simulada con aviso de progreso", "CaptionM")]],
              colWidths=[BODY_W / 2, BODY_W / 2], hAlign="CENTER")
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                           ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                           ("TOPPADDING", (0, 0), (-1, -1), 4),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    t.spaceAfter = 12
    return t


# Every top-level Python component is named and explained in Spanish below.
MODULES = {
    "__init__.py": "Identidad y versión del paquete Python. Es la puerta de entrada compartida de los módulos.",
    "access.py": "Política de dueño, sesiones temporales y autorizaciones individuales; revisa cada trabajo con la autorización vigente.",
    "admin.py": "Administración, intentos de acceso, diagnósticos y redacción prudente de registros.",
    "agent.py": "Agente conversacional: interpreta intenciones, elige herramientas y conserva historiales acotados.",
    "ai.py": "Registro de proveedores y modelos remotos, selección, salud, límites locales y reparto de solicitudes.",
    "answer.py": "Respuestas sin IA a partir de fuentes públicas: extracción, clasificación de frases y citas.",
    "app.py": "Cableado principal de Delta Chat: eventos, recepción ordenada, arranque y parada de trabajadores.",
    "browser.py": "Capturas, PDF y renderizado de páginas mediante Chromium, cuando el entorno es compatible.",
    "cf_safety.py": "Presupuesto conservador local para el uso compartido de Cloudflare Workers AI.",
    "chat.py": "Envío y consulta de mensajes de Delta Chat, actualizaciones WebXDC y seguimiento de entrega.",
    "coder.py": "Programador: valida rutas y archivos, prepara proyectos y ejecuta comprobaciones permitidas en aislamiento.",
    "commands.py": "Analiza comandos del chat, los traduce a operaciones y aplica ajustes válidos.",
    "config.py": "Lee variables de entorno, define valores iniciales y aplica límites y controles de acceso.",
    "documents.py": "Extrae texto de documentos y lo prepara para una consulta, sin tratar el archivo como instrucciones fiables.",
    "egress.py": "Pasarela HTTPS del sandbox: valida destinos públicos y controla el uso de un proxy configurado.",
    "extras.py": "Utilidades sin modelo, entre ellas conversión, idioma, hora y subtítulos cuando existen.",
    "facebook_account.py": "Integración oficial opcional y de lectura, anterior a la retirada de tarjetas de plataformas.",
    "files.py": "Examina enlaces directos, detecta tamaño y formato, descarga con Range y protege reanudaciones.",
    "images.py": "Selecciona proveedores y modelos de imagen, valida respuestas y genera un archivo compacto.",
    "links.py": "Representa enlaces con comandos cortos para navegar desde el chat.",
    "live_web.py": "Coordina el navegador remoto efímero y su canal de tiempo real en chats cifrados individuales.",
    "mail_profiles.py": "Aplica perfiles de entrega verificados por buzón y límites de tamaño por parte.",
    "maintenance.py": "Aviso del dueño en dos idiomas, activación y desactivación de la pausa operativa.",
    "media.py": "Inspección y descarga de medios públicos mediante yt-dlp; disponibilidad depende de cada fuente.",
    "media_worker.py": "Trabajador de medios privados que usa solo la sesión del usuario autorizado en el sandbox.",
    "miniapp.py": "Construye la WebXDC, vincula cada instancia al chat y atiende operaciones y actualizaciones.",
    "music.py": "Consulta metadatos públicos de música de Internet Archive y conserva las declaraciones de licencia.",
    "names.py": "Nombres y alias de comandos en español e inglés.",
    "net.py": "Cliente HTTP y control de URLs, direcciones públicas, redirecciones y límites de lectura.",
    "pager.py": "Paginación de listas y navegación por resultados en el chat.",
    "perf.py": "Métricas y diagnóstico acotado de CPU, memoria, disco, red y tiempos de respuesta.",
    "personal_ai.py": "Bóveda de APIs propias por persona, transporte cifrado, descubrimiento y selección explícita.",
    "private_media.py": "Entrega al trabajador aislado únicamente el estado del usuario y sitio correspondientes.",
    "profile.py": "Actualiza cuidadosamente el perfil y estado público del bot sin reemplazar texto personalizado.",
    "progress.py": "Mensajes y reacciones de progreso, con actualizaciones acotadas y finalización de tareas.",
    "purge_users.py": "Herramienta excepcional de limpieza autorizada; no debe ejecutarse como mantenimiento rutinario.",
    "queue.py": "Cola de descarga, preparación, verificación, división, pausas, recuperación y entrega por partes.",
    "reader.py": "Lector compacto: reduce HTML a contenido legible y divide páginas largas.",
    "remote_worker.py": "Entrada del navegador aislado: recibe comandos limitados y devuelve imágenes de la vista.",
    "sandbox.py": "Crea procesos aislados con namespaces, chroot y permisos reducidos; falla cerrado si faltan requisitos.",
    "smart_agent.py": "Agente de investigación con un conjunto acotado de herramientas públicas y sin escritura privada.",
    "social_vault.py": "Protege credenciales de conexiones oficiales opcionales por remitente.",
    "state.py": "Estado persistente, ajustes personales, contadores, generación de pausa y guardado atómico.",
    "tiktok_account.py": "Integración oficial opcional de lectura, distinta del inicio de sesión web manual.",
    "tools.py": "Búsqueda, noticias, tiempo, clima, tasas, cálculo y otras herramientas de consulta.",
    "usage.py": "Contadores por persona y prioridades de uso para compartir recursos de forma controlada.",
    "util.py": "Funciones comunes de idioma, limpieza, nombres, fechas, tamaños y textos.",
    "web.py": "Orquesta acciones sobre enlaces: lector, captura, PDF y altas en la cola de archivos o medios.",
    "web_vault.py": "Guarda el estado del navegador cifrado, con separación por usuario y sitio.",
    "youtube_account.py": "Integración OAuth de lectura opcional; no demuestra descargas de vídeo ni acceso ilimitado.",
}


def module_inventory() -> list[list[str]]:
    paths = sorted((ROOT / "michiflin").glob("*.py"))
    missing = [path.name for path in paths if path.name not in MODULES]
    if missing:
        raise RuntimeError("Manual descriptions missing for modules: " + ", ".join(missing))
    rows = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = [node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                 and not node.name.startswith("_")][:5]
        markers = ", ".join(names) if names else "constantes y metadatos"
        rows.append([path.name, MODULES[path.name] + "\nPuntos de entrada: " + markers + "."])
    return rows


def build_story() -> list:
    story: list = [Spacer(1, 1), PageBreak(), chapter("Cómo aprovechar esta guía")]
    story += [p("Este manual es un mapa para usar el bot y también para comprenderlo, repararlo y trasladarlo. No necesitas memorizarlo: empieza por la operación diaria; usa la referencia de archivos cuando vayas a cambiar código."),
              note("Dos entregas distintas", "El ZIP contiene el código fuente real, sus pruebas y archivos de configuración de ejemplo. Este PDF explica qué hace cada parte y cómo reproducirla. Las capturas y vídeos ornamentales de docs/media no forman parte del ZIP de correo; el PDF se entrega aparte."),
              p("El sistema integra Delta Chat, una miniapp WebXDC, un servicio Python y proveedores externos. El proyecto no controla la disponibilidad ni el precio de terceros. La propia aplicación puede cambiar; consulta los textos visibles y el código de la versión que estés usando."),
              sub("Leyenda honesta"),
              bullet("Comprobado: una prueba concreta terminó correctamente en el entorno indicado."),
              bullet("Simulado: la interfaz se probó con datos y un puente de prueba; no representa la cuenta real."),
              bullet("Pendiente: depende de infraestructura, proveedor o verificaciones que no se han completado."),
              note("Límite importante", "La instalación actual usa corredores efímeros de GitHub. El respaldo habitual no incluye los parciales de la cola: cambiar de corredor puede perder bytes y progreso. No se promete recuperación completa ni ejecución exactamente una vez.", True),
              PageBreak(), chapter("Índice")]
    toc = TableOfContents()
    toc.levelStyles = [styles["TocM"]]
    story += [toc, PageBreak(), chapter("01 · El bot en una mirada"),
              p("Michiflin AI es un bot que conversa por correo mediante Delta Chat. La miniapp es su panel; el proceso Python es quien valida permisos, consulta servicios y prepara archivos. El teléfono no contiene el servidor del bot."),
              FlowDiagram(),
              p("El correo transporta mensajes y actualizaciones WebXDC. En un chat individual cifrado, el contenido viaja protegido entre las cuentas; el servidor de correo puede observar metadatos de encaminamiento, como remitente, destinatario y hora. Los proveedores externos reciben los datos que les envíes para procesar una solicitud."),
              table(["Pieza", "Qué hace", "Dónde vive"], [
                  ["Delta Chat", "Cuenta, chats, claves y transporte de mensajes.", "Cliente del teléfono y cuenta del bot."],
                  ["Miniapp", "Interfaz ES/EN, formularios, vistas y almacenamiento propio de esa instancia.", "Archivos webxdc/ empaquetados como .xdc."],
                  ["Servidor", "Autorización, cola, agentes y comunicación con servicios.", "Proceso Python iniciado con bot.py."],
                  ["Estado", "Ajustes, identidad, metadatos de cola y bóvedas.", "CONFIG_DIR y copia cifrada privada."],
                  ["Archivos de trabajo", "Descargas parciales y piezas preparadas.", "JOB_DIR; no forma parte del respaldo actual."],
              ], [100, 250, BODY_W - 350]),
              note("No confundas copias", "El código fuente se puede descargar de nuevo. El estado privado, las claves y los archivos parciales son otros datos: no están dentro del ZIP de código y requieren su propio respaldo."),
              sub("Del primer mensaje a la respuesta"),
              bullet("El bot recibe un evento o revisa el cursor de mensajes pendientes."),
              bullet("Comprueba quién envió la solicitud, el tipo de chat, el permiso actual y las pausas."),
              bullet("La operación usa herramientas locales o consulta un proveedor externo si corresponde."),
              bullet("El resultado vuelve a la miniapp o al chat; los archivos pueden dividirse en partes."),
              bullet("El estado se guarda y se respalda por separado, sujeto a las limitaciones de la infraestructura."),
              PageBreak(), chapter("02 · Recorrido de uso"),
              sub("Inicio y navegación"),
              p("Abre el chat verificado del bot y entra en la miniapp más reciente. Inicio reúne el buscador, accesos directos y un resumen de la cola. La barra inferior conduce a IA, Web, Apps y Descargas; Ajustes se abre desde el icono correspondiente. Las etiquetas pueden variar con la versión y el idioma."),
              sub("Buscar, leer y abrir la web"),
              p("Escribe una pregunta o pega un enlace. El lector ligero prioriza texto y enlaces; una captura o PDF necesita un navegador disponible en el servidor. El navegador Live es otra función: muestra una sesión remota en un cliente compatible con tiempo real. No todas las páginas o clientes lo admiten."),
              p("En Live puedes intentar iniciar sesión en un sitio y elegir Guardar sesión. Esa acción guarda el estado cifrado por persona y sitio; no demuestra que el proveedor acepte el inicio de sesión. La sesión del navegador normal de tu teléfono no se copia automáticamente. Si aparece CAPTCHA, complétalo tú cuando sea posible; no intentes eludirlo."),
              sub("IA, agente y modelos personales"),
              p("El agente sin cuota intenta resolver tareas con herramientas y fuentes, sin activar por sí solo la generación de imágenes. El agente con IA puede hacer varias llamadas al modelo elegido. Verifica siempre el resultado, las fuentes y el coste real en el proveedor."),
              p("En Mis APIs y modelos, una persona autorizada puede registrar su propio proveedor HTTPS, revisar los modelos detectados y elegir capacidades. La clave no se vuelve a mostrar; el proveedor elegido sí recibe la clave y las consultas. Detectar una etiqueta o activar una casilla no demuestra que esa función exista o sea gratis."),
              bullet("Usa un proveedor de confianza y comprueba sus tarifas, límites y permisos."),
              bullet("Selecciona el modelo propio de forma explícita; no se usa de manera automática."),
              bullet("No pegues claves en mensajes de chat, capturas ni URLs. La interfaz necesita cifrado compatible."),
              sub("Imágenes y Programador"),
              p("En imágenes elige fuente y modelo; la disponibilidad solo se confirma al generar. Los límites locales son controles del bot, no una promesa de cuota oficial. Algunos servicios pueden cobrar, estar saturados o cambiar. Revisa la licencia y no presentes una imagen fallida como generada."),
              p("En Programador describe lo que quieres, inspecciona los archivos y su diferencia, ejecuta las pruebas disponibles y exporta el proyecto si corresponde. El aislamiento puede deshabilitar la ejecución cuando el servidor no reúne requisitos. Un proyecto exportado o una respuesta del modelo no prueban que el programa esté terminado ni sea seguro."),
              sub("YouTube y música"),
              p("YouTube ofrece búsqueda y la opción de mostrar más resultados cuando hay otra página. Encontrar un vídeo no garantiza poder descargarlo: el sitio, el formato, la conexión y sus condiciones pueden impedirlo. Descarga solo lo que tengas derecho a descargar."),
              p("La música obtenida de fuentes con licencias declaradas debe revisarse caso por caso. La API oficial de Deezer ofrece muestras, no equivale a una API para descargar canciones completas, incluso si una persona tiene una suscripción."),
              PageBreak(), chapter("03 · Descargas, pausas y recuperación"),
              sub("De un enlace al chat"),
              p("En Descargas pega el enlace y elige Archivo, Vídeo o Audio. El bot examina el recurso, crea un trabajo y muestra su progreso. Según los ajustes, prepara una o varias partes antes de entregarlas al chat. Un enlace admitido no garantiza que el sitio permita la descarga."),
              p("Los archivos generales pueden llegar como partes numeradas con SHA-256. Los medios largos pueden convertirse en segmentos reproducibles. Conserva todas las partes en Archivos, no en una galería que renombre o recodifique, y usa Unir partes para reconstruir y comprobar el resultado."),
              sub("Controles del dueño"),
              p("Mantenimiento y aviso permite al dueño escribir mensajes en español e inglés y pausar las tareas de forma global. Las personas autorizadas actuales y las nuevas reciben el aviso al intentar usar el bot. Desactivar mantenimiento permite continuar trabajos elegibles, sin quitar pausas manuales ni alterar permisos."),
              p("El kill switch es una pausa de seguridad asociada a la persona. Ambos controles son cooperativos: una petición ya enviada a un proveedor o una entrega que entró en el core de correo puede finalizar. No prometen cancelar cobros ni borrar correo de salida."),
              screenshot_pair(),
              sub("Qué significa el progreso retenido"),
              p("Para enlaces directos, el bot conserva parciales compatibles y los reanuda mediante HTTP Range. Cuando el origen ofrece un ETag fuerte, lo usa con If-Range; si el recurso cambia o el rango es incompatible, protege el parcial y muestra un error. Sin validador fuerte, puede reanudar con una advertencia: el usuario debe verificar el archivo."),
              p("En una prueba privada, 1.048.576 bytes sobrevivieron a un reinicio del proceso en el mismo disco; el servidor recibió el offset y el validador correctos y el resultado coincidió con el SHA-256 de la fixture. Otra prueba cambió la fuente y comprobó que el parcial no se mezcló. Son pruebas reales locales, no una garantía para todos los sitios."),
              note("Si cambia el corredor", "En GitHub Actions el disco local se pierde al terminar el job. El estado cifrado puede conservar el trabajo, pero no el archivo parcial de /tmp. La interfaz informa cuando faltan bytes; todavía se necesita almacenamiento persistente apropiado para conservarlos entre corredores.", True),
              sub("Si una entrega es incierta"),
              p("No pulses Reintentar repetidamente. Revisa primero el chat y la lista de partes: un mensaje aceptado por el servidor puede tardar o llegar después. El bot registra intención y evita algunos reenvíos automáticos, pero un fallo o una copia antigua impiden garantizar ausencia absoluta de duplicados."),
              PageBreak(), chapter("04 · Acceso y datos personales"),
              sub("El dueño y las invitaciones"),
              p("La instalación productiva mantiene OWNER_ONLY=1. Solo el dueño puede abrir una sesión temporal controlada y aprobar a cada invitado por separado. Autorizar a alguien no transforma sus datos en datos del dueño; al cerrar o revocar se bloquean sus trabajos según la política vigente. No se reactivan permisos históricos por una actualización."),
              p("Mantenimiento tiene prioridad. No publiques invitaciones, direcciones internas, códigos de confirmación, tokens, cookies o archivos de estado. La aplicación no debe configurarse como un servicio abierto al público sin revisar expresamente seguridad y términos del proveedor."),
              sub("Qué contiene una copia cifrada"),
              p("El estado puede incluir claves de la cuenta Delta Chat, notas, ajustes, metadatos de trabajos, enlaces y bóvedas cifradas. El respaldo usa una contraseña independiente para cifrar el conjunto y se almacena en un repositorio privado. Trátalo como sensible aunque esté cifrado."),
              bullet("Conserva la clave de respaldo y WEB_SESSION_KEY en un gestor seguro y fuera del código."),
              bullet("No cambies WEB_SESSION_KEY durante una migración sin un plan de recuperación: la bóveda antigua podría quedar ilegible."),
              bullet("Los adjuntos grandes y parciales de JOB_DIR no están incluidos por defecto."),
              bullet("Borrar una sesión actual no elimina necesariamente copias antiguas ni revoca la sesión ante el proveedor."),
              sub("Qué sucede al actualizar la miniapp"),
              p("Cada versión de la WebXDC tiene almacenamiento independiente. El bot envía una instancia nueva a los chats pertinentes y deja de aceptar operaciones ordinarias de la antigua. La instancia anterior se conserva: no la borres hasta que hayas verificado un respaldo o una migración de los datos que te importan. La conservación del mensaje, por sí sola, no demuestra una migración de proyectos."),
              PageBreak(), chapter("05 · Cómo se construyó"),
              p("El proyecto combina piezas existentes: deltabot-cli y deltachat2 para correo y eventos; Python para la lógica; HTML, CSS y JavaScript para WebXDC; requests para HTTP; yt-dlp y ffmpeg para medios; Playwright para navegación; y cryptography para bóvedas y respaldos. No hay un modelo de lenguaje pesado permanente dentro de Actions."),
              table(["Capa", "Archivos clave", "Responsabilidad"], [
                  ["Entrada", "bot.py, michiflin/app.py", "Arranque, eventos, comprobación del remitente y cierre ordenado."],
                  ["Interfaz", "webxdc/*, michiflin/miniapp.py", "Vista local y puente de operaciones por WebXDC."],
                  ["Seguridad", "access.py, state.py, net.py, sandbox.py", "Permisos, pausas, límites de red y aislamiento."],
                  ["Trabajo", "queue.py, files.py, media.py", "Descargar, guardar, comprobar, partir y entregar."],
                  ["Servicios", "ai.py, personal_ai.py, images.py, tools.py", "Modelos, proveedores propios y utilidades."],
                  ["Operación", "state_backup.py, .github/workflows/", "Copias cifradas, pruebas, sesión y relevo."],
              ], [90, 165, BODY_W - 255]),
              sub("Diseño de una solicitud de miniapp"),
              p("La vista envía un identificador de solicitud y la sesión actual. miniapp.py comprueba la instancia, su chat cifrado, el remitente, el estado y el identificador antes de llamar a una operación registrada. La respuesta puede viajar en fragmentos y se representa en la interfaz. Una sesión nueva invalida solicitudes antiguas que no deben repetirse a ciegas."),
              sub("Diseño de la cola"),
              p("El ciclo normal es: en cola, descargando, empaquetando, listo, enviando y terminado. Pausa, cancelación y fallo son transiciones separadas. El trabajador comprueba límites de disco y autorización, calcula huellas y guarda la intención de entrega. En una recuperación protege fuentes dudosas en lugar de sobrescribirlas."),
              sub("Aislamiento y datos no confiables"),
              p("Las páginas, archivos y respuestas de modelos se consideran datos, no instrucciones de administración. El navegador y algunas tareas se ejecutan en procesos aislados, con permisos y red restringidos cuando el sistema lo soporta. Ningún sandbox debe tratarse como una garantía absoluta: mantén dependencias al día y revisa su configuración."),
              PageBreak(), chapter("06 · Reconstruir el proyecto desde el ZIP"),
              note("Trabaja primero en una copia", "No ejecutes una segunda instancia con la cuenta productiva. Para aprender o probar, usa una cuenta sintética y carpetas nuevas. No importes la copia privada en una máquina que no controles."),
              p("El ZIP enviado por Delta Chat se genera a partir de archivos rastreados de un commit e incluye un listado SHA256SUMS.txt. No incluye .git, cuentas, estados, claves ni historiales. Si faltan archivos multimedia de documentación no afecta al código del bot."),
              sub("Paso 1 · Extraer y verificar"),
              code("unzip Michiflin_codigo_fuente.zip -d proyecto\ncd proyecto/Michiflin-AI\nsha256sum -c SHA256SUMS.txt"),
              p("La verificación compara cada archivo con el manifiesto incluido. Protege frente a corrupción accidental, pero si un atacante modificara también el manifiesto necesitarías una huella obtenida por otro canal de confianza."),
              sub("Paso 2 · Preparar las herramientas"),
              p("Para un servidor Linux utiliza Python 3.11 o posterior; el workflow actual usa Python 3.12. Necesitarás Git para desarrollo, ffmpeg, 7-Zip, y Chromium de Playwright para funciones web. Deno es recomendable para ciertos formatos de yt-dlp. Los requisitos de aislamiento son adicionales."),
              code("python3 -m venv .venv\n. .venv/bin/activate\npython -m pip install -r requirements.txt\npython -m playwright install chromium"),
              p("El entorno virtual se crea localmente y no forma parte del ZIP. Las versiones con rangos pueden cambiar; para una instalación reproducible en el futuro, conserva además un inventario de versiones y hashes verificados en tu propia copia de operación."),
              sub("Paso 3 · Entender la configuración"),
              p("Lee .env.example y README.md. El programa no carga automáticamente un archivo .env: el servicio o el sistema deben entregar las variables al proceso. Establece OWNER_ADDRESS y OWNER_ONLY=1 para un despliegue privado. Define CONFIG_DIR y JOB_DIR en rutas controladas y separa los secretos del repositorio."),
              table(["Nombre", "Función", "Cuidado"], [
                  ["OWNER_ADDRESS", "Identifica al dueño.", "Verifica que sea la cuenta correcta; no sustituye una verificación de claves."],
                  ["OWNER_ONLY", "Arranque privado del servicio.", "Mantener 1 en producción."],
                  ["WEB_SESSION_KEY", "Protege bóvedas personales y sesiones web.", "Estable, larga y respaldada por separado."],
                  ["BOT_EPOCH", "Vincula las instancias de miniapp con el despliegue.", "Cambiarla invalida instancias antiguas."],
                  ["CONFIG_DIR / JOB_DIR", "Estado de cuenta / archivos temporales.", "No confundir el respaldo del estado con el de los parciales."],
                  ["STATE_BACKUP_*", "Ubicación y frase de cifrado del respaldo.", "Nunca publiques valores ni los coloques en el ZIP."],
              ], [145, 166, BODY_W - 311]),
              p("En GitHub Actions, los nombres actuales de secretos incluyen OWNER_ADDRESS, WEB_SESSION_KEY, BOT_EPOCH, STATE_BACKUP_REPO, STATE_BACKUP_PASSPHRASE y STATE_GITHUB_TOKEN. El workflow entrega el último al proceso como GITHUB_TOKEN para el respaldo. MICHIFLIN_GITHUB_TOKEN y las claves de proveedores son opcionales según la función. La existencia de un nombre no demuestra que su valor sea válido."),
              p("Configura esos valores tú mismo en los secretos del repositorio o en el gestor de tu servidor. Utiliza permisos mínimos, no los publiques en archivos, mensajes ni capturas y guarda las claves de recuperación por separado. Una cuenta nueva o un token nuevo no sustituyen las claves antiguas del bot."),
              sub("Paso 4 · Cuenta de prueba y arranque"),
              p("Solo en una carpeta y cuenta de prueba puedes inicializar una identidad nueva con el asistente del bot. Una identidad nueva no es una restauración: cambia la dirección y las claves. Para una cuenta existente, usa una restauración verificada en lugar de inicializar otra."),
              code("python bot.py --config-dir ./prueba init \\\n  DCACCOUNT:nine.testrun.org\npython bot.py --config-dir ./prueba serve"),
              p("Antes de abrir el servicio para un ensayo, configura el dueño de prueba y una clave de sesión de prueba. La dirección y la invitación de esa cuenta deben permanecer privadas. El CLI también ofrece list, link, export e import; consulta python bot.py --help y las advertencias de la versión instalada."),
              sub("Paso 5 · Ejecutar pruebas"),
              code("python -m unittest tests.test_logic tests.test_safety -v\npython -m unittest tests.test_operational_maintenance -v\npython -m unittest tests.test_personal_maintenance -v\npython -m unittest tests.test_owner_delivery -v\npython -m unittest tests.test_webxdc tests.test_webxdc_browser -v"),
              p("La suite completa y los requisitos de navegador y sandbox están en .github/workflows/tests.yml. Un test omitido no cuenta como aprobado; una prueba local no sustituye una prueba real del correo o de un proveedor externo."),
              PageBreak(), chapter("07 · Anatomía y recreación de cada módulo"),
              p("La lista siguiente cubre todos los archivos Python de michiflin/. Los nombres de entrada son un mapa, no una API pública garantizada. Para recrear una pieza exactamente, extrae su archivo del ZIP, verifica SHA256SUMS.txt y lee sus pruebas; para modificarla, cambia primero la prueba que describe el comportamiento esperado."),
              table(["Archivo", "Responsabilidad y puntos de entrada"], module_inventory(), [112, BODY_W - 112]),
              sub("Interfaz WebXDC"),
              table(["Archivo", "Qué reconstruye"], [
                  ["index.html", "Estructura de la aplicación, áreas y vistas principales."],
                  ["app.css", "Diseño adaptable, colores, estados y accesibilidad visual."],
                  ["core.js", "Puente con Delta Chat, solicitudes, sesiones, fragmentos y reintentos seguros."],
                  ["app.js", "Navegación, acciones, estado visible y control de formularios."],
                  ["i18n.js", "Textos y etiquetas en español e inglés."],
                  ["coder.js", "Interfaz del Programador y control de proyectos e historial."],
                  ["live.js", "Interfaz de navegación en tiempo real y manejo de su cierre."],
                  ["mp4.js", "Rutinas de unión de segmentos multimedia en el cliente."],
                  ["brands.js / icon.png", "Identidad visual y recursos gráficos usados por la app."],
                  ["manifest.toml", "Metadatos requeridos para el paquete .xdc."],
              ], [135, BODY_W - 135]),
              sub("Otros archivos que no debes olvidar"),
              p("bot.py inicia el paquete. state_backup.py implementa el respaldo cifrado. requirements.txt declara dependencias; .env.example muestra nombres sin valores. .github/workflows/tests.yml define la CI y michiflin.yml define la sesión de producción. Los scripts de .github/scripts son herramientas operativas; tests/ es la red de seguridad. LICENSE describe los derechos del código."),
              table(["Pruebas y herramientas", "Qué protegen o reconstruyen"], [
                  ["test_logic / test_safety", "Comportamiento central, controles de URL, permisos y rutas de seguridad."],
                  ["test_new_features / test_ai_music", "Herramientas, proveedores, música y cambios funcionales recientes."],
                  ["test_backup_maintenance", "Copias cifradas, restauración y rotación solo tras verificación."],
                  ["test_personal_maintenance", "Bóvedas personales, mantenimiento y límites por usuario."],
                  ["test_operational_maintenance", "Pausa, cola, recuperación, reanudación y advertencias."],
                  ["test_owner_delivery", "Destino verificado, envío cifrado y prevención de reintentos inseguros."],
                  ["test_webxdc / browser", "Puente JavaScript e interfaz real con Playwright en ambos idiomas."],
                  ["test_web_session / proxy_gateway", "Aislamiento del navegador y control de la salida a red."],
                  ["test_social_accounts", "Aislamiento y permisos de las integraciones oficiales opcionales."],
                  ["bridge.js / coder_history.js", "Pruebas de mensajes WebXDC, historial y manejo de proyectos."],
                  ["live_web.js / mp4join.js", "Pruebas de controles Live y unión de segmentos multimedia."],
                  ["tools/build_manual.py", "Generador de esta guía con sus capturas sintéticas y fuentes instaladas."],
                  [".github/scripts/", "Entrega voluntaria al dueño, limpieza de logs y comprobaciones de respaldo."],
              ], [165, BODY_W - 165]),
              note("Regla para tocar una pieza", "Antes de cambiar un módulo, identifica su llamador, los datos que guarda, sus límites de seguridad y las pruebas asociadas. Haz el cambio en una rama o copia, añade una prueba que falle antes de corregir y valida en staging. No edites datos productivos para probar código."),
              PageBreak(), chapter("08 · Guía de implementación por flujo"),
              sub("Recepción y autorización"),
              p("app.py recibe eventos y revisa el cursor; chat.py envuelve las llamadas RPC. access.py y admin.py determinan si el remitente puede operar. state.py conserva ajustes y generaciones que permiten detectar una pausa durante una tarea. Los cambios de acceso deben comprobarse otra vez antes de un efecto externo."),
              sub("Miniapp y prevención de repetición"),
              p("miniapp.py construye el .xdc, registra sus instancias, comprueba el chat y ofrece operaciones con @op. webxdc/core.js maneja peticiones, fragmentos y respuestas. El token de sesión y el libro de identificadores ayudan a rechazar repeticiones inseguras tras un reinicio; no garantizan exactamente una ejecución para todos los comandos o servicios."),
              sub("Bóvedas de proveedores y sesiones"),
              p("personal_ai.py deriva claves con HKDF a partir de WEB_SESSION_KEY, separa nombres por HMAC y cifra la bóveda por persona. Para registrar una clave, un cliente compatible usa un reto y transporte RSA-OAEP/AES-GCM antes de la actualización persistente. web_vault.py separa el estado de navegador por persona y sitio. Estas medidas no impiden que un proveedor legítimo vea lo que se le envía."),
              sub("Descarga directa y archivos"),
              p("files.py inspecciona el origen, protege URLs no públicas y usa Range con validación de Content-Range. Un ETag fuerte se guarda antes de escribir y se envía con If-Range. Si la fuente cambia, se conserva el parcial; si no hay validador fuerte, se avisa. queue.py calcula SHA-256 de la fuente terminada, separa piezas y registra su entrega."),
              sub("Vídeo y audio"),
              p("media.py usa yt-dlp y separa intentos incompatibles; ffmpeg prepara segmentos cuando hace falta. private_media.py y media_worker.py reducen el alcance de las credenciales de sesión. Una descarga local de fixture validó Range; no demuestra que YouTube permita la descarga de un vídeo determinado."),
              sub("Web y Live"),
              p("web.py y reader.py ofrecen una lectura ligera; browser.py y live_web.py coordinan Chromium. sandbox.py y egress.py reducen el acceso a archivos y red del trabajador. El proxy se usa solo si está configurado; un proxy configurado que falla no debe derivar silenciosamente a acceso directo en Live."),
              sub("Programador y agentes"),
              p("coder.py valida rutas, limita archivos y delega comprobaciones en el aislamiento disponible. smart_agent.py usa herramientas de lectura limitadas y trata las páginas como datos. agent.py puede escoger una respuesta sin cuota. Ningún modelo garantiza exactitud: revisa el código antes de ejecutarlo o publicarlo."),
              PageBreak(), chapter("09 · Mantenimiento paso a paso"),
              sub("Cada semana o antes de una actualización"),
              bullet("Consulta estado, espacio libre, cola y mensajes de error; comprueba que los avisos no contengan datos privados."),
              bullet("Revisa proveedores de IA, límites y costes reales. Desactiva o revoca claves que ya no necesites."),
              bullet("Comprueba que exista una copia reciente y que una restauración aislada funcione, sin sobrescribir el estado en uso."),
              bullet("Revisa dependencias y avisos de seguridad; prueba primero en una copia o staging."),
              sub("Para publicar una versión segura"),
              bullet("Activa mantenimiento y deja claro el aviso en ambos idiomas cuando sea necesario."),
              bullet("Revisa el diff, ejecuta pruebas, inspecciona el ZIP y busca credenciales antes de cualquier push."),
              bullet("Publica sin force-push salvo autorización expresa; espera la CI del commit exacto."),
              bullet("Coordina un único relevo. Confirma la restauración de estado y que haya una sola instancia activa."),
              bullet("Comprueba la llegada de la miniapp nueva, conserva la anterior y prueba una operación segura."),
              bullet("Desactiva mantenimiento cuando el servicio y el respaldo estén verificados; comunica las limitaciones."),
              sub("Copias y restauración"),
              p("state_backup.py toma instantáneas consistentes de bases SQLite, excluye algunos archivos voluminosos, cifra el archivo y lo guarda en un repositorio privado. La rotación de copias administradas solo procede tras verificaciones. Se intenta una copia periódica cada 30 minutos y otra al finalizar normalmente; si una falla, la copia disponible puede ser más antigua."),
              p("Para ensayar una restauración, detén el proceso en la copia de prueba, conserva cualquier carpeta existente y restaura en un CONFIG_DIR vacío. No ejecutes una restauración destructiva sobre el único estado. Comprueba identidad, ajustes, bóvedas y cola con datos sintéticos o una copia autorizada."),
              code("# Solo en un entorno aislado, con sus variables seguras\nCONFIG_DIR=/ruta/de/prueba python state_backup.py restore\n# La orden save usa el CONFIG_DIR configurado:\npython state_backup.py save"),
              note("No repitas una purga", "Las herramientas de limpieza o rotación pueden borrar datos. No las ejecutes como mantenimiento general ni elimines copias anteriores sin una copia nueva verificada y una decisión explícita del dueño.", True),
              sub("Si algo falla"),
              table(["Síntoma", "Primero revisa", "Evita"], [
                  ["Miniapp antigua", "Abre la nueva instancia recibida y conserva la anterior.", "Borrar proyectos locales sin respaldo."],
                  ["Trabajo detenido", "Pausa, autorización, espacio, estado del origen y aviso de bytes.", "Repetir un envío incierto a ciegas."],
                  ["Proveedor de IA falla", "Modelo, estado, límites, permisos y coste en la cuenta del proveedor.", "Pegar una clave en un chat o registro."],
                  ["Live no abre", "Cliente compatible, chat cifrado, sandbox y proxy configurado.", "Eludir CAPTCHA o afirmar que WARP funciona sin comprobarlo."],
                  ["No se restaura", "Ubicación, passphrase, clave de sesión y copia previa intacta.", "Sobrescribir el único estado o crear otra identidad por accidente."],
              ], [106, 212, BODY_W - 318]),
              PageBreak(), chapter("10 · Hospedaje y migración futura"),
              p("Hospedaje significa la máquina y los servicios que mantienen el bot ejecutándose: CPU, memoria, disco, red, sistema operativo y proceso. GitHub Actions arranca máquinas temporales para jobs; no es un disco persistente entre ellos. Usarlo de forma continua puede entrar en conflicto con límites o políticas de la plataforma."),
              sub("Opciones y decisión"),
              table(["Opción", "Ventaja", "Límite real"], [
                  ["Servidor con disco persistente", "JOB_DIR puede sobrevivir a reinicios del proceso.", "Exige una máquina propia o contratada, mantenimiento y copias; no elimina fallos del disco."],
                  ["MEGA gratis", "Anuncia 20 GB de almacenamiento.", "Necesita cuenta; límites de descarga variables y riesgo de borrado por inactividad. No garantiza una restauración grande a tiempo."],
                  ["Google Drive gratis", "Hasta 15 GB por cuenta.", "Compartidos con correo y fotos; 12 GiB de cola ocupan casi todo. Una cuenta llena puede afectar Gmail."],
                  ["Oracle Always Free", "Documenta 20 GB de objetos en cuentas gratuitas elegibles.", "Registro normalmente requiere tarjeta; hay límites y riesgo al terminar el periodo de prueba si se supera el cupo."],
              ], [120, 172, BODY_W - 292]),
              p("Estos datos eran los publicados al preparar el manual, el 29 de octubre de 2026; planes, disponibilidad territorial, términos y tarifas pueden cambiar. Ningún servicio se ha contratado ni configurado solo por estar en esta tabla. Un almacén externo además necesita integración, cifrado, comprobación y una prueba real de recuperación antes de poder prometer persistencia."),
              note("Capacidad no equivale a continuidad", "La cola máxima de 12 GiB ocupa aproximadamente 12,88 GB decimales, antes de otros archivos. Dos copias completas necesitarían unos 25,77 GB, más que los 20 GB anunciados por MEGA. Mantener versiones seguras, transferir cambios y restaurar después de una caída requiere un diseño y una prueba adicionales.", True),
              sub("Plan de traslado sin perder la identidad"),
              bullet("1. Prepara el destino: Linux, dependencias, espacio para 12 GiB de cola más sistema y copias, red, seguridad y costes revisados."),
              bullet("2. Activa mantenimiento, observa la cola y espera a que las operaciones en curso se detengan o se reconcilien. Recuerda que los envíos ya iniciados pueden terminar."),
              bullet("3. Crea y verifica una copia cifrada reciente del estado. Conserva por separado las claves necesarias y copia JOB_DIR mediante un canal seguro si quieres retener parciales; el respaldo actual no lo hace."),
              bullet("4. Evita dos instancias simultáneas de la misma cuenta. Deshabilita nuevos relevos del host anterior y espera su cierre ordenado antes de abrir la cuenta en el nuevo host."),
              bullet("5. Restaura en una carpeta vacía y configura el mismo OWNER_ADDRESS, WEB_SESSION_KEY y BOT_EPOCH. No inicialices una identidad nueva si deseas conservar la anterior."),
              bullet("6. Monta el JOB_DIR copiado y comprueba tamaños y huellas; no marques un archivo como completo si falta o no coincide. Si un parcial no existe, informa que habrá que descargarlo otra vez."),
              bullet("7. Inicia una sola instancia, verifica dueño, cifrado, respaldo, pausa y cola; prueba una operación inocua. Mantén intacto el host anterior hasta verificar el nuevo."),
              bullet("8. Si algo falla, detén el destino y vuelve al origen solo después de asegurar que no quede otra instancia activa. No borres claves ni la copia anterior hasta haber validado la recuperación."),
              sub("Ejemplo orientativo de servicio Linux"),
              p("Este es un esquema para que un administrador lo adapte; no crea usuarios, permisos ni secretos automáticamente. El archivo de entorno debe estar protegido y fuera de Git. El navegador y el sandbox requieren herramientas y permisos adicionales revisados por separado."),
              code("[Unit]\nDescription=Michiflin AI\nAfter=network-online.target\n\n[Service]\nUser=michiflin\nWorkingDirectory=/opt/michiflin\nEnvironmentFile=/etc/michiflin/michiflin.env\nExecStart=/opt/michiflin/.venv/bin/python /opt/michiflin/bot.py \\\n  --config-dir /var/lib/michiflin/state serve\nRestart=on-failure\n\n[Install]\nWantedBy=multi-user.target"),
              p("Una ruta persistente posible para la cola es /var/lib/michiflin/jobs. Ajusta JOB_DIR en el entorno protegido, comprueba propietario y espacio, y cifra el disco según el modelo de amenazas. Un disco persistente no sustituye un backup independiente."),
              PageBreak(), chapter("11 · Pruebas, límites y decisiones técnicas"),
              sub("Qué se comprobó en la validación privada"),
              bullet("Mantenimiento con dueño y dos invitados sintéticos, por Delta Chat y WebXDC reales, en español e inglés."),
              bullet("Nueva miniapp cifrada enviada proactivamente; la anterior permaneció y rechazó operaciones ordinarias."),
              bullet("Interfaz real de Desktop para activar y desactivar mantenimiento; el aviso de pérdida de progreso se mostró en una prueba visual simulada."),
              bullet("Descarga local interrumpida y reanudada en el mismo disco con Range, If-Range y SHA-256; protección frente a un origen cambiado."),
              bullet("Entrega de dos adjuntos distintos en un chat verificado sintético, con cifrado extremo a extremo y hashes coincidentes."),
              bullet("Regresión local amplia y prueba real de Deno dentro del sandbox. La CI remota y las pruebas de proveedores externos dependen del commit y entorno publicados."),
              sub("Lo que esas pruebas no demuestran"),
              bullet("Migración automática del almacenamiento local entre instancias de miniapp."),
              bullet("Persistencia de todos los parciales al perder un corredor de GitHub."),
              bullet("Entrega o facturación exactamente una vez frente a todos los fallos posibles."),
              bullet("Que WARP esté activo en producción, o que una descarga específica de YouTube funcione."),
              bullet("Que cada proveedor de IA sea gratuito, acepte login o mantenga sus modelos disponibles."),
              sub("Cómo interpretar SHA-256"),
              p("Una huella identifica bytes: si la fuente confiable y el archivo recibido tienen la misma huella, es muy improbable una corrupción accidental. Una huella calculada solo después de descargar no prueba por sí misma la autoría, la licencia ni que el servidor remoto fuera honesto."),
              PageBreak(), chapter("12 · Glosario y fuentes"),
              table(["Término", "Significado práctico"], [
                  ["WebXDC", "Miniapp empaquetada dentro de un mensaje de Delta Chat; cada instancia tiene almacenamiento propio."],
                  ["Runner / corredor", "Máquina que ejecuta un job de GitHub Actions; la alojada por GitHub es temporal."],
                  ["Estado", "Datos que permiten seguir con la misma cuenta, ajustes y trabajos; no es lo mismo que el código."],
                  ["Sandbox", "Aislamiento que limita a un proceso; su eficacia depende del sistema y de su configuración."],
                  ["Range / If-Range", "Cabeceras HTTP para pedir una parte de un recurso y condicionar la reanudación a un validador."],
                  ["ETag fuerte", "Identificador de versión anunciado por el servidor; útil si ese servidor lo aplica correctamente."],
                  ["SHA-256", "Huella de un archivo o conjunto de bytes usada para detectar diferencias."],
                  ["Secreto", "Valor sensible, como clave de API o frase de cifrado, que se guarda fuera del código."],
                  ["CI", "Pruebas automáticas de integración continua; su resultado corresponde a un commit concreto."],
                  ["Relevo", "Parada de una sesión e inicio de otra; debe coordinar estado, salida y una sola instancia."],
              ], [130, BODY_W - 130]),
              p("Documentación del proyecto: README.md, README.es.md y docs/. Las condiciones y capacidades de terceros cambian; consulta siempre las fuentes oficiales antes de contratar, migrar o distribuir contenido."),
              bullet("Delta Chat y WebXDC: https://delta.chat/en/help y https://webxdc.org/docs/"),
              bullet("GitHub runners: https://docs.github.com/en/actions/how-tos/manage-runners/github-hosted-runners/use-github-hosted-runners"),
              bullet("MEGA: https://mega.io/storage y https://help.mega.io/plans-storage/space-storage/transfer-quota"),
              bullet("MEGA inactividad: https://help.mega.io/accounts/create-account/account-expiry · Limitaciones del cliente: https://rclone.org/mega/"),
              bullet("Google: https://support.google.com/googleone/answer/9312312 y https://support.google.com/drive/answer/6374270"),
              bullet("Oracle Free: https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm"),
              bullet("Cloudflare R2: https://developers.cloudflare.com/r2/pricing/ · Deezer: https://support.deezer.com/hc/en-gb/articles/360011538897-Deezer-FAQs-For-Developers"),
              note("Tu principio de seguridad", "Conserva código y datos por separado, usa una sola instancia productiva, verifica cada copia y cada destinatario, y no compartas contraseñas, claves, cookies ni frases semilla por chat."),
              Spacer(1, 8), p("FIN DE LA GUÍA · Consulta el ZIP para los archivos completos y el informe de la versión para los resultados y pendientes concretos.", "SmallM")]
    return story


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    Manual(OUTPUT).multiBuild(build_story())
    print("Manual creado:", OUTPUT)


if __name__ == "__main__":
    main()
