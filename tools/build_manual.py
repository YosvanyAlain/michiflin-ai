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
            canvas.drawString(59, 237, "30 de octubre de 2026 · edición 5.5")
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


def screenshot_51() -> Table:
    paths = (ROOT / "docs/manual-media/agent-5-1-simulado.png", ROOT / "docs/manual-media/rates-5-3-simulado.png")
    images = [Image(str(paths[0]), width=4.2 * cm, height=9.45 * cm),
              Image(str(paths[1]), width=4.2 * cm, height=9.45 * cm)]
    result = Table([images, [p("Agente · prueba de móvil simulada", "CaptionM"),
                              p("Tasas compactas · datos simulados", "CaptionM")]],
                   colWidths=[BODY_W / 2, BODY_W / 2], hAlign="CENTER")
    result.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                ("ALIGN", (0, 0), (-1, -1), "CENTER")]))
    result.spaceAfter = 12
    return result


def screenshot_kit_55() -> Table:
    paths = (ROOT / "docs/manual-media/kit-5-5-simulado.png", ROOT / "docs/manual-media/rar-selectivo-5-5-simulado.png")
    images = [Image(str(path), width=4.2 * cm, height=9.09 * cm) for path in paths]
    result = Table([images, [p("Kit integrado · archivos de ejemplo", "CaptionM"),
                              p("RAR selectivo · prueba simulada", "CaptionM")]],
                   colWidths=[BODY_W / 2, BODY_W / 2], hAlign="CENTER")
    result.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                ("ALIGN", (0, 0), (-1, -1), "CENTER")]))
    result.spaceAfter = 12
    return result


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
    "community.py": "Admisión por SecureJoin, vínculo de clave y chat, revocación y respaldo de permisos verificados.",
    "config.py": "Lee variables de entorno, define valores iniciales y aplica límites y controles de acceso.",
    "documents.py": "Extrae texto de documentos y lo prepara para una consulta, sin tratar el archivo como instrucciones fiables.",
    "egress.py": "Pasarela HTTPS del sandbox: valida destinos públicos y controla el uso de un proxy configurado.",
    "extras.py": "Utilidades sin modelo, entre ellas conversión, idioma, hora y subtítulos cuando existen.",
    "facebook_account.py": "Integración oficial opcional y de lectura, anterior a la retirada de tarjetas de plataformas.",
    "filekit.py": "Espacio de archivos temporal por identidad, copias inmutables, árbol y operaciones con historial.",
    "filekit_api.py": "Operaciones autorizadas del kit y límites del puente con la miniapp.",
    "filekit_archive.py": "Archivador temporal sin red y con permisos reducidos; falla cerrado si no puede aislarse.",
    "filekit_crypto.py": "Sobres cifrados con claves efímeras para contraseñas y contenido remoto.",
    "filekit_join_remote.py": "Unión por bloques de partes numeradas remotas, con huella opcional e historial.",
    "filekit_rar_native.py": "Aislamiento sin red, límites y publicación atómica para la extracción selectiva de RAR.",
    "filekit_rar_worker.py": "Lectura nativa de RAR por bloques dentro del entorno restringido.",
    "filekit_transfer.py": "Importación explícita por adjuntos y copia de trabajos verificados sin borrar los originales.",
    "files.py": "Examina enlaces directos, detecta tamaño y formato, descarga con Range y protege reanudaciones.",
    "identity_guard.py": "Verifica chat, clave y permisos; recuperación por correo solo si se activa, con riesgo de suplantación y registro de claves retiradas.",
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
    "rate_store.py": "Valida y guarda las últimas tasas de elTOQUE y sus ventanas históricas sin incluir credenciales.",
    "rates_monitor.py": "Consulta compartida y cooperativa de elTOQUE, salud y alertas privadas al dueño verificado.",
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
              note("Dos entregas distintas", "El ZIP de fuente contiene archivos rastreados, pruebas, documentación y un manifiesto SHA-256. No contiene cuentas, secretos, invitaciones ni estado privado. Este PDF explica el uso y la operación; ninguna copia de código sustituye al respaldo cifrado de la cuenta."),
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
              p("El agente unificado ofrece dos modos: sin cuota de modelos y con IA/API. El primero usa herramientas y fuentes sin activar imágenes por sí solo; el segundo puede elegir un modelo o usar selección automática y realizar hasta cuatro pasos. Los adjuntos compatibles van directamente al modelo elegido con el contexto reciente. Verifica el resultado y el coste real del proveedor."),
              p("La conversación automática anterior conserva una entrada separada de historial. Los catálogos de texto e imagen se abren por proveedor; los modelos de texto también se agrupan por categoría. La disponibilidad real puede cambiar."),
              sub("Identidad nueva y verificación"),
              p("Para una identidad realmente nueva hay que crear otra cuenta y otras claves; cambiar solo el relay o una época no basta. El modo estricto compara la huella pública principal del dueño obtenida por un canal independiente, exige chat cifrado y rechaza mensajes no autorizados. La recuperación por correo es una excepción opcional: acepta cambios de clave de direcciones autorizadas y, por ello, una suplantación de correo puede obtener sus permisos. El corte solo acepta una cuenta ya verificada, conserva primero una copia cifrada y comprobada de los datos presentes, separa los vínculos antiguos y sella la nueva identidad contra retrocesos silenciosos. Los trabajos viejos quedan guardados, pero no se reanudan con identificadores antiguos. La miniapp local no migra automáticamente. La exportación independiente del historial conserva mensajes y adjuntos disponibles, pero no recrea la cuenta ni sus claves; consulta docs/RECOVER_OWNER_HISTORY.md. Los acuses de lectura reales dependen de los clientes y la red, y no prueban que una tarea haya terminado. Nunca guardes una clave privada ni una frase semilla en el ajuste de huella."),
              sub("Traducción, clima y elTOQUE"),
              p("El traductor no usa cuota de modelos de IA, acepta hasta 4500 caracteres y envía el texto a Google, al LibreTranslate configurado o a Apertium beta. No envíes secretos: los proveedores pueden guardar datos y aplicar límites. Apertium ofrece traducciones orientativas, puede marcar palabras desconocidas con * y no admite todos los pares; si la detección es dudosa, elige el idioma de origen. Clima usa Open-Meteo. Las tasas usan la API autorizada de elTOQUE: una consulta compartida actualiza aproximadamente cada cinco minutos mientras el bot funciona. El banner muestra la antigüedad real; si falla, se muestran únicamente datos guardados con aviso. El historial usa ventanas reales, indica huecos y permite copiar el enlace oficial. Los códigos originales se conservan; ECU se presenta EUR por comparación con el sitio, sin alias documentado en la API. BTC y USDT (TRC20) se expresan por dólar equivalente; TRX por unidad. Las conversiones y el escenario están activados por el operador y son cálculos propios, no cotizaciones ni pronósticos de elTOQUE. La autorización para derivados no está confirmada; revisa las condiciones del proveedor o desactívalos."),
              screenshot_51(),
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
              p("Los archivos generales pueden llegar como partes numeradas con SHA-256. Los medios largos pueden convertirse en segmentos reproducibles. Conserva todas las partes en Archivos, no en una galería que renombre o recodifique, y usa Unir partes para reconstruirlas. La verificación requiere huellas disponibles y soporte del cliente."),
              sub("Kit de archivos"),
              p("En Descargas abre Kit de archivos. En este dispositivo guarda copias dentro de la instancia de la miniapp; Servidor temporal usa el espacio temporal del usuario autorizado. El árbol permite desplazarse para ver nombres completos, seleccionar por carpeta y renombrar varios elementos con vista previa, además de copiar, mover, eliminar y deshacer o rehacer. Los originales del teléfono no se modifican. Vaciar historial elimina las versiones anteriores recuperables."),
              p("Se pueden editar textos UTF-8 y previsualizar imágenes o medios compatibles; otros binarios se exportan para abrirlos fuera. El texto local admite hasta 4 MiB; la lectura remota hasta 200.000 bytes y la edición remota hasta 70.000 bytes. La edición detecta cambios hechos desde otra ventana."),
              p("El kit crea ZIP o 7z y abre formatos compatibles. En el servidor, selecciona todas las partes RAR genuinas y la primera, visualiza la lista cifrada y elige qué archivos extraer. El lector nativo UnRAR 7.30 beta 2 procesa por bloques dentro de un aislamiento sin red; no garantiza todas las variantes. Los sólidos pueden requerir procesar datos anteriores. El espacio libre debe cubrir aproximadamente dos veces la salida seleccionada, además de la entrada y la reserva. Por defecto rigen 4 GiB por archivo, 12 GiB por selección y límites de RAM y tiempo."),
              p("El unidor está integrado en el gestor. En el teléfono conserva la unión de partes crudas y de segmentos multimedia compatibles; en el servidor une partes .001, .002 y siguientes por bloques, con SHA-256 opcional y sin borrar las partes. Si una lista no indica el total y no hay huella, no se puede detectar una última parte ausente. Une primero las partes archivo.rar.001 antes de abrir la copia archivo.rar. Un proceso interrumpido no publica resultados parciales y debe volver a iniciarse."),
              p("Para importar al servidor, la app prepara un borrador que debes enviar tú al chat del bot sin cambiar su marca; espera la confirmación. Cada adjunto debe respetar el límite por parte y la copia se verifica con SHA-256. Las descargas listas o terminadas se copian solo si sus huellas permiten comprobar las partes. El envío desde el servidor usa la cola y sus límites de buzón."),
              note("Copias temporales", "El cliente puede recuperar el almacenamiento local de esta instancia y el servidor puede perder su espacio temporal al cambiar de corredor; no forman parte del respaldo del estado. Exporta y comprueba las copias importantes. Contraseñas, huellas, listas y selecciones internas de RAR y contenido remoto usan sobres cifrados efímeros. Los nombres del árbol y metadatos operativos permanecen en las actualizaciones del chat cifrado.", True),
              sub("Controles del dueño"),
              p("Mantenimiento y aviso está desactivado por defecto. El dueño puede escribir mensajes en español e inglés, guardar una duración de hasta un año o dejar la finalización manual, y pausar las tareas de forma global. Las personas autorizadas actuales y las nuevas reciben el aviso. Al finalizar, los trabajos elegibles pueden continuar sin quitar pausas manuales ni alterar permisos. Abrir Ajustes no cambia el kill switch ni Nauta Estricto."),
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
              p("El modo seguro por defecto mantiene OWNER_ONLY=1. Para compartir mediante invitación, el operador activa INVITE_AUTO_ACCESS=1 y OWNER_ONLY=0. Quien complete SecureJoin y la verificación cifrada entra automáticamente como usuario, sin aprobación individual; el dueño y el administrador de respaldo conservan los controles sensibles. Las listas antiguas no se reactivan."),
              p("La invitación es una credencial: quien la recibe puede compartirla. Entrégala solo en privado. El dueño puede cerrar o reabrir el acceso, revocar o bloquear invitados. Un cierre se respalda; si falla el respaldo, el bloqueo local podría no sobrevivir a un reinicio. Un invitado con clave nueva debe repetir SecureJoin y sus proyectos de la miniapp anterior no se trasladan solos."),
              note("Recuperación por correo", "Si se habilita expresamente, una dirección permitida puede cambiar de clave sin demostrar la anterior. Esto facilita la recuperación tras cambiar de teléfono, pero una dirección suplantada podría obtener sus permisos aunque la conexión esté cifrada. En modo invitación se limita a administradores; los invitados deben verificar la nueva clave. Los trabajos del chat antiguo y los datos locales previos no se trasladan automáticamente.", True),
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
              p("El ZIP de código se genera a partir de archivos rastreados de un commit e incluye SHA256SUMS.txt. No incluye .git, cuentas, estados, claves ni historiales. Si se verifica desde la carpeta Michiflin-AI, el manifiesto comprueba los archivos de esa carpeta."),
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
                  ["OWNER_ONLY", "Arranque privado del servicio.", "1 es el valor seguro; 0 permite el modo de invitación expresamente activado."],
                  ["INVITE_AUTO_ACCESS", "Admisión mediante SecureJoin verificado.", "0 por defecto; 1 con OWNER_ONLY=0 permite entrar a quien reciba la invitación."],
                  ["OWNER_BACKUP_ADDRESS", "Administrador de respaldo opcional.", "Tiene permisos administrativos; no publicar su dirección."],
                  ["EMAIL_KEY_RECOVERY", "Permite cambiar la clave de una dirección autorizada.", "0 por defecto; 1 acepta riesgo de suplantación."],
                  ["WEB_SESSION_KEY", "Protege bóvedas personales y sesiones web.", "Estable, larga y respaldada por separado."],
                  ["BOT_EPOCH", "Vincula miniapps y trabajos con el despliegue.", "En modo estricto los trabajos antiguos quedan guardados, sin enviarse."],
                  ["ELTOQUE_API_TOKEN", "Autoriza consultas oficiales de tasas.", "Guardar como secreto; no compartir."],
                  ["ELTOQUE_REFRESH_SECONDS", "Intervalo compartido (120–3600 s).", "Por defecto 300 s; sujeto a límites del proveedor."],
                  ["ELTOQUE_DERIVED_CALCULATIONS", "Controla conversiones y escenarios.", "1 por solicitud del operador; 0 desactiva. Autorización de derivados no confirmada."],
                  ["CONFIG_DIR / JOB_DIR", "Estado de cuenta / archivos temporales.", "No confundir el respaldo del estado con el de los parciales."],
                  ["STATE_BACKUP_*", "Ubicación y frase de cifrado del respaldo.", "Nunca publiques valores ni los coloques en el ZIP."],
              ], [145, 166, BODY_W - 311]),
              p("En GitHub Actions, los nombres actuales de secretos incluyen OWNER_ADDRESS, WEB_SESSION_KEY, BOT_EPOCH, STATE_BACKUP_REPO, STATE_BACKUP_PASSPHRASE, STATE_GITHUB_TOKEN y ELTOQUE_API_TOKEN. El workflow entrega el último al proceso como GITHUB_TOKEN para el respaldo. MICHIFLIN_GITHUB_TOKEN y las claves de proveedores son opcionales según la función. La existencia de un nombre no demuestra que su valor sea válido."),
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
              sub("Evaluar un hospedaje"),
              p("Una propuesta de hospedaje debe explicar el sistema operativo, recursos y límites de red, disponibilidad, coste, responsabilidades, aislamiento y política de copias. Verifica las condiciones vigentes con el proveedor antes de contratar. No se ha contratado ni configurado un nuevo servidor por describirlo aquí."),
              note("Capacidad no equivale a continuidad", "La cola, el estado cifrado y la miniapp son datos diferentes. Antes de migrar, mide lo que realmente existe, preserva los originales y verifica cada copia; un cambio de servidor puede perder los parciales que no se hayan trasladado expresamente.", True),
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
              bullet("Migración de relay con cuentas sintéticas: misma huella OpenPGP, historia previa, transporte antiguo, mensajes cifrados y restauración de copias cifradas; no equivale a confirmar la cuenta de producción."),
              bullet("Regresión local amplia y prueba real de Deno dentro del sandbox. La CI remota y las pruebas de proveedores externos dependen del commit y entorno publicados."),
              sub("Lo que esas pruebas no demuestran"),
              bullet("Migración automática del almacenamiento local entre instancias de miniapp."),
              bullet("Persistencia de todos los parciales al perder un corredor de GitHub."),
              bullet("Entrega o facturación exactamente una vez frente a todos los fallos posibles."),
              bullet("Que WARP esté activo en producción, o que una descarga específica de YouTube funcione."),
              bullet("Que cada proveedor de IA sea gratuito, acepte login o mantenga sus modelos disponibles."),
              sub("Cómo interpretar SHA-256"),
              p("Una huella identifica bytes: si la fuente confiable y el archivo recibido tienen la misma huella, es muy improbable una corrupción accidental. Una huella calculada solo después de descargar no prueba por sí misma la autoría, la licencia ni que el servidor remoto fuera honesto."),
              PageBreak(), chapter("12 · Tutorial práctico y trucos"),
              sub("Primeros diez minutos"),
              bullet("1. Abre la conversación autorizada en Delta Chat. Comprueba que sea el contacto correcto y que el mensaje esté cifrado; no compartas su enlace o código de invitación."),
              bullet("2. Abre la miniapp más reciente. La introducción y los nombres cambian con el idioma; conserva la instancia anterior porque sus datos locales son independientes."),
              bullet("3. En Inicio escribe una pregunta sencilla o pega una página pública. Si quieres una respuesta sin cuota de modelos, selecciona ese modo del Agente; en IA/API revisa proveedor, límites y posible coste."),
              bullet("4. Visita Apps para traducción, clima y tasas. Los datos proceden de servicios externos: consulta la antigüedad y recuerda que las conversiones son cálculos propios, no pronósticos de elTOQUE."),
              bullet("5. En Descargas observa la cola. Ajusta la calidad y tamaño de parte a tu buzón antes de iniciar; si un envío es incierto, comprueba el chat antes de repetir."),
              sub("Organiza y comprueba un archivo"),
              bullet("1. En Descargas, abre Kit de archivos y elige el dispositivo o servidor temporal. Importa una copia de prueba y deja intacto el original del teléfono."),
              bullet("2. Selecciona uno o varios archivos de la carpeta. Para un renombrado por lotes, revisa la vista previa, extensiones y colisiones; confirma solo lo deseado. Deshacer revierte operaciones compatibles de la sesión."),
              bullet("3. Si tienes partes numeradas, selecciona la serie, revisa orden y huecos conocidos, e introduce la huella original si la conoces. Sin total ni huella no se puede garantizar que exista la última parte."),
              bullet("4. Para un RAR grande o cifrado compatible, copia las partes al espacio temporal, abre el archivo en el servidor, suministra la contraseña cuando corresponda, lista el contenido y marca solo las entradas que quieras extraer."),
              bullet("5. Espera el resultado y verifica o exporta la copia. El espacio remoto puede desaparecer al cambiar el corredor y no está en el respaldo habitual. El procesamiento de archivos sólidos puede requerir leer entradas anteriores."),
              note("Contraseñas de archivos", "La contraseña y la selección remota usan sobres cifrados, pero el procesamiento tiene lugar en el servidor aislado. No envíes contraseñas en mensajes, capturas o informes; listar nombres visibles no confirma que la contraseña de los datos sea correcta.", True),
              screenshot_kit_55(),
              sub("Consejos que evitan pérdidas"),
              bullet("No confundas guardar una copia en el kit con guardar el original en Fotos o Archivos. Exporta el resultado y comprueba su tamaño o huella antes de borrar nada."),
              bullet("La unión cruda grande puede hacerse por bloques en el servidor; el remux de medios compatibles permanece local. Un archivo multimedia roto puede requerir herramientas adicionales."),
              bullet("Mantenimiento suspende trabajos de manera cooperativa y avisa a usuarios autorizados; una llamada o entrega ya iniciada puede terminar. El progreso puede conservarse solo si sus archivos siguen disponibles."),
              bullet("La recuperación por correo facilita el acceso tras reinstalar, pero disminuye la garantía de identidad. Si se suplanta una dirección autorizada, el atacante podría ocuparla. Desactívala si ese riesgo no es aceptable."),
              bullet("Actualiza el bot por cambios revisados, no por editar secretos al azar. Si no se reconoce una operación o un proveedor responde con límites, detente y consulta su documentación."),
              PageBreak(), chapter("13 · Recreación paso a paso"),
              p("Este orden describe cómo reconstruir y comprender el sistema a partir del código disponible; no pretende certificar la cronología histórica de cada cambio."),
              bullet("1. Define requisitos y fronteras: chat privado, permisos, cuotas por buzón, sistema operativo de destino y capacidades que dependen de terceros."),
              bullet("2. Prepara una cuenta desechable y Python. Conecta los eventos de deltachat2/deltabot-cli en bot.py y app.py; conserva la identidad productiva fuera del ensayo."),
              bullet("3. Implementa configuración, aislamiento de remitentes y estado en config.py, access.py, admin.py, identity_guard.py y state.py. Comprueba permisos tanto al recibir como antes de enviar o ejecutar efectos."),
              bullet("4. Crea la interfaz HTML/CSS/JavaScript en webxdc/, empaquétala desde miniapp.py y vincula cada instancia al chat y a la sesión. Añade textos paralelos en español e inglés."),
              bullet("5. Registra operaciones limitadas del puente y mensajes de respuesta; usa identificadores y puntos de control para evitar repeticiones inseguras. Las garantías dependen del transporte."),
              bullet("6. Añade descarga, validación, división, huellas y cola con files.py y queue.py. Prueba pausa, cancelación, cambios en el origen y envío incierto sin sobrescribir originales."),
              bullet("7. Integra el kit: archivo local de la instancia, árbol, operaciones reversibles, unión, ZIP/7z y trabajador RAR selectivo aislado. Comprueba rutas, enlaces, límites y permisos; conserva las licencias incluidas."),
              bullet("8. Conecta búsqueda, lector, traducción, tasas y modelos remotos. Trata los resultados externos como datos no confiables; verifica licencias, términos, tarifas y confidencialidad."),
              bullet("9. Añade respaldos cifrados, procedimientos de recuperación y pruebas de restauración aisladas. El respaldo de estado no incluye todos los adjuntos ni los parciales de la cola."),
              bullet("10. Ejecuta las pruebas locales y en CI, revisa seguridad y capturas, inspecciona el diff y solo después coordina una publicación y un relevo con una sola instancia."),
              note("Fuentes del proyecto", "Usa la copia exacta de código y sus pruebas como referencia. El manual describe el diseño, no autoriza redistribución ni garantiza que dependencias o proveedores conserven sus comportamientos.", True),
              PageBreak(), chapter("14 · Glosario y fuentes"),
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
              bullet("elTOQUE: https://eltoque.com/api-tasas-de-eltoque-terminos-y-condiciones-de-uso"),
              bullet("UnRAR: https://www.rarlab.com/rar_add.htm · 7-Zip: https://www.7-zip.org/"),
              note("Tu principio de seguridad", "Conserva código y datos por separado, usa una sola instancia productiva, verifica cada copia y cada destinatario, y no compartas contraseñas, claves, cookies ni frases semilla por chat."),
              Spacer(1, 8), p("FIN DE LA GUÍA · Consulta el ZIP para los archivos completos y el informe de la versión para los resultados y pendientes concretos.", "SmallM")]
    return story


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    Manual(OUTPUT).multiBuild(build_story())
    print("Manual creado:", OUTPUT)


if __name__ == "__main__":
    main()
