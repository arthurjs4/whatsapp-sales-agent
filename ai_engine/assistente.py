"""
Chopp 🍺 — o assistente de vendas da agência no grupo do WhatsApp.

O que ele faz quando chamado ("Chopp, ..."):
  - Monta a FICHA do lead: HubSpot (contato, card, notas) + conversa do WhatsApp + histórico do playbook
  - Orienta o próximo passo de cada lead, conforme a coluna em que ele está
  - Escreve mensagens prontas (follow-up, reativação, Loom não assistido...)
  - Faz plano de reativação de uma coluna inteira (ex.: todos os Perdidos)
  - Escreve a copy/roteiro do Loom seguindo o playbook (Etapa 4)
  - Pesquisa o lead na internet e só salva o que passar na conferência dupla
  - Cadastra lead novo no HubSpot a partir de uma mensagem no grupo
"""
import json
import os
import re
import time
from datetime import datetime
from urllib.parse import urlparse

import requests

import brain
import ia
from brain import (CODIGOS_ANTIGOS, DADOS, ETAPAS, MOTIVOS_PERDA, ORDEM, PLAYBOOK, ROTULO, SEM,
                   hs, ids_do_funil, registro_get, registro_set, registro_todos)

NOME = "Chopp"
CONVERSAS_DIR = os.path.join(DADOS, "conversas")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}


# =========================================================================== IA
def llm(prompt: str, json_mode: bool = False, ctx: int = 16384, temperatura: float = 0.3, limite: int = 2500) -> str:
    """Chama a IA configurada em ia.py (Claude ou Ollama). 'temperatura' fica só por compatibilidade."""
    return ia.chamar(prompt, json_saida=json_mode, ctx=ctx, limite=limite)


def para_whatsapp(texto: str) -> str:
    texto = re.sub(r"\*\*(.+?)\*\*", r"*\1*", texto)
    texto = re.sub(r"^#+\s*", "", texto, flags=re.M)
    return texto.strip()


# =========================================================================== conversas guardadas
def salvar_conversa(telefone: str, msgs: list[dict]):
    os.makedirs(CONVERSAS_DIR, exist_ok=True)
    with open(os.path.join(CONVERSAS_DIR, f"{telefone}.json"), "w", encoding="utf-8") as f:
        json.dump(msgs[-60:], f, ensure_ascii=False)


def ler_conversa(telefone: str, n: int = 20) -> list[dict]:
    try:
        with open(os.path.join(CONVERSAS_DIR, f"{telefone}.json"), encoding="utf-8") as f:
            return json.load(f)[-n:]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


# =========================================================================== estratégia por coluna
ESTRATEGIA = {
    "PROSPECCAO": "Objetivo: conseguir a 1ª resposta. Esperar até o Dia 3. Se cair em recepcionista/número geral, "
                  "perguntar 'Falo com o dono ou com quem cuida do marketing?'. Anotar o template (A/B).",
    "FOLLOWUP": "Objetivo: resposta. Dia 3: trazer algo novo (busca refeita, concorrente aparecendo antes) e oferecer o "
                "vídeo de 4 min. Dia 5: último, leve ('última mensagem pra não te encher'). Sem resposta: Lead Frio, "
                "retomar em 60 dias com uma observação nova.",
    "EM_CONVERSA": "Objetivo: o lead pedir o vídeo ou aceitar ver ao vivo. Responder rápido, com um detalhe real do negócio, "
                   "e oferecer o vídeo de 4 min (ou chamada ao vivo de 10 min). Se for recepção, achar o dono.",
    "AGUARDANDO_RETORNO": "A vez é do LEAD (vai analisar, falar com sócio/gerente, encaminhou para outra pessoa). Não "
                          "pressionar. Depois de 3 dias sem retorno: mensagem curta e gentil perguntando se conseguiu ver, "
                          "oferecendo ajuda (ex.: falar direto com a pessoa que decide). Se foi recepção/robô que "
                          "encaminhou, tentar achar o contato do dono. Após 3 cobranças sem resposta: Frio (60 dias).",
    "LOOM_ACEITO": "Objetivo: reservar a call AGORA (Etapa 3) e entregar o Loom até as 10h do dia seguinte. Opção 1 "
                   "(reservar dia/hora nos próximos 2-3 dias úteis) ou Opção 2 (gravo ou mostro ao vivo?). Aceitou "
                   "reserva: convite no Google Agenda na hora. Recusou: gravar mesmo assim e marcar como Morno.",
    "LOOM_ENVIADO": "Objetivo: marcar a call de 15 min. Ver nas estatísticas do Loom se assistiu e até onde. Dia +1: se não "
                    "assistiu, 'Conseguiu dar uma olhada? Se preferir, te resumo em 2 linhas aqui mesmo'; se assistiu, "
                    "mandar informação nova com print e propor 2 horários. Dia +4: 'Faz sentido pra vocês agora ou "
                    "deixo pra outro momento?'. Dia +10: encerramento educado (só se for verdade). Sem tempo "
                    "para call: proposta por áudio de 2 min.",
    "REUNIAO_AGENDADA": "Objetivo: o lead comparecer e fechar na call. Convite no Google Agenda, lembrete 1h antes, "
                        "proposta e contrato prontos, planilha com volume de buscas e custo por clique preenchidos.",
    "REUNIAO_FEITA": "Objetivo: fechar. Combinar data e hora exatas para a resposta (nunca 'me avisa'), mandar resumo em "
                     "texto no mesmo dia com a conta feita na call, e cobrar na data combinada.",
    "PROPOSTA": "Proposta enviada por escrito: a vez é do lead decidir. Após 3 dias: 'Conseguiram analisar a proposta? "
                "Se ficou alguma dúvida, te explico em 15 minutos.' Sempre puxar para a conversa de 15 min com quem "
                "decide (gerente/dono), com data e hora exatas.",
    "FECHADO_GANHO": "Objetivo: começar bem. Setup no dia seguinte e relatório periódico no "
                     "WhatsApp (gasto, contatos, custo por contato).",
    "PERDIDO": "Depende do motivo. Frio (sem resposta): retomar após 60 dias com uma observação NOVA (busca refeita, "
               "concorrente novo anunciando, avaliação recente, época do ano). Achou caro: depois de 60-90 dias, "
               "mostrar a conta de retorno por áudio de 2 min. Fechou com outra empresa: em 60-90 dias perguntar como "
               "está indo e oferecer uma segunda opinião com a busca ao vivo. Sem interesse: o playbook manda não "
               "insistir; só reabrir após 90+ dias se houver uma novidade realmente relevante, com 1 mensagem leve.",
}

