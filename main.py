import os
import json
import asyncio
import smtplib
import requests
import io
import pdfplumber
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from typing import Optional, List
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr
from dotenv import load_dotenv
from google import genai as google_genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
from fpdf import FPDF

load_dotenv()

app = FastAPI(title="Fedor Sawoloka - API Backend v3.0")

# CORS: permitir peticiones desde yosoyelruso.com y localhost
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://yosoyelruso.com",
        "http://yosoyelruso.com",
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1:5500",
        "*"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Configuración ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MAILCHIMP_API_KEY = os.getenv("MAILCHIMP_API_KEY")
MAILCHIMP_LIST_ID = os.getenv("MAILCHIMP_LIST_ID")
MAILCHIMP_SERVER_PREFIX = os.getenv("MAILCHIMP_SERVER_PREFIX", "us7")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
GMAIL_USER = os.getenv("GMAIL_USER", "fedor.sawoloka@gmail.com")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

# ID de la hoja de cálculo para la lista de acceso del Programa Anti-Inercia
# Usa la misma hoja principal; la lista de acceso está en la pestaña "Acceso_Programa"
PROGRAMA_SHEET_ID = os.getenv("PROGRAMA_SHEET_ID", GOOGLE_SHEET_ID)

# ============================================================
# MODELOS DE DATOS — Configura tu IA (existente)
# ============================================================

class FormData(BaseModel):
    email: str
    mailchimp_consent: bool = False
    nombre_cargo: str
    filosofia_trabajo: str
    responsabilidades: str
    diferenciador: str
    audiencia: str
    proyecto_actual: str
    cuello_botella: str
    uso_ia: List[str]
    nivel_ayuda: List[str]
    nivel_autonomia: List[str]
    tipo_resultado: List[str]
    importancia_accion: List[str]
    estilo_comunicacion: List[str]
    palabras_evitar: str
    formato_preferido: List[str]
    enlaces_referencia: Optional[str] = ""

class GenerateResponse(BaseModel):
    success: bool
    document: Optional[str] = None
    error: Optional[str] = None
    fallback: bool = False
    email_sent: bool = False

class PdfRequest(BaseModel):
    document: str
    nombre: Optional[str] = "Profesional"

# ============================================================
# MODELOS DE DATOS — Programa Anti-Inercia (nuevo)
# ============================================================

class AccessCheckRequest(BaseModel):
    email: str

class AccessCheckResponse(BaseModel):
    allowed: bool
    message: str

# ============================================================
# FUNCIONES AUXILIARES — Configura tu IA (existente, sin cambios)
# ============================================================

def classify_profile(data: FormData) -> dict:
    nombre_lower = data.nombre_cargo.lower()
    profile_type = "Otro"
    if any(w in nombre_lower for w in ["ceo", "director", "presidente", "vp", "chief"]):
        profile_type = "Ejecutivo"
    elif any(w in nombre_lower for w in ["gerente", "manager", "jefe", "head"]):
        profile_type = "Gerente"
    elif any(w in nombre_lower for w in ["dueño", "propietario", "fundador", "owner"]):
        profile_type = "Dueño de negocio"
    elif any(w in nombre_lower for w in ["emprendedor", "entrepreneur", "startup"]):
        profile_type = "Emprendedor"
    elif any(w in nombre_lower for w in ["consultor", "consultant", "asesor", "advisor"]):
        profile_type = "Consultor"
    elif any(w in nombre_lower for w in ["marketing", "marketer", "growth", "publicidad"]):
        profile_type = "Marketer"
    elif any(w in nombre_lower for w in ["creador", "creator", "content", "influencer"]):
        profile_type = "Creador"
    elif any(w in nombre_lower for w in ["freelance", "independiente", "autónomo"]):
        profile_type = "Freelancer"

    uso_str = " ".join(data.uso_ia).lower()
    cuello_lower = data.cuello_botella.lower()
    combined = uso_str + " " + cuello_lower
    need = "Claridad"
    if any(w in combined for w in ["productividad", "tiempo", "eficiencia", "automatizar"]):
        need = "Productividad"
    elif any(w in combined for w in ["contenido", "escribir", "publicar", "redes", "post"]):
        need = "Contenido"
    elif any(w in combined for w in ["organizar", "organización", "orden", "caos"]):
        need = "Organización"
    elif any(w in combined for w in ["estrategia", "plan", "dirección", "rumbo"]):
        need = "Estrategia"
    elif any(w in combined for w in ["vender", "ventas", "clientes", "conversión"]):
        need = "Ventas"
    elif any(w in combined for w in ["delegar", "equipo", "team", "colaborar"]):
        need = "Delegación"
    elif any(w in combined for w in ["sistema", "proceso", "flujo", "workflow"]):
        need = "Sistemas"
    elif any(w in combined for w in ["posicionamiento", "marca", "branding", "reputación"]):
        need = "Posicionamiento"
    elif any(w in combined for w in ["decisión", "decidir", "priorizar", "elegir"]):
        need = "Toma de decisiones"

    autonomia_str = " ".join(data.nivel_autonomia).lower()
    resultado_str = " ".join(data.tipo_resultado).lower()
    maturity = "Explorador"
    if "copiloto" in autonomia_str or "sistemas completos" in resultado_str:
        maturity = "Listo para ejecutar"
    elif "planes" in resultado_str or "estructura" in autonomia_str:
        maturity = "En transición"

    score = 0
    decision_roles = ["ceo", "director", "gerente", "dueño", "fundador", "propietario", "vp", "chief"]
    if any(w in nombre_lower for w in decision_roles):
        score += 2
    if len(data.proyecto_actual) > 30:
        score += 2
    if len(data.cuello_botella) > 50:
        score += 2
    if data.enlaces_referencia and ("http" in data.enlaces_referencia or "www" in data.enlaces_referencia):
        score += 1
    if len(data.filosofia_trabajo) > 50 and len(data.diferenciador) > 50:
        score += 1
    if "copiloto" in autonomia_str or "crítico" in " ".join(data.importancia_accion).lower():
        score += 2

    if score <= 3:
        commercial_potential = "Frío"
    elif score <= 6:
        commercial_potential = "Interesante"
    elif score <= 8:
        commercial_potential = "Calificado"
    else:
        commercial_potential = "Premium"

    return {
        "profile_type": profile_type,
        "need": need,
        "maturity": maturity,
        "commercial_potential": commercial_potential,
        "score": score
    }


def generate_document_gemini(data: FormData) -> str:
    client = google_genai.Client(api_key=GEMINI_API_KEY)
    uso_str = ", ".join(data.uso_ia) if data.uso_ia else "No especificado"
    nivel_ayuda_str = ", ".join(data.nivel_ayuda) if data.nivel_ayuda else "No especificado"
    nivel_autonomia_str = ", ".join(data.nivel_autonomia) if data.nivel_autonomia else "No especificado"
    tipo_resultado_str = ", ".join(data.tipo_resultado) if data.tipo_resultado else "No especificado"
    importancia_accion_str = ", ".join(data.importancia_accion) if data.importancia_accion else "No especificado"
    estilo_str = ", ".join(data.estilo_comunicacion) if data.estilo_comunicacion else "No especificado"
    formato_str = ", ".join(data.formato_preferido) if data.formato_preferido else "No especificado"

    prompt = f"""Eres un experto en inteligencia artificial y productividad profesional. Con base en las siguientes respuestas de un profesional, genera un Documento Maestro de Contexto claro, estructurado y en primera persona, listo para ser pegado en cualquier chat de IA (ChatGPT, Gemini, Claude, etc.).

INSTRUCCIONES CRÍTICAS:
- El documento debe estar en PRIMERA PERSONA (yo soy, yo trabajo, yo quiero...)
- NO debe ser narrativo ni inspiracional — debe ser directamente operativo
- La IA que lo lea debe entender exactamente: quién es el usuario, cómo trabaja, qué quiere lograr con la IA, y cómo debe comportarse con él
- Incluye una sección de "Instrucciones implícitas para la IA" al final, donde traduces toda la información anterior en directrices concretas de comportamiento
- El documento debe cubrir TODAS las secciones indicadas en la estructura
- Al final, en línea aparte, incluye exactamente: ---\\nDocumento generado con base en la metodología Gold Standard de Fedor Sawoloka.

ESTRUCTURA OBLIGATORIA DEL DOCUMENTO:
1. Identidad profesional
2. Contexto de trabajo
3. Objetivo de uso de IA
4. Nivel de autonomía requerido
5. Estilo de comunicación
6. Formato de respuesta preferido
7. Nivel de ejecución esperado
8. Instrucciones implícitas para la IA

RESPUESTAS DEL USUARIO:

SECCIÓN 1 - IDENTIDAD PROFESIONAL:
- Nombre y cargo: {data.nombre_cargo}
- Filosofía de trabajo: {data.filosofia_trabajo}
- Responsabilidades principales: {data.responsabilidades}
- Diferenciador profesional: {data.diferenciador}

SECCIÓN 2 - CONTEXTO DE TRABAJO:
- Audiencia / cliente / equipo: {data.audiencia}
- Proyecto o área de enfoque actual: {data.proyecto_actual}
- Mayor problema o cuello de botella: {data.cuello_botella}

SECCIÓN 3 - COMPORTAMIENTO DE LA IA:
- Para qué quiere usar la IA: {uso_str}
- Nivel de ayuda esperado: {nivel_ayuda_str}
- Nivel de autonomía requerido: {nivel_autonomia_str}
- Tipo de resultado esperado: {tipo_resultado_str}
- Importancia de la acción (no solo pensar): {importancia_accion_str}

SECCIÓN 4 - ESTILO DE COMUNICACIÓN:
- Estilo de comunicación preferido: {estilo_str}
- Palabras o estilos a evitar: {data.palabras_evitar}
- Formato de información preferido: {formato_str}

SECCIÓN 5 - CONTEXTO ADICIONAL:
- Referencias / enlaces: {data.enlaces_referencia or 'No especificado'}
"""
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text


