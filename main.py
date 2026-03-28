import os
from inference_sdk import InferenceHTTPClient
MODEL_ID = "ember-training-poc/1"  # project_id/version_id
client = InferenceHTTPClient(
    api_url="http://localhost:9001",
    api_key=os.environ["ROBOFLOW_API_KEY"],
)
# Use a LOCAL image file here to avoid any internet dependency in the request itself
result = client.infer("/home/librodo112/Downloads/OIP-3154093064.jpg", model_id=MODEL_ID)
print("predictions:", len(result.get("predictions", [])))

