"""Gera dados 100% fictícios (nomes inventados, telefones 555) em data_storage/ para testar o agente sem expor ninguém."""
import json, os, time

AQUI = os.path.dirname(os.path.abspath(__file__))
SAIDA = os.path.join(os.path.dirname(AQUI), "data_storage")
os.makedirs(os.path.join(SAIDA, "conversas"), exist_ok=True)
AGORA = int(time.time())
DIA = 86400

# (telefone, nome, empresa, cidade, estagio, mensagens [(dias_atras, quem, texto)])
CASOS = [
 ("15550100101", "Carla", "Café Aurora", "Orlando", "LOOM_ENVIADO", [
   (6, "a", "Boa tarde! Vi uma avaliação agradecendo à Carla pelo atendimento no Café Aurora. Posso te mandar um vídeo de 4 min com o que achei na busca do Google?"),
   (5.5, "c", "Pode sim"), (5, "a", "Segue o vídeo: loom.com/share/exemplo1. Quinta ou sexta, qual fica melhor para 15 min?")]),
 ("15550100102", "Rafael", "Studio Lume", "Boston", "FOLLOWUP", [
   (5, "a", "Oi Rafael! Pesquisei 'estúdio de pilates em Boston' e vocês quase não aparecem. Posso te mostrar em um vídeo curto?"),
   (2, "a", "Fiz a busca de novo hoje e um concorrente continua aparecendo antes. Te mando o vídeo?")]),
 ("15550100103", "Marina", "Clínica Vitta", "Miami", "REUNIAO_AGENDADA", [
   (8, "a", "Olá Marina, vi o perfil da Clínica Vitta. Posso te mostrar um vídeo de 4 min?"),
   (7.9, "c", "Pode mandar"), (7, "a", "Segue: loom.com/share/exemplo2. Terça às 15h ou quarta às 10h?"),
   (6, "c", "Terça às 15h está ótimo"), (6, "a", "Fechado! Envio o convite agora.")]),
 ("15550100104", "Diego", "Oficina Central", "Newark", "PERDIDO", [
   (14, "a", "Oi Diego! Pesquisei 'mecânica em Newark' e achei alguns pontos para melhorar. Posso mandar um vídeo?"),
   (13, "c", "Obrigado, mas não tenho interesse no momento.")]),
 ("15550100105", "Patrícia", "Bela Casa Decor", "Atlanta", "EM_CONVERSA", [
   (3, "a", "Oi Patrícia, tudo bem? Vi as avaliações da Bela Casa Decor. Posso te mostrar uma ideia em vídeo?"),
   (2.8, "c", "Oi! Que tipo de ideia?"), (2.7, "a", "Mostro como vocês aparecem hoje quando alguém busca por decoração na cidade. Mando o vídeo?")]),
 ("15550100106", "Lucas", "Sabor da Praça", "Tampa", "FECHADO_GANHO", [
   (20, "a", "Olá Lucas! Posso te mandar um vídeo de 4 min sobre o Sabor da Praça?"),
   (19, "c", "Pode"), (18, "a", "Segue: loom.com/share/exemplo3. Quarta às 11h para conversarmos?"),
   (17, "c", "Combinado"), (12, "a", "Obrigado pela conversa! Segue a proposta por escrito."),
   (10, "c", "Fechado, pode começar. Pagamento feito.")]),
 ("15550100107", "Helena", "Pet Feliz", "Dallas", "AGUARDANDO_RETORNO", [
   (9, "a", "Oi Helena, posso te mostrar em vídeo como o Pet Feliz aparece no Google?"),
   (8, "c", "Vou ver com meu sócio e te retorno.")]),
 ("15550100108", "Tiago", "Auto Brilho", "Houston", "PROSPECCAO", [
   (1, "a", "Oi Tiago! Pesquisei 'estética automotiva em Houston' e vi uma oportunidade. Posso te mandar um vídeo curto?")]),
 ("15550100109", "Bianca", "Doce Ponto", "Austin", "LOOM_ACEITO", [
   (2, "a", "Oi Bianca! Posso te mostrar um vídeo de 4 min sobre o Doce Ponto?"),
   (1.9, "c", "Claro, manda!")]),
 ("15550100110", "Otávio", "Estúdio Vento Sul", "Denver", "PROPOSTA", [
   (15, "a", "Oi Otávio, posso mandar um vídeo sobre o Estúdio Vento Sul?"), (14.5, "c", "Pode sim"),
   (14, "a", "Segue: loom.com/share/exemplo4"), (10, "a", "Conversamos e segue a proposta por escrito, como combinado."),
   (9, "c", "Recebi, vou analisar com calma.")]),
 ("15550100111", "Sofia", "Lar Doce Imóveis", "Seattle", "REUNIAO_FEITA", [
   (12, "a", "Olá Sofia! Posso te mandar um vídeo de 4 min?"), (11, "c", "Pode"),
   (10, "a", "Segue: loom.com/share/exemplo5. Quinta às 14h?"), (9, "c", "Quinta às 14h ok"),
   (7, "a", "Foi ótimo conversar! Te mando o resumo hoje e combinamos a resposta na segunda às 10h.")]),
 ("15550100112", "Ícaro", "Fit Total", "Chicago", "PERDIDO", [
   (25, "a", "Oi Ícaro, posso mandar um vídeo sobre a Fit Total?"), (24, "c", "Pode"),
   (23, "a", "Segue: loom.com/share/exemplo6"), (20, "a", "Conseguiu dar uma olhada?"),
   (19, "c", "Vi sim, mas achei caro e fechei com outra empresa.")]),
 ("15550100113", "Renata", "Floricultura Jardim", "Phoenix", "FOLLOWUP", [
   (7, "a", "Oi Renata! Pesquisei 'floricultura em Phoenix'. Posso te mostrar o que encontrei em vídeo?"),
   (4, "a", "Fiz a busca de novo e vi uma novidade. Te mando o vídeo?"),
   (2, "a", "Última mensagem para não te encher. Se um dia quiser ver, é só me chamar.")]),
 ("15550100114", "Bruno", "Mecânica Rápida", "Portland", "EM_CONVERSA", [
   (4, "a", "Oi Bruno, posso mandar um vídeo sobre a Mecânica Rápida?"), (3.5, "c", "Quem é você?"),
   (3.4, "a", "Sou da agência que ajuda negócios locais a aparecerem no Google. Posso mostrar em vídeo de 4 min?")]),
 ("15550100115", "Camila", "Ateliê Camila", "San Diego", "LOOM_ENVIADO", [
   (5, "a", "Oi Camila! Posso te mostrar um vídeo de 4 min sobre o Ateliê?"), (4.8, "c", "Pode mandar"),
   (4.5, "a", "Segue: loom.com/share/exemplo7. Quinta às 10h ou sexta às 16h?"), (1, "a", "Conseguiu dar uma olhada?")]),
]

