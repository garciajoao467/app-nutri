import os
import sys
import urllib.parse
from datetime import datetime, timedelta
import json # <-- Adicionado import faltante

import bcrypt
import google.generativeai as genai
import requests
import sqlalchemy
from fastapi import FastAPI, HTTPException
from sqlalchemy import (Column, DateTime, Float, ForeignKey, Integer, String,
                        create_engine, func)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from dotenv import load_dotenv # Para carregar variáveis de ambiente (secrets)

# ---- Prints de Debug Iniciais ----
print("DEBUG: Script main.py iniciado.")
print(f"DEBUG: Tentando carregar dotenv...")
# -----------------------------

# Carrega variáveis do arquivo .env (se existir) e do ambiente do Railway/outros
load_dotenv()

print("DEBUG: dotenv carregado (ou não encontrado, o que é normal no deploy).")
print("DEBUG: Tentando carregar secrets do ambiente...")

# --- 1. CONFIGURAÇÃO INICIAL E SECRETS ---
# Carrega TODAS as variáveis de ambiente PRIMEIRO
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
USDA_API_KEY = os.getenv('USDA_API_KEY') # <-- Carregado ANTES de ser usado
DB_PASSWORD = os.getenv('DB_PASSWORD')
DB_CONNECTION_TEMPLATE = os.getenv('DB_CONNECTION_TEMPLATE')
TEST_USER_ID = int(os.getenv('TEST_USER_ID', '1')) # Pega ID ou usa '1' como padrão

# ---- Prints de Debug DEPOIS de Carregar ----
print(f"DEBUG: GEMINI_API_KEY carregado? {'Sim' if GEMINI_API_KEY else 'NÃO'}")
print(f"DEBUG: USDA_API_KEY carregado? {'Sim' if USDA_API_KEY else 'NÃO'}") # <-- Agora funciona
print(f"DEBUG: DB_PASSWORD carregado? {'Sim' if DB_PASSWORD else 'NÃO'}")
print(f"DEBUG: DB_CONNECTION_TEMPLATE carregado? {'Sim' if DB_CONNECTION_TEMPLATE else 'NÃO'}")
# -------------------------------------------

# Validação inicial dos secrets essenciais
if not all([GEMINI_API_KEY, USDA_API_KEY, DB_PASSWORD, DB_CONNECTION_TEMPLATE]):
    missing = [k for k,v in {
        'GEMINI_API_KEY': GEMINI_API_KEY,
        'USDA_API_KEY': USDA_API_KEY,
        'DB_PASSWORD': DB_PASSWORD,
        'DB_CONNECTION_TEMPLATE': DB_CONNECTION_TEMPLATE
    }.items() if not v]
    error_msg = f"ERRO CRÍTICO: Secrets ausentes: {', '.join(missing)}. Verifique as 'Variables' no Railway."
    print(error_msg)
    sys.exit(error_msg)

print("DEBUG: Todos os secrets essenciais parecem estar carregados.")

# --- 2. CONFIGURAÇÃO DO BANCO DE DADOS ---
print("Configurando banco de dados...")
Base = declarative_base()
engine = None
SessionLocal = None

try:
    safe_password = urllib.parse.quote_plus(DB_PASSWORD)
    db_string = DB_CONNECTION_TEMPLATE.replace("[PASSWORD_PLACEHOLDER]", safe_password)

    if ":6543" not in db_string or "postgresql+psycopg" not in db_string:
        print("--- ALERTA: DB_CONNECTION_TEMPLATE NÃO PARECE CORRETO (Pooler + psycopg + porta 6543) ---")

    engine = create_engine(
        db_string,
        pool_size=1,
        max_overflow=0
    )

    # Testa a conexão
    with engine.connect() as connection:
        print("✅ Conexão inicial com o banco de dados bem-sucedida!")

    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    print("✅ Configuração do banco de dados concluída.")

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

    # Base.metadata.create_all(bind=engine) # Melhor rodar manualmente ou com Alembic

except Exception as e:
    print(f"--- ERRO GRAVE AO CONFIGURAR O BANCO DE DADOS ---")
    print(f"Erro: {e}")
    sys.exit("Falha na inicialização do banco de dados.")

