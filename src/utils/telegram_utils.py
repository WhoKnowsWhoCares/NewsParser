import os
import httpx

from loguru import logger
import requests
import json

chat_ids = None

def get_all_subscribers(bot_token):
    url = f'https://api.telegram.org/bot{bot_token}/getUpdates'
    try:
        doc = requests.get(url).text
        result = json.loads(doc).get('result')
        ids = list(set([elem['message']['chat']['id'] for elem in result]))
    except Exception as e:
        logger.error(f'Error geting chat ids\n{e}')
    return ids


async def send_tg_message_to_all(message, check_pattern_func=None):
    global chat_ids
    logger.info("Sending message to telegram")
    bot_token = os.getenv('bot_token')    
    if chat_ids is None:
        chat_ids = get_all_subscribers(bot_token)
    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    text = "\n\n".join(filter(None, (message.get('title'),message.get('summary'),message.get('link'))))
    if not (check_pattern_func is None):
        if not check_pattern_func(text):
            return
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    status = []
    for id in chat_ids:
        params = {
            'text': text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": False,
            "reply_to_message_id": None,
            "chat_id": str(id)
        }
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, params=params, headers=headers)
                response.raise_for_status()
        except Exception as e:
            logger.error(e)
            status.append(-1)
        status.append(response.status_code)
    return status
    

async def send_tg_message(message, check_pattern_func=None):
    '''Через бот отправляет сообщение напрямую в канал через telegram api'''
    logger.info("Sending message to telegram")
    chat_id = os.getenv('chat_id')
    bot_token = os.getenv('bot_token')    
    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'

    text = "\n\n".join(filter(None, (message.get('title'),message.get('summary'),message.get('link'))))
    if not (check_pattern_func is None):
        if not check_pattern_func(text):
            return
    params = {
        'text': text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": False,
        "reply_to_message_id": None,
        "chat_id": str(chat_id)
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
    except Exception as e:
        logger.error(e)
        return -1
    logger.info(f"Sent successfully. Title: {message.get('title','')}")
    return response.status_code