def conv(msgs):
    return [{"de": "agencia" if q == "a" else "cliente", "texto": t, "quando": int(AGORA - d * DIA)} for d, q, t in msgs]

leads = {}
for i, (tel, nome, emp, cid, est, msgs) in enumerate(CASOS):
    m = conv(msgs)
    with open(os.path.join(SAIDA, "conversas", f"{tel}.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=1)
    if i < 8:
        ult = m[-1]
        leads[tel] = {"contato_id": f"fake-{i+1}", "nome": nome, "empresa": emp, "cidade": cid, "estagio": est,
                      "ultima_msg_ts": ult["quando"], "ultima_msg_de": ult["de"], "followups": 0, "nao_e_lead": False,
                      "resumo": f"Lead fictício de {emp} ({cid}), estágio {est}.", "temperatura": "morno", "origem": "exemplo"}
with open(os.path.join(SAIDA, "leads.json"), "w", encoding="utf-8") as f:
    json.dump(leads, f, ensure_ascii=False, indent=1)
with open(os.path.join(SAIDA, "nao_sao_leads.json"), "w", encoding="utf-8") as f:
    json.dump({"numeros": ["15550100999"]}, f)
with open(os.path.join(SAIDA, "config.json"), "w", encoding="utf-8") as f:
    json.dump({"grupo_alertas": ""}, f)
print("dados fictícios gerados em", SAIDA)