# --- 3. CONFIGURAÇÃO DO GEMINI ---
print("Configurando Gemini...")
try:
    genai.configure(api_key=GEMINI_API_KEY)
    generation_config = {
        "temperature": 0.1, "top_p": 1, "top_k": 1,
        "max_output_tokens": 2048, "response_mime_type": "application/json",
    }
    model = genai.GenerativeModel(
        model_name="gemini-1.5-pro-latest", # <-- Corrigido nome do modelo
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
    print("✅ Modelo Gemini configurado.")
except Exception as e:
    print(f"--- ERRO AO CONFIGURAR O GEMINI ---")
    print(f"Erro: {e}")
    sys.exit("Falha na inicialização do Gemini.")

# --- 4. FUNÇÕES DE LÓGICA (DO COLAB) ---
# (Aqui estão as funções que você colou)
def extrair_alimentos_da_frase(frase):
    print("\n1. Processando sua refeição com o Gemini...")
    prompt_completo = prompt_template.format(frase_do_usuario=frase)
    try:
        response = model.generate_content(prompt_completo)
        return json.loads(response.text)
    except Exception as e:
        print(f"Ocorreu um erro ao chamar a API do Gemini: {e}")
        return None

def buscar_dados_nutricionais(item, usda_key):
    alimento_nome = item['alimento']
    quantidade = item['quantidade']
    unidade = item['unidade']

    print(f"2. Buscando dados de '{alimento_nome}' no USDA...")
    url = f"https://api.nal.usda.gov/fdc/v1/foods/search?api_key={usda_key}&query={alimento_nome}"

    try:
        response = requests.get(url) # Adicionar tratamento de erro SSL se necessário no futuro

        if response.status_code == 200:
            data = response.json()
            if data['foods']:
                primeiro_alimento = data['foods'][0]
                valores_base_100g = {}
                for nut in primeiro_alimento.get('foodNutrients', []):
                    if nut['nutrientName'] == 'Energy' and nut.get('unitName') == 'KCAL':
                        valores_base_100g['Calorias (Kcal)'] = nut.get('value', 0)
                    elif nut['nutrientName'] == 'Protein':
                        valores_base_100g['Proteínas (g)'] = nut.get('value', 0)
                    elif nut['nutrientName'] == 'Total lipid (fat)':
                        valores_base_100g['Gorduras (g)'] = nut.get('value', 0)
                    elif nut['nutrientName'] == 'Carbohydrate, by difference':
                        valores_base_100g['Carboidratos (g)'] = nut.get('value', 0)

                print(f"   -> Alimento encontrado: '{primeiro_alimento.get('description')}' (Valores base por 100g)")

                if unidade == 'grama':
                    print(f"   -> Calculando para {quantidade} gramas...")
                    fator = quantidade / 100.0
                    nutrientes_calculados = {}
                    for nome, valor in valores_base_100g.items():
                        nutrientes_calculados[nome] = round(valor * fator, 2)
                    return nutrientes_calculados
                else: # Inclui 'unidade' e outros casos não suportados
                    print(f"   -> Unidade '{unidade}' não suportada para cálculo. Retornando valores base (100g).")
                    return valores_base_100g # Retorna base se não for 'grama'
            else:
                print(f"   -> Alimento '{alimento_nome}' não encontrado no banco de dados do USDA.")
                return None
        else:
            print(f"   -> Erro ao buscar no USDA: Status {response.status_code}")
            return None
    except Exception as e:
        print(f"Ocorreu um erro ao chamar a API do USDA: {e}")
        return None

# --- 5. INICIALIZAÇÃO DO FASTAPI ---
print("Iniciando FastAPI app...")
app = FastAPI(title="API de Nutrição com IA")
print("✅ FastAPI app iniciado.")


# --- 6. ENDPOINTS DA API ---

@app.get("/")
async def root():
    """ Endpoint raiz para verificar se a API está online. """
    return {"message": "API de Nutrição com IA está funcionando!"}

# --- (PRÓXIMOS PASSOS: Adicionar endpoints /registrar_refeicao e /resumo-do-dia aqui) ---
