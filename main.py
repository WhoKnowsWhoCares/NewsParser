import os
import sys
import httpx
import asyncio
# import importlib.util
# import sys
# import glob

from collections import deque
from telethon import TelegramClient
from loguru import logger
from dotenv_vault import load_dotenv
from pymongo import MongoClient

from src.utils.utils import get_history, send_error_message

# Load environment variables
load_dotenv()

# Load all modules from parsers.
from src.parsers.telegram_parser import telegram_parser
from src.parsers.rss_parser import rss_parser
from src.parsers.bcs_parser import bcs_parser

# TODO: Make all parsers with general structure to load it dynamicly
# modules = glob.glob('src/parsers/*.py')
# for module in modules:
#     name = module.split('/')[-1].split('.')[0]
#     spec = importlib.util.spec_from_file_location(name, module)
#     lib = importlib.util.module_from_spec(spec)
#     sys.modules[name] = lib
#     spec.loader.exec_module(lib)

logger.remove()
logger.add("./logs/log_{time:YYYY-MM-DD}.log", level='INFO',
           rotation="00:01", retention="90 days",
           backtrace=True, diagnose=True)
logger.add(sys.stdout, level='INFO')

###########################
# Можно добавить телеграм канал, rss ссылку или изменить фильтр новостей

telegram_channels = {
    1099860397: 'https://t.me/rbc_news',
    # 1428717522: 'https://t.me/gazprom',
    # 1101170442: 'https://t.me/rian_ru',
    # 1133408457: 'https://t.me/prime1',
    # 1149896996: 'https://t.me/interfaxonline',
    # 1001029560: 'https://t.me/bcs_express',
    # 1203560567: 'https://t.me/markettwits',  # Канал аггрегатор новостей
}

rss_channels = {
    # 'www.rbc.ru': 'https://rssexport.rbc.ru/rbcnews/news/20/full.rss',
    'www.ria.ru': 'https://ria.ru/export/rss2/archive/index.xml',
    # 'www.1prime.ru': 'https://1prime.ru/export/rss2/index.xml',
    # 'www.interfax.ru': 'https://www.interfax.ru/rss.asp',
}


def check_pattern_func(text):
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

# Connect to MongoDB
login = os.getenv('mongodb_login')
pwd = os.getenv('mongodb_pwd')
address = os.getenv('mongodb_address')
port = os.getenv('mongodb_port')
client = MongoClient(f'mongodb://{login}:{pwd}@{address}:{port}/')
db = client['news']
collection = db['news']

def insert_record_w_check(collection, record_data):

    # Check if the key already exists in the collection
    existing_record = collection.find_one({'title': record_data['title']})
    if existing_record:
        logger.error("Record with this title already exists.")
        return
    
    # Insert the record if the key does not exist
    collection.insert_one(record_data)
    logger.info(f"Record inserted successfully. Title: {record_data['title']}")
    
###########################
# Если у парсеров много ошибок или появляются повторные новости

# 50 первых символов от поста - это ключ для поиска повторных постов
n_test_chars = 50

# Количество уже опубликованных постов, чтобы их не повторять
amount_messages = 50

# Очередь уже опубликованных постов
parsed_q = deque(maxlen=amount_messages)
posted_q = deque(maxlen=amount_messages)

# +/- интервал между запросами у rss и кастомного парсеров в секундах
timeout = 2

###########################
api_id = os.getenv('api_id')
api_hash = os.getenv('api_hash')
chat_id = os.getenv('chat_id')
bot_token = os.getenv('bot_token')
    
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# bot = TelegramClient('news_parser_bot', api_id, api_hash, base_logger=logger, loop=loop)
# bot.start(bot_token=bot_token)


# async def send_message_func(text):
#     '''Отправляет посты в канал через бот'''
#     await bot.send_message(entity=chat_id,
#                            parse_mode='html', link_preview=False, message=text)
#     logger.info(text)


# Телеграм парсер
# client = telegram_parser('news_parser', api_id, api_hash, telegram_channels, posted_q,
#                          n_test_chars=n_test_chars,logger=logger, loop=loop)

# Список из уже опубликованных постов, чтобы их не дублировать
history = loop.run_until_complete(
    get_history(client, chat_id, n_test_chars, amount_messages)
)
posted_q.extend(history)
httpx_client = httpx.AsyncClient()

# Добавляй в текущий event_loop rss парсеры
for source, rss_link in rss_channels.items():
    # https://docs.python-guide.org/writing/gotchas/#late-binding-closures
    async def rss_wrapper(source, rss_link):
        try:
            await rss_parser(httpx_client, source, rss_link, parsed_q, posted_q,
                             n_test_chars, timeout, check_pattern_func, logger)
        except Exception as e:
            message = f'&#9888; ERROR: {source} parser is down! \n{e}'
            await send_error_message(message, bot_token, chat_id, logger)
    loop.create_task(rss_wrapper(source, rss_link))


# Добавляй в текущий event_loop кастомный парсер
# async def bcs_wrapper():
#     try:
#         await bcs_parser(httpx_client, posted_q, n_test_chars, timeout,
#                          check_pattern_func, logger)
#     except Exception as e:
#         message = f'&#9888; ERROR: bcs-express.ru parser is down! \n{e}'
#         await send_error_message(message, bot_token, chat_id, logger)
# loop.create_task(bcs_wrapper())


try:
    client.run_until_disconnected()
except Exception as e:
    message = f'&#9888; ERROR: telegram parser (all parsers) is down! \n{e}'
    loop.run_until_complete(send_error_message(message, bot_token, chat_id, logger))
finally:
    loop.run_until_complete(httpx_client.aclose())
    loop.close()