LOOM_ROTEIRO = """Roteiro do Loom (playbook, Etapa 4, máximo 4 minutos, narração em português, buscas no idioma local):
0:00-0:20 Gancho com o concorrente: "[Nome], quando alguém em [cidade] procura [serviço] hoje, quem aparece primeiro é o
          [concorrente], com anúncio. Vocês, com [X] avaliações, aparecem em [posição]. Deixa eu te mostrar."
0:20-1:00 Busca ao vivo: pesquisar 2 ou 3 termos, mostrar quem aparece e quem está Patrocinado.
1:00-1:50 2 ou 3 achados no site e no perfil (sem WhatsApp, sem preço, 0 anúncios, landing parada). SÓ fatos verificados.
1:50-2:30 Tamanho do mercado: "Cerca de [X] buscas por mês, clique entre [A] e [B] dólares" (Keyword Planner). Nunca promessa.
2:30-2:50 Dica grátis: uma melhoria simples (ex.: link de WhatsApp no site).
2:50-3:20 Condições do serviço: [preencher com as condições reais da sua oferta].
3:20-4:00 O que fica para a call + chamada: "O que não consigo calcular daqui é quanto vale um cliente pra vocês. Na call de
          15 min, com esse número, te mostro quantos clientes por mês pagam a campanha. [Dia] ou [dia], qual fica melhor?"
Antes de gravar: Keyword Planner (volume e custo por clique de 3-4 termos), print de quem aparece Patrocinado,
Central de Transparência (0 anúncios no domínio), revisar o site ao vivo, câmera no canto, texto fixo com as 2 datas da call."""


# =========================================================================== ficha do lead
PROPS_FICHA = ["firstname", "lastname", "company", "jobtitle", "phone", "email", "website", "city", "state", "address",
               "instagram", "tipo_negocio", "servico_interesse", "orcamento_anuncios", "hs_lead_status", "lifecyclestage",
               "estagio_funil", "temperatura_lead", "score_lead", "origem_lead", "followups_enviados", "resumo_conversa",
               "proxima_acao", "data_reuniao", "pesquisa_verificada", "diagnostico_site"]
_rotulos_cache: dict = {}


def rotulos_ao_vivo() -> dict:
    """id da coluna -> nome atual da coluna no HubSpot (funciona mesmo se renomearem)."""
    if not _rotulos_cache:
        r = hs("GET", "/crm/v3/pipelines/deals")
        for p in r.json().get("results", []) if r.ok else []:
            for s in p["stages"]:
                _rotulos_cache[s["id"]] = s["label"]
    return _rotulos_cache


def contato_do_lead(telefone: str) -> dict | None:
    reg = registro_get(telefone)
    if reg.get("contato_id"):
        r = hs("GET", f"/crm/v3/objects/contacts/{reg['contato_id']}", params={"properties": ",".join(PROPS_FICHA)})
        if r.ok:
            return r.json()
    c = brain.buscar_contato(telefone)
    if c:
        r = hs("GET", f"/crm/v3/objects/contacts/{c['id']}", params={"properties": ",".join(PROPS_FICHA)})
        return r.json() if r.ok else c
    return None


def negocio_do_lead(contato_id: str, telefone: str) -> dict | None:
    pid, _ = ids_do_funil()
    d = brain.buscar_negocio(contato_id, pid, telefone)
    if not d:
        return None
    r = hs("GET", f"/crm/v3/objects/deals/{d['id']}",
           params={"properties": "dealname,dealstage,amount,closed_lost_reason,description,proxima_acao,temperatura_lead"})
    return r.json() if r.ok else d


def fatos_chave(p: dict, notas: list[str]) -> dict:
    """Fatos já verificados (auditoria da planilha, pesquisa do Chopp, diagnóstico do site), lidos pelo código."""
    texto = " | ".join(notas or []) + " | " + (p.get("pesquisa_verificada") or "")
    f = {}
    m = re.search(r"\bNota: ([\d.,]+)", texto) or re.search(r"nota m[ée]dia no Google: ([\d.,]+)", texto)
    if m:
        f["nota"] = m.group(1).rstrip(".,")
    m = re.search(r"Avaliacoes: (\d+)", texto) or re.search(r"n[úu]mero de avalia[çc][õo]es no Google: (\d+)", texto)
    if m:
        f["avaliacoes"] = m.group(1)
    m = re.search(r"Anuncia Google Ads\?: (N[ãa]o|Sim)", texto, re.I)
    if m:
        f["anuncia"] = m.group(1).lower().startswith("s")
        d = re.search(r"Evidencia de anuncios: [^|]*?(\d{2}/\d{2}/\d{4})", texto)
        f["anuncia_data"] = d.group(1) if d else ""
    m = re.search(r"Dominio do site: ([^\s|]+)", texto)
    if m:
        f["dominio"] = m.group(1)
    m = re.search(r"Diagnostico do site: ([^|]+)", texto)
    if m and not re.search(r"pendente|bloque|n/?a\b|sem site", m.group(1), re.I):
        f["diag_planilha"] = m.group(1).strip()[:300]
    nome = p.get("firstname") or ""
    if nome and nome.lower() in (p.get("company") or "").lower():
        nome = ""
    f["decisor"] = nome
    f["achados"] = _achados_verificados(p)
    if f.get("anuncia") is False:
        f["achados"].append("0 anúncios do domínio na Central de Transparência do Google"
                            + (f" ({f['anuncia_data']})" if f.get("anuncia_data") else ""))
    return f


def _linha_fatos(f: dict) -> str:
    itens = []
    if f.get("nota") or f.get("avaliacoes"):
        itens.append(f"nota {f.get('nota', '?')} com {f.get('avaliacoes', '?')} avaliações no Google")
    itens += f.get("achados", [])
    if f.get("anuncia"):
        itens.append("já anuncia no Google")
    elif "anuncia" not in f:
        itens.append("anúncios no Google: NÃO verificado (não afirme se anuncia ou não)")
    if f.get("diag_planilha"):
        itens.append(f"auditoria do site: {f['diag_planilha']}")
    return "; ".join(itens) or SEM


