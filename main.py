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
        model_name="gemini-2.5-pro",
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
        response = model.generate_content(prompt_completo)
        alimentos = json.loads(response.text)
        if not isinstance(alimentos, list):
            logger.error(f"Resposta inesperada do Gemini (não é lista): {alimentos}")
            return None
        for item in alimentos:
             if not all(key in item for key in ["alimento", "quantidade", "unidade"]):
                  logger.error(f"Item inválido na resposta do Gemini: {item}")
                  return None
        return alimentos
    except json.JSONDecodeError as e:
         logger.error(f"Erro ao decodificar JSON do Gemini: {e}. Resposta: {response.text}")
         return None
    except Exception as e:
        logger.exception(f"Ocorreu um erro ao chamar a API do Gemini: {e}")
        return None

# ----- FUNÇÃO CORRIGIDA -----
def buscar_dados_nutricionais(item: dict, usda_key: str):
    alimento_nome = item.get('alimento')
    quantidade = item.get('quantidade')
    unidade = item.get('unidade')

    if not all([alimento_nome, quantidade, unidade]):
         logger.warning(f"Item inválido para busca no USDA: {item}")
         return None

    logger.info(f"2. Buscando dados de '{alimento_nome}' no USDA...")
    url = f"https://api.nal.usda.gov/fdc/v1/foods/search?api_key={usda_key}&query={alimento_nome}"

    try: # Bloco try começa aqui
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        data = response.json()
        if data['foods']:
            primeiro_alimento = data['foods'][0]
            valores_base_100g = {}
            for nut in primeiro_alimento.get('foodNutrients', []):
                name = nut.get('nutrientName')
                unit = nut.get('unitName', '').upper()
                value = nut.get('value', 0)

                if name == 'Energy' and unit == 'KCAL':
                    valores_base_100g['Calorias (Kcal)'] = value
                elif name == 'Protein' and unit == 'G':
                     valores_base_100g['Proteínas (g)'] = value
                elif name == 'Total lipid (fat)' and unit == 'G':
                     valores_base_100g['Gorduras (g)'] = value
                elif name == 'Carbohydrate, by difference' and unit == 'G':
                     valores_base_100g['Carboidratos (g)'] = value

            logger.info(f"   -> Encontrado: '{primeiro_alimento.get('description')}' (Base 100g)")

            if unidade == 'grama':
                if quantidade <= 0: return None
                logger.info(f"   -> Calculando para {quantidade} gramas...")
                fator = quantidade / 100.0
                nutrientes_calculados = {k: round(v * fator, 2) for k, v in valores_base_100g.items()}
                return nutrientes_calculados
            else:
                logger.warning(f"   -> Unidade '{unidade}' não suportada. Retornando base 100g.")
                return valores_base_100g
        else:
            logger.warning(f"   -> Alimento '{alimento_nome}' não encontrado no USDA.")
            return None
    # ---- Bloco except CORRIGIDO ----
    except requests.exceptions.RequestException as e: # Captura erros de rede
         logger.error(f"Erro de rede ao chamar API do USDA: {e}")
         return None
    except Exception as e: # Captura outros erros inesperados
        logger.exception(f"Ocorreu um erro inesperado ao buscar dados no USDA: {e}")
        return None
    # -------------------------------
# ----- FIM DA FUNÇÃO CORRIGIDA -----

# --- FUNÇÃO PARA PEGAR SESSÃO DO BANCO ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- 5. MODELOS DE DADOS PARA A API (Pydantic) ---
class RefeicaoInput(BaseModel):
    frase_refeicao: str
    tipo_refeicao: str

class RefeicaoOutput(BaseModel):
     id: int
     mensagem: str
     total_calorias: float = 0
     total_proteinas: float = 0
     total_gorduras: float = 0
     total_carboidratos: float = 0

# --- 6. INICIALIZAÇÃO DO FASTAPI ---
logger.info("Iniciando FastAPI app...")
app = FastAPI(title="API de Nutrição com IA")
logger.info("✅ FastAPI app iniciado.")

# --- 7. ENDPOINTS DA API ---

@app.get("/")
async def root():
    logger.info("Endpoint '/' acessado.")
    return {"message": "API de Nutrição com IA está funcionando!"}

@app.post("/registrar-refeicao/", response_model=RefeicaoOutput)
async def registrar_refeicao(refeicao_input: RefeicaoInput, db: Session = Depends(get_db)):
    logger.info(f"Recebido pedido para registrar: {refeicao_input.tipo_refeicao} - '{refeicao_input.frase_refeicao}'")

    # 1. Extrair alimentos com Gemini
    dados_alimentos = extrair_alimentos_da_frase(refeicao_input.frase_refeicao)
    if not dados_alimentos:
        logger.error("Falha ao extrair alimentos com Gemini.")
        raise HTTPException(status_code=400, detail="Não foi possível processar a descrição da refeição com a IA.")

    # 2. Buscar dados nutricionais e calcular totais
    total_calorias, total_proteinas, total_gorduras, total_carboidratos = 0, 0, 0, 0
    itens_processados = 0
    for item in dados_alimentos:
        nutrientes = buscar_dados_nutricionais(item, USDA_API_KEY)
        if nutrientes:
            itens_processados += 1
            total_calorias += nutrientes.get('Calorias (Kcal)', 0)
            total_proteinas += nutrientes.get('Proteínas (g)', 0)
            total_gorduras += nutrientes.get('Gorduras (g)', 0)
            total_carboidratos += nutrientes.get('Carboidratos (g)', 0)

    if itens_processados == 0:
         logger.error("Nenhum item da refeição pôde ser encontrado no USDA.")
         raise HTTPException(status_code=404, detail="Nenhum dos alimentos descritos foi encontrado no banco de dados nutricional.")

    logger.info(f"Refeição calculada: Cal={total_calorias}, Prot={total_proteinas}, Gord={total_gorduras}, Carb={total_carboidratos}")

    # 3. Salvar no banco de dados
    try:
        nova_refeicao_db = RefeicaoRegistrada(
            data=datetime.utcnow(),
            tipo_refeicao=refeicao_input.tipo_refeicao,
            total_calorias=round(total_calorias, 2),
            total_proteinas=round(total_proteinas, 2),
            total_gorduras=round(total_gorduras, 2),
            total_carboidratos=round(total_carboidratos, 2),
            usuario_id=TEST_USER_ID
        )
        db.add(nova_refeicao_db)
        db.commit()
        db.refresh(nova_refeicao_db)

        logger.info(f"✅ Refeição salva no banco com ID: {nova_refeicao_db.id}")

        return RefeicaoOutput(
            id=nova_refeicao_db.id,
            mensagem="Refeição registrada com sucesso!",
            total_calorias=nova_refeicao_db.total_calorias,
            total_proteinas=nova_refeicao_db.total_proteinas,
            total_gorduras=nova_refeicao_db.total_gorduras,
            total_carboidratos=nova_refeicao_db.total_carboidratos
        )

    except Exception as e:
        logger.exception("Ocorreu um erro ao salvar a refeição no banco de dados.")
        db.rollback()
        raise HTTPException(status_code=500, detail="Erro interno ao salvar a refeição.")

# --- (PRÓXIMO PASSO: Adicionar endpoint /resumo-do-dia aqui) ---
