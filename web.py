import os
import json

from flask import Flask, render_template
# from flask_socketio import SocketIO
from pymongo import MongoClient
from dotenv_vault import load_dotenv

load_dotenv()

app = Flask(__name__)

# Connect to MongoDB
login = os.getenv('mongodb_login')
pwd = os.getenv('mongodb_pwd')
address = os.getenv('mongodb_address')
port = os.getenv('mongodb_port')
mongo_uri = f'{address}:{port}'
client = MongoClient(f'mongodb://{login}:{pwd}@{mongo_uri}/')


@app.route('/')
def index():
    collection = client['news']['news']
    records = collection.find().sort('parsing_dttm', -1).limit(20)
    return render_template('index.html', records=records)


def send_update():
    collection = client['news']['news']
    records = collection.find().sort('parsing_dttm', -1).limit(20)
    records_json = json.dumps([{
        "title": record.get('title',''),
        "summary": record.get('summary',''),
        "text": record.get('text',''),
        "source": record.get('source',''),
        "link": record.get('link',''),
        "parsing_dttm": record.get('parsing_dttm','').strftime('%Y-%m-%d %H:%M:%S')
    } for record in records])
    app.emit('update', records_json, broadcast=True)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)