def ficha(telefone: str, completa: bool = True) -> str:
    reg = registro_get(telefone)
    c = contato_do_lead(telefone)
    p = (c or {}).get("properties", {})
    notas = brain._notas_do_contato(c["id"], 4, 5000) if c else []
    d = negocio_do_lead(c["id"], telefone) if c else None
    dp = (d or {}).get("properties", {})
    coluna = rotulos_ao_vivo().get(dp.get("dealstage"), "") if d else ""
    est = CODIGOS_ANTIGOS.get(reg.get("estagio"), reg.get("estagio"))
    if not coluna:
        coluna = ROTULO.get(est, "sem card no funil")

    def v(x):
        return x if x not in (None, "") else SEM

    nome = " ".join(x for x in (p.get("firstname"), p.get("lastname")) if x)
    empresa_nome = (p.get("company") or "").lower()
    if nome and empresa_nome and (nome.lower() in empresa_nome or empresa_nome in nome.lower()):
        nome = ""  # o "nome" é só a empresa repetida: não sabemos quem é o decisor
    linhas = [f"### {p.get('company') or reg.get('empresa') or nome or '+' + telefone}  (wa.me/{telefone})",
              f"Coluna no funil: {coluna} | código: {est or '-'} | status do lead: {v(p.get('hs_lead_status'))} | "
              f"temperatura: {v(p.get('temperatura_lead') or reg.get('temperatura'))}",
              f"Contato/decisor: {v(nome)} ({v(p.get('jobtitle'))}) | cidade: {v(p.get('city'))}/{v(p.get('state'))} | "
              f"ramo: {v(p.get('tipo_negocio'))}",
              f"Site: {v(p.get('website'))} | Instagram: {v(p.get('instagram'))} | e-mail: {v(p.get('email'))}",
              f"Diagnóstico do site: {v(p.get('diagnostico_site'))}",
              f"FATOS-CHAVE verificados (use para personalizar): {_linha_fatos(fatos_chave(p, notas))}"]
    if d:
        linhas.append(f"Card: {dp.get('dealname')} | valor: {v(dp.get('amount'))} | motivo de perda: {v(dp.get('closed_lost_reason'))}")
    linhas += [f"Resumo: {v(p.get('resumo_conversa') or reg.get('resumo'))}",
               f"Próxima ação registrada: {v(p.get('proxima_acao') or reg.get('proxima_acao'))}"]
    hist = []
    if reg.get("template"):
        hist.append(f"template da 1ª msg: {reg['template']}")
    if reg.get("primeiro_ts"):
        hist.append(f"1º contato: {brain._fmt_ts(reg['primeiro_ts'])}")
    hist.append(f"follow-ups de prospecção feitos: {reg.get('followups', 0)}")
    if reg.get("loom_aceito_ts"):
        hist.append(f"pediu o Loom em {brain._fmt_ts(reg['loom_aceito_ts'])}")
    if reg.get("loom_enviado_ts"):
        dias = (time.time() - reg["loom_enviado_ts"]) / 86400
        hist.append(f"Loom enviado em {brain._fmt_ts(reg['loom_enviado_ts'])} (há {dias:.0f} dias), "
                    f"follow-ups pós-Loom: {reg.get('toques_pos_loom', 0)}, respondeu depois do Loom: "
                    f"{'sim' if reg.get('cliente_pos_loom') else 'não'}")
    if reg.get("data_reuniao"):
        hist.append(f"call: {reg['data_reuniao']}")
    if reg.get("frio_desde"):
        hist.append(f"frio desde {brain._fmt_ts(reg['frio_desde'])}")
    linhas.append("Histórico do playbook: " + "; ".join(hist))
    if completa:
        if p.get("pesquisa_verificada"):
            linhas.append(f"Pesquisa verificada: {p['pesquisa_verificada'][:1500]}")
        if notas:
            linhas.append("Notas no HubSpot:\n" + "\n".join(f"  • {n[:900]}" for n in notas))
        conversa = ler_conversa(telefone, 20)
        if conversa:
            linhas.append("Últimas mensagens no WhatsApp:\n" + "\n".join(
                f"  [{brain._fmt_ts(m.get('quando'))}] {'Agência' if m.get('de') == 'agencia' else 'Lead'}: "
                f"{str(m.get('texto'))[:300]}" for m in conversa))
        else:
            linhas.append("Últimas mensagens no WhatsApp: (ainda não analisadas pelo Chopp)")
    return "\n".join(linhas)


# =========================================================================== achar leads citados
VAZIAS = set(brain.PALAVRAS_VAZIAS) | set("""chopp chop monte monta faça faca crie cria escreva mande manda mensagem
texto copy roteiro pesquise pesquisa internet cadastre cadastra adicione novo nova prosseguir proseguir prosigo
devo fazer reativar reativação reativacao recuperar todos todas aba kanban coluna etapa perdido perdidos frio frios
porque pois ainda viu nao não vi assistiu enviei mandei quero queria ele dele dela pro pra para""".split())


def achar_leads(texto: str, limite: int = 4) -> list[str]:
    palavras = [w for w in re.findall(r"[\wÀ-ú']{3,}", texto.lower()) if w not in VAZIAS]
    digitos = re.sub(r"\D", "", texto)
    pontos = {}
    for tel, r in registro_todos().items():
        if r.get("nao_e_lead"):
            continue
        alvo = f"{r.get('nome', '')} {r.get('empresa', '')}".lower()
        n = sum(1 for w in palavras if w in alvo)
        if digitos and len(digitos) >= 6 and digitos in tel:
            n += 10
        if n:
            pontos[tel] = n
    if pontos:
        melhor = max(pontos.values())
        return [t for t, n in sorted(pontos.items(), key=lambda kv: -kv[1]) if n == melhor][:limite]
    # Não está no registro: procura no HubSpot
    if not palavras:
        return []
    r = hs("POST", "/crm/v3/objects/contacts/search",
           json={"query": " ".join(palavras[:4]), "limit": limite, "properties": ["phone", "company", "firstname"]})
    achados = []
    for c in r.json().get("results", []) if r.ok else []:
        tel = re.sub(r"\D", "", c["properties"].get("phone") or "")
        if tel:
            registro_set(tel, contato_id=c["id"], empresa=c["properties"].get("company") or "",
                         nome=c["properties"].get("firstname") or "")
            achados.append(tel)
    return achados


ETAPAS_TEXTO = [
    (r"perdid|frios?\b|reativ|recuper", "PERDIDO"), (r"prospec", "PROSPECCAO"), (r"follow", "FOLLOWUP"),
    (r"em conversa", "EM_CONVERSA"), (r"aguardando|esperando retorno|cobrar retorno", "AGUARDANDO_RETORNO"),
    (r"propostas?( enviadas?)?\b", "PROPOSTA"),
    (r"loom aceito|pediram o (v[ií]deo|loom)|gravar", "LOOM_ACEITO"),
    (r"loom enviado|receberam o loom|enviei o loom pra todos", "LOOM_ENVIADO"),
    (r"reuni[aã]o agendada|calls? marcad|calls? agendad", "REUNIAO_AGENDADA"),
    (r"reuni[aã]o feita|calls? feita", "REUNIAO_FEITA"), (r"fechad|ganho|clientes ativos", "FECHADO_GANHO"),
]


def etapa_em_massa(texto: str) -> str | None:
    t = texto.lower()
    if not re.search(r"todos|todas|aba|kanban|coluna|etapa|fase|que est[aã]o|leads? (perdid|frio|em oportunidade)", t):
        return None
    if "oportunidade" in t:
        return "OPORTUNIDADE"  # fase do ciclo de vida no HubSpot (todo contato com card)
    for padrao, cod in ETAPAS_TEXTO:
        if re.search(padrao, t):
            return cod
    return None


