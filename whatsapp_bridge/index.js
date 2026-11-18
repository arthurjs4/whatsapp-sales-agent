/*
  Ponte WhatsApp <-> Cérebro (brain.py)

  - Acompanha TODAS as conversas individuais (as do lead e as suas), inclusive
    prospecções sem resposta, para controlar os follow-ups.
  - Transcreve áudios antes da análise.
  - Avisa no grupo da equipe: Loom aceito, lead respondeu, reunião marcada, fechamento,
    e manda o relatório de pendências nos horários definidos.

  Uso:
    node index.js                  -> modo normal
    node index.js --importar tudo  -> analisa todo o histórico e segue ao vivo
    node index.js --importar 30    -> só os últimos 30 dias

  No grupo da equipe:
    !agente     -> define este grupo como o grupo de alertas
    !relatorio  -> manda as pendências agora
    !ajuda      -> lista os comandos
*/
require('dotenv').config();
const path = require('path');
const fs = require('fs');
const { exec } = require('child_process');
const QRCode = require('qrcode');
// Correção: ao preparar o WhatsApp, a biblioteca registra ~25 funções na página uma por uma. Às vezes uma parte
// do WhatsApp Web não responde e um desses registros trava para sempre (o agente nunca fica "pronto").
// Aqui cada registro tem limite de 15 s; se travar, segue em frente (os eventos que usamos já estão registrados antes).
// Precisa vir ANTES do require('whatsapp-web.js').
const utilPuppeteer = require('whatsapp-web.js/src/util/Puppeteer');
utilPuppeteer.exposeFunctionIfAbsent = async (page, nome, fn) => {
    const existe = await page.evaluate((n) => !!window[n], nome);
    if (existe) return;
    let travou = true;
    const registro = page.exposeFunction(nome, fn).catch(() => {}).finally(() => { travou = false; });
    await Promise.race([registro, new Promise((r) => setTimeout(r, 15000))]);
    if (travou) console.log('(aviso: o WhatsApp demorou para registrar "' + nome + '"; seguindo sem esperar)');
};
const { Client, LocalAuth } = require('whatsapp-web.js');

const BRAIN = 'http://127.0.0.1:5000';
const WEBHOOK_SECRET = process.env.WEBHOOK_SECRET;
const ESPERA_MS = Number(process.env.ESPERA_SEGUNDOS || 30) * 1000;      // conversa "assenta" antes de analisar
const ANALISES_SIMULTANEAS = Number(process.env.ANALISES_SIMULTANEAS || 2);
const HORARIOS = (process.env.ALERTA_HORARIOS || '09:00,15:00').split(',').map((h) => h.trim()).filter(Boolean);
const VARREDURA_MIN = Number(process.env.VARREDURA_MINUTOS || 30);
const MSGS_POR_ANALISE = 40;
const CONFIG_ARQ = path.join(__dirname, '..', 'data_storage', 'config.json');

if (!WEBHOOK_SECRET) {
    console.error('Falta WEBHOOK_SECRET no arquivo .env do whatsapp_bridge.');
    process.exit(1);
}

const argImportar = process.argv.indexOf('--importar');
const valorImportar = argImportar === -1 ? '' : String(process.argv[argImportar + 1] || '');
const IMPORTAR_DIAS = argImportar === -1 ? 0
    : valorImportar.toLowerCase() === 'tudo' ? 3650
    : Number(valorImportar) || 90;

// Conectar por número (código de pareamento) em vez de QR Code: --numero 5511999990000
const argNumero = process.argv.indexOf('--numero');
const NUMERO_PAREAMENTO = argNumero === -1 ? '' : String(process.argv[argNumero + 1] || '').replace(/\D/g, '');
if (argNumero !== -1 && (NUMERO_PAREAMENTO.length < 10 || NUMERO_PAREAMENTO.length > 15)) {
    console.error('Número inválido. Use o formato internacional só com dígitos, ex.: --numero 5511999990000');
    process.exit(1);
}