def generate_document_fallback(data: FormData) -> str:
    uso_str = ", ".join(data.uso_ia) if data.uso_ia else "No especificado"
    nivel_ayuda_str = ", ".join(data.nivel_ayuda) if data.nivel_ayuda else "No especificado"
    nivel_autonomia_str = ", ".join(data.nivel_autonomia) if data.nivel_autonomia else "No especificado"
    tipo_resultado_str = ", ".join(data.tipo_resultado) if data.tipo_resultado else "No especificado"
    importancia_accion_str = ", ".join(data.importancia_accion) if data.importancia_accion else "No especificado"
    estilo_str = ", ".join(data.estilo_comunicacion) if data.estilo_comunicacion else "No especificado"
    formato_str = ", ".join(data.formato_preferido) if data.formato_preferido else "No especificado"

    doc = f"""# Documento Maestro de Contexto

## 1. Identidad Profesional
{data.nombre_cargo}

Mi filosofía de trabajo: {data.filosofia_trabajo}

Mis responsabilidades principales:
{data.responsabilidades}

Lo que me diferencia: {data.diferenciador}

## 2. Contexto de Trabajo
Trabajo principalmente con: {data.audiencia}

Proyecto o área de enfoque actual: {data.proyecto_actual}

Mi principal cuello de botella: {data.cuello_botella}

## 3. Objetivo de Uso de IA
Quiero usar la IA principalmente para: {uso_str}

## 4. Nivel de Autonomía Requerido
Nivel de ayuda que espero: {nivel_ayuda_str}
Nivel de autonomía que quiero: {nivel_autonomia_str}

## 5. Estilo de Comunicación
Estilo preferido: {estilo_str}
Palabras o estilos a evitar: {data.palabras_evitar}

## 6. Formato de Respuesta Preferido
{formato_str}

## 7. Nivel de Ejecución Esperado
Tipo de resultado que espero: {tipo_resultado_str}
Importancia de la acción (no solo pensar): {importancia_accion_str}

## 8. Instrucciones Implícitas para la IA
- Respóndeme siempre en primera persona y de forma directa
- Adapta tu tono a: {estilo_str}
- Evita: {data.palabras_evitar}
- Entrega resultados en formato: {formato_str}
- Mi nivel de autonomía esperado es: {nivel_autonomia_str}
- Prioriza la acción sobre la teoría: {importancia_accion_str}

## Referencias
{data.enlaces_referencia or 'No especificado'}

---
Documento generado con base en la metodología Gold Standard de Fedor Sawoloka."""
    return doc


def save_to_google_sheets(data: FormData, tags: dict):
    try:
        if GOOGLE_CREDENTIALS_JSON:
            import json as json_module
            creds_dict = json_module.loads(GOOGLE_CREDENTIALS_JSON)
            creds = service_account.Credentials.from_service_account_info(
                creds_dict,
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
        else:
            credentials_path = GOOGLE_CREDENTIALS_FILE
            if not os.path.isabs(credentials_path):
                credentials_path = os.path.join(os.path.dirname(__file__), credentials_path)
            creds = service_account.Credentials.from_service_account_file(
                credentials_path,
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )

        service = build("sheets", "v4", credentials=creds)
        sheet = service.spreadsheets()

        uso_str = ", ".join(data.uso_ia) if data.uso_ia else ""
        nivel_ayuda_str = ", ".join(data.nivel_ayuda) if data.nivel_ayuda else ""
        nivel_autonomia_str = ", ".join(data.nivel_autonomia) if data.nivel_autonomia else ""
        tipo_resultado_str = ", ".join(data.tipo_resultado) if data.tipo_resultado else ""
        importancia_accion_str = ", ".join(data.importancia_accion) if data.importancia_accion else ""
        estilo_str = ", ".join(data.estilo_comunicacion) if data.estilo_comunicacion else ""
        formato_str = ", ".join(data.formato_preferido) if data.formato_preferido else ""

        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            data.email,
            data.nombre_cargo,
            data.filosofia_trabajo,
            data.responsabilidades,
            data.diferenciador,
            data.audiencia,
            data.proyecto_actual,
            data.cuello_botella,
            uso_str,
            nivel_ayuda_str,
            nivel_autonomia_str,
            tipo_resultado_str,
            importancia_accion_str,
            estilo_str,
            data.palabras_evitar,
            formato_str,
            data.enlaces_referencia or "",
            "Sí" if data.mailchimp_consent else "No",
            tags.get("profile_type", ""),
            tags.get("need", ""),
            tags.get("maturity", ""),
            tags.get("commercial_potential", ""),
            str(tags.get("score", 0))
        ]

        body = {"values": [row]}
        sheet.values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="A:X",
            valueInputOption="RAW",
            body=body
        ).execute()
        return True
    except Exception as e:
        print(f"Error guardando en Google Sheets: {e}")
        return False


def subscribe_to_mailchimp(data: FormData, tags: dict):
    if not data.mailchimp_consent:
        return False
    try:
        nombre_parts = data.nombre_cargo.split(" ")
        first_name = nombre_parts[0] if nombre_parts else ""
        mailchimp_tags = ["configura-tu-ia"]
        profile = tags.get("profile_type", "").lower().replace(" ", "-")
        if profile and profile != "otro":
            mailchimp_tags.append(f"perfil-{profile}")
        need = tags.get("need", "").lower().replace(" ", "-")
        if need:
            mailchimp_tags.append(f"necesidad-{need}")
        maturity = tags.get("maturity", "").lower().replace(" ", "-")
        if maturity:
            mailchimp_tags.append(f"madurez-{maturity}")
        potential = tags.get("commercial_potential", "").lower()
        if potential:
            mailchimp_tags.append(f"potencial-{potential}")

        url = f"https://{MAILCHIMP_SERVER_PREFIX}.api.mailchimp.com/3.0/lists/{MAILCHIMP_LIST_ID}/members"
        payload = {
            "email_address": data.email,
            "status": "subscribed",
            "merge_fields": {"FNAME": first_name},
            "tags": mailchimp_tags
        }
        response = requests.post(url, auth=("anystring", MAILCHIMP_API_KEY), json=payload)
        if response.status_code == 400 and "already a list member" in response.text:
            import hashlib
            email_hash = hashlib.md5(data.email.lower().encode()).hexdigest()
            update_url = f"https://{MAILCHIMP_SERVER_PREFIX}.api.mailchimp.com/3.0/lists/{MAILCHIMP_LIST_ID}/members/{email_hash}"
            requests.patch(update_url, auth=("anystring", MAILCHIMP_API_KEY),
                           json={"merge_fields": {"FNAME": first_name}, "tags": mailchimp_tags})
            tags_url = f"https://{MAILCHIMP_SERVER_PREFIX}.api.mailchimp.com/3.0/lists/{MAILCHIMP_LIST_ID}/members/{email_hash}/tags"
            tags_payload = {"tags": [{"name": t, "status": "active"} for t in mailchimp_tags]}
            requests.post(tags_url, auth=("anystring", MAILCHIMP_API_KEY), json=tags_payload)
        return True
    except Exception as e:
        print(f"Error en Mailchimp: {e}")
        return False


def send_document_by_email(recipient_email: str, document: str, nombre_cargo: str):
    if not GMAIL_APP_PASSWORD:
        print("GMAIL_APP_PASSWORD no configurado, omitiendo envío de email")
        return False
    try:
        nombre = nombre_cargo.split(",")[0].strip() if "," in nombre_cargo else nombre_cargo.split()[0]
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Tu Documento Maestro de Contexto para IA está listo"
        msg["From"] = f"Fedor Sawoloka <{GMAIL_USER}>"
        msg["To"] = recipient_email

        lines = []
        for line in document.split("\n"):
            if line.startswith("## "):
                lines.append(f"<h2>{line[3:]}</h2>")
            elif line.startswith("# "):
                lines.append(f"<h1>{line[2:]}</h1>")
            elif line.startswith("---"):
                lines.append("<hr>")
            elif line.strip() == "":
                lines.append("<br>")
            else:
                lines.append(f"<p>{line}</p>")
        doc_html_clean = "\n".join(lines)

        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 700px; margin: 0 auto; padding: 20px; color: #2C3E50;">
            <div style="background: #2C3E50; padding: 20px; border-radius: 8px 8px 0 0;">
                <h1 style="color: white; margin: 0; font-size: 20px;">Tu Documento Maestro de Contexto</h1>
                <p style="color: #FF8C42; margin: 5px 0 0 0;">Generado con la metodología Gold Standard de Fedor Sawoloka</p>
            </div>
            <div style="background: #f5f6fa; padding: 25px; border-radius: 0 0 8px 8px; border: 1px solid #dee2e6;">
                <p>Hola, aquí está tu documento listo para usar en cualquier IA.</p>
                <p><strong>Instrucciones:</strong> Copia el texto del documento y pégalo al inicio de cualquier conversación con ChatGPT, Claude, Gemini u otra IA.</p>
                <hr style="border: 1px solid #dee2e6; margin: 20px 0;">
                {doc_html_clean}
                <hr style="border: 1px solid #dee2e6; margin: 20px 0;">
                <p style="font-size: 12px; color: #6c757d;">Generado en <a href="https://yosoyelruso.com/configura-tu-ia/" style="color: #FF8C42;">yosoyelruso.com/configura-tu-ia</a></p>
            </div>
        </body>
        </html>
        """
        text_body = f"Tu Documento Maestro de Contexto\n\n{document}\n\n---\nGenerado en yosoyelruso.com/configura-tu-ia"
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, recipient_email, msg.as_string())
        print(f"Email enviado exitosamente a {recipient_email}")
        return True
    except Exception as e:
        print(f"Error enviando email: {e}")
        return False


# ============================================================
# FUNCIONES AUXILIARES — Programa Anti-Inercia (nuevo)
# ============================================================

def get_google_sheets_service():
    """Devuelve un servicio autenticado de Google Sheets."""
    if GOOGLE_CREDENTIALS_JSON:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
    else:
        credentials_path = GOOGLE_CREDENTIALS_FILE
        if not os.path.isabs(credentials_path):
            credentials_path = os.path.join(os.path.dirname(__file__), credentials_path)
        creds = service_account.Credentials.from_service_account_file(
            credentials_path,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
    return build("sheets", "v4", credentials=creds)


def check_email_access(email: str) -> bool:
    """
    Verifica si el email tiene acceso al Programa Anti-Inercia.
    Lee la pestaña 'Acceso_Programa' del Google Sheet.
    Columna A: email | Columna B: activo (SI/NO)
    """
    try:
        service = get_google_sheets_service()
        sheet = service.spreadsheets()
        result = sheet.values().get(
            spreadsheetId=PROGRAMA_SHEET_ID,
            range="Acceso_Programa!A:B"
        ).execute()
        values = result.get("values", [])
        email_lower = email.strip().lower()
        for row in values:
            if len(row) >= 1 and row[0].strip().lower() == email_lower:
                # Si hay columna B, verificar que sea "SI" o "SÍ" o "si" o "sí"
                if len(row) >= 2:
                    estado = row[1].strip().upper()
                    return estado in ["SI", "SÍ", "YES", "ACTIVO", "1", "TRUE"]
                else:
                    # Si no hay columna B, la presencia del email ya da acceso
                    return True
        return False
    except Exception as e:
        print(f"Error verificando acceso: {e}")
        return False


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extrae texto limpio de un PDF en bytes usando pdfplumber."""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text_parts = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    text_parts.append(text)
            return "\n".join(text_parts)
    except Exception as e:
        print(f"Error extrayendo texto del PDF: {e}")
        return ""