def leads_na_etapa(codigo: str, limite: int = 25) -> list[str]:
    """Busca no HubSpot (fonte da verdade) todos os cards de uma coluna e devolve os telefones."""
    if codigo == "OPORTUNIDADE":
        r = hs("POST", "/crm/v3/objects/contacts/search", json={
            "filterGroups": [{"filters": [{"propertyName": "lifecyclestage", "operator": "EQ", "value": "opportunity"}]}],
            "limit": 100, "properties": ["phone", "company", "firstname"]})
        tels = []
        for c in r.json().get("results", []) if r.ok else []:
            tel = re.sub(r"\D", "", c["properties"].get("phone") or "")
            if tel:
                if not registro_get(tel).get("contato_id"):
                    registro_set(tel, contato_id=c["id"], empresa=c["properties"].get("company") or "")
                tels.append(tel)
        return tels[:limite]
    _, etapas = ids_do_funil()
    r = hs("POST", "/crm/v3/objects/deals/search", json={
        "filterGroups": [{"filters": [{"propertyName": "dealstage", "operator": "EQ", "value": etapas[codigo]}]}],
        "limit": 100, "properties": ["dealname"]})
    ids = [d["id"] for d in r.json().get("results", [])] if r.ok else []
    if not ids:
        return []
    a = hs("POST", "/crm/v4/associations/deals/contacts/batch/read", json={"inputs": [{"id": i} for i in ids]})
    contatos = [str(x["to"][0]["toObjectId"]) for x in a.json().get("results", []) if x.get("to")] if a.ok else []
    if not contatos:
        return []
    b = hs("POST", "/crm/v3/objects/contacts/batch/read", json={
        "properties": ["phone", "company", "firstname"], "inputs": [{"id": i} for i in contatos[:100]]})
    tels = []
    for c in b.json().get("results", []) if b.ok else []:
        tel = re.sub(r"\D", "", c["properties"].get("phone") or "")
        if tel:
            if not registro_get(tel).get("contato_id"):
                registro_set(tel, contato_id=c["id"], empresa=c["properties"].get("company") or "")
            tels.append(tel)
    return tels[:limite]


# =========================================================================== respostas com IA
PROMPT_ASSISTENTE = """Você é o {nome} 🍺, assistente de vendas da a agência (Ana e Bruno vendem gestão de Google Ads
para negócios brasileiros nos EUA, prospectando pelo WhatsApp com a oferta de um vídeo Loom de 4 min). Hoje é {hoje}.
Responda no grupo de WhatsApp dos sócios, em português, direto e prático. Use *negrito* (um asterisco) e listas curtas.

{playbook}

Estratégia por coluna do funil:
{estrategias}

REGRAS IMPORTANTES:
- Use SOMENTE os fatos das fichas. Nunca invente nome, número, avaliação, concorrente, data ou detalhe do site.
  Quando faltar um dado para a mensagem, deixe um campo entre colchetes, ex.: [concorrente], [minuto], [link].
- Mensagens para leads: no idioma em que o lead conversa, tom humano, curtas, sem "IA"/"automação"/"agente", chamando
  a call de "conversa de 15 minutos". Coloque cada mensagem pronta em itálico (_texto_) para facilitar copiar.
- Respeite o histórico: não repita uma mensagem que a agência já mandou; leve em conta quantos follow-ups já foram feitos.
- Se o playbook disser para NÃO mandar nada agora (ex.: lead pediu para encerrar), diga isso.

{tarefa}

=== FICHAS ===
{fichas}

=== CONVERSA RECENTE NO GRUPO ===
{historico}

Pedido de {autor}: {pedido}
Resposta:"""

TAREFAS_TEXTO = {
    "orientar": "TAREFA: diga em que ponto cada lead está, o objetivo da etapa e o PASSO A PASSO do que fazer agora, "
                "com a mensagem pronta quando couber.",
    "mensagem": """TAREFA: escreva a mensagem pronta pedida, específica para o lead.
1) Identifique a SITUAÇÃO descrita no pedido e use a mensagem do playbook para ELA:
   - Loom enviado e NÃO assistido -> A = "Oi, tudo bem? Conseguiu dar uma olhada no vídeo que te mandei? Se preferir,
     te resumo em 2 linhas aqui mesmo." | B = o resumo em 2 linhas usando 1 dos FATOS-CHAVE (ex.: "vocês têm nota 5
     no Google, mas 0 anúncios: quem procura [serviço] em [cidade] vê o concorrente antes"). Não mande o link de novo.
   - Loom assistido e sem resposta -> informação nova (print/dado da ficha) + 2 opções de horário para os 15 min.
   - Loom enviado há 4+ dias -> "Faz sentido pra vocês agora ou deixo pra outro momento?"
   - Prospecção sem resposta -> follow-up do Dia 3 (algo novo) ou Dia 5 (último), conforme os follow-ups já feitos.
   - Pediu o vídeo -> reservar a call (Opção 1 ou 2). Lead frio/perdido -> retomada com uma observação NOVA.
   NUNCA reenvie a mensagem de envio do Loom ("gravei! São 4 min...") se o Loom já foi enviado.
2) Personalize com 1 fato da linha FATOS-CHAVE da ficha (ex.: 0 anúncios, site sem WhatsApp, nota/avaliações).
3) Trate a pessoa pelo primeiro nome do DECISOR. Se a ficha não tiver o nome de uma pessoa, comece com "Oi, tudo bem?"
   (nunca chame a pessoa pelo nome da empresa).
4) Preencha com os dados que a ficha JÁ TEM (cidade, ramo/serviço, site, nome). Colchetes só para o que não existe.
5) Dê 2 versões DIFERENTES: A (a do playbook, completa) e B (até 2 linhas, que OBRIGATORIAMENTE cita 1 dos
   FATOS-CHAVE com o número/dado concreto e termina com uma pergunta curta). Seja breve, exatamente neste formato:
   *Mensagem para [empresa]*
   *A:* _texto_
   *B:* _texto_
   *Quando mandar:* 1 linha
6) "Quando mandar": siga os dias do playbook a partir das datas do histórico (ex.: Dia +1 = 1 dia depois do envio
   do Loom). Se a data não estiver na ficha, diga isso.""",
    "massa": "TAREFA: plano para TODOS os leads da coluna listados nas fichas. Agrupe por situação (ex.: motivo de perda, "
             "dias parado). Para cada lead: 1 linha de diagnóstico + quando agir + mensagem pronta curta e específica. "
             "No fim, as 2 ou 3 regras gerais que valem para o grupo.",
    "geral": "TAREFA: responda à pergunta usando as fichas e o playbook.",
}


def _fmt_historico(h: list[dict]) -> str:
    return "\n".join(f"{x['quem']}: {x['texto']}" for x in h[-6:]) or "(nenhuma)"


def _achados_verificados(p: dict) -> list[str]:
    """Achados do site que o Chopp conferiu de verdade (diagnóstico pelo HTML)."""
    diag = (p.get("diagnostico_site") or "").lower()
    achados = []
    if "whatsapp: não" in diag:
        achados.append("o site não tem link de WhatsApp")
    if "mostra preço: não" in diag:
        achados.append("o site não mostra preço")
    if "só por formulário" in diag:
        achados.append("o contato no site é só por formulário")
    return achados