// ---------------------------------------------------------------- configuração salva
function lerConfig() {
    try { return JSON.parse(fs.readFileSync(CONFIG_ARQ, 'utf8')); } catch { return {}; }
}
function salvarConfig(c) {
    fs.mkdirSync(path.dirname(CONFIG_ARQ), { recursive: true });
    fs.writeFileSync(CONFIG_ARQ, JSON.stringify(c, null, 1));
}
let config = lerConfig();

// ---------------------------------------------------------------- QR Code
const QR_ARQUIVO = path.join(__dirname, 'qrcode.png');
const QR_PAGINA = path.join(__dirname, 'qrcode.html');
const HTML_QR = `<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<title>Conectar WhatsApp</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f7f3ee;font-family:system-ui,sans-serif;color:#0a0c10}
.box{background:#fff;padding:28px;border-radius:20px;text-align:center;box-shadow:0 20px 50px -25px rgba(0,0,0,.35)}
img{width:340px;height:340px;display:block;margin:0 auto}p{margin:16px 0 0;font-size:15px}small{color:#677080}</style></head>
<body><div class="box"><img id="qr" src="qrcode.png" alt="QR Code do WhatsApp">
<p>WhatsApp &gt; Aparelhos conectados &gt; Conectar um aparelho</p>
<small>O código se atualiza sozinho. Pode fechar esta aba depois de conectar.</small></div>
<script>setInterval(function(){document.getElementById('qr').src='qrcode.png?t='+Date.now()},2000)</script></body></html>`;

const client = new Client({
    authStrategy: new LocalAuth(),
    puppeteer: { headless: true, args: ['--no-sandbox', '--disable-setuid-sandbox'] },
    ...(NUMERO_PAREAMENTO ? { pairWithPhoneNumber: { phoneNumber: NUMERO_PAREAMENTO, showNotification: true, intervalMs: 180000 } } : {})
});

// Código de pareamento (quando conecta por número)
const CODIGO_PAGINA = path.join(__dirname, 'codigo.html');
let codigoAberto = false;
client.on('code', (codigo) => {
    const formatado = String(codigo).replace(/^(.{4})(.{4})$/, '$1-$2');
    console.log('\n==============================================');
    console.log('  CÓDIGO PARA CONECTAR O WHATSAPP: ' + formatado);
    console.log('==============================================');
    console.log('No celular do número +' + NUMERO_PAREAMENTO + ':');
    console.log('  WhatsApp > Aparelhos conectados > Conectar um aparelho >');
    console.log('  "Conectar com número de telefone" > digite o código.');
    console.log('(Também chega uma notificação do WhatsApp no celular. O código muda a cada 3 minutos.)\n');
    const html = `<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>Conectar WhatsApp</title>
<meta http-equiv="refresh" content="20">
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f7f3ee;font-family:system-ui,sans-serif;color:#0a0c10}
.box{background:#fff;padding:36px 40px;border-radius:20px;text-align:center;box-shadow:0 20px 50px -25px rgba(0,0,0,.35);max-width:440px}
.cod{font:700 48px/1 ui-monospace,Consolas,monospace;letter-spacing:.12em;margin:18px 0;color:#0a0c10}
ol{text-align:left;font-size:15px;line-height:1.7;padding-left:20px}small{color:#677080}</style></head>
<body><div class="box"><div>Código para o número <b>+${NUMERO_PAREAMENTO}</b></div><div class="cod">${formatado}</div>
<ol><li>No celular, abra o WhatsApp</li><li><b>Aparelhos conectados</b> &gt; <b>Conectar um aparelho</b></li>
<li>Toque em <b>Conectar com número de telefone</b></li><li>Digite o código acima</li></ol>
<small>O código muda a cada 3 minutos; esta página se atualiza sozinha. Também chega uma notificação no celular.</small></div></body></html>`;
    fs.writeFileSync(CODIGO_PAGINA, html);
    if (!codigoAberto) {
        exec('start "" "' + CODIGO_PAGINA + '"');
        codigoAberto = true;
    }
});

