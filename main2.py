import os
import base64
import requests

API_KEY = os.environ["ROBOFLOW_API_KEY"]
MODEL_ID = "ember-training-poc/1"
IMAGE_PATH = "/home/librodo112/Downloads/OIP-3154093064.jpg"

with open(IMAGE_PATH, "rb") as f:
    img_b64 = base64.b64encode(f.read()).decode("utf-8")

response = requests.post(
    f"http://localhost:9001/{MODEL_ID}",
    params={"api_key": API_KEY},
    json={"image": {"type": "base64", "value": img_b64}},
)

result = response.json()
print(result)
print("predictions:", len(result.get("predictions", [])))
