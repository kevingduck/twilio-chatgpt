from flask import Flask, render_template, jsonify, request, redirect, url_for
from twilio.rest import Client
import os
from dotenv import load_dotenv

app = Flask(__name__)

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/get_numbers/<area_code>')
def get_numbers(area_code):
    load_dotenv()

    account_sid = os.getenv('TWILIO_ACCOUNT_SID')
    auth_token = os.getenv('TWILIO_AUTH_TOKEN')
    client = Client(account_sid, auth_token)

    numbers = client.available_phone_numbers('US').local.list(area_code=area_code, limit=10)
    return jsonify([str(number.phone_number) for number in numbers])


@app.route('/setup_number', methods=['POST'])
def setup_number():
    load_dotenv()

    account_sid = os.getenv('TWILIO_ACCOUNT_SID')
    auth_token = os.getenv('TWILIO_AUTH_TOKEN')
    voice_url = os.getenv('VOICE_URL') # Get the voice_url from environment variables
    client = Client(account_sid, auth_token)

    print(request.get_json())

    data = request.get_json()
    number = data['number']

    # Use Twilio's API to purchase and configure the number
    client.incoming_phone_numbers.create(
        voice_url=voice_url,
        phone_number=number)
    return jsonify({'status': 'success'}), 200

@app.route('/provision_number', methods=['POST'])
def provision_number():
    load_dotenv()

    account_sid = os.getenv('TWILIO_ACCOUNT_SID')
    auth_token = os.getenv('TWILIO_AUTH_TOKEN')
    client = Client(account_sid, auth_token)

    if request.is_json:
        data = request.get_json()
        number = data['number']
        
        # Use Twilio's API to purchase and configure the number
        client.incoming_phone_numbers.create(phone_number=number)
        return jsonify({'status': 'success'}), 200
    else:
        return jsonify({'status': 'error', 'message': 'Invalid request format'}), 400
    
if __name__ == '__main__':
    app.run(debug=True)