let qrAberto = false;
client.on('qr', async (qr) => {
    try {
        await QRCode.toFile(QR_ARQUIVO, qr, { width: 420, margin: 2 });
        if (!qrAberto) {
            fs.writeFileSync(QR_PAGINA, HTML_QR);
            exec('start "" "' + QR_PAGINA + '"');
            qrAberto = true;
            console.log('QR Code aberto no navegador. Ele se atualiza sozinho.');
        }
    } catch (e) {
        console.error('Não consegui gerar a imagem do QR Code:', e.message);
    }
});
client.on('auth_failure', (m) => console.error('Falha na autenticação:', m));

// Às vezes o WhatsApp abre e conecta, mas a biblioteca trava no meio da preparação e nunca avisa "pronto".
// Se em 8 min não ficar pronto, o agente reinicia sozinho (código 2 = o .bat liga de novo).
let prontoEm = 0;
client.on('authenticated', () => {
    console.log('WhatsApp aberto, preparando o agente...');
    setTimeout(() => {
        if (!prontoEm) {
            console.log('O WhatsApp abriu, mas o agente não terminou de carregar. Reiniciando em instantes...');
            client.destroy().catch(() => {}).finally(() => process.exit(2));
        }
    }, 480000);
});
client.on('ready', () => { prontoEm = Date.now(); });
client.on('disconnected', async (motivo) => {
    console.log('Desconectado do WhatsApp:', motivo);
    await client.destroy().catch(() => {});
    process.exit(1); // o iniciar.bat religa sozinho
});

// ---------------------------------------------------------------- utilidades
function conversaValida(chatId) {
    return chatId && !chatId.endsWith('@g.us') && !chatId.endsWith('@newsletter')
        && !chatId.endsWith('@broadcast') && chatId !== 'status@broadcast';
}

const TIPOS_DE_SISTEMA = new Set(['e2e_notification', 'notification_template', 'notification', 'gp2',
    'protocol', 'ciphertext', 'revoked', 'call_log_missed']);
const ROTULO_MIDIA = {
    image: '[imagem]', video: '[vídeo]', document: '[documento]', sticker: '[figurinha]',
    location: '[localização]', vcard: '[contato]', call_log: '[ligação]'
};

async function chamarCerebro(rota, metodo, corpo, limiteMs) {
    const resp = await fetch(BRAIN + rota, {
        method: metodo,
        headers: { 'Content-Type': 'application/json', 'X-Webhook-Secret': WEBHOOK_SECRET },
        body: corpo ? JSON.stringify(corpo) : undefined,
        signal: AbortSignal.timeout(limiteMs || 240000)
    });
    const texto = await resp.text();
    let json; try { json = JSON.parse(texto); } catch { json = { status: texto }; }
    if (!resp.ok) throw new Error('Cérebro respondeu ' + resp.status + ': ' + texto.slice(0, 200));
    return json;
}

async function telefoneReal(chatId) {
    if (chatId.endsWith('@c.us')) return chatId.split('@')[0];
    try {
        const [info] = await client.getContactLidAndPhone([chatId]);
        if (info && info.pn) return info.pn.split('@')[0];
    } catch (e) {
        console.warn('Não consegui converter ' + chatId + ' em telefone:', e.message);
    }
    return null;
}

const transcricoes = new Map();    // id da mensagem -> texto do áudio
const ultimoAnalisado = new Map(); // conversa -> horário da última mensagem já analisada

async function textoDaMensagem(m) {
    if (m.type === 'chat') return m.body;
    if (m.type === 'ptt' || m.type === 'audio') {
        const id = m.id._serialized;
        if (transcricoes.has(id)) return transcricoes.get(id);
        let texto = '[áudio]';
        try {
            const midia = await m.downloadMedia();
            if (midia && midia.data) {
                const r = await chamarCerebro('/transcrever', 'POST', { id, dados: midia.data }, 120000);
                if (r.texto) texto = '[áudio] ' + r.texto;
            }
        } catch (_) { /* áudio antigo indisponível */ }
        transcricoes.set(id, texto);
        return texto;
    }
    const rotulo = ROTULO_MIDIA[m.type] || '[' + m.type + ']';
    return m.body && m.body.length < 300 ? rotulo + ' ' + m.body : rotulo;
}