FALAS_ACHADO = {
    "o site não tem link de WhatsApp": "Entrei no site de vocês e não achei um botão de WhatsApp.",
    "o site não mostra preço": "O site não mostra preço nem uma faixa de valores.",
    "o contato no site é só por formulário": "O contato no site é só por formulário.",
}
PROMPT_TERMOS = """Negócio: {empresa}. Ramo: {ramo}. Cidade: {cidade}, {estado}.
Devolva só este JSON:
{{"servico": "o que um cliente procura, em português, curto (ex.: 'jiu-jitsu', 'salgados para festa')",
  "nicho": "o nicho no singular com artigo, em português (ex.: 'uma academia de luta', 'um buffet')",
  "idioma": "idioma das buscas locais (inglês nos EUA; francês em Quebec; espanhol em Porto Rico)",
  "termos": ["3 buscas que um morador digitaria no Google, NO IDIOMA LOCAL, com o nome da cidade"]}}"""


def copy_loom(telefone: str) -> str:
    """Copy do Loom montada pelo CÓDIGO com o roteiro do playbook: só entram fatos verificados da ficha.
    O modelo só sugere os termos de busca. O resto que falta vira [preencher: ...]."""
    c = contato_do_lead(telefone)
    if not c:
        return f"🍺 Não achei esse lead no CRM (wa.me/{telefone})."
    p = c["properties"]
    f = fatos_chave(p, brain._notas_do_contato(c["id"], 6, 5000))
    empresa = p.get("company") or p.get("firstname") or "o lead"
    cidade = p.get("city") or "[cidade]"
    try:
        t = json.loads(llm(PROMPT_TERMOS.format(empresa=empresa, ramo=p.get("tipo_negocio") or "não informado",
                                                cidade=cidade, estado=p.get("state") or ""),
                           json_mode=True, temperatura=0, limite=300, ctx=4096))
    except (requests.RequestException, ValueError):
        t = {}
    servico = t.get("servico") or "[preencher: serviço]"
    nicho = t.get("nicho") or "[preencher: nicho]"
    termos = [x for x in (t.get("termos") or []) if isinstance(x, str)][:3]
    nome = f["decisor"]
    vocativo = f"{nome}, q" if nome else "Q"

    if f.get("avaliacoes"):
        aval = f"Vocês, com {f['avaliacoes']} avaliações" + (f" e nota {f['nota']}" if f.get("nota") else "")
    else:
        aval = "Vocês, com [preencher: nº de avaliações] avaliações"
    falas = [FALAS_ACHADO.get(a) for a in f["achados"] if a in FALAS_ACHADO]
    if f.get("anuncia") is False:
        falas.append("Olhei na Central de Transparência do Google: hoje não tem nenhum anúncio rodando no site de vocês.")
    obs_planilha = f"\n   ℹ️ Da auditoria da planilha (confira e transforme em fala): {f['diag_planilha']}" if f.get("diag_planilha") else ""
    while len(falas) < 2:
        falas.append("[preencher: achado verificado ao revisar o site/perfil ao vivo]")
    dica = ("Uma dica que vocês podem fazer hoje, de graça: colocar um botão de WhatsApp no topo do site."
            if "o site não tem link de WhatsApp" in f["achados"] else
            "Uma dica que vocês podem fazer hoje, de graça: [preencher: melhoria simples que você viu, ex.: botão de WhatsApp].")

    linhas = [
        f"🎬 *Copy do Loom · {empresa}*",
        f"Máx. 4 min · narração em português · buscas em {t.get('idioma') or '[idioma local]'}",
        "",
        "*0:00–0:20 · Gancho*",
        f"_\"{vocativo}uando alguém em {cidade} procura {servico} hoje, quem aparece primeiro é o "
        f"[preencher: concorrente], com anúncio. {aval}, aparecem em [preencher: posição]. Deixa eu te mostrar.\"_",
        "",
        "*0:20–1:00 · Busca ao vivo*",
        "Pesquise: " + (", ".join(f"\"{x}\"" for x in termos) if termos else "[preencher: 2-3 termos no idioma local]"),
        "_\"Olha quem aparece quando alguém procura isso. Esses com 'Patrocinado' estão pagando pra aparecer na frente.\"_",
        "",
        "*1:00–1:50 · Achados (só fatos verificados)*",
        *[f"_\"{x}\"_" for x in falas[:3]],
    ]
    if obs_planilha:
        linhas.append(obs_planilha.strip("\n"))
    linhas += [
        "",
        "*1:50–2:30 · Tamanho do mercado* (Keyword Planner, nunca prometa resultado)",
        f"_\"São cerca de [preencher: buscas/mês] buscas por mês por isso em {cidade}, com clique entre "
        f"[preencher: US$ mín] e [preencher: US$ máx].\"_",
        "",
        "*2:30–2:50 · Dica grátis*",
        f"_\"{dica}\"_",
        "",
        "*2:50–3:20 · Condições do serviço*",
        "_\"[preencher: condições reais da sua oferta]\"_",
        "",
        "*3:20–4:00 · O que fica para a conversa + chamada*",
        "_\"O que eu não consigo calcular daqui é quanto vale um cliente pra vocês. Numa conversa de 15 minutos, com esse "
        "número, te mostro quantos clientes por mês pagam a campanha. [Dia] ou [dia], qual fica melhor?\"_",
        "",
        "*Mensagem de envio (Dia 0):*",
        f"_{nome + ', g' if nome else 'G'}ravei! São 4 min, o ponto principal está em 1:50: [link]. [Dia] às [hora] ou "
        f"[dia] às [hora], qual fica melhor pra conversarmos 15 min?_",
        "",
        checklist_loom(p, f),
    ]
    return "\n".join(linhas)


def checklist_loom(p: dict, f: dict) -> str:
    """Checklist 'antes de gravar' montado pelo código a partir da ficha (o LLM não decide o que está feito)."""
    def m(ok, texto):
        return f"{'✅' if ok else '⬜'} {texto}"

    site = p.get("website") or f.get("dominio")
    itens = [
        m(f["decisor"], f"Decisor: {f['decisor']}" if f["decisor"] else "Decisor (peça: \"Chopp, pesquise o lead\")"),
        m(p.get("city"), f"Cidade: {p['city']}" if p.get("city") else "Cidade"),
        m(site, f"Site: {site}" if site else "Site"),
        m(p.get("diagnostico_site"), f"Diagnóstico do site ({p['diagnostico_site']})" if p.get("diagnostico_site")
          else "Diagnóstico do site (WhatsApp, preço, formulário)"),
        m(f.get("avaliacoes"), f"Nota/avaliações: {f.get('nota', '?')} · {f['avaliacoes']}" if f.get("avaliacoes")
          else "Nota e nº de avaliações no Google"),
        m("anuncia" in f, "Central de Transparência" + (f": {'anuncia' if f.get('anuncia') else '0 anúncios'}"
                                                        f"{' em ' + f['anuncia_data'] if f.get('anuncia_data') else ''}"
                                                        if "anuncia" in f else " (anúncios no domínio)")),
        "⬜ Keyword Planner: volume e custo por clique de 3-4 termos",
        "⬜ Concorrente que aparece primeiro + print do Patrocinado",
        "⬜ Revisar o site ao vivo",
        "⬜ Câmera no canto + texto fixo com as 2 datas da conversa",
    ]
    return "*Antes de gravar:*\n" + "\n".join(itens)


