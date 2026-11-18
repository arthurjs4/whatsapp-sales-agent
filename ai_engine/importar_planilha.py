"""
Importa a planilha de prospecção (aba "Leads") para o HubSpot.

  - Todo lead vira CONTATO, com os dados da planilha e uma nota com o histórico.
  - Só quem RESPONDEU ganha CARD no funil (Loom enviado -> "Proposta enviada";
    outras respostas -> "Qualificando").
  - Quem não respondeu fica com Status do lead "Tentativa de contato" (ou "Novo",
    se ainda não foi abordado). Quando responder no WhatsApp, o agente cria o card.
  - Não duplica: procura pelo telefone antes de criar.

Uso (venv ativo, dentro de ai_engine):
    python importar_planilha.py "C:\\caminho\\planilha.xlsx"            -> PRÉVIA (não grava nada)
    python importar_planilha.py "C:\\caminho\\planilha.xlsx" --gravar   -> grava no HubSpot
"""
import re
import sys
import time

import openpyxl
import requests

import brain

API, H = brain.API, brain.HS_HEADERS


def txt(v) -> str:
    return "" if v is None else str(v).strip()


PADRAO_TEL = re.compile(r"\+?\(?\d[\d\s\-().]{8,}\d")


def _normalizar(trecho: str) -> str:
    if "/" in trecho:            # parece data (06/10/2026), não telefone
        return ""
    digitos = re.sub(r"\D", "", trecho)
    if len(digitos) == 10:       # EUA/Canadá sem o +1
        digitos = "1" + digitos
    return digitos if 11 <= len(digitos) <= 15 else ""


def _primeiro_telefone(texto: str) -> str:
    for m in PADRAO_TEL.finditer(texto):
        tel = _normalizar(m.group(0))
        if tel:
            return tel
    return ""


def telefone_de(linha: dict) -> str:
    """A coluna WhatsApp é texto livre; procura números reais em ordem de confiança."""
    wa = txt(linha.get("WhatsApp"))
    # 1) link wa.me com a mensagem (?text=): é o número efetivamente usado
    # 2) "NOVO NUMERO ..." indicado no texto
    # 3) qualquer outro link wa.me
    for padrao in (r"wa\.me/(\d{10,15})\?text=", r"NOVO N[UÚ]MERO.*?(\+\d[\d\s\-().]{9,18}\d)", r"wa\.me/(\d{10,15})"):
        m = re.search(padrao, wa, re.IGNORECASE)
        if m and _normalizar(m.group(1)):
            return _normalizar(m.group(1))
    # 4) telefone confirmado / 5) telefone
    for col in ("Telefone confirmado", "Telefone"):
        tel = _primeiro_telefone(txt(linha.get(col)))
        if tel:
            return tel
    # 6) qualquer número no texto do WhatsApp que não seja o marcado como "não está no WhatsApp"
    if "nao esta no whatsapp" not in wa.lower():
        return _primeiro_telefone(wa)
    return ""


def nome_do_dono(linha: dict) -> tuple[str, str, str]:
    """'Ana Silva (proprietária)' ou 'Ana Silva - dona' -> ('Ana', 'Silva', 'proprietária')."""
    bruto = txt(linha.get("Dono identificado (nome e cargo)")) or txt(linha.get("Proprietario (nome e cargo)"))
    if not bruto or bruto.lower().startswith(("nao ", "não ", "sem ", "n/a", "-")):
        return "", "", ""
    cargo = ""
    m = re.match(r"^\s*([^(\-–,;]+?)\s*(?:[(\-–,;]\s*(.+?)\)?)?\s*$", bruto)
    nome = m.group(1) if m else bruto
    cargo = (m.group(2) or "") if m else ""
    partes = nome.split()
    if not partes or len(partes) > 5:
        return "", "", ""
    return partes[0], " ".join(partes[1:]), cargo[:100]


def classificar(linha: dict) -> tuple[str | None, str]:
    """Devolve (coluna do funil ou None, status do lead no HubSpot)."""
    status = (txt(linha.get("Status contato")) + " " + txt(linha.get("Situacao da auditoria"))).upper()
    etapa = txt(linha.get("Etapa da prospecção")).upper()
    if "LOOM ENVIADO" in status:
        return "LOOM_ENVIADO", "IN_PROGRESS"
    if "LOOM" in status and ("ACEIT" in status or "PEDIU" in status):
        return "LOOM_ACEITO", "IN_PROGRESS"
    if "RESPONDEU" in status and "AUTOMATICA" not in status:
        return "EM_CONVERSA", "CONNECTED"
    if etapa == "ABORDADO" or "MENSAGEM ENVIADA" in status or "CONTATADO" in status:
        return "PROSPECCAO", "ATTEMPTED_TO_CONTACT"
    return None, "NEW"


CAMPOS_NOTA = [
    "Prio", "Nicho", "Subnicho", "Nota", "Avaliacoes", "Confianca BR", "Evidencia de dono brasileiro",
    "Tem site?", "Dominio do site", "Diagnostico do site", "Recomendação + o que mudaria (feito pelo GPT)",
    "Anuncia Google Ads?", "Evidencia de anuncios", "Agencia contratada?", "Instagram", "Ultimo post Instagram",
    "Angulo de abordagem", "Detalhe verificavel para a mensagem", "Idioma de abordagem", "Mensagem Inicial",
    "Telefone", "Telefone confirmado", "Fonte do telefone e divergencias", "Proprietario (nome e cargo)",
    "Status contato", "Data contato", "Etapa da prospecção", "Situacao da auditoria", "Pendencias",
    "Resultado da validacao", "Observacao",
] + [f"Follow-up {i}" for i in range(1, 11)]