async function montarAnalise(chatId, origem) {
    const chat = await client.getChatById(chatId);
    const brutas = (await chat.fetchMessages({ limit: MSGS_POR_ANALISE })).filter((m) => !TIPOS_DE_SISTEMA.has(m.type));
    const mensagens = [];
    for (const m of brutas) {
        const texto = await textoDaMensagem(m);
        if (texto && texto.trim()) mensagens.push({ de: m.fromMe ? 'agencia' : 'cliente', texto, quando: m.timestamp });
    }
    let nome = '';
    try {
        const contato = await chat.getContact();
        nome = contato.pushname || contato.name || '';
    } catch (_) { /* sem nome */ }
    return { phone: await telefoneReal(chatId), nome_whatsapp: nome, mensagens, origem, ultima: chat.timestamp };
}

// ---------------------------------------------------------------- grupo da equipe
const enviadasPeloAgente = new Set(); // ids das mensagens que o próprio agente mandou (não responde a si mesmo)

function lembrar(m) {
    if (m && m.id && m.id._serialized) {
        enviadasPeloAgente.add(m.id._serialized);
        if (enviadasPeloAgente.size > 500) enviadasPeloAgente.delete(enviadasPeloAgente.values().next().value);
    }
    return m;
}

async function mandar(chatId, texto) {
    return lembrar(await client.sendMessage(chatId, texto));
}

async function avisarGrupo(texto) {
    if (!config.grupo_alertas || !texto) return;
    try {
        await mandar(config.grupo_alertas, texto);
    } catch (e) {
        console.error('Não consegui mandar no grupo:', e.message);
    }
}

// Conversa: "Chopp, ..." ou resposta a uma mensagem do Chopp
async function chamaramOAgente(msg) {
    if (/^@?(chopp|chop|agente)\b/i.test((msg.body || '').trim())) return true;
    if (msg.hasQuotedMsg) {
        try {
            const citada = await msg.getQuotedMessage();
            return citada && enviadasPeloAgente.has(citada.id._serialized);
        } catch (_) { return false; }
    }
    return false;
}

// Pesquisa de telefone: o cérebro marca [[WA:5511...]] e aqui o WhatsApp confere se o número tem conta
// (só consulta; não manda nada para o número).
async function conferirWhatsapps(texto) {
    const marcas = [...new Set((texto.match(/\[\[WA:(\d{10,15})\]\]/g) || []))];
    for (const marca of marcas) {
        const numero = marca.slice(5, -2);
        let resultado = '❓ não consegui conferir';
        try {
            const id = await client.getNumberId(numero);
            resultado = id ? '✅ tem WhatsApp' : '❌ sem WhatsApp';
        } catch (_) { /* fica o ❓ */ }
        texto = texto.split(marca).join(resultado);
    }
    return texto;
}

async function responderNoGrupo(msg) {
    const pergunta = (msg.body || '').trim().replace(/^@?(chopp|chop|agente)[\s,:!?-]*/i, '').trim();
    let autor = '';
    try { const c = await msg.getContact(); autor = c.pushname || c.name || ''; } catch (_) { /* sem nome */ }
    console.log('Pergunta no grupo (' + (autor || 'sócio') + '): ' + pergunta.slice(0, 80));
    try {
        await msg.react(/pesquis|internet|confer|verific|descobr|telefone|whats|complet/i.test(pergunta) ? '🔎' : '👀');
    } catch (_) { /* reação é opcional */ }
    try {
        const r = await chamarCerebro('/perguntar', 'POST', { pergunta, autor }, 900000);
        lembrar(await msg.reply(await conferirWhatsapps(r.resposta)));
    } catch (e) {
        lembrar(await msg.reply('🍺 Tive um problema para responder agora (' + e.message.slice(0, 80) + ').'));
    }
    try { await msg.react(''); } catch (_) { /* remove o 👀 */ }
}

