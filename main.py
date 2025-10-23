import os
import sys
import urllib.parse
from datetime import datetime, timedelta
import json
import logging # Para logs mais detalhados

import bcrypt
import google.generativeai as genai
import requests
import sqlalchemy
from fastapi import FastAPI, HTTPException, Depends # Depends adicionado
from sqlalchemy import (Column, DateTime, Float, ForeignKey, Integer, String,
                        create_engine, func)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session # Session adicionado
from dotenv import load_dotenv
from pydantic import BaseModel # Para definir modelos de dados de entrada

# Configuração básica de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

logger.info("Script main.py iniciado.")
logger.info("Tentando carregar dotenv...")
load_dotenv()
logger.info("dotenv carregado (ou não encontrado).")
logger.info("Tentando carregar secrets do ambiente...")

# --- 1. CONFIGURAÇÃO INICIAL E SECRETS ---
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
USDA_API_KEY = os.getenv('USDA_API_KEY')
DB_PASSWORD = os.getenv('DB_PASSWORD')
DB_CONNECTION_TEMPLATE = os.getenv('DB_CONNECTION_TEMPLATE')
TEST_USER_ID = int(os.getenv('TEST_USER_ID', '1'))

logger.info(f"GEMINI_API_KEY carregado? {'Sim' if GEMINI_API_KEY else 'NÃO'}")
logger.info(f"USDA_API_KEY carregado? {'Sim' if USDA_API_KEY else 'NÃO'}")
logger.info(f"DB_PASSWORD carregado? {'Sim' if DB_PASSWORD else 'NÃO'}")
logger.info(f"DB_CONNECTION_TEMPLATE carregado? {'Sim' if DB_CONNECTION_TEMPLATE else 'NÃO'}")

# Validação inicial dos secrets essenciais
if not all([GEMINI_API_KEY, USDA_API_KEY, DB_PASSWORD, DB_CONNECTION_TEMPLATE]):
    missing = [k for k,v in {
        'GEMINI_API_KEY': GEMINI_API_KEY, 'USDA_API_KEY': USDA_API_KEY,
        'DB_PASSWORD': DB_PASSWORD, 'DB_CONNECTION_TEMPLATE': DB_CONNECTION_TEMPLATE
    }.items() if not v]
    error_msg = f"ERRO CRÍTICO: Secrets ausentes: {', '.join(missing)}."
    logger.error(error_msg)
    sys.exit(error_msg)

logger.info("Todos os secrets essenciais parecem estar carregados.")

# --- 2. CONFIGURAÇÃO DO BANCO DE DADOS ---
logger.info("Configurando banco de dados...")
Base = declarative_base()
engine = None
SessionLocal = None

try:
    safe_password = urllib.parse.quote_plus(DB_PASSWORD)
    db_string = DB_CONNECTION_TEMPLATE.replace("[PASSWORD_PLACEHOLDER]", safe_password)

    if ":6543" not in db_string or "postgresql+psycopg" not in db_string:
         logger.warning("--- ALERTA: DB_CONNECTION_TEMPLATE NÃO PARECE CORRETO (Pooler + psycopg + porta 6543) ---")

    engine = create_engine(
        db_string,
        pool_size=1,
        max_overflow=0
    )
    # Testa a conexão
    with engine.connect() as connection:
        logger.info("✅ Conexão inicial com o banco de dados bem-sucedida!")

    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    logger.info("✅ Configuração do banco de dados concluída.")

    # Define as tabelas
    class Usuario(Base):
        __tablename__ = 'usuarios'
        id = Column(Integer, primary_key=True)
        email = Column(String, nullable=False, unique=True)
        senha_hash = Column(String, nullable=False)
        meta_calorias = Column(Float, default=2000.0)
        refeicoes = relationship("RefeicaoRegistrada", back_populates="usuario")

    class RefeicaoRegistrada(Base):
        __tablename__ = 'refeicoes_registradas'
        id = Column(Integer, primary_key=True)
        data = Column(DateTime, nullable=False, default=datetime.utcnow)
        tipo_refeicao = Column(String, nullable=False)
        total_calorias = Column(Float, default=0)
        total_proteinas = Column(Float, default=0)
        total_gorduras = Column(Float, default=0)
        total_carboidratos = Column(Float, default=0)
        usuario_id = Column(Integer, ForeignKey('usuarios.id'), nullable=False)
        usuario = relationship("Usuario", back_populates="refeicoes")

except Exception as e:
    logger.exception(f"--- ERRO GRAVE AO CONFIGURAR O BANCO DE DADOS ---")
    sys.exit("Falha na inicialização do banco de dados.")

# --- 3. CONFIGURAÇÃO DO GEMINI ---
logger.info("Configurando Gemini...")
try:
    genai.configure(api_key=GEMINI_API_KEY)
    generation_config = {
        "temperature": 0.1, "top_p": 1, "top_k": 1,
        "max_output_tokens": 2048, "response_mime_type": "application/json",
    }
    model = genai.GenerativeModel(
        model_name="gemini-1.5-pro-latest",
        generation_config=generation_config
    )
    prompt_template = """
Você é um assistente de nutrição especialista em extrair dados de texto.
Sua única função é analisar a frase do usuário e retornar uma lista de alimentos em formato JSON.

Regra 1: O JSON deve ter as chaves "alimento", "quantidade" e "unidade".
Regra 2: (Tradução) O nome do "alimento" deve ser traduzido para o inglês para ser compatível com o banco de dados USDA FoodData Central.
Regra 3: (Conversão de Unidade) Esta é a regra mais importante. Se a unidade for uma medida caseira como 'fatia', 'unidade', 'copo', 'xícara', 'colher de sopa', 'pequeno', 'médio', 'grande', etc., sua função é usar seu conhecimento para encontrar o peso médio em gramas para esse alimento e fazer a conversão.
O JSON final deve ter SEMPRE a "unidade" como "grama" e a "quantidade" como o peso total em gramas.
Se a unidade já for 'g', 'kg' ou 'mg', apenas converta para 'grama'.

Exemplo 1:
Frase do usuário: "2 fatias de pão integral e 1 banana média"
Sua resposta:
[
  {{
    "alimento": "whole wheat bread",
    "quantidade": 50,
    "unidade": "grama"
  }},
  {{
    "alimento": "banana",
    "quantidade": 120,
    "unidade": "grama"
  }}
]

Exemplo 2:
Frase do usuário: "150g de arroz e 1 colher de sopa de azeite"
Sua resposta:
[
  {{
    "alimento": "rice",
    "quantidade": 150,
    "unidade": "grama"
  }},
  {{
    "alimento": "olive oil",
    "quantidade": 15,
    "unidade": "grama"
  }}
]

Agora, analise a seguinte frase:
"{frase_do_usuario}"
"""
    logger.info("✅ Modelo Gemini configurado.")
except Exception as e:
    logger.exception(f"--- ERRO AO CONFIGURAR O GEMINI ---")
    sys.exit("Falha na inicialização do Gemini.")

# --- 4. FUNÇÕES DE LÓGICA (DO COLAB) ---
def extrair_alimentos_da_frase(frase: str):
    logger.info(f"1. Processando com Gemini: '{frase}'")
    if not frase: return None
    prompt_completo = prompt_template.format(frase_do_usuario=frase)
    try:
        response