def gerar_resposta(pedido: str, autor: str, tarefa: str, telefones: list[str], completa: bool, historico: list[dict]) -> str:
    fichas = "\n\n".join(ficha(t, completa) for t in telefones) or "(nenhum lead encontrado)"
    prompt = PROMPT_ASSISTENTE.format(
        nome=NOME, hoje=datetime.now().strftime("%A, %d/%m/%Y %H:%M"), playbook=PLAYBOOK,
        estrategias="\n".join(f"- {ROTULO[c]}: {t}" for c, t in ESTRATEGIA.items()),
        tarefa=TAREFAS_TEXTO[tarefa], fichas=fichas, historico=_fmt_historico(historico), autor=autor or "um sócio",
        pedido=pedido)
    resposta = re.sub(r"^\s*🍺\s*", "", para_whatsapp(llm(prompt, ctx=24576, limite=3000)))  # o 🍺 é posto por quem chama
    # sem nome do decisor: nunca chamar a pessoa pelo nome da empresa ("_Academia Exemplo BJJ, tudo bem?" -> "_Oi, tudo bem?")
    for tel in telefones:
        c = contato_do_lead(tel)
        p = (c or {}).get("properties", {})
        empresa = p.get("company") or ""
        notas = brain._notas_do_contato(c["id"], 6, 5000) if c else []
        if "anuncia" not in fatos_chave(p, notas) and re.search(
                r"(n[ãa]o|sem|nenhum|0)\b[^._\n]{0,40}an[úu]nci", resposta, re.I):
            resposta += ("\n\n⚠️ A mensagem fala de anúncios, mas o Chopp NÃO verificou isso para "
                         f"{empresa or 'esse lead'}. Confira na Central de Transparência antes de mandar.")
        if empresa and not fatos_chave(p, []).get("decisor"):
            variantes = sorted({empresa, re.sub(r"\s*\(.*?\)", "", empresa).strip()}, key=len, reverse=True)
            for nome in filter(None, variantes):
                resposta = re.sub(r"_(?:Oi,?\s*)?" + re.escape(nome) + r"\s*[,!]?\s*(?:tudo bem\??)?\s*",
                                  "_Oi, tudo bem? ", resposta)
    return resposta.replace("_Oi, tudo bem? Oi", "_Oi")


# =========================================================================== pesquisa com conferência dupla
CAMPOS_PESQUISA = {
    "site_oficial": "endereço do site oficial da empresa (URL)",
    "dono_nome": "nome do dono/sócio/fundador/head coach (quem decide)",
    "dono_cargo": "cargo dessa pessoa",
    "endereco": "endereço físico (rua e cidade)",
    "instagram": "perfil do Instagram (@ ou URL)",
    "nota_google": "nota média no Google (ex.: 4.8)",
    "num_avaliacoes": "número de avaliações no Google",
    "ano_fundacao": "ano de fundação / há quantos anos existe",
    "servicos": "principais serviços oferecidos",
    "email": "e-mail de contato",
}


def _texto_da_pagina(url: str) -> tuple[str, str]:
    """(texto visível, html) da página."""
    try:
        r = requests.get(url, headers=UA, timeout=15)
        if not r.ok or "text/html" not in r.headers.get("content-type", ""):
            return "", ""
        from bs4 import BeautifulSoup
        sopa = BeautifulSoup(r.text, "html.parser")
        for tag in sopa(["script", "style", "noscript", "svg"]):
            tag.decompose()
        return re.sub(r"\s+", " ", sopa.get_text(" ")).strip()[:15000], r.text
    except requests.RequestException:
        return "", ""


def _normal(t: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9@.]+", " ", t).strip()


def _dominio(url: str) -> str:
    return urlparse(url if "://" in url else "https://" + url).netloc.lower().replace("www.", "")


def diagnosticar_site(html: str) -> str:
    """Checklist do playbook direto do HTML: link de WhatsApp? preço? só formulário?"""
    if not html:
        return ""
    baixo = html.lower()
    whatsapp = bool(re.search(r"wa\.me/|api\.whatsapp\.com|whatsapp\.com/send", baixo))
    preco = bool(re.search(r"(\$|us\$|r\$)\s?\d{2,}", baixo))
    formulario = "<form" in baixo
    itens = [f"link de WhatsApp: {'sim' if whatsapp else 'NÃO'}", f"mostra preço: {'sim' if preco else 'não'}"]
    if formulario and not whatsapp:
        itens.append("contato só por formulário")
    return "; ".join(itens)


