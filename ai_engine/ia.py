"""
Ponto único de acesso à IA do agente. Todo o resto (brain.py, assistente.py, pesquisa.py) chama ia.chamar().

Configuração no ai_engine\\.env:
    ANTHROPIC_API_KEY=...            chave da API da Anthropic (platform.claude.com)
    IA_PROVEDOR=claude               claude (padrão quando há chave) ou ollama (modelo local, grátis)
    CLAUDE_MODELO=claude-sonnet-5-5  qualquer modelo Claude
    CLAUDE_ESFORCO=medium            low | medium | high | xhigh | max
    OLLAMA_MODEL=qwen3:14b           usado no modo ollama e como reserva se a API falhar

Se a API da Anthropic falhar (sem internet, sem crédito), a chamada cai no Ollama local, se ele estiver ligado.
"""
import json
import os
import re

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

TEM_CHAVE = bool(os.environ.get("ANTHROPIC_API_KEY"))
PROVEDOR = os.environ.get("IA_PROVEDOR", "claude" if TEM_CHAVE else "ollama").lower()
CLAUDE_MODELO = os.environ.get("CLAUDE_MODELO", "claude-sonnet-5-5")
CLAUDE_ESFORCO = os.environ.get("CLAUDE_ESFORCO", "medium")
OLLAMA_MODELO = os.environ.get("OLLAMA_MODEL", "qwen3:14b")
OLLAMA = "http://localhost:11434"

_cliente = None
gasto = {"chamadas": 0, "entrada": 0, "saida": 0}  # tokens usados desde que o cérebro ligou


def descricao() -> str:
    if PROVEDOR == "claude":
        return f"{CLAUDE_MODELO} (esforço {CLAUDE_ESFORCO})"
    return f"{OLLAMA_MODELO} (local)"


def _cliente_claude():
    global _cliente
    if _cliente is None:
        import anthropic
        _cliente = anthropic.Anthropic()
    return _cliente


def _so_json(texto: str) -> str:
    """Tira texto antes/depois do JSON (e cercas ```json), caso o modelo escreva algo a mais."""
    texto = re.sub(r"^```(?:json)?\s*|\s*```$", "", texto.strip())
    ini, fim = texto.find("{"), texto.rfind("}")
    return texto[ini:fim + 1] if ini != -1 and fim > ini else texto


def _claude(prompt: str, json_saida: bool) -> str:
    import anthropic
    if json_saida:
        prompt += "\n\nResponda SOMENTE com o JSON pedido, sem nenhum texto antes ou depois."
    try:
        r = _cliente_claude().beta.messages.create(
            model=CLAUDE_MODELO,
            max_tokens=16000,
            output_config={"effort": CLAUDE_ESFORCO},
            # se o modelo recusar por segurança, a própria API refaz com um modelo reserva
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIStatusError as e:
        raise requests.RequestException(f"API Anthropic {e.status_code}: {str(e.message)[:200]}") from e
    except anthropic.APIConnectionError as e:
        raise requests.RequestException(f"Sem conexão com a API Anthropic: {e}") from e
    gasto["chamadas"] += 1
    gasto["entrada"] += getattr(r.usage, "input_tokens", 0) or 0
    gasto["saida"] += getattr(r.usage, "output_tokens", 0) or 0
    if r.stop_reason == "refusal":
        print("A IA recusou responder este pedido.")
        return "{}" if json_saida else ""
    texto = "".join(b.text for b in r.content if b.type == "text").strip()
    return _so_json(texto) if json_saida else texto


def _ollama(prompt: str, json_saida: bool, ctx: int, limite: int) -> str:
    corpo = {"model": OLLAMA_MODELO, "prompt": prompt, "stream": False, "keep_alive": "30m",
             "options": {"temperature": 0, "num_ctx": ctx, "num_predict": limite}}
    if json_saida:
        corpo["format"] = "json"
    if OLLAMA_MODELO.startswith(("qwen3", "deepseek-r1")):
        corpo["think"] = False
    r = requests.post(f"{OLLAMA}/api/generate", json=corpo, timeout=400)
    r.raise_for_status()
    return re.sub(r"<think>.*?</think>", "", r.json().get("response", ""), flags=re.S).strip()


def chamar(prompt: str, json_saida: bool = False, ctx: int = 16384, limite: int = 2500) -> str:
    """Manda o prompt para a IA configurada e devolve o texto (ou só o JSON, se json_saida=True).
    ctx/limite só valem para o Ollama. Erros saem como requests.RequestException."""
    if PROVEDOR == "claude":
        try:
            return _claude(prompt, json_saida)
        except requests.RequestException as e:
            print(f"{e} -> tentando o modelo local (Ollama)")
            try:
                return _ollama(prompt, json_saida, ctx, limite)
            except requests.RequestException:
                raise e
    return _ollama(prompt, json_saida, ctx, limite)


def chamar_json(prompt: str, **kw) -> dict:
    return json.loads(chamar(prompt, json_saida=True, **kw) or "{}")


def disponivel() -> bool:
    if PROVEDOR == "claude":
        return TEM_CHAVE
    try:
        requests.get(f"{OLLAMA}/api/tags", timeout=3).raise_for_status()
        return True
    except requests.RequestException:
        return False
