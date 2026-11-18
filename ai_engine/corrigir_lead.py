"""
Correção manual de um lead (quando a equipe sabe algo que a conversa não mostra).
Grava no HubSpot (card + contato + nota) e trava o lead para a auditoria não desfazer.

Uso (venv ativo, dentro de ai_engine):
    python corrigir_lead.py 15550100001 REUNIAO_AGENDADA "Call marcada para a semana que vem"
    python corrigir_lead.py 15550100002 FOLLOWUP "Reunião não aconteceu: remarcar" --especial
"""
import sys
import time
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
import brain  # noqa: E402
from brain import ROTULO, hs, registro_get, registro_set  # noqa: E402


def corrigir(tel: str, estagio: str, situacao: str, especial: bool = False, vez: str = "") -> str:
    reg = registro_get(tel)
    if not reg.get("contato_id"):
        c = brain.buscar_contato(tel)
        if not c:
            return f"Não achei o lead +{tel} no CRM."
        reg["contato_id"] = c["id"]
    pipeline_id, etapas = brain.ids_do_funil()
    if especial:
        situacao = "⭐ FOLLOW-UP ESPECIAL: " + situacao
    vez = vez or ("agencia" if especial or estagio in ("EM_CONVERSA", "LOOM_ACEITO") else "lead")
    agora = time.time()
    proximo = datetime.fromtimestamp(agora + (0 if especial else 3 * 86400)).strftime("%Y-%m-%d")
    nav = {"vez_de": vez, "situacao_lead": situacao[:250], "proximo_contato": proximo}

    negocio = brain.buscar_negocio(reg["contato_id"], pipeline_id, tel)
    if negocio:
        hs("PATCH", f"/crm/v3/objects/deals/{negocio['id']}", json={"properties": {"dealstage": etapas[estagio], **nav}})
    hs("PATCH", f"/crm/v3/objects/contacts/{reg['contato_id']}",
       json={"properties": {"estagio_funil": estagio, **nav}})
    hs("POST", "/crm/v3/objects/notes", json={
        "properties": {"hs_timestamp": brain.iso_utc(agora),
                       "hs_note_body": f"<b>✍️ Correção manual da equipe</b><br>Etapa: <b>{ROTULO[estagio]}</b><br>{situacao}"},
        "associations": [{"to": {"id": reg["contato_id"]},
                          "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}]}]})
    registro_set(tel, estagio=estagio, situacao=situacao, vez_de=vez, follow_up_especial=especial,
                 fixado_manual_ts=agora, contato_id=reg["contato_id"])
    nome = reg.get("empresa") or reg.get("nome") or "+" + tel
    return f"{nome}: {ROTULO[estagio]} · {situacao}{' (card atualizado)' if negocio else ' (sem card no funil)'}"


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    print(corrigir(args[0], args[1], args[2], especial="--especial" in sys.argv))