async function enviarRelatorio(motivo) {
    try {
        const r = await chamarCerebro('/pendencias', 'GET', null, 120000);
        await avisarGrupo(r.texto);
        console.log('Relatório enviado ao grupo (' + motivo + ').');
    } catch (e) {
        console.error('Erro ao montar o relatório:', e.message);
    }
}

async function enviarMetricas() {
    try {
        const r = await chamarCerebro('/metricas', 'GET', null, 60000);
        await avisarGrupo(r.texto);
    } catch (e) {
        console.error('Erro ao montar as métricas:', e.message);
    }
}

// Playbook: lembrete 1h antes de cada call (confere a cada 5 min)
setInterval(async () => {
    if (!client.info || !config.grupo_alertas) return;
    try {
        const r = await chamarCerebro('/lembretes', 'GET', null, 30000);
        for (const aviso of r.avisos || []) await avisarGrupo(aviso);
    } catch (_) { /* cérebro fora do ar: tenta na próxima */ }
}, 5 * 60000);

const AJUDA = '🍺 *Chopp* · como falar comigo (comece com *Chopp*)\n\n'
    + '*Consultar*\n'
    + '• _Chopp, quem pediu o Loom e ainda não recebeu?_\n'
    + '• _Chopp, como está o Academia Exemplo?_\n\n'
    + '*Como prosseguir*\n'
    + '• _Chopp, como prosseguir com o Academia Exemplo?_\n'
    + '• _Chopp, como reativar todos os leads perdidos?_\n\n'
    + '*Mensagens prontas*\n'
    + '• _Chopp, monte uma mensagem pro Academia Exemplo, enviei o Loom e ele ainda não viu_\n\n'
    + '*Loom*\n'
    + '• _Chopp, faça a copy do Loom pra Café Exemplo_\n\n'
    + '*Pesquisa e cadastro*\n'
    + '• _Chopp, pesquise a Café Exemplo na internet_ (só salvo o que passar na conferência dupla)\n'
    + '• _Chopp, confere se o WhatsApp do Academia Exemplo está certo_ (✅/❌ sem mandar mensagem ao número)\n'
    + '• _Chopp, quem é o dono da Bowls Exemplo e qual o Instagram?_ (qualquer empresa, lead ou não)\n'
    + '• _Chopp, complete os dados que faltam da Contabil Exemplo_\n'
    + '• _Chopp, salva_ (grava no HubSpot o que a última pesquisa confirmou)\n'
    + '• _Chopp, cadastre: Empresa X, +1 555..., Orlando FL, dono João, site..._\n\n'
    + '*Comandos*: *!relatorio* · *!metricas* · *!chopp* (define este grupo) · *!ajuda*\n'
    + 'Ou responda a uma mensagem minha para continuar a conversa. Relatórios automáticos: ' + HORARIOS.join(' e ') + '.';

async function tratarComandoDeGrupo(msg, grupoId) {
    const cmd = (msg.body || '').trim().toLowerCase();
    if (cmd === '!chopp' || cmd === '!agente') {
        config.grupo_alertas = grupoId;
        salvarConfig(config);
        console.log('Grupo de alertas configurado: ' + grupoId);
        await mandar(grupoId, '🍺 Fala, time! Aqui é o *Chopp*. Este grupo agora recebe os meus alertas.\n\n' + AJUDA);
        return;
    }
    if (grupoId !== config.grupo_alertas) return;
    if (cmd === '!relatorio' || cmd === '!relatório' || cmd === '!pendencias') await enviarRelatorio('pedido no grupo');
    else if (cmd === '!metricas' || cmd === '!métricas') await enviarMetricas();
    else if (cmd === '!ajuda') await mandar(grupoId, AJUDA);
}

