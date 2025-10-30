# main.py (v1.3 - Autenticação com JWT)

import os
import sys
import urllib.parse
from datetime import datetime, date, time, timedelta, timezone # timezone adicionado
import json
import logging

import bcrypt # Removido, vamos usar passlib
import google.generativeai as genai
import requests
import sqlalchemy
from fastapi import FastAPI, HTTPException, Depends, status # status adicionado
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import (Column, DateTime, Float, ForeignKey, Integer, String,
                        create_engine, func)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session
from dotenv import load_dotenv
from pydantic import BaseModel, EmailStr # EmailStr adicionado

# --- NOVAS IMPORTAÇÕES PARA AUTENTICAÇÃO ---
from passlib.context import CryptContext # Para hash de senhas
from jose import JWTError, jwt # Para JSON Web Tokens
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm # Para formulário de login

# --- Configuração básica de logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

logger.info("Script main.py iniciado.")
load_dotenv()
logger.info("Tentando carregar secrets do ambiente...")

# --- 1. CONFIGURAÇÃO INICIAL E SECRETS ---
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
USDA_API_KEY = os.getenv('USDA_API_KEY')
DB_PASSWORD = os.getenv('DB_PASSWORD')
DB_CONNECTION_TEMPLATE = os.getenv('DB_CONNECTION_TEMPLATE')
# TEST_USER_ID = int(os.getenv('TEST_USER_ID', '1')) # <-- NÃO VAMOS MAIS USAR ISSO

# --- NOVAS VARIÁVEIS DE AUTENTICAÇÃO ---
# Você DEVE adicionar estes dois como "Variables" no Railway
SECRET_KEY = os.getenv("SECRET_KEY") # Chave secreta para assinar os tokens
ALGORITHM = os.getenv("ALGORITHM", "HS256") # Algoritmo de assinatura
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60")) # Token expira em 60 min

