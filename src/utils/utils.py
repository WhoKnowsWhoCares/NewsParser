import random

from loguru import logger
from .user_agents import user_agent_list  # список из значений user-agent


def random_user_agent_headers(file = None):
    '''Возвращет рандомный user-agent и друге параметры для имитации запроса из браузера'''
    headers = None
    if file:
        try:
            with open(file,'r') as f:
                headers = f.readlines()
        except FileNotFoundError:
            logger.error('Could not get subscribers')
    if not headers:
        headers = user_agent_list
    rnd_index = random.randint(0, len(headers) - 1)
    header = {
        'User-Agent': headers[rnd_index],
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate, br',
        'DNT': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
    }
    return header


def check_gazp_pattern_func(text):
    '''Вибирай только посты или статьи про газпром или газ'''
    words = text.lower().split()
    key_words = [
        'газп',     # газпром
        'газо',     # газопровод, газофикация...
        'поток',    # сервеный поток, северный поток 2, южный поток
        'спг',      # спг - сжиженный природный газ
        'gazp',
    ]
    for word in words:
        if 'газ' in word and len(word) < 6:  # газ, газу, газом, газа
            return True
        for key in key_words:
            if key in word:
                return True
    return False