// ---------------------------------------------------------------- fila de análises
const fila = [];
const naFila = new Set();
const emAnalise = new Set();
let trabalhadores = 0;
let importacao = null; // { total, feitas, novos, ia }

function enfileirar(chatId, origem) {
    if (naFila.has(chatId)) return;
    naFila.add(chatId);
    fila.push({ chatId, origem });
    while (trabalhadores < ANALISES_SIMULTANEAS) {
        trabalhadores++;
        processarFila().finally(() => { trabalhadores--; verificarFimDaImportacao(); });
    }
}

async function processarFila() {
    while (fila.length) {
        const i = fila.findIndex((item) => !emAnalise.has(item.chatId));
        if (i === -1) return;
        const { chatId, origem } = fila.splice(i, 1)[0];
        naFila.delete(chatId);
        emAnalise.add(chatId);
        try {
            const dados = await montarAnalise(chatId, origem);
            if (!dados.phone || !dados.mensagens.length) continue;
            const inicio = Date.now();
            const r = await chamarCerebro('/analisar', 'POST', dados);
            ultimoAnalisado.set(chatId, dados.ultima);
            const seg = ((Date.now() - inicio) / 1000).toFixed(1);
            if (importacao && origem === 'importacao') {
                importacao.feitas++;
                if (r.contato_criado) importacao.novos++;
            }
            if (r.status === 'atualizado' || r.status === 'teste') {
                const de = r.anterior && r.mudou ? r.anterior + ' -> ' : '';
                console.log('[' + dados.phone + '] ' + (r.contato_criado ? 'NOVO no CRM, ' : '') + de + r.estagio
                    + (r.mudou ? '' : ' (sem mudança)') + ' · ' + (r.usou_ia ? 'IA ' : 'regra ') + seg + 's · fila ' + fila.length);
            } else if (r.status !== 'sem_mudanca') {
                console.log('[' + dados.phone + '] ' + r.status + ' · fila ' + fila.length);
            }
            if (r.alerta) await avisarGrupo(r.alerta);
        } catch (e) {
            console.error('[' + chatId + '] Erro:', e.name === 'TimeoutError' ? 'o Cérebro demorou demais' : e.message);
        } finally {
            emAnalise.delete(chatId);
        }
    }
}

function verificarFimDaImportacao() {
    if (!importacao || fila.length || emAnalise.size || trabalhadores) return;
    const resumo = importacao;
    importacao = null;
    console.log('\nIMPORTAÇÃO CONCLUÍDA: ' + resumo.feitas + ' conversas analisadas, ' + resumo.novos + ' leads novos no CRM.\n');
    avisarGrupo('📥 *Importação concluída*: ' + resumo.feitas + ' conversas analisadas, ' + resumo.novos
        + ' leads novos no CRM.').then(() => enviarRelatorio('fim da importação'));
}

// ---------------------------------------------------------------- mensagens ao vivo
const timers = new Map();

client.on('message_create', async (msg) => {
    const chatId = msg.fromMe ? msg.to : msg.from;

    if (chatId && chatId.endsWith('@g.us')) {
        if (enviadasPeloAgente.has(msg.id._serialized)) return; // mensagem do próprio agente
        try {
            if ((msg.body || '').trim().startsWith('!')) await tratarComandoDeGrupo(msg, chatId);
            else if (chatId === config.grupo_alertas && await chamaramOAgente(msg)) await responderNoGrupo(msg);
        } catch (e) {
            console.error('Erro no grupo:', e.message);
        }
        return;
    }
    if (!conversaValida(chatId) || TIPOS_DE_SISTEMA.has(msg.type)) return;

    const previa = msg.type === 'chat' ? msg.body.slice(0, 60)
        : (msg.type === 'ptt' || msg.type === 'audio') ? '[áudio]' : (ROTULO_MIDIA[msg.type] || msg.type);
    console.log('  ' + chatId.split('@')[0] + ' (' + (msg.fromMe ? 'você' : 'lead') + '): ' + previa);

    clearTimeout(timers.get(chatId));
    timers.set(chatId, setTimeout(() => { timers.delete(chatId); enfileirar(chatId, 'ao_vivo'); }, ESPERA_MS));
});