def limpiar_para_pdf(texto: str) -> str:
    """
    Convierte caracteres Unicode especiales a equivalentes ASCII/Latin-1
    compatibles con la fuente Helvetica de fpdf2.
    """
    reemplazos = {
        # Comillas tipográficas
        '\u2018': "'", '\u2019': "'", '\u201a': "'", '\u201b': "'",
        '\u201c': '"', '\u201d': '"', '\u201e': '"', '\u201f': '"',
        # Guiones especiales
        '\u2013': '-', '\u2014': '-', '\u2015': '-',
        # Puntos suspensivos
        '\u2026': '...',
        # Espacios especiales
        '\u00a0': ' ', '\u202f': ' ', '\u2009': ' ',
        # Flechas y símbolos comunes
        '\u2192': '->', '\u2190': '<-', '\u2022': '-', '\u25cf': '-',
        '\u2713': 'OK', '\u2714': 'OK', '\u2715': 'X', '\u2716': 'X',
        '\u2610': '[ ]', '\u2611': '[X]',
        # Fracciones y superscripts
        '\u00bd': '1/2', '\u00bc': '1/4', '\u00be': '3/4',
        # Otros comunes
        '\u00b7': '-', '\u2217': '*', '\u00d7': 'x', '\u00f7': '/',
    }
    for char, reemplazo in reemplazos.items():
        texto = texto.replace(char, reemplazo)
    # Eliminar cualquier caracter fuera del rango Latin-1 que quede
    resultado = []
    for c in texto:
        try:
            c.encode('latin-1')
            resultado.append(c)
        except (UnicodeEncodeError, UnicodeDecodeError):
            resultado.append('?')
    return ''.join(resultado)


def generate_pdf_branded(content: str, titulo: str, subtitulo: str, nombre_archivo: str) -> bytes:
    """
    Genera un PDF con branding de la marca Anti-Inercia.
    Paleta: Azul marino #2C3E50, Naranja #FF8C42, Blanco, Gris claro.
    """
    # Limpiar TODOS los textos antes de cualquier operacion PDF
    content = limpiar_para_pdf(content)
    titulo = limpiar_para_pdf(titulo)
    subtitulo = limpiar_para_pdf(subtitulo)

    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # Encabezado con fondo azul marino
    pdf.set_fill_color(44, 62, 80)
    pdf.rect(0, 0, 210, 28, 'F')
    pdf.set_text_color(255, 255, 255)
    pdf.set_font('Helvetica', 'B', 14)
    pdf.set_xy(15, 7)
    pdf.cell(0, 8, titulo, ln=False)
    pdf.set_font('Helvetica', '', 9)
    pdf.set_text_color(255, 140, 66)
    pdf.set_xy(15, 18)
    pdf.cell(0, 6, subtitulo, ln=False)

    pdf.set_y(35)
    pdf.set_text_color(44, 62, 80)

    lines = content.split('\n')
    for line in lines:
        line_stripped = line.strip()
        if line_stripped.startswith('## '):
            pdf.ln(5)
            pdf.set_font('Helvetica', 'B', 12)
            pdf.set_text_color(44, 62, 80)
            pdf.multi_cell(0, 6, line_stripped[3:], ln=True)
            pdf.set_draw_color(255, 140, 66)
            pdf.set_line_width(0.5)
            pdf.line(15, pdf.get_y(), 195, pdf.get_y())
            pdf.ln(3)
        elif line_stripped.startswith('# '):
            pdf.ln(4)
            pdf.set_font('Helvetica', 'B', 14)
            pdf.set_text_color(44, 62, 80)
            pdf.multi_cell(0, 7, line_stripped[2:], ln=True)
            pdf.ln(2)
        elif line_stripped.startswith('### '):
            pdf.ln(3)
            pdf.set_font('Helvetica', 'B', 11)
            pdf.set_text_color(255, 140, 66)
            pdf.multi_cell(0, 6, line_stripped[4:], ln=True)
            pdf.ln(1)
        elif line_stripped.startswith('---'):
            pdf.ln(2)
            pdf.set_draw_color(200, 200, 200)
            pdf.set_line_width(0.3)
            pdf.line(15, pdf.get_y(), 195, pdf.get_y())
            pdf.ln(3)
        elif line_stripped == '':
            pdf.ln(3)
        else:
            clean = line_stripped.replace('**', '').replace('*', '')
            # Detectar listas con guión o bullet
            if clean.startswith('- ') or clean.startswith('• '):
                pdf.set_font('Helvetica', '', 10)
                pdf.set_text_color(60, 60, 60)
                pdf.set_x(20)
                pdf.multi_cell(175, 5, clean, ln=True)
            else:
                pdf.set_font('Helvetica', '', 10)
                pdf.set_text_color(60, 60, 60)
                pdf.multi_cell(0, 5, clean, ln=True)

    # Pie de página
    pdf.set_y(-15)
    pdf.set_font('Helvetica', 'I', 8)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 10, f'Programa Anti-Inercia de Marca Personal  |  yosoyelruso.com  |  {datetime.now().strftime("%d/%m/%Y")}', align='C')

    return bytes(pdf.output())


def save_programa_lead(email: str, modulo: int, datos_adicionales: dict = None):
    """Registra la actividad del usuario en el programa en Google Sheets."""
    try:
        service = get_google_sheets_service()
        sheet = service.spreadsheets()
        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            email,
            f"Módulo {modulo}",
            json.dumps(datos_adicionales or {}, ensure_ascii=False)
        ]
        body = {"values": [row]}
        sheet.values().append(
            spreadsheetId=PROGRAMA_SHEET_ID,
            range="Actividad_Programa!A:D",
            valueInputOption="RAW",
            body=body
        ).execute()
        return True
    except Exception as e:
        print(f"Error guardando actividad del programa: {e}")
        return False


def generate_modulo0_gemini(respuestas: dict) -> str:
    """Genera el Mapa de Fricciones (Módulo 0) usando Gemini."""
    client = google_genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""Eres el motor de análisis del Programa Anti-Inercia de Marca Personal, creado por Fedor Sawoloka.

Este programa se basa en la Metodología Gold Standard Anti-Inercia, cuyos principios fundamentales son:
1. DIAGNÓSTICO PRIMERO, ESTRATEGIA DESPUÉS: Nunca proponer soluciones sin un diagnóstico profundo. La calidad de la estrategia depende de la calidad del análisis.
2. VALOR REAL VS. VALOR PERCIBIDO: El problema central de la mayoría de profesionales es que tienen más valor del que comunican. Tu trabajo es identificar esa brecha.
3. ROI COMO MÉTRICA PRINCIPAL: El éxito se mide en impacto de negocio (clientes, ingresos, posicionamiento), no en métricas de vanidad.
4. SISTEMA SOBRE TÁCTICAS: No se entregan listas de tareas, sino diagnósticos que permiten construir sistemas sostenibles.
5. ANÁLISIS SINCERO SOBRE PLANTILLA: Cada análisis es producto del caso específico, no de aplicar una fórmula genérica.

Tu tarea es generar el "Mapa de Fricciones" — el documento de diagnóstico del Módulo 0.

INSTRUCCIONES DE TONO Y ESTILO:
- Este es el PRIMER módulo del programa. La persona acaba de comenzar. Tu tono debe ser HONESTO Y DIRECTO, pero nunca brutal ni desmotivador.
- El objetivo es que el participante salga pensando: "Esto me abrió los ojos" — no "Esto me destruyó".
- Confronta la realidad con respeto. Nombra los problemas con claridad, pero siempre desde un lugar constructivo.
- NO uses lenguaje condescendiente ni paternalista. Habla de igual a igual, como un estratega que ve lo que el participante aún no puede ver.
- Identifica patrones reales. No repitas las respuestas del usuario — interprétalas.
- Si el diferenciador del usuario usa palabras vacías como "calidad", "experiencia" o "pasión", señálalo con tacto: no como un error, sino como una oportunidad de mejora.
- Formato: usa ## para secciones principales y ### para subsecciones.
- NO uses emojis ni símbolos decorativos.

VOCABULARIO DE LA METODOLOGÍA (úsalo de forma natural, no forzada):
- "Inercia": patrón de comportamiento que se repite y frena el avance
- "Valor real vs. valor percibido": brecha entre lo que el profesional vale y lo que el mercado percibe
- "Arquetipo": el posicionamiento único que el profesional debe ocupar (se desarrollará en módulos posteriores)
- "Paradoja": el problema central que resume la situación del participante (ej. "El experto que nadie conoce")
- "Anti-Inercia": la filosofía de romper patrones que frenan el crecimiento

ESTRUCTURA OBLIGATORIA DEL MAPA DE FRICCIONES:

## Diagnóstico de Punto Cero
Síntesis clara de dónde está parado el participante hoy. Identifica la "Paradoja" central — el problema raíz que resume su situación en una frase con nombre propio (ej. "El Experto Invisible", "El Profesional en Construcción"). Máximo 3 párrafos. Honesto, no cruel.

## Radiografía del Negocio
Análisis de la situación actual: fuente de clientes, flujo, formalización de oferta. Qué está funcionando (si algo lo está) y qué no. Termina con una implicación estratégica clara.

## Estado de la Presencia Digital
Qué tan visible y coherente es su presencia hoy. Identifica la brecha entre lo que existe y lo que necesita. Termina con una implicación estratégica clara.

