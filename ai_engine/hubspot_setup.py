"""
Prepara o HubSpot para o agente. Pode rodar quantas vezes quiser: não duplica nada.

  - Funil de negócios no fluxo de vendas (prospecção -> follow-up -> Loom -> reunião)
  - Renomeia colunas antigas preservando os cards que já estão nelas
  - Cria/atualiza os campos que o agente preenche no contato e no negócio

Uso (com o venv ativo, dentro de ai_engine):
    python hubspot_setup.py
"""
import os
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
TOKEN = os.environ["HUBSPOT_ACCESS_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
API = "https://api.hubapi.com"

PIPELINE_NOME = "Funil de Vendas WhatsApp"

# (rótulo no HubSpot, código usado pelo agente, probabilidade, coluna final?)
ETAPAS = [
    ("Prospecção enviada", "PROSPECCAO", 0.02, False),
    ("Follow-up", "FOLLOWUP", 0.03, False),
    ("Em conversa", "EM_CONVERSA", 0.10, False),
    ("Aguardando retorno do lead", "AGUARDANDO_RETORNO", 0.12, False),
    ("Loom aceito – gravar", "LOOM_ACEITO", 0.25, False),
    ("Loom enviado", "LOOM_ENVIADO", 0.40, False),
    ("Reunião agendada", "REUNIAO_AGENDADA", 0.60, False),
    ("Reunião feita", "REUNIAO_FEITA", 0.75, False),
    ("Proposta enviada – decidindo", "PROPOSTA", 0.80, False),
    ("Fechado: ganho", "FECHADO_GANHO", 1.0, True),
    ("Perdido", "PERDIDO", 0.0, True),
]

# Colunas da versão anterior -> nova coluna (os cards vão junto)
RENOMEAR = {"Proposta enviada": "Loom enviado", "Qualificando": "Em conversa"}
# Códigos antigos -> novos (para contatos e registro local)
CODIGOS_ANTIGOS = {"PROPOSTA_ENVIADA": "LOOM_ENVIADO", "QUALIFICANDO": "EM_CONVERSA",
                   "NOVO_LEAD": "EM_CONVERSA", "NEGOCIACAO": "REUNIAO_FEITA"}

GRUPO = "agente_whatsapp"


def _opcoes(pares):
    return [{"label": rot, "value": val, "displayOrder": i} for i, (rot, val) in enumerate(pares)]


OPCOES_ESTAGIO = _opcoes([(rot, cod) for rot, cod, _, _ in ETAPAS])

# De quem é o próximo passo (aparece no card e facilita filtrar "o que eu preciso fazer hoje")
VEZ_DE = [("👉 Nossa vez (responder/agir)", "agencia"), ("⏳ Vez do lead (aguardar)", "lead"),
          ("— Nada pendente", "ninguem")]
CAMPOS_NAVEGACAO = [
    {"name": "vez_de", "label": "De quem é a vez", "type": "enumeration", "fieldType": "select",
     "options": _opcoes(VEZ_DE)},
    {"name": "situacao_lead", "label": "Situação (Chopp)", "type": "string", "fieldType": "text"},
    {"name": "proximo_contato", "label": "Próximo contato em", "type": "date", "fieldType": "date"},
]

CAMPOS_CONTATO = [
    {"name": "estagio_funil", "label": "Estágio do funil (agente)", "type": "enumeration", "fieldType": "select",
     "options": OPCOES_ESTAGIO},
    {"name": "temperatura_lead", "label": "Temperatura do lead (IA)", "type": "enumeration", "fieldType": "select",
     "options": _opcoes([("🔥 Quente", "quente"), ("🌤️ Morno", "morno"), ("❄️ Frio", "frio")])},
    {"name": "score_lead", "label": "Score do lead (0-100, IA)", "type": "number", "fieldType": "number"},
    {"name": "origem_lead", "label": "Origem do lead", "type": "enumeration", "fieldType": "select",
     "options": _opcoes([("Prospecção ativa", "prospeccao"), ("Chamou a gente (inbound)", "inbound")])},
    {"name": "followups_enviados", "label": "Follow-ups enviados", "type": "number", "fieldType": "number"},
    {"name": "resumo_conversa", "label": "Resumo da conversa (IA)", "type": "string", "fieldType": "textarea"},
    {"name": "proxima_acao", "label": "Próxima ação (IA)", "type": "string", "fieldType": "text"},
    {"name": "sugestao_resposta", "label": "Sugestão de resposta (IA)", "type": "string", "fieldType": "textarea"},
    {"name": "data_reuniao", "label": "Reunião combinada (IA)", "type": "string", "fieldType": "text"},
    {"name": "servico_interesse", "label": "Serviço de interesse (IA)", "type": "string", "fieldType": "text"},
    {"name": "tipo_negocio", "label": "Tipo de negócio do lead (IA)", "type": "string", "fieldType": "text"},
    {"name": "orcamento_anuncios", "label": "Orçamento para anúncios (IA)", "type": "string", "fieldType": "text"},
    {"name": "ultima_analise_ia", "label": "Última análise da IA", "type": "string", "fieldType": "text"},
    {"name": "instagram", "label": "Instagram", "type": "string", "fieldType": "text"},
    {"name": "pesquisa_verificada", "label": "Pesquisa verificada (Chopp)", "type": "string", "fieldType": "textarea"},
    {"name": "diagnostico_site", "label": "Diagnóstico do site (Chopp)", "type": "string", "fieldType": "text"},
] + CAMPOS_NAVEGACAO

CAMPOS_NEGOCIO = [
    {"name": "temperatura_lead", "label": "Temperatura (IA)", "type": "enumeration", "fieldType": "select",
     "options": _opcoes([("🔥 Quente", "quente"), ("🌤️ Morno", "morno"), ("❄️ Frio", "frio")])},
    {"name": "proxima_acao", "label": "Próxima ação (IA)", "type": "string", "fieldType": "text"},
    {"name": "origem_lead", "label": "Origem do lead", "type": "enumeration", "fieldType": "select",
     "options": _opcoes([("Prospecção ativa", "prospeccao"), ("Chamou a gente (inbound)", "inbound")])},
] + CAMPOS_NAVEGACAO


def obter_pipeline():
    r = requests.get(f"{API}/crm/v3/pipelines/deals", headers=H, timeout=20)
    r.raise_for_status()
    pipelines = r.json()["results"]
    for p in pipelines:
        if p["label"] == PIPELINE_NOME:
            return p, True
    for p in pipelines:
        if p["id"] == "default" or p["label"] == "Sales Pipeline":
            return p, False
    return (pipelines[0] if pipelines else None), False


def negocios_na_coluna(stage_id):
    r = requests.post(f"{API}/crm/v3/objects/deals/search", headers=H, timeout=20, json={
        "filterGroups": [{"filters": [{"propertyName": "dealstage", "operator": "EQ", "value": stage_id}]}], "limit": 1})
    r.raise_for_status()
    return r.json().get("total", 0)


def garantir_pipeline():
    pipeline, ja_e_nosso = obter_pipeline()
    corpo_etapas = [
        {"label": rot, "displayOrder": i,
         "metadata": {"probability": str(prob), **({"isClosed": "true"} if final else {})}}
        for i, (rot, _, prob, final) in enumerate(ETAPAS)
    ]
    if pipeline is None:
        r = requests.post(f"{API}/crm/v3/pipelines/deals", headers=H, timeout=20,
                          json={"label": PIPELINE_NOME, "displayOrder": 0, "stages": corpo_etapas})
        print("OK  funil criado" if r.ok else f"XX  funil ({r.status_code}): {r.text[:300]}")
        return r.ok

    existentes = {s["label"]: s["id"] for s in pipeline["stages"]}
    if all(rot in existentes for rot, *_ in ETAPAS) and len(existentes) == len(ETAPAS):
        print(f"OK  funil '{PIPELINE_NOME}' já está no formato atual")
        return True

    # Só faltam colunas novas (as atuais continuam todas): cria as novas e reordena, sem mexer nos cards
    if ja_e_nosso and all(rot in {r for r, *_ in ETAPAS} for rot in existentes):
        base = f"{API}/crm/v3/pipelines/deals/{pipeline['id']}/stages"
        for etapa in corpo_etapas:
            if etapa["label"] in existentes:
                r = requests.patch(f"{base}/{existentes[etapa['label']]}", headers=H, timeout=20,
                                   json={"displayOrder": etapa["displayOrder"]})
            else:
                r = requests.post(base, headers=H, timeout=20, json=etapa)
                if r.ok:
                    print(f"OK  coluna nova: {etapa['label']}")
            if not r.ok:
                print(f"XX  coluna '{etapa['label']}' ({r.status_code}): {r.text[:300]}")
                return False
        print(f"OK  funil atualizado: {' -> '.join(rot for rot, *_ in ETAPAS)}")
        return True

    # Reaproveita ids: mesmo nome, ou coluna antiga que foi renomeada (os cards vão junto)
    usados = set()
    for etapa in corpo_etapas:
        antigo = next((velho for velho, novo in RENOMEAR.items() if novo == etapa["label"]), None)
        for nome in (etapa["label"], antigo):
            if nome and nome in existentes and existentes[nome] not in usados:
                etapa["stageId"] = existentes[nome]
                usados.add(existentes[nome])
                break

    # Colunas que vão sumir não podem ter cards
    for nome, sid in existentes.items():
        if sid not in usados and negocios_na_coluna(sid) > 0:
            print(f"XX  a coluna '{nome}' tem cards e não existe no funil novo. Mova esses cards e rode de novo.")
            return False

    r = requests.put(f"{API}/crm/v3/pipelines/deals/{pipeline['id']}", headers=H, timeout=20,
                     json={"label": PIPELINE_NOME, "displayOrder": 0, "stages": corpo_etapas})
    if r.ok:
        print(f"OK  funil atualizado: {' -> '.join(rot for rot, *_ in ETAPAS)}")
        return True
    print(f"XX  não consegui atualizar o funil ({r.status_code}): {r.text[:400]}")
    return False


def garantir_grupo(objeto):
    g = requests.get(f"{API}/crm/v3/properties/{objeto}/groups/{GRUPO}", headers=H, timeout=20)
    if g.status_code == 404:
        c = requests.post(f"{API}/crm/v3/properties/{objeto}/groups", headers=H, timeout=20,
                          json={"name": GRUPO, "label": "Agente WhatsApp", "displayOrder": -1})
        if not c.ok:
            print(f"XX  grupo de campos em {objeto} ({c.status_code}): {c.text[:200]}")
            return False
    return True


def garantir_campos(objeto, campos):
    if not garantir_grupo(objeto):
        return False
    ok = True
    for campo in campos:
        existe = requests.get(f"{API}/crm/v3/properties/{objeto}/{campo['name']}", headers=H, timeout=20)
        if existe.ok:
            if campo["type"] == "enumeration":
                atuais = {o["value"] for o in existe.json().get("options", [])}
                novos = {o["value"] for o in campo["options"]}
                if atuais != novos:
                    # mantém temporariamente as opções antigas para migrar os valores
                    extra = [{"label": v, "value": v, "displayOrder": 99, "hidden": True} for v in atuais - novos]
                    requests.patch(f"{API}/crm/v3/properties/{objeto}/{campo['name']}", headers=H, timeout=20,
                                   json={"options": campo["options"] + extra})
            continue
        r = requests.post(f"{API}/crm/v3/properties/{objeto}", headers=H, timeout=20,
                          json={**campo, "groupName": GRUPO})
        if r.ok:
            print(f"OK  campo '{campo['name']}' criado em {objeto}")
        else:
            ok = False
            print(f"XX  campo '{campo['name']}' em {objeto} ({r.status_code}): {r.text[:200]}")
    return ok


def migrar_codigos_antigos():
    """Contatos com estágios da versão anterior passam para os códigos novos."""
    total = 0
    for velho, novo in CODIGOS_ANTIGOS.items():
        while True:
            r = requests.post(f"{API}/crm/v3/objects/contacts/search", headers=H, timeout=20, json={
                "filterGroups": [{"filters": [{"propertyName": "estagio_funil", "operator": "EQ", "value": velho}]}],
                "limit": 100})
            ids = [c["id"] for c in r.json().get("results", [])] if r.ok else []
            if not ids:
                break
            requests.post(f"{API}/crm/v3/objects/contacts/batch/update", headers=H, timeout=30, json={
                "inputs": [{"id": i, "properties": {"estagio_funil": novo}} for i in ids]})
            total += len(ids)
            if len(ids) < 100:
                break
    if total:
        print(f"OK  {total} contato(s) migrados para os estágios novos")
    # remove as opções antigas
    requests.patch(f"{API}/crm/v3/properties/contacts/estagio_funil", headers=H, timeout=20,
                   json={"options": OPCOES_ESTAGIO})


if __name__ == "__main__":
    p = garantir_pipeline()
    c = garantir_campos("contacts", CAMPOS_CONTATO)
    d = garantir_campos("deals", CAMPOS_NEGOCIO)
    if c:
        migrar_codigos_antigos()
    print("\nTudo pronto." if p and c and d else "\nAlgumas partes falharam (veja acima).")