// ---------------------------------------------------------------- importação e varreduras
const esperar = (ms) => new Promise((r) => setTimeout(r, ms));

// Logo após conectar, o WhatsApp ainda está carregando e getChats() pode falhar.
// Em vez de cair, espera e tenta de novo.
// Lista leve: só id, se é grupo e a data da última mensagem. O getChats() da biblioteca carrega os dados de
// TODOS os grupos e falha inteiro (erro "r") se um único grupo der problema (ex.: grupo que você saiu).
async function listaLeveDeConversas() {
    const itens = await client.pupPage.evaluate(() => {
        const chats = window.require('WAWebCollections').Chat.getModelsArray();
        return chats.map((c) => ({
            id: c.id._serialized,
            isGroup: c.id.server === 'g.us' || !!c.groupMetadata,
            timestamp: c.t || 0,
        }));
    });
    return itens.map((c) => ({ id: { _serialized: c.id }, isGroup: c.isGroup, timestamp: c.timestamp }));
}

async function listarConversas(tentativas = 12) {
    for (let i = 1; i <= tentativas; i++) {
        try {
            try {
                return await listaLeveDeConversas();
            } catch (_) {
                return await client.getChats();
            }
        } catch (e) {
            if (i === tentativas) throw e;
            console.log('WhatsApp ainda carregando as conversas... nova tentativa em 15s (' + i + '/' + tentativas + ')');
            await esperar(15000);
        }
    }
    return [];
}

async function esperarSincronizacao() {
    let anterior = -1;
    let estavel = 0;
    for (let i = 0; i < 20 && estavel < 2; i++) {
        const total = (await listarConversas()).length;
        console.log('Sincronizando conversas do WhatsApp... ' + total + ' encontradas');
        estavel = total === anterior ? estavel + 1 : 0;
        anterior = total;
        if (estavel < 2) await esperar(15000);
    }
}

async function conversasDesde(dias) {
    const limite = Math.floor(Date.now() / 1000) - dias * 86400;
    return (await listarConversas()).filter((c) => !c.isGroup && conversaValida(c.id._serialized)
        && c.timestamp && c.timestamp >= limite);
}

async function importarConversas(dias) {
    await esperarSincronizacao();
    const alvo = await conversasDesde(dias);
    const periodo = dias >= 3650 ? 'de todo o histórico' : 'dos últimos ' + dias + ' dias';
    const minutos = Math.ceil((alvo.length * 8) / ANALISES_SIMULTANEAS / 60);
    console.log('\nIMPORTAÇÃO: ' + alvo.length + ' conversas ' + periodo + ' na fila (até ~' + minutos + ' min; '
        + 'conversas sem resposta do lead são instantâneas).');
    console.log('Conversas pessoais são reconhecidas e não vão para o CRM.\n');
    importacao = { total: alvo.length, feitas: 0, novos: 0 };
    alvo.forEach((c) => enfileirar(c.id._serialized, 'importacao'));
    if (!alvo.length) verificarFimDaImportacao();
}

async function varredura(origem) {
    try {
        const alvo = await conversasDesde(30);
        // só conversas com mensagem nova desde a última análise
        alvo.filter((c) => (ultimoAnalisado.get(c.id._serialized) || 0) < c.timestamp)
            .forEach((c) => enfileirar(c.id._serialized, origem));
    } catch (e) {
        console.error('Erro na varredura:', e.message);
    }
}

// Relatórios nos horários definidos (uma vez por dia em cada horário)
const enviadosHoje = new Set();
setInterval(() => {
    const d = new Date();
    const hhmm = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
    const chave = d.toDateString() + ' ' + hhmm;
    if (HORARIOS.includes(hhmm) && !enviadosHoje.has(chave) && client.info) {
        enviadosHoje.add(chave);
        enviarRelatorio('horário ' + hhmm).then(() => {
            // toda segunda, no primeiro relatório do dia: métricas da semana (playbook)
            if (d.getDay() === 1 && hhmm === HORARIOS[0]) enviarMetricas();
        });
    }
}, 30000);

