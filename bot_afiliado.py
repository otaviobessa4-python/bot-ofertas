import html
import json
import logging
import os
import re
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
CHANNEL_ID = os.environ["CHANNEL_ID"]
HORA_INICIO = int(os.getenv("HORA_INICIO", "8"))
HORA_FIM = int(os.getenv("HORA_FIM", "22"))

PASTA = os.path.dirname(os.path.abspath(__file__))
FILA_PATH = os.path.join(PASTA, "fila.json")
CONFIG_PATH = os.path.join(PASTA, "config.json")
MAX_ERROS = 3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot_afiliado")

URL_RE = re.compile(r"https?://\S+")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TelegramBot/1.0)"}


def carregar(path, padrao):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return padrao


def salvar(path, dados):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)


def intervalo_atual():
    return int(carregar(CONFIG_PATH, {}).get("intervalo", 30))


def og(page, prop):
    for pattern in (
        rf'<meta[^>]+property=["\']og:{prop}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:{prop}["\']',
    ):
        m = re.search(pattern, page, re.I)
        if m:
            return html.unescape(m.group(1)).strip()
    return None


def buscar_produto(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        r.raise_for_status()
        return {"titulo": og(r.text, "title"), "imagem": og(r.text, "image")}
    except Exception as e:
        log.warning("Nao consegui ler a pagina: %s", e)
        return {"titulo": None, "imagem": None}


def montar_legenda(titulo, preco, nota):
    linhas = ["🔥 <b>OFERTA</b>"]
    if titulo:
        linhas.append(f"\n{html.escape(titulo)}")
    if preco:
        linhas.append(f"\n💰 <b>{html.escape(preco)}</b>")
    if nota:
        linhas.append(f"\n{html.escape(nota)}")
    linhas.append("\n<i>Link de afiliado: posso receber comissao pelas compras.</i>")
    return "\n".join(linhas)


async def publicar(bot, item):
    produto = buscar_produto(item["link"])
    legenda = montar_legenda(produto["titulo"], item.get("preco"), item.get("nota"))
    botao = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛒 Comprar no Mercado Livre", url=item["link"])]]
    )
    if produto["imagem"]:
        await bot.send_photo(
            CHANNEL_ID, produto["imagem"], caption=legenda,
            parse_mode=ParseMode.HTML, reply_markup=botao,
        )
    else:
        await bot.send_message(
            CHANNEL_ID, legenda, parse_mode=ParseMode.HTML,
            reply_markup=botao, disable_web_page_preview=True,
        )


async def postar_proximo(bot):
    fila = carregar(FILA_PATH, [])
    if not fila:
        return "vazia"
    item = fila[0]
    try:
        await publicar(bot, item)
    except Exception as e:
        log.exception("Erro ao publicar")
        fila = carregar(FILA_PATH, [])
        if fila and fila[0]["link"] == item["link"]:
            fila[0]["erros"] = fila[0].get("erros", 0) + 1
            if fila[0]["erros"] >= MAX_ERROS:
                fila.pop(0)
                await bot.send_message(
                    OWNER_ID,
                    f"Removi um item da fila apos {MAX_ERROS} erros:\n{item['link']}\n{e}",
                )
            salvar(FILA_PATH, fila)
        return "erro"
    fila = carregar(FILA_PATH, [])
    if fila and fila[0]["link"] == item["link"]:
        fila.pop(0)
        salvar(FILA_PATH, fila)
    return "ok"


async def job_postar(context: ContextTypes.DEFAULT_TYPE):
    if not (HORA_INICIO <= datetime.now().hour < HORA_FIM):
        return
    await postar_proximo(context.bot)


def agendar(job_queue, minutos):
    for job in job_queue.get_jobs_by_name("postador"):
        job.schedule_removal()
    job_queue.run_repeating(job_postar, interval=minutos * 60, first=10, name="postador")


def e_dono(update: Update):
    return update.effective_user is not None and update.effective_user.id == OWNER_ID


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not e_dono(update):
        await update.message.reply_text("Este bot e privado.")
        return
    await update.message.reply_text(
        "Pronto! Me mande seus links de afiliado do Mercado Livre.\n"
        "Opcional: 2a linha = preco, 3a linha = observacao.\n\n"
        f"Eu posto 1 a cada {intervalo_atual()} min, das {HORA_INICIO}h as {HORA_FIM}h.\n\n"
        "/fila  /agora  /intervalo N  /remover N  /limpar"
    )


async def cmd_fila(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not e_dono(update):
        return
    fila = carregar(FILA_PATH, [])
    if not fila:
        await update.message.reply_text("Fila vazia.")
        return
    linhas = [f"{i}. {it['link'][:50]}" for i, it in enumerate(fila, 1)]
    await update.message.reply_text(
        f"Fila ({len(fila)}), 1 a cada {intervalo_atual()} min:\n" + "\n".join(linhas),
        disable_web_page_preview=True,
    )


async def cmd_agora(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not e_dono(update):
        return
    r = await postar_proximo(context.bot)
    msgs = {"vazia": "Fila vazia.", "ok": "Publicado!", "erro": "Erro ao publicar (veja o log)."}
    await update.message.reply_text(msgs[r])


async def cmd_intervalo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not e_dono(update):
        return
    try:
        minutos = int(context.args[0])
        if minutos < 5:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text("Use: /intervalo 30  (minimo 5 minutos)")
        return
    salvar(CONFIG_PATH, {"intervalo": minutos})
    agendar(context.application.job_queue, minutos)
    await update.message.reply_text(f"Agora posto 1 a cada {minutos} min.")


async def cmd_remover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not e_dono(update):
        return
    fila = carregar(FILA_PATH, [])
    try:
        pos = int(context.args[0])
        if pos < 1:
            raise IndexError
        fila.pop(pos - 1)
    except (IndexError, ValueError):
        await update.message.reply_text("Use: /remover 2  (numero que aparece em /fila)")
        return
    salvar(FILA_PATH, fila)
    await update.message.reply_text("Removido.")


async def cmd_limpar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not e_dono(update):
        return
    salvar(FILA_PATH, [])
    await update.message.reply_text("Fila esvaziada.")


async def receber(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not e_dono(update):
        return
    linhas = [l.strip() for l in update.message.text.splitlines() if l.strip()]
    m = URL_RE.search(linhas[0]) if linhas else None
    if not m:
        await update.message.reply_text("Mande o link de afiliado na primeira linha.")
        return
    item = {
        "link": m.group(0),
        "preco": linhas[1] if len(linhas) > 1 else None,
        "nota": "\n".join(linhas[2:]) if len(linhas) > 2 else None,
        "erros": 0,
    }
    fila = carregar(FILA_PATH, [])
    fila.append(item)
    salvar(FILA_PATH, fila)
    await update.message.reply_text(
        f"Na fila (posicao {len(fila)}). Posto 1 a cada {intervalo_atual()} min."
    )


class Saude(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def servidor_saude():
    porta = os.getenv("PORT")
    if not porta:
        return
    servidor = HTTPServer(("0.0.0.0", int(porta)), Saude)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()


def main():
    servidor_saude()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("fila", cmd_fila))
    app.add_handler(CommandHandler("agora", cmd_agora))
    app.add_handler(CommandHandler("intervalo", cmd_intervalo))
    app.add_handler(CommandHandler("remover", cmd_remover))
    app.add_handler(CommandHandler("limpar", cmd_limpar))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receber))
    agendar(app.job_queue, intervalo_atual())
    log.info("Bot rodando...")
    app.run_polling()


if __name__ == "__main__":
    main()
