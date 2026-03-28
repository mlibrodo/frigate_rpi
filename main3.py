import os
import requests

API_KEY = os.environ["ROBOFLOW_API_KEY"]
MODEL_ID = "ember-training-poc/1"
IMAGE_PATH = "/home/librodo112/Downloads/OIP-3154093064.jpg"

with open(IMAGE_PATH, "rb") as f:
    response = requests.post(
        f"http://localhost:9001/{MODEL_ID}",
        params={"api_key": API_KEY},
        files={"file": ("image.jpg", f, "image/jpeg")},
    )

result = response.json()
print("raw response:", result)
print("predictions:", len(result.get("predictions", [])))
