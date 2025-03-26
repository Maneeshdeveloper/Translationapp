import firebase_admin
from firebase_admin import credentials, db
import speech_recognition as sr
from gtts import gTTS
import os
import tempfile
from pydub import AudioSegment
from pydub.playback import play
import threading
import queue
import time
import sys
from deep_translator import GoogleTranslator

# Initialize Firebase
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred, {'databaseURL': "https://gamma-18df8-default-rtdb.firebaseio.com/"})

# Global termination flag
terminate_flag = False
speak_queue = queue.Queue()

# User Input: Get User ID
user_id = input("Enter your User ID: ").strip()

# Fetch user's language from Firebase
def get_user_language(user_id):
    """Retrieve the preferred language of the user from Firebase."""
    user_data = db.reference(f"users/{user_id}").get()
    if user_data and "language" in user_data:
        return user_data["language"]
    else:
        print("⚠️ Language not found for user. Defaulting to English.")
        return "en"

# Ask if the user wants to initiate or wait for connection
initiate = input("Do you want to initiate a connection? (yes/no): ").strip().lower()

conn_key = None
target_id = None

if initiate == "yes":
    target_id = input("Enter Target User ID: ").strip()
    conn_key = f"{user_id}_{target_id}"
    
    # Send connection request
    db.reference(f"active_connections/{conn_key}").set({"accepted": False, "terminate": False})
    print(f"📨 Connection request sent to {target_id}... Waiting for acceptance.")

    # Wait for acceptance
    while not db.reference(f"active_connections/{conn_key}/accepted").get():
        time.sleep(1)
    
    print(f"✅ Connection established with {target_id}!")

elif initiate == "no":
    print("⏳ Waiting for connection requests...")
    
    while True:
        connections = db.reference("active_connections").get()
        if connections:
            for key, value in connections.items():
                if user_id in key and not value.get("accepted", False):
                    target_id = key.replace(user_id, "").replace("_", "")
                    conn_key = key
                    db.reference(f"active_connections/{conn_key}/accepted").set(True)
                    print(f"✅ Connection established with {target_id}!")
                    break
        if conn_key:
            break
        time.sleep(1)

else:
    print("Invalid input! Exiting...")
    sys.exit(0)

# Define chat paths
chat_send_path = f"chats/{user_id}_{target_id}"
chat_receive_path = f"chats/{target_id}_{user_id}"
conn_ref = db.reference(f"active_connections/{conn_key}")

# Get language preferences for both users
source_lang = get_user_language(user_id)  # Current user's language
target_lang = get_user_language(target_id)  # Connected user's language

print(f"🗣 Your language: {source_lang} | 🎧 Target's language: {target_lang}")

def translate_text(text, target_language):
    """Translate the given text to the target language."""
    if target_language == source_lang:  # No translation needed if both users have the same language
        return text
    return GoogleTranslator(source='auto', target=target_language).translate(text)

def speak(text, lang):
    """Convert text to speech and play it in the specified language."""
    if terminate_flag:
        return
    
    tts = gTTS(text=text, lang=lang)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as temp_file:
        temp_filename = temp_file.name  
    
    try:
        tts.save(temp_filename)
        audio = AudioSegment.from_file(temp_filename, format="mp3")
        play(audio)
    finally:
        if os.path.exists(temp_filename):
            os.unlink(temp_filename)

def listen_and_send():
    """Continuously listen for speech, translate, and send it to Firebase."""
    global terminate_flag
    
    while not terminate_flag:
        if conn_ref.get() and conn_ref.get().get("terminate", False):
            terminate_call()
            return
        
        with sr.Microphone() as source:
            recognizer = sr.Recognizer()
            recognizer.adjust_for_ambient_noise(source)
            print(f"🎙 Listening in {source_lang}...")
            try:
                audio = recognizer.listen(source, timeout=1, phrase_time_limit=5)
                original_text = recognizer.recognize_google(audio, language=source_lang)
                print(f"📝 Recognized: {original_text}")

                # Translate to target's language before sending
                translated_text = translate_text(original_text, target_lang)
                print(f"🔄 Translated to {target_lang}: {translated_text}")

                if original_text.lower() in ["terminate", f"terminate {user_id}"]:
                    print("🚨 Termination command detected. Ending chat...")
                    conn_ref.update({"terminate": True})
                    terminate_call()
                    return
                
                chat_ref = db.reference(chat_send_path).push()
                chat_ref.set({
                    "original_text": original_text,
                    "translated_text": translated_text,
                    "sender": user_id,
                    "timestamp": time.time()
                })
                print(f"📤 Sent: {translated_text}")
            except (sr.UnknownValueError, sr.RequestError, sr.WaitTimeoutError):
                pass

def fetch_and_process_messages():
    """Continuously fetch, translate (if needed), and play incoming messages."""
    global terminate_flag
    last_processed_time = 0  

    while not terminate_flag:
        try:
            conn_data = conn_ref.get()
            if conn_data is None or conn_data.get("terminate", False):
                terminate_call()
                return

            messages = db.reference(chat_receive_path).order_by_child("timestamp").start_at(last_processed_time).get()
            if messages:
                sorted_messages = sorted(messages.items(), key=lambda x: x[1]["timestamp"])
                for _, message_data in sorted_messages:
                    sender = message_data.get("sender", "")
                    received_text = message_data.get("translated_text", "")  # Fetch translated text
                    timestamp = message_data.get("timestamp", 0)
                    if timestamp > last_processed_time and sender != user_id:
                        print(f"📥 Received from {target_id}: {received_text}")

                        # Translate received message to the current user's language
                        final_text = translate_text(received_text, source_lang)
                        print(f"🔄 Translated to {source_lang}: {final_text}")

                        speak(final_text, source_lang)
                        last_processed_time = timestamp 
        except Exception as e:
            print(f"Error fetching messages: {e}")
        time.sleep(0.1)

def terminate_call():
    """Terminate the call and clean up Firebase."""
    global terminate_flag
    terminate_flag = True
    print("\n🚨 Call terminated. Cleaning up Firebase data...")
    db.reference(f"chats/{user_id}_{target_id}").delete()
    db.reference(f"chats/{target_id}_{user_id}").delete()
    db.reference(f"active_connections/{conn_key}").delete()
    print("✅ Chat history and active connection deleted. Exiting...")
    
    speak_queue.put("TERMINATE") # Stop speech output processing
    sys.exit(0)

# Start threads
threading.Thread(target=listen_and_send, daemon=True).start()
threading.Thread(target=fetch_and_process_messages, daemon=True).start()

while not terminate_flag:
    time.sleep(1)