def pesquisar_lead(telefone: str, salvar: bool = True) -> str:
    c = contato_do_lead(telefone)
    if not c:
        return f"🍺 Não achei esse lead no CRM (wa.me/{telefone})."
    p = c["properties"]
    empresa = p.get("company") or p.get("firstname") or ""
    cidade = " ".join(x for x in (p.get("city"), p.get("state")) if x)
    if not empresa:
        return "🍺 Esse lead está sem nome de empresa no CRM, então não tenho o que pesquisar."

    from ddgs import DDGS
    buscas = [f"{empresa} {cidade}", f"{empresa} {cidade} owner", f"{empresa} {cidade} reviews"]
    resultados = []
    for q in buscas:
        try:
            resultados += DDGS().text(q, max_results=6)
        except Exception as e:  # sem internet / limite de buscas
            print(f"Busca falhou ({q}): {e}")
        time.sleep(1)
    urls = []
    site_atual = p.get("website") or ""
    if site_atual:
        urls.append(site_atual if "://" in site_atual else "https://" + site_atual)
    for x in resultados:
        u = x.get("href") or ""
        if u and u not in urls and not re.search(r"facebook\.com|instagram\.com|tiktok\.com|linkedin\.com", u):
            urls.append(u)
    paginas = []
    for u in urls[:7]:
        texto, html = _texto_da_pagina(u)
        if len(texto) > 200:
            paginas.append({"url": u, "texto": texto, "html": html})
    trechos_busca = "\n".join(f"- {x.get('title')} | {x.get('href')} | {x.get('body')}" for x in resultados[:15])
    if not paginas and not resultados:
        return "🍺 Não consegui pesquisar agora (a internet ou a busca não respondeu). Tenta de novo daqui a pouco."

    # 1) extração: cada informação precisa vir com o TRECHO EXATO e a URL de onde saiu
    fontes = "\n\n".join(f"[FONTE {i + 1}] {pg['url']}\n{pg['texto'][:5000]}" for i, pg in enumerate(paginas))
    prompt = (f"Você pesquisa a empresa '{empresa}' ({cidade}). Leia as fontes e extraia SOMENTE o que estiver "
              f"escrito nelas sobre ESTA empresa (cuidado com empresas de nome parecido em outra cidade).\n"
              f"Campos: {json.dumps(CAMPOS_PESQUISA, ensure_ascii=False)}\n"
              f"Responda em JSON: {{\"campo\": {{\"valor\": \"...\", \"trecho\": \"frase copiada LITERALMENTE da fonte\", "
              f"\"fonte\": numero_da_fonte}}}}. Omita campos que não aparecem.\n\n"
              f"Resultados da busca:\n{trechos_busca}\n\n{fontes}")
    try:
        achados = json.loads(llm(prompt, json_mode=True, ctx=32768, temperatura=0, limite=1500))
    except (json.JSONDecodeError, requests.RequestException):
        achados = {}

    site_oficial = ""
    aprovados, reprovados = {}, {}
    for campo, info in achados.items():
        if campo not in CAMPOS_PESQUISA or not isinstance(info, dict) or not info.get("valor"):
            continue
        valor, trecho = str(info["valor"]).strip(), str(info.get("trecho") or "")
        try:
            pg = paginas[int(info.get("fonte", 0)) - 1]
        except (ValueError, IndexError):
            pg = None
        # 2) conferência 1 (código): o trecho tem que estar escrito na página
        texto_fonte = _normal(pg["texto"]) if pg else _normal(trechos_busca)
        conf1 = bool(trecho) and _normal(trecho)[:120] in texto_fonte
        # fontes independentes que citam o mesmo valor
        dominios = {_dominio(x["url"]) for x in paginas if _normal(valor) and _normal(valor) in _normal(x["texto"])}
        if _normal(valor) in _normal(trechos_busca):
            dominios.add("resultado-da-busca")
        aprovados_tmp = {"valor": valor, "trecho": trecho, "url": pg["url"] if pg else "", "conf1": conf1,
                         "dominios": dominios}
        (aprovados if conf1 else reprovados)[campo] = aprovados_tmp

    # 3) conferência 2 (segunda leitura da IA, separada): o trecho confirma mesmo o valor?
    if aprovados:
        lista = "\n".join(f"{i + 1}. Empresa '{empresa}' | {CAMPOS_PESQUISA[cmp]} = '{x['valor']}' | trecho: \"{x['trecho']}\""
                          for i, (cmp, x) in enumerate(aprovados.items()))
        verif = f"""Confira cada afirmação abaixo LENDO SÓ o trecho citado. Responda em JSON {{"1": true/false, ...}}:
true apenas se o trecho afirma claramente aquele valor PARA ESSA EMPRESA.\n{lista}"""
        try:
            notas = json.loads(llm(verif, json_mode=True, ctx=8192, temperatura=0, limite=400))
        except (json.JSONDecodeError, requests.RequestException):
            notas = {}
        for i, cmp in enumerate(list(aprovados)):
            if notas.get(str(i + 1)) is not True:
                reprovados[cmp] = aprovados.pop(cmp)

    if "site_oficial" in aprovados:
        site_oficial = aprovados["site_oficial"]["valor"]
    dominio_oficial = _dominio(site_oficial or site_atual) if (site_oficial or site_atual) else ""
    # regra final: site oficial OU 2 fontes diferentes
    for cmp in list(aprovados):
        x = aprovados[cmp]
        oficial = dominio_oficial and _dominio(x["url"]) == dominio_oficial
        if not oficial and len(x["dominios"]) < 2 and cmp != "site_oficial":
            reprovados[cmp] = aprovados.pop(cmp)

    # diagnóstico do site pelo HTML (checklist do playbook)
    diag = ""
    alvo = site_oficial or site_atual
    if alvo:
        _, html = _texto_da_pagina(alvo if "://" in alvo else "https://" + alvo)
        diag = diagnosticar_site(html)

    # salva no HubSpot só o que passou (e só em campos vazios)
    props = {}
    if "site_oficial" in aprovados and not p.get("website"):
        props["website"] = aprovados["site_oficial"]["valor"]
    if "dono_nome" in aprovados and (not p.get("firstname") or (p.get("firstname") or "").lower() in empresa.lower()):
        partes = aprovados["dono_nome"]["valor"].split(" ", 1)
        props["firstname"] = partes[0]
        if len(partes) > 1:
            props["lastname"] = partes[1]
    if "dono_cargo" in aprovados and not p.get("jobtitle"):
        props["jobtitle"] = aprovados["dono_cargo"]["valor"][:100]
    for cmp, prop in (("endereco", "address"), ("instagram", "instagram"), ("email", "email")):
        if cmp in aprovados and not p.get(prop):
            props[prop] = aprovados[cmp]["valor"][:250]
    if diag:
        props["diagnostico_site"] = diag
    resumo_ok = "; ".join(f"{CAMPOS_PESQUISA[k].split(' (')[0]}: {x['valor']} (fonte: {x['url'] or 'busca'})"
                          for k, x in aprovados.items())
    if resumo_ok:
        props["pesquisa_verificada"] = (f"{datetime.now():%d/%m/%Y} · " + resumo_ok)[:3000]
    if props and salvar:
        hs("PATCH", f"/crm/v3/objects/contacts/{c['id']}", json={"properties": props})

    nota = ["<b>🍺 Pesquisa do Chopp (conferência dupla)</b>"]
    nota += [f"✅ {CAMPOS_PESQUISA[k]}: {x['valor']} — \"{x['trecho'][:200]}\" ({x['url']})" for k, x in aprovados.items()]
    if diag:
        nota.append(f"🔎 Site: {diag}")
    nota += [f"⚠️ NÃO confirmado, conferir: {CAMPOS_PESQUISA[k]}: {x['valor']} ({x['url'] or 'busca'})"
             for k, x in reprovados.items()]
    if salvar:
        hs("POST", "/crm/v3/objects/notes", json={
            "properties": {"hs_timestamp": brain.iso_utc(time.time()), "hs_note_body": "<br>".join(nota)},
            "associations": [{"to": {"id": c["id"]}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}]}]})
        registro_set(telefone, pesquisa_ts=time.time())
    else:
        print("[modo teste] salvaria no HubSpot:", props)

    linhas = [f"🔎 *Pesquisa: {empresa}* ({len(paginas)} páginas lidas)"]
    if aprovados:
        linhas.append("*Confirmado (conferência dupla) e salvo no HubSpot:*")
        linhas += [f"✅ {CAMPOS_PESQUISA[k].split(' (')[0]}: {x['valor']}" for k, x in aprovados.items()]
    if diag:
        linhas.append(f"🌐 Site: {diag}")
    if reprovados:
        linhas.append("*Encontrado mas NÃO confirmado (não salvei, conferir):*")
        linhas += [f"⚠️ {CAMPOS_PESQUISA[k].split(' (')[0]}: {x['valor']}" for k, x in reprovados.items()]
    if not aprovados and not reprovados and not diag:
        linhas.append("Não encontrei nada confiável sobre esse lead.")
    linhas.append("_Regras: o trecho tem que estar escrito na página, uma 2ª leitura confirma, e só salvo se vier do site "
                  "oficial ou de 2 fontes diferentes._")
    return "🍺 " + "\n".join(linhas)