// ---------------------------------------------------------------- início
// Um erro inesperado é registrado, mas não derruba o agente.
process.on('unhandledRejection', (e) => console.error('Erro inesperado (o agente continua):', e && e.message ? e.message : e));
process.on('uncaughtException', (e) => console.error('Erro inesperado (o agente continua):', e && e.message ? e.message : e));

// Correção: nas conversas novas (@lid) a biblioteca quebra ao buscar a "última mensagem" no banco local do
// WhatsApp (erro "r" / IndexedDB DataError) e a conversa inteira falha. Se isso acontecer, monta a conversa
// sem esse campo (o agente lê as mensagens por outro caminho, então não perde nada).
async function corrigirPagina() {
    await client.pupPage.evaluate(() => {
        if (!window.WWebJS || window.WWebJS.__corrigidoChopp) return;
        const original = window.WWebJS.getChatModel;
        window.WWebJS.getChatModel = async (chat, opcoes) => {
            try {
                return await original(chat, opcoes);
            } catch (e) {
                const semUltima = new Proxy(chat, {
                    get(alvo, prop) {
                        if (prop === 'lastReceivedKey') return null;
                        const v = alvo[prop];
                        return typeof v === 'function' ? v.bind(alvo) : v;
                    },
                });
                return await original(semUltima, opcoes);
            }
        };
        window.WWebJS.__corrigidoChopp = true;
    });
}

client.on('ready', async () => {
  try {
    await corrigirPagina().catch((e) => console.error('Aviso: não consegui aplicar a correção das conversas:', e.message));
    console.log('O Agente está conectado ao WhatsApp!');
    fs.rm(QR_ARQUIVO, { force: true }, () => {});
    fs.rm(QR_PAGINA, { force: true }, () => {});
    fs.rm(CODIGO_PAGINA, { force: true }, () => {});

    try {
        const s = await chamarCerebro('/saude', 'GET', null, 5000);
        console.log('Cérebro: no ar | IA: ' + (s.ollama ? s.modelo : 'OLLAMA DESLIGADO')
            + ' | HubSpot: ' + (s.hubspot_ativo ? 'ATIVO' : 'modo teste'));
    } catch (_) {
        console.warn('ATENÇÃO: o Cérebro (python brain.py) não está respondendo. Ligue ele antes de usar.');
    }
    if (config.grupo_alertas) console.log('Grupo de alertas: configurado. Relatórios às ' + HORARIOS.join(' e ') + '.');
    else console.log('Grupo de alertas: NÃO configurado. Mande "!chopp" no grupo da equipe.');

    setInterval(() => varredura('ao_vivo'), VARREDURA_MIN * 60000);
    if (IMPORTAR_DIAS) {
        await importarConversas(IMPORTAR_DIAS);
    } else {
        await varredura('sincronizacao'); // pega o que chegou enquanto o agente estava desligado
    }
  } catch (e) {
    console.error('Não consegui ler as conversas agora (' + e.message + ').');
    console.error('O agente segue ligado acompanhando mensagens novas; a varredura tenta de novo em '
        + VARREDURA_MIN + ' min.' + (IMPORTAR_DIAS ? ' Para importar, rode o comando de importação de novo depois.' : ''));
  }
});

client.initialize().catch((e) => {
    const msg = String(e && e.message ? e.message : e);
    if (/already running/i.test(msg)) {
        console.log('\nJá existe outra janela do Agente usando este WhatsApp. Esta janela vai fechar:');
        console.log('a outra continua funcionando normalmente.');
        process.exit(3); // 3 = duplicado: o .bat não religa
    }
    console.error('Não consegui abrir o WhatsApp:', msg);
    process.exit(1);
});