## Fricciones Identificadas
Las 3 inercias principales que están frenando su avance. Para cada una:
### [Nombre de la inercia]
- Cómo se manifiesta en su día a día
- Qué la está causando (raíz real, no síntoma)

## Nivel de Disposición para Ejecutar
Análisis honesto del nivel de compromiso declarado vs. la evidencia en sus respuestas. Si hay contradicción entre lo que dice y lo que muestra, señálala con respeto. El objetivo es que el participante tome conciencia, no que se sienta juzgado.

## Lo que viene: Tus Próximas Prioridades
No estrategia de contenido todavía. Las 3 cosas que este participante debe resolver o clarificar antes de avanzar al Módulo 1. Formuladas como oportunidades, no como reproches.

---
Documento generado por el Programa Anti-Inercia de Marca Personal | Metodologia de Fedor Sawoloka | yosoyelruso.com

RESPUESTAS DEL PARTICIPANTE:

BLOQUE 1 — NEGOCIO HOY:
- ¿A qué te dedicas?: {respuestas.get('dedicacion', 'No respondido')}
- Tiempo ejerciendo de forma independiente: {respuestas.get('tiempo_independiente', 'No respondido')}
- Principal fuente de clientes: {respuestas.get('fuente_clientes', 'No respondido')}
- Clientes activos: {respuestas.get('clientes_activos', 'No respondido')}
- Flujo de clientes actual: {respuestas.get('flujo_clientes', 'No respondido')}

BLOQUE 2 — OFERTA:
- ¿Tiene servicios y precios definidos?: {respuestas.get('servicios_definidos', 'No respondido')}
- Facilidad para explicar lo que hace: {respuestas.get('facilidad_explicar', 'No respondido')}
- Diferenciador (palabras del usuario): {respuestas.get('diferenciador', 'No respondido')}
- Resultado concreto que obtiene un cliente: {respuestas.get('resultado_cliente', 'No respondido')}

BLOQUE 3 — PRESENCIA DIGITAL:
- Plataformas activas: {respuestas.get('plataformas', 'No respondido')}
- Frecuencia de publicación: {respuestas.get('frecuencia_publicacion', 'No respondido')}
- Qué aparece al buscar su nombre en Google: {respuestas.get('google_resultado', 'No respondido')}
- Qué encuentra un cliente potencial en su perfil: {respuestas.get('perfil_actual', 'No respondido')}

BLOQUE 4 — FRICCIONES REALES:
- Razón principal por la que no está donde quiere: {respuestas.get('razon_principal', 'No respondido')}
- Ampliación de la razón (si aplica): {respuestas.get('razon_ampliacion', 'No respondido')}
- ¿Ha intentado trabajar su marca antes?: {respuestas.get('intento_previo', 'No respondido')}
- ¿Qué falló en intentos anteriores?: {respuestas.get('que_fallo', 'No respondido')}
- Nivel de disposición para ejecutar (1-10): {respuestas.get('disposicion', 'No respondido')}
- Qué necesitaría para que el programa valiera la pena: {respuestas.get('exito_definido', 'No respondido')}

BLOQUE 5 — CONTEXTO:
- Horas semanales disponibles: {respuestas.get('horas_semanales', 'No respondido')}
- Nivel de comodidad con herramientas digitales: {respuestas.get('nivel_digital', 'No respondido')}
- Presupuesto disponible: {respuestas.get('presupuesto', 'No respondido')}
"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text


# ============================================================
# ENDPOINT DE DIAGNÓSTICO (temporal — solo para debugging)
# ============================================================

@app.get("/programa/diagnostico")
def diagnostico_programa():
    """Endpoint temporal para verificar configuración del programa."""
    resultado = {}
    try:
        service = get_google_sheets_service()
        # Obtener info del spreadsheet (pestañas disponibles)
        spreadsheet = service.spreadsheets().get(
            spreadsheetId=PROGRAMA_SHEET_ID
        ).execute()
        pestanas = [s['properties']['title'] for s in spreadsheet.get('sheets', [])]
        resultado['sheet_id'] = PROGRAMA_SHEET_ID
        resultado['pestanas_disponibles'] = pestanas
        resultado['acceso_sheet'] = 'OK'

        # Intentar leer Acceso_Programa
        try:
            values_result = service.spreadsheets().values().get(
                spreadsheetId=PROGRAMA_SHEET_ID,
                range="Acceso_Programa!A:B"
            ).execute()
            valores = values_result.get('values', [])
            resultado['acceso_programa_filas'] = len(valores)
            resultado['acceso_programa_contenido'] = valores[:5]  # primeras 5 filas
        except Exception as e2:
            resultado['error_lectura_acceso_programa'] = str(e2)

        # Obtener email de la cuenta de servicio
        if GOOGLE_CREDENTIALS_JSON:
            creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
            resultado['service_account_email'] = creds_dict.get('client_email', 'no encontrado')

    except Exception as e:
        resultado['error'] = str(e)

    return resultado


# ============================================================
# ENDPOINTS — Configura tu IA (existentes, sin cambios)
# ============================================================

@app.get("/")
def root():
    return {"status": "ok", "service": "Fedor Sawoloka - Backend v3.0"}

@app.get("/health")
def health():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.post("/generate", response_model=GenerateResponse)
async def generate(data: FormData):
    tags = classify_profile(data)
    document = None
    fallback_used = False
    try:
        document = generate_document_gemini(data)
    except Exception as e:
        print(f"Gemini falló: {e}")
        fallback_used = True
        document = generate_document_fallback(data)
    try:
        save_to_google_sheets(data, tags)
    except Exception as e:
        print(f"Google Sheets falló: {e}")
    try:
        subscribe_to_mailchimp(data, tags)
    except Exception as e:
        print(f"Mailchimp falló: {e}")
    email_sent = False
    try:
        email_sent = send_document_by_email(data.email, document, data.nombre_cargo)
    except Exception as e:
        print(f"Email falló: {e}")
    return GenerateResponse(
        success=True,
        document=document,
        fallback=fallback_used,
        email_sent=email_sent
    )

