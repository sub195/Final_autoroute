# backend/config.py
import os
from dotenv import load_dotenv
from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings
from azure.storage.blob import BlobServiceClient
from azure.core.credentials import AzureKeyCredential
from azure.ai.translation.text import TextTranslationClient
from langchain_core.messages import SystemMessage, HumanMessage
# from sqlalchemy import create_engine, text
from sqlalchemy import create_engine, text
import urllib.parse
import certifi


load_dotenv()

import certifi, os
# Make Requests-based SDKs (incl. Azure) use a trusted CA bundle
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

# ↓ reduce retries & add timeouts so requests fail fast instead of hanging
LLM_REQUEST_TIMEOUT = 30  # seconds
LLM_MAX_RETRIES = 1       # fail fast on network issues


llm = AzureChatOpenAI(
    azure_deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME"),
    openai_api_version="2024-02-15-preview",
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    temperature=0,
    max_tokens=1000,
    timeout=LLM_REQUEST_TIMEOUT,
    max_retries=LLM_MAX_RETRIES,
)

embeddings_client = AzureOpenAIEmbeddings(
    azure_deployment=os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME"),
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    timeout=LLM_REQUEST_TIMEOUT,
    max_retries=LLM_MAX_RETRIES,
)



BLOB_CONNECTION_STRING = os.getenv("AZURE_BLOB_CONNECTION_STRING")
BLOB_CONTAINER_NAME = os.getenv("BLOB_CONTAINER_NAME")

blob_service_client = BlobServiceClient.from_connection_string(BLOB_CONNECTION_STRING)
container_client = blob_service_client.get_container_client(BLOB_CONTAINER_NAME)

translator_credential = AzureKeyCredential(os.getenv("AZURE_TRANSLATOR_KEY"))
translator_endpoint = os.getenv("AZURE_TRANSLATOR_ENDPOINT")
translator_region = os.getenv("AZURE_TRANSLATOR_REGION")

translator_client = TextTranslationClient(
    endpoint=translator_endpoint,
    credential=translator_credential,
    region=translator_region
)

# def translate_text(text: str, to_lang: str, from_lang: str = None):
#     """Translates text using Azure AI Translator. Fast-fail if same lang."""
#     if not text or not text.strip() or to_lang == from_lang:
#         return text
#     try:
#         body = [{'text': text}]
#         response = translator_client.translate(
#             body=body,
#             to_language=[to_lang],
#             from_language=from_lang
#         )
#         return response[0].translations[0].text
#     except Exception as e:
#         print(f"Translation failed: {e}")
#         return text

def translate_text(text: str, to_lang: str, from_lang: str = None) -> str:
    """
    Primary: Azure AI Translator (fast).
    Fallback: LLM-based translation via Azure OpenAI if translator fails.
    If languages are same or text empty, returns original text.
    """
    if not text or not text.strip():
        return text
    if from_lang and to_lang and from_lang.lower().split("-")[0] == to_lang.lower().split("-")[0]:
        return text

    # 1) Trying Azure Translator
    try:
        body = [{'text': text}]
        resp = translator_client.translate(
            body=body,
            to_language=[to_lang],
            from_language=from_lang
        )
        return resp[0].translations[0].text
    except Exception as e:
        print(f"Translation failed (Azure Translator): {e}")

    # 2) Fallback: Use the LLM to translate if Azure translate doesn't work
    try:
        system_msg = (
            "You are a precise translator. "
            f"Translate the user's message into '{to_lang}'. "
            "Return only the translation, no extra commentary."
        )
        prompt = [SystemMessage(content=system_msg), HumanMessage(content=text)]
        out = llm.invoke(prompt).content
        return out
    except Exception as e2:
        print(f"Translation fallback failed (LLM): {e2}")
        return text





_SQL_ENGINE = None

def get_sql_engine():
    """
    Returns a singleton SQLAlchemy Engine.
    Priority:
      1) SQLALCHEMY_URL (e.g., mssql+pytds://user:pass@server.database.windows.net:1433/db)
      2) AZURE_SQL_CONNECTION_STRING (ODBC Driver 18) -> converted to SQLAlchemy URL
    Special handling for pytds: enable TLS by passing certifi CA via connect_args['cafile'].
    """
    global _SQL_ENGINE
    if _SQL_ENGINE is not None:
        return _SQL_ENGINE

    raw_url = os.getenv("SQLALCHEMY_URL", "").strip()
    odbc = os.getenv("AZURE_SQL_CONNECTION_STRING", "").strip()

    if raw_url:
        url = raw_url
        connect_args = {}
        # If using pytds, we MUST pass cafile to enable TLS (Azure requires it)
        if raw_url.startswith("mssql+pytds://"):
            connect_args = {
                "cafile": certifi.where(),
                # If our corp proxy does SSL inspection and you need to bypass hostname check:
                # "validate_host": False,
            }
        _SQL_ENGINE = create_engine(url, pool_pre_ping=True, future=True, connect_args=connect_args)

    elif odbc:
        # Converting a raw ODBC string to SQLAlchemy URL
        # Example ODBC string:
        # Driver={ODBC Driver 18 for SQL Server};Server=tcp:server.database.windows.net,1433;Database=db;Uid=user;Pwd=pass;Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;
        sa_url = "mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(odbc)
        _SQL_ENGINE = create_engine(sa_url, pool_pre_ping=True, future=True)

    else:
        return None

    # quick probe
    try:
        with _SQL_ENGINE.connect() as c:
            c.execute(sa_text("SELECT 1"))
    except Exception as e:
        print(f"[SQL] Engine probe failed: {e}")
        _SQL_ENGINE = None
        return None

    print("[SQL] Engine initialized")
    return _SQL_ENGINE