# =========================================================================== cadastro de lead pelo grupo
def cadastrar_lead(pedido: str) -> str:
    prompt = f"""Extraia os dados do novo lead desta mensagem. Responda em JSON com as chaves: empresa, dono_nome,
dono_cargo, telefone (só dígitos, com código do país; EUA = 1), cidade, estado, nicho, site, instagram, observacoes
(todo o resto que foi dito: detalhes, avaliações, diagnóstico, ideias de gancho). Use "" para o que não foi dito.
Mensagem: {pedido}"""
    try:
        d = json.loads(llm(prompt, json_mode=True, ctx=8192, temperatura=0, limite=800))
    except (json.JSONDecodeError, requests.RequestException):
        return "🍺 Não entendi os dados. Manda assim: _Chopp, cadastre: Empresa X, telefone +1..., cidade, dono, site..._"
    tel = re.sub(r"\D", "", str(d.get("telefone") or ""))
    if len(tel) == 10:
        tel = "1" + tel
    empresa = str(d.get("empresa") or "").strip()
    if not empresa and not tel:
        return "🍺 Preciso pelo menos do nome da empresa ou do telefone."
    if tel and brain.buscar_contato(tel):
        return f"🍺 Esse telefone já está no CRM (wa.me/{tel}). Quer que eu pesquise ou faça a copy do Loom?"
    props = {"company": empresa, "phone": "+" + tel if tel else "", "city": d.get("cidade") or "", "state": d.get("estado") or "",
             "website": d.get("site") or "", "instagram": d.get("instagram") or "", "tipo_negocio": d.get("nicho") or "",
             "jobtitle": d.get("dono_cargo") or "", "hs_lead_status": "NEW", "lifecyclestage": "lead",
             "origem_lead": "prospeccao"}
    dono = str(d.get("dono_nome") or "").split(" ", 1)
    props["firstname"] = dono[0] if dono[0] else empresa[:60]
    if len(dono) > 1:
        props["lastname"] = dono[1]
    props = {k: v for k, v in props.items() if v}
    r = hs("POST", "/crm/v3/objects/contacts", json={"properties": props})
    if not r.ok:
        return f"🍺 O HubSpot recusou o cadastro ({r.status_code}): {r.text[:150]}"
    cid = r.json()["id"]
    if d.get("observacoes"):
        hs("POST", "/crm/v3/objects/notes", json={
            "properties": {"hs_timestamp": brain.iso_utc(time.time()),
                           "hs_note_body": "<b>🍺 Cadastro pelo grupo</b><br>" + str(d["observacoes"])[:3000]},
            "associations": [{"to": {"id": cid}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}]}]})
    if tel:
        registro_set(tel, contato_id=cid, empresa=empresa, nome=" ".join(dono).strip(), cidade=props.get("city", ""))
    resumo = ", ".join(f"{k}: {v}" for k, v in props.items() if k in ("company", "phone", "city", "state", "website", "firstname", "tipo_negocio"))
    return (f"🍺 Cadastrado no HubSpot ✅\n{resumo}\n\nPróximos passos: _Chopp, pesquise a {empresa or tel}_ (completa o que "
            f"falta, com conferência dupla) e _Chopp, faça a copy do Loom pra {empresa or tel}_.")


# =========================================================================== roteador
_historico: list[dict] = []

INTENCOES = [
    ("salvar", r"^\s*(pode )?(salva|salvar|grava|gravar)\b"),
    ("cadastrar", r"\bcadastr|adicion[ae]\w* (o |um |esse |este )?(novo )?lead|novo lead\b"),
    # pesquisa universal: pergunta específica sobre qualquer empresa (telefone, dono, Instagram...) ou "complete"
    ("pesquisa_livre", r"qual (é |e )?(o |a )?(telefone|whats|n[uú]mero|celular|instagram|insta|site|e-?mail|dono|"
                       r"endere|hor[aá]rio|nota)|quem (é|e) o dono|"
                       r"(confer|verific|checa|cheque)\w*.{0,40}(telefone|whats|n[uú]mero|contato|dados|informa|site|"
                       r"instagram|e-?mail|endere)|descobr|complet\w* (os |as )?(dados|informa)|o que falta|"
                       r"dados que faltam|ach[ae] (o|outro|um) (n[uú]mero|telefone|contato|whats)"),
    ("pesquisar", r"pesquis|busc\w* na internet|procur\w* na internet"),
    ("loom", r"(copy|roteiro|script)\W+(\w+\W+){0,4}loom|loom\W+(\w+\W+){0,3}(copy|roteiro|script)|copy de loom"),
    ("mensagem", r"mensagem|monte|escrev|redij|texto pr|follow.?up pr|o que (eu )?mand"),
    ("orientar", r"como pros|como proce|como prosig|o que (eu )?(devo|fa[cç]o|fazer)|pr[oó]ximo passo|estrat|como recuper|como reativ"),
]


def responder(pedido: str, autor: str) -> str:
    try:
        brain.completar_registro()
    except requests.RequestException:
        pass
    t = pedido.lower()
    intencoes = {nome for nome, padrao in INTENCOES if re.search(padrao, t)}

    if "salvar" in intencoes:
        import pesquisa
        resposta = pesquisa.salvar()
    elif "cadastrar" in intencoes:
        resposta = cadastrar_lead(pedido)
    elif "pesquisa_livre" in intencoes and "mensagem" not in intencoes:
        import pesquisa
        resposta = pesquisa.pesquisar(pedido)
    else:
        massa = etapa_em_massa(pedido)
        leads = [] if massa else achar_leads(pedido)
        if "pesquisar" in intencoes and not leads and not massa:
            import pesquisa
            resposta = pesquisa.pesquisar(pedido)  # empresa que não está no CRM
        elif "pesquisar" in intencoes and leads:
            resposta = "\n\n".join(pesquisar_lead(tel) for tel in leads[:2])
        elif "loom" in intencoes and leads:
            tel = leads[0]
            extra = ""
            if not registro_get(tel).get("pesquisa_ts"):
                extra = pesquisar_lead(tel) + "\n\n"   # completa a ficha antes de escrever a copy
            resposta = extra + copy_loom(tel)
        elif massa:
            tels = leads_na_etapa(massa)
            rotulo = "Oportunidade (fase do ciclo de vida)" if massa == "OPORTUNIDADE" else ROTULO[massa]
            if not tels:
                resposta = f"🍺 Não há nenhum lead em *{rotulo}* agora."
            else:
                resposta = (f"🍺 *{len(tels)} leads em {rotulo}*\n\n"
                            + gerar_resposta(pedido, autor, "massa", tels, False, _historico))
        elif leads:
            tarefa = "mensagem" if "mensagem" in intencoes else "orientar" if "orientar" in intencoes else "geral"
            resposta = "🍺 " + gerar_resposta(pedido, autor, tarefa, leads[:3], True, _historico)
        else:
            resposta = brain.responder_pergunta(pedido, autor)  # perguntas gerais sobre o funil

    _historico.extend([{"quem": autor or "sócio", "texto": pedido}, {"quem": NOME, "texto": resposta[:800]}])
    del _historico[:-12]
    return resposta
