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


def generate_pdf_branded(content: str, titulo: str, subtitulo: str, nombre_archivo: str) -> bytes:
    """
    Genera un PDF con branding de la marca Anti-Inercia.
    Paleta: Azul marino #2C3E50, Naranja #FF8C42, Blanco, Gris claro.
    """
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

Tu tarea es generar el "Mapa de Fricciones" — el documento de diagnóstico del Módulo 0 — a partir de las respuestas del participante.

INSTRUCCIONES CRÍTICAS:
- Sé directo, analítico y sin adornos. Este no es un documento motivacional.
- Identifica patrones reales, no repitas las respuestas del usuario.
- El documento debe ser accionable: cada sección debe terminar con una implicación clara para la estrategia.
- Usa el tono de Fedor Sawoloka: directo, crítico, orientado a resultados. Sin rodeos.
- NO uses palabras como "calidad", "experiencia", "pasión" como fortalezas — si el usuario las usó, señálalo como una inercia.
- Formato: usa ## para secciones principales y ### para subsecciones.

ESTRUCTURA OBLIGATORIA DEL MAPA DE FRICCIONES:

## Diagnóstico de Punto Cero
Síntesis de dónde está parado el participante hoy. Máximo 3 párrafos. Sin suavizar la realidad.

## Radiografía del Negocio
Análisis de la situación actual: fuente de clientes, flujo, formalización de oferta. Qué está funcionando y qué no.

## Estado de la Presencia Digital
Qué tan visible y coherente es su presencia hoy. Brecha entre lo que existe y lo que necesita.

## Fricciones Identificadas
Las 3 fricciones principales que están frenando su avance. Cada una con:
- Nombre de la fricción
- Cómo se manifiesta
- Qué la está causando (raíz real, no síntoma)

## Nivel de Disposición para Ejecutar
Análisis honesto de su nivel de compromiso y capacidad de ejecución basado en sus respuestas. Incluye el score de disposición declarado y lo que implica.

## Implicaciones Estratégicas
Qué debe resolver primero antes de hablar de estrategia de contenido o posicionamiento. Las 3 prioridades en orden.

---
Documento generado por el Programa Anti-Inercia de Marca Personal | Metodología de Fedor Sawoloka | yosoyelruso.com

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
