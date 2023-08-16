import os
from pymongo import MongoClient
from loguru import logger


def get_db_connection():
    # Connect to MongoDB
    mongo_uri = os.getenv('MONGO_DB_URI')
    if not mongo_uri:
        address = os.getenv('mongodb_address')
        port = os.getenv('mongodb_port')
        mongo_uri = f'{address}:{port}'
    login = os.getenv('mongodb_login')
    pwd = os.getenv('mongodb_pwd')
    logger.debug(f'Connecting to MongoDB: {mongo_uri}')
    client = MongoClient(f'mongodb://{login}:{pwd}@{mongo_uri}/')
    return client
    

def insert_record_into_db(client, record_data):
    logger.debug('Inserting record into MongoDB')
    db = client['news']
    collection = db['news']
    # Check if the key already exists in the collection
    existing_record = collection.find_one({'title': record_data['title']})
    if existing_record:
        logger.error("Record with this title already exists.")
        return
    
    # Insert the record if the key does not exist
    collection.insert_one(record_data)
    logger.debug(f"Record inserted successfully. Title: {record_data['title']}")
    return 0
    
    
def get_history(connection, n_test_chars=50, amount_messages=50):
    '''Забирает из канала уже опубликованные посты для того, чтобы их не дублировать'''
    db = connection['news']
    collection = db['news']
    history = []
    messages = collection.find().sort('parsing_dttm', -1).limit(amount_messages)
    for message in messages:
        text = message.get('title','')
        history.append(text)
    return history
