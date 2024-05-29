from flask import Flask, render_template, jsonify
from twilio.rest import Client
import os
from dotenv import load_dotenv

app = Flask(__name__)

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/get_numbers')
def get_numbers():
    load_dotenv()

    account_sid = os.getenv('TWILIO_ACCOUNT_SID')
    auth_token = os.getenv('TWILIO_AUTH_TOKEN')
    client = Client(account_sid, auth_token)

    numbers = client.available_phone_numbers('US').local.list(limit=10)
    return jsonify([str(number.phone_number) for number in numbers])

if __name__ == '__main__':
    app.run(debug=True)