@app.post("/download-pdf")
def download_pdf(req: PdfRequest):
    try:
        pdf = FPDF(orientation='P', unit='mm', format='A4')
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        pdf.set_fill_color(44, 62, 80)
        pdf.rect(0, 0, 210, 22, 'F')
        pdf.set_text_color(255, 255, 255)
        pdf.set_font('Helvetica', 'B', 13)
        pdf.set_xy(15, 8)
        pdf.cell(0, 8, 'Documento Maestro de Contexto', ln=False)
        pdf.set_font('Helvetica', '', 9)
        pdf.set_text_color(255, 140, 66)
        pdf.set_xy(15, 16)
        pdf.cell(0, 6, 'Metodologia Gold Standard  |  yosoyelruso.com', ln=False)
        pdf.set_y(28)
        pdf.set_text_color(44, 62, 80)
        lines = req.document.split('\n')
        for line in lines:
            line = line.strip()
            if line.startswith('## '):
                pdf.ln(4)
                pdf.set_font('Helvetica', 'B', 12)
                pdf.set_text_color(44, 62, 80)
                pdf.multi_cell(0, 6, line[3:], ln=True)
                pdf.set_draw_color(255, 140, 66)
                pdf.set_line_width(0.5)
                pdf.line(15, pdf.get_y(), 195, pdf.get_y())
                pdf.ln(3)
            elif line.startswith('# '):
                pdf.ln(4)
                pdf.set_font('Helvetica', 'B', 14)
                pdf.set_text_color(44, 62, 80)
                pdf.multi_cell(0, 7, line[2:], ln=True)
                pdf.ln(2)
            elif line.startswith('---'):
                pdf.ln(2)
                pdf.set_draw_color(200, 200, 200)
                pdf.set_line_width(0.3)
                pdf.line(15, pdf.get_y(), 195, pdf.get_y())
                pdf.ln(3)
            elif line == '':
                pdf.ln(3)
            else:
                clean = line.replace('**', '').replace('*', '')
                pdf.set_font('Helvetica', '', 10)
                pdf.set_text_color(60, 60, 60)
                pdf.multi_cell(0, 5, clean, ln=True)
        pdf_bytes = pdf.output()
        pdf_buffer = io.BytesIO(bytes(pdf_bytes))
        return StreamingResponse(
            pdf_buffer,
            media_type='application/pdf',
            headers={
                'Content-Disposition': 'attachment; filename="documento-maestro-contexto.pdf"',
                'Content-Type': 'application/pdf'
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Error generando PDF: {str(e)}')


# ============================================================
# ENDPOINTS — Programa Anti-Inercia (nuevos)
# ============================================================

@app.post("/programa/check-access", response_model=AccessCheckResponse)
async def check_access(req: AccessCheckRequest):
    """
    Verifica si un email tiene acceso al Programa Anti-Inercia.
    Lee la pestaña 'Acceso_Programa' del Google Sheet.
    """
    allowed = check_email_access(req.email)
    if allowed:
        return AccessCheckResponse(
            allowed=True,
            message="Acceso verificado. Bienvenido al Programa Anti-Inercia."
        )
    else:
        return AccessCheckResponse(
            allowed=False,
            message="Este correo no tiene acceso al programa. Si ya realizaste tu pago, escríbele a Fedor directamente."
        )


@app.post("/programa/modulo0/generar")
async def generar_modulo0(
    email: str = Form(...),
    dedicacion: str = Form(...),
    tiempo_independiente: str = Form(...),
    fuente_clientes: str = Form(...),
    clientes_activos: str = Form(...),
    flujo_clientes: str = Form(...),
    servicios_definidos: str = Form(...),
    facilidad_explicar: str = Form(...),
    diferenciador: str = Form(...),
    resultado_cliente: str = Form(...),
    plataformas: str = Form(...),
    frecuencia_publicacion: str = Form(...),
    google_resultado: str = Form(...),
    perfil_actual: str = Form(...),
    razon_principal: str = Form(...),
    razon_ampliacion: str = Form(default=""),
    intento_previo: str = Form(...),
    que_fallo: str = Form(default=""),
    disposicion: str = Form(...),
    exito_definido: str = Form(...),
    horas_semanales: str = Form(...),
    nivel_digital: str = Form(...),
    presupuesto: str = Form(...)
):
    """
    Procesa el Módulo 0 del Programa Anti-Inercia:
    1. Verifica acceso del email
    2. Genera el Mapa de Fricciones con Gemini
    3. Registra actividad en Google Sheets
    4. Devuelve el PDF para descarga
    """

    # 1. Verificar acceso
    if not check_email_access(email):
        raise HTTPException(
            status_code=403,
            detail="Este correo no tiene acceso al programa."
        )

    # 2. Construir diccionario de respuestas
    respuestas = {
        "dedicacion": dedicacion,
        "tiempo_independiente": tiempo_independiente,
        "fuente_clientes": fuente_clientes,
        "clientes_activos": clientes_activos,
        "flujo_clientes": flujo_clientes,
        "servicios_definidos": servicios_definidos,
        "facilidad_explicar": facilidad_explicar,
        "diferenciador": diferenciador,
        "resultado_cliente": resultado_cliente,
        "plataformas": plataformas,
        "frecuencia_publicacion": frecuencia_publicacion,
        "google_resultado": google_resultado,
        "perfil_actual": perfil_actual,
        "razon_principal": razon_principal,
        "razon_ampliacion": razon_ampliacion,
        "intento_previo": intento_previo,
        "que_fallo": que_fallo,
        "disposicion": disposicion,
        "exito_definido": exito_definido,
        "horas_semanales": horas_semanales,
        "nivel_digital": nivel_digital,
        "presupuesto": presupuesto
    }

    # 3. Generar documento con Gemini
    try:
        documento = generate_modulo0_gemini(respuestas)
    except Exception as e:
        print(f"Gemini falló en Módulo 0: {e}")
        raise HTTPException(status_code=500, detail="Error generando el documento. Intenta de nuevo en unos minutos.")

    # 4. Registrar actividad
    try:
        save_programa_lead(email, 0, {"disposicion": disposicion, "horas": horas_semanales})
    except Exception as e:
        print(f"Error registrando actividad M0: {e}")

    # 5. Generar y devolver PDF
    try:
        pdf_bytes = generate_pdf_branded(
            content=documento,
            titulo="Mapa de Fricciones — Módulo 0",
            subtitulo="Programa Anti-Inercia de Marca Personal  |  yosoyelruso.com",
            nombre_archivo="mapa-de-fricciones-modulo-0.pdf"
        )
        pdf_buffer = io.BytesIO(pdf_bytes)
        return StreamingResponse(
            pdf_buffer,
            media_type='application/pdf',
            headers={
                'Content-Disposition': 'attachment; filename="mapa-de-fricciones-modulo-0.pdf"',
                'Content-Type': 'application/pdf'
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Error generando PDF: {str(e)}')


# ============================================================
# FUNCIONES GENERADORAS — Módulos 1, 2 y 3
# ============================================================

def generate_modulo1_gemini(respuestas: dict, contexto_m0: str) -> str:
    """Genera el Documento de Auditoría y Diagnóstico (Módulo 1) usando Gemini."""
    client = google_genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""Eres el motor de análisis del Programa Anti-Inercia de Marca Personal, creado por Fedor Sawoloka.

Este módulo genera el "Documento de Auditoría y Diagnóstico" — el Entregable 1 de la Metodología Gold Standard Anti-Inercia.

METODOLOGÍA GOLD STANDARD — PRINCIPIOS QUE RIGEN ESTE DOCUMENTO:
1. DIAGNÓSTICO BASADO EN EVIDENCIA: Cada hallazgo debe estar respaldado por datos específicos del participante, no en generalizaciones.
2. VALOR REAL VS. VALOR PERCIBIDO: Identifica la brecha entre lo que el profesional realmente vale y lo que el mercado percibe de él.
3. ANÁLISIS SINCERO SOBRE PLANTILLA: Este documento es producto del caso específico, no de aplicar una fórmula genérica.
4. SISTEMA SOBRE TÁCTICAS: El objetivo es identificar el sistema que falta, no dar una lista de tareas.
5. ROI COMO MÉTRICA: El éxito se mide en impacto de negocio, no en métricas de vanidad.

INSTRUCCIONES DE TONO Y ESTILO:
- Tono: directo, analítico, profesional. Como un estratega que ve el panorama completo.
- NO es un documento motivacional, pero tampoco es brutal. Es honesto y constructivo.
- Usa el vocabulario de la metodología: "inercia", "valor percibido", "arquetipo", "paradoja", "Anti-Inercia".
- Cada sección debe terminar con una implicación estratégica clara.
- Formato: ## para secciones principales, ### para subsecciones.
- NO uses emojis ni símbolos decorativos.

ESTRUCTURA OBLIGATORIA — ENTREGABLE 1 (AUDITORÍA Y DIAGNÓSTICO):

## Resumen Ejecutivo
La "Paradoja" o problema central del participante (ej. "El Experto que el Mercado No Puede Ver"). La brecha entre su valor real y su valor percibido, en 2-3 párrafos directos.

## Oportunidad de Mercado
Contextualiza el servicio del participante dentro de una tendencia real. Por qué lo que ofrece es una necesidad, no un lujo. Basado en su sector y audiencia declarados.

## Auditoría de Presencia Digital
Análisis detallado de cada canal declarado por el participante. Para cada uno: qué existe, qué comunica, qué falta y qué implicación tiene. Incluye el análisis del perfil principal con la bio que el participante describió.

## Análisis de Competencia y Referentes
Basado en los 3 perfiles analizados por el participante. Para cada uno: posicionamiento, estrategia de contenido, fortalezas, debilidades y lección aplicable. Cierra con la síntesis: el hueco de mercado que el participante puede ocupar.

## Diagnóstico FODA
Matriz con fortalezas, oportunidades, debilidades y amenazas. Cada punto con evidencia concreta de las respuestas del participante. Sin adjetivos vacíos.

## Identificación de Inercias Críticas
Las 3 inercias más importantes identificadas en este módulo (pueden complementar o profundizar las del Módulo 0). Para cada una:
### [Nombre de la inercia]
- Cómo se manifiesta con evidencia específica
- Raíz real (no el síntoma)
- Impacto en el negocio si no se rompe

## El Arquetipo Ganador
El posicionamiento único que este participante debe ocupar. Nombre del arquetipo, sus atributos y la nueva Propuesta de Valor Única (PUV). Basado en el diferenciador real identificado en sus respuestas y en el análisis de competencia.

## Diagnóstico Final
El problema raíz resumido en una sola frase contundente. La síntesis de todo el análisis anterior.

## El Siguiente Paso
Puente hacia el Módulo 2 (Síntesis Estratégica y ADN). Qué construirán juntos en el próximo módulo y por qué este diagnóstico lo hace posible.

---
Documento generado por el Programa Anti-Inercia de Marca Personal | Metodologia de Fedor Sawoloka | yosoyelruso.com

CONTEXTO DEL MÓDULO 0 (Mapa de Fricciones previo del participante):
{contexto_m0[:3000] if contexto_m0 else 'No disponible'}

RESPUESTAS DEL MÓDULO 1:

PARTE 1 — AUDITORÍA DE PRESENCIA ACTUAL:
- Foto de perfil: {respuestas.get('foto_perfil', 'No respondido')}
- Bio actual (texto exacto): {respuestas.get('bio_actual', 'No respondido')}
- ¿Un desconocido entendería en 10 segundos qué hace?: {respuestas.get('claridad_bio', 'No respondido')}
- ¿Tiene llamado a la acción en el perfil?: {respuestas.get('cta_perfil', 'No respondido')}
- Patrón en las últimas 9 publicaciones: {respuestas.get('patron_publicaciones', 'No respondido')}
- Nivel de interacción promedio: {respuestas.get('nivel_interaccion', 'No respondido')}

CANALES SECUNDARIOS:
{respuestas.get('canales_secundarios', 'No respondido')}

AUDITORÍA WEB:
- ¿Tiene sitio web?: {respuestas.get('tiene_web', 'No respondido')}
- Qué encuentra alguien en los primeros 5 segundos: {respuestas.get('web_primeros_segundos', 'No respondido')}
- ¿Tiene formulario de contacto funcionando?: {respuestas.get('web_formulario', 'No respondido')}
- ¿Tiene sistema de captación de correos?: {respuestas.get('web_captacion', 'No respondido')}

PARTE 2 — ANÁLISIS DE COMPETENCIA:
- Perfil 1 — Nombre: {respuestas.get('comp1_nombre', 'No respondido')}
- Perfil 1 — Por qué lo eligió: {respuestas.get('comp1_razon', 'No respondido')}
- Perfil 1 — Posicionamiento: {respuestas.get('comp1_posicionamiento', 'No respondido')}
- Perfil 1 — Tipo de contenido: {respuestas.get('comp1_contenido', 'No respondido')}
- Perfil 1 — Nivel de interacción: {respuestas.get('comp1_interaccion', 'No respondido')}
- Perfil 1 — Mayor fortaleza: {respuestas.get('comp1_fortaleza', 'No respondido')}
- Perfil 1 — Mayor debilidad: {respuestas.get('comp1_debilidad', 'No respondido')}
- Perfil 1 — Qué ofrece que el participante no: {respuestas.get('comp1_ventaja', 'No respondido')}
- Perfil 1 — Qué puede ofrecer el participante que él no: {respuestas.get('comp1_oportunidad', 'No respondido')}

- Perfil 2 — Nombre: {respuestas.get('comp2_nombre', 'No respondido')}
- Perfil 2 — Por qué lo eligió: {respuestas.get('comp2_razon', 'No respondido')}
- Perfil 2 — Posicionamiento: {respuestas.get('comp2_posicionamiento', 'No respondido')}
- Perfil 2 — Tipo de contenido: {respuestas.get('comp2_contenido', 'No respondido')}
- Perfil 2 — Nivel de interacción: {respuestas.get('comp2_interaccion', 'No respondido')}
- Perfil 2 — Mayor fortaleza: {respuestas.get('comp2_fortaleza', 'No respondido')}
- Perfil 2 — Mayor debilidad: {respuestas.get('comp2_debilidad', 'No respondido')}
- Perfil 2 — Qué ofrece que el participante no: {respuestas.get('comp2_ventaja', 'No respondido')}
- Perfil 2 — Qué puede ofrecer el participante que él no: {respuestas.get('comp2_oportunidad', 'No respondido')}

- Perfil 3 — Nombre: {respuestas.get('comp3_nombre', 'No respondido')}
- Perfil 3 — Por qué lo eligió: {respuestas.get('comp3_razon', 'No respondido')}
- Perfil 3 — Posicionamiento: {respuestas.get('comp3_posicionamiento', 'No respondido')}
- Perfil 3 — Tipo de contenido: {respuestas.get('comp3_contenido', 'No respondido')}
- Perfil 3 — Nivel de interacción: {respuestas.get('comp3_interaccion', 'No respondido')}
- Perfil 3 — Mayor fortaleza: {respuestas.get('comp3_fortaleza', 'No respondido')}
- Perfil 3 — Mayor debilidad: {respuestas.get('comp3_debilidad', 'No respondido')}
- Perfil 3 — Qué ofrece que el participante no: {respuestas.get('comp3_ventaja', 'No respondido')}
- Perfil 3 — Qué puede ofrecer el participante que él no: {respuestas.get('comp3_oportunidad', 'No respondido')}

- Patrón común en los 3 perfiles: {respuestas.get('patron_competencia', 'No respondido')}
- Hueco de mercado identificado: {respuestas.get('hueco_mercado', 'No respondido')}
- Aprendizaje del ejercicio: {respuestas.get('aprendizaje_competencia', 'No respondido')}

PARTE 3 — CLIENTE IDEAL:
- Quién es la persona específica que más se beneficia: {respuestas.get('cliente_ideal', 'No respondido')}
- Problema principal antes de llegar al participante: {respuestas.get('problema_cliente', 'No respondido')}
- Qué ha intentado antes y por qué no funcionó: {respuestas.get('intentos_cliente', 'No respondido')}
- Palabras que usa para describir su problema: {respuestas.get('palabras_cliente', 'No respondido')}
- Resultado específico que busca: {respuestas.get('resultado_buscado', 'No respondido')}
- Dónde está hoy (plataformas, qué consume, a quién sigue): {respuestas.get('donde_esta_cliente', 'No respondido')}
- Por qué no ha resuelto su problema todavía: {respuestas.get('razon_sin_resolver', 'No respondido')}

PARTE 4 — FODA PERSONAL:
- Fortalezas (mínimo 3 con evidencia): {respuestas.get('fortalezas', 'No respondido')}
- Debilidades (mínimo 3 honestas): {respuestas.get('debilidades', 'No respondido')}
- Oportunidades: {respuestas.get('oportunidades', 'No respondido')}
- Amenazas: {respuestas.get('amenazas', 'No respondido')}

PARTE 5 — INERCIAS CRÍTICAS:
- Inercia 1 — Patrón que se repite: {respuestas.get('inercia1_patron', 'No respondido')}
- Inercia 1 — Cómo se manifiesta: {respuestas.get('inercia1_manifestacion', 'No respondido')}
- Inercia 1 — Tiempo con esta inercia: {respuestas.get('inercia1_tiempo', 'No respondido')}
- Inercia 1 — Qué ha intentado para romperla: {respuestas.get('inercia1_intentos', 'No respondido')}

- Inercia 2 — Patrón que se repite: {respuestas.get('inercia2_patron', 'No respondido')}
- Inercia 2 — Cómo se manifiesta: {respuestas.get('inercia2_manifestacion', 'No respondido')}
- Inercia 2 — Tiempo con esta inercia: {respuestas.get('inercia2_tiempo', 'No respondido')}
- Inercia 2 — Qué ha intentado para romperla: {respuestas.get('inercia2_intentos', 'No respondido')}

- Inercia 3 — Patrón que se repite: {respuestas.get('inercia3_patron', 'No respondido')}
- Inercia 3 — Cómo se manifiesta: {respuestas.get('inercia3_manifestacion', 'No respondido')}
- Inercia 3 — Tiempo con esta inercia: {respuestas.get('inercia3_tiempo', 'No respondido')}
- Inercia 3 — Qué ha intentado para romperla: {respuestas.get('inercia3_intentos', 'No respondido')}
"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text


def generate_modulo2_gemini(respuestas: dict, contexto_previo: str) -> str:
    """Genera el Documento Maestro de Marca (Módulo 2) usando Gemini."""
    client = google_genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""Eres el motor de análisis del Programa Anti-Inercia de Marca Personal, creado por Fedor Sawoloka.

Este módulo genera el "Documento Maestro de Marca" — el núcleo estratégico de toda la identidad del participante.

ADVERTENCIA: Este es el módulo más importante del programa. Si el análisis aquí falla, todo lo que viene después falla. No apliques fórmulas genéricas. Trabaja exclusivamente con los datos del participante y el contexto de los módulos anteriores.

METODOLOGÍA GOLD STANDARD — PRINCIPIOS:
1. El Documento Maestro de Marca es el activo más valioso que puede tener un profesional independiente: claridad estratégica documentada.
2. Cada pilar debe estar libre de adjetivos vacíos. "Calidad", "experiencia" y "pasión" no son diferenciadores.
3. El Arquetipo Ganador debe ser tan específico que el cliente ideal, al leerlo, sienta que le están hablando directamente a él.
4. El Enemigo Común no es un competidor. Es una práctica, creencia o forma de hacer las cosas que le hace daño al cliente ideal.

INSTRUCCIONES DE TONO:
- Este documento es el espejo estratégico del participante. Debe sentir que "nadie más podría haber escrito esto sin conocerme a fondo".
- Directo, sin rodeos, sin relleno. Cada párrafo debe justificar su existencia.
- Formato: ## para secciones, ### para pilares.
- NO uses emojis ni símbolos decorativos.

ESTRUCTURA OBLIGATORIA — DOCUMENTO MAESTRO DE MARCA:

## El Problema Raíz
Una sola frase de entre 20 y 50 palabras que identifica el problema central del participante. No una lista. No una queja. Un diagnóstico. Seguido de 1 párrafo que explica por qué es el problema raíz y no un síntoma.

## Los 5 Pilares del ADN de Marca

### Pilar 1 — El Problema que Resuelves
No lo que hace. El problema concreto y específico que elimina. Redactado en las palabras que usa su cliente ideal, no en jerga técnica.

### Pilar 2 — Para Quién Exactamente
No un segmento demográfico. Una persona específica con contexto, situación y momento de urgencia. Tan específico que el cliente ideal se reconozca al leerlo.

### Pilar 3 — El Diferenciador Operativo
Qué hace de forma distinta a los demás en su mercado. Con evidencia o ejemplo concreto que lo respalde. Sin adjetivos vacíos.

### Pilar 4 — El Tono de Voz
La personalidad de su marca en palabras concretas. Cómo suena cuando explica lo que domina. Incluye frases que suenan como él y frases que NO suenan como él.

### Pilar 5 — El Enemigo Común
La práctica, creencia o forma de hacer las cosas en su industria contra la que lucha. Por qué le hace daño concreto a su cliente ideal. Formulado como postura, no como ataque a personas.

## El Arquetipo Ganador
La síntesis de los 5 pilares en una frase estructurada:
"Soy el/la [quién eres] que ayuda a [para quién exactamente] a [qué resultado concreto] a través de [diferenciador operativo], sin [el Enemigo Común que eliminas de la ecuación]."

Seguido de: por qué este arquetipo es el correcto para este participante, basado en el análisis de los módulos anteriores.

## Validación del Documento Maestro
Evaluación honesta de los 4 criterios de validación:
1. ¿Puede explicar su posicionamiento en 30 segundos sin usar palabras vacías?
2. ¿Su cliente ideal se reconocería al leer el Arquetipo Ganador?
3. ¿El Enemigo Común ataca una idea o práctica (no personas)?
4. ¿El diferenciador operativo es demostrable con evidencia?

Para cada criterio: Verde (cumple), Amarillo (necesita ajuste) o Rojo (requiere reescritura), con explicación específica.

## Lo que viene: Módulo 3
Qué construirán en la Propuesta de Estrategia y por qué este Documento Maestro es la base que lo hace posible.

---
Documento generado por el Programa Anti-Inercia de Marca Personal | Metodologia de Fedor Sawoloka | yosoyelruso.com

CONTEXTO DE MÓDULOS ANTERIORES:
{contexto_previo[:4000] if contexto_previo else 'No disponible'}

RESPUESTAS DEL MÓDULO 2:

PARTE 1 — EL PROBLEMA RAÍZ:
- Problema raíz (frase del participante): {respuestas.get('problema_raiz', 'No respondido')}
- Por qué es el problema raíz y no un síntoma: {respuestas.get('raiz_explicacion', 'No respondido')}

PARTE 2 — LOS 5 PILARES:
PILAR 1:
- Problema concreto que resuelve: {respuestas.get('p1_problema', 'No respondido')}
- En palabras del cliente: {respuestas.get('p1_palabras_cliente', 'No respondido')}

PILAR 2:
- Quién es exactamente: {respuestas.get('p2_quien', 'No respondido')}
- Momento de urgencia (detonador): {respuestas.get('p2_urgencia', 'No respondido')}

PILAR 3:
- Qué hace diferente: {respuestas.get('p3_diferente', 'No respondido')}
- Evidencia del diferencial: {respuestas.get('p3_evidencia', 'No respondido')}

PILAR 4:
- Estilo de comunicación natural: {respuestas.get('p4_estilo', 'No respondido')}
- Frases que suenan como él: {respuestas.get('p4_frases_si', 'No respondido')}
- Frases que NO suenan como él: {respuestas.get('p4_frases_no', 'No respondido')}
- Palabras que usa frecuentemente: {respuestas.get('p4_palabras_propias', 'No respondido')}
- Palabras del mercado que rechaza: {respuestas.get('p4_palabras_rechazo', 'No respondido')}

PILAR 5:
- Contra qué práctica o creencia lucha: {respuestas.get('p5_enemigo', 'No respondido')}
- Por qué le hace daño al cliente ideal: {respuestas.get('p5_dano', 'No respondido')}

PARTE 3 — ARQUETIPO GANADOR:
- Arquetipo formulado por el participante: {respuestas.get('arquetipo', 'No respondido')}
- ¿Le representa completamente?: {respuestas.get('arquetipo_validacion', 'No respondido')}
- Qué parte necesita ajuste (si aplica): {respuestas.get('arquetipo_ajuste', 'No respondido')}

PARTE 4 — VALIDACIÓN FINAL:
- ¿Puede explicar sin palabras vacías?: {respuestas.get('val_sin_vacias', 'No respondido')}
- ¿Cliente ideal se reconocería?: {respuestas.get('val_cliente_reconoce', 'No respondido')}
- ¿Enemigo Común ataca idea o persona?: {respuestas.get('val_enemigo', 'No respondido')}
- ¿Diferenciador es demostrable?: {respuestas.get('val_diferenciador', 'No respondido')}
"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text


def generate_modulo3_gemini(respuestas: dict, contexto_previo: str) -> str:
    """Genera la Propuesta de Estrategia (Módulo 3) usando Gemini."""
    client = google_genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""Eres el motor de análisis del Programa Anti-Inercia de Marca Personal, creado por Fedor Sawoloka.

Este módulo genera la "Propuesta de Estrategia" — el Entregable 2 de la Metodología Gold Standard Anti-Inercia.

PRINCIPIO RECTOR: Un diagnóstico sin estrategia es un documento bonito que no le sirve a nadie. Una estrategia sin diagnóstico es humo. Este documento convierte todo lo construido en los módulos anteriores en un plan con objetivos medibles, un sistema de conversión y un calendario de acción.

METODOLOGÍA GOLD STANDARD — PRINCIPIOS PARA ESTE ENTREGABLE:
1. EJECUTABILIDAD: El participante puede implementar esta estrategia sin necesidad de aclaraciones adicionales.
2. COHERENCIA: La estrategia resuelve directamente los problemas e inercias identificados en los módulos anteriores.
3. MÉTRICAS CLARAS: Cada objetivo tiene KPIs medibles y metas específicas.
4. SISTEMA SOBRE TÁCTICAS: No es una lista de tareas. Es un sistema sostenible.
5. ROI COMO MÉTRICA: Los objetivos están enfocados en impacto de negocio, no en vanity metrics.

INSTRUCCIONES DE TONO:
- Estratégico, ejecutable, sin relleno.
- Cada sección debe poder implementarse esta semana, no "en algún momento".
- Formato: ## para secciones, ### para subsecciones.
- NO uses emojis ni símbolos decorativos.

ESTRUCTURA OBLIGATORIA — PROPUESTA DE ESTRATEGIA:

## Introducción y Objetivos
Objetivo general y 3 objetivos específicos con KPIs y metas a 6 meses. Uno de visibilidad, uno de credibilidad, uno de conversión. Cada uno con número, plazo y criterio de éxito. Sin objetivos vagos.

## Los 3 Pilares Estratégicos de Contenido
Basados en el Documento Maestro de Marca del Módulo 2.
### Pilar A — Atracción
Tema central, 3 ejemplos de títulos concretos, formato recomendado. Función: que gente nueva descubra al participante.
### Pilar B — Autoridad
Tema central, 3 ejemplos de títulos concretos, tipo de evidencia a usar. Función: que quien ya lo conoce lo crea capaz.
### Pilar C — Conversión
Tema central, 3 ejemplos de títulos concretos, llamado a la acción principal. Función: que quien ya lo cree dé el siguiente paso.

## Sistema de Conversión
El camino de menor resistencia desde que alguien descubre al participante hasta que se convierte en cliente. Punto de entrada, siguiente paso, cierre. Configuración técnica recomendada según el nivel tecnológico declarado. La fricción principal identificada y cómo eliminarla.

## Plan de Implementación en Fases
### Fase 1 — Preparación (Semanas 1-2)
Acciones específicas para construir la base antes de aparecer en público.
### Fase 2 — Activación (Semanas 3-6)
Frecuencia de publicación, canal principal, cómo verificar que el embudo funciona.
### Fase 3 — Autoridad (Semanas 7-10)
Primera pieza de autoridad profunda, activación del pilar de conversión, primeras métricas a revisar.
### Fase 4 — Escalabilidad (Semanas 11-12)
Qué documentar, qué sistematizar, cómo diseñar el siguiente ciclo de 90 días.

## Las 3 Métricas que Debe Revisar
Explicación paso a paso de cómo acceder a cada métrica en su plataforma principal. Sin asumir conocimiento técnico previo. Calibradas al canal elegido y nivel tecnológico declarado.

## Protocolo de Ajuste — Los 3 Escenarios
Para consultar cuando algo no funcione:
- Escenario A: El alcance no crece — qué revisar y qué cambiar
- Escenario B: El alcance crece pero no llegan contactos — qué revisar y qué cambiar
- Escenario C: Llegan contactos pero no cierra — qué revisar y qué cambiar

## Propuesta de Colaboración
Cómo la Asesoría Estratégica Continua con Fedor Sawoloka puede acelerar la implementación de esta estrategia. Qué incluye, qué resuelve y cómo dar el siguiente paso.

---
Documento generado por el Programa Anti-Inercia de Marca Personal | Metodologia de Fedor Sawoloka | yosoyelruso.com

CONTEXTO DE MÓDULOS ANTERIORES:
{contexto_previo[:5000] if contexto_previo else 'No disponible'}

RESPUESTAS DEL MÓDULO 3:

PARTE 1 — OBJETIVOS Y KPIs:
- Objetivo de visibilidad: {respuestas.get('obj_visibilidad', 'No respondido')}
- Cómo medir el avance semana a semana: {respuestas.get('obj_visibilidad_medicion', 'No respondido')}
- Objetivo de credibilidad: {respuestas.get('obj_credibilidad', 'No respondido')}
- Evidencia concreta a producir: {respuestas.get('obj_credibilidad_evidencia', 'No respondido')}
- Objetivo de conversión: {respuestas.get('obj_conversion', 'No respondido')}
- Qué debe pasar en el embudo para alcanzarlo: {respuestas.get('obj_conversion_embudo', 'No respondido')}

PARTE 2 — PILARES DE CONTENIDO:
- Pilar Atracción — tema central: {respuestas.get('pilar_a_tema', 'No respondido')}
- Pilar Atracción — 3 ejemplos de títulos: {respuestas.get('pilar_a_titulos', 'No respondido')}
- Pilar Atracción — formato: {respuestas.get('pilar_a_formato', 'No respondido')}
- Pilar Autoridad — tema central: {respuestas.get('pilar_b_tema', 'No respondido')}
- Pilar Autoridad — 3 ejemplos de títulos: {respuestas.get('pilar_b_titulos', 'No respondido')}
- Pilar Autoridad — tipo de evidencia: {respuestas.get('pilar_b_evidencia', 'No respondido')}
- Pilar Conversión — tema central: {respuestas.get('pilar_c_tema', 'No respondido')}
- Pilar Conversión — 3 ejemplos de títulos: {respuestas.get('pilar_c_titulos', 'No respondido')}
- Pilar Conversión — llamado a la acción: {respuestas.get('pilar_c_cta', 'No respondido')}

PARTE 3 — SISTEMA DE CONVERSIÓN:
- Punto de entrada principal: {respuestas.get('embudo_entrada', 'No respondido')}
- Siguiente paso después de descubrirlo: {respuestas.get('embudo_siguiente', 'No respondido')}
- Paso final antes de convertirse en cliente: {respuestas.get('embudo_cierre', 'No respondido')}
- Descripción del embudo completo: {respuestas.get('embudo_descripcion', 'No respondido')}
- Dónde se pierde la mayoría de la gente: {respuestas.get('embudo_friccion', 'No respondido')}
- Cómo eliminar esa fricción: {respuestas.get('embudo_solucion', 'No respondido')}
- Nivel tecnológico actual: {respuestas.get('nivel_tecnologico', 'No respondido')}
- Herramientas ya configuradas: {respuestas.get('herramientas_actuales', 'No respondido')}
- Primera herramienta a configurar en 15 días: {respuestas.get('herramienta_proxima', 'No respondido')}

PARTE 4 — PLAN DE IMPLEMENTACIÓN:
- Mayor obstáculo para la Fase 1: {respuestas.get('fase1_obstaculo', 'No respondido')}
- Qué necesita para arrancar sin fricción: {respuestas.get('fase1_necesita', 'No respondido')}
- Frecuencia de publicación en Fase 2: {respuestas.get('fase2_frecuencia', 'No respondido')}
- Canal principal en Fase 2: {respuestas.get('fase2_canal', 'No respondido')}
- Pieza de autoridad en mente para Fase 3: {respuestas.get('fase3_pieza', 'No respondido')}

PARTE 5 — VALIDACIÓN ESTRATÉGICA:
- ¿Objetivos alcanzables con recursos actuales?: {respuestas.get('val_objetivos', 'No respondido')}
- ¿El embudo es operable hoy?: {respuestas.get('val_embudo', 'No respondido')}
- ¿La Fase 1 puede arrancar esta semana?: {respuestas.get('val_fase1', 'No respondido')}
- Qué bloquea el arranque (si aplica): {respuestas.get('val_bloqueo', 'No respondido')}
"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text


# ============================================================
# ENDPOINTS — Módulos 1, 2 y 3
# ============================================================

@app.post("/programa/modulo1/generar")
async def generar_modulo1(
    email: str = Form(...),
    # Parte 1 — Auditoría de perfil principal
    foto_perfil: str = Form(...),
    bio_actual: str = Form(...),
    claridad_bio: str = Form(...),
    cta_perfil: str = Form(...),
    patron_publicaciones: str = Form(...),
    nivel_interaccion: str = Form(...),
    # Canales secundarios (JSON string)
    canales_secundarios: str = Form(default=""),
    # Auditoría web
    tiene_web: str = Form(...),
    web_primeros_segundos: str = Form(default=""),
    web_formulario: str = Form(default=""),
    web_captacion: str = Form(default=""),
    # Parte 2 — Competencia (3 perfiles)
    comp1_nombre: str = Form(...),
    comp1_razon: str = Form(...),
    comp1_posicionamiento: str = Form(...),
    comp1_contenido: str = Form(...),
    comp1_interaccion: str = Form(...),
    comp1_fortaleza: str = Form(...),
    comp1_debilidad: str = Form(...),
    comp1_ventaja: str = Form(...),
    comp1_oportunidad: str = Form(...),
    comp2_nombre: str = Form(...),
    comp2_razon: str = Form(...),
    comp2_posicionamiento: str = Form(...),
    comp2_contenido: str = Form(...),
    comp2_interaccion: str = Form(...),
    comp2_fortaleza: str = Form(...),
    comp2_debilidad: str = Form(...),
    comp2_ventaja: str = Form(...),
    comp2_oportunidad: str = Form(...),
    comp3_nombre: str = Form(...),
    comp3_razon: str = Form(...),
    comp3_posicionamiento: str = Form(...),
    comp3_contenido: str = Form(...),
    comp3_interaccion: str = Form(...),
    comp3_fortaleza: str = Form(...),
    comp3_debilidad: str = Form(...),
    comp3_ventaja: str = Form(...),
    comp3_oportunidad: str = Form(...),
    patron_competencia: str = Form(...),
    hueco_mercado: str = Form(...),
    aprendizaje_competencia: str = Form(...),
    # Parte 3 — Cliente ideal
    cliente_ideal: str = Form(...),
    problema_cliente: str = Form(...),
    intentos_cliente: str = Form(...),
    palabras_cliente: str = Form(...),
    resultado_buscado: str = Form(...),
    donde_esta_cliente: str = Form(...),
    razon_sin_resolver: str = Form(...),
    # Parte 4 — FODA
    fortalezas: str = Form(...),
    debilidades: str = Form(...),
    oportunidades: str = Form(...),
    amenazas: str = Form(...),
    # Parte 5 — Inercias
    inercia1_patron: str = Form(...),
    inercia1_manifestacion: str = Form(...),
    inercia1_tiempo: str = Form(...),
    inercia1_intentos: str = Form(...),
    inercia2_patron: str = Form(...),
    inercia2_manifestacion: str = Form(...),
    inercia2_tiempo: str = Form(...),
    inercia2_intentos: str = Form(...),
    inercia3_patron: str = Form(...),
    inercia3_manifestacion: str = Form(...),
    inercia3_tiempo: str = Form(...),
    inercia3_intentos: str = Form(...),
    # PDF del módulo anterior
    pdf_m0: UploadFile = File(...)
):
    if not check_email_access(email):
        raise HTTPException(status_code=403, detail="Este correo no tiene acceso al programa.")

    contexto_m0 = extract_text_from_pdf(await pdf_m0.read())

    respuestas = {k: v for k, v in locals().items() if k not in ['email', 'pdf_m0', 'contexto_m0']}

    try:
        documento = generate_modulo1_gemini(respuestas, contexto_m0)
    except Exception as e:
        print(f"Gemini falló en Módulo 1: {e}")
        raise HTTPException(status_code=500, detail="Error generando el documento. Intenta de nuevo en unos minutos.")

    try:
        save_programa_lead(email, 1, {"perfiles_analizados": f"{comp1_nombre}, {comp2_nombre}, {comp3_nombre}"})
    except Exception as e:
        print(f"Error registrando actividad M1: {e}")

    try:
        pdf_bytes = generate_pdf_branded(
            content=documento,
            titulo="Documento de Auditoria y Diagnostico — Modulo 1",
            subtitulo="Programa Anti-Inercia de Marca Personal  |  yosoyelruso.com",
            nombre_archivo="auditoria-diagnostico-modulo-1.pdf"
        )
        pdf_buffer = io.BytesIO(pdf_bytes)
        return StreamingResponse(
            pdf_buffer,
            media_type='application/pdf',
            headers={
                'Content-Disposition': 'attachment; filename="auditoria-diagnostico-modulo-1.pdf"',
                'Content-Type': 'application/pdf'
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Error generando PDF: {str(e)}')


@app.post("/programa/modulo2/generar")
async def generar_modulo2(
    email: str = Form(...),
    # Parte 1 — Problema raíz
    problema_raiz: str = Form(...),
    raiz_explicacion: str = Form(...),
    # Parte 2 — 5 Pilares
    p1_problema: str = Form(...),
    p1_palabras_cliente: str = Form(...),
    p2_quien: str = Form(...),
    p2_urgencia: str = Form(...),
    p3_diferente: str = Form(...),
    p3_evidencia: str = Form(...),
    p4_estilo: str = Form(...),
    p4_frases_si: str = Form(...),
    p4_frases_no: str = Form(...),
    p4_palabras_propias: str = Form(...),
    p4_palabras_rechazo: str = Form(...),
    p5_enemigo: str = Form(...),
    p5_dano: str = Form(...),
    # Parte 3 — Arquetipo
    arquetipo: str = Form(...),
    arquetipo_validacion: str = Form(...),
    arquetipo_ajuste: str = Form(default=""),
    # Parte 4 — Validación
    val_sin_vacias: str = Form(...),
    val_cliente_reconoce: str = Form(...),
    val_enemigo: str = Form(...),
    val_diferenciador: str = Form(...),
    # PDFs anteriores
    pdf_m0: UploadFile = File(...),
    pdf_m1: UploadFile = File(...)
):
    if not check_email_access(email):
        raise HTTPException(status_code=403, detail="Este correo no tiene acceso al programa.")

    texto_m0 = extract_text_from_pdf(await pdf_m0.read())
    texto_m1 = extract_text_from_pdf(await pdf_m1.read())
    contexto_previo = f"=== MÓDULO 0 ===\n{texto_m0}\n\n=== MÓDULO 1 ===\n{texto_m1}"

    respuestas = {k: v for k, v in locals().items() if k not in ['email', 'pdf_m0', 'pdf_m1', 'contexto_previo', 'texto_m0', 'texto_m1']}

    try:
        documento = generate_modulo2_gemini(respuestas, contexto_previo)
    except Exception as e:
        print(f"Gemini falló en Módulo 2: {e}")
        raise HTTPException(status_code=500, detail="Error generando el documento. Intenta de nuevo en unos minutos.")

    try:
        save_programa_lead(email, 2, {"arquetipo": arquetipo[:100]})
    except Exception as e:
        print(f"Error registrando actividad M2: {e}")

    try:
        pdf_bytes = generate_pdf_branded(
            content=documento,
            titulo="Documento Maestro de Marca — Modulo 2",
            subtitulo="Programa Anti-Inercia de Marca Personal  |  yosoyelruso.com",
            nombre_archivo="documento-maestro-marca-modulo-2.pdf"
        )
        pdf_buffer = io.BytesIO(pdf_bytes)
        return StreamingResponse(
            pdf_buffer,
            media_type='application/pdf',
            headers={
                'Content-Disposition': 'attachment; filename="documento-maestro-marca-modulo-2.pdf"',
                'Content-Type': 'application/pdf'
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Error generando PDF: {str(e)}')


@app.post("/programa/modulo3/generar")
async def generar_modulo3(
    email: str = Form(...),
    # Parte 1 — Objetivos
    obj_visibilidad: str = Form(...),
    obj_visibilidad_medicion: str = Form(...),
    obj_credibilidad: str = Form(...),
    obj_credibilidad_evidencia: str = Form(...),
    obj_conversion: str = Form(...),
    obj_conversion_embudo: str = Form(...),
    # Parte 2 — Pilares de contenido
    pilar_a_tema: str = Form(...),
    pilar_a_titulos: str = Form(...),
    pilar_a_formato: str = Form(...),
    pilar_b_tema: str = Form(...),
    pilar_b_titulos: str = Form(...),
    pilar_b_evidencia: str = Form(...),
    pilar_c_tema: str = Form(...),
    pilar_c_titulos: str = Form(...),
    pilar_c_cta: str = Form(...),
    # Parte 3 — Sistema de conversión
    embudo_entrada: str = Form(...),
    embudo_siguiente: str = Form(...),
    embudo_cierre: str = Form(...),
    embudo_descripcion: str = Form(...),
    embudo_friccion: str = Form(...),
    embudo_solucion: str = Form(...),
    nivel_tecnologico: str = Form(...),
    herramientas_actuales: str = Form(...),
    herramienta_proxima: str = Form(...),
    # Parte 4 — Plan de implementación
    fase1_obstaculo: str = Form(...),
    fase1_necesita: str = Form(...),
    fase2_frecuencia: str = Form(...),
    fase2_canal: str = Form(...),
    fase3_pieza: str = Form(...),
    # Parte 5 — Validación
    val_objetivos: str = Form(...),
    val_embudo: str = Form(...),
    val_fase1: str = Form(...),
    val_bloqueo: str = Form(default=""),
    # PDFs anteriores
    pdf_m0: UploadFile = File(...),
    pdf_m1: UploadFile = File(...),
    pdf_m2: UploadFile = File(...)
):
    if not check_email_access(email):
        raise HTTPException(status_code=403, detail="Este correo no tiene acceso al programa.")

    texto_m0 = extract_text_from_pdf(await pdf_m0.read())
    texto_m1 = extract_text_from_pdf(await pdf_m1.read())
    texto_m2 = extract_text_from_pdf(await pdf_m2.read())
    contexto_previo = f"=== MÓDULO 0 ===\n{texto_m0}\n\n=== MÓDULO 1 ===\n{texto_m1}\n\n=== MÓDULO 2 ===\n{texto_m2}"

    respuestas = {k: v for k, v in locals().items() if k not in ['email', 'pdf_m0', 'pdf_m1', 'pdf_m2', 'contexto_previo', 'texto_m0', 'texto_m1', 'texto_m2']}

    try:
        documento = generate_modulo3_gemini(respuestas, contexto_previo)
    except Exception as e:
        print(f"Gemini falló en Módulo 3: {e}")
        raise HTTPException(status_code=500, detail="Error generando el documento. Intenta de nuevo en unos minutos.")

    try:
        save_programa_lead(email, 3, {"canal_principal": fase2_canal, "frecuencia": fase2_frecuencia})
    except Exception as e:
        print(f"Error registrando actividad M3: {e}")

    try:
        pdf_bytes = generate_pdf_branded(
            content=documento,
            titulo="Propuesta de Estrategia — Modulo 3",
            subtitulo="Programa Anti-Inercia de Marca Personal  |  yosoyelruso.com",
            nombre_archivo="propuesta-estrategia-modulo-3.pdf"
        )
        pdf_buffer = io.BytesIO(pdf_bytes)
        return StreamingResponse(
            pdf_buffer,
            media_type='application/pdf',
            headers={
                'Content-Disposition': 'attachment; filename="propuesta-estrategia-modulo-3.pdf"',
                'Content-Type': 'application/pdf'
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Error generando PDF: {str(e)}')
