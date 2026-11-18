"""
Sincroniza a planilha de pesquisa da equipe (layout de 58 colunas, aba "Leads") com o CRM e o agente.

O que faz (não apaga nada):
  - lead da planilha que já existe no CRM: atualiza os dados de pesquisa (empresa, cidade, site, ramo, dono,
    Instagram, diagnóstico) e cria uma nota "📋 Planilha" com prioridade, ângulo, avaliações, anúncios, follow-ups
  - lead da planilha que não existe: cria o contato como "ainda não abordado" (sem card no funil)
  - guarda tudo no registro do agente (campo "planilha"), que a IA usa como contexto ao ler as conversas
A ETAPA não vem da planilha: ela sai das conversas reais do WhatsApp (auditoria.py).

Uso (venv ativo, dentro de ai_engine):
    python sincronizar_planilha.py "C:\\caminho\\planilha.xlsx"            -> só mostra o que faria
    python sincronizar_planilha.py "C:\\caminho\\planilha.xlsx" --gravar   -> grava
"""
import re
import sys
import time

import openpyxl

sys.stdout.reconfigure(encoding="utf-8")
import brain  # noqa: E402
from brain import hs, registro_get, registro_set  # noqa: E402

GRAVAR = "--gravar" in sys.argv


def tel(t) -> str:
    d = re.sub(r"\D", "", str(t or ""))
    return "1" + d if len(d) == 10 else d


def ler(caminho: str) -> list[dict]:
    wb = openpyxl.load_workbook(caminho, data_only=True)
    ws = wb["Leads"]
    linhas = [r for r in ws.iter_rows(values_only=True) if any(c not in (None, "") for c in r)]
    cab = [str(c).strip() for c in linhas[0]]
    leads = []
    for r in linhas[1:]:
        d = {cab[i]: ("" if v is None else str(v).strip()) for i, v in enumerate(r) if i < len(cab)}
        if d.get("Empresa"):
            leads.append(d)
    return leads


def dono(d: dict) -> str:
    if d.get("Dono identificado") not in ("Sim", "Parcial"):
        return ""
    nome = re.split(r"[(\-/,;]", d.get("Proprietario", ""))[0].strip()
    if not nome or re.search(r"n[ãa]o identificad|desconhecid", nome, re.I):
        return ""
    return nome


def resumo_planilha(d: dict) -> str:
    """Texto curto com o que a equipe sabe do lead (vira contexto da IA e nota no HubSpot)."""
    partes = []
    for rot, col in (("Prio", "Prio"), ("Nicho", "Nicho"), ("Subnicho", "Subnicho"), ("Nota", "Nota"),
                     ("Avaliacoes", "Avaliacoes"), ("Dono", "Proprietario"), ("Site", "Dominio do site"),
                     ("Diagnostico do site", "Diagnostico do site/motivo do problema"),
                     ("Anuncia Google Ads?", "Anuncia Google Ads?"), ("Instagram", "Instagram"),
                     ("Angulo de abordagem", "Angulo de abordagem"), ("Observacao", "Observacao"),
                     ("WhatsApp", "WhatsApp"), ("Status do negocio", "Status do negocio"),
                     ("Agencia contratada?", "Agencia contratada?"), ("Pendencias", "Pendencias"),
                     ("Idioma", "Idioma de abordagem"), ("Rodada", "Rodada")):
        if d.get(col):
            partes.append(f"{rot}: {d[col][:400]}")
    fus = [d[f"Follow-up {i}"] for i in range(1, 11) if d.get(f"Follow-up {i}")]
    if fus:
        partes.append("Follow-ups registrados: " + " / ".join(f[:200] for f in fus))
    return " | ".join(partes)


def main():
    caminho = next(a for a in sys.argv[1:] if not a.startswith("--"))
    leads = ler(caminho)
    print(f"{'GRAVANDO' if GRAVAR else 'SIMULAÇÃO'} · {len(leads)} leads na planilha\n")
    atualizados, criados = 0, 0
    for d in leads:
        wa = re.search(r"wa\.me/(\d+)", d.get("WhatsApp", ""))
        telefone = tel(wa.group(1)) if wa else tel(d.get("Telefone"))
        sem_whatsapp = "SEM WHATSAPP" in d.get("WhatsApp", "").upper()
        resumo = resumo_planilha(d)
        props = {"company": d["Empresa"], "city": d.get("Cidade", ""), "state": d.get("UF", ""),
                 "tipo_negocio": " · ".join(x for x in (d.get("Nicho"), d.get("Subnicho")) if x)[:200]}
        if d.get("Dominio do site"):
            props["website"] = d["Dominio do site"]
        if d.get("Instagram"):
            props["instagram"] = d["Instagram"][:250]
        if d.get("Diagnostico do site/motivo do problema"):
            props["diagnostico_site"] = d["Diagnostico do site/motivo do problema"][:250]
        nome = dono(d)
        if nome:
            partes = nome.split(" ", 1)
            props["firstname"] = partes[0]
            if len(partes) > 1:
                props["lastname"] = partes[1]
        props = {k: v for k, v in props.items() if v}

        contato = brain.buscar_contato(telefone) if len(telefone) >= 10 else None
        if contato:
            acao = "atualizar"
            atualizados += 1
        else:
            acao = "criar (ainda não abordado)"
            criados += 1
        print(f"{acao:28} {d['Empresa'][:40]:40} +{telefone or 'sem telefone'}{'  · SEM WHATSAPP' if sem_whatsapp else ''}")
        if not GRAVAR:
            continue

        if contato:
            r = hs("PATCH", f"/crm/v3/objects/contacts/{contato['id']}", json={"properties": props})
            if not r.ok:
                print(f"   HubSpot recusou ({r.status_code}): {r.text[:150]}")
            cid = contato["id"]
        else:
            novo = dict(props, lifecyclestage="lead", hs_lead_status="NEW", origem_lead="prospeccao",
                        vez_de="agencia",
                        situacao_lead=("🚫 Sem WhatsApp: abordar por telefone, e-mail ou Instagram · ainda não abordado"
                                       if sem_whatsapp or not telefone else "Ainda não abordado (planilha)"))
            if telefone:
                novo["phone"] = "+" + telefone
            r = hs("POST", "/crm/v3/objects/contacts", json={"properties": novo})
            if not r.ok:
                print(f"   HubSpot recusou ({r.status_code}): {r.text[:150]}")
                continue
            cid = r.json()["id"]
        hs("POST", "/crm/v3/objects/notes", json={
            "properties": {"hs_timestamp": brain.iso_utc(time.time()),
                           "hs_note_body": "<b>📋 Planilha da equipe (pesquisa)</b><br>" + resumo.replace(" | ", "<br>")},
            "associations": [{"to": {"id": cid}, "types": [{"associationCategory": "HUBSPOT_DEFINED",
                                                            "associationTypeId": 202}]}]})
        if telefone:
            registro_set(telefone, contato_id=cid, empresa=d["Empresa"], planilha=resumo[:3000],
                         sem_whatsapp=sem_whatsapp, nome=registro_get(telefone).get("nome") or nome)
        time.sleep(0.3)
    print(f"\n{atualizados} atualizados · {criados} criados como 'ainda não abordado'")


if __name__ == "__main__":
    main()
