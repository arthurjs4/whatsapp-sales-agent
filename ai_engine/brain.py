"""
Cérebro do agente de vendas.

Recebe as conversas do WhatsApp (via whatsapp_bridge), entende em que ponto do
fluxo de vendas cada lead está e mantém o HubSpot organizado:

  Prospecção enviada -> Follow-up -> Em conversa -> Loom aceito (gravar) ->
  Loom enviado -> Reunião agendada -> Reunião feita -> Fechado / Perdido

O lead pode pular etapas (ex.: aceitar o Loom na 1ª mensagem). Follow-ups só
contam enquanto o lead não respondeu.

Rotas:
  POST /analisar     analisa uma conversa e atualiza o CRM
  POST /transcrever  transcreve um áudio do WhatsApp
  GET  /pendencias   o que a equipe precisa fazer agora (para o grupo do WhatsApp)
  GET  /saude        status do cérebro, Ollama e HubSpot
"""
import base64
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request

import ia
from hubspot_setup import CODIGOS_ANTIGOS, ETAPAS, PIPELINE_NOME

AQUI = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(AQUI, ".env"))

app = Flask(__name__)

# ------------------------------------------------------------------ configuração
HUBSPOT_TOKEN = os.environ["HUBSPOT_ACCESS_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
ATUALIZAR_HUBSPOT = os.environ.get("ATUALIZAR_HUBSPOT", "0") == "1"
MODEL_NAME = os.environ.get("OLLAMA_MODEL", "qwen3:14b")
WHISPER_MODELO = os.environ.get("WHISPER_MODELO", "small")
FU_DIAS = float(os.environ.get("FOLLOWUP_DIAS", "2"))          # dias entre follow-ups
MAX_FOLLOWUPS = int(os.environ.get("MAX_FOLLOWUPS", "2"))       # depois disso, encerra
RESPONDER_MIN = float(os.environ.get("RESPONDER_MINUTOS", "30"))  # lead esperando resposta
FUSO_REUNIOES = ZoneInfo(os.environ.get("FUSO_REUNIOES", "America/New_York"))

OLLAMA = "http://localhost:11434"
API = "https://api.hubapi.com"
HS_HEADERS = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
DADOS = os.path.join(os.path.dirname(AQUI), "data_storage")

CODIGOS = [cod for _, cod, _, _ in ETAPAS]
ROTULO = {cod: rot for rot, cod, _, _ in ETAPAS}
ORDEM = {cod: i for i, cod in enumerate(CODIGOS)}
DEGRAU = dict(ORDEM, AGUARDANDO_RETORNO=ORDEM["EM_CONVERSA"])  # mesmo degrau: só muda de quem é a vez
FINAIS = {"FECHADO_GANHO", "PERDIDO"}
SEM_RESPOSTA = {"PROSPECCAO", "FOLLOWUP"}
MOTIVOS_PERDA = {
    "preco": "Achou caro", "fechou_com_outro": "Fechou com outra empresa",
    "sem_resposta": "Lead Frio: sem resposta aos follow-ups (retomar em 60 dias)",
    "sem_interesse": "Sem interesse", "outro": "Outro motivo",
    "numero_errado": "Número não atendido (achar outro contato)",
    "nao_e_lead": "Não é lead (conversa pessoal)",
}
FRIO_RETOMAR_DIAS = float(os.environ.get("FRIO_RETOMAR_DIAS", "60"))
LOOM_FU_DIAS = [1, 4, 10]  # playbook, Etapa 5: follow-ups depois do Loom

# Regras do playbook que valem para sugestões de resposta e para o chat do grupo
PLAYBOOK = """Regras do playbook (exemplo genérico, adapte ao seu negócio):
- Prospecção (Dia 1) termina numa pergunta de sim/não oferecendo um vídeo curto. Templates: A (elogio de avaliação
  citando funcionário) e B (curiosidade: "pesquisei [serviço] em [cidade]...").
- Sem resposta: follow-up no Dia 3 e Dia 5 (o último, curto e sem pressão). Depois: lead Frio, retomar em 60 dias
  com uma nova observação.
- "Não tenho interesse": agradecer em uma linha e encerrar, sem insistir.
- Pediu o vídeo: reservar a conversa na hora, com datas nos próximos 2-3 dias úteis. Prefere ao vivo: pula o vídeo.
- Vídeo: no máximo 4 min. Envio com o ponto principal e duas opções de horário para conversar 15 min.
- Depois do vídeo: Dia +1 (perguntar se conseguiu ver), Dia +4 (perguntar se faz sentido agora) e Dia +10
  (encerramento, só se for verdade).
- Lembrete 1h antes da conversa com o link.
- Conversa de 15 min: perguntas sobre o valor do cliente, proposta (valores definidos por você), relatório periódico.
  Não fechou: combinar data e hora exatas.
- Evite jargão técnico com o lead. Chame de "conversa de 15 minutos", nunca de "reunião". Nunca prometer número de
  clientes ou retorno. Nunca inventar informação do site/perfil."""
STATUS_LEAD = {
    "PROSPECCAO": "ATTEMPTED_TO_CONTACT", "FOLLOWUP": "ATTEMPTED_TO_CONTACT",
    "EM_CONVERSA": "IN_PROGRESS", "AGUARDANDO_RETORNO": "IN_PROGRESS", "LOOM_ACEITO": "IN_PROGRESS",
    "LOOM_ENVIADO": "IN_PROGRESS", "REUNIAO_AGENDADA": "OPEN_DEAL", "REUNIAO_FEITA": "OPEN_DEAL",
    "PROPOSTA": "OPEN_DEAL", "PERDIDO": "UNQUALIFIED",
}


def campos_navegacao(a: dict) -> dict:
    """De quem é a vez, situação e próximo contato: os campos que facilitam achar o que fazer no HubSpot."""
    props = {}
    if a.get("vez_de") in ("agencia", "lead", "ninguem"):
        props["vez_de"] = a["vez_de"]
    if a.get("situacao"):
        props["situacao_lead"] = a["situacao"][:250]
    quando = a.get("proximo_contato_ts")
    props["proximo_contato"] = (datetime.fromtimestamp(quando, timezone.utc).strftime("%Y-%m-%d")
                                if quando else "")
    return props
PALAVRAS_PROSPECCAO = re.compile(
    r"an[uú]nci|google|v[ií]deo|loom|marketing|tr[aá]fego|clientes|site|aparece|pesquis", re.IGNORECASE)


def agora() -> float:
    return time.time()


def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ registro local
# telefone -> tudo que o agente sabe do lead (estágio, ids no HubSpot, datas do fluxo).
# Serve de memória, evita duplicar contatos e alimenta os alertas do grupo.
REGISTRO_ARQ = os.path.join(DADOS, "leads.json")
_reg_lock = threading.RLock()


def _ler_registro() -> dict:
    try:
        with open(REGISTRO_ARQ, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _gravar_registro(dados: dict):
    os.makedirs(DADOS, exist_ok=True)
    tmp = REGISTRO_ARQ + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=1)
    os.replace(tmp, REGISTRO_ARQ)


def registro_get(telefone: str) -> dict:
    with _reg_lock:
        r = dict(_ler_registro().get(telefone, {}))
    if r.get("estagio") in CODIGOS_ANTIGOS:
        r["estagio"] = CODIGOS_ANTIGOS[r["estagio"]]
    return r


def registro_set(telefone: str, **valores):
    with _reg_lock:
        dados = _ler_registro()
        dados.setdefault(telefone, {}).update(valores)
        _gravar_registro(dados)


def registro_del(telefone: str):
    with _reg_lock:
        dados = _ler_registro()
        if dados.pop(telefone, None) is not None:
            _gravar_registro(dados)


def registro_todos() -> dict:
    with _reg_lock:
        return _ler_registro()


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def lock_do(telefone: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(telefone, threading.Lock())


# ------------------------------------------------------------------ transcrição de áudio
TRANSCRICOES_ARQ = os.path.join(DADOS, "transcricoes.json")
_whisper = None
_whisper_lock = threading.RLock()  # reentrante: transcrever_audio e _modelo_whisper usam o mesmo


def _modelo_whisper():
    global _whisper
    with _whisper_lock:
        if _whisper is None:
            from faster_whisper import WhisperModel
            # Processador: ~3s para 20s de áudio. (A placa de vídeo precisaria de bibliotecas
            # extras da NVIDIA e, sem elas, trava em vez de dar erro.)
            try:
                _whisper = WhisperModel(WHISPER_MODELO, device="cpu", compute_type="int8", local_files_only=True)
            except Exception:
                _whisper = WhisperModel(WHISPER_MODELO, device="cpu", compute_type="int8")  # baixa na 1ª vez
            print(f"Transcrição de áudio pronta (modelo '{WHISPER_MODELO}')")
        return _whisper


def transcrever_audio(dados: bytes) -> str:
    import numpy as np
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg não encontrado")
    pcm = subprocess.run([ffmpeg, "-v", "error", "-i", "pipe:0", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
                         input=dados, capture_output=True, check=True).stdout
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    with _whisper_lock:
        modelo = _modelo_whisper()
        segmentos, _ = modelo.transcribe(audio, beam_size=1, vad_filter=True)
        return " ".join(s.text.strip() for s in segmentos).strip()


def _transcricoes() -> dict:
    try:
        with open(TRANSCRICOES_ARQ, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ------------------------------------------------------------------ análise da conversa
PROMPT = """Você analisa uma conversa de WhatsApp entre a AGÊNCIA (Ana e Bruno, Google Ads para negócios
brasileiros nos EUA) e um possível Cliente. Hoje é {hoje}.

Como a agência vende:
1) Manda uma mensagem de prospecção citando algo real do negócio e OFERECE gravar um vídeo (Loom) de ~4 min
   mostrando o que aparece hoje no Google. 2) Se o Cliente aceita, a agência grava e envia o Loom. 3) O Loom convida
   para uma conversa de 15 min. 4) Na conversa (ou por escrito) vai a proposta e eles fecham o contrato.
Se o Cliente não responde, a agência faz até 2 follow-ups e encerra (a última costuma dizer "última mensagem").

{playbook}

COMO LER A CONVERSA (muito importante):
- Linhas "Cliente [AUTOMÁTICA]" são respostas automáticas, robôs ou cardápios do WhatsApp Business. NÃO são o
  Cliente respondendo: ignore-as para decidir interesse, aceite ou recusa.
- Atendente virtual/robô que só agradece, manda cardápio ou diz "vou encaminhar" também não é o dono decidindo.
- "Ok", "sim", "pode", "pode mandar", "quero" logo depois de a Agência oferecer o vídeo = o Cliente ACEITOU o vídeo.
- "Obrigado", "beleza", "👍", "tudo bem" sozinhos são só cortesia: não é interesse, nem aceite, nem recusa.
- "Vou analisar", "vou ver com meu sócio/gerente/a Marta", "encaminhei para o setor responsável", "te retorno",
  "se aprovar te mando o contato" = o Cliente vai dar retorno: a vez é DELE, a Agência só aguarda.
- "Já temos uma empresa/equipe que faz isso", "não estamos precisando", "não temos interesse" = recusou.
- Olhe as DATAS: se a Agência já respondeu depois da última pergunta do Cliente, não falta resposta da Agência.

{memoria}Responda SOMENTE com um JSON. Primeiro os textos (OBRIGATÓRIOS, nunca vazios):
- "resumo": 2 a 3 frases sobre quem é o Cliente e em que ponto está
- "situacao": 1 frase curta (até 90 caracteres) dizendo em que pé está e de quem é a vez. Ex.: "Vai falar com a
  Marta e mandar o contato: aguardar", "Proposta enviada por escrito, gerente vai analisar", "Só o robô respondeu"
- "proxima_acao": próximo passo da agência segundo o playbook (curto)
- "sugestao_resposta": a mensagem que a agência deve mandar quando for a hora, seguindo os textos do playbook,
  com os dados reais da conversa (nome, cidade, serviço), no idioma do Cliente

Depois true/false, com base no que está ESCRITO:
- "assunto_pessoal": conversa pessoal (família, amigos) ou INTERNA da agência (sócio/equipe trocando links, senhas,
  roteiros, sites, tarefas), ou seja, NÃO é a agência vendendo para um negócio?
- "so_resposta_automatica": TODAS as mensagens do lado do Cliente são automáticas/robôs (nenhuma pessoa respondeu)?
- "cliente_pediu_loom": o Cliente aceitou ou pediu o vídeo/análise oferecido pela agência?
- "loom_enviado": a Agência já enviou o vídeo, Loom ou link do vídeo?
- "reuniao_combinada": combinaram dia/horário de conversa, call ou ligação (um propõe e o outro confirma)?
- "reuniao_aconteceu": a conversa/call já aconteceu (ex.: "foi ótima a call de hoje", "como conversamos ontem")?
- "proposta_enviada": a Agência mandou proposta com valores por escrito ou em documento?
- "cliente_aceitou": o Cliente aceitou CONTRATAR, pediu contrato ou forma de pagamento, assinou ou pagou?
- "cliente_recusou": o Cliente disse que não quer, não tem interesse, não está precisando ou já tem outra empresa?
- "lead_vai_retornar": o Cliente disse que vai analisar, pensar, consultar alguém, encaminhou para outra pessoa ou
  setor, ou prometeu mandar algo, e ainda NÃO voltou com a resposta?
- "falta_resposta_nossa": a última mensagem do Cliente (não automática) tem pergunta, pedido ou proposta que a
  Agência AINDA NÃO respondeu? (false se a Agência já respondeu depois, ou se o Cliente só agradeceu/confirmou)

Depois (só se aparecer na conversa; nunca invente; senão ""):
- "temperatura": "quente" (quer avançar), "morno" (interessado sem pressa) ou "frio" (pouco interesse)
- "score": número de 0 a 100 com a chance de fechar
- "nome" (da PESSOA que decide, não da empresa), "email", "empresa", "cidade"
- "tipo_negocio": ramo do Cliente (ex.: "contabilidade", "restaurante")
- "orcamento_anuncios": quanto o CLIENTE quer investir em anúncios
- "valor_proposta": valor mensal que a AGÊNCIA propôs, só o número (ex.: 800)
- "data_reuniao": quando a conversa foi marcada, por extenso (ex.: "quinta às 15h")
- "data_reuniao_iso": a mesma data no formato AAAA-MM-DDTHH:MM, calculada a partir de hoje
- "motivo_perda": se recusou: preco, fechou_com_outro, sem_interesse ou outro

Formato:
{{"resumo": "", "situacao": "", "proxima_acao": "", "sugestao_resposta": "", "assunto_pessoal": false,
"so_resposta_automatica": false, "cliente_pediu_loom": false, "loom_enviado": false, "reuniao_combinada": false,
"reuniao_aconteceu": false, "proposta_enviada": false, "cliente_aceitou": false, "cliente_recusou": false,
"lead_vai_retornar": false, "falta_resposta_nossa": false, "temperatura": "", "score": 0, "nome": "", "email": "",
"empresa": "", "cidade": "", "tipo_negocio": "", "orcamento_anuncios": "", "valor_proposta": "", "data_reuniao": "",
"data_reuniao_iso": "", "motivo_perda": ""}}

Conversa (mais antiga primeiro; entre parênteses, quanto tempo passou desde a mensagem anterior):
{conversa}
"""

FATOS = ["assunto_pessoal", "so_resposta_automatica", "cliente_pediu_loom", "loom_enviado", "reuniao_combinada",
         "reuniao_aconteceu", "proposta_enviada", "cliente_aceitou", "cliente_recusou", "lead_vai_retornar",
         "falta_resposta_nossa"]
TEXTOS = ["resumo", "situacao", "proxima_acao", "sugestao_resposta", "temperatura", "nome", "email", "empresa",
          "cidade", "tipo_negocio", "orcamento_anuncios", "valor_proposta", "data_reuniao", "data_reuniao_iso",
          "motivo_perda"]
LINK_LOOM = re.compile(r"loom\.com/|\[v[ií]deo\]", re.IGNORECASE)
COBRANCA = re.compile(r"contrato|link de pagamento|pagamento|invoice|payment|stripe|pix|boleto", re.IGNORECASE)
CONFIRMOU = re.compile(r"\bpago\b|paguei|pagamento feito|assinei|assinado|quando come[çc]amos|pode come[çc]ar|"
                       r"\bpaid\b|signed|let'?s start", re.IGNORECASE)

# ---- leitura de contexto por regras (não dependem da IA)
# Respostas automáticas / robôs do WhatsApp Business (saudação, ausência, cardápio, "um atendente já vai falar")
AUTOMATICA = re.compile(
    r"thanks? (you )?for (contacting|reaching out|your message)|let us know how we can help|will be with you|"
    r"\brepresentative\b|get back to you|first opportunity|unavailable right now|messages only|order online|"
    r"opening hours|business hours|"
    r"obrigad[oa] (por|pelo|pela) (entrar em |seu |sua )?(contato|mensagem|chamar)|"
    r"agradece(mos)? (o |pelo |pela |a |por )?(seu |sua )?(contato|mensagem)|seja(m)? (super )?bem[- ]?vind|"
    r"em breve,? (iremos|retornaremos|responderemos|entraremos|vamos)|(como|em que) podemos (te |lhe )?ajudar|"
    r"hor[áa]rios? de (atendimento|funcionamento)|n[ãa]o estamos dispon[íi]veis|n[ãa]o aceitamos chamadas|"
    r"fa[çc]a (o )?(seu|sua) (pedido|ordem)|pronta entrega|mensagens? (e lembretes )?autom[áa]tic|"
    r"n[ãa]o (é|e|est[áa]) monitorad|n[ãa]o realizamos atendimento", re.IGNORECASE)
NAO_MONITORADO = re.compile(r"n[ãa]o (é|e|est[áa]) monitorad|n[ãa]o realizamos atendimento|not monitored|"
                            r"exclusivamente para (o )?envio", re.IGNORECASE)
RECUSA = re.compile(r"n[ãa]o (temos|tenho) interesse|n[ãa]o (estamos|estou) (interessad|necessitando|precisando)|"
                    r"not interested|j[áa] (temos|tenho|trabalhamos com)|empresa parceira|already (have|work)",
                    re.IGNORECASE)
# Só cortesia: "obrigado", "beleza 👍", "tudo bem", "valeu"...
CORTESIA = re.compile(
    r"((oi|ol[aá]|opa|bom dia|boa tarde|boa noite|ok|okay|certo|beleza|blz|show|top|perfeito|entendi|ata|"
    r"(muito )?(obrigad[oa]|obg|valeu|vlw)( (pelas? dicas?|pela mensagem|pelo contato|pela aten[çc][ãa]o))?|"
    r"(eu )?te agrade[çc]o|agrade[çc]o|thanks?( you)?|thank you|por nada|de nada|igualmente|sucesso|"
    r"tudo (bem|certo|ok)|kk+|am[ée]m)\s?)+")
# Aceite curto ("ok", "pode", "sim") logo depois de a agência oferecer o vídeo
ACEITE = re.compile(r"^((oi|ol[aá]|opa|bom dia|boa tarde|boa noite)\s)*(ok(ay)?|sim|pode|claro|quero|manda|yes|sure|"
                    r"please|por favor|com certeza|bora|vamos|pode ser)\b")
OFERTA_VIDEO = re.compile(r"v[íi]deo|video|loom", re.IGNORECASE)
NEGACAO = re.compile(r"\bn[ãa]o\b|\bno\b|\bnot\b|\bnunca\b", re.IGNORECASE)
# Follow-up de encerramento da agência ("última mensagem, prometo")
ENCERRAMENTO = re.compile(r"[úu]ltima (mensagem|que (eu )?mando|vez)|vou encerrar|encerrar por aqui|"
                          r"pra n[ãa]o (ficar )?insistir|sem insist[êe]ncia|fico por aqui", re.IGNORECASE)


def _limpo(texto: str) -> str:
    t = re.sub(r"[^\w\sáéíóúâêôãõçà]", " ", (texto or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def e_cortesia(texto: str) -> bool:
    t = _limpo(texto)
    return not t or bool(CORTESIA.fullmatch(t))


def normalizar_mensagens(mensagens: list[dict]) -> list[dict]:
    limpas = [{"de": "agencia" if m.get("de") == "agencia" else "cliente",
               "texto": str(m.get("texto") or "").strip(), "quando": int(m.get("quando") or 0)}
              for m in mensagens if str(m.get("texto") or "").strip()]
    limpas.sort(key=lambda m: m["quando"])
    marcar_automaticas(limpas)
    return limpas


def marcar_automaticas(msgs: list[dict]):
    """Marca m['auto']=True nas respostas automáticas/robôs do lado do lead (não contam como resposta)."""
    ultima_agencia = None
    vistos = set()
    for m in msgs:
        if m["de"] == "agencia":
            ultima_agencia = m["quando"]
            m["auto"] = False
            continue
        t = m["texto"]
        atraso = m["quando"] - ultima_agencia if ultima_agencia else None
        repetida = len(t) > 30 and t in vistos
        modelo = bool(AUTOMATICA.search(t)) and not RECUSA.search(t)
        m["auto"] = repetida or (modelo and (atraso is None or atraso <= 600))
        vistos.add(t)


NAO_SAO_LEADS_ARQ = os.path.join(DADOS, "nao_sao_leads.json")


def nao_sao_leads() -> set[str]:
    """Números que nunca são leads (sócios, equipe, família). Edite data_storage/nao_sao_leads.json."""
    try:
        with open(NAO_SAO_LEADS_ARQ, encoding="utf-8") as f:
            return {re.sub(r"\D", "", str(n)) for n in json.load(f).get("numeros", [])}
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def humanas(msgs: list[dict]) -> list[dict]:
    """Mensagens que contam: todas da agência + as do lead que não são automáticas."""
    return [m for m in msgs if m["de"] == "agencia" or not m.get("auto")]


def contar_toques(msgs: list[dict]) -> tuple[int, float]:
    """Envios da agência: mensagens separadas por mais de 12h contam como um novo toque."""
    toques, ultimo = 0, 0.0
    for m in msgs:
        if m["de"] == "agencia":
            if not ultimo or m["quando"] - ultimo > 12 * 3600:
                toques += 1
            ultimo = m["quando"]
    return toques, ultimo


def aceitou_video_curto(msgs: list[dict]) -> bool:
    """'Ok' / 'Pode' / 'Sim' logo depois de a agência oferecer o vídeo = aceitou o vídeo (não depende da IA)."""
    ultima_agencia = None
    for m in humanas(msgs):
        if m["de"] == "agencia":
            ultima_agencia = m
            continue
        t = _limpo(m["texto"])
        if (ultima_agencia and OFERTA_VIDEO.search(ultima_agencia["texto"]) and "?" in ultima_agencia["texto"]
                and len(t) <= 60 and ACEITE.match(t) and not NEGACAO.search(t)):
            return True
    return False


def rastreio_playbook(msgs: list[dict]) -> dict:
    """Fatos do playbook tirados direto das mensagens (sem IA)."""
    msgs = humanas(msgs)
    primeira = next((m for m in msgs if m["de"] == "agencia"), None)
    template = ""
    if primeira and msgs[0]["de"] == "agencia":
        t = primeira["texto"].lower()
        if "avalia" in t or "review" in t:
            template = "A"
        elif "pesquisei" in t or "searched" in t:
            template = "B"
    loom = next((m for m in msgs if m["de"] == "agencia" and LINK_LOOM.search(m["texto"])), None)
    r = {"template": template, "primeiro_ts": msgs[0]["quando"] if msgs else 0,
         "loom_enviado_ts": loom["quando"] if loom else 0, "toques_pos_loom": 0, "cliente_pos_loom": False}
    if loom:
        depois = [m for m in msgs if m["quando"] > loom["quando"]]
        r["cliente_pos_loom"] = any(m["de"] == "cliente" for m in depois)
        ultimo_cliente = max((m["quando"] for m in depois if m["de"] == "cliente"), default=0)
        # toques da agência depois do Loom que ainda não tiveram resposta
        r["toques_pos_loom"] = contar_toques([m for m in depois if m["de"] == "agencia" and m["quando"] > ultimo_cliente])[0]
    return r


def parece_prospeccao(msgs: list[dict]) -> bool:
    primeira = next((m["texto"] for m in msgs if m["de"] == "agencia"), "")
    return len(primeira) >= 80 and bool(PALAVRAS_PROSPECCAO.search(primeira))


def _intervalo(seg: float) -> str:
    if seg < 120:
        return f"+{int(seg)}s"
    if seg < 7200:
        return f"+{int(seg // 60)}min"
    if seg < 2 * 86400:
        return f"+{int(seg // 3600)}h"
    return f"+{int(seg // 86400)} dias"


def montar_conversa(msgs: list[dict]) -> str:
    linhas = []
    anterior = None
    for m in msgs[-40:]:
        quem = "Agência" if m["de"] == "agencia" else ("Cliente [AUTOMÁTICA]" if m.get("auto") else "Cliente")
        quando = datetime.fromtimestamp(m["quando"]).strftime("[%d/%m %H:%M] ") if m["quando"] else ""
        gap = f"({_intervalo(m['quando'] - anterior)}) " if anterior and m["quando"] else ""
        linhas.append(f"{quando}{gap}{quem}: {m['texto'].replace(chr(10), ' ')[:600]}")
        anterior = m["quando"]
    return "\n".join(linhas)


def perguntar_ia(msgs: list[dict], memoria: dict) -> dict | None:
    lembranca = ""
    if memoria.get("planilha"):
        lembranca += ("Pesquisa da equipe sobre este negócio (planilha; use como CONTEXTO sobre quem é o Cliente, "
                      "dono, site e o que já foi enviado. A ETAPA se decide pelas mensagens abaixo, não pela planilha):\n"
                      f"{memoria['planilha'][:2500]}\n\n")
    if memoria.get("resumo"):
        lembranca += f"O que já se sabia deste Cliente (pode estar desatualizado): {memoria['resumo']}\n\n"
    prompt = PROMPT.format(hoje=datetime.now().strftime("%A, %d/%m/%Y"), memoria=lembranca, playbook=PLAYBOOK,
                           conversa=montar_conversa(msgs))
    try:
        return ia.chamar_json(prompt, ctx=12288, limite=2500)
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"Erro na análise da IA: {e}")
        return None


def estagio_pelos_fatos(f: dict, vez_de: str) -> str:
    if f["cliente_aceitou"]:
        return "FECHADO_GANHO"
    if f["cliente_recusou"]:
        return "PERDIDO"
    if f["reuniao_combinada"] and not f["reuniao_aconteceu"]:
        return "REUNIAO_AGENDADA"
    if f["proposta_enviada"]:
        return "PROPOSTA"
    if f["reuniao_aconteceu"]:
        return "REUNIAO_FEITA"
    if f["loom_enviado"]:
        return "LOOM_ENVIADO"
    if f["cliente_pediu_loom"]:
        return "LOOM_ACEITO"
    # conversa em andamento: separa "nossa vez de responder" de "aguardando o lead"
    return "EM_CONVERSA" if vez_de == "agencia" else "AGUARDANDO_RETORNO"


def proximo_contato(estagio: str, a: dict, ultima_ts: float) -> float:
    """Quando agir de novo (vai para o campo 'Próximo contato em' do HubSpot)."""
    t = agora()
    if estagio in SEM_RESPOSTA:
        return (a.get("ultimo_toque") or t) + FU_DIAS * 86400
    if estagio == "PERDIDO":
        return (a.get("ultimo_toque") or t) + FRIO_RETOMAR_DIAS * 86400 if a.get("motivo_perda") == "sem_resposta" else 0
    if estagio in ("AGUARDANDO_RETORNO", "PROPOSTA"):
        return (ultima_ts or t) + 3 * 86400
    if estagio == "LOOM_ACEITO":
        return t + 86400
    if estagio == "LOOM_ENVIADO":
        return (ultima_ts or t) + 86400
    if estagio == "EM_CONVERSA":
        return t
    return 0


def analisar(msgs: list[dict], memoria: dict) -> dict | None:
    """Devolve a análise completa. Regras fixas para o que é óbvio; a IA só para entender o que o lead disse."""
    if msgs and "auto" not in msgs[0]:
        marcar_automaticas(msgs)
    toques, ultimo_toque = contar_toques([m for m in msgs if m["de"] == "agencia"])
    reais = [m for m in msgs if m["de"] == "cliente" and not m.get("auto")]
    automaticas = [m for m in msgs if m["de"] == "cliente" and m.get("auto")]
    base = {c: "" for c in TEXTOS}
    base.update({"fatos": {f: False for f in FATOS}, "e_lead": True, "score": 0, "toques": toques,
                 "ultimo_toque": ultimo_toque, "usou_ia": False, "vez_de": "lead"})
    origem = "prospeccao" if msgs and msgs[0]["de"] == "agencia" else "inbound"
    base["origem"] = origem
    so_cortesia = bool(reais) and all(e_cortesia(m["texto"]) for m in reais) and not aceitou_video_curto(msgs)

    def sem_resposta(situacao: str = "") -> dict:
        r = dict(base)
        r["estagio"] = "PROSPECCAO" if toques <= 1 else "FOLLOWUP"
        r["followups"] = max(0, toques - 1)
        ultima_ag = next((m["texto"] for m in reversed(msgs) if m["de"] == "agencia"), "")
        encerrou = toques > MAX_FOLLOWUPS or (toques >= 2 and ENCERRAMENTO.search(ultima_ag))
        if encerrou and agora() - ultimo_toque >= FU_DIAS * 86400:
            r["estagio"], r["motivo_perda"] = "PERDIDO", "sem_resposta"
        if any(m["de"] == "agencia" and LINK_LOOM.search(m["texto"]) for m in msgs) and r["estagio"] != "PERDIDO":
            r["estagio"] = "LOOM_ENVIADO"
        if any(NAO_MONITORADO.search(m["texto"]) for m in automaticas):
            r["estagio"], r["motivo_perda"] = "PERDIDO", "numero_errado"
            situacao = "🚫 Número só de avisos automáticos (não é atendido): achar outro contato"
        r["temperatura"] = "frio"
        r["vez_de"] = "ninguem" if r["estagio"] == "PERDIDO" else "lead"
        r["situacao"] = situacao or ("Sem resposta do lead" if not automaticas else "🤖 Só a resposta automática respondeu")
        if r["estagio"] == "PERDIDO" and r["motivo_perda"] == "sem_resposta":
            r["situacao"] = (situacao or "Sem resposta") + f" · frio, retomar em {FRIO_RETOMAR_DIAS:g} dias"
        r["resumo"] = memoria.get("resumo") or (
            f"Prospecção enviada, {r['followups']} follow-up(s) feito(s), sem resposta de uma pessoa do negócio.")
        r["proxima_acao"] = (f"Lead Frio: retomar em {FRIO_RETOMAR_DIAS:g} dias com uma nova observação"
                             if r["motivo_perda"] == "sem_resposta" else
                             "Achar outro contato (site, Instagram, Google)" if r["motivo_perda"] == "numero_errado"
                             else f"Follow-up do Dia {3 + 2 * r['followups']} se não responder")
        return r

    if not reais:
        return sem_resposta()
    if so_cortesia:
        # "Obrigado"/"beleza" sem responder à oferta: segue o fluxo de quem não respondeu (Dia 3, Dia 5, Frio)
        r = sem_resposta(f"Respondeu só com cortesia (\"{reais[-1]['texto'][:30]}\")")
        r["nome"] = memoria.get("nome", "")
        return r

    dados = perguntar_ia(msgs, memoria)
    if dados is None:
        return None
    r = dict(base)
    r["usou_ia"] = True
    for c in TEXTOS:
        r[c] = str(dados.get(c) or "").strip()
    fatos = {f: dados.get(f) is True or str(dados.get(f)).lower() == "true" for f in FATOS}
    if any(m["de"] == "agencia" and LINK_LOOM.search(m["texto"]) for m in msgs):
        fatos["loom_enviado"] = True  # link do Loom ou vídeo enviado: não depende da IA
    if aceitou_video_curto(msgs):
        fatos["cliente_pediu_loom"] = True  # "Ok"/"Pode" logo depois da oferta do vídeo
    if any(RECUSA.search(m["texto"]) for m in reais[-3:]):
        fatos["cliente_recusou"] = True
    # Fechamento: agência mandou contrato/pagamento e o cliente confirmou -> não depende da IA
    cobranca = next((m["quando"] for m in msgs if m["de"] == "agencia" and COBRANCA.search(m["texto"])), None)
    if cobranca and any(m["de"] == "cliente" and m["quando"] >= cobranca and CONFIRMOU.search(m["texto"]) for m in reais):
        fatos["cliente_aceitou"] = True
    r["fatos"] = fatos
    r["e_lead"] = not fatos["assunto_pessoal"]
    try:
        r["score"] = max(0, min(100, int(float(dados.get("score") or 0))))
    except (TypeError, ValueError):
        r["score"] = 0
    if r["temperatura"] not in ("quente", "morno", "frio"):
        r["temperatura"] = "quente" if r["score"] >= 70 else "morno" if r["score"] >= 35 else "frio"

    def resposta_rapida(m: dict) -> bool:
        antes = [x["quando"] for x in msgs if x["de"] == "agencia" and x["quando"] <= m["quando"]]
        return bool(antes) and m["quando"] - antes[-1] <= 120

    # A IA só pode dizer "é tudo robô" se todas as respostas chegaram em até 2 min (tempo de robô, não de pessoa)
    if fatos["so_resposta_automatica"] and all(resposta_rapida(m) for m in reais):
        auto = sem_resposta()
        auto.update({k: r[k] for k in ("nome", "empresa", "cidade", "tipo_negocio") if r[k]})
        auto["usou_ia"] = True
        return auto

    # De quem é a vez: pela ÚLTIMA mensagem que conta (robô não conta)
    ultima = humanas(msgs)[-1]
    if ultima["de"] == "agencia" or e_cortesia(ultima["texto"]):
        vez = "lead"
    elif fatos["lead_vai_retornar"] and "?" not in ultima["texto"]:
        vez = "lead"
    else:
        vez = "agencia"  # o lead falou por último com conteúdo (não é só "obrigado"): a vez é nossa
    r["estagio"] = estagio_pelos_fatos(fatos, vez)
    if r["estagio"] == "LOOM_ACEITO":
        vez = "agencia"  # pediu o vídeo: gravar e enviar é com a gente
    r["vez_de"] = vez

    # Toques da agência depois da última fala do lead: insistiu e nada -> Frio (retomar em 60 dias)
    desde = reais[-1]["quando"]
    if r["estagio"] == "LOOM_ENVIADO":
        # depois do Loom o playbook dá 3 follow-ups (+1, +4, +10): conta só os que vieram depois do vídeo
        loom_ts = next((m["quando"] for m in msgs if m["de"] == "agencia" and LINK_LOOM.search(m["texto"])), 0)
        desde = max(desde, loom_ts)
    depois = [m for m in msgs if m["de"] == "agencia" and m["quando"] > desde]
    toques_depois, ultimo_depois = contar_toques(depois)
    encerrou = toques_depois >= 3 or (toques_depois >= 2 and depois and ENCERRAMENTO.search(depois[-1]["texto"]))
    if (r["estagio"] in ("EM_CONVERSA", "AGUARDANDO_RETORNO", "LOOM_ACEITO", "LOOM_ENVIADO", "PROPOSTA") and encerrou
            and agora() - ultimo_depois >= FU_DIAS * 86400):
        r["estagio"], r["motivo_perda"] = "PERDIDO", "sem_resposta"
        r["situacao"] = f"Parou de responder após {toques_depois} mensagens nossas · frio, retomar em {FRIO_RETOMAR_DIAS:g} dias"
    if r["estagio"] in FINAIS:
        r["vez_de"] = "ninguem"
    r["followups"] = max(0, contar_toques([m for m in msgs if m["de"] == "agencia"
                                           and m["quando"] < reais[0]["quando"]])[0] - 1)
    if r["estagio"] != "PERDIDO":
        r["motivo_perda"] = ""
    elif not r["motivo_perda"]:
        r["motivo_perda"] = "sem_interesse"
    if r["email"] and "@" not in r["email"]:
        r["email"] = ""
    return r


def estagio_final(atual: str | None, sugerido: str) -> str:
    """Só avança (pode pular etapas). Exceções: Perdido a qualquer momento; lead perdido pode voltar;
    'Em conversa' e 'Aguardando retorno' são o mesmo degrau (alternam conforme de quem é a vez)."""
    atual = CODIGOS_ANTIGOS.get(atual, atual)
    if not atual or atual not in ORDEM:
        return sugerido
    if sugerido == "PERDIDO" or atual == "PERDIDO":
        return sugerido
    return sugerido if DEGRAU[sugerido] >= DEGRAU[atual] else atual


# ------------------------------------------------------------------ HubSpot
_cache_funil: dict = {}


def ids_do_funil() -> tuple[str, dict[str, str]]:
    if _cache_funil.get("id") and len(_cache_funil.get("etapas", {})) == len(ETAPAS):
        return _cache_funil["id"], _cache_funil["etapas"]
    r = requests.get(f"{API}/crm/v3/pipelines/deals", headers=HS_HEADERS, timeout=20)
    r.raise_for_status()
    for p in r.json()["results"]:
        if p["label"] == PIPELINE_NOME:
            por_rotulo = {s["label"]: s["id"] for s in p["stages"]}
            _cache_funil["id"] = p["id"]
            _cache_funil["etapas"] = {cod: por_rotulo[rot] for rot, cod, _, _ in ETAPAS if rot in por_rotulo}
            return _cache_funil["id"], _cache_funil["etapas"]
    raise RuntimeError(f"Funil '{PIPELINE_NOME}' não encontrado. Rode: python hubspot_setup.py")


PROPS_CONTATO = ["firstname", "lastname", "email", "company", "city", "phone", "mobilephone", "website",
                 "lifecyclestage", "estagio_funil", "origem_lead"]


def hs(metodo: str, caminho: str, **kw) -> requests.Response:
    kw.setdefault("timeout", 20)
    for tentativa in range(3):
        r = requests.request(metodo, f"{API}{caminho}", headers=HS_HEADERS, **kw)
        if r.status_code != 429:
            return r
        time.sleep(2 + tentativa * 3)  # limite de chamadas do HubSpot: espera e tenta de novo
    return r


def buscar_contato(telefone: str) -> dict | None:
    salvo = registro_get(telefone)
    if salvo.get("contato_id"):
        r = hs("GET", f"/crm/v3/objects/contacts/{salvo['contato_id']}", params={"properties": ",".join(PROPS_CONTATO)})
        if r.ok:
            return r.json()
        registro_set(telefone, contato_id=None, negocio_id=None)
    com_mais = "+" + telefone
    grupos = [{"filters": [{"propertyName": p, "operator": "EQ", "value": v}]}
              for p, v in (("phone", com_mais), ("phone", telefone), ("mobilephone", com_mais))]
    grupos.append({"filters": [{"propertyName": "phone", "operator": "CONTAINS_TOKEN", "value": "*" + telefone[-8:]}]})
    r = hs("POST", "/crm/v3/objects/contacts/search",
           json={"filterGroups": grupos, "properties": PROPS_CONTATO, "limit": 5})
    r.raise_for_status()
    for c in r.json().get("results", []):
        numeros = [re.sub(r"\D", "", c["properties"].get(k) or "") for k in ("phone", "mobilephone")]
        if any(n and (n == telefone or n.endswith(telefone[-10:])) for n in numeros):
            return c
    return None


def criar_contato(telefone: str, nome: str, origem: str) -> dict:
    props = {"phone": "+" + telefone, "lifecyclestage": "lead", "origem_lead": origem}
    if nome:
        partes = nome.split(" ", 1)
        props["firstname"] = partes[0]
        if len(partes) > 1:
            props["lastname"] = partes[1]
    r = hs("POST", "/crm/v3/objects/contacts", json={"properties": props})
    r.raise_for_status()
    c = r.json()
    c.setdefault("properties", {}).update(props)
    return c


def atualizar_contato(contato: dict, a: dict, estagio: str):
    atuais = contato.get("properties", {})
    props = {"estagio_funil": estagio, "ultima_analise_ia": datetime.now().strftime("%d/%m/%Y %H:%M"),
             "followups_enviados": str(a.get("followups", 0))}
    if estagio in STATUS_LEAD:
        props["hs_lead_status"] = STATUS_LEAD[estagio]
    props.update(campos_navegacao(a))
    for campo_hs, campo_ia in (("resumo_conversa", "resumo"), ("proxima_acao", "proxima_acao"),
                               ("sugestao_resposta", "sugestao_resposta"), ("data_reuniao", "data_reuniao"),
                               ("tipo_negocio", "tipo_negocio"), ("orcamento_anuncios", "orcamento_anuncios"),
                               ("temperatura_lead", "temperatura")):
        if a.get(campo_ia):
            props[campo_hs] = str(a[campo_ia])[:1000]
    if a.get("usou_ia"):
        props["score_lead"] = str(a.get("score", 0))
    if not atuais.get("origem_lead") and a.get("origem"):
        props["origem_lead"] = a["origem"]
    if a.get("nome") and not atuais.get("firstname"):
        partes = a["nome"].split(" ", 1)
        props["firstname"] = partes[0]
        if len(partes) > 1 and not atuais.get("lastname"):
            props["lastname"] = partes[1]
    for campo_ia, campo_hs in (("email", "email"), ("empresa", "company"), ("cidade", "city")):
        if a.get(campo_ia) and not atuais.get(campo_hs):
            props[campo_hs] = a[campo_ia]
    if estagio == "FECHADO_GANHO":
        props["lifecyclestage"] = "customer"
    elif estagio in ("REUNIAO_AGENDADA", "REUNIAO_FEITA") and atuais.get("lifecyclestage") in (
            None, "", "subscriber", "lead", "marketingqualifiedlead", "salesqualifiedlead"):
        props["lifecyclestage"] = "opportunity"

    r = hs("PATCH", f"/crm/v3/objects/contacts/{contato['id']}", json={"properties": props})
    if not r.ok:
        for campo in ("lifecyclestage", "hs_lead_status"):  # o HubSpot às vezes recusa voltar esses campos
            props.pop(campo, None)
        r = hs("PATCH", f"/crm/v3/objects/contacts/{contato['id']}", json={"properties": props})
    if not r.ok:
        print(f"HubSpot recusou atualizar o contato ({r.status_code}): {r.text[:300]}")


def garantir_empresa(contato: dict, a: dict, negocio_id: str | None):
    """Cria ou reaproveita a Empresa do lead e liga ao contato e ao negócio."""
    nome = a.get("empresa") or contato.get("properties", {}).get("company")
    if not nome:
        return
    ja = hs("GET", f"/crm/v4/objects/contacts/{contato['id']}/associations/companies")
    if ja.ok and ja.json().get("results"):
        empresa_id = str(ja.json()["results"][0]["toObjectId"])
    else:
        busca = hs("POST", "/crm/v3/objects/companies/search", json={
            "filterGroups": [{"filters": [{"propertyName": "name", "operator": "EQ", "value": nome}]}], "limit": 1})
        achou = busca.json().get("results", []) if busca.ok else []
        if achou:
            empresa_id = achou[0]["id"]
        else:
            props = {"name": nome[:200]}
            if a.get("cidade") or contato.get("properties", {}).get("city"):
                props["city"] = a.get("cidade") or contato["properties"]["city"]
            site = contato.get("properties", {}).get("website")
            if site:
                props["domain"] = re.sub(r"^https?://(www\.)?", "", site).split("/")[0]
            c = hs("POST", "/crm/v3/objects/companies", json={"properties": props})
            if not c.ok:
                return
            empresa_id = c.json()["id"]
        hs("PUT", f"/crm/v4/objects/contacts/{contato['id']}/associations/default/companies/{empresa_id}")
    if negocio_id:
        hs("PUT", f"/crm/v4/objects/deals/{negocio_id}/associations/default/companies/{empresa_id}")


def buscar_negocio(contato_id: str, pipeline_id: str, telefone: str) -> dict | None:
    salvo = registro_get(telefone)
    if salvo.get("negocio_id"):
        r = hs("GET", f"/crm/v3/objects/deals/{salvo['negocio_id']}", params={"properties": "pipeline,dealstage,dealname"})
        if r.ok and r.json()["properties"].get("pipeline") == pipeline_id:
            return r.json()
    r = hs("GET", f"/crm/v4/objects/contacts/{contato_id}/associations/deals")
    ids = [str(x["toObjectId"]) for x in r.json().get("results", [])] if r.ok else []
    if not ids:
        return None
    r = hs("POST", "/crm/v3/objects/deals/batch/read", json={
        "properties": ["pipeline", "dealstage", "dealname", "createdate"], "inputs": [{"id": i} for i in ids]})
    if not r.ok:
        return None
    nossos = [d for d in r.json().get("results", []) if d["properties"].get("pipeline") == pipeline_id]
    nossos.sort(key=lambda d: d["properties"].get("createdate") or "", reverse=True)
    return nossos[0] if nossos else None


def salvar_negocio(contato_id: str, negocio: dict | None, titulo: str, estagio: str, a: dict,
                   pipeline_id: str, etapas: dict[str, str]) -> str:
    props = {"dealstage": etapas[estagio], **campos_navegacao(a)}
    if a.get("resumo"):
        props["description"] = a["resumo"][:1000]
    if a.get("proxima_acao"):
        props["proxima_acao"] = a["proxima_acao"][:250]
    if a.get("temperatura"):
        props["temperatura_lead"] = a["temperatura"]
    if a.get("origem"):
        props["origem_lead"] = a["origem"]
    valor = re.sub(r"[^\d.]", "", str(a.get("valor_proposta") or "").replace(",", "."))
    if valor:
        try:
            props["amount"] = str(float(valor))
        except ValueError:
            pass
    if estagio in FINAIS:
        props["closedate"] = iso_utc(agora())
    if estagio == "PERDIDO" and a.get("motivo_perda"):
        props["closed_lost_reason"] = MOTIVOS_PERDA.get(a["motivo_perda"], a["motivo_perda"])

    if negocio:
        if titulo and negocio["properties"].get("dealname") != titulo and a.get("empresa") \
                and a["empresa"].lower() not in (negocio["properties"].get("dealname") or "").lower():
            props["dealname"] = titulo  # descobriu a empresa: o card passa a ter o nome dela
        r = hs("PATCH", f"/crm/v3/objects/deals/{negocio['id']}", json={"properties": props})
        if not r.ok:
            print(f"HubSpot recusou atualizar o negócio ({r.status_code}): {r.text[:300]}")
        return negocio["id"]
    props.update({"dealname": titulo, "pipeline": pipeline_id})
    r = hs("POST", "/crm/v3/objects/deals", json={"properties": props, "associations": [
        {"to": {"id": contato_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 3}]}]})
    r.raise_for_status()
    return r.json()["id"]


def _assoc(contato_id: str, negocio_id: str | None, tipo_contato: int, tipo_negocio: int) -> list:
    assoc = [{"to": {"id": contato_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": tipo_contato}]}]
    if negocio_id:
        assoc.append({"to": {"id": negocio_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": tipo_negocio}]})
    return assoc


def criar_nota(contato_id: str, negocio_id: str, de: str | None, para: str, a: dict, origem: str):
    linhas = [f"<b>🍺 Chopp</b> ({'importação' if origem == 'importacao' else 'ao vivo'})",
              f"Estágio: {ROTULO.get(de, 'novo') if de else 'novo'} → <b>{ROTULO[para]}</b>"]
    for rotulo, campo in (("Reunião", "data_reuniao"), ("Temperatura", "temperatura"), ("Resumo", "resumo"),
                          ("Próxima ação", "proxima_acao"), ("Sugestão de resposta", "sugestao_resposta")):
        if a.get(campo):
            linhas.append(f"{rotulo}: {a[campo]}")
    if a.get("motivo_perda"):
        linhas.append(f"Motivo: {MOTIVOS_PERDA.get(a['motivo_perda'], a['motivo_perda'])}")
    r = hs("POST", "/crm/v3/objects/notes", json={
        "properties": {"hs_timestamp": iso_utc(agora()), "hs_note_body": "<br>".join(linhas)},
        "associations": _assoc(contato_id, negocio_id, 202, 214)})
    if not r.ok:
        print(f"Não consegui criar a nota ({r.status_code}): {r.text[:200]}")


TAREFAS = {
    # estágio: (título, prazo em horas a partir de agora, prioridade)
    "LOOM_ACEITO": ("🎥 Gravar e enviar o Loom para {nome} (até amanhã 10h)", 16, "HIGH"),
    "LOOM_ENVIADO": ("Follow-up do Loom (Dia +1) com {nome}", 24, "MEDIUM"),
    "REUNIAO_FEITA": ("Resposta combinada na call com {nome}: mandar resumo e cobrar na data", 24, "HIGH"),
    "EM_CONVERSA": ("👉 Responder {nome} (a vez é nossa)", 2, "MEDIUM"),
    "AGUARDANDO_RETORNO": ("⏳ Cobrar retorno de {nome} se não responder", 72, "LOW"),
    "PROPOSTA": ("📄 Cobrar a decisão sobre a proposta com {nome}", 72, "MEDIUM"),
}


def concluir_tarefa(tarefa_id: str | None):
    if tarefa_id:
        hs("PATCH", f"/crm/v3/objects/tasks/{tarefa_id}", json={"properties": {"hs_task_status": "COMPLETED"}})


def criar_tarefa(titulo: str, prazo_ts: float, prioridade: str, corpo: str, contato_id: str, negocio_id: str | None) -> str | None:
    r = hs("POST", "/crm/v3/objects/tasks", json={
        "properties": {"hs_timestamp": iso_utc(prazo_ts), "hs_task_subject": titulo[:250], "hs_task_body": corpo[:2000],
                       "hs_task_status": "NOT_STARTED", "hs_task_priority": prioridade, "hs_task_type": "TODO"},
        "associations": _assoc(contato_id, negocio_id, 204, 216)})
    return r.json()["id"] if r.ok else None


def criar_reuniao(a: dict, nome: str, contato_id: str, negocio_id: str) -> str | None:
    try:
        inicio = datetime.strptime(a["data_reuniao_iso"][:16], "%Y-%m-%dT%H:%M").replace(tzinfo=FUSO_REUNIOES)
    except (KeyError, ValueError):
        return None
    if inicio.timestamp() < agora() - 86400:
        return None
    fim = inicio + timedelta(minutes=30)
    r = hs("POST", "/crm/v3/objects/meetings", json={
        "properties": {"hs_timestamp": iso_utc(inicio.timestamp()), "hs_meeting_title": f"Reunião com {nome}",
                       "hs_meeting_start_time": iso_utc(inicio.timestamp()), "hs_meeting_end_time": iso_utc(fim.timestamp()),
                       "hs_meeting_outcome": "SCHEDULED", "hs_meeting_body": a.get("resumo", "")[:1000]},
        "associations": _assoc(contato_id, negocio_id, 200, 212)})
    return r.json()["id"] if r.ok else None


def sincronizar_hubspot(telefone: str, nome_whatsapp: str, a: dict, origem: str, forcar: bool = False) -> dict:
    """forcar=True (auditoria): grava o estágio da nova análise mesmo que seja 'para trás'."""
    pipeline_id, etapas = ids_do_funil()
    salvo = registro_get(telefone)
    contato = buscar_contato(telefone)
    criado = contato is None
    if criado:
        contato = criar_contato(telefone, a.get("nome") or nome_whatsapp, a.get("origem", "prospeccao"))
    registro_set(telefone, contato_id=contato["id"])

    anterior = salvo.get("estagio") or CODIGOS_ANTIGOS.get(contato["properties"].get("estagio_funil"),
                                                            contato["properties"].get("estagio_funil"))
    negocio = None if criado else buscar_negocio(contato["id"], pipeline_id, telefone)
    if negocio and not anterior:
        anterior = next((c for c, sid in etapas.items() if sid == negocio["properties"].get("dealstage")), None)

    estagio = a["estagio"] if forcar else estagio_final(anterior, a["estagio"])
    a = dict(a, estagio=estagio)
    if "proximo_contato_ts" not in a:
        a["proximo_contato_ts"] = proximo_contato(estagio, a, a.get("ultima_ts") or 0)
    atualizar_contato(contato, a, estagio)
    p = contato["properties"]
    nome = a.get("nome") or " ".join(x for x in (p.get("firstname"), p.get("lastname")) if x) or nome_whatsapp or "+" + telefone
    empresa = a.get("empresa") or p.get("company") or ""
    titulo = f"{empresa or nome} · WhatsApp"
    negocio_id = salvar_negocio(contato["id"], negocio, titulo, estagio, a, pipeline_id, etapas)
    garantir_empresa(contato, a, negocio_id)

    mudou = estagio != anterior
    extras = {}
    if mudou:
        criar_nota(contato["id"], negocio_id, anterior, estagio, a, origem)
        concluir_tarefa(salvo.get("tarefa_aberta"))
        extras["tarefa_aberta"] = None
        if estagio in TAREFAS:
            tit, horas, prio = TAREFAS[estagio]
            extras["tarefa_aberta"] = criar_tarefa(tit.format(nome=empresa or nome), agora() + horas * 3600, prio,
                                                   f"{a.get('resumo', '')}\n\nSugestão: {a.get('sugestao_resposta', '')}",
                                                   contato["id"], negocio_id)
    if estagio == "REUNIAO_AGENDADA" and a.get("data_reuniao_iso") and salvo.get("reuniao_iso") != a["data_reuniao_iso"]:
        if criar_reuniao(a, empresa or nome, contato["id"], negocio_id):
            extras["reuniao_iso"] = a["data_reuniao_iso"]

    registro_set(telefone, negocio_id=negocio_id, **extras)
    return {"status": "atualizado", "estagio": estagio, "anterior": anterior, "mudou": mudou,
            "contato_criado": criado, "contato_id": contato["id"], "negocio_id": negocio_id,
            "nome": nome, "empresa": empresa}


# ------------------------------------------------------------------ alertas para o grupo
def nome_curto(r: dict, telefone: str) -> str:
    return r.get("empresa") or r.get("nome") or "+" + telefone


def alerta_imediato(telefone: str, anterior: str | None, estagio: str, a: dict, reg: dict) -> str | None:
    quem = f"*{nome_curto(reg, telefone)}*"
    link = f"wa.me/{telefone}"
    if estagio == "LOOM_ACEITO":
        return (f"🎥 {quem} *pediu o vídeo!*\n1) Responda agora *reservando a call* (Etapa 3):\n"
                f"_{a.get('sugestao_resposta') or 'Fechado! Gravo hoje e te mando até amanhã às 10h. Já deixo [dia] às [hora] separado pra conversarmos 15 min?'}_\n"
                f"2) Gravar o Loom (máx. 4 min) e enviar até amanhã às 10h.\n{link}")
    if estagio == "EM_CONVERSA" and a.get("vez_de") == "agencia" and (anterior in SEM_RESPOSTA or anterior is None):
        return f"💬 {quem} respondeu. Bora responder!\nSugestão: _{a.get('sugestao_resposta', '')}_\n{link}"
    if estagio == "PROPOSTA" and anterior != "PROPOSTA":
        return f"📄 Proposta com {quem}: {a.get('situacao') or 'aguardando a decisão'}.\nCobrar em 3 dias se não responder."
    if estagio == "REUNIAO_AGENDADA":
        return (f"📅 Call de 15 min marcada com {quem}: {a.get('data_reuniao') or 'ver conversa'}\n"
                f"Mande o convite no Google Agenda agora. Aviso 1h antes.\n{link}")
    if estagio == "FECHADO_GANHO":
        return f"🎉 {quem} *FECHOU!* {a.get('resumo', '')}"
    if estagio == "PERDIDO" and a.get("motivo_perda") != "sem_resposta":
        return f"❌ {quem} recusou ({MOTIVOS_PERDA.get(a.get('motivo_perda'), 'sem motivo')}).\n{a.get('resumo', '')}"
    return None


def _primeiro_nome(r: dict) -> str:
    return (r.get("nome") or "").split(" ")[0] or "[Nome]"


def _msg_followup(qual: int, r: dict) -> str:
    nome = _primeiro_nome(r)
    if qual == 1:
        return (f"{nome}, fiz a busca de novo hoje e o [concorrente] continua aparecendo antes de vocês. "
                f"Te mando o vídeo de 4 min mostrando isso?")
    return (f"{nome}, última mensagem pra não te encher. Se um dia quiser ver o que aparece pra quem procura "
            f"[serviço] em {r.get('cidade') or '[cidade]'}, é só me chamar aqui.")


def _msg_pos_loom(dia: int, r: dict) -> str:
    if dia == 1:
        return ("Conseguiu dar uma olhada? Se preferir, te resumo em 2 linhas aqui mesmo. "
                "(Se ASSISTIU no Loom: mande uma informação nova com print e proponha [dia] ou [dia].)")
    if dia == 4:
        return "Faz sentido pra vocês agora ou deixo pra outro momento?"
    return ("Encerrando por aqui. Se quiser retomar depois, é só me chamar. "
            "(Mensagem de encerramento, só se for verdade.)")


def calcular_pendencias(encerrar: bool = True) -> dict:
    t = agora()
    p = {"especial": [], "gravar_loom": [], "responder": [], "cobrar_retorno": [], "followups": [], "loom_followups": [],
         "reunioes": [], "retomar_frios": [], "frios": []}
    for tel, r in registro_todos().items():
        est = CODIGOS_ANTIGOS.get(r.get("estagio"), r.get("estagio"))
        if not est or r.get("nao_e_lead"):
            continue
        item = {"telefone": tel, "nome": nome_curto(r, tel), "resumo": r.get("resumo", ""),
                "sugestao": r.get("sugestao_resposta", "")}
        horas_toque = (t - (r.get("ultimo_toque") or t)) / 3600

        # Follow-up especial (marcado à mão pela equipe): aparece sempre, em bloco próprio, e nunca vira Frio sozinho
        if r.get("follow_up_especial"):
            p["especial"].append({**item, "situacao": r.get("situacao", ""),
                                  "dias": round((t - (r.get("ultima_msg_ts") or t)) / 86400)})
            continue

        # Etapa 3/4: pediu o vídeo -> gravar e enviar até o dia seguinte às 10h
        if est == "LOOM_ACEITO":
            aceito = r.get("loom_aceito_ts") or t
            prazo = datetime.fromtimestamp(aceito).replace(hour=10, minute=0, second=0) + timedelta(days=1)
            p["gravar_loom"].append({**item, "horas": round((t - aceito) / 3600, 1),
                                     "atrasado": t > prazo.timestamp(), "prazo": prazo.strftime("%d/%m 10h")})

        # Etapa 2: sem resposta -> follow-up Dia 3 e Dia 5; depois, Frio
        elif est in SEM_RESPOSTA and horas_toque >= FU_DIAS * 24:
            feitos = r.get("followups", 0)
            if feitos < MAX_FOLLOWUPS:
                qual = feitos + 1
                p["followups"].append({**item, "qual": qual, "dia": 1 + 2 * qual, "mensagem": _msg_followup(qual, r)})
            elif encerrar:
                p["frios"].append(item)

        # Etapa 5: Loom enviado sem resposta -> Dia +1, +4, +10
        elif est == "LOOM_ENVIADO" and r.get("loom_enviado_ts") and not r.get("cliente_pos_loom"):
            n = r.get("toques_pos_loom", 0)
            if n < len(LOOM_FU_DIAS):
                dia = LOOM_FU_DIAS[n]
                if t >= r["loom_enviado_ts"] + dia * 86400:
                    p["loom_followups"].append({**item, "dia": dia, "mensagem": _msg_pos_loom(dia, r)})

        # Lead mandou mensagem e a vez é nossa (pergunta/pedido sem resposta). "Obrigado", robô ou
        # "vou analisar" não entram aqui.
        vez = r.get("vez_de") or ("agencia" if r.get("ultima_msg_de") == "cliente" else "lead")
        if est not in FINAIS | SEM_RESPOSTA | {"LOOM_ACEITO"} and r.get("ultima_msg_de") == "cliente" and vez == "agencia":
            minutos = (t - (r.get("ultimo_cliente_ts") or t)) / 60
            if minutos >= RESPONDER_MIN:
                p["responder"].append({**item, "horas": round(minutos / 60, 1)})

        # O lead ficou de dar retorno (vai analisar, falar com alguém, decidir a proposta): cobrar após 3 dias
        if est in ("AGUARDANDO_RETORNO", "PROPOSTA"):
            ultimo = max(r.get("ultimo_cliente_ts") or 0, r.get("ultimo_agencia_ts") or 0)
            if ultimo and t - ultimo >= 3 * 86400:
                p["cobrar_retorno"].append({**item, "dias": round((t - ultimo) / 86400),
                                            "situacao": r.get("situacao", ""), "proposta": est == "PROPOSTA"})

        if est == "REUNIAO_AGENDADA":
            p["reunioes"].append({**item, "quando": r.get("data_reuniao", "")})

        # Frio há 60 dias -> retomar com uma nova observação
        if est == "PERDIDO" and r.get("frio_desde") and t - r["frio_desde"] >= FRIO_RETOMAR_DIAS * 86400:
            p["retomar_frios"].append({**item, "dias": round((t - r["frio_desde"]) / 86400)})

    # Quem não respondeu nem ao follow-up do Dia 5 vira Frio
    for item in p["frios"]:
        with lock_do(item["telefone"]):
            reg = registro_get(item["telefone"])
            a = {c: "" for c in TEXTOS}
            a.update({"estagio": "PERDIDO", "motivo_perda": "sem_resposta", "followups": reg.get("followups", 0),
                      "temperatura": "frio", "usou_ia": False,
                      "resumo": reg.get("resumo") or "Não respondeu à prospecção nem aos follow-ups dos Dias 3 e 5.",
                      "proxima_acao": f"Lead Frio: retomar em {FRIO_RETOMAR_DIAS:g} dias com uma nova observação"})
            if ATUALIZAR_HUBSPOT:
                try:
                    sincronizar_hubspot(item["telefone"], reg.get("nome", ""), a, "ao_vivo")
                except (requests.RequestException, RuntimeError) as e:
                    print(f"Erro ao marcar como Frio {item['telefone']}: {e}")
            registro_set(item["telefone"], estagio="PERDIDO", frio_desde=agora(), temperatura="frio")
    return p


def texto_relatorio(p: dict) -> str:
    def link(i): return f"wa.me/{i['telefone']}"
    blocos = []
    if p.get("especial"):
        blocos.append("⭐ *FOLLOW-UP ESPECIAL*\n" + "\n".join(
            f"• {i['nome']} · última mensagem há {i['dias']} dias · {link(i)}\n   ↳ {i['situacao'][:160]}"
            for i in p["especial"]))
    if p["gravar_loom"]:
        blocos.append("🎥 *LOOM PARA GRAVAR* (pediu o vídeo · enviar até as 10h do dia seguinte)\n" + "\n".join(
            f"• {'⚠️ *ATRASADO* · ' if i['atrasado'] else ''}{i['nome']} · pediu há {i['horas']:g}h · prazo {i['prazo']} · {link(i)}"
            for i in p["gravar_loom"]) + "\n   ↳ Reservou a call na hora (Etapa 3)? Se não, ofereça junto com o envio.")
    if p["responder"]:
        blocos.append("💬 *ESPERANDO RESPOSTA DE VOCÊS*\n" + "\n".join(
            f"• {i['nome']} · há {i['horas']:g}h · {link(i)}\n   ↳ {i['sugestao'][:200]}" for i in p["responder"]))
    if p["cobrar_retorno"]:
        blocos.append("⏳ *COBRAR RETORNO* (o lead ficou de responder)\n" + "\n".join(
            f"• {i['nome']} · {'proposta · ' if i['proposta'] else ''}parado há {i['dias']} dias · {link(i)}"
            + (f"\n   ↳ {i['situacao'][:140]}" if i.get("situacao") else "")
            + ("\n   ↳ _Conseguiram analisar a proposta? Se ficou alguma dúvida, te explico em 15 minutos._"
               if i["proposta"] else "\n   ↳ _Oi! Passando pra saber se conseguiu ver isso. Posso te ajudar em algo?_")
            for i in p["cobrar_retorno"]))
    if p["followups"]:
        blocos.append("🔁 *FOLLOW-UP DE HOJE* (sem resposta à prospecção)\n" + "\n".join(
            f"• {i['nome']} · *Dia {i['dia']}*{' (último)' if i['qual'] >= MAX_FOLLOWUPS else ''} · {link(i)}\n"
            f"   ↳ _{i['mensagem']}_" for i in p["followups"]))
    if p["loom_followups"]:
        blocos.append("⏳ *FOLLOW-UP DO LOOM* (enviado e sem resposta)\n" + "\n".join(
            f"• {i['nome']} · *Dia +{i['dia']}* · {link(i)}\n   ↳ _{i['mensagem']}_" for i in p["loom_followups"]))
    if p["reunioes"]:
        blocos.append("📅 *CALLS MARCADAS* (lembrete 1h antes)\n" + "\n".join(
            f"• {i['nome']} · {i['quando'] or 'ver conversa'}" for i in p["reunioes"]))
    if p["retomar_frios"]:
        blocos.append(f"🧊 *RETOMAR LEADS FRIOS* ({FRIO_RETOMAR_DIAS:g} dias) · com uma observação nova\n" + "\n".join(
            f"• {i['nome']} · frio há {i['dias']} dias · {link(i)}" for i in p["retomar_frios"]))
    if p["frios"]:
        blocos.append(f"🧊 *VIRARAM FRIOS HOJE* (sem resposta após o Dia 5 · voltar em {FRIO_RETOMAR_DIAS:g} dias)\n"
                      + "\n".join(f"• {i['nome']}" for i in p["frios"]))
    if not blocos:
        return "🍺 *Chopp*: nenhuma pendência agora. Tudo em dia com o playbook!"
    return f"🍺 *Chopp · {datetime.now().strftime('%d/%m %H:%M')}*\n\n" + "\n\n".join(blocos)


def lembretes_de_call() -> list[str]:
    """Playbook, Etapa 6: lembrete no WhatsApp 1 hora antes da call."""
    avisos = []
    t = agora()
    for tel, r in registro_todos().items():
        iso = r.get("reuniao_iso")
        if not iso or r.get("lembrete_enviado") == iso:
            continue
        try:
            inicio = datetime.strptime(iso[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=FUSO_REUNIOES).timestamp()
        except ValueError:
            continue
        if 0 <= inicio - t <= 75 * 60:
            minutos = round((inicio - t) / 60)
            avisos.append(f"⏰ *Call com {nome_curto(r, tel)} em {minutos} min*\nMande o lembrete: _{_primeiro_nome(r)}, "
                          f"tudo certo pra daqui a pouco? Link: [link]_\nwa.me/{tel}")
            registro_set(tel, lembrete_enviado=iso)
    return avisos


def texto_metricas(dias: int = 7) -> str:
    """Playbook: métricas da semana por etapa e por template (A/B)."""
    t = agora()
    leads = [r for r in registro_todos().values() if r.get("estagio") and not r.get("nao_e_lead")]

    def ordem(r):
        return ORDEM.get(CODIGOS_ANTIGOS.get(r["estagio"], r["estagio"]), -1)

    def pct(a, b):
        return f"{(100 * a / b):.0f}% ({a}/{b})" if b else "—"

    def bloco(grupo, titulo):
        prosp = [r for r in grupo if r.get("origem") == "prospeccao" or r.get("template")]
        resp = [r for r in prosp if r.get("respondeu")]
        loom = [r for r in grupo if r.get("loom_aceito_ts") or ordem(r) >= ORDEM["LOOM_ACEITO"] and r["estagio"] != "PERDIDO"]
        enviados = [r for r in grupo if r.get("loom_enviado_ts")]
        call = [r for r in grupo if ordem(r) >= ORDEM["REUNIAO_AGENDADA"] and r["estagio"] != "PERDIDO"]
        fech = [r for r in grupo if r["estagio"] == "FECHADO_GANHO"]
        linhas = [f"*{titulo}*",
                  f"• Responderam à 1ª mensagem: {pct(len(resp), len(prosp))}"]
        for tpl in ("A", "B"):
            env = [r for r in prosp if r.get("template") == tpl]
            if env:
                linhas.append(f"   ↳ Template {tpl}: {pct(sum(1 for r in env if r.get('respondeu')), len(env))}")
        linhas += [f"• Pediram o Loom: {pct(len(loom), len(resp) or len(grupo))}",
                   f"• Marcaram a call (de quem recebeu Loom): {pct(sum(1 for r in enviados if r in call), len(enviados))}",
                   f"• Fecharam (de quem marcou call): {pct(len(fech), len(call))}"]
        return "\n".join(linhas)

    semana = [r for r in leads if (r.get("primeiro_ts") or 0) >= t - dias * 86400]
    texto = "📊 *Métricas do playbook*\n\n" + bloco(semana, f"Últimos {dias} dias ({len(semana)} leads)") + \
            "\n\n" + bloco(leads, f"Desde o início ({len(leads)} leads)")
    total_a = sum(1 for r in leads if r.get("template") == "A")
    total_b = sum(1 for r in leads if r.get("template") == "B")
    if total_a or total_b:
        texto += (f"\n\nTemplates enviados: A={total_a} · B={total_b}"
                  + ("" if min(total_a, total_b) >= 40 else " · o playbook pede 40+ de cada antes de escolher o vencedor"))
    return texto


# ------------------------------------------------------------------ conversa no grupo
PROMPT_CHAT = """Você é o Chopp 🍺, assistente de vendas da a agência (Ana e Bruno vendem Google Ads para
negócios brasileiros nos EUA, prospectando pelo WhatsApp com a oferta de um vídeo Loom). Hoje é {hoje}.
Você responde no grupo de WhatsApp dos sócios, em português, de forma curta e direta (é WhatsApp),
usando *negrito* para nomes. Use SOMENTE os dados abaixo; se a resposta não estiver neles, diga que não sabe.
Regras: nunca invente leads, números, datas, cidades ou próximas ações. Campo "(sem informação)" significa que
não sabemos: diga isso, não complete. Ao falar de um lead, use exatamente a EMPRESA e o wa.me da MESMA linha.
Se perguntarem de uma empresa que não aparece nos dados, diga que não encontrou. Ao listar leads, inclua o wa.me.

Fluxo do funil: Prospecção enviada → Follow-up → Em conversa → Loom aceito (gravar) → Loom enviado →
Reunião agendada → Reunião feita → Fechado / Perdido. Follow-up a cada {fu} dias, no máximo {maxfu}.
Quando pedirem "o que mandar", use os textos do playbook com os dados reais do lead.

{playbook}

=== NÚMEROS DO FUNIL ===
{contagem}

=== PENDÊNCIAS AGORA ===
{pendencias}

=== LEADS RELEVANTES ===
{leads}

=== CONVERSA RECENTE NO GRUPO ===
{historico}

Pergunta de {autor}: {pergunta}
Resposta:"""

_historico_chat: list[dict] = []
PALAVRAS_VAZIAS = set("""a o as os de da do das dos e em no na nos nas um uma uns umas que qual quais quem com para pra por
sobre como esta está estão tem ja já me te se ele ela eles elas isso esse essa esses essas lead leads agente cliente
clientes quando onde foi vai ser ainda mais menos todos todas hoje ontem semana mes mês dia dias loom reuniao reunião
followup follow up mandar enviar enviou enviado responder respondeu proposta funil status situacao situação""".split())


def _fmt_ts(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%d/%m %H:%M") if ts else "-"


SEM = "(sem informação)"


def _linha_lead(tel: str, r: dict, completa: bool) -> str:
    est = CODIGOS_ANTIGOS.get(r.get("estagio"), r.get("estagio"))
    base = (f"- EMPRESA: {r.get('empresa') or SEM} | CONTATO: {r.get('nome') or SEM} | CIDADE: {r.get('cidade') or SEM}"
            f" | ESTÁGIO: {ROTULO.get(est, est or 'sem card no funil')} | wa.me/{tel}")
    if r.get("temperatura"):
        base += f" | {r['temperatura']}"
    if not completa:
        return base
    extra = [f"  resumo: {r.get('resumo') or SEM}", f"  próxima ação: {r.get('proxima_acao') or SEM}",
             f"  follow-ups feitos: {r.get('followups', 0)} | último contato da agência: {_fmt_ts(r.get('ultimo_agencia_ts'))}"
             f" | última msg do lead: {_fmt_ts(r.get('ultimo_cliente_ts'))}"]
    if r.get("data_reuniao"):
        extra.append(f"  reunião: {r['data_reuniao']}")
    if r.get("loom_aceito_ts"):
        extra.append(f"  aceitou o Loom em: {_fmt_ts(r['loom_aceito_ts'])}")
    if r.get("sugestao_resposta"):
        extra.append(f"  sugestão de resposta: {r['sugestao_resposta']}")
    if r.get("contato_id"):
        notas = _notas_do_contato(r["contato_id"])
        if notas:
            extra.append("  notas no HubSpot:\n" + "\n".join(f"    • {n}" for n in notas))
    return base + "\n" + "\n".join(extra)


def _notas_do_contato(contato_id: str, limite: int = 3, tamanho: int = 900) -> list[str]:
    a = hs("GET", f"/crm/v4/objects/contacts/{contato_id}/associations/notes")
    ids = [str(x["toObjectId"]) for x in a.json().get("results", [])] if a.ok else []
    if not ids:
        return []
    r = hs("POST", "/crm/v3/objects/notes/batch/read", json={
        "properties": ["hs_note_body", "hs_timestamp"], "inputs": [{"id": i} for i in ids[-20:]]})
    notas = sorted(r.json().get("results", []) if r.ok else [], key=lambda n: n["properties"].get("hs_timestamp") or "",
                   reverse=True)[:limite]
    limpas = []
    for n in notas:
        texto = re.sub(r"<br\s*/?>", " | ", n["properties"].get("hs_note_body") or "")
        texto = re.sub(r"<[^>]+>", "", texto)
        limpas.append(re.sub(r"\s+", " ", texto).strip()[:tamanho])
    return limpas


def _buscar_no_hubspot(termos: list[str]) -> list[str]:
    """Leads que não estão no registro (ex.: só importados da planilha): busca por nome/empresa."""
    achados = []
    for termo in termos[:3]:
        r = hs("POST", "/crm/v3/objects/contacts/search", json={
            "query": termo, "limit": 3,
            "properties": ["firstname", "lastname", "company", "phone", "city", "estagio_funil", "hs_lead_status",
                           "resumo_conversa", "proxima_acao", "tipo_negocio", "temperatura_lead"]})
        for c in r.json().get("results", []) if r.ok else []:
            p = c["properties"]
            tel = re.sub(r"\D", "", p.get("phone") or "")
            est = CODIGOS_ANTIGOS.get(p.get("estagio_funil"), p.get("estagio_funil"))
            notas = _notas_do_contato(c["id"], 2)
            achados.append(f"- EMPRESA: {p.get('company') or SEM} | CONTATO: {p.get('firstname') or ''} {p.get('lastname') or ''} | "
                           f"CIDADE: {p.get('city') or SEM} | ESTÁGIO: {ROTULO.get(est, 'sem card no funil')} | "
                           f"status do lead: {p.get('hs_lead_status') or SEM} | ramo: {p.get('tipo_negocio') or SEM} | wa.me/{tel}\n"
                           f"  resumo: {p.get('resumo_conversa') or SEM}\n  próxima ação: {p.get('proxima_acao') or SEM}"
                           + ("\n  notas no HubSpot:\n" + "\n".join(f"    • {n}" for n in notas) if notas else ""))
    return list(dict.fromkeys(achados))


def completar_registro():
    """Leads que só têm o id do HubSpot (ex.: importados da planilha) ganham nome, empresa e resumo."""
    faltando = {r["contato_id"]: tel for tel, r in registro_todos().items()
                if r.get("contato_id") and ((not r.get("empresa") and not r.get("nome")) or "cidade" not in r)}
    ids = list(faltando)
    for i in range(0, len(ids), 100):
        r = hs("POST", "/crm/v3/objects/contacts/batch/read", json={
            "properties": ["firstname", "lastname", "company", "city", "resumo_conversa", "proxima_acao", "temperatura_lead"],
            "inputs": [{"id": x} for x in ids[i:i + 100]]})
        for c in r.json().get("results", []) if r.ok else []:
            p = c["properties"]
            dados = {"nome": " ".join(x for x in (p.get("firstname"), p.get("lastname")) if x),
                     "empresa": p.get("company") or "", "cidade": p.get("city") or ""}
            for de, para in (("resumo_conversa", "resumo"), ("proxima_acao", "proxima_acao"), ("temperatura_lead", "temperatura")):
                if p.get(de):
                    dados[para] = p[de]
            registro_set(faltando[c["id"]], **dados)


def responder_pergunta(pergunta: str, autor: str) -> str:
    try:
        completar_registro()
    except requests.RequestException:
        pass
    reg = {t: r for t, r in registro_todos().items() if not r.get("nao_e_lead") and r.get("estagio")}
    p = calcular_pendencias(encerrar=False)

    contagem = {}
    for r in reg.values():
        est = CODIGOS_ANTIGOS.get(r["estagio"], r["estagio"])
        contagem[ROTULO.get(est, est)] = contagem.get(ROTULO.get(est, est), 0) + 1
    txt_contagem = "\n".join(f"- {k}: {v}" for k, v in contagem.items()) or "- nenhum lead analisado ainda"

    # Leads citados na pergunta: o que bate com MAIS palavras (nome, empresa, cidade) ou com o telefone
    palavras = [w for w in re.findall(r"[\wÀ-ú']{3,}", pergunta.lower()) if w not in PALAVRAS_VAZIAS]
    digitos = re.sub(r"\D", "", pergunta)
    todos = registro_todos()  # inclui quem ainda não tem card (ex.: planilha sem resposta)
    pontos = {}
    for tel, r in todos.items():
        if r.get("nao_e_lead"):
            continue
        alvo = f"{r.get('nome', '')} {r.get('empresa', '')}".lower()
        n = sum(1 for w in palavras if w in alvo)
        if digitos and len(digitos) >= 6 and digitos in tel:
            n += 10
        if n:
            pontos[tel] = n
    melhor = max(pontos.values(), default=0)
    citados = [t for t, n in pontos.items() if n == melhor][:6] if melhor else []
    cobertas = {w for t in citados for w in palavras if w in f"{todos[t].get('nome', '')} {todos[t].get('empresa', '')}".lower()}
    sobrando = [w for w in palavras if w not in cobertas and len(w) >= 4]

    blocos = []
    if citados:
        blocos.append("Leads citados na pergunta:\n" + "\n".join(_linha_lead(t, todos[t], True) for t in citados))
    if sobrando:
        hubspot = _buscar_no_hubspot(sobrando)
        if hubspot:
            blocos.append("Encontrados no HubSpot pelas palavras " + ", ".join(sobrando) + ":\n" + "\n".join(hubspot))
    ativos = sorted(reg.items(), key=lambda kv: kv[1].get("atualizado_em", 0), reverse=True)[:80]
    blocos.append("Todos os leads com card no funil:\n" + "\n".join(_linha_lead(t, r, False) for t, r in ativos))
    txt_leads = "\n\n".join(blocos)

    historico = "\n".join(f"{h['quem']}: {h['texto']}" for h in _historico_chat[-6:]) or "(nenhuma)"
    prompt = PROMPT_CHAT.format(hoje=datetime.now().strftime("%A, %d/%m/%Y %H:%M"), fu=f"{FU_DIAS:g}", maxfu=MAX_FOLLOWUPS,
                                playbook=PLAYBOOK,
                                contagem=txt_contagem, pendencias=texto_relatorio(p), leads=txt_leads,
                                historico=historico, autor=autor or "um sócio", pergunta=pergunta)
    try:
        resposta = ia.chamar(prompt, ctx=16384, limite=2500)
        resposta = re.sub(r"\*\*(.+?)\*\*", r"*\1*", resposta)  # negrito no formato do WhatsApp
        resposta = re.sub(r"^#+\s*", "", resposta, flags=re.M)
    except requests.RequestException as e:
        return f"🍺 Não consegui pensar agora ({str(e)[:80]}). Tenta de novo em instantes."
    _historico_chat.extend([{"quem": autor or "sócio", "texto": pergunta}, {"quem": "Chopp", "texto": resposta[:600]}])
    del _historico_chat[:-12]
    return "🍺 " + resposta


# ------------------------------------------------------------------ rotas
def autorizado() -> bool:
    return request.headers.get("X-Webhook-Secret") == WEBHOOK_SECRET


@app.route("/saude", methods=["GET"])
def saude():
    # "ollama" = a IA configurada está disponível (o nome do campo ficou por compatibilidade com o WhatsApp)
    return jsonify({"cerebro": True, "ollama": ia.disponivel(), "hubspot_ativo": ATUALIZAR_HUBSPOT,
                    "modelo": ia.descricao(), "uso": ia.gasto}), 200


@app.route("/transcrever", methods=["POST"])
def rota_transcrever():
    if not autorizado():
        return jsonify({"erro": "não autorizado"}), 401
    d = request.get_json(silent=True) or {}
    chave = str(d.get("id") or "")
    with _reg_lock:
        cache = _transcricoes()
    if chave and chave in cache:
        return jsonify({"texto": cache[chave], "cache": True})
    try:
        texto = transcrever_audio(base64.b64decode(d.get("dados") or ""))
    except Exception as e:
        print(f"Erro ao transcrever áudio: {e}")
        return jsonify({"texto": "", "erro": str(e)[:200]}), 200
    if chave:
        with _reg_lock:
            cache = _transcricoes()
            cache[chave] = texto
            os.makedirs(DADOS, exist_ok=True)
            with open(TRANSCRICOES_ARQ, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
    return jsonify({"texto": texto})


@app.route("/lembretes", methods=["GET"])
def rota_lembretes():
    if not autorizado():
        return jsonify({"erro": "não autorizado"}), 401
    return jsonify({"avisos": lembretes_de_call()})


@app.route("/metricas", methods=["GET"])
def rota_metricas():
    if not autorizado():
        return jsonify({"erro": "não autorizado"}), 401
    return jsonify({"texto": texto_metricas(int(request.args.get("dias", 7)))})


@app.route("/perguntar", methods=["POST"])
def rota_perguntar():
    if not autorizado():
        return jsonify({"erro": "não autorizado"}), 401
    d = request.get_json(silent=True) or {}
    pergunta = str(d.get("pergunta") or "").strip()
    if not pergunta:
        return jsonify({"resposta": "🍺 Pode perguntar! Ex.: *Chopp, quem pediu o Loom e ainda não recebeu?*"})
    import assistente  # o Chopp: fichas, mensagens, copy de Loom, pesquisa e cadastro
    try:
        return jsonify({"resposta": assistente.responder(pergunta, str(d.get("autor") or ""))})
    except Exception as e:
        print(f"Erro no Chopp: {e}")
        return jsonify({"resposta": f"🍺 Tive um problema para responder ({str(e)[:120]}). Tenta de novo?"})


@app.route("/pendencias", methods=["GET"])
def rota_pendencias():
    if not autorizado():
        return jsonify({"erro": "não autorizado"}), 401
    p = calcular_pendencias()
    return jsonify({"texto": texto_relatorio(p), "itens": p})


def aplicar_analise(telefone: str, msgs: list[dict], a: dict, memoria: dict, nome_whatsapp: str, origem: str,
                    forcar: bool = False) -> tuple[dict, dict]:
    """Grava a análise no HubSpot e no registro local. forcar=True é a auditoria (pode corrigir para trás)."""
    if memoria.get("follow_up_especial"):
        if a["estagio"] in ("REUNIAO_AGENDADA", "REUNIAO_FEITA", "PROPOSTA", "FECHADO_GANHO", "PERDIDO"):
            registro_set(telefone, follow_up_especial=False)  # andou de verdade: sai do follow-up especial
        else:
            a = dict(a, estagio="FOLLOWUP", vez_de="agencia",
                     situacao=memoria.get("situacao") or "⭐ Follow-up especial")
            forcar = True
    if not a["e_lead"]:
        # conversa pessoal que já tinha virado contato: sai do funil (Perdido · não é lead)
        a = dict(a, estagio="PERDIDO", motivo_perda="nao_e_lead", vez_de="ninguem",
                 situacao="Não é lead (conversa pessoal)")
    contam = humanas(msgs)
    ultima = contam[-1] if contam else msgs[-1]
    ultimo_cliente = max((m["quando"] for m in contam if m["de"] == "cliente"), default=0)
    ultimo_agencia = max((m["quando"] for m in msgs if m["de"] == "agencia"), default=0)
    a = dict(a, ultima_ts=ultima["quando"])

    resultado = {"status": "teste", "anterior": memoria.get("estagio"), "nome": "", "empresa": "",
                 "estagio": a["estagio"] if forcar else estagio_final(memoria.get("estagio"), a["estagio"])}
    if ATUALIZAR_HUBSPOT:
        resultado = sincronizar_hubspot(telefone, nome_whatsapp, a, origem, forcar=forcar)

    estagio = resultado["estagio"]
    reg = {"estagio": estagio, "ultima_msg_ts": msgs[-1]["quando"], "ultima_msg_de": ultima["de"],
           "ultimo_cliente_ts": ultimo_cliente, "ultimo_agencia_ts": ultimo_agencia,
           "ultimo_toque": a.get("ultimo_toque") or ultimo_agencia, "followups": a.get("followups", 0),
           "nao_e_lead": not a["e_lead"], "atualizado_em": agora(), "vez_de": a.get("vez_de", ""),
           "situacao": a.get("situacao", ""), "motivo_perda": a.get("motivo_perda", ""),
           "so_automatica": bool(not ultimo_cliente and any(m.get("auto") for m in msgs))}
    for campo in ("resumo", "proxima_acao", "sugestao_resposta", "temperatura", "data_reuniao", "origem"):
        if a.get(campo):
            reg[campo] = a[campo]
    reg["nome"] = resultado.get("nome") or a.get("nome") or memoria.get("nome") or nome_whatsapp
    reg["empresa"] = resultado.get("empresa") or a.get("empresa") or memoria.get("empresa", "")
    if estagio == "LOOM_ACEITO" and (forcar or memoria.get("estagio") != "LOOM_ACEITO"):
        reg["loom_aceito_ts"] = ultimo_cliente or agora()
    reg.update(rastreio_playbook(msgs))
    if estagio == "REUNIAO_AGENDADA" and re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", a.get("data_reuniao_iso") or ""):
        reg["reuniao_iso"] = a["data_reuniao_iso"][:16]
    if not reg["template"] and memoria.get("template"):
        reg["template"] = memoria["template"]
    reg["respondeu"] = bool(ultimo_cliente)
    if estagio == "PERDIDO" and a.get("motivo_perda") == "sem_resposta":
        if forcar or not memoria.get("frio_desde"):
            reg["frio_desde"] = a.get("ultimo_toque") or agora()   # playbook: lead Frio, retomar em 60 dias
    elif memoria.get("frio_desde"):
        reg["frio_desde"] = 0         # voltou a conversar
    registro_set(telefone, **reg)
    return resultado, reg


@app.route("/analisar", methods=["POST"])
def rota_analisar():
    if not autorizado():
        return jsonify({"erro": "não autorizado"}), 401
    d = request.get_json(silent=True) or {}
    telefone = re.sub(r"\D", "", str(d.get("phone") or ""))
    msgs = normalizar_mensagens(d.get("mensagens") or [])
    origem = d.get("origem") or "ao_vivo"
    nome_whatsapp = (d.get("nome_whatsapp") or "").strip()
    if not telefone or not msgs:
        return jsonify({"erro": "faltou o telefone ou as mensagens"}), 400

    with lock_do(telefone):
        memoria = registro_get(telefone)
        ultima_ts = msgs[-1]["quando"]
        if memoria.get("contato_id") or any(m["de"] == "cliente" for m in msgs) or parece_prospeccao(msgs):
            import assistente
            assistente.salvar_conversa(telefone, msgs)  # o Chopp usa nas fichas e mensagens
        # Nada mudou desde a última análise: não gasta IA
        if memoria.get("ultima_msg_ts") == ultima_ts and not d.get("forcar"):
            return jsonify({"status": "sem_mudanca", "estagio": memoria.get("estagio")}), 200

        so_agencia = not any(m["de"] == "cliente" for m in msgs)
        if so_agencia and not memoria.get("contato_id") and not parece_prospeccao(msgs):
            registro_set(telefone, ultima_msg_ts=ultima_ts, nao_e_lead=True)
            return jsonify({"status": "ignorado_nao_e_prospeccao"}), 200

        if telefone in nao_sao_leads() and not memoria.get("contato_id"):
            registro_set(telefone, ultima_msg_ts=ultima_ts, nao_e_lead=True)
            return jsonify({"status": "ignorado_nao_e_lead"}), 200
        inicio = agora()
        a = analisar(msgs, memoria)
        if a is None:
            return jsonify({"status": "falha_na_analise"}), 200
        if telefone in nao_sao_leads():
            a["e_lead"] = False
        if not a["e_lead"] and not memoria.get("contato_id"):
            registro_set(telefone, ultima_msg_ts=ultima_ts, nao_e_lead=True)
            return jsonify({"status": "ignorado_nao_e_lead"}), 200
        try:
            resultado, reg = aplicar_analise(telefone, msgs, a, memoria, nome_whatsapp, origem)
        except (requests.RequestException, RuntimeError, KeyError) as e:
            print(f"Erro ao sincronizar com o HubSpot: {e}")
            return jsonify({"status": "erro_hubspot", "erro": str(e)[:300]}), 200
        print(f"{telefone} -> {resultado['estagio']:18} {'IA' if a['usou_ia'] else 'regra'} "
              f"{agora() - inicio:4.1f}s | vez: {a.get('vez_de')} | {a.get('situacao', '')[:70]}")
        estagio = resultado["estagio"]
        mudou = estagio != memoria.get("estagio")
        alerta = None
        if mudou and origem == "ao_vivo":
            alerta = alerta_imediato(telefone, memoria.get("estagio"), estagio, a, {**memoria, **reg})

    return jsonify({**resultado, "mudou": mudou, "alerta": alerta, "resumo": a.get("resumo", ""),
                    "usou_ia": a["usou_ia"]}), 200


if __name__ == "__main__":
    print(f"Cérebro no ar | IA: {ia.descricao()} | HubSpot: {'ATIVO' if ATUALIZAR_HUBSPOT else 'modo teste'}")
    app.run(host="127.0.0.1", port=int(os.environ.get("PORTA_CEREBRO", "5000")), threaded=True)
