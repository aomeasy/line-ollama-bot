import os
import requests
from flask import Flask, request, abort

from linebot import (
    LineBotApi, WebhookHandler
)
from linebot.exceptions import (
    InvalidSignatureError
)
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
)

# Initialize Flask app
app = Flask(__name__)

# --- Environment Variables ---
# Get LINE credentials from environment variables for security.
# These must be set on your Render dashboard.
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')

# Get Ollama server and model details from environment variables.
# OLLAMA_API_URL should be your server's IP address.
# OLLAMA_MODEL should be the model name you want to use.
OLLAMA_API_URL = os.getenv('OLLAMA_API_URL')
OLLAMA_MODEL = os.getenv('OLLAMA_MODEL')

# Check if required variables are set
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, OLLAMA_API_URL, OLLAMA_MODEL]):
    print("Error: Please set all required environment variables.")
    # Exit if variables are missing to prevent runtime errors.
    exit(1)

# Initialize LINE Bot API and Webhook Handler
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

@app.route("/callback", methods=['POST'])
def callback():
    """
    Endpoint for LINE's webhook.
    Receives messages from LINE, verifies the signature, and passes it to the handler.
    """
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Please check your channel access token or channel secret.")
        abort(400) # Returns a 400 Bad Request error if the signature is invalid.
    
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """
    Handles incoming text messages from LINE.
    """
    user_message = event.message.text
    print(f"Received message: {user_message}")

    # --- Ollama API Call ---
    # Prepare the payload to send to the Ollama API
    ollama_payload = {
        "model": OLLAMA_MODEL,
        "prompt": user_message,
        "stream": False # Set to False for a single, complete response.
    }
    
    try:
        # Use requests.post to send the message to the Ollama server
        response = requests.post(f"{OLLAMA_API_URL}/api/generate", json=ollama_payload)
        response.raise_for_status() # Raises an exception for bad status codes (4xx or 5xx)
        
        ollama_response_data = response.json()
        generated_text = ollama_response_data.get("response", "ขออภัย, ไม่สามารถสร้างคำตอบได้ในขณะนี้")
        
        print(f"Ollama responded: {generated_text}")

    except requests.exceptions.RequestException as e:
        print(f"Error calling Ollama API: {e}")
        generated_text = "ขออภัย, เกิดข้อผิดพลาดในการเชื่อมต่อกับ AI"

    # --- LINE Reply ---
    # Reply to the user on LINE with the generated text.
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=generated_text)
    )

if __name__ == "__main__":
    # Get the port from the environment variable (Render sets this automatically)
    port = int(os.environ.get('PORT', 5000))
    # Run the Flask app on all available network interfaces.
    app.run(host='0.0.0.0', port=port)