# Validação dos secrets
if not all([GEMINI_API_KEY, USDA_API_KEY, DB_PASSWORD, DB_CONNECTION_TEMPLATE, SECRET_KEY]):
    missing = [k for k,v in {
        'GEMINI_API_KEY': GEMINI_API_KEY, 'USDA_API_KEY': USDA_API_KEY,
        'DB_PASSWORD': DB_PASSWORD, 'DB_CONNECTION_TEMPLATE': DB_CONNECTION_TEMPLATE,
        'SECRET_KEY': SECRET_KEY # <-- Novo secret obrigatório
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
    engine = create_engine(db_string, pool_size=1, max_overflow=0)
    with engine.connect() as connection:
        logger.info("✅ Conexão inicial com o banco de dados bem-sucedida!")
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    logger.info("✅ Configuração do banco de dados concluída.")

    # --- Definição das Tabelas (sem alteração) ---
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

    # Cria as tabelas se não existirem
    # (Em produção, usar migrações (Alembic), mas para nós isto é seguro)
    Base.metadata.create_all(bind=engine)
    logger.info("Tabelas 'usuarios' e 'refeicoes_registradas' verificadas/criadas.")

except Exception as e:
    logger.exception(f"--- ERRO GRAVE AO CONFIGURAR O BANCO DE DADOS ---")
    sys.exit("Falha na inicialização do banco de dados.")

# --- 3. CONFIGURAÇÃO DO GEMINI ---
# (Sem alterações)
logger.info("Configurando Gemini...")
try:
    genai.configure(api_key=GEMINI_API_KEY)
    generation_config = {
        "temperature": 0.1, "top_p": 1, "top_k": 1,
        "max_output_tokens": 2048, "response_mime_type": "application/json",
    }
    model = genai.GenerativeModel(
        model_name="gemini-2.5-pro", # Ou o modelo que funcionou para si
        generation_config=generation_config
    )
    prompt_template = """
(COLE O SEU PROMPT COMPLETO AQUI)
...
Agora, analise a seguinte frase:
"{frase_do_usuario}"
"""
    logger.info("✅ Modelo Gemini configurado.")
except Exception as e:
    logger.exception(f"--- ERRO AO CONFIGURAR O GEMINI ---")
    sys.exit("Falha na inicialização do Gemini.")

# --- 4. FUNÇÕES DE LÓGICA (DO COLAB) ---
# (Sem alterações nas funções `extrair_alimentos_da_frase` e `buscar_dados_nutricionais`)
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

def buscar_dados_nutricionais(item: dict, usda_key: str):
    alimento_nome = item.get('alimento')
    quantidade = item.get('quantidade')
    unidade = item.get('unidade')
    if not all([alimento_nome, quantidade, unidade]): return None
    logger.info(f"2. Buscando dados de '{alimento_nome}' no USDA...")
    url = f"https://api.nal.usda.gov/fdc/v1/foods/search?api_key={usda_key}&query={alimento_nome}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data['foods']:
            primeiro_alimento = data['foods'][0]
            valores_base_100g = {}
            for nut in primeiro_alimento.get('foodNutrients', []):
                name = nut.get('nutrientName'); unit = nut.get('unitName', '').upper(); value = nut.get('value', 0)
                if name == 'Energy' and unit == 'KCAL': valores_base_100g['Calorias (Kcal)'] = value
                elif name == 'Protein' and unit == 'G': valores_base_100g['Proteínas (g)'] = value
                elif name == 'Total lipid (fat)' and unit == 'G': valores_base_100g['Gorduras (g)'] = value
                elif name == 'Carbohydrate, by difference' and unit == 'G': valores_base_100g['Carboidratos (g)'] = value
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
    except requests.exceptions.RequestException as e:
         logger.error(f"Erro de rede ao chamar API do USDA: {e}")
         return None
    except Exception as e:
        logger.exception(f"Ocorreu um erro inesperado ao buscar dados no USDA: {e}")
        return None

# --- FUNÇÃO PARA PEGAR SESSÃO DO BANCO ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- 5. LÓGICA DE AUTENTICAÇÃO E SEGURANÇA ---
logger.info("Configurando lógica de autenticação...")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token") # Diz ao FastAPI que o token é esperado no endpoint /token

def verificar_senha(senha_plana: str, senha_hash: str) -> bool:
    """Verifica se a senha plana corresponde ao hash armazenado."""
    return pwd_context.verify(senha_plana, senha_hash)

def get_hash_senha(senha: str) -> str:
    """Gera um hash bcrypt para a senha."""
    return pwd_context.hash(senha)

def criar_token_acesso(data: dict, expires_delta: timedelta | None = None):
    """Cria um novo token JWT."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def get_usuario_por_email(db: Session, email: str):
    """Busca um usuário pelo email no banco."""
    return db.query(Usuario).filter(Usuario.email == email).first()

async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    """Dependência para proteger endpoints: decodifica o token e encontra o usuário."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Não foi possível validar as credenciais",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub") # "sub" (subject) é o email do usuário
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    
    usuario = get_usuario_por_email(db, email=email)
    if usuario is None:
        raise credentials_exception
    return usuario
logger.info("✅ Lógica de autenticação configurada.")

# --- 6. MODELOS DE DADOS PARA A API (Pydantic) ---
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

class ResumoDiaOutput(BaseModel):
    data: date
    meta_calorias: float
    total_calorias: float = 0
    total_proteinas: float = 0
    total_gorduras: float = 0
    total_carboidratos: float = 0
    calorias_restantes: float = 0

# --- NOVOS MODELOS DE AUTENTICAÇÃO ---
class UserCreate(BaseModel):
    """Modelo para receber dados de cadastro."""
    email: EmailStr
    password: str
    meta_calorias: float = 2000.0

class UserOutput(BaseModel):
    """Modelo para retornar dados do usuário (sem senha)."""
    id: int
    email: EmailStr
    meta_calorias: float
    class Config:
        orm_mode = True # No Pydantic v1, era orm_mode

class Token(BaseModel):
    """Modelo para retornar o token de acesso."""
    access_token: str
    token_type: str

class TokenData(BaseModel):
    email: str | None = None

# --- 7. INICIALIZAÇÃO DO FASTAPI ---
logger.info("Iniciando FastAPI app...")
app = FastAPI(title="API de Nutrição com IA")

# --- Configuração CORS ---
origins = ["*"] # Permite TUDO (idealmente restringir)
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
logger.info("✅ Middleware CORS configurado.")
logger.info("✅ FastAPI app iniciado.")

# --- 8. ENDPOINTS DA API ---

@app.get("/")
async def root():
    logger.info("Endpoint '/' acessado.")
    return {"message": "API de Nutrição com IA está funcionando!"}

# --- NOVO ENDPOINT DE CADASTRO ---
@app.post("/cadastrar/", response_model=UserOutput)
async def cadastrar_usuario(user: UserCreate, db: Session = Depends(get_db)):
    """Cria um novo usuário no banco de dados."""
    logger.info(f"Tentativa de cadastro para email: {user.email}")
    db_user = get_usuario_por_email(db, email=user.email)
    if db_user:
        logger.warning("Email já cadastrado.")
        raise HTTPException(status_code=400, detail="Email já cadastrado")
    
    hashed_password = get_hash_senha(user.password)
    novo_usuario = Usuario(
        email=user.email, 
        senha_hash=hashed_password, 
        meta_calorias=user.meta_calorias
    )
    db.add(novo_usuario)
    db.commit()
    db.refresh(novo_usuario)
    logger.info(f"Usuário {user.email} cadastrado com sucesso.")
    return novo_usuario

# --- NOVO ENDPOINT DE LOGIN (TOKEN) ---
@app.post("/token", response_model=Token)
async def login_para_token(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """Recebe email (no campo username) e senha para gerar um token JWT."""
    logger.info(f"Tentativa de login para: {form_data.username}")
    usuario = get_usuario_por_email(db, email=form_data.username)
    if not usuario or not verificar_senha(form_data.password, usuario.senha_hash):
        logger.warning("Falha no login: email ou senha inválidos.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email ou senha incorretos",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = criar_token_acesso(
        data={"sub": usuario.email}, expires_delta=access_token_expires
    )
    logger.info(f"Login bem-sucedido para {usuario.email}. Token gerado.")
    return {"access_token": access_token, "token_type": "bearer"}

# --- ENDPOINT ATUALIZADO (PROTEGIDO) ---
@app.post("/registrar-refeicao/", response_model=RefeicaoOutput)
async def registrar_refeicao(
    refeicao_input: RefeicaoInput, 
    db: Session = Depends(get_db),
    usuario_atual: Usuario = Depends(get_current_user) # <-- Proteção!
):
    """Recebe a descrição de uma refeição, processa e salva no banco DO USUÁRIO LOGADO."""
    logger.info(f"Usuário {usuario_atual.email} registrando refeição: '{refeicao_input.frase_refeicao}'")
    
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

    logger.info(f"Refeição calculada: Cal={total_calorias}")

    # 3. Salvar no banco de dados
    try:
        nova_refeicao_db = RefeicaoRegistrada(
            data=datetime.utcnow(),
            tipo_refeicao=refeicao_input.tipo_refeicao,
            total_calorias=round(total_calorias, 2),
            total_proteinas=round(total_proteinas, 2),
            total_gorduras=round(total_gorduras, 2),
            total_carboidratos=round(total_carboidratos, 2),
            usuario_id=usuario_atual.id # <-- Usa o ID do usuário logado!
        )
        db.add(nova_refeicao_db)
        db.commit()
        db.refresh(nova_refeicao_db)
        logger.info(f"✅ Refeição salva (ID: {nova_refeicao_db.id}) para o usuário {usuario_atual.email}")
        
        return RefeicaoOutput(
            id=nova_refeicao_db.id, mensagem="Refeição registrada com sucesso!",
            total_calorias=nova_refeicao_db.total_calorias, total_proteinas=nova_refeicao_db.total_proteinas,
            total_gorduras=nova_refeicao_db.total_gorduras, total_carboidratos=nova_refeicao_db.total_carboidratos
        )
    except Exception as e:
        logger.exception("Ocorreu um erro ao salvar a refeição no banco de dados.")
        db.rollback()
        raise HTTPException(status_code=500, detail="Erro interno ao salvar a refeição.")

# --- ENDPOINT ATUALIZADO (PROTEGIDO) ---
@app.get("/resumo-do-dia/", response_model=ResumoDiaOutput)
async def get_resumo_do_dia(
    db: Session = Depends(get_db),
    usuario_atual: Usuario = Depends(get_current_user) # <-- Proteção!
):
    """Calcula e retorna o resumo nutricional total para o dia atual (UTC) DO USUÁRIO LOGADO."""
    logger.info(f"Recebido pedido de resumo do dia para o usuário: {usuario_atual.email}")

    try:
        meta_calorias = usuario_atual.meta_calorias
        logger.info(f"Meta de calorias do usuário: {meta_calorias} Kcal")
        
        hoje_utc = datetime.utcnow().date()
        inicio_do_dia_utc = datetime.combine(hoje_utc, time.min)
        fim_do_dia_utc = inicio_do_dia_utc + timedelta(days=1)
        logger.info(f"Buscando registros entre {inicio_do_dia_utc} e {fim_do_dia_utc} (UTC)...")
        
        resumo_query = db.query(
            func.sum(RefeicaoRegistrada.total_calorias).label("total_cal"),
            func.sum(RefeicaoRegistrada.total_proteinas).label("total_prot"),
            func.sum(RefeicaoRegistrada.total_gorduras).label("total_gord"),
            func.sum(RefeicaoRegistrada.total_carboidratos).label("total_carb")
        ).filter(
            RefeicaoRegistrada.usuario_id == usuario_atual.id, # <-- Usa o ID do usuário logado!
            RefeicaoRegistrada.data >= inicio_do_dia_utc,
            RefeicaoRegistrada.data < fim_do_dia_utc
        ).first()
        
        total_cal = resumo_query.total_cal or 0
        total_prot = resumo_query.total_prot or 0
        total_gord = resumo_query.total_gord or 0
        total_carb = resumo_query.total_carb or 0
        calorias_restantes = meta_calorias - total_cal
        
        logger.info(f"Resumo do dia calculado: Cal={total_cal}")
        
        return ResumoDiaOutput(
            data=hoje_utc, meta_calorias=meta_calorias,
            total_calorias=round(total_cal, 2), total_proteinas=round(total_prot, 2),
            total_gorduras=round(total_gord, 2), total_carboidratos=round(total_carb, 2),
            calorias_restantes=round(calorias_restantes, 2)
        )
    except Exception as e:
        logger.exception("Ocorreu um erro ao calcular o resumo do dia.")
        raise HTTPException(status_code=500, detail="Erro interno ao calcular o resumo do dia.")