def corpo_nota(linha: dict) -> str:
    partes = ["<b>Importado da planilha de prospecção</b>"]
    for col in CAMPOS_NOTA:
        v = txt(linha.get(col))
        if v:
            partes.append(f"<b>{col}:</b> {v[:1500]}")
    return "<br>".join(partes)


def ler_planilha(caminho: str) -> list[dict]:
    ws = openpyxl.load_workbook(caminho, read_only=True, data_only=True)["Leads"]
    linhas = [r for r in ws.iter_rows(values_only=True) if r and any(x not in (None, "") for x in r)]
    cab = [txt(c) for c in linhas[0]]
    return [dict(zip(cab, r)) for r in linhas[1:]]


def site_de(linha: dict) -> str:
    d = txt(linha.get("Dominio do site"))
    if not d or " " in d or "." not in d:
        return ""
    return d if d.startswith("http") else "https://" + d


def importar(caminho: str, gravar: bool):
    leads = ler_planilha(caminho)
    pipeline_id, etapas = brain.ids_do_funil()
    resumo = {"criados": 0, "ja_existiam": 0, "cards": 0, "sem_telefone": 0, "erros": 0}

    print(f"{'GRAVANDO NO HUBSPOT' if gravar else 'PRÉVIA (nada é gravado)'}: {len(leads)} leads\n")
    for n, linha in enumerate(leads, 1):
        empresa = txt(linha.get("Empresa"))
        tel = telefone_de(linha)
        if not tel:
            resumo["sem_telefone"] += 1
            print(f"{n:3}. {empresa[:38]:38} SEM TELEFONE, pulado")
            continue
        estagio, status_lead = classificar(linha)
        primeiro, ultimo, cargo = nome_do_dono(linha)
        props = {
            "phone": "+" + tel,
            "company": empresa[:200],
            "city": txt(linha.get("Cidade")),
            "state": txt(linha.get("UF")),
            "website": site_de(linha),
            "jobtitle": cargo,
            "firstname": primeiro or empresa[:60],
            "lastname": ultimo,
            "hs_lead_status": status_lead,
            "lifecyclestage": "lead",
            "tipo_negocio": " / ".join(x for x in (txt(linha.get("Nicho")), txt(linha.get("Subnicho"))) if x)[:200],
            "servico_interesse": "Prospecção ativa (planilha)",
        }
        if estagio:
            props["estagio_funil"] = estagio
        props = {k: v for k, v in props.items() if v}

        existente = brain.buscar_contato(tel)
        acao = "já existe, completar" if existente else "criar contato"
        coluna = brain.ROTULO[estagio] if estagio else "sem card (" + status_lead + ")"
        print(f"{n:3}. {empresa[:38]:38} +{tel:<14} {acao:22} -> {coluna}")
        if not gravar:
            resumo["ja_existiam" if existente else "criados"] += 1
            resumo["cards"] += bool(estagio)
            continue

        try:
            if existente:
                atuais = existente["properties"]
                # não sobrescreve o que já estava preenchido no HubSpot
                novos = {k: v for k, v in props.items() if not atuais.get(k) or k in ("hs_lead_status",)}
                novos.pop("lifecyclestage", None)
                if atuais.get("estagio_funil"):
                    novos.pop("estagio_funil", None)
                    estagio = None  # o agente já está cuidando desse lead
                if novos:
                    requests.patch(f"{API}/crm/v3/objects/contacts/{existente['id']}", headers=H, timeout=20,
                                   json={"properties": novos}).raise_for_status()
                contato_id = existente["id"]
                resumo["ja_existiam"] += 1
            else:
                r = requests.post(f"{API}/crm/v3/objects/contacts", headers=H, timeout=20, json={"properties": props})
                if not r.ok:  # algum campo recusado: tenta com o básico
                    basico = {k: props[k] for k in ("phone", "company", "firstname", "lastname", "city") if k in props}
                    r = requests.post(f"{API}/crm/v3/objects/contacts", headers=H, timeout=20, json={"properties": basico})
                r.raise_for_status()
                contato_id = r.json()["id"]
                resumo["criados"] += 1
            brain.registro_set(tel, contato_id=contato_id)

            negocio_id = None
            if estagio:
                corpo = {
                    "properties": {"dealname": f"{empresa[:80]} · WhatsApp", "pipeline": pipeline_id,
                                   "dealstage": etapas[estagio],
                                   "description": txt(linha.get("Status contato"))[:1000]},
                    "associations": [{"to": {"id": contato_id}, "types": [
                        {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 3}]}],
                }
                r = requests.post(f"{API}/crm/v3/objects/deals", headers=H, timeout=20, json=corpo)
                r.raise_for_status()
                negocio_id = r.json()["id"]
                brain.registro_set(tel, negocio_id=negocio_id, estagio=estagio)
                resumo["cards"] += 1

            assoc = [{"to": {"id": contato_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}]}]
            if negocio_id:
                assoc.append({"to": {"id": negocio_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 214}]})
            requests.post(f"{API}/crm/v3/objects/notes", headers=H, timeout=20, json={
                "properties": {"hs_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                               "hs_note_body": corpo_nota(linha)},
                "associations": assoc}).raise_for_status()
            time.sleep(0.25)  # respeita o limite de chamadas do HubSpot
        except requests.RequestException as e:
            resumo["erros"] += 1
            print(f"     ERRO: {e}")

    print(f"\nResumo: {resumo}")
    if not gravar:
        print('\nNada foi gravado. Para gravar, rode de novo com --gravar no final.')


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    importar(sys.argv[1], gravar="--gravar" in sys.